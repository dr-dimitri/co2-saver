# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Public sensor platform states, identities, verified publication, and lifecycle."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import timedelta
from fractions import Fraction
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant import loader
from homeassistant.components.sensor import SensorEntity
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_test_home_assistant,
    mock_registry,
)

from custom_components.co2saver.const import DOMAIN

from .test_runtime import (
    _HOUSE,
    _INTERVAL,
    _WALLBOX,
    _baseline,
    _energy,
    _grid,
    _plan,
    _setup,
    _tick,
    _vector,
    runtime_environment,
    timers,
)
from .test_storage_runtime import _pv_charge, _site

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant, State

    from .test_runtime import _Timer

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")
__all__ = ("runtime_environment", "timers")

_NET_KEYS = ("net_savings", "direct_net_savings", "storage_net_savings")
_BURDEN_KEYS = ("gross_avoided", "pv_lifecycle", "battery_lifecycle")
_ENERGY_KEYS = (
    "direct_pv_energy",
    "storage_pv_energy",
    "unassigned_direct_energy",
    "unassigned_storage_energy",
    "unvalued_direct_energy",
    "unvalued_storage_energy",
)
_SYSTEM_KEYS = (*_NET_KEYS, *_BURDEN_KEYS, *_ENERGY_KEYS)
_CONSUMER_KEYS = ("net_savings", "direct_pv_energy", "storage_pv_energy")


def _entity_id(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    metric: str,
    consumer_id: str | None = None,
) -> str:
    """Resolve stable result identity instead of depending on translated entity IDs."""
    suffix = metric if consumer_id is None else f"consumer:{consumer_id}:{metric}"
    result = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}:{suffix}"
    )
    assert result is not None
    return result


def _state(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    metric: str,
    consumer_id: str | None = None,
) -> State:
    """Read an actually published sensor state using its registry-backed identity."""
    state = hass.states.get(_entity_id(hass, entry, metric, consumer_id))
    assert state is not None
    return state


def _value(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    metric: str,
    consumer_id: str | None = None,
) -> float:
    """Require a numeric published sensor value at the presentation boundary."""
    state = _state(hass, entry, metric, consumer_id)
    assert state.attributes["accounting_status"] == entry.runtime_data.status
    return float(state.state)


def _owned_entries(
    hass: HomeAssistant, entry: MockConfigEntry
) -> list[er.RegistryEntry]:
    """Return only this integration's result entities, never its source entities."""
    return er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)


def _assert_all_unavailable(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Unavailable current measurements must never masquerade as valid zero totals."""
    for entity in _owned_entries(hass, entry):
        state = hass.states.get(entity.entity_id)
        assert state is not None
        assert state.state == STATE_UNAVAILABLE


async def _direct_interval(
    hass: HomeAssistant,
    timers: list[_Timer],
    sources: dict[str, er.RegistryEntry],
    *,
    mode: str = "aggregate_shares",
) -> None:
    """Evaluate the existing direct reference through the real platform dispatcher."""
    await _baseline(hass, sources, timers, mode=mode)
    _vector(hass, sources, _INTERVAL, cycles=1, mode=mode)
    _grid(hass, sources, _INTERVAL)
    await _tick(hass, timers, _INTERVAL)


async def test_initial_sensor_contract_has_stable_ids_device_and_statistics_classes(
    hass: HomeAssistant, timers: list[_Timer]
) -> None:
    """Setup creates twelve system and three per-consumer sensors, all unavailable."""
    plan, _sources = _plan(hass)
    entry = await _setup(hass, plan)
    assert len(timers) == 1
    entities = _owned_entries(hass, entry)
    assert len(entities) == 18
    _assert_all_unavailable(hass, entry)
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert len(devices) == 1
    assert devices[0].identifiers == {(DOMAIN, entry.entry_id)}
    assert devices[0].name == entry.title
    assert devices[0].entry_type is dr.DeviceEntryType.SERVICE
    assert all(entity.device_id == devices[0].id for entity in entities)
    assert all(entity.has_entity_name for entity in entities)
    for key in _SYSTEM_KEYS:
        state = _state(hass, entry, key)
        emission = key in (*_NET_KEYS, *_BURDEN_KEYS)
        assert state.attributes["unit_of_measurement"] == (
            "kgCO₂e" if emission else "kWh"
        )
        assert state.attributes["state_class"] == (
            "total" if key in _NET_KEYS else "total_increasing"
        )
        if emission:
            assert "device_class" not in state.attributes
        else:
            assert state.attributes["device_class"] == "energy"
        assert state.attributes["friendly_name"]
        assert not any(isinstance(value, list) for value in state.attributes.values())
    for consumer in (_HOUSE, _WALLBOX):
        for key in _CONSUMER_KEYS:
            state = _state(hass, entry, key, consumer)
            assert state.attributes["unit_of_measurement"] == (
                "kgCO₂e" if key == "net_savings" else "kWh"
            )
            assert state.attributes["state_class"] == (
                "total" if key == "net_savings" else "total_increasing"
            )


@pytest.mark.parametrize("topology", ["inverter", "smart_meter"])
@pytest.mark.parametrize("mode", ["aggregate_shares", "separate_meters"])
async def test_direct_sensor_values_follow_verified_system_and_consumer_results(
    hass: HomeAssistant, timers: list[_Timer], topology: str, mode: str
) -> None:
    """All topologies publish kg CO₂e and kWh without proportional attribution."""
    plan, sources = _plan(hass, topology=topology, mode=mode)
    entry = await _setup(hass, plan)
    await _direct_interval(hass, timers, sources, mode=mode)
    expected = {
        "net_savings": 0.72,
        "direct_net_savings": 0.72,
        "storage_net_savings": 0,
        "gross_avoided": 0.8,
        "pv_lifecycle": 0.08,
        "battery_lifecycle": 0,
        "direct_pv_energy": 2,
        "storage_pv_energy": 0,
        "unassigned_direct_energy": 0.75 if mode == "aggregate_shares" else 1,
        "unassigned_storage_energy": 0,
        "unvalued_direct_energy": 0,
        "unvalued_storage_energy": 0,
    }
    for metric, value in expected.items():
        assert _value(hass, entry, metric) == pytest.approx(value)
    household = 1.25 if mode == "aggregate_shares" else 1
    assert _value(hass, entry, "direct_pv_energy", _HOUSE) == household
    assert _value(hass, entry, "net_savings", _HOUSE) == pytest.approx(household * 0.36)
    assert _value(hass, entry, "direct_pv_energy", _WALLBOX) == 0
    assert _value(hass, entry, "net_savings") == pytest.approx(
        _value(hass, entry, "direct_net_savings")
        + _value(hass, entry, "storage_net_savings")
    )


async def test_negative_net_values_remain_signed_total_sensors(
    hass: HomeAssistant, timers: list[_Timer]
) -> None:
    """A manufacturing burden larger than avoidance visibly lowers net totals."""
    plan, sources = _plan(hass)
    plan["factors"]["pv_factor"] = "500"
    entry = await _setup(hass, plan)
    await _direct_interval(hass, timers, sources)
    assert _value(hass, entry, "net_savings") == pytest.approx(-0.2)
    assert _value(hass, entry, "direct_net_savings") == pytest.approx(-0.2)
    assert _value(hass, entry, "net_savings", _HOUSE) == pytest.approx(-0.125)
    assert _state(hass, entry, "net_savings").attributes["state_class"] == "total"
    assert _value(hass, entry, "gross_avoided") == pytest.approx(0.8)
    assert _value(hass, entry, "pv_lifecycle") == 1


async def test_storage_sensors_publish_delayed_burdens_and_combined_net(
    hass: HomeAssistant, timers: list[_Timer]
) -> None:
    """Ledger-backed storage sensors stay zero on charge and publish on discharge."""
    site = await _site(hass, timers)
    await _pv_charge(site)
    entry = site.entry
    assert _value(hass, entry, "storage_net_savings") == 0
    assert _value(hass, entry, "storage_pv_energy") == 0
    assert _value(hass, entry, "net_savings") == pytest.approx(0.72)
    await site.step({"discharge": 2, "load": 2}, grid="500")
    storage_net = float(Fraction(7840, 9000))
    assert _value(hass, entry, "storage_pv_energy") == 2
    assert _value(hass, entry, "storage_net_savings") == pytest.approx(storage_net)
    assert _value(hass, entry, "net_savings") == pytest.approx(0.72 + storage_net)
    assert _value(hass, entry, "gross_avoided") == pytest.approx(1.8)
    assert _value(hass, entry, "pv_lifecycle") == pytest.approx(
        0.08 + float(Fraction(800, 9000))
    )
    assert _value(hass, entry, "battery_lifecycle") == pytest.approx(0.04)
    assert _value(hass, entry, "storage_pv_energy", _HOUSE) == 1.5
    assert _value(hass, entry, "storage_pv_energy", _WALLBOX) == 0.5


@pytest.mark.parametrize("fault", ["grid", "energy"])
async def test_sensor_availability_tracks_invalid_current_inputs_and_recovery(
    hass: HomeAssistant, timers: list[_Timer], fault: str
) -> None:
    """Invalid data hides live values, then recovery publishes only verified history."""
    plan, sources = _plan(hass)
    entry = await _setup(hass, plan)
    await _direct_interval(hass, timers, sources)
    period = _INTERVAL + timedelta(minutes=1)
    _vector(hass, sources, period, cycles=2)
    _grid(hass, sources, period, value="unavailable" if fault == "grid" else "400")
    if fault == "energy":
        _energy(hass, sources["grid_import"], "unavailable", period)
    await _tick(hass, timers, period)
    _assert_all_unavailable(hass, entry)
    recovery = period + timedelta(minutes=1)
    _vector(hass, sources, recovery, cycles=3)
    _grid(hass, sources, recovery)
    await _tick(hass, timers, recovery)
    assert _value(hass, entry, "direct_pv_energy") == (6 if fault == "grid" else 2)
    assert _value(hass, entry, "net_savings") == pytest.approx(
        1.44 if fault == "grid" else 0.72
    )
    assert _value(hass, entry, "unvalued_direct_energy") == (
        2 if fault == "grid" else 0
    )


async def test_store_verification_failure_publishes_no_partial_sensor_value(
    hass: HomeAssistant, timers: list[_Timer], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A physically written but unverified generation cannot reach entity states."""
    plan, sources = _plan(hass)
    entry = await _setup(hass, plan)
    await _baseline(hass, sources, timers)
    before = entry.runtime_data.state
    key = entry.runtime_data.store.store_key
    original_load = Store.async_load
    started = asyncio.Event()
    allow = asyncio.Event()
    loads = 0

    async def fail_verification(
        store: Store[dict[str, object]],
    ) -> dict[str, object] | None:
        nonlocal loads
        if store.key == key:
            loads += 1
            if loads == 2:
                started.set()
                await allow.wait()
                message = "read-back verification unavailable"
                raise OSError(message)
        return await original_load(store)

    monkeypatch.setattr(Store, "async_load", fail_verification)
    _vector(hass, sources, _INTERVAL, cycles=1)
    _grid(hass, sources, _INTERVAL)
    task = asyncio.create_task(timers[-1].action(_INTERVAL))
    await started.wait()
    assert _value(hass, entry, "net_savings") == 0
    assert _value(hass, entry, "gross_avoided") == 0
    assert _value(hass, entry, "direct_pv_energy") == 0
    assert entry.runtime_data.state == before
    allow.set()
    await task
    await hass.async_block_till_done()
    _assert_all_unavailable(hass, entry)
    assert entry.runtime_data.state == before


async def test_entity_id_rename_keeps_identity_and_survives_reload(
    hass: HomeAssistant, timers: list[_Timer]
) -> None:
    """User-selected entity IDs never change the authoritative unique identity."""
    plan, sources = _plan(hass)
    entry = await _setup(hass, plan)
    await _direct_interval(hass, timers, sources)
    registry = er.async_get(hass)
    original = _entity_id(hass, entry, "net_savings")
    renamed = registry.async_update_entity(
        original, new_entity_id="sensor.my_pv_savings"
    )
    await hass.async_block_till_done()
    assert renamed.unique_id == f"{entry.entry_id}:net_savings"
    assert _entity_id(hass, entry, "net_savings") == "sensor.my_pv_savings"
    assert _value(hass, entry, "net_savings") == pytest.approx(0.72)
    assert await hass.config_entries.async_reload(entry.entry_id)
    assert _entity_id(hass, entry, "net_savings") == "sensor.my_pv_savings"
    _assert_all_unavailable(hass, entry)
    await _tick(hass, timers, _INTERVAL + timedelta(minutes=1))
    assert _value(hass, entry, "net_savings") == pytest.approx(0.72)
    assert len(_owned_entries(hass, entry)) == 18


async def test_reload_and_unload_remove_old_sensor_dispatcher_callbacks(
    hass: HomeAssistant, timers: list[_Timer], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each active entity receives one update; unloaded entities receive none."""
    plan, sources = _plan(hass)
    entry = await _setup(hass, plan)
    await _direct_interval(hass, timers, sources)
    for _ in range(3):
        assert await hass.config_entries.async_reload(entry.entry_id)
    owned = {entity.entity_id for entity in _owned_entries(hass, entry)}
    writes: dict[str, int] = {}
    original_write = SensorEntity.async_write_ha_state

    def count_write(entity: SensorEntity) -> None:
        if entity.entity_id in owned:
            writes[entity.entity_id] = writes.get(entity.entity_id, 0) + 1
        original_write(entity)

    monkeypatch.setattr(SensorEntity, "async_write_ha_state", count_write)
    signal = entry.runtime_data.update_signal
    async_dispatcher_send(hass, signal)
    await hass.async_block_till_done()
    assert writes == dict.fromkeys(owned, 1)
    assert await hass.config_entries.async_unload(entry.entry_id)
    writes.clear()
    async_dispatcher_send(hass, signal)
    await hass.async_block_till_done()
    assert writes == {}


async def test_consumer_rename_and_removal_preserve_history_without_active_orphans(
    hass: HomeAssistant, timers: list[_Timer]
) -> None:
    """Labels keep UUID histories while removed consumers stop producing states."""
    plan, sources = _plan(hass)
    entry = await _setup(hass, plan)
    await _baseline(hass, sources, timers)
    _vector(hass, sources, _INTERVAL)
    _energy(hass, sources["pv_generation"], "103", _INTERVAL)
    _energy(hass, sources["load"], "103", _INTERVAL)
    _grid(hass, sources, _INTERVAL)
    await _tick(hass, timers, _INTERVAL)
    old_state = entry.runtime_data.state
    assert dict(old_state.consumer_totals)[_WALLBOX].direct_pv_kwh == Fraction(3, 4)
    wallbox_id = _entity_id(hass, entry, "net_savings", _WALLBOX)
    before_name = _state(hass, entry, "net_savings", _WALLBOX).attributes[
        "friendly_name"
    ]
    changed: dict[str, Any] = deepcopy(dict(entry.data))
    changed["consumption"]["consumers"][0]["name"] = "Car Charger"
    hass.config_entries.async_update_entry(entry, data=changed)
    assert await hass.config_entries.async_reload(entry.entry_id)
    assert entry.runtime_data.state == old_state
    assert _entity_id(hass, entry, "net_savings", _WALLBOX) == wallbox_id
    assert (
        _state(hass, entry, "net_savings", _WALLBOX).attributes["friendly_name"]
        != before_name
    )
    assert (
        "Car Charger"
        in _state(hass, entry, "net_savings", _WALLBOX).attributes["friendly_name"]
    )
    changed = deepcopy(dict(entry.data))
    changed["consumption"]["consumers"] = [
        {"consumer_id": "c" * 32, "name": "Heat pump", "share": "0.1"}
    ]
    hass.config_entries.async_update_entry(entry, data=changed)
    assert await hass.config_entries.async_reload(entry.entry_id)
    assert (
        dict(entry.runtime_data.state.consumer_totals)[_WALLBOX]
        == dict(old_state.consumer_totals)[_WALLBOX]
    )
    assert _entity_id(hass, entry, "net_savings", "c" * 32) != wallbox_id
    old_sensor = hass.states.get(wallbox_id)
    assert old_sensor is None or old_sensor.state == STATE_UNAVAILABLE
    new_sensor = _state(hass, entry, "net_savings", "c" * 32)
    assert new_sensor.state == STATE_UNAVAILABLE
    period = _INTERVAL + timedelta(minutes=1)
    _vector(hass, sources, period, cycles=1)
    _grid(hass, sources, period)
    await _tick(hass, timers, period)
    assert _value(hass, entry, "net_savings", "c" * 32) == 0
    assert _value(hass, entry, "net_savings") == pytest.approx(1.08)


async def test_new_home_assistant_instance_restores_sensor_values_from_generation(
    hass: HomeAssistant, timers: list[_Timer]
) -> None:
    """Restored values come from the verified generation with stable registry IDs."""
    plan, sources = _plan(hass)
    entry = await _setup(hass, plan)
    await _direct_interval(hass, timers, sources)
    expected_id = _entity_id(hass, entry, "net_savings")
    all_registry = dict(er.async_get(hass).entities)
    saved = entry.runtime_data.state
    assert await hass.config_entries.async_unload(entry.entry_id)
    async with async_test_home_assistant(
        config_dir=hass.config.config_dir
    ) as restarted:
        restarted.data.pop(loader.DATA_CUSTOM_COMPONENTS)
        mock_registry(restarted, all_registry)
        restored = MockConfigEntry(
            domain=DOMAIN,
            entry_id=entry.entry_id,
            title=entry.title,
            data=dict(entry.data),
        )
        restored.add_to_hass(restarted)
        assert await restarted.config_entries.async_setup(restored.entry_id)
        assert restored.runtime_data.state == saved
        assert _entity_id(restarted, restored, "net_savings") == expected_id
        _assert_all_unavailable(restarted, restored)
        _vector(restarted, sources, _INTERVAL, cycles=1)
        _grid(restarted, sources, _INTERVAL)
        await _tick(restarted, timers, _INTERVAL + timedelta(minutes=1))
        assert _value(restarted, restored, "net_savings") == pytest.approx(0.72)
        assert _value(restarted, restored, "direct_pv_energy") == 2
        assert await restarted.config_entries.async_unload(restored.entry_id)
        await restarted.async_stop()
