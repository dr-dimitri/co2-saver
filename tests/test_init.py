# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Verify owner-bound setup and source lifecycle through Home Assistant."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.co2saver.bootstrap import async_reserve_bootstrap, manifest_lock
from custom_components.co2saver.const import DOMAIN
from custom_components.co2saver.measurement.models import MeasurementPhase

if TYPE_CHECKING:
    from pathlib import Path

    from homeassistant.core import HomeAssistant

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.fixture
def plan(hass: HomeAssistant, tmp_path: Path) -> dict[str, Any]:
    """Isolate physical collision checks and create stable registry bindings."""
    hass.config.config_dir = str(tmp_path)
    registry = er.async_get(hass)
    identities = {
        role: registry.async_get_or_create("sensor", "test", role).id
        for role in ("pv_generation", "grid_import", "grid_export", "load", "grid_co2")
    }
    pair = sorted((identities["grid_import"], identities["grid_export"]))
    return {
        "topology": "inverter",
        "sources": {
            role: identities[role]
            for role in ("pv_generation", "grid_import", "grid_export")
        },
        "plant_key": f"grid:{pair[0]}:{pair[1]}",
        "synchronous_sources_confirmed": True,
        "battery": None,
        "consumption": {
            "mode": "aggregate_shares",
            "household_id": uuid4().hex,
            "household_source": identities["load"],
            "consumers": [],
        },
        "factors": {
            "grid_intensity_source": identities["grid_co2"],
            "grid_max_age_minutes": 60,
            "pv_factor": "40",
        },
    }


async def _new_entry(hass: HomeAssistant, plan: dict[str, Any]) -> MockConfigEntry:
    """Reserve a verified bootstrap before constructing its owner entry."""
    async with manifest_lock(hass):
        locator = await async_reserve_bootstrap(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="CO2 Saver",
        data={
            **deepcopy(plan),
            "storage_id": locator,
        },
    )
    entry.add_to_hass(hass)
    return entry


async def test_setup_reload_unload_preserves_verified_generation(
    hass: HomeAssistant, plan: dict[str, Any]
) -> None:
    """Battery entries restore identically and each setup starts one runner."""
    registry = er.async_get(hass)
    plan["battery"] = {
        "battery_id": uuid4().hex,
        "charge_source": registry.async_get_or_create("sensor", "test", "charge").id,
        "discharge_source": registry.async_get_or_create(
            "sensor", "test", "discharge"
        ).id,
        "usable_capacity_kwh": "10",
        "round_trip_efficiency": "0.9",
    }
    plan["factors"]["battery_factor"] = "20"
    entry = await _new_entry(hass, plan)
    with patch(
        "custom_components.co2saver.measurement.ha.UtcMinuteRunner.start"
    ) as start:
        assert await hass.config_entries.async_setup(entry.entry_id)
        assert entry.state is ConfigEntryState.LOADED
        initial = entry.runtime_data.state
        assert initial.measurement.phase is MeasurementPhase.AWAITING_SEGMENT_BASELINE
        assert initial.measurement.baseline is None
        for _ in range(3):
            assert await hass.config_entries.async_reload(entry.entry_id)
            assert entry.runtime_data.state == initial
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert entry.state is ConfigEntryState.NOT_LOADED
        assert start.call_count == 4


async def test_missing_configuration_is_permanent_setup_error(
    hass: HomeAssistant,
) -> None:
    """An old empty scaffold entry is never a valid pristine installation."""
    entry = MockConfigEntry(domain=DOMAIN, data={})
    entry.add_to_hass(hass)
    with patch(
        "custom_components.co2saver.runtime.async_handle_source_entity_changes"
    ) as listen:
        assert not await hass.config_entries.async_setup(entry.entry_id)
        assert entry.state is ConfigEntryState.SETUP_ERROR
        listen.assert_not_called()


async def test_rename_retains_segment_and_unload_removes_callbacks(
    hass: HomeAssistant, plan: dict[str, Any]
) -> None:
    """A mutable entity ID changes neither the locator nor the ledger."""
    entry = await _new_entry(hass, plan)
    assert await hass.config_entries.async_setup(entry.entry_id)
    state = entry.runtime_data.state
    registry = er.async_get(hass)
    source = registry.async_get(plan["sources"]["pv_generation"])
    assert source is not None
    registry.async_update_entity(source.entity_id, new_entity_id="sensor.renamed_pv")
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.state == state
    assert await hass.config_entries.async_unload(entry.entry_id)
    with patch.object(hass.config_entries, "async_reload") as reload_entry:
        registry.async_update_entity(
            "sensor.renamed_pv", new_entity_id="sensor.final_pv"
        )
        await hass.async_block_till_done()
        reload_entry.assert_not_called()


async def test_source_removal_stops_only_its_entry(
    hass: HomeAssistant, plan: dict[str, Any]
) -> None:
    """Source removal unloads listeners and rejects the missing binding."""
    entry = await _new_entry(hass, plan)
    assert await hass.config_entries.async_setup(entry.entry_id)
    registry = er.async_get(hass)
    source = registry.async_get(plan["sources"]["grid_import"])
    assert source is not None
    registry.async_remove(source.entity_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR


async def test_missing_initialized_generation_does_not_activate_listeners(
    hass: HomeAssistant, plan: dict[str, Any], hass_storage: dict[str, Any]
) -> None:
    """Missing authoritative state cannot become a fresh zero generation."""
    entry = await _new_entry(hass, plan)
    assert await hass.config_entries.async_setup(entry.entry_id)
    store_key = entry.runtime_data.store.store_key
    assert await hass.config_entries.async_unload(entry.entry_id)
    del hass_storage[store_key]
    with patch(
        "custom_components.co2saver.runtime.async_handle_source_entity_changes"
    ) as listen:
        assert not await hass.config_entries.async_setup(entry.entry_id)
        assert entry.state is ConfigEntryState.SETUP_ERROR
        listen.assert_not_called()
