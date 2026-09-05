# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Exact lifecycle factors and vendor-neutral grid intensity contract tests."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from fractions import Fraction
from typing import TYPE_CHECKING

import pytest
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_registry import RegistryEntryDisabler
from homeassistant.util import dt as dt_util

from custom_components.co2saver.config_factors import (
    GRID_INTENSITY_UNITS,
    GridIntensitySample,
    HomeAssistantGridIntensityReader,
    canonical_decimal,
    grid_intensity_selector,
    grid_sample_time_error,
    validate_factor_selection,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


_NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def freeze_validation_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep freshness boundaries deterministic without sleeping."""
    monkeypatch.setattr(dt_util, "utcnow", lambda: _NOW)


def _source(hass: HomeAssistant) -> tuple[str, dict[str, object]]:
    """Publish one valid arbitrary-provider CO₂ sensor."""
    entry = er.async_get(hass).async_get_or_create("sensor", "test", "grid_carbon")
    hass.states.async_set(
        entry.entity_id,
        "400.125",
        {ATTR_UNIT_OF_MEASUREMENT: "gCO2eq/kWh"},
        timestamp=_NOW.timestamp(),
    )
    return entry.entity_id, {
        "grid_intensity_source": entry.entity_id,
        "grid_max_age_minutes": 60,
        "pv_factor": "40.000",
    }


def test_factors_preserve_exact_explicit_values_and_stable_identity(
    hass: HomeAssistant,
) -> None:
    """Factor selection is detached, canonical, serializable, and side-effect free."""
    entity_id, values = _source(hass)
    values["battery_factor"] = "12.00000000000000000000000000000000000000001"
    original = deepcopy(values)
    factors, errors = validate_factor_selection(
        hass, with_battery=True, user_input=values
    )

    assert errors == {}
    assert factors == {
        "grid_intensity_source": er.async_get(hass).async_get(entity_id).id,
        "grid_max_age_minutes": 60,
        "pv_factor": "40",
        "battery_factor": values["battery_factor"],
    }
    assert json.loads(json.dumps(factors)) == factors
    assert values == original
    assert hass.config_entries.async_entries("co2saver") == []


@pytest.mark.parametrize(
    "factor", [0, "-0.000", "5000.000", "0.0000000000000000000000000001"]
)
def test_inclusive_factor_bounds_are_exact(hass: HomeAssistant, factor: object) -> None:
    """Zero and the inclusive upper bound are accepted without silent defaults."""
    _entity_id, values = _source(hass)
    values["pv_factor"] = factor
    factors, errors = validate_factor_selection(
        hass, with_battery=False, user_input=values
    )
    assert errors == {}
    assert factors is not None
    assert Fraction(factors["pv_factor"]) == Fraction(factor)
    assert "battery_factor" not in factors


@pytest.mark.parametrize("age", [1, 1440, 60.0, "60.000"])
def test_age_selector_values_are_safe_bounded_integers(
    hass: HomeAssistant, age: object
) -> None:
    """An integral NumberSelector float has an exact bounded integer meaning."""
    _entity_id, values = _source(hass)
    values["grid_max_age_minutes"] = age
    factors, errors = validate_factor_selection(
        hass, with_battery=False, user_input=values
    )
    assert errors == {}
    assert factors is not None
    assert type(factors["grid_max_age_minutes"]) is int


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("pv_factor", "-0.1", "factor_out_of_range"),
        ("pv_factor", "5000.000000000000000000001", "factor_out_of_range"),
        ("pv_factor", True, "invalid_number"),
        ("pv_factor", 1.5, "invalid_number"),
        ("pv_factor", Decimal(1), "invalid_number"),
        ("pv_factor", "NaN", "invalid_number"),
        ("pv_factor", "Infinity", "invalid_number"),
        ("pv_factor", "4e1", "invalid_number"),
        ("pv_factor", " 40", "invalid_number"),
        ("pv_factor", "4,5", "invalid_decimal_separator"),
        ("grid_max_age_minutes", 0, "grid_age_out_of_range"),
        ("grid_max_age_minutes", 1441, "grid_age_out_of_range"),
        ("grid_max_age_minutes", "1.0001", "grid_age_out_of_range"),
        ("grid_max_age_minutes", 1.5, "invalid_number"),
        ("grid_max_age_minutes", float("inf"), "invalid_number"),
        ("grid_max_age_minutes", True, "invalid_number"),
        ("grid_intensity_source", 3, "invalid_selection"),
        ("grid_intensity_source", " sensor.grid", "invalid_selection"),
        ("grid_intensity_source", "sensor.missing", "source_not_registered"),
        ("battery_factor", "12", "unexpected_field"),
    ],
)
def test_invalid_factor_inputs_fail_with_field_error(
    hass: HomeAssistant, field: str, value: object, error: str
) -> None:
    """Unsupported shape, precision, localization, and range never become defaults."""
    _entity_id, values = _source(hass)
    values[field] = value
    assert validate_factor_selection(hass, with_battery=False, user_input=values) == (
        None,
        {field: error},
    )


@pytest.mark.parametrize(
    "field",
    ["grid_intensity_source", "grid_max_age_minutes", "pv_factor", "battery_factor"],
)
def test_every_selected_parameter_is_required(hass: HomeAssistant, field: str) -> None:
    """No lifecycle factor or source may be inferred from missing input."""
    _entity_id, values = _source(hass)
    values["battery_factor"] = "12"
    del values[field]
    assert validate_factor_selection(hass, with_battery=True, user_input=values) == (
        None,
        {field: "required"},
    )


@pytest.mark.parametrize("unit", GRID_INTENSITY_UNITS)
def test_supported_gram_and_kilogram_units_normalize_exactly(
    hass: HomeAssistant, unit: str
) -> None:
    """All declared CO₂e spellings map onto exact grams without vendor assumptions."""
    entity_id, _values = _source(hass)
    entry = er.async_get(hass).async_get(entity_id)
    hass.states.async_set(
        entity_id,
        "0.400125" if unit.startswith("kg") else "400.125",
        {ATTR_UNIT_OF_MEASUREMENT: unit},
        timestamp=_NOW.timestamp(),
    )
    sample, error = HomeAssistantGridIntensityReader(hass, entry.id).read()
    assert error is None
    assert sample == GridIntensitySample(Fraction("400.125"), _NOW, entry.id)


@pytest.mark.parametrize(
    ("value", "unit", "error"),
    [
        ("unavailable", "gCO2eq/kWh", "source_unavailable"),
        ("unknown", "gCO2eq/kWh", "source_unavailable"),
        ("500", "ppm", "invalid_grid_unit"),
        ("400", "g/kWh", "invalid_grid_unit"),
        ("400", None, "invalid_grid_unit"),
        ("NaN", "gCO2eq/kWh", "invalid_grid_value"),
        ("infinity", "gCO2eq/kWh", "invalid_grid_value"),
        ("-1", "gCO2eq/kWh", "invalid_grid_value"),
        ("5.000000000000000000001", "kgCO2e/kWh", "invalid_grid_value"),
    ],
)
def test_grid_sensor_values_cannot_fake_usable_intensity(
    hass: HomeAssistant, value: str, unit: object, error: str
) -> None:
    """Room-air concentration, nonfinite state, and out-of-range data are rejected."""
    entity_id, values = _source(hass)
    hass.states.async_set(
        entity_id, value, {ATTR_UNIT_OF_MEASUREMENT: unit}, timestamp=_NOW.timestamp()
    )
    assert validate_factor_selection(hass, with_battery=False, user_input=values) == (
        None,
        {"grid_intensity_source": error},
    )


@pytest.mark.parametrize(
    ("offset", "error"),
    [
        (timedelta(minutes=-60), None),
        (timedelta(minutes=-60, microseconds=-1), "grid_source_stale"),
        (timedelta(microseconds=1), "future_last_reported"),
    ],
)
def test_grid_publication_time_has_exact_inclusive_freshness(
    hass: HomeAssistant, offset: timedelta, error: str | None
) -> None:
    """Freshness uses last_reported and excludes future publications."""
    entity_id, values = _source(hass)
    state = hass.states.get(entity_id)
    state.last_reported = _NOW + offset
    _parameters, errors = validate_factor_selection(
        hass, with_battery=False, user_input=values
    )
    assert errors == ({"grid_intensity_source": error} if error else {})


def test_grid_reader_copies_reported_time_and_survives_entity_rename(
    hass: HomeAssistant,
) -> None:
    """Repeated reports cannot mutate accepted samples or change source identity."""
    entity_id, _values = _source(hass)
    registry = er.async_get(hass)
    entry = registry.async_get(entity_id)
    reader = HomeAssistantGridIntensityReader(hass, entry.id)
    sample, error = reader.read()
    assert error is None
    state = hass.states.get(entity_id)
    state.last_reported = _NOW + timedelta(seconds=1)
    assert sample.observed_at == _NOW
    with pytest.raises(FrozenInstanceError):
        sample.observed_at = _NOW + timedelta(days=1)
    registry.async_update_entity(entity_id, new_entity_id="sensor.renamed_grid")
    hass.states.async_set(
        "sensor.renamed_grid",
        "400.125",
        {ATTR_UNIT_OF_MEASUREMENT: "gCO2eq/kWh"},
        timestamp=_NOW.timestamp(),
    )
    assert reader.read() == (sample, None)


def test_grid_source_lifecycle_errors_are_explicit(hass: HomeAssistant) -> None:
    """Removed, disabled, absent and wrong-domain sources remain distinguishable."""
    entity_id, values = _source(hass)
    registry = er.async_get(hass)
    entry = registry.async_get(entity_id)
    reader = HomeAssistantGridIntensityReader(hass, entry.id)
    hass.states.async_remove(entity_id)
    assert reader.read() == (None, "source_missing")
    registry.async_update_entity(entity_id, disabled_by=RegistryEntryDisabler.USER)
    assert reader.read() == (None, "source_disabled")
    assert validate_factor_selection(hass, with_battery=False, user_input=values)[
        1
    ] == {"grid_intensity_source": "source_disabled"}
    registry.async_remove(entity_id)
    assert reader.read() == (None, "source_not_registered")
    wrong = registry.async_get_or_create("number", "test", "wrong_domain")
    values["grid_intensity_source"] = wrong.id
    assert validate_factor_selection(hass, with_battery=False, user_input=values)[
        1
    ] == {"grid_intensity_source": "invalid_domain"}
    assert HomeAssistantGridIntensityReader(hass, wrong.id).read() == (
        None,
        "invalid_domain",
    )


def test_invalid_and_non_utc_publication_times_are_handled(hass: HomeAssistant) -> None:
    """Timezone-aware times normalize to UTC; naive timestamps cannot be guessed."""
    entity_id, values = _source(hass)
    state = hass.states.get(entity_id)
    state.last_reported = _NOW.replace(tzinfo=None)
    assert validate_factor_selection(hass, with_battery=False, user_input=values)[
        1
    ] == {"grid_intensity_source": "invalid_last_reported"}
    state.last_reported = _NOW.astimezone(timezone(timedelta(hours=2)))
    assert (
        validate_factor_selection(hass, with_battery=False, user_input=values)[1] == {}
    )


def test_public_helpers_reject_malformed_domain_inputs() -> None:
    """Manually supplied dates and bounds obey the same exact grid sample contract."""
    sample = GridIntensitySample(Fraction(400), _NOW, "registry-id")
    for age in (0, 1441, True):
        with pytest.raises(ValueError, match="maximum age"):
            grid_sample_time_error(sample, _NOW, age)
    with pytest.raises(ValueError, match="interval end"):
        grid_sample_time_error(sample, _NOW.replace(tzinfo=None), 60)
    with pytest.raises(ValueError, match="observed_at"):
        GridIntensitySample(Fraction(400), _NOW.replace(tzinfo=None), "id")
    with pytest.raises(ValueError, match="registry ID"):
        GridIntensitySample(Fraction(400), _NOW, " invalid ")
    with pytest.raises(ValueError, match="finite"):
        canonical_decimal(Decimal("NaN"))
    assert grid_intensity_selector().config["filter"] == [
        {"domain": ["sensor"], "unit_of_measurement": list(GRID_INTENSITY_UNITS)}
    ]
