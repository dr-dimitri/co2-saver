# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Tests for exact, side-effect-free consumer-plan validation."""

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
from homeassistant.util import dt as dt_util

from custom_components.co2saver.config_consumers import (
    validate_consumer_input,
    validate_consumption_selection,
)
from custom_components.co2saver.const import ATTR_CO2SAVER_PERIOD_END, DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_registry import RegistryEntry


_NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)
_PERIOD_END = _NOW - timedelta(minutes=1)
_LAST_REPORTED = _PERIOD_END + timedelta(seconds=30)
_HOUSEHOLD_ID = "1" * 32
_WALLBOX_ID = "2" * 32
_HEAT_PUMP_ID = "3" * 32


@pytest.fixture(autouse=True)
def freeze_validation_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the full physical-vector check deterministic."""
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
    """Publish one semantically valid cumulative local-energy counter."""
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


def _register_source(hass: HomeAssistant, role: str) -> RegistryEntry:
    """Create one registered source in the common physical period."""
    entry = er.async_get(hass).async_get_or_create(
        "sensor",
        "consumer_test",
        role,
        suggested_object_id=role,
    )
    _publish_energy_state(hass, entry.entity_id)
    return entry


@pytest.fixture
def sources(hass: HomeAssistant) -> dict[str, RegistryEntry]:
    """Provide all prior-stage and consumer measurement roles."""
    return {
        role: _register_source(hass, role)
        for role in (
            "pv_generation",
            "grid_import",
            "grid_export",
            "battery_charge",
            "battery_discharge",
            "aggregate_load",
            "household_load",
            "wallbox_load",
            "heat_pump_load",
        )
    }


def _existing_sources(
    sources: dict[str, RegistryEntry],
    *,
    with_battery: bool = False,
) -> dict[str, str]:
    """Build the canonical output of the completed source/storage stages."""
    roles = ["pv_generation", "grid_import", "grid_export"]
    if with_battery:
        roles.extend(("battery_charge", "battery_discharge"))
    return {role: sources[role].id for role in roles}


def _aggregate_plan(
    sources: dict[str, RegistryEntry],
    consumers: list[object] | None = None,
) -> dict[str, object]:
    """Build one aggregate-meter full-plan input."""
    return {
        "mode": "aggregate_shares",
        "household_id": _HOUSEHOLD_ID,
        "household_source": sources["aggregate_load"].entity_id,
        "consumers": [] if consumers is None else consumers,
    }


def _separate_plan(
    sources: dict[str, RegistryEntry],
    consumers: list[object] | None = None,
) -> dict[str, object]:
    """Build one separate-meter full-plan input."""
    return {
        "mode": "separate_meters",
        "household_id": _HOUSEHOLD_ID,
        "household_source": sources["household_load"].entity_id,
        "consumers": [] if consumers is None else consumers,
    }


def test_aggregate_consumer_editor_trims_name_and_normalizes_exact_percent() -> None:
    """The row editor returns a ratio but never invents a consumer identity."""
    candidate, errors = validate_consumer_input(
        "aggregate_shares",
        {
            "name": "  Wallbox  ",
            "share_percent": "12.34567890123456789012345678900",
        },
    )

    assert errors == {}
    assert candidate == {
        "name": "Wallbox",
        "share": "0.12345678901234567890123456789",
    }
    assert "consumer_id" not in candidate


@pytest.mark.parametrize(
    ("percentage", "expected_share"),
    [
        pytest.param("0", "0", id="zero"),
        pytest.param("-0.000", "0", id="signed-zero"),
        pytest.param("100.000", "1", id="one-hundred"),
        pytest.param(25, "0.25", id="exact-integer"),
    ],
)
def test_aggregate_consumer_editor_accepts_inclusive_share_boundaries(
    percentage: str | int,
    expected_share: str,
) -> None:
    """Zero attribution and a zero household remainder are both explicit."""
    candidate, errors = validate_consumer_input(
        "aggregate_shares",
        {"name": "Load", "share_percent": percentage},
    )

    assert errors == {}
    assert candidate == {"name": "Load", "share": expected_share}


def test_separate_consumer_editor_keeps_source_unresolved() -> None:
    """Registry lookup waits until the complete physical vector is available."""
    candidate, errors = validate_consumer_input(
        "separate_meters",
        {"name": " Wallbox ", "source": "sensor.wallbox_energy"},
    )

    assert errors == {}
    assert candidate == {"name": "Wallbox", "source": "sensor.wallbox_energy"}
    assert "consumer_id" not in candidate


@pytest.mark.parametrize("mode", ["", "shares", "AGGREGATE_SHARES", 1, None])
def test_consumer_editor_rejects_unknown_modes(mode: object) -> None:
    """An editor never guesses which mutually exclusive fields are intended."""
    assert validate_consumer_input(
        mode,  # type: ignore[arg-type]
        {"name": "Wallbox", "share_percent": "25"},
    ) == (None, {"base": "invalid_consumption_mode"})


@pytest.mark.parametrize(
    ("user_input", "expected_errors"),
    [
        pytest.param(
            {"name": "", "share_percent": "20"},
            {"name": "required"},
            id="missing-name",
        ),
        pytest.param(
            {"name": "   ", "share_percent": "20"},
            {"name": "invalid_name"},
            id="blank-name",
        ),
        pytest.param(
            {"name": 5, "share_percent": "20"},
            {"name": "invalid_name"},
            id="non-string-name",
        ),
        pytest.param(
            {"name": "Wallbox"},
            {"share_percent": "required"},
            id="missing-share",
        ),
        pytest.param(
            {"name": "Wallbox", "share_percent": "20", "source": "sensor.x"},
            {"source": "unexpected_field"},
            id="mixed-source",
        ),
    ],
)
def test_aggregate_editor_shape_errors_are_field_specific(
    user_input: dict[str, object],
    expected_errors: dict[str, str],
) -> None:
    """Incomplete or mixed aggregate rows are never silently accepted."""
    assert validate_consumer_input("aggregate_shares", user_input) == (
        None,
        expected_errors,
    )


@pytest.mark.parametrize(
    ("value", "expected_error"),
    [
        pytest.param(-1, "share_out_of_range", id="negative"),
        pytest.param("100.00000000000000000001", "share_out_of_range", id="above"),
        pytest.param("12,5", "invalid_decimal_separator", id="comma"),
        pytest.param("1e2", "invalid_number", id="exponent"),
        pytest.param(" 25", "invalid_number", id="whitespace"),
        pytest.param(25.0, "invalid_number", id="float"),
        pytest.param(True, "invalid_number", id="bool"),
        pytest.param(Decimal(25), "invalid_number", id="decimal-object"),
    ],
)
def test_aggregate_editor_share_errors_fail_closed(
    value: object,
    expected_error: str,
) -> None:
    """Percentages retain exact decimal UI semantics and accepted bounds."""
    assert validate_consumer_input(
        "aggregate_shares",
        {"name": "Wallbox", "share_percent": value},
    ) == (None, {"share_percent": expected_error})


@pytest.mark.parametrize(
    ("user_input", "expected_errors"),
    [
        pytest.param(
            {"name": "Wallbox"},
            {"source": "required"},
            id="missing-source",
        ),
        pytest.param(
            {"name": "Wallbox", "source": 5},
            {"source": "invalid_selection"},
            id="malformed-source",
        ),
        pytest.param(
            {"name": "Wallbox", "source": " sensor.wallbox"},
            {"source": "invalid_selection"},
            id="source-whitespace",
        ),
        pytest.param(
            {"name": "Wallbox", "source": "sensor.wallbox", "share_percent": "1"},
            {"share_percent": "unexpected_field"},
            id="mixed-share",
        ),
        pytest.param(
            {"name": "   ", "source": "sensor.wallbox"},
            {"name": "invalid_name"},
            id="blank-name",
        ),
    ],
)
def test_separate_editor_shape_errors_are_field_specific(
    user_input: dict[str, object],
    expected_errors: dict[str, str],
) -> None:
    """Separate-meter rows require exactly one unresolved source selection."""
    assert validate_consumer_input("separate_meters", user_input) == (
        None,
        expected_errors,
    )


@pytest.mark.parametrize("mode", ["aggregate_shares", "separate_meters"])
def test_household_only_plan_is_canonical_serializable_and_detached(
    hass: HomeAssistant,
    sources: dict[str, RegistryEntry],
    mode: str,
) -> None:
    """Both modes support a household without any additional consumers."""
    existing = _existing_sources(sources)
    plan = (
        _aggregate_plan(sources)
        if mode == "aggregate_shares"
        else _separate_plan(sources)
    )
    original_existing = deepcopy(existing)
    original_plan = deepcopy(plan)

    draft, errors = validate_consumption_selection(hass, existing, plan)

    assert errors == {}
    assert draft is not None
    assert dict(draft) == {
        "mode": mode,
        "household_id": _HOUSEHOLD_ID,
        "household_source": sources[
            "aggregate_load" if mode == "aggregate_shares" else "household_load"
        ].id,
        "consumers": [],
    }
    assert json.loads(json.dumps(draft)) == draft
    assert existing == original_existing
    assert plan == original_plan
    assert not hass.config_entries.async_entries(DOMAIN)


def test_aggregate_plan_retains_zero_share_and_exact_household_remainder(
    hass: HomeAssistant,
    sources: dict[str, RegistryEntry],
) -> None:
    """Every named row remains explicit while shares sum exactly to one."""
    plan = _aggregate_plan(
        sources,
        [
            {"consumer_id": _WALLBOX_ID, "name": " Wallbox ", "share": "0.25"},
            {"consumer_id": _HEAT_PUMP_ID, "name": "Heat pump", "share": "0.750"},
        ],
    )

    draft, errors = validate_consumption_selection(
        hass,
        _existing_sources(sources),
        plan,
    )

    assert errors == {}
    assert draft is not None
    assert draft["consumers"] == [
        {"consumer_id": _WALLBOX_ID, "name": "Wallbox", "share": "0.25"},
        {"consumer_id": _HEAT_PUMP_ID, "name": "Heat pump", "share": "0.75"},
    ]

    plan["consumers"] = [{"consumer_id": _WALLBOX_ID, "name": "Zero", "share": "0.000"}]
    draft, errors = validate_consumption_selection(
        hass,
        _existing_sources(sources),
        plan,
    )
    assert errors == {}
    assert draft is not None
    assert draft["consumers"] == [
        {"consumer_id": _WALLBOX_ID, "name": "Zero", "share": "0"}
    ]

    plan["consumers"] = [{"consumer_id": _WALLBOX_ID, "name": "All", "share": 1}]
    draft, errors = validate_consumption_selection(
        hass,
        _existing_sources(sources),
        plan,
    )
    assert errors == {}
    assert draft is not None
    assert draft["consumers"] == [
        {"consumer_id": _WALLBOX_ID, "name": "All", "share": "1"}
    ]


def test_share_sum_just_above_one_is_rejected_without_rounding(
    hass: HomeAssistant,
    sources: dict[str, RegistryEntry],
) -> None:
    """Arbitrarily small exact excess cannot consume a negative household rest."""
    plan = _aggregate_plan(
        sources,
        [
            {"consumer_id": _WALLBOX_ID, "name": "Wallbox", "share": "0.9"},
            {
                "consumer_id": _HEAT_PUMP_ID,
                "name": "Heat pump",
                "share": "0.100000000000000000000000000001",
            },
        ],
    )

    assert validate_consumption_selection(
        hass,
        _existing_sources(sources),
        plan,
    ) == (None, {"consumers": "shares_exceed_total"})


def test_separate_plan_resolves_every_non_overlapping_meter_to_registry_uuid(
    hass: HomeAssistant,
    sources: dict[str, RegistryEntry],
) -> None:
    """Household and additional loads retain stable IDs and distinct sources."""
    plan = _separate_plan(
        sources,
        [
            {
                "consumer_id": _WALLBOX_ID,
                "name": " Wallbox ",
                "source": sources["wallbox_load"].entity_id,
            },
            {
                "consumer_id": _HEAT_PUMP_ID,
                "name": "Heat pump",
                "source": sources["heat_pump_load"].id,
            },
        ],
    )

    draft, errors = validate_consumption_selection(
        hass,
        _existing_sources(sources, with_battery=True),
        plan,
    )

    assert errors == {}
    assert draft == {
        "mode": "separate_meters",
        "household_id": _HOUSEHOLD_ID,
        "household_source": sources["household_load"].id,
        "consumers": [
            {
                "consumer_id": _WALLBOX_ID,
                "name": "Wallbox",
                "source": sources["wallbox_load"].id,
            },
            {
                "consumer_id": _HEAT_PUMP_ID,
                "name": "Heat pump",
                "source": sources["heat_pump_load"].id,
            },
        ],
    }


@pytest.mark.parametrize(
    ("mode", "row", "unexpected_field"),
    [
        pytest.param(
            "aggregate_shares",
            {
                "consumer_id": _WALLBOX_ID,
                "name": "Wallbox",
                "share": "0.25",
                "source": "sensor.wallbox",
            },
            "source",
            id="aggregate-with-source",
        ),
        pytest.param(
            "separate_meters",
            {
                "consumer_id": _WALLBOX_ID,
                "name": "Wallbox",
                "share": "0.25",
                "source": "sensor.wallbox",
            },
            "share",
            id="separate-with-share",
        ),
    ],
)
def test_full_plan_rejects_mixed_consumer_modes(
    hass: HomeAssistant,
    sources: dict[str, RegistryEntry],
    mode: str,
    row: dict[str, object],
    unexpected_field: str,
) -> None:
    """No row can carry both share and meter semantics."""
    plan = (
        _aggregate_plan(sources, [row])
        if mode == "aggregate_shares"
        else _separate_plan(sources, [row])
    )

    assert validate_consumption_selection(
        hass,
        _existing_sources(sources),
        plan,
    ) == (
        None,
        {f"consumer:{_WALLBOX_ID}:{unexpected_field}": "unexpected_field"},
    )


@pytest.mark.parametrize(
    ("consumers", "expected_errors"),
    [
        pytest.param(
            "not-a-list",
            {"consumers": "invalid_consumer_plan"},
            id="not-list",
        ),
        pytest.param(
            [None],
            {"consumer_index:0": "invalid_consumer_plan"},
            id="non-mapping-row",
        ),
        pytest.param(
            [{"consumer_id": "bad", "name": "Wallbox", "share": "0.25"}],
            {"consumer_index:0:consumer_id": "invalid_consumer_id"},
            id="invalid-id",
        ),
        pytest.param(
            [{"consumer_id": _WALLBOX_ID, "name": "Wallbox"}],
            {f"consumer:{_WALLBOX_ID}:share": "required"},
            id="incomplete-row",
        ),
        pytest.param(
            [{"name": "Wallbox", "share": "0.25"}],
            {"consumer_index:0:consumer_id": "required"},
            id="missing-id",
        ),
        pytest.param(
            [{"consumer_id": _WALLBOX_ID, "name": " ", "share": "0.25"}],
            {f"consumer:{_WALLBOX_ID}:name": "invalid_name"},
            id="blank-name",
        ),
        pytest.param(
            [{"consumer_id": _WALLBOX_ID, "name": "Wallbox", "share": "1e-1"}],
            {f"consumer:{_WALLBOX_ID}:share": "invalid_number"},
            id="malformed-canonical-share",
        ),
        pytest.param(
            [{"consumer_id": _WALLBOX_ID, "name": "Wallbox", "share": "1.001"}],
            {f"consumer:{_WALLBOX_ID}:share": "share_out_of_range"},
            id="share-out-of-range",
        ),
    ],
)
def test_malformed_full_plan_rows_are_not_dropped(
    hass: HomeAssistant,
    sources: dict[str, RegistryEntry],
    consumers: object,
    expected_errors: dict[str, str],
) -> None:
    """One malformed additional consumer rejects the entire full plan."""
    plan = _aggregate_plan(sources)
    plan["consumers"] = consumers

    assert validate_consumption_selection(
        hass,
        _existing_sources(sources),
        plan,
    ) == (None, expected_errors)


def test_separate_full_plan_requires_every_consumer_source(
    hass: HomeAssistant,
    sources: dict[str, RegistryEntry],
) -> None:
    """Mode switches cannot leave a source-less consumer row valid."""
    plan = _separate_plan(
        sources,
        [{"consumer_id": _WALLBOX_ID, "name": "Wallbox"}],
    )

    assert validate_consumption_selection(
        hass,
        _existing_sources(sources),
        plan,
    ) == (None, {f"consumer:{_WALLBOX_ID}:source": "required"})


@pytest.mark.parametrize("duplicate", ["household", "consumer"])
def test_consumer_ids_are_globally_unique(
    hass: HomeAssistant,
    sources: dict[str, RegistryEntry],
    duplicate: str,
) -> None:
    """Renaming and ordering cannot merge two stable result histories."""
    first_id = _HOUSEHOLD_ID if duplicate == "household" else _WALLBOX_ID
    plan = _aggregate_plan(
        sources,
        [
            {"consumer_id": first_id, "name": "Wallbox", "share": "0.2"},
            {"consumer_id": _WALLBOX_ID, "name": "Heat pump", "share": "0.3"},
        ],
    )

    assert validate_consumption_selection(
        hass,
        _existing_sources(sources),
        plan,
    ) == (None, {"consumers": "duplicate_consumer_id"})


def test_duplicate_household_and_consumer_sources_mark_each_editable_field(
    hass: HomeAssistant,
    sources: dict[str, RegistryEntry],
) -> None:
    """One physical local-load counter cannot own two consumer roles."""
    plan = _separate_plan(
        sources,
        [
            {
                "consumer_id": _WALLBOX_ID,
                "name": "Wallbox",
                "source": sources["household_load"].entity_id,
            }
        ],
    )

    assert validate_consumption_selection(
        hass,
        _existing_sources(sources),
        plan,
    ) == (
        None,
        {
            "household_source": "duplicate_source",
            f"consumer:{_WALLBOX_ID}:source": "duplicate_source",
        },
    )


def test_load_source_cannot_reuse_a_prior_pv_grid_or_battery_role(
    hass: HomeAssistant,
    sources: dict[str, RegistryEntry],
) -> None:
    """The complete site vector gives every physical counter exactly one owner."""
    plan = _separate_plan(
        sources,
        [
            {
                "consumer_id": _WALLBOX_ID,
                "name": "Wallbox",
                "source": sources["battery_charge"].id,
            }
        ],
    )

    assert validate_consumption_selection(
        hass,
        _existing_sources(sources, with_battery=True),
        plan,
    ) == (
        None,
        {
            f"consumer:{_WALLBOX_ID}:source": "duplicate_source",
            "base": "invalid_source_vector",
        },
    )


def test_consumer_semantics_and_complete_period_are_validated_together(
    hass: HomeAssistant,
    sources: dict[str, RegistryEntry],
) -> None:
    """Separate loads use the same semantic and synchronous source contract."""
    plan = _separate_plan(
        sources,
        [
            {
                "consumer_id": _WALLBOX_ID,
                "name": "Wallbox",
                "source": sources["wallbox_load"].entity_id,
            }
        ],
    )
    _publish_energy_state(hass, sources["wallbox_load"].entity_id, unit="J")
    assert validate_consumption_selection(
        hass,
        _existing_sources(sources),
        plan,
    ) == (
        None,
        {f"consumer:{_WALLBOX_ID}:source": "invalid_unit"},
    )

    _publish_energy_state(
        hass,
        sources["wallbox_load"].entity_id,
        period_end=_PERIOD_END + timedelta(seconds=1),
        reported_at=_LAST_REPORTED + timedelta(seconds=1),
    )
    assert validate_consumption_selection(
        hass,
        _existing_sources(sources),
        plan,
    ) == (
        None,
        {
            "household_source": "sources_not_synchronized",
            f"consumer:{_WALLBOX_ID}:source": "sources_not_synchronized",
            "base": "invalid_source_vector",
        },
    )


@pytest.mark.parametrize(
    ("field", "value", "expected_error"),
    [
        pytest.param("mode", None, "required", id="missing-mode"),
        pytest.param("mode", "shares", "invalid_consumption_mode", id="invalid-mode"),
        pytest.param("household_id", None, "required", id="missing-id"),
        pytest.param(
            "household_id",
            "not-a-uuid",
            "invalid_consumer_id",
            id="invalid-id",
        ),
        pytest.param("household_source", 1, "invalid_selection", id="source"),
        pytest.param("consumers", None, "required", id="consumers"),
    ],
)
def test_full_plan_header_shape_fails_closed(
    hass: HomeAssistant,
    sources: dict[str, RegistryEntry],
    field: str,
    value: object,
    expected_error: str,
) -> None:
    """The validator never guesses missing identities, modes, or load sources."""
    plan = _aggregate_plan(sources)
    plan[field] = value

    assert validate_consumption_selection(
        hass,
        _existing_sources(sources),
        plan,
    ) == (None, {field: expected_error})


@pytest.mark.parametrize(
    "existing",
    [
        pytest.param({}, id="empty"),
        pytest.param({"grid_import": "only-one-role"}, id="incomplete-grid"),
        pytest.param(
            {
                "grid_import": "one",
                "grid_export": "two",
                "battery_charge": "three",
            },
            id="half-battery",
        ),
        pytest.param(
            {
                "grid_import": "one",
                "grid_export": "two",
                "unsupported": "three",
            },
            id="unexpected-role",
        ),
        pytest.param(
            {
                "pv_generation": "one",
                "pv_plausibility": "two",
                "grid_import": "three",
                "grid_export": "four",
            },
            id="conflicting-pv-topologies",
        ),
    ],
)
def test_incomplete_prior_stage_vector_fails_closed(
    hass: HomeAssistant,
    sources: dict[str, RegistryEntry],
    existing: dict[str, str],
) -> None:
    """Consumer validation cannot repair or reinterpret an earlier-stage draft."""
    assert validate_consumption_selection(
        hass,
        existing,
        _aggregate_plan(sources),
    ) == (None, {"base": "invalid_source_vector"})
