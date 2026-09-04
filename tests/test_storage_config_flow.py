# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Public flow-result tests for optional battery storage configuration."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import SelectSelector, TextSelector
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.co2saver.config_flow import Co2SaverConfigFlow
from custom_components.co2saver.const import ATTR_CO2SAVER_PERIOD_END, DOMAIN

if TYPE_CHECKING:
    from collections.abc import Iterator

    from homeassistant.config_entries import ConfigFlowResult
    from homeassistant.core import HomeAssistant

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

_BATTERY_IDENTITY = "existing-physical-battery"
_LONG_CAPACITY = "123.456789012345678901234567890123456789"
_LONG_RATIO = "0.123456789012345678901234567890123456789"
_LONG_PERCENT = "12.3456789012345678901234567890123456789"


@pytest.fixture(autouse=True)
def no_incomplete_flow_side_effects(hass: HomeAssistant) -> Iterator[None]:
    """Prove that issue #6 only mutates its isolated in-memory draft."""
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
def consumer_steps() -> Iterator[list[Co2SaverConfigFlow]]:
    """Observe the real handoff to issue #7 through the public flow method."""
    flows: list[Co2SaverConfigFlow] = []
    original = Co2SaverConfigFlow.async_step_consumers

    async def capture(
        flow: Co2SaverConfigFlow, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        flows.append(flow)
        return await original(flow, user_input)

    with patch.object(
        Co2SaverConfigFlow,
        "async_step_consumers",
        autospec=True,
        side_effect=capture,
    ):
        yield flows


@pytest.fixture
def energy_sources(hass: HomeAssistant) -> dict[str, er.RegistryEntry]:
    """Publish every supported role at one fresh physical period."""
    registry = er.async_get(hass)
    period_end = (dt_util.utcnow() - timedelta(seconds=1)).isoformat()
    entries: dict[str, er.RegistryEntry] = {}
    for role in (
        "pv_generation",
        "grid_import",
        "grid_export",
        "pv_plausibility",
        "battery_charge",
        "battery_discharge",
        "alternative_charge",
        "alternative_discharge",
    ):
        entry = registry.async_get_or_create(
            "sensor", "storage_flow_test", role, suggested_object_id=role
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


def _source_selection(
    sources: dict[str, er.RegistryEntry], topology: str
) -> dict[str, object]:
    """Select the complete non-battery vector through current entity IDs."""
    roles = (
        ("pv_generation", "grid_import", "grid_export")
        if topology == "inverter"
        else ("grid_import", "grid_export")
    )
    return {
        **{role: sources[role].entity_id for role in roles},
        "synchronous_sources_confirmed": True,
    }


def _battery_input(
    sources: dict[str, er.RegistryEntry], *, use_registry_ids: bool = False
) -> dict[str, object]:
    """Build one exact storage submission in either accepted identity notation."""
    charge = sources["battery_charge"]
    discharge = sources["battery_discharge"]
    return {
        "battery_charge": charge.id if use_registry_ids else charge.entity_id,
        "battery_discharge": discharge.id if use_registry_ids else discharge.entity_id,
        "usable_capacity_kwh": "13.500",
        "round_trip_efficiency_percent": "90.00",
        "battery_sources_confirmed": True,
    }


def _existing_battery_input(
    sources: dict[str, er.RegistryEntry], *, identity: str
) -> dict[str, object]:
    """Re-submit the exact long persisted scalars with an explicit identity choice."""
    values = _battery_input(sources)
    values.update(
        {
            "usable_capacity_kwh": _LONG_CAPACITY,
            "round_trip_efficiency_percent": _LONG_PERCENT,
            "battery_identity": identity,
        }
    )
    return values


def _schema_field(result: ConfigFlowResult, field: str) -> tuple[vol.Marker, object]:
    """Return one marker and selector from a public form result."""
    for marker, selector in result["data_schema"].schema.items():
        if str(marker) == field:
            return marker, selector
    message = f"missing field: {field}"
    raise AssertionError(message)


async def _start_user_storage(
    hass: HomeAssistant,
    sources: dict[str, er.RegistryEntry],
    topology: str,
) -> ConfigFlowResult:
    """Advance a new public flow through its already accepted source step."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
        data={"topology": topology},
    )
    assert result["step_id"] == "sources"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _source_selection(sources, topology)
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "storage"
    return result


async def _start_reconfigure_storage(
    hass: HomeAssistant,
    sources: dict[str, er.RegistryEntry],
    entry: MockConfigEntry,
) -> ConfigFlowResult:
    """Advance reconfiguration while keeping the authoritative entry untouched."""
    topology = entry.data["topology"]
    assert isinstance(topology, str)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
        data={"topology": topology},
    )
    assert result["step_id"] == "sources"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _source_selection(sources, topology)
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "storage"
    return result


def _configuration_data(
    sources: dict[str, er.RegistryEntry],
    battery: dict[str, str] | None,
) -> dict[str, Any]:
    """Build an existing entry with history that storage edits must preserve."""
    grid_ids = sorted([sources["grid_import"].id, sources["grid_export"].id])
    return {
        "topology": "inverter",
        "sources": {
            role: sources[role].id
            for role in ("pv_generation", "grid_import", "grid_export")
        },
        "plant_key": "grid:" + ":".join(grid_ids),
        "synchronous_sources_confirmed": True,
        "battery": deepcopy(battery),
        "storage_id": "stable-store-locator",
        "accounting_reference": {"generation": "preserved-generation"},
    }


@pytest.fixture
def configured_battery_entry(
    hass: HomeAssistant, energy_sources: dict[str, er.RegistryEntry]
) -> tuple[MockConfigEntry, dict[str, Any]]:
    """Install an entry containing one physical battery and exact long scalars."""
    battery = {
        "battery_id": _BATTERY_IDENTITY,
        "charge_source": energy_sources["battery_charge"].id,
        "discharge_source": energy_sources["battery_discharge"].id,
        "usable_capacity_kwh": _LONG_CAPACITY,
        "round_trip_efficiency": _LONG_RATIO,
    }
    original = _configuration_data(energy_sources, battery)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=deepcopy(original),
        options={"preserve": {"history": True}},
    )
    entry.add_to_hass(hass)
    return entry, original


@pytest.mark.parametrize("topology", ["inverter", "smart_meter"])
async def test_explicit_no_battery_reaches_consumers_without_side_effects(
    hass: HomeAssistant,
    energy_sources: dict[str, er.RegistryEntry],
    consumer_steps: list[Co2SaverConfigFlow],
    topology: str,
) -> None:
    """Both topologies require an explicit no-battery choice and commit nothing."""
    result = await _start_user_storage(hass, energy_sources, topology)
    marker, selector = _schema_field(result, "battery_present")
    assert marker.default is vol.UNDEFINED
    assert "suggested_value" not in (marker.description or {})
    assert isinstance(selector, SelectSelector)
    assert selector.config["options"] == ["without_battery", "with_battery"]
    assert selector.config["translation_key"] == "battery_present"
    with pytest.raises(InvalidData) as exception:
        await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert exception.value.path == ["battery_present"]

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"battery_present": "without_battery"}
    )

    assert result["step_id"] == "consumers"
    flow = consumer_steps[-1]
    assert flow.configuration_draft["battery"] is None
    assert flow.battery_change_pending is False
    assert not hass.config_entries.async_entries(DOMAIN)
    retry = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert retry["errors"] == {"base": "setup_incomplete"}
    hass.config_entries.flow.async_abort(retry["flow_id"])


@pytest.mark.parametrize("topology", ["inverter", "smart_meter"])
async def test_new_battery_form_and_draft_are_exact_and_stable(
    hass: HomeAssistant,
    energy_sources: dict[str, er.RegistryEntry],
    consumer_steps: list[Co2SaverConfigFlow],
    topology: str,
) -> None:
    """A new battery has no capacity default and keeps one ID through retries."""
    result = await _start_user_storage(hass, energy_sources, topology)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"battery_present": "with_battery"}
    )
    assert result["step_id"] == "storage_sources"
    capacity_marker, capacity_selector = _schema_field(result, "usable_capacity_kwh")
    efficiency_marker, efficiency_selector = _schema_field(
        result, "round_trip_efficiency_percent"
    )
    confirmation_marker, _confirmation_selector = _schema_field(
        result, "battery_sources_confirmed"
    )
    assert capacity_marker.default is vol.UNDEFINED
    assert "suggested_value" not in (capacity_marker.description or {})
    assert isinstance(capacity_selector, TextSelector)
    assert capacity_selector.config["type"] == "text"
    assert capacity_selector.config["suffix"] == "kWh"
    assert efficiency_marker.default is vol.UNDEFINED
    assert efficiency_marker.description["suggested_value"] == "90"
    assert isinstance(efficiency_selector, TextSelector)
    assert efficiency_selector.config["type"] == "text"
    assert efficiency_selector.config["suffix"] == "%"
    assert confirmation_marker.default() is False
    assert "suggested_value" not in (confirmation_marker.description or {})
    assert "battery_identity" not in {
        str(field) for field in result["data_schema"].schema
    }

    incomplete = _battery_input(energy_sources)
    del incomplete["usable_capacity_kwh"]
    with pytest.raises(InvalidData) as exception:
        await hass.config_entries.flow.async_configure(result["flow_id"], incomplete)
    assert exception.value.path == ["usable_capacity_kwh"]

    submission = _battery_input(energy_sources)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], submission
    )
    assert result["step_id"] == "consumers"
    flow = consumer_steps[-1]
    battery = flow.configuration_draft["battery"]
    assert battery == {
        "battery_id": battery["battery_id"],
        "charge_source": energy_sources["battery_charge"].id,
        "discharge_source": energy_sources["battery_discharge"].id,
        "usable_capacity_kwh": "13.5",
        "round_trip_efficiency": "0.9",
    }
    assert len(battery["battery_id"]) == 32
    int(battery["battery_id"], 16)
    assert flow.battery_change_pending is True
    draft = flow.configuration_draft
    assert json.loads(json.dumps(draft)) == draft
    again = await flow.async_step_storage_sources(submission)
    assert again["step_id"] == "consumers"
    assert flow.configuration_draft["battery"]["battery_id"] == battery["battery_id"]
    assert not hass.config_entries.async_entries(DOMAIN)
    hass.config_entries.flow.async_abort(result["flow_id"])


@pytest.mark.parametrize("omit_confirmation", [False, True])
async def test_storage_requires_explicit_direction_confirmation(
    hass: HomeAssistant,
    energy_sources: dict[str, er.RegistryEntry],
    consumer_steps: list[Co2SaverConfigFlow],
    *,
    omit_confirmation: bool,
) -> None:
    """An unchecked or omitted physical-source confirmation cannot advance."""
    result = await _start_user_storage(hass, energy_sources, "inverter")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"battery_present": "with_battery"}
    )
    submission = _battery_input(energy_sources)
    if omit_confirmation:
        submission.pop("battery_sources_confirmed")
    else:
        submission["battery_sources_confirmed"] = False
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], submission
    )
    assert result["step_id"] == "storage_sources"
    assert result["errors"] == {
        "battery_sources_confirmed": "battery_confirmation_required"
    }
    assert consumer_steps == []
    assert not hass.config_entries.async_entries(DOMAIN)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _battery_input(energy_sources)
    )
    assert result["step_id"] == "consumers"
    hass.config_entries.flow.async_abort(result["flow_id"])


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(("0.1", "0.1", "0.1", "0.001"), id="lower-bounds"),
        pytest.param(("1000.000", "100.000", "1000", "1"), id="upper-bounds"),
    ],
)
async def test_public_flow_preserves_exact_storage_boundaries(
    hass: HomeAssistant,
    energy_sources: dict[str, er.RegistryEntry],
    consumer_steps: list[Co2SaverConfigFlow],
    case: tuple[str, str, str, str],
) -> None:
    """Accepted edge values reach the draft without float conversion."""
    capacity, efficiency, expected_capacity, expected_ratio = case
    result = await _start_user_storage(hass, energy_sources, "inverter")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"battery_present": "with_battery"}
    )
    submission = _battery_input(energy_sources)
    submission["usable_capacity_kwh"] = capacity
    submission["round_trip_efficiency_percent"] = efficiency
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], submission
    )

    battery = consumer_steps[-1].configuration_draft["battery"]
    assert battery["usable_capacity_kwh"] == expected_capacity
    assert battery["round_trip_efficiency"] == expected_ratio
    hass.config_entries.flow.async_abort(result["flow_id"])


@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            (
                "usable_capacity_kwh",
                "0.099999999999999999999999999999",
                "capacity_out_of_range",
            ),
            id="capacity-below",
        ),
        pytest.param(
            (
                "usable_capacity_kwh",
                "1000.000000000000000000000000001",
                "capacity_out_of_range",
            ),
            id="capacity-above",
        ),
        pytest.param(
            (
                "round_trip_efficiency_percent",
                "0",
                "efficiency_out_of_range",
            ),
            id="efficiency-zero",
        ),
        pytest.param(
            (
                "round_trip_efficiency_percent",
                "100.000000000000000000000000001",
                "efficiency_out_of_range",
            ),
            id="efficiency-above",
        ),
        pytest.param(
            (
                "usable_capacity_kwh",
                "13,5",
                "invalid_decimal_separator",
            ),
            id="decimal-comma",
        ),
        pytest.param(
            ("round_trip_efficiency_percent", "9e1", "invalid_number"),
            id="exponent",
        ),
    ],
)
async def test_storage_errors_preserve_exact_input_for_retry(
    hass: HomeAssistant,
    energy_sources: dict[str, er.RegistryEntry],
    consumer_steps: list[Co2SaverConfigFlow],
    case: tuple[str, str, str],
) -> None:
    """Rejected text stays visible while a corrected submission can continue."""
    field, value, error = case
    result = await _start_user_storage(hass, energy_sources, "inverter")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"battery_present": "with_battery"}
    )
    invalid = _battery_input(energy_sources)
    invalid[field] = value
    result = await hass.config_entries.flow.async_configure(result["flow_id"], invalid)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "storage_sources"
    assert result["errors"][field] == error
    marker, _selector = _schema_field(result, field)
    assert marker.description["suggested_value"] == value
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _battery_input(energy_sources)
    )
    assert result["step_id"] == "consumers"
    assert consumer_steps[-1].configuration_draft["battery"] is not None
    hass.config_entries.flow.async_abort(result["flow_id"])


async def test_registry_uuid_source_error_is_normalized_and_retryable(
    hass: HomeAssistant,
    energy_sources: dict[str, er.RegistryEntry],
    consumer_steps: list[Co2SaverConfigFlow],
) -> None:
    """A UUID submission returns current entity-ID UI state after semantic failure."""
    result = await _start_user_storage(hass, energy_sources, "inverter")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"battery_present": "with_battery"}
    )
    charge = energy_sources["battery_charge"]
    state = hass.states.get(charge.entity_id)
    assert state is not None
    attributes = dict(state.attributes)
    hass.states.async_set(
        charge.entity_id,
        state.state,
        {**attributes, "unit_of_measurement": "W"},
    )
    submission = _battery_input(energy_sources, use_registry_ids=True)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], submission
    )

    assert result["errors"]["battery_charge"] == "invalid_unit"
    marker, selector = _schema_field(result, "battery_charge")
    assert marker.description["suggested_value"] == charge.entity_id
    assert charge.entity_id in selector.config["include_entities"]
    hass.states.async_set(charge.entity_id, state.state, attributes)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], submission
    )
    assert result["step_id"] == "consumers"
    assert (
        consumer_steps[-1].configuration_draft["battery"]["charge_source"] == charge.id
    )
    hass.config_entries.flow.async_abort(result["flow_id"])


async def test_reconfigure_suggests_exact_values_but_not_physical_identity(
    hass: HomeAssistant,
    energy_sources: dict[str, er.RegistryEntry],
    configured_battery_entry: tuple[MockConfigEntry, dict[str, Any]],
    consumer_steps: list[Co2SaverConfigFlow],
) -> None:
    """Persisted decimals survive exponent shifting while identity stays required."""
    entry, original = configured_battery_entry
    original_options = deepcopy(dict(entry.options))
    result = await _start_reconfigure_storage(hass, energy_sources, entry)
    presence_marker, _presence_selector = _schema_field(result, "battery_present")
    assert presence_marker.description["suggested_value"] == "with_battery"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"battery_present": "with_battery"}
    )
    charge_marker, _charge_selector = _schema_field(result, "battery_charge")
    discharge_marker, _discharge_selector = _schema_field(result, "battery_discharge")
    capacity_marker, _capacity_selector = _schema_field(result, "usable_capacity_kwh")
    efficiency_marker, _efficiency_selector = _schema_field(
        result, "round_trip_efficiency_percent"
    )
    identity_marker, identity_selector = _schema_field(result, "battery_identity")
    confirmation_marker, _confirmation_selector = _schema_field(
        result, "battery_sources_confirmed"
    )
    assert (
        charge_marker.description["suggested_value"]
        == energy_sources["battery_charge"].entity_id
    )
    assert (
        discharge_marker.description["suggested_value"]
        == energy_sources["battery_discharge"].entity_id
    )
    assert capacity_marker.description["suggested_value"] == _LONG_CAPACITY
    assert efficiency_marker.description["suggested_value"] == _LONG_PERCENT
    assert identity_marker.default is vol.UNDEFINED
    assert "suggested_value" not in (identity_marker.description or {})
    assert isinstance(identity_selector, SelectSelector)
    assert identity_selector.config["options"] == [
        "same_physical_battery",
        "physical_battery_replaced",
    ]
    assert confirmation_marker.default() is False
    assert "suggested_value" not in (confirmation_marker.description or {})

    missing_identity = _existing_battery_input(
        energy_sources, identity="same_physical_battery"
    )
    del missing_identity["battery_identity"]
    with pytest.raises(InvalidData) as exception:
        await hass.config_entries.flow.async_configure(
            result["flow_id"], missing_identity
        )
    assert exception.value.path == ["battery_identity"]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        _existing_battery_input(energy_sources, identity="same_physical_battery"),
    )

    flow = consumer_steps[-1]
    assert flow.configuration_draft["battery"] == original["battery"]
    assert flow.battery_change_pending is False
    assert dict(entry.data) == original
    assert dict(entry.options) == original_options
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    hass.config_entries.flow.async_abort(result["flow_id"])


async def test_replacement_identity_is_cached_and_decision_is_transient(
    hass: HomeAssistant,
    energy_sources: dict[str, er.RegistryEntry],
    configured_battery_entry: tuple[MockConfigEntry, dict[str, Any]],
    consumer_steps: list[Co2SaverConfigFlow],
) -> None:
    """Retry/back keeps one replacement ID and always compares with the original."""
    entry, original = configured_battery_entry
    result = await _start_reconfigure_storage(hass, energy_sources, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"battery_present": "with_battery"}
    )
    replacement = _existing_battery_input(
        energy_sources, identity="physical_battery_replaced"
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], replacement
    )
    flow = consumer_steps[-1]
    replacement_id = flow.configuration_draft["battery"]["battery_id"]
    assert replacement_id != _BATTERY_IDENTITY
    assert flow.battery_change_pending is True
    assert "battery_identity" not in flow.configuration_draft["battery"]
    assert "battery_change_pending" not in flow.configuration_draft

    again = await flow.async_step_storage_sources(replacement)
    assert again["step_id"] == "consumers"
    assert flow.configuration_draft["battery"]["battery_id"] == replacement_id
    same = await flow.async_step_storage_sources(
        _existing_battery_input(energy_sources, identity="same_physical_battery")
    )
    assert same["step_id"] == "consumers"
    assert flow.configuration_draft["battery"] == original["battery"]
    assert flow.battery_change_pending is False
    await flow.async_step_storage_sources(replacement)
    assert flow.configuration_draft["battery"]["battery_id"] == replacement_id
    assert flow.battery_change_pending is True
    assert dict(entry.data) == original
    hass.config_entries.flow.async_abort(result["flow_id"])


async def test_remove_and_back_to_same_battery_restores_original_identity(
    hass: HomeAssistant,
    energy_sources: dict[str, er.RegistryEntry],
    configured_battery_entry: tuple[MockConfigEntry, dict[str, Any]],
    consumer_steps: list[Co2SaverConfigFlow],
) -> None:
    """An uncommitted removal can be reversed without inventing a battery ID."""
    entry, original = configured_battery_entry
    result = await _start_reconfigure_storage(hass, energy_sources, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"battery_present": "without_battery"}
    )
    flow = consumer_steps[-1]
    assert flow.configuration_draft["battery"] is None
    assert flow.battery_change_pending is True

    storage_sources = await flow.async_step_storage({"battery_present": "with_battery"})
    assert storage_sources["step_id"] == "storage_sources"
    identity_marker, _identity_selector = _schema_field(
        storage_sources, "battery_identity"
    )
    assert "suggested_value" not in (identity_marker.description or {})
    consumers = await flow.async_step_storage_sources(
        _existing_battery_input(energy_sources, identity="same_physical_battery")
    )
    assert consumers["step_id"] == "consumers"
    assert flow.configuration_draft["battery"] == original["battery"]
    assert flow.battery_change_pending is False
    assert dict(entry.data) == original
    hass.config_entries.flow.async_abort(result["flow_id"])


@pytest.mark.parametrize("change", ["source_pair", "capacity", "efficiency"])
async def test_same_battery_parameter_changes_keep_id_and_mark_pending(
    hass: HomeAssistant,
    energy_sources: dict[str, er.RegistryEntry],
    configured_battery_entry: tuple[MockConfigEntry, dict[str, Any]],
    consumer_steps: list[Co2SaverConfigFlow],
    change: str,
) -> None:
    """Every effective storage edit keeps the physical ID but changes the draft."""
    entry, original = configured_battery_entry
    result = await _start_reconfigure_storage(hass, energy_sources, entry)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"battery_present": "with_battery"}
    )
    submission = _existing_battery_input(
        energy_sources, identity="same_physical_battery"
    )
    if change == "source_pair":
        submission["battery_charge"] = energy_sources["alternative_charge"].entity_id
        expected_field = "charge_source"
        expected_value = energy_sources["alternative_charge"].id
    elif change == "capacity":
        submission["usable_capacity_kwh"] = "14.2500"
        expected_field = "usable_capacity_kwh"
        expected_value = "14.25"
    else:
        submission["round_trip_efficiency_percent"] = "87.500"
        expected_field = "round_trip_efficiency"
        expected_value = "0.875"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], submission
    )

    flow = consumer_steps[-1]
    battery = flow.configuration_draft["battery"]
    assert battery["battery_id"] == _BATTERY_IDENTITY
    assert battery[expected_field] == expected_value
    assert flow.battery_change_pending is True
    assert dict(entry.data) == original
    hass.config_entries.flow.async_abort(result["flow_id"])


async def test_readding_after_committed_absence_gets_one_new_stable_identity(
    hass: HomeAssistant,
    energy_sources: dict[str, er.RegistryEntry],
    consumer_steps: list[Co2SaverConfigFlow],
) -> None:
    """An entry without a battery has no old identity to reuse or confirm."""
    original = _configuration_data(energy_sources, None)
    entry = MockConfigEntry(domain=DOMAIN, data=deepcopy(original))
    entry.add_to_hass(hass)
    result = await _start_reconfigure_storage(hass, energy_sources, entry)
    presence_marker, _presence_selector = _schema_field(result, "battery_present")
    assert presence_marker.description["suggested_value"] == "without_battery"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"battery_present": "with_battery"}
    )
    assert "battery_identity" not in {
        str(field) for field in result["data_schema"].schema
    }
    capacity_marker, _capacity_selector = _schema_field(result, "usable_capacity_kwh")
    efficiency_marker, _efficiency_selector = _schema_field(
        result, "round_trip_efficiency_percent"
    )
    assert "suggested_value" not in (capacity_marker.description or {})
    assert efficiency_marker.description["suggested_value"] == "90"
    submission = _battery_input(energy_sources)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], submission
    )
    flow = consumer_steps[-1]
    battery_id = flow.configuration_draft["battery"]["battery_id"]
    await flow.async_step_storage_sources(submission)
    assert flow.configuration_draft["battery"]["battery_id"] == battery_id
    assert battery_id != _BATTERY_IDENTITY
    assert flow.battery_change_pending is True
    assert dict(entry.data) == original
    hass.config_entries.flow.async_abort(result["flow_id"])
