# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Tests for side-effect-free battery source and parameter validation."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from homeassistant.components.sensor import (
    ATTR_STATE_CLASS,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import ATTR_DEVICE_CLASS, ATTR_UNIT_OF_MEASUREMENT
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_registry import RegistryEntryDisabler
from homeassistant.util import dt as dt_util

from custom_components.co2saver.config_storage import validate_storage_selection
from custom_components.co2saver.const import ATTR_CO2SAVER_PERIOD_END, DOMAIN

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_registry import RegistryEntry


_NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)
_PERIOD_END = _NOW - timedelta(minutes=1)
_LAST_REPORTED = _PERIOD_END + timedelta(seconds=30)


@pytest.fixture(autouse=True)
def freeze_validation_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep current-vector validation deterministic."""
    monkeypatch.setattr(dt_util, "utcnow", lambda: _NOW)


def _publish_energy_state(  # noqa: PLR0913
    hass: HomeAssistant,
    entity_id: str,
    *,
    value: object = "100",
    unit: object = "kWh",
    device_class: object = SensorDeviceClass.ENERGY,
    state_class: object = SensorStateClass.TOTAL_INCREASING,
    period_end: datetime = _PERIOD_END,
    reported_at: datetime = _LAST_REPORTED,
) -> None:
    """Publish one current cumulative energy reading."""
    hass.states.async_set(
        entity_id,
        str(value),
        {
            ATTR_DEVICE_CLASS: device_class,
            ATTR_STATE_CLASS: state_class,
            ATTR_UNIT_OF_MEASUREMENT: unit,
            ATTR_CO2SAVER_PERIOD_END: period_end,
        },
        timestamp=reported_at.timestamp(),
    )


def _register_source(
    hass: HomeAssistant,
    role: str,
    *,
    domain: str = "sensor",
    disabled: bool = False,
) -> RegistryEntry:
    """Create one registry source with a valid synchronized state."""
    registry = er.async_get(hass)
    entry = registry.async_get_or_create(
        domain,
        "storage_test",
        role,
        suggested_object_id=role,
    )
    if disabled:
        entry = registry.async_update_entity(
            entry.entity_id,
            disabled_by=RegistryEntryDisabler.USER,
        )
    _publish_energy_state(hass, entry.entity_id)
    return entry


def _valid_storage_input(
    hass: HomeAssistant,
) -> tuple[dict[str, str], dict[str, object], dict[str, RegistryEntry]]:
    """Build a complete PV/grid/battery vector and exact scalar input."""
    entries = {
        role: _register_source(hass, role)
        for role in (
            "pv_generation",
            "grid_import",
            "grid_export",
            "battery_charge",
            "battery_discharge",
        )
    }
    existing_sources = {
        role: entries[role].id
        for role in ("pv_generation", "grid_import", "grid_export")
    }
    user_input: dict[str, object] = {
        "battery_charge": entries["battery_charge"].entity_id,
        "battery_discharge": entries["battery_discharge"].id,
        "usable_capacity_kwh": "13.500",
        "round_trip_efficiency_percent": "90.00",
    }
    return existing_sources, user_input, entries


def _validate(
    hass: HomeAssistant,
    existing_sources: Mapping[str, str],
    user_input: Mapping[str, object],
) -> tuple[dict[str, str] | None, dict[str, str]]:
    """Keep test assertions concise without weakening the public type contract."""
    parameters, errors = validate_storage_selection(
        hass,
        existing_sources,
        user_input,
    )
    return parameters, errors


def test_valid_storage_selection_is_canonical_serializable_and_detached(
    hass: HomeAssistant,
) -> None:
    """Persist only registry UUIDs and exact canonical decimal strings."""
    existing_sources, user_input, entries = _valid_storage_input(hass)
    original_sources = deepcopy(existing_sources)
    original_input = deepcopy(user_input)

    parameters, errors = _validate(hass, existing_sources, user_input)

    assert errors == {}
    assert parameters == {
        "charge_source": entries["battery_charge"].id,
        "discharge_source": entries["battery_discharge"].id,
        "usable_capacity_kwh": "13.5",
        "round_trip_efficiency": "0.9",
    }
    assert json.loads(json.dumps(parameters)) == parameters
    assert existing_sources == original_sources
    assert user_input == original_input
    assert not hass.config_entries.async_entries(DOMAIN)


@pytest.mark.parametrize(
    ("capacity", "efficiency", "expected_capacity", "expected_ratio"),
    [
        pytest.param("0.1", "0.1", "0.1", "0.001", id="lower-bounds"),
        pytest.param("1000.000", "100.000", "1000", "1", id="upper-bounds"),
        pytest.param(7, 90, "7", "0.9", id="exact-integers"),
        pytest.param(
            "00013.5000",
            "33.3333333333333333333333333333333333333300",
            "13.5",
            "0.3333333333333333333333333333333333333333",
            id="long-exact-decimals",
        ),
    ],
)
def test_exact_numeric_boundaries_are_canonicalized_without_rounding(
    hass: HomeAssistant,
    capacity: str | int,
    efficiency: str | int,
    expected_capacity: str,
    expected_ratio: str,
) -> None:
    """Canonicalization retains every submitted decimal digit exactly."""
    existing_sources, user_input, _entries = _valid_storage_input(hass)
    user_input["usable_capacity_kwh"] = capacity
    user_input["round_trip_efficiency_percent"] = efficiency

    parameters, errors = _validate(hass, existing_sources, user_input)

    assert errors == {}
    assert parameters is not None
    assert parameters["usable_capacity_kwh"] == expected_capacity
    assert parameters["round_trip_efficiency"] == expected_ratio


@pytest.mark.parametrize(
    "missing_field",
    [
        "battery_charge",
        "battery_discharge",
        "usable_capacity_kwh",
        "round_trip_efficiency_percent",
    ],
)
def test_every_storage_field_is_required(
    hass: HomeAssistant,
    missing_field: str,
) -> None:
    """Battery direction and both physical parameters have no silent default."""
    existing_sources, user_input, _entries = _valid_storage_input(hass)
    del user_input[missing_field]

    assert _validate(hass, existing_sources, user_input) == (
        None,
        {missing_field: "required"},
    )


def test_unexpected_storage_fields_are_rejected(hass: HomeAssistant) -> None:
    """Later-stage or control fields cannot leak into storage parameters."""
    existing_sources, user_input, _entries = _valid_storage_input(hass)
    user_input["battery_sources_confirmed"] = True
    user_input["storage_id"] = "invented"

    assert _validate(hass, existing_sources, user_input) == (
        None,
        {
            "battery_sources_confirmed": "unexpected_field",
            "storage_id": "unexpected_field",
        },
    )


@pytest.mark.parametrize(
    ("field", "value", "expected_error"),
    [
        pytest.param("usable_capacity_kwh", True, "invalid_number", id="bool"),
        pytest.param("usable_capacity_kwh", 13.5, "invalid_number", id="float"),
        pytest.param(
            "usable_capacity_kwh",
            Decimal("13.5"),
            "invalid_number",
            id="decimal-object",
        ),
        pytest.param("usable_capacity_kwh", "NaN", "invalid_number", id="nan"),
        pytest.param(
            "round_trip_efficiency_percent",
            "Infinity",
            "invalid_number",
            id="infinity",
        ),
        pytest.param(
            "round_trip_efficiency_percent",
            "9e1",
            "invalid_number",
            id="exponent",
        ),
        pytest.param(
            "round_trip_efficiency_percent",
            " 90",
            "invalid_number",
            id="leading-space",
        ),
        pytest.param(
            "round_trip_efficiency_percent",
            "90 ",
            "invalid_number",
            id="trailing-space",
        ),
        pytest.param(
            "usable_capacity_kwh",
            "13,5",
            "invalid_decimal_separator",
            id="decimal-comma",
        ),
    ],
)
def test_inexact_localized_and_non_plain_numbers_fail_closed(
    hass: HomeAssistant,
    field: str,
    value: object,
    expected_error: str,
) -> None:
    """The UI boundary never converts a binary float or guesses locale."""
    existing_sources, user_input, _entries = _valid_storage_input(hass)
    user_input[field] = value

    assert _validate(hass, existing_sources, user_input) == (
        None,
        {field: expected_error},
    )


@pytest.mark.parametrize(
    ("field", "value", "expected_error"),
    [
        pytest.param(
            "usable_capacity_kwh",
            "0.099999999999999999999999",
            "capacity_out_of_range",
            id="capacity-below",
        ),
        pytest.param(
            "usable_capacity_kwh",
            "1000.000000000000000000001",
            "capacity_out_of_range",
            id="capacity-above",
        ),
        pytest.param(
            "usable_capacity_kwh",
            "-1",
            "capacity_out_of_range",
            id="capacity-negative",
        ),
        pytest.param(
            "round_trip_efficiency_percent",
            "0",
            "efficiency_out_of_range",
            id="efficiency-zero",
        ),
        pytest.param(
            "round_trip_efficiency_percent",
            "-0.1",
            "efficiency_out_of_range",
            id="efficiency-negative",
        ),
        pytest.param(
            "round_trip_efficiency_percent",
            "100.000000000000000000001",
            "efficiency_out_of_range",
            id="efficiency-above",
        ),
    ],
)
def test_physical_parameter_ranges_are_exact_and_inclusive_where_specified(
    hass: HomeAssistant,
    field: str,
    value: object,
    expected_error: str,
) -> None:
    """Capacity and efficiency enforce the accepted ADR boundaries."""
    existing_sources, user_input, _entries = _valid_storage_input(hass)
    user_input[field] = value

    assert _validate(hass, existing_sources, user_input) == (
        None,
        {field: expected_error},
    )


@pytest.mark.parametrize("value", [False, 7, " sensor.charge", "sensor.charge "])
def test_malformed_battery_source_selection_is_rejected(
    hass: HomeAssistant,
    value: object,
) -> None:
    """Battery source selection uses the same canonical shape as PV/grid."""
    existing_sources, user_input, _entries = _valid_storage_input(hass)
    user_input["battery_charge"] = value

    assert _validate(hass, existing_sources, user_input) == (
        None,
        {"battery_charge": "invalid_selection"},
    )


def test_registry_domain_disabled_and_missing_errors_are_field_specific(
    hass: HomeAssistant,
) -> None:
    """Both battery directions resolve through the authoritative registry."""
    existing_sources, user_input, _entries = _valid_storage_input(hass)
    wrong_domain = _register_source(hass, "wrong_charge", domain="number")
    disabled = _register_source(hass, "disabled_discharge", disabled=True)

    user_input["battery_charge"] = wrong_domain.id
    user_input["battery_discharge"] = disabled.entity_id
    parameters, errors = _validate(hass, existing_sources, user_input)

    assert parameters is None
    assert errors == {
        "battery_charge": "invalid_domain",
        "battery_discharge": "source_disabled",
    }

    user_input["battery_charge"] = "00000000000000000000000000000000"
    assert _validate(hass, existing_sources, user_input) == (
        None,
        {
            "battery_charge": "source_not_registered",
            "battery_discharge": "source_disabled",
        },
    )


def test_charge_and_discharge_must_be_distinct_sources(
    hass: HomeAssistant,
) -> None:
    """A counter cannot simultaneously represent AC energy in and out."""
    existing_sources, user_input, _entries = _valid_storage_input(hass)
    user_input["battery_discharge"] = user_input["battery_charge"]

    assert _validate(hass, existing_sources, user_input) == (
        None,
        {
            "battery_charge": "duplicate_source",
            "battery_discharge": "duplicate_source",
        },
    )


def test_battery_source_cannot_reuse_an_existing_physical_role(
    hass: HomeAssistant,
) -> None:
    """PV, grid, charge, and discharge ownership is globally one-to-one."""
    existing_sources, user_input, _entries = _valid_storage_input(hass)
    user_input["battery_charge"] = existing_sources["grid_import"]

    assert _validate(hass, existing_sources, user_input) == (
        None,
        {
            "battery_charge": "duplicate_source",
            "base": "invalid_source_vector",
        },
    )


def test_battery_semantics_are_validated_by_the_shared_energy_reader(
    hass: HomeAssistant,
) -> None:
    """A selector hint cannot admit a power sensor as cumulative energy."""
    existing_sources, user_input, entries = _valid_storage_input(hass)
    _publish_energy_state(
        hass,
        entries["battery_discharge"].entity_id,
        device_class="power",
    )

    assert _validate(hass, existing_sources, user_input) == (
        None,
        {"battery_discharge": "invalid_device_class"},
    )


def test_previous_source_failure_is_exposed_at_the_storage_step_base(
    hass: HomeAssistant,
) -> None:
    """A now-invalid prior selection cannot be hidden by valid battery fields."""
    existing_sources, user_input, entries = _valid_storage_input(hass)
    _publish_energy_state(hass, entries["grid_export"].entity_id, unit="J")

    assert _validate(hass, existing_sources, user_input) == (
        None,
        {"base": "invalid_source_vector"},
    )


def test_complete_vector_must_share_one_exact_physical_period(
    hass: HomeAssistant,
) -> None:
    """Battery sources join PV/grid in the same atomic measurement vector."""
    existing_sources, user_input, entries = _valid_storage_input(hass)
    _publish_energy_state(
        hass,
        entries["battery_charge"].entity_id,
        period_end=_PERIOD_END + timedelta(seconds=1),
        reported_at=_LAST_REPORTED + timedelta(seconds=1),
    )

    assert _validate(hass, existing_sources, user_input) == (
        None,
        {
            "battery_charge": "sources_not_synchronized",
            "battery_discharge": "sources_not_synchronized",
            "base": "invalid_source_vector",
        },
    )


@pytest.mark.parametrize(
    "existing_sources",
    [
        pytest.param({}, id="missing-existing-vector"),
        pytest.param(
            {"battery_charge": "unexpected-existing-role"},
            id="overlapping-battery-role",
        ),
    ],
)
def test_existing_vector_precondition_fails_closed(
    hass: HomeAssistant,
    existing_sources: dict[str, str],
) -> None:
    """Storage validation requires a distinct completed source-stage vector."""
    _valid_existing, user_input, _entries = _valid_storage_input(hass)

    assert _validate(hass, existing_sources, user_input) == (
        None,
        {"base": "invalid_source_vector"},
    )
