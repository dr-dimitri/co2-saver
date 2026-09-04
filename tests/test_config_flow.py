# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Public flow-result tests for staged PV/grid setup and source replacement."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.co2saver.config_flow import Co2SaverConfigFlow
from custom_components.co2saver.const import ATTR_CO2SAVER_PERIOD_END, DOMAIN

if TYPE_CHECKING:
    from collections.abc import Iterator

    from homeassistant.config_entries import ConfigFlowResult
    from homeassistant.core import HomeAssistant

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.fixture
def storage_steps() -> Iterator[list[Co2SaverConfigFlow]]:
    """Observe the public next-step handoff while executing its real behavior."""
    flows: list[Co2SaverConfigFlow] = []
    original = Co2SaverConfigFlow.async_step_storage

    async def capture(
        flow: Co2SaverConfigFlow, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        flows.append(flow)
        return await original(flow, user_input)

    with patch.object(
        Co2SaverConfigFlow, "async_step_storage", autospec=True, side_effect=capture
    ):
        yield flows


@pytest.fixture
def sources(hass: HomeAssistant) -> dict[str, er.RegistryEntry]:
    """Publish registered direction-separated counters from one physical period."""
    registry = er.async_get(hass)
    period_end = dt_util.utcnow().isoformat()
    entries = {}
    for role in ("pv_generation", "grid_import", "grid_export", "pv_plausibility"):
        entry = registry.async_get_or_create("sensor", "test", role)
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


def _selection(
    sources: dict[str, er.RegistryEntry], topology: str, *, plausibility: bool = False
) -> dict[str, Any]:
    """Build the user-visible selection, never internal source UUIDs."""
    roles = ["grid_import", "grid_export"]
    if topology == "inverter":
        roles.append("pv_generation")
    elif plausibility:
        roles.append("pv_plausibility")
    return {
        **{role: sources[role].entity_id for role in roles},
        "synchronous_sources_confirmed": True,
    }


@pytest.mark.parametrize(
    ("topology", "plausibility"),
    [("inverter", False), ("smart_meter", False), ("smart_meter", True)],
)
async def test_sources_reach_storage_without_committing(
    hass: HomeAssistant,
    sources: dict[str, er.RegistryEntry],
    storage_steps: list[Co2SaverConfigFlow],
    topology: str,
    *,
    plausibility: bool,
) -> None:
    """Both topologies produce an isolated JSON draft, no entry/store/listener."""
    with (
        patch("homeassistant.helpers.storage.Store.async_save") as save,
        patch(
            "custom_components.co2saver.measurement.ha.UtcMinuteRunner.start"
        ) as start,
        patch(
            "homeassistant.helpers.helper_integration.async_handle_source_entity_changes"
        ) as listen,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "user"
        assert result["last_step"] is False
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"topology": topology}
        )
        assert result["step_id"] == "sources"
        fields = {str(key) for key in result["data_schema"].schema}
        expected = {"grid_import", "grid_export", "synchronous_sources_confirmed"}
        expected.add("pv_generation" if topology == "inverter" else "pv_plausibility")
        assert fields == expected
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], _selection(sources, topology, plausibility=plausibility)
        )
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == "storage"
        assert result["errors"] == {}
        flow = storage_steps[-1]
        draft = flow.configuration_draft
        assert json.loads(json.dumps(draft)) == draft
        assert draft["topology"] == topology
        assert draft["synchronous_sources_confirmed"] is True
        assert draft["sources"]["grid_import"] == sources["grid_import"].id
        assert draft["sources"]["grid_export"] == sources["grid_export"].id
        assert draft["plant_key"] == "grid:" + ":".join(
            sorted([sources["grid_import"].id, sources["grid_export"].id])
        )
        assert ("pv_plausibility" in draft["sources"]) is plausibility
        assert flow.unique_id is None
        draft["sources"].clear()
        assert flow.configuration_draft["sources"]
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"battery_present": "without_battery"}
        )
        assert result["step_id"] == "consumers"
        assert result["errors"] == {}
        with pytest.raises(InvalidData) as error:
            await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert error.value.path == ["mode"]
        hass.config_entries.flow.async_abort(result["flow_id"])
        assert not hass.config_entries.async_entries(DOMAIN)
        save.assert_not_called()
        start.assert_not_called()
        listen.assert_not_called()


@pytest.mark.parametrize("topology", [None, "power", [], 1])
async def test_invalid_topology(hass: HomeAssistant, topology: object) -> None:
    """Reject malformed or unsupported topology choices before source selection."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}, data={"topology": topology}
    )
    assert result["step_id"] == "user"
    assert result["errors"] == {"topology": "invalid_topology"}


@pytest.mark.parametrize("use_registry_ids", [False, True])
@pytest.mark.parametrize(
    ("change", "field", "error"),
    [
        ("unit", "grid_import", "invalid_unit"),
        ("state_class", "grid_import", "invalid_state_class"),
        ("duplicate", "grid_export", "duplicate_source"),
        ("confirmation", "synchronous_sources_confirmed", "confirmation_required"),
        ("period", "grid_import", "invalid_period_end"),
        ("unavailable", "grid_import", "source_unavailable"),
    ],
)
async def test_source_errors_are_actionable_and_retryable(  # noqa: PLR0913
    hass: HomeAssistant,
    sources: dict[str, er.RegistryEntry],
    change: str,
    field: str,
    error: str,
    *,
    use_registry_ids: bool,
) -> None:
    """Current semantic failures stay on the source form and allow correction."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}, data={"topology": "inverter"}
    )
    selection = _selection(sources, "inverter")
    if use_registry_ids:
        selection.update(
            {role: sources[role].id for role in selection if role in sources}
        )
    grid = sources["grid_import"]
    state = hass.states.get(grid.entity_id)
    assert state is not None
    attributes = dict(state.attributes)
    if change == "unit":
        hass.states.async_set(
            grid.entity_id, "100", {**attributes, "unit_of_measurement": "W"}
        )
    elif change == "state_class":
        hass.states.async_set(
            grid.entity_id, "100", {**attributes, "state_class": "measurement"}
        )
    elif change == "duplicate":
        selection["grid_export"] = selection["grid_import"]
    elif change == "confirmation":
        selection["synchronous_sources_confirmed"] = False
    elif change == "period":
        invalid_attributes = dict(attributes)
        invalid_attributes.pop(ATTR_CO2SAVER_PERIOD_END)
        hass.states.async_set(grid.entity_id, "100", invalid_attributes)
    else:
        hass.states.async_set(grid.entity_id, "unavailable", attributes)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], selection
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "sources"
    assert result["errors"][field] == error
    fields = {str(key): key for key in result["data_schema"].schema}
    assert (
        fields["pv_generation"].description["suggested_value"]
        == sources["pv_generation"].entity_id
    )
    assert not hass.config_entries.async_entries(DOMAIN)
    hass.states.async_set(grid.entity_id, "100", attributes)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _selection(sources, "inverter")
    )
    assert result["step_id"] == "storage"
    assert result["errors"] == {}


async def test_missing_required_source_stays_on_form(
    hass: HomeAssistant, sources: dict[str, er.RegistryEntry]
) -> None:
    """Home Assistant's required-field schema rejects an incomplete topology."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}, data={"topology": "inverter"}
    )
    selection = _selection(sources, "inverter")
    selection.pop("grid_export")
    with pytest.raises(InvalidData) as exception:
        await hass.config_entries.flow.async_configure(result["flow_id"], selection)
    assert exception.value.path == ["grid_export"]
    assert not hass.config_entries.async_entries(DOMAIN)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _selection(sources, "inverter")
    )
    assert result["step_id"] == "storage"


@pytest.mark.parametrize("swapped", [False, True])
async def test_duplicate_plant_aborts_across_topologies(
    hass: HomeAssistant,
    sources: dict[str, er.RegistryEntry],
    *,
    swapped: bool,
) -> None:
    """A reversed import/export selection still identifies the same boundary."""
    plant_key = "grid:" + ":".join(
        sorted([sources["grid_import"].id, sources["grid_export"].id])
    )
    entry = MockConfigEntry(domain=DOMAIN, data={"plant_key": plant_key})
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}, data={"topology": "smart_meter"}
    )
    selection = _selection(sources, "smart_meter")
    if swapped:
        selection["grid_import"], selection["grid_export"] = (
            selection["grid_export"],
            selection["grid_import"],
        )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], selection
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


@pytest.mark.parametrize("replace_grid", [False, True])
async def test_reconfigure_preserves_history_and_resolves_renames(
    hass: HomeAssistant,
    sources: dict[str, er.RegistryEntry],
    storage_steps: list[Co2SaverConfigFlow],
    *,
    replace_grid: bool,
) -> None:
    """Source changes stage a separate draft without touching persisted identity."""
    original = {
        "topology": "inverter",
        "sources": {
            role: entry.id
            for role, entry in sources.items()
            if role != "pv_plausibility"
        },
        "plant_key": "grid:"
        + ":".join(sorted([sources["grid_import"].id, sources["grid_export"].id])),
        "storage_id": "existing-storage-locator",
        "accounting_reference": {"generation": "existing-generation"},
        "synchronous_sources_confirmed": True,
    }
    entry = MockConfigEntry(
        domain=DOMAIN, data=deepcopy(original), options={"preserve": True}
    )
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    renamed = registry.async_update_entity(
        sources["grid_import"].entity_id, new_entity_id="sensor.renamed_grid_import"
    )
    state = hass.states.get(sources["grid_import"].entity_id)
    assert state is not None
    hass.states.async_set(renamed.entity_id, state.state, state.attributes)
    sources["grid_import"] = renamed
    with (
        patch("homeassistant.helpers.storage.Store.async_save") as save,
        patch.object(hass.config_entries, "async_reload") as reload_entry,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
        )
        assert result["step_id"] == "reconfigure"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"topology": "smart_meter"}
        )
        schema = result["data_schema"].schema
        fields = {str(key): key for key in schema}
        assert fields["grid_import"].description["suggested_value"] == renamed.entity_id
        assert "suggested_value" not in (
            fields["synchronous_sources_confirmed"].description or {}
        )
        selection = _selection(sources, "smart_meter")
        if replace_grid:
            selection["grid_import"] = sources["pv_plausibility"].entity_id
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], selection
        )
        assert result["step_id"] == "storage"
        flow = storage_steps[-1]
        draft = flow.configuration_draft
        assert draft["topology"] == "smart_meter"
        assert "pv_generation" not in draft["sources"]
        assert (draft["plant_key"] != original["plant_key"]) is replace_grid
        assert draft["storage_id"] == original["storage_id"]
        draft["accounting_reference"]["generation"] = "changed-copy"
        assert (
            flow.configuration_draft["accounting_reference"]
            == original["accounting_reference"]
        )
        assert dict(entry.data) == original
        assert dict(entry.options) == {"preserve": True}
        assert entry.unique_id is None
        save.assert_not_called()
        reload_entry.assert_not_called()
        hass.config_entries.flow.async_abort(result["flow_id"])


async def test_reconfigure_cannot_take_another_plant(
    hass: HomeAssistant, sources: dict[str, er.RegistryEntry]
) -> None:
    """Exclude only the source entry, never another existing plant."""
    entry = MockConfigEntry(domain=DOMAIN, data={"plant_key": "grid:old:pair"})
    other = MockConfigEntry(
        domain=DOMAIN,
        data={
            "plant_key": "grid:"
            + ":".join(sorted([sources["grid_import"].id, sources["grid_export"].id]))
        },
    )
    entry.add_to_hass(hass)
    other.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
        data={"topology": "smart_meter"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], _selection(sources, "smart_meter")
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data["plant_key"] == "grid:old:pair"
