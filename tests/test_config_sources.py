# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Tests for side-effect-free source selection and canonicalization."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from homeassistant.components.sensor import (
    ATTR_STATE_CLASS,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_registry import RegistryEntryDisabler
from homeassistant.util import dt as dt_util

from custom_components.co2saver.config_sources import (
    energy_entity_selector,
    source_fields,
    validate_source_selection,
)
from custom_components.co2saver.const import ATTR_CO2SAVER_PERIOD_END, DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_registry import RegistryEntry


_NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)
_PERIOD_END = _NOW - timedelta(minutes=1)
_LAST_REPORTED = _PERIOD_END + timedelta(seconds=30)


@pytest.fixture(autouse=True)
def freeze_validation_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep all inclusive source-age boundaries deterministic."""
    monkeypatch.setattr(dt_util, "utcnow", lambda: _NOW)


def _publish_energy_state(  # noqa: PLR0913
    hass: HomeAssistant,
    entity_id: str,
    *,
    value: object = "100",
    unit: object = "kWh",
    device_class: object = SensorDeviceClass.ENERGY,
    state_class: object = SensorStateClass.TOTAL_INCREASING,
    period_end: object = _PERIOD_END,
    reported_at: datetime = _LAST_REPORTED,
    include_period_end: bool = True,
) -> None:
    """Publish one current cumulative counter with explicit semantics."""
    attributes = {
        ATTR_DEVICE_CLASS: device_class,
        ATTR_STATE_CLASS: state_class,
        ATTR_UNIT_OF_MEASUREMENT: unit,
    }
    if include_period_end:
        attributes[ATTR_CO2SAVER_PERIOD_END] = period_end
    hass.states.async_set(
        entity_id,
        str(value),
        attributes,
        timestamp=reported_at.timestamp(),
    )


def _register_source(
    hass: HomeAssistant,
    role: str,
    *,
    domain: str = "sensor",
    publish: bool = True,
    disabled: bool = False,
) -> RegistryEntry:
    """Register one test entity and normally publish a valid energy state."""
    registry = er.async_get(hass)
    entry = registry.async_get_or_create(
        domain,
        "test",
        role,
        suggested_object_id=role,
    )
    if disabled:
        entry = registry.async_update_entity(
            entry.entity_id,
            disabled_by=RegistryEntryDisabler.USER,
        )
    if publish:
        _publish_energy_state(hass, entry.entity_id)
    return entry


def _valid_selection(
    hass: HomeAssistant,
    topology: str,
    *,
    include_plausibility: bool = False,
) -> tuple[dict[str, object], dict[str, RegistryEntry]]:
    """Build one synchronized source selection using current entity IDs."""
    entries: dict[str, RegistryEntry] = {}
    selection: dict[str, object] = {}
    for role in source_fields(topology):
        if role == "pv_plausibility" and not include_plausibility:
            continue
        entry = _register_source(hass, role)
        entries[role] = entry
        selection[role] = entry.entity_id
    selection["synchronous_sources_confirmed"] = True
    return selection, entries


def test_source_fields_define_only_the_two_accepted_topologies() -> None:
    """Expose one ordered field contract shared with the config flow."""
    assert source_fields("inverter") == (
        "pv_generation",
        "grid_import",
        "grid_export",
    )
    assert source_fields("smart_meter") == (
        "grid_import",
        "grid_export",
        "pv_plausibility",
    )
    with pytest.raises(ValueError, match="unsupported source topology"):
        source_fields("power")


def test_energy_selector_adds_the_missing_state_class_filter(
    hass: HomeAssistant,
) -> None:
    """Whitelist enabled total counters while retaining native energy filters."""
    eligible = _register_source(hass, "eligible")
    total = _register_source(hass, "total")
    _publish_energy_state(
        hass,
        total.entity_id,
        state_class=SensorStateClass.TOTAL,
    )
    wrong_state_class = _register_source(hass, "measurement")
    _publish_energy_state(
        hass,
        wrong_state_class.entity_id,
        state_class=SensorStateClass.MEASUREMENT,
    )
    _register_source(hass, "disabled", disabled=True)
    _register_source(hass, "no_state", publish=False)
    _register_source(hass, "wrong_domain", domain="number")

    selector = energy_entity_selector(hass)

    assert selector.config["filter"] == [
        {
            "domain": ["sensor"],
            "device_class": ["energy"],
            "unit_of_measurement": ["Wh", "kWh", "MWh"],
        }
    ]
    assert selector.config["include_entities"] == sorted(
        (eligible.entity_id, total.entity_id)
    )


def test_energy_selector_keeps_an_empty_match_none_whitelist(
    hass: HomeAssistant,
) -> None:
    """An installation without total counters must not broaden the picker."""
    _register_source(hass, "measurement_only")
    entry = er.async_get(hass).async_get("sensor.measurement_only")
    assert entry is not None
    _publish_energy_state(
        hass,
        entry.entity_id,
        state_class=SensorStateClass.MEASUREMENT,
    )

    assert energy_entity_selector(hass).config["include_entities"] == []


@pytest.mark.parametrize("state_class", [[], {}, ["total"], 1, None])
def test_energy_selector_ignores_malformed_state_classes(
    hass: HomeAssistant, state_class: object
) -> None:
    """An unrelated malformed registry sensor must not crash every energy form."""
    entry = _register_source(hass, "malformed")
    _publish_energy_state(hass, entry.entity_id, state_class=state_class)
    assert energy_entity_selector(hass).config["include_entities"] == []


def test_valid_inverter_selection_is_canonical_and_serializable(
    hass: HomeAssistant,
) -> None:
    """Accept entity IDs and UUIDs but retain registry identity exclusively."""
    selection, entries = _valid_selection(hass, "inverter")
    selection["grid_import"] = entries["grid_import"].id

    draft, errors = validate_source_selection(hass, "inverter", selection)

    assert errors == {}
    assert draft is not None
    assert draft == {
        "topology": "inverter",
        "sources": {
            role: entries[role].id
            for role in ("pv_generation", "grid_import", "grid_export")
        },
        "plant_key": "grid:"
        + ":".join(sorted((entries["grid_import"].id, entries["grid_export"].id))),
        "synchronous_sources_confirmed": True,
    }
    assert json.loads(json.dumps(draft)) == draft
    assert hass.config_entries.async_entries(DOMAIN) == []


@pytest.mark.parametrize("include_plausibility", [False, True])
def test_valid_smart_meter_selection_has_only_selected_roles(
    hass: HomeAssistant,
    *,
    include_plausibility: bool,
) -> None:
    """Keep the plausibility counter optional and never invent a PV source."""
    selection, entries = _valid_selection(
        hass,
        "smart_meter",
        include_plausibility=include_plausibility,
    )

    draft, errors = validate_source_selection(hass, "smart_meter", selection)

    assert errors == {}
    assert draft is not None
    assert set(draft["sources"]) == {
        "grid_import",
        "grid_export",
        *(("pv_plausibility",) if include_plausibility else ()),
    }
    assert "pv_generation" not in draft["sources"]
    assert draft["sources"] == {role: entry.id for role, entry in entries.items()}


@pytest.mark.parametrize("topology", ["", "power", "INVERTER"])
def test_invalid_topology_is_rejected_before_input_inspection(
    hass: HomeAssistant,
    topology: str,
) -> None:
    """Never guess or normalize a materially different topology."""
    assert validate_source_selection(hass, topology, {}) == (
        None,
        {"base": "invalid_topology"},
    )


@pytest.mark.parametrize(
    "missing_field",
    ["pv_generation", "grid_import", "grid_export"],
)
def test_inverter_requires_every_physical_role(
    hass: HomeAssistant,
    missing_field: str,
) -> None:
    """An incomplete topology remains on the same concrete field."""
    selection, _entries = _valid_selection(hass, "inverter")
    del selection[missing_field]

    assert validate_source_selection(hass, "inverter", selection) == (
        None,
        {missing_field: "required"},
    )


@pytest.mark.parametrize("confirmation", [None, False, 1, "true"])
def test_synchronous_contract_requires_the_exact_true_boolean(
    hass: HomeAssistant,
    confirmation: object,
) -> None:
    """Truthy substitutes cannot silently confirm a physical guarantee."""
    selection, _entries = _valid_selection(hass, "inverter")
    selection["synchronous_sources_confirmed"] = confirmation

    assert validate_source_selection(hass, "inverter", selection) == (
        None,
        {"synchronous_sources_confirmed": "confirmation_required"},
    )


def test_unexpected_roles_and_topology_payload_are_rejected(
    hass: HomeAssistant,
) -> None:
    """Only the fields of the separately chosen topology enter the draft."""
    selection, _entries = _valid_selection(hass, "inverter")
    selection["pv_plausibility"] = "sensor.extra"
    selection["topology"] = "smart_meter"

    draft, errors = validate_source_selection(hass, "inverter", selection)

    assert draft is None
    assert errors == {
        "pv_plausibility": "unexpected_field",
        "topology": "unexpected_field",
    }


@pytest.mark.parametrize("value", [False, 7, " sensor.energy", "sensor.energy "])
def test_malformed_entity_selection_is_rejected(
    hass: HomeAssistant,
    value: object,
) -> None:
    """Selections must be canonical entity IDs or registry UUID strings."""
    selection, _entries = _valid_selection(hass, "inverter")
    selection["grid_import"] = value

    assert validate_source_selection(hass, "inverter", selection) == (
        None,
        {"grid_import": "invalid_selection"},
    )


def test_registry_domain_disabled_and_missing_errors_are_field_specific(
    hass: HomeAssistant,
) -> None:
    """Resolve every submitted field through the authoritative registry."""
    selection, _entries = _valid_selection(hass, "inverter")
    wrong_domain = _register_source(hass, "wrong_grid", domain="number")
    disabled = _register_source(hass, "disabled_grid", disabled=True)

    selection["pv_generation"] = "00000000000000000000000000000000"
    selection["grid_import"] = wrong_domain.id
    selection["grid_export"] = disabled.entity_id

    assert validate_source_selection(hass, "inverter", selection) == (
        None,
        {
            "pv_generation": "source_not_registered",
            "grid_import": "invalid_domain",
            "grid_export": "source_disabled",
        },
    )


def test_duplicate_registry_ownership_marks_every_affected_role(
    hass: HomeAssistant,
) -> None:
    """One physical counter cannot own two direction or production roles."""
    selection, _entries = _valid_selection(hass, "inverter")
    selection["grid_export"] = selection["grid_import"]

    draft, errors = validate_source_selection(hass, "inverter", selection)

    assert draft is None
    assert errors == {
        "grid_import": "duplicate_source",
        "grid_export": "duplicate_source",
    }


@pytest.mark.parametrize(
    ("attributes", "expected_error"),
    [
        ({ATTR_DEVICE_CLASS: "power"}, "invalid_device_class"),
        ({ATTR_STATE_CLASS: "measurement"}, "invalid_state_class"),
        ({ATTR_STATE_CLASS: []}, "invalid_state_class"),
        ({ATTR_STATE_CLASS: {}}, "invalid_state_class"),
        ({ATTR_UNIT_OF_MEASUREMENT: "J"}, "invalid_unit"),
        ({ATTR_CO2SAVER_PERIOD_END: "not-a-timestamp"}, "invalid_period_end"),
        ({ATTR_CO2SAVER_PERIOD_END: "2026-09-05T11:59:00"}, "invalid_period_end"),
    ],
)
def test_invalid_current_semantics_are_rejected_by_the_issue_4_reader(
    hass: HomeAssistant,
    attributes: dict[str, object],
    expected_error: str,
) -> None:
    """Selector hints never replace authoritative server-side semantics."""
    selection, entries = _valid_selection(hass, "inverter")
    entry = entries["grid_import"]
    state = hass.states.get(entry.entity_id)
    assert state is not None
    _publish_energy_state(hass, entry.entity_id)
    changed = dict(state.attributes)
    changed.update(attributes)
    hass.states.async_set(
        entry.entity_id,
        state.state,
        changed,
        timestamp=_LAST_REPORTED.timestamp(),
    )

    draft, errors = validate_source_selection(hass, "inverter", selection)

    assert draft is None
    assert errors == {"grid_import": expected_error}


@pytest.mark.parametrize(
    ("value", "expected_error"),
    [
        (STATE_UNKNOWN, "source_unavailable"),
        (STATE_UNAVAILABLE, "source_unavailable"),
        ("not-a-number", "invalid_value"),
        ("nan", "invalid_value"),
        ("inf", "invalid_value"),
        ("-0.1", "invalid_value"),
    ],
)
def test_unavailable_or_invalid_current_values_are_rejected(
    hass: HomeAssistant,
    value: str,
    expected_error: str,
) -> None:
    """No invalid counter value may become a plausible zero."""
    selection, entries = _valid_selection(hass, "inverter")
    _publish_energy_state(hass, entries["grid_import"].entity_id, value=value)

    assert validate_source_selection(hass, "inverter", selection) == (
        None,
        {"grid_import": expected_error},
    )


def test_missing_state_period_and_last_reported_are_distinguished(
    hass: HomeAssistant,
) -> None:
    """Require all current scalars copied by the synchronous reader."""
    selection, entries = _valid_selection(hass, "inverter")
    grid_import = entries["grid_import"]
    hass.states.async_remove(grid_import.entity_id)
    assert validate_source_selection(hass, "inverter", selection) == (
        None,
        {"grid_import": "source_missing"},
    )

    _publish_energy_state(
        hass,
        grid_import.entity_id,
        include_period_end=False,
    )
    assert validate_source_selection(hass, "inverter", selection) == (
        None,
        {"grid_import": "invalid_period_end"},
    )

    _publish_energy_state(hass, grid_import.entity_id)
    state = hass.states.get(grid_import.entity_id)
    assert state is not None
    state.last_reported = _LAST_REPORTED.replace(tzinfo=None)
    assert validate_source_selection(hass, "inverter", selection) == (
        None,
        {"grid_import": "invalid_last_reported"},
    )


@pytest.mark.parametrize(
    ("period_end", "reported_at", "expected_error"),
    [
        (_NOW + timedelta(seconds=1), _NOW, "future_period_end"),
        (_PERIOD_END, _NOW + timedelta(seconds=1), "future_last_reported"),
        (
            _NOW - timedelta(seconds=10),
            _NOW - timedelta(seconds=11),
            "period_after_publication",
        ),
        (
            _NOW - timedelta(seconds=61),
            _NOW,
            "publication_delay",
        ),
    ],
)
def test_per_source_time_contract_is_field_specific(
    hass: HomeAssistant,
    period_end: datetime,
    reported_at: datetime,
    expected_error: str,
) -> None:
    """Reject future and structurally impossible publication timestamps."""
    selection, entries = _valid_selection(hass, "inverter")
    _publish_energy_state(
        hass,
        entries["grid_import"].entity_id,
        period_end=period_end,
        reported_at=reported_at,
    )

    assert validate_source_selection(hass, "inverter", selection) == (
        None,
        {"grid_import": expected_error},
    )


def test_freshness_and_publication_boundaries_are_inclusive(
    hass: HomeAssistant,
) -> None:
    """Accept exactly 300 seconds of age and 60 seconds publication delay."""
    selection, entries = _valid_selection(hass, "inverter")
    boundary_period = _NOW - timedelta(seconds=300)
    boundary_report = boundary_period + timedelta(seconds=60)
    for entry in entries.values():
        _publish_energy_state(
            hass,
            entry.entity_id,
            period_end=boundary_period,
            reported_at=boundary_report,
        )

    draft, errors = validate_source_selection(hass, "inverter", selection)

    assert errors == {}
    assert draft is not None


def test_stale_complete_vector_is_rejected_without_estimation(
    hass: HomeAssistant,
) -> None:
    """Reject a complete first vector just beyond the 300-second ceiling."""
    selection, entries = _valid_selection(hass, "smart_meter")
    stale_period = _NOW - timedelta(seconds=301)
    stale_report = stale_period + timedelta(seconds=60)
    for entry in entries.values():
        _publish_energy_state(
            hass,
            entry.entity_id,
            period_end=stale_period,
            reported_at=stale_report,
        )

    assert validate_source_selection(hass, "smart_meter", selection) == (
        None,
        {"grid_import": "source_stale"},
    )


def test_mixed_physical_periods_return_retryable_field_errors(
    hass: HomeAssistant,
) -> None:
    """A transient mixed vector can be corrected and resubmitted unchanged."""
    selection, entries = _valid_selection(hass, "inverter")
    _publish_energy_state(
        hass,
        entries["grid_export"].entity_id,
        period_end=_PERIOD_END + timedelta(seconds=1),
        reported_at=_LAST_REPORTED + timedelta(seconds=1),
    )

    draft, errors = validate_source_selection(hass, "inverter", selection)

    assert draft is None
    assert errors == dict.fromkeys(
        ("pv_generation", "grid_import", "grid_export"),
        "sources_not_synchronized",
    )

    _publish_energy_state(hass, entries["grid_export"].entity_id)
    draft, errors = validate_source_selection(hass, "inverter", selection)
    assert errors == {}
    assert draft is not None
