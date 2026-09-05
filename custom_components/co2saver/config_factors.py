# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Validate exact lifecycle factors and registry-bound grid intensity samples."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, NotRequired, TypedDict

from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    EntityWithDeviceFilterSelectorConfig,
)
from homeassistant.util import dt as dt_util

from .domain import DomainValidationError, EmissionFactor

if TYPE_CHECKING:
    from collections.abc import Mapping
    from fractions import Fraction

    from homeassistant.core import HomeAssistant


GRID_INTENSITY_UNITS = tuple(
    f"{prefix}{separator}{suffix}/kWh"
    for prefix in ("g", "kg")
    for separator in ("", " ")
    for suffix in ("CO2eq", "CO2e", "CO₂e")
)
_PLAIN_DECIMAL = re.compile(r"-?[0-9]+(?:\.[0-9]+)?\Z")
_MAX_FACTOR = Decimal(5000)
_MAX_AGE = Decimal(1440)
_SOURCE_FIELD = "grid_intensity_source"
_AGE_FIELD = "grid_max_age_minutes"


class FactorParameters(TypedDict):
    """Serializable factors with exact decimals and stable source identity."""

    grid_intensity_source: str
    grid_max_age_minutes: int
    pv_factor: str
    battery_factor: NotRequired[str]


@dataclass(frozen=True, slots=True)
class GridIntensitySample:
    """One immutable, exact sample copied from a Home Assistant state."""

    value_g_co2e_per_kwh: Fraction
    observed_at: datetime
    source_registry_id: str

    def __post_init__(self) -> None:
        """Reject malformed manually constructed intensity samples."""
        EmissionFactor(self.value_g_co2e_per_kwh)
        if self.observed_at.tzinfo is not UTC:
            message = "grid intensity observed_at must use canonical UTC"
            raise ValueError(message)
        if (
            type(self.source_registry_id) is not str
            or not self.source_registry_id
            or self.source_registry_id != self.source_registry_id.strip()
        ):
            message = "grid intensity must have a stable source registry ID"
            raise ValueError(message)


def parse_exact_decimal(value: object) -> tuple[Decimal | None, str | None]:
    """Accept plain decimal UI strings and integers without float coercion."""
    if type(value) is int:
        return Decimal(value), None
    if type(value) is not str:
        return None, "invalid_number"
    if "," in value:
        return None, "invalid_decimal_separator"
    if _PLAIN_DECIMAL.fullmatch(value) is None:
        return None, "invalid_number"
    return Decimal(value), None


def canonical_decimal(value: Decimal) -> str:
    """Format exact finite decimals without context-dependent rounding."""
    if not value.is_finite():
        message = "canonical decimal must be finite"
        raise ValueError(message)
    if value.is_zero():
        return "0"
    result = format(value, "f")
    return result.rstrip("0").rstrip(".") if "." in result else result


def grid_intensity_selector() -> EntitySelector:
    """Offer compatible units independently of vendor and device class."""
    return EntitySelector(
        EntitySelectorConfig(
            filter=EntityWithDeviceFilterSelectorConfig(
                domain="sensor", unit_of_measurement=list(GRID_INTENSITY_UNITS)
            )
        )
    )


def _registry_source(
    hass: HomeAssistant, selection: object
) -> tuple[str | None, str | None]:
    """Resolve an entity ID or registry UUID without retaining display identity."""
    if selection is None or selection == "":
        return None, "required"
    if type(selection) is not str or selection != selection.strip():
        return None, "invalid_selection"
    entry = er.async_get(hass).async_get(selection)
    if entry is None:
        return None, "source_not_registered"
    if entry.domain != "sensor":
        return None, "invalid_domain"
    if entry.disabled:
        return None, "source_disabled"
    return entry.id, None


class HomeAssistantGridIntensityReader:
    """Resolve a registry identity and synchronously copy its current CO₂ sample."""

    def __init__(self, hass: HomeAssistant, source_registry_id: str) -> None:
        """Bind the reader without reading, retaining, or subscribing to state."""
        self._hass = hass
        self._source_registry_id = source_registry_id

    def read(self) -> tuple[GridIntensitySample | None, str | None]:
        """Copy state scalars once and reject unsupported or unavailable data."""
        entry = er.async_get(self._hass).async_get(self._source_registry_id)
        if entry is None or entry.id != self._source_registry_id:
            return None, "source_not_registered"
        if entry.domain != "sensor":
            return None, "invalid_domain"
        if entry.disabled:
            return None, "source_disabled"
        state = self._hass.states.get(entry.entity_id)
        if state is None:
            return None, "source_missing"
        value = state.state
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        reported = state.last_reported
        return _normalize_grid_sample(value, unit, reported, self._source_registry_id)


def _normalize_grid_sample(
    value: str, unit: object, reported: object, registry_id: str
) -> tuple[GridIntensitySample | None, str | None]:
    """Normalize copied scalar values without keeping the mutable State object."""
    if value in (STATE_UNKNOWN, STATE_UNAVAILABLE):
        return None, "source_unavailable"
    if type(unit) is not str or unit not in GRID_INTENSITY_UNITS:
        return None, "invalid_grid_unit"
    try:
        factor = (
            EmissionFactor.from_kg_per_kwh(value)
            if unit.startswith("kg")
            else EmissionFactor.from_g_per_kwh(value)
        )
    except DomainValidationError:
        return None, "invalid_grid_value"
    if (
        not isinstance(reported, datetime)
        or reported.tzinfo is None
        or reported.utcoffset() is None
    ):
        return None, "invalid_last_reported"
    try:
        observed_at = reported.astimezone(UTC)
    except OverflowError, ValueError:
        return None, "invalid_last_reported"
    return GridIntensitySample(factor.grams_per_kwh, observed_at, registry_id), None


def grid_sample_time_error(
    sample: GridIntensitySample, interval_end: datetime, max_age_minutes: int
) -> str | None:
    """Apply the inclusive freshness window at the consumption/discharge time."""
    if interval_end.tzinfo is not UTC:
        message = "grid intensity interval end must use canonical UTC"
        raise ValueError(message)
    if type(max_age_minutes) is not int or not 1 <= max_age_minutes <= _MAX_AGE:
        message = "grid intensity maximum age must be 1 through 1440 minutes"
        raise ValueError(message)
    if sample.observed_at > interval_end:
        return "future_last_reported"
    if interval_end - sample.observed_at > timedelta(minutes=max_age_minutes):
        return "grid_source_stale"
    return None


def _numeric_parameters(
    user_input: Mapping[str, object], *, with_battery: bool
) -> tuple[dict[str, Decimal], dict[str, str]]:
    """Enforce explicit lifecycle inputs, supported ranges, and exact field shape."""
    number_fields = [_AGE_FIELD, "pv_factor"]
    if with_battery:
        number_fields.append("battery_factor")
    allowed = {_SOURCE_FIELD, *number_fields}
    errors = {key: "unexpected_field" for key in user_input if key not in allowed}
    numbers: dict[str, Decimal] = {}
    for field in number_fields:
        raw = user_input.get(field)
        if raw is None or raw == "":
            errors[field] = "required"
            continue
        if field == _AGE_FIELD and type(raw) is float and raw.is_integer():
            raw = int(raw)
        value, error = parse_exact_decimal(raw)
        if value is None:
            errors[field] = error or "invalid_number"
            continue
        if field == _AGE_FIELD:
            if not 1 <= value <= _MAX_AGE or value != value.to_integral_value():
                errors[field] = "grid_age_out_of_range"
        elif not 0 <= value <= _MAX_FACTOR:
            errors[field] = "factor_out_of_range"
        numbers[field] = value
    return numbers, errors


def validate_factor_selection(
    hass: HomeAssistant,
    with_battery: bool,  # noqa: FBT001 - established config boundary API
    user_input: Mapping[str, object],
) -> tuple[FactorParameters | None, dict[str, str]]:
    """Return exact factor parameters after validating the live grid source."""
    if type(with_battery) is not bool:
        return None, {"base": "invalid_measurement_plan"}
    numbers, errors = _numeric_parameters(user_input, with_battery=with_battery)
    registry_id, source_error = _registry_source(hass, user_input.get(_SOURCE_FIELD))
    if source_error is not None:
        errors[_SOURCE_FIELD] = source_error
    if errors:
        return None, errors
    if registry_id is None:  # pragma: no cover - successful resolution contract
        return None, {"base": "invalid_measurement_plan"}
    sample, source_error = HomeAssistantGridIntensityReader(hass, registry_id).read()
    max_age = int(numbers[_AGE_FIELD])
    if sample is not None:
        source_error = grid_sample_time_error(sample, dt_util.utcnow(), max_age)
    if source_error is not None:
        return None, {_SOURCE_FIELD: source_error}
    parameters = FactorParameters(
        grid_intensity_source=registry_id,
        grid_max_age_minutes=max_age,
        pv_factor=canonical_decimal(numbers["pv_factor"]),
    )
    if with_battery:
        parameters["battery_factor"] = canonical_decimal(numbers["battery_factor"])
    return parameters, {}


__all__ = (
    "GRID_INTENSITY_UNITS",
    "FactorParameters",
    "GridIntensitySample",
    "HomeAssistantGridIntensityReader",
    "canonical_decimal",
    "grid_intensity_selector",
    "grid_sample_time_error",
    "parse_exact_decimal",
    "validate_factor_selection",
)
