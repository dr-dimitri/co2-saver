# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Release smoke from the complete public flow to an actual disk-backed restart."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta
from fractions import Fraction
from typing import TYPE_CHECKING, Any

from homeassistant import bootstrap, loader
from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import CoreState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_test_home_assistant

from custom_components.co2saver.bootstrap import manifest_store
from custom_components.co2saver.const import DOMAIN
from custom_components.co2saver.measurement.models import MeasurementPhase

from .test_runtime import _BASELINE, _INTERVAL, _START, _energy, _grid, _tick, timers

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from datetime import datetime
    from pathlib import Path

    import pytest
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant, State

    from .test_runtime import _Timer

__all__ = ("timers",)


@asynccontextmanager
async def _installation(config_dir: Path) -> AsyncIterator[HomeAssistant]:
    """Load HA's own registries and config entries from real, unmocked storage."""
    # Deliberately avoid the hass/hass_storage fixtures and the test helper's
    # default in-memory registries: the second instance must read actual files.
    async with async_test_home_assistant(
        config_dir=str(config_dir), load_registries=False
    ) as hass:
        hass.data.pop(loader.DATA_CUSTOM_COMPONENTS)
        try:
            assert await bootstrap.async_load_base_functionality(hass)
            yield hass
        finally:
            # HA's final-write event also flushes delayed registry/entry saves.
            await hass.async_stop()


def _sources(hass: HomeAssistant) -> dict[str, er.RegistryEntry]:
    """Create only five synthetic sources with stable provider identities."""
    registry = er.async_get(hass)
    return {
        role: registry.async_get_or_create(
            "sensor", "release_smoke", role, suggested_object_id=f"smoke_{role}"
        )
        for role in ("pv_generation", "grid_import", "grid_export", "load", "grid_co2")
    }


def _publish(
    hass: HomeAssistant,
    sources: dict[str, er.RegistryEntry],
    period: datetime,
    *,
    cycles: int,
) -> None:
    """Publish 4 PV / 1 import / 2 export / 3 load kWh per physical interval."""
    for role, increment in (
        ("pv_generation", 4),
        ("grid_import", 1),
        ("grid_export", 2),
        ("load", 3),
    ):
        _energy(hass, sources[role], str(100 + cycles * increment), period)
    _grid(hass, sources, period)


async def _configure(
    hass: HomeAssistant, sources: dict[str, er.RegistryEntry]
) -> ConfigEntry:
    """Submit every visible form without replacing integration setup or unload."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    forms: tuple[tuple[str, dict[str, Any]], ...] = (
        ("user", {"topology": "inverter"}),
        (
            "sources",
            {
                **{
                    role: sources[role].entity_id
                    for role in ("pv_generation", "grid_import", "grid_export")
                },
                "synchronous_sources_confirmed": True,
            },
        ),
        ("storage", {"battery_present": "without_battery"}),
        ("consumers", {"mode": "aggregate_shares"}),
        (
            "aggregate_load",
            {
                "household_source": sources["load"].entity_id,
                "load_measurement_confirmed": True,
            },
        ),
        ("consumer_menu", {"action": "finish"}),
        (
            "factors",
            {
                "grid_intensity_source": sources["grid_co2"].entity_id,
                "grid_max_age_minutes": 60,
                "pv_factor": "40",
            },
        ),
    )
    for step_id, data in forms:
        assert result["type"] is FlowResultType.FORM
        assert result["step_id"] == step_id
        assert not result["errors"]
        result = await hass.config_entries.flow.async_configure(result["flow_id"], data)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    entry = result["result"]
    assert entry.state is ConfigEntryState.LOADED
    return entry


def _sensor(hass: HomeAssistant, entry_id: str, metric: str) -> State:
    """Resolve a published result through its stable registry identity."""
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry_id}:{metric}"
    )
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    return state


async def test_fresh_install_flow_measurement_disk_restart_and_unload(  # noqa: PLR0915
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, timers: list[_Timer]
) -> None:
    """Create, measure, persist, restore and unload in one public HA lifecycle."""
    monkeypatch.setattr(dt_util, "utcnow", lambda: _START)
    async with _installation(tmp_path) as hass:
        assert not hass.config_entries.async_entries(DOMAIN)
        sources = _sources(hass)
        _publish(hass, sources, _START, cycles=0)
        entry = await _configure(hass, sources)
        entry_id = entry.entry_id
        entry_data = dict(entry.data)
        household_id = entry.data["consumption"]["household_id"]
        assert len(timers) == 1
        assert _sensor(hass, entry_id, "net_savings").state == STATE_UNAVAILABLE
        manifest = await manifest_store(hass, entry.data["storage_id"]).async_load()
        assert manifest is not None
        assert manifest.initialized
        assert manifest.owner_entry_id == entry_id

        _publish(hass, sources, _BASELINE, cycles=0)
        await _tick(hass, timers, _BASELINE)
        assert entry.runtime_data.state.measurement.phase is MeasurementPhase.ACTIVE
        assert entry.runtime_data.state.totals.direct_pv_kwh == 0
        _publish(hass, sources, _INTERVAL, cycles=1)
        await _tick(hass, timers, _INTERVAL)
        saved = entry.runtime_data.state
        assert saved.totals.direct_pv_kwh == 2
        assert saved.totals.direct_gross_g == 800
        assert saved.totals.direct_pv_burden_g == 80
        assert saved.totals.direct_net_g == 720
        assert dict(saved.consumer_totals)[household_id] == saved.totals
        assert await entry.runtime_data.store.async_load() == saved
        assert Fraction(_sensor(hass, entry_id, "net_savings").state) == Fraction(
            18, 25
        )
        assert (
            _sensor(hass, entry_id, "net_savings").attributes["unit_of_measurement"]
            == "kgCO₂e"
        )
        assert Fraction(_sensor(hass, entry_id, "direct_pv_energy").state) == 2
        assert (
            _sensor(hass, entry_id, "direct_pv_energy").attributes[
                "unit_of_measurement"
            ]
            == "kWh"
        )

    assert hass.state is CoreState.stopped
    async with _installation(tmp_path) as restarted:
        restored = restarted.config_entries.async_get_entry(entry_id)
        assert restored is not None
        assert restored is not entry
        assert dict(restored.data) == entry_data
        registry = er.async_get(restarted)
        assert all(
            registry.async_get(source.id) == source for source in sources.values()
        )
        assert await restarted.config_entries.async_setup(entry_id)
        await restarted.async_block_till_done()
        assert restored.state is ConfigEntryState.LOADED
        assert restored.runtime_data.state == saved
        assert len(timers) == 2
        assert _sensor(restarted, entry_id, "net_savings").state == STATE_UNAVAILABLE

        # Replaying the last vector makes the restored result available without
        # creating any additional interval or cumulative credit.
        _publish(restarted, sources, _INTERVAL, cycles=1)
        await _tick(restarted, timers, _INTERVAL + timedelta(minutes=1))
        assert restored.runtime_data.state == saved
        assert Fraction(_sensor(restarted, entry_id, "net_savings").state) == Fraction(
            18, 25
        )
        next_period = _INTERVAL + timedelta(minutes=2)
        _publish(restarted, sources, next_period, cycles=2)
        await _tick(restarted, timers, next_period)
        assert restored.runtime_data.state.totals.direct_pv_kwh == 4
        assert Fraction(_sensor(restarted, entry_id, "net_savings").state) == Fraction(
            36, 25
        )
        final_state = restored.runtime_data.state
        store = restored.runtime_data.store
        assert await restarted.config_entries.async_unload(entry_id)
        await restarted.async_block_till_done()
        assert restored.state is ConfigEntryState.NOT_LOADED
        assert timers[-1].cancelled.is_set()
        assert _sensor(restarted, entry_id, "net_savings").state == STATE_UNAVAILABLE
        _publish(restarted, sources, next_period + timedelta(minutes=1), cycles=3)
        await _tick(restarted, timers, next_period + timedelta(minutes=1))
        assert await store.async_load() == final_state
