# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Public config- and options-flow tests for local-consumer configuration."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import patch
from uuid import UUID

import pytest
import voluptuous as vol
from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import SelectSelector, TextSelector
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.co2saver.const import ATTR_CO2SAVER_PERIOD_END, DOMAIN
from custom_components.co2saver.consumer_flow import ConsumerFlowSteps

if TYPE_CHECKING:
    from collections.abc import Iterator

    from homeassistant.config_entries import ConfigFlowResult
    from homeassistant.core import HomeAssistant

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

_HOUSEHOLD_ID = "1" * 32
_WALLBOX_ID = "2" * 32
_READDED_WALLBOX_ID = "3" * 32


@pytest.fixture(autouse=True)
def no_incomplete_flow_side_effects(hass: HomeAssistant) -> Iterator[None]:
    """Prove that issue #7 changes neither persistence nor runtime wiring."""
    with (
        patch("homeassistant.helpers.storage.Store.async_load") as load,
        patch("homeassistant.helpers.storage.Store.async_save") as save,
        patch(
            "custom_components.co2saver.measurement.ha.UtcMinuteRunner.start"
        ) as start,
        patch(
            "homeassistant.helpers.helper_integration.async_handle_source_entity_changes"
        ) as listen,
        patch.object(hass.config_entries, "async_update_entry") as update_entry,
        patch.object(hass.config_entries, "async_reload") as reload_entry,
    ):
        yield
        load.assert_not_called()
        save.assert_not_called()
        start.assert_not_called()
        listen.assert_not_called()
        update_entry.assert_not_called()
        reload_entry.assert_not_called()


@pytest.fixture
def factor_steps() -> Iterator[list[ConsumerFlowSteps]]:
    """Capture accepted drafts while executing the real issue-#8 handoff."""
    flows: list[ConsumerFlowSteps] = []
    original = ConsumerFlowSteps.async_step_factors

    async def capture(
        flow: ConsumerFlowSteps, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        flows.append(flow)
        return await original(flow, user_input)

    with patch.object(
        ConsumerFlowSteps,
        "async_step_factors",
        autospec=True,
        side_effect=capture,
    ):
        yield flows


@pytest.fixture
def energy_sources(hass: HomeAssistant) -> dict[str, er.RegistryEntry]:
    """Publish upstream and local-load counters for one fresh physical period."""
    registry = er.async_get(hass)
    period_end = (dt_util.utcnow() - timedelta(seconds=1)).isoformat()
    entries: dict[str, er.RegistryEntry] = {}
    for role in (
        "pv_generation",
        "grid_import",
        "grid_export",
        "aggregate_load",
        "household_load",
        "wallbox_load",
        "heat_pump_load",
    ):
        entry = registry.async_get_or_create(
            "sensor", "consumer_flow_test", role, suggested_object_id=role
        )
        hass.states.async_set(
            entry.entity_id,
            "100",
            {
                "device_class": "energy",
                "state_class": "total_increasing",
                "unit_of_measurement": "kWh",
                ATTR_CO2SAVER_PERIOD_END: period_end,
            },
        )
        entries[role] = entry
    return entries


def _schema_field(result: ConfigFlowResult, field: str) -> tuple[vol.Marker, object]:
    """Return one public form marker and selector by field name."""
    for marker, selector in result["data_schema"].schema.items():
        if str(marker) == field:
            return marker, selector
    message = f"missing field: {field}"
    raise AssertionError(message)


def _source_selection(
    sources: dict[str, er.RegistryEntry],
    topology: str = "inverter",
) -> dict[str, object]:
    """Build the accepted source-stage input for either PV topology."""
    roles = (
        ("pv_generation", "grid_import", "grid_export")
        if topology == "inverter"
        else ("grid_import", "grid_export")
    )
    return {role: sources[role].entity_id for role in roles} | {
        "synchronous_sources_confirmed": True
    }


async def _start_config_consumers(
    hass: HomeAssistant,
    sources: dict[str, er.RegistryEntry],
    topology: str = "inverter",
) -> ConfigFlowResult:
    """Reach issue #7 through the real user/source/storage flow chain."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
        data={"topology": topology},
    )
    assert result["step_id"] == "sources"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _source_selection(sources, topology)
    )
    assert result["step_id"] == "storage"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"battery_present": "without_battery"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "consumers"
    return result


async def _choose_mode_and_meter(
    hass: HomeAssistant,
    result: ConfigFlowResult,
    sources: dict[str, er.RegistryEntry],
    mode: str,
) -> ConfigFlowResult:
    """Select one consumption mode and confirm its local-load boundary."""
    result = await _configure(hass, result, {"mode": mode})
    source_role = "aggregate_load" if mode == "aggregate_shares" else "household_load"
    result = await _configure(
        hass,
        result,
        {
            "household_source": sources[source_role].entity_id,
            "load_measurement_confirmed": True,
        },
    )
    assert result["step_id"] == "consumer_menu"
    return result


async def _configure(
    hass: HomeAssistant,
    result: ConfigFlowResult,
    user_input: dict[str, object],
) -> ConfigFlowResult:
    """Advance either public manager according to the flow result's source."""
    manager = (
        hass.config_entries.flow
        if result["handler"] == DOMAIN
        else hass.config_entries.options
    )
    return await manager.async_configure(result["flow_id"], user_input)


async def _add_consumer(
    hass: HomeAssistant,
    result: ConfigFlowResult,
    *,
    mode: str,
    name: str,
    assignment: str,
) -> ConfigFlowResult:
    """Use the public add sequence for either share or separate-meter mode."""
    result = await _configure(hass, result, {"action": "add"})
    assert result["step_id"] == "consumer_add"
    user_input: dict[str, object] = {"name": name}
    if mode == "aggregate_shares":
        user_input["share_percent"] = assignment
    else:
        user_input.update(
            source=assignment,
            consumer_measurement_confirmed=True,
        )
    result = await _configure(hass, result, user_input)
    assert result["step_id"] == "consumer_menu"
    return result


def _consumer_selector_options(result: ConfigFlowResult) -> list[dict[str, str]]:
    """Read stable consumer values and display labels from a public selector."""
    _marker, selector = _schema_field(result, "consumer_id")
    assert isinstance(selector, SelectSelector)
    return selector.config["options"]


def _consumption_plan(
    sources: dict[str, er.RegistryEntry], mode: str
) -> dict[str, object]:
    """Build one canonical existing plan with a single Wallbox."""
    if mode == "aggregate_shares":
        household_source = sources["aggregate_load"].id
        consumer = {
            "consumer_id": _WALLBOX_ID,
            "name": "Wallbox",
            "share": "0.25",
        }
    else:
        household_source = sources["household_load"].id
        consumer = {
            "consumer_id": _WALLBOX_ID,
            "name": "Wallbox",
            "source": sources["wallbox_load"].id,
        }
    return {
        "mode": mode,
        "household_id": _HOUSEHOLD_ID,
        "household_source": household_source,
        "consumers": [consumer],
    }


def _entry_data(
    sources: dict[str, er.RegistryEntry], consumption: dict[str, object]
) -> dict[str, object]:
    """Build authoritative entry data whose unrelated keys must remain intact."""
    grid_ids = sorted([sources["grid_import"].id, sources["grid_export"].id])
    return {
        "topology": "inverter",
        "sources": {
            role: sources[role].id
            for role in ("pv_generation", "grid_import", "grid_export")
        },
        "plant_key": "grid:" + ":".join(grid_ids),
        "synchronous_sources_confirmed": True,
        "battery": None,
        "storage_id": "stable-store-locator",
        "accounting_reference": {"generation": "preserved-generation"},
        "consumption": deepcopy(consumption),
    }


def _add_entry(
    hass: HomeAssistant,
    sources: dict[str, er.RegistryEntry],
    consumption: dict[str, object],
) -> tuple[MockConfigEntry, dict[str, object], dict[str, object]]:
    """Install one entry with conflicting opaque options for authority checks."""
    data = _entry_data(sources, consumption)
    options: dict[str, object] = {
        "opaque": {"preserve": True},
        "consumption": {"mode": "must_not_override_entry_data"},
    }
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=deepcopy(data),
        options=deepcopy(options),
    )
    entry.add_to_hass(hass)
    return entry, data, options


async def _start_options_consumers(
    hass: HomeAssistant, entry: MockConfigEntry
) -> ConfigFlowResult:
    """Start the registered OptionsFlow through Home Assistant's public manager."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "consumers"
    return result


@pytest.mark.parametrize("mode", ["aggregate_shares", "separate_meters"])
async def test_household_only_both_modes_reach_factors_without_committing(
    hass: HomeAssistant,
    energy_sources: dict[str, er.RegistryEntry],
    factor_steps: list[ConsumerFlowSteps],
    mode: str,
) -> None:
    """A confirmed household meter is a complete plan in either input mode."""
    result = await _start_config_consumers(hass, energy_sources)
    mode_marker, mode_selector = _schema_field(result, "mode")
    assert mode_marker.default is vol.UNDEFINED
    assert "suggested_value" not in (mode_marker.description or {})
    assert isinstance(mode_selector, SelectSelector)
    assert mode_selector.config["options"] == [
        "aggregate_shares",
        "separate_meters",
    ]
    result = await _choose_mode_and_meter(hass, result, energy_sources, mode)
    result = await _configure(hass, result, {"action": "finish"})

    assert result["step_id"] == "factors"
    flow = factor_steps[-1]
    consumption = flow.configuration_draft["consumption"]
    source_role = "aggregate_load" if mode == "aggregate_shares" else "household_load"
    assert consumption == {
        "mode": mode,
        "household_id": consumption["household_id"],
        "household_source": energy_sources[source_role].id,
        "consumers": [],
    }
    assert len(consumption["household_id"]) == 32
    int(consumption["household_id"], 16)
    assert json.loads(json.dumps(flow.configuration_draft)) == flow.configuration_draft
    blocked = await _configure(hass, result, {})
    assert blocked["errors"] == {"base": "setup_incomplete"}
    assert not hass.config_entries.async_entries(DOMAIN)
    hass.config_entries.flow.async_abort(blocked["flow_id"])


@pytest.mark.parametrize(
    "confirmation",
    [
        pytest.param(False, id="unchecked"),
        pytest.param(None, id="omitted"),
    ],
)
async def test_load_requires_explicit_confirmation_and_keeps_retry_source(
    hass: HomeAssistant,
    energy_sources: dict[str, er.RegistryEntry],
    factor_steps: list[ConsumerFlowSteps],
    *,
    confirmation: bool | None,
) -> None:
    """An unchecked load boundary stays editable without losing its selection."""
    result = await _start_config_consumers(hass, energy_sources)
    result = await _configure(hass, result, {"mode": "separate_meters"})
    submission: dict[str, object] = {
        "household_source": energy_sources["household_load"].entity_id
    }
    if confirmation is not None:
        submission["load_measurement_confirmed"] = confirmation
    result = await _configure(hass, result, submission)

    assert result["step_id"] == "separate_load"
    assert result["errors"] == {
        "load_measurement_confirmed": "load_confirmation_required"
    }
    source_marker, _source_selector = _schema_field(result, "household_source")
    confirmation_marker, _confirmation_selector = _schema_field(
        result, "load_measurement_confirmed"
    )
    assert source_marker.description["suggested_value"] == (
        energy_sources["household_load"].entity_id
    )
    assert confirmation_marker.default() is False
    assert factor_steps == []

    result = await _configure(
        hass,
        result,
        {
            "household_source": energy_sources["household_load"].entity_id,
            "load_measurement_confirmed": True,
        },
    )
    assert result["step_id"] == "consumer_menu"
    hass.config_entries.flow.async_abort(result["flow_id"])


async def test_smart_meter_topology_reaches_factors_with_complete_consumer_draft(
    hass: HomeAssistant,
    energy_sources: dict[str, er.RegistryEntry],
    factor_steps: list[ConsumerFlowSteps],
) -> None:
    """The alternate upstream topology completes the same issue-#7 handoff."""
    result = await _start_config_consumers(hass, energy_sources, topology="smart_meter")
    result = await _choose_mode_and_meter(
        hass, result, energy_sources, "aggregate_shares"
    )
    result = await _add_consumer(
        hass,
        result,
        mode="aggregate_shares",
        name="Wallbox",
        assignment="12.500",
    )
    result = await _configure(hass, result, {"action": "finish"})

    assert result["step_id"] == "factors"
    draft = factor_steps[-1].configuration_draft
    assert draft["topology"] == "smart_meter"
    assert draft["sources"] == {
        "grid_import": energy_sources["grid_import"].id,
        "grid_export": energy_sources["grid_export"].id,
    }
    consumption = draft["consumption"]
    assert consumption["household_source"] == energy_sources["aggregate_load"].id
    assert consumption["consumers"] == [
        {
            "consumer_id": consumption["consumers"][0]["consumer_id"],
            "name": "Wallbox",
            "share": "0.125",
        }
    ]
    assert not hass.config_entries.async_entries(DOMAIN)
    hass.config_entries.flow.async_abort(result["flow_id"])


@pytest.mark.parametrize(
    ("percentage", "expected_ratio"),
    [("0.000", "0"), ("100.000", "1")],
)
async def test_aggregate_wallbox_accepts_exact_inclusive_share_boundaries(
    hass: HomeAssistant,
    energy_sources: dict[str, er.RegistryEntry],
    factor_steps: list[ConsumerFlowSteps],
    percentage: str,
    expected_ratio: str,
) -> None:
    """Zero attribution and zero household remainder are both explicit drafts."""
    result = await _start_config_consumers(hass, energy_sources)
    result = await _choose_mode_and_meter(
        hass, result, energy_sources, "aggregate_shares"
    )
    result = await _add_consumer(
        hass,
        result,
        mode="aggregate_shares",
        name="Wallbox",
        assignment=percentage,
    )
    result = await _configure(hass, result, {"action": "finish"})

    assert result["step_id"] == "factors"
    consumers = factor_steps[-1].configuration_draft["consumption"]["consumers"]
    assert consumers == [
        {
            "consumer_id": consumers[0]["consumer_id"],
            "name": "Wallbox",
            "share": expected_ratio,
        }
    ]
    assert len(consumers[0]["consumer_id"]) == 32
    assert not hass.config_entries.async_entries(DOMAIN)
    hass.config_entries.flow.async_abort(result["flow_id"])


async def test_aggregate_share_sum_over_100_blocks_finish(
    hass: HomeAssistant,
    energy_sources: dict[str, er.RegistryEntry],
    factor_steps: list[ConsumerFlowSteps],
) -> None:
    """Individually valid exact shares cannot over-allocate measured load."""
    result = await _start_config_consumers(hass, energy_sources)
    result = await _choose_mode_and_meter(
        hass, result, energy_sources, "aggregate_shares"
    )
    result = await _add_consumer(
        hass,
        result,
        mode="aggregate_shares",
        name="Wallbox",
        assignment="60",
    )
    result = await _add_consumer(
        hass,
        result,
        mode="aggregate_shares",
        name="Heat pump",
        assignment="40.0000000000000000001",
    )
    result = await _configure(hass, result, {"action": "finish"})

    assert result["step_id"] == "consumer_menu"
    assert result["errors"] == {"base": "shares_exceed_total"}
    assert result["description_placeholders"]["consumer_summary"] == (
        "1. Wallbox — 60 %\n2. Heat pump — 40.0000000000000000001 %"
    )
    assert factor_steps == []
    assert not hass.config_entries.async_entries(DOMAIN)
    hass.config_entries.flow.async_abort(result["flow_id"])


async def test_separate_wallbox_resolves_distinct_meter_and_reaches_factors(
    hass: HomeAssistant,
    energy_sources: dict[str, er.RegistryEntry],
    factor_steps: list[ConsumerFlowSteps],
) -> None:
    """A confirmed non-overlapping Wallbox source joins the physical vector."""
    result = await _start_config_consumers(hass, energy_sources)
    result = await _choose_mode_and_meter(
        hass, result, energy_sources, "separate_meters"
    )
    result = await _add_consumer(
        hass,
        result,
        mode="separate_meters",
        name="Wallbox",
        assignment=energy_sources["wallbox_load"].entity_id,
    )
    result = await _configure(hass, result, {"action": "finish"})

    assert result["step_id"] == "factors"
    consumers = factor_steps[-1].configuration_draft["consumption"]["consumers"]
    assert consumers == [
        {
            "consumer_id": consumers[0]["consumer_id"],
            "name": "Wallbox",
            "source": energy_sources["wallbox_load"].id,
        }
    ]
    assert not hass.config_entries.async_entries(DOMAIN)
    hass.config_entries.flow.async_abort(result["flow_id"])


@pytest.mark.parametrize(
    "confirmation",
    [
        pytest.param(False, id="unchecked"),
        pytest.param(None, id="omitted"),
    ],
)
async def test_separate_consumer_requires_explicit_confirmation_on_retry(
    hass: HomeAssistant,
    energy_sources: dict[str, er.RegistryEntry],
    factor_steps: list[ConsumerFlowSteps],
    *,
    confirmation: bool | None,
) -> None:
    """A source is never accepted as a consumer without explicit direction consent."""
    result = await _start_config_consumers(hass, energy_sources)
    result = await _choose_mode_and_meter(
        hass, result, energy_sources, "separate_meters"
    )
    result = await _configure(hass, result, {"action": "add"})
    submission: dict[str, object] = {
        "name": "Wallbox",
        "source": energy_sources["wallbox_load"].entity_id,
    }
    if confirmation is not None:
        submission["consumer_measurement_confirmed"] = confirmation
    result = await _configure(hass, result, submission)

    assert result["step_id"] == "consumer_add"
    assert result["errors"] == {
        "consumer_measurement_confirmed": "consumer_confirmation_required"
    }
    source_marker, _source_selector = _schema_field(result, "source")
    confirmation_marker, _confirmation_selector = _schema_field(
        result, "consumer_measurement_confirmed"
    )
    assert source_marker.description["suggested_value"] == (
        energy_sources["wallbox_load"].entity_id
    )
    assert confirmation_marker.default() is False
    assert factor_steps == []

    result = await _configure(
        hass,
        result,
        {
            "name": "Wallbox",
            "source": energy_sources["wallbox_load"].entity_id,
            "consumer_measurement_confirmed": True,
        },
    )
    result = await _configure(hass, result, {"action": "finish"})
    assert result["step_id"] == "factors"
    assert (
        factor_steps[-1].configuration_draft["consumption"]["consumers"][0]["source"]
        == energy_sources["wallbox_load"].id
    )
    hass.config_entries.flow.async_abort(result["flow_id"])


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            ("household_load", False, "duplicate_source"),
            id="duplicate-household",
        ),
        pytest.param(
            ("grid_import", False, "invalid_source_vector"),
            id="duplicate-upstream",
        ),
        pytest.param(
            ("wallbox_load", True, "invalid_unit"),
            id="invalid-current-state",
        ),
    ],
)
async def test_separate_duplicate_or_invalid_consumer_blocks_finish(
    hass: HomeAssistant,
    energy_sources: dict[str, er.RegistryEntry],
    factor_steps: list[ConsumerFlowSteps],
    case: tuple[str, bool, str],
) -> None:
    """Finish revalidates overlap and current semantics of every local meter."""
    source_role, invalidate_after_add, expected_error = case
    result = await _start_config_consumers(hass, energy_sources)
    result = await _choose_mode_and_meter(
        hass, result, energy_sources, "separate_meters"
    )
    source = energy_sources[source_role]
    result = await _add_consumer(
        hass,
        result,
        mode="separate_meters",
        name="Wallbox",
        assignment=source.entity_id,
    )
    if invalidate_after_add:
        state = hass.states.get(source.entity_id)
        assert state is not None
        hass.states.async_set(
            source.entity_id,
            state.state,
            {**state.attributes, "unit_of_measurement": "W"},
        )
    result = await _configure(hass, result, {"action": "finish"})

    assert result["step_id"] == "consumer_menu"
    assert result["errors"] == {"base": expected_error}
    assert factor_steps == []
    assert not hass.config_entries.async_entries(DOMAIN)
    hass.config_entries.flow.async_abort(result["flow_id"])


async def test_separate_source_identity_survives_entity_id_reuse(
    hass: HomeAssistant,
    energy_sources: dict[str, er.RegistryEntry],
    factor_steps: list[ConsumerFlowSteps],
) -> None:
    """Finish follows the selected registry UUID, never a reused entity ID."""
    result = await _start_config_consumers(hass, energy_sources)
    result = await _choose_mode_and_meter(
        hass, result, energy_sources, "separate_meters"
    )
    original = energy_sources["wallbox_load"]
    result = await _add_consumer(
        hass,
        result,
        mode="separate_meters",
        name="Wallbox",
        assignment=original.entity_id,
    )

    original_state = hass.states.get(original.entity_id)
    assert original_state is not None
    registry = er.async_get(hass)
    renamed = registry.async_update_entity(
        original.entity_id,
        new_entity_id="sensor.renamed_wallbox_load",
    )
    hass.states.async_set(
        renamed.entity_id,
        original_state.state,
        original_state.attributes,
    )
    hass.states.async_remove(original.entity_id)
    replacement = registry.async_get_or_create(
        "sensor",
        "consumer_flow_replacement",
        "wallbox_load",
        suggested_object_id="wallbox_load",
    )
    assert replacement.entity_id == original.entity_id
    hass.states.async_set(
        replacement.entity_id,
        original_state.state,
        original_state.attributes,
    )
    result = await _configure(hass, result, {"action": "finish"})

    assert result["step_id"] == "factors"
    consumer = factor_steps[-1].configuration_draft["consumption"]["consumers"][0]
    assert consumer["source"] == original.id
    assert consumer["source"] == renamed.id
    assert consumer["source"] != replacement.id
    hass.config_entries.flow.async_abort(result["flow_id"])


async def test_options_rename_preserves_consumer_identity_and_entry(
    hass: HomeAssistant,
    energy_sources: dict[str, er.RegistryEntry],
    factor_steps: list[ConsumerFlowSteps],
) -> None:
    """Options rename edits a detached data-authoritative plan under the same UUID."""
    plan = _consumption_plan(energy_sources, "aggregate_shares")
    entry, original_data, original_options = _add_entry(hass, energy_sources, plan)
    result = await _start_options_consumers(hass, entry)
    mode_marker, _mode_selector = _schema_field(result, "mode")
    assert mode_marker.description["suggested_value"] == "aggregate_shares"
    result = await _choose_mode_and_meter(
        hass, result, energy_sources, "aggregate_shares"
    )
    result = await _configure(hass, result, {"action": "edit"})
    assert _consumer_selector_options(result) == [
        {"value": _WALLBOX_ID, "label": "1. Wallbox"}
    ]
    result = await _configure(hass, result, {"consumer_id": _WALLBOX_ID})
    name_marker, _name_selector = _schema_field(result, "name")
    share_marker, share_selector = _schema_field(result, "share_percent")
    assert name_marker.description["suggested_value"] == "Wallbox"
    assert share_marker.description["suggested_value"] == "25"
    assert isinstance(share_selector, TextSelector)
    assert share_selector.config["type"] == "text"
    assert share_selector.config["suffix"] == "%"
    result = await _configure(
        hass,
        result,
        {"name": "EV charger", "share_percent": "25.000"},
    )
    assert result["description_placeholders"]["consumer_summary"] == (
        "1. EV charger — 25 %"
    )

    result = await _configure(hass, result, {"action": "edit"})
    assert _consumer_selector_options(result) == [
        {"value": _WALLBOX_ID, "label": "1. EV charger"}
    ]
    result = await _configure(hass, result, {"consumer_id": _WALLBOX_ID})
    result = await _configure(
        hass,
        result,
        {"name": "EV charger", "share_percent": "25"},
    )
    result = await _configure(hass, result, {"action": "finish"})

    assert result["step_id"] == "factors"
    flow = factor_steps[-1]
    consumption = flow.configuration_draft["consumption"]
    assert consumption["consumers"] == [
        {
            "consumer_id": _WALLBOX_ID,
            "name": "EV charger",
            "share": "0.25",
        }
    ]
    detached = flow.configuration_draft
    detached["consumption"]["consumers"].clear()
    assert flow.configuration_draft["consumption"]["consumers"]
    blocked = await _configure(hass, result, {})
    assert blocked["errors"] == {"base": "setup_incomplete"}
    assert dict(entry.data) == original_data
    assert dict(entry.options) == original_options
    assert hass.config_entries.async_entries(DOMAIN) == [entry]
    hass.config_entries.options.async_abort(blocked["flow_id"])


async def test_options_edit_replaces_separate_source_under_stable_consumer_id(
    hass: HomeAssistant,
    energy_sources: dict[str, er.RegistryEntry],
    factor_steps: list[ConsumerFlowSteps],
) -> None:
    """A separate meter can change without replacing the logical consumer."""
    plan = _consumption_plan(energy_sources, "separate_meters")
    entry, original_data, original_options = _add_entry(hass, energy_sources, plan)
    result = await _start_options_consumers(hass, entry)
    result = await _choose_mode_and_meter(
        hass, result, energy_sources, "separate_meters"
    )
    result = await _configure(hass, result, {"action": "edit"})
    result = await _configure(hass, result, {"consumer_id": _WALLBOX_ID})
    source_marker, _source_selector = _schema_field(result, "source")
    confirmation_marker, _confirmation_selector = _schema_field(
        result, "consumer_measurement_confirmed"
    )
    assert source_marker.description["suggested_value"] == (
        energy_sources["wallbox_load"].entity_id
    )
    assert confirmation_marker.default() is False
    assert "suggested_value" not in (confirmation_marker.description or {})

    result = await _configure(
        hass,
        result,
        {
            "name": "Wallbox",
            "source": energy_sources["heat_pump_load"].entity_id,
            "consumer_measurement_confirmed": True,
        },
    )
    assert result["description_placeholders"]["consumer_summary"] == (
        "1. Wallbox — sensor.heat_pump_load"
    )
    result = await _configure(hass, result, {"action": "finish"})

    assert result["step_id"] == "factors"
    consumers = factor_steps[-1].configuration_draft["consumption"]["consumers"]
    assert consumers == [
        {
            "consumer_id": _WALLBOX_ID,
            "name": "Wallbox",
            "source": energy_sources["heat_pump_load"].id,
        }
    ]
    assert dict(entry.data) == original_data
    assert dict(entry.options) == original_options
    hass.config_entries.options.async_abort(result["flow_id"])


async def test_options_remove_and_readd_uses_one_fresh_retry_stable_id(
    hass: HomeAssistant,
    energy_sources: dict[str, er.RegistryEntry],
    factor_steps: list[ConsumerFlowSteps],
) -> None:
    """Explicit removal ends an identity; the same later name gets a fresh UUID."""
    plan = _consumption_plan(energy_sources, "aggregate_shares")
    entry, original_data, original_options = _add_entry(hass, energy_sources, plan)
    result = await _start_options_consumers(hass, entry)
    result = await _choose_mode_and_meter(
        hass, result, energy_sources, "aggregate_shares"
    )
    result = await _configure(hass, result, {"action": "remove"})
    assert _consumer_selector_options(result) == [
        {"value": _WALLBOX_ID, "label": "1. Wallbox"}
    ]
    confirmation_marker, _confirmation_selector = _schema_field(
        result, "confirm_removal"
    )
    assert confirmation_marker.default() is False
    assert "suggested_value" not in (confirmation_marker.description or {})
    rejected = await _configure(
        hass,
        result,
        {"consumer_id": _WALLBOX_ID, "confirm_removal": False},
    )
    assert rejected["step_id"] == "consumer_remove"
    assert rejected["errors"] == {"confirm_removal": "removal_confirmation_required"}
    consumer_marker, _consumer_selector = _schema_field(rejected, "consumer_id")
    assert consumer_marker.description["suggested_value"] == _WALLBOX_ID
    result = await _configure(
        hass,
        rejected,
        {"consumer_id": _WALLBOX_ID, "confirm_removal": True},
    )
    _action_marker, action_selector = _schema_field(result, "action")
    assert isinstance(action_selector, SelectSelector)
    assert action_selector.config["options"] == ["add", "finish"]

    with patch(
        "custom_components.co2saver.consumer_flow.uuid4",
        return_value=UUID(hex=_READDED_WALLBOX_ID),
    ) as make_uuid:
        result = await _configure(hass, result, {"action": "add"})
        invalid = await _configure(
            hass,
            result,
            {"name": "", "share_percent": "25"},
        )
        assert invalid["step_id"] == "consumer_add"
        assert invalid["errors"] == {"name": "required"}
        result = await _configure(
            hass,
            invalid,
            {"name": "Wallbox", "share_percent": "25"},
        )
        make_uuid.assert_called_once_with()

    result = await _configure(hass, result, {"action": "edit"})
    assert _consumer_selector_options(result) == [
        {"value": _READDED_WALLBOX_ID, "label": "1. Wallbox"}
    ]
    result = await _configure(hass, result, {"consumer_id": _READDED_WALLBOX_ID})
    result = await _configure(
        hass,
        result,
        {"name": "Wallbox", "share_percent": "25"},
    )
    result = await _configure(hass, result, {"action": "finish"})

    consumers = factor_steps[-1].configuration_draft["consumption"]["consumers"]
    assert consumers[0]["consumer_id"] == _READDED_WALLBOX_ID
    assert consumers[0]["consumer_id"] != _WALLBOX_ID
    assert dict(entry.data) == original_data
    assert dict(entry.options) == original_options
    hass.config_entries.options.async_abort(result["flow_id"])


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            (
                "aggregate_shares",
                "separate_meters",
                "separate_load",
                "household_load",
                "source",
                "wallbox_load",
            ),
            id="aggregate-to-separate",
        ),
        pytest.param(
            (
                "separate_meters",
                "aggregate_shares",
                "aggregate_load",
                "aggregate_load",
                "share_percent",
                "33.3333333333333333333",
            ),
            id="separate-to-aggregate",
        ),
    ],
)
async def test_options_mode_switch_keeps_ids_but_requires_reassignment(
    hass: HomeAssistant,
    energy_sources: dict[str, er.RegistryEntry],
    factor_steps: list[ConsumerFlowSteps],
    case: tuple[str, str, str, str, str, str],
) -> None:
    """A mode switch never reinterprets an old source as a share or vice versa."""
    initial_mode, target_mode, load_step, load_role, field, assignment = case
    plan = _consumption_plan(energy_sources, initial_mode)
    entry, original_data, original_options = _add_entry(hass, energy_sources, plan)
    result = await _start_options_consumers(hass, entry)
    result = await _configure(hass, result, {"mode": target_mode})
    assert result["step_id"] == load_step
    household_marker, _household_selector = _schema_field(result, "household_source")
    assert "suggested_value" not in (household_marker.description or {})
    result = await _configure(
        hass,
        result,
        {
            "household_source": energy_sources[load_role].entity_id,
            "load_measurement_confirmed": True,
        },
    )
    assert result["description_placeholders"]["consumer_summary"] == ("1. Wallbox — —")
    blocked = await _configure(hass, result, {"action": "finish"})
    assert blocked["step_id"] == "consumer_menu"
    assert blocked["errors"] == {"base": "required"}
    assert factor_steps == []

    result = await _configure(hass, blocked, {"action": "edit"})
    assert _consumer_selector_options(result) == [
        {"value": _WALLBOX_ID, "label": "1. Wallbox"}
    ]
    result = await _configure(hass, result, {"consumer_id": _WALLBOX_ID})
    name_marker, _name_selector = _schema_field(result, "name")
    assignment_marker, _assignment_selector = _schema_field(result, field)
    assert name_marker.description["suggested_value"] == "Wallbox"
    assert "suggested_value" not in (assignment_marker.description or {})
    editor_input: dict[str, object] = {"name": "Wallbox"}
    if target_mode == "separate_meters":
        editor_input.update(
            source=energy_sources[assignment].entity_id,
            consumer_measurement_confirmed=True,
        )
    else:
        editor_input["share_percent"] = assignment
    result = await _configure(hass, result, editor_input)
    result = await _configure(hass, result, {"action": "finish"})

    assert result["step_id"] == "factors"
    consumption = factor_steps[-1].configuration_draft["consumption"]
    assert consumption["mode"] == target_mode
    assert consumption["household_id"] == _HOUSEHOLD_ID
    assert consumption["consumers"][0]["consumer_id"] == _WALLBOX_ID
    assert consumption["consumers"][0]["name"] == "Wallbox"
    if target_mode == "separate_meters":
        assert consumption["consumers"][0]["source"] == energy_sources[assignment].id
    else:
        assert consumption["consumers"][0]["share"] == ("0.333333333333333333333")
    assert dict(entry.data) == original_data
    assert dict(entry.options) == original_options
    hass.config_entries.options.async_abort(result["flow_id"])
