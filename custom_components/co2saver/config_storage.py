# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Validate an optional battery's cumulative energy inputs and parameters."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import TYPE_CHECKING, TypedDict

from custom_components.co2saver.config_sources import validate_energy_sources

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.core import HomeAssistant


_BATTERY_CHARGE = "battery_charge"
_BATTERY_DISCHARGE = "battery_discharge"
_USABLE_CAPACITY_KWH = "usable_capacity_kwh"
_ROUND_TRIP_EFFICIENCY_PERCENT = "round_trip_efficiency_percent"
_SOURCE_FIELDS = (_BATTERY_CHARGE, _BATTERY_DISCHARGE)
_NUMBER_FIELDS = (_USABLE_CAPACITY_KWH, _ROUND_TRIP_EFFICIENCY_PERCENT)
_ALLOWED_FIELDS = frozenset((*_SOURCE_FIELDS, *_NUMBER_FIELDS))
_PLAIN_DECIMAL = re.compile(r"-?[0-9]+(?:\.[0-9]+)?\Z")
_MINIMUM_CAPACITY_KWH = Decimal("0.1")
_MAXIMUM_CAPACITY_KWH = Decimal(1000)
_MINIMUM_EFFICIENCY_PERCENT = Decimal(0)
_MAXIMUM_EFFICIENCY_PERCENT = Decimal(100)


class StorageParameters(TypedDict):
    """Serializable, canonical battery configuration passed to later steps."""

    charge_source: str
    discharge_source: str
    usable_capacity_kwh: str
    round_trip_efficiency: str


def _parse_decimal(value: object) -> tuple[Decimal | None, str | None]:
    """Parse an exact UI decimal without admitting binary floating point."""
    if type(value) is int:
        return Decimal(value), None
    if type(value) is not str:
        return None, "invalid_number"
    if "," in value:
        return None, "invalid_decimal_separator"
    if _PLAIN_DECIMAL.fullmatch(value) is None:
        return None, "invalid_number"
    return Decimal(value), None


def _canonical_decimal(value: Decimal) -> str:
    """Format a finite Decimal exactly, without context rounding or exponent."""
    canonical = format(value, "f")
    if "." in canonical:
        canonical = canonical.rstrip("0").rstrip(".")
    return canonical


def _ratio_from_percent(value: Decimal) -> str:
    """Shift a percentage exactly by two decimal places."""
    sign, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int):  # pragma: no cover - parser guarantees finite
        message = "efficiency percentage must be finite"
        raise TypeError(message)
    return _canonical_decimal(Decimal((sign, digits, exponent - 2)))


def _shape_and_number_errors(
    user_input: Mapping[str, object],
) -> tuple[dict[str, Decimal], dict[str, str]]:
    """Validate the exact storage-step shape and scalar boundaries."""
    errors = {
        field: "unexpected_field"
        for field in user_input
        if field not in _ALLOWED_FIELDS
    }

    for field in _SOURCE_FIELDS:
        value = user_input.get(field)
        if value is None or value == "":
            errors[field] = "required"
        elif type(value) is not str or value != value.strip():
            errors[field] = "invalid_selection"

    numbers: dict[str, Decimal] = {}
    for field in _NUMBER_FIELDS:
        value = user_input.get(field)
        if value is None or value == "":
            errors[field] = "required"
            continue
        parsed, error = _parse_decimal(value)
        if parsed is None:
            errors[field] = error or "invalid_number"
            continue
        numbers[field] = parsed

    capacity = numbers.get(_USABLE_CAPACITY_KWH)
    if capacity is not None and not (
        _MINIMUM_CAPACITY_KWH <= capacity <= _MAXIMUM_CAPACITY_KWH
    ):
        errors[_USABLE_CAPACITY_KWH] = "capacity_out_of_range"

    efficiency = numbers.get(_ROUND_TRIP_EFFICIENCY_PERCENT)
    if efficiency is not None and not (
        _MINIMUM_EFFICIENCY_PERCENT < efficiency <= _MAXIMUM_EFFICIENCY_PERCENT
    ):
        errors[_ROUND_TRIP_EFFICIENCY_PERCENT] = "efficiency_out_of_range"
    return numbers, errors


def _storage_source_errors(source_errors: Mapping[str, str]) -> dict[str, str]:
    """Keep battery field errors visible and summarize earlier-step failures."""
    errors = {
        role: error for role, error in source_errors.items() if role in _SOURCE_FIELDS
    }
    if any(role not in _SOURCE_FIELDS for role in source_errors):
        errors["base"] = "invalid_source_vector"
    return errors


def validate_storage_selection(
    hass: HomeAssistant,
    existing_sources: Mapping[str, str],
    user_input: Mapping[str, object],
) -> tuple[StorageParameters | None, dict[str, str]]:
    """Validate one battery draft without storing or mutating configuration."""
    numbers, errors = _shape_and_number_errors(user_input)
    if errors:
        return None, errors
    if not existing_sources or any(role in existing_sources for role in _SOURCE_FIELDS):
        return None, {"base": "invalid_source_vector"}

    selections: dict[str, object] = dict(existing_sources)
    selections.update(
        {
            _BATTERY_CHARGE: user_input[_BATTERY_CHARGE],
            _BATTERY_DISCHARGE: user_input[_BATTERY_DISCHARGE],
        }
    )
    resolved, source_errors = validate_energy_sources(hass, selections)
    if source_errors:
        return None, _storage_source_errors(source_errors)
    if resolved is None:  # pragma: no cover - success contract of shared validator
        return None, {"base": "invalid_source_vector"}

    capacity = numbers[_USABLE_CAPACITY_KWH]
    efficiency = numbers[_ROUND_TRIP_EFFICIENCY_PERCENT]
    return (
        StorageParameters(
            charge_source=resolved[_BATTERY_CHARGE],
            discharge_source=resolved[_BATTERY_DISCHARGE],
            usable_capacity_kwh=_canonical_decimal(capacity),
            round_trip_efficiency=_ratio_from_percent(efficiency),
        ),
        {},
    )


__all__ = ("StorageParameters", "validate_storage_selection")
