# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Public no-battery runtime transactions, current CO₂ samples, and lifecycle."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant import loader
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_test_home_assistant,
    mock_registry,
)

from custom_components.co2saver.bootstrap import async_reserve_bootstrap, manifest_lock
from custom_components.co2saver.config_factors import HomeAssistantGridIntensityReader
from custom_components.co2saver.const import ATTR_CO2SAVER_PERIOD_END, DOMAIN
from custom_components.co2saver.measurement import ha as measurement_ha
from custom_components.co2saver.measurement.ha import HomeAssistantEnergyReader
from custom_components.co2saver.measurement.models import MeasurementPhase

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from homeassistant.core import HomeAssistant

    from custom_components.co2saver.config_factors import GridIntensitySample
    from custom_components.co2saver.measurement.models import EnergyObservation

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")

_START = datetime(2026, 9, 5, 12, 0, 20, tzinfo=UTC)
_BASELINE = _START.replace(second=0) + timedelta(minutes=1)
_INTERVAL = _BASELINE + timedelta(minutes=1)
_HOUSE = "a" * 32
_WALLBOX = "b" * 32


@dataclass
class _Timer:
    """One public UTC timer registration with observable cancellation."""

    action: Callable[[datetime], Awaitable[None]]
    cancelled: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class _Reads:
    """Count real immutable source copies, including invalid observations."""

    energy: int = 0
    grid: int = 0


@pytest.fixture(autouse=True)
def runtime_environment(
    hass: HomeAssistant, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep segment installation time and physical storage directory deterministic."""
    hass.config.config_dir = str(tmp_path)
    monkeypatch.setattr(dt_util, "utcnow", lambda: _START)


@pytest.fixture
def timers(monkeypatch: pytest.MonkeyPatch) -> list[_Timer]:
    """Drive actual runner callbacks through their public timer registration."""
    registered: list[_Timer] = []

    def track(
        _hass: HomeAssistant,
        action: Callable[[datetime], Awaitable[None]],
        *,
        second: int,
    ) -> Callable[[], None]:
        assert second == 0
        timer = _Timer(action)
        registered.append(timer)
        return timer.cancelled.set

    monkeypatch.setattr(measurement_ha, "async_track_utc_time_change", track)
    return registered


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> _Reads:
    """Observe the actual adapters without changing their returned samples."""
    counts = _Reads()
    read_energy = HomeAssistantEnergyReader.read
    read_grid = HomeAssistantGridIntensityReader.read

    def energy(reader: HomeAssistantEnergyReader) -> tuple[EnergyObservation, ...]:
        counts.energy += 1
        return read_energy(reader)

    def grid(
        reader: HomeAssistantGridIntensityReader,
    ) -> tuple[GridIntensitySample | None, str | None]:
        counts.grid += 1
        return read_grid(reader)

    monkeypatch.setattr(HomeAssistantEnergyReader, "read", energy)
    monkeypatch.setattr(HomeAssistantGridIntensityReader, "read", grid)
    return counts


def _plan(
    hass: HomeAssistant,
    *,
    topology: str = "inverter",
    mode: str = "aggregate_shares",
    battery: bool = False,
) -> tuple[dict[str, Any], dict[str, er.RegistryEntry]]:
    """Register a complete site with explicit aggregate or separate load semantics."""
    registry = er.async_get(hass)
    sources = {
        role: registry.async_get_or_create(
            "sensor", "runtime_test", role, suggested_object_id=f"runtime_{role}"
        )
        for role in (
            "pv_generation",
            "grid_import",
            "grid_export",
            "load",
            "wallbox",
            "grid_co2",
            "charge",
            "discharge",
        )
    }
    source_roles = ["grid_import", "grid_export"]
    if topology == "inverter":
        source_roles.append("pv_generation")
    pair = sorted((sources["grid_import"].id, sources["grid_export"].id))
    consumer = {"consumer_id": _WALLBOX, "name": "Wallbox"}
    consumer.update(
        {"share": "0.25"}
        if mode == "aggregate_shares"
        else {"source": sources["wallbox"].id}
    )
    factors: dict[str, Any] = {
        "grid_intensity_source": sources["grid_co2"].id,
        "grid_max_age_minutes": 60,
        "pv_factor": "40",
    }
    if battery:
        factors["battery_factor"] = "20"
    plan = {
        "topology": topology,
        "sources": {role: sources[role].id for role in source_roles},
        "plant_key": f"grid:{pair[0]}:{pair[1]}",
        "synchronous_sources_confirmed": True,
        "battery": {
            "battery_id": "c" * 32,
            "charge_source": sources["charge"].id,
            "discharge_source": sources["discharge"].id,
            "usable_capacity_kwh": "10",
            "round_trip_efficiency": "0.9",
        }
        if battery
        else None,
        "consumption": {
            "mode": mode,
            "household_id": _HOUSE,
            "household_source": sources["load"].id,
            "consumers": [consumer],
        },
        "factors": factors,
    }
    return plan, sources


async def _setup(hass: HomeAssistant, plan: dict[str, Any]) -> MockConfigEntry:
    """Complete bootstrap and public Home Assistant setup without flow internals."""
    async with manifest_lock(hass):
        storage_id = await async_reserve_bootstrap(hass)
    entry = MockConfigEntry(
        domain=DOMAIN, title="CO2 Saver", data={**plan, "storage_id": storage_id}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.LOADED
    return entry


def _energy(
    hass: HomeAssistant,
    source: er.RegistryEntry,
    value: str,
    period: datetime,
    *,
    reported_at: datetime | None = None,
) -> None:
    """Publish one physical cumulative kWh sample with independent publication time."""
    hass.states.async_set(
        source.entity_id,
        value,
        {
            "device_class": "energy",
            "state_class": "total_increasing",
            "unit_of_measurement": "kWh",
            ATTR_CO2SAVER_PERIOD_END: period.isoformat(),
        },
        timestamp=(reported_at or period).timestamp(),
    )


def _vector(  # noqa: PLR0913
    hass: HomeAssistant,
    sources: dict[str, er.RegistryEntry],
    period: datetime,
    *,
    cycles: int = 0,
    mode: str = "aggregate_shares",
    missing: str | None = None,
) -> None:
    """Publish a 4 PV / 1 import / 2 export / 3 local kWh reference interval."""
    increments = {
        "pv_generation": 4,
        "grid_import": 1,
        "grid_export": 2,
        "load": 3 if mode == "aggregate_shares" else 2,
        "wallbox": 1,
        "charge": 0,
        "discharge": 0,
    }
    for role, increment in increments.items():
        if role != missing:
            _energy(hass, sources[role], str(100 + cycles * increment), period)


def _grid(
    hass: HomeAssistant,
    sources: dict[str, er.RegistryEntry],
    reported_at: datetime,
    value: str = "400",
    unit: str = "gCO2e/kWh",
) -> None:
    """Publish the one grid-intensity state available at a poll."""
    hass.states.async_set(
        sources["grid_co2"].entity_id,
        value,
        {"unit_of_measurement": unit},
        timestamp=reported_at.timestamp(),
    )


async def _tick(hass: HomeAssistant, timers: list[_Timer], when: datetime) -> None:
    """Run exactly one registered UTC callback and drain resulting HA tasks."""
    await timers[-1].action(when)
    await hass.async_block_till_done()


async def _baseline(
    hass: HomeAssistant,
    sources: dict[str, er.RegistryEntry],
    timers: list[_Timer],
    *,
    mode: str = "aggregate_shares",
) -> None:
    """Observe the first physical post-installation vector without credit."""
    _vector(hass, sources, _BASELINE, mode=mode)
    _grid(hass, sources, _BASELINE)
    await _tick(hass, timers, _BASELINE)


@pytest.mark.parametrize("topology", ["inverter", "smart_meter"])
@pytest.mark.parametrize("mode", ["aggregate_shares", "separate_meters"])
async def test_full_runtime_matrix_books_exact_system_consumers_and_remainder(
    hass: HomeAssistant, timers: list[_Timer], reads: _Reads, topology: str, mode: str
) -> None:
    """Every accepted topology and allocation mode uses the existing domain model."""
    plan, sources = _plan(hass, topology=topology, mode=mode)
    entry = await _setup(hass, plan)
    assert len(timers) == 1
    assert reads.energy == reads.grid == 0
    assert entry.runtime_data.runner is not None
    assert not entry.runtime_data.available
    await _baseline(hass, sources, timers, mode=mode)
    assert entry.runtime_data.state.totals.direct_pv_kwh == 0
    _vector(hass, sources, _INTERVAL, cycles=1, mode=mode)
    _grid(hass, sources, _INTERVAL)
    await _tick(hass, timers, _INTERVAL)
    state = entry.runtime_data.state
    assert state.totals.direct_pv_kwh == 2
    assert state.totals.direct_gross_g == 800
    assert state.totals.direct_pv_burden_g == 80
    assert state.totals.direct_net_g == 720
    assert state.totals.storage_pv_kwh == 0
    assert entry.runtime_data.available
    household = Fraction(5, 4) if mode == "aggregate_shares" else Fraction(1)
    assert dict(state.consumer_totals)[_HOUSE].direct_pv_kwh == household
    assert dict(state.consumer_totals)[_WALLBOX].direct_pv_kwh == 0
    assert state.unassigned_direct_kwh == 2 - household
    assert (
        sum(
            (total.direct_pv_kwh for _, total in state.consumer_totals),
            state.unassigned_direct_kwh,
        )
        == state.totals.direct_pv_kwh
    )
    assert state == await entry.runtime_data.store.async_load()
    assert reads.energy == reads.grid == 2
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_setup_and_state_events_never_trigger_source_reads(
    hass: HomeAssistant, timers: list[_Timer], reads: _Reads
) -> None:
    """State changes, reports and unobserved transient faults never form intervals."""
    plan, sources = _plan(hass)
    entry = await _setup(hass, plan)
    _vector(hass, sources, _BASELINE)
    _grid(hass, sources, _BASELINE)
    _energy(hass, sources["grid_import"], "unavailable", _BASELINE)
    _energy(hass, sources["grid_import"], "100", _BASELINE)
    await hass.async_block_till_done()
    assert reads.energy == reads.grid == 0
    await _tick(hass, timers, _BASELINE)
    assert reads.energy == reads.grid == 1
    assert entry.runtime_data.state.measurement.phase is MeasurementPhase.ACTIVE
    assert dict(entry.runtime_data.state.diagnostics)["discarded_intervals"] == 0


async def test_duplicate_poll_does_not_write_or_credit_again(
    hass: HomeAssistant, timers: list[_Timer], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unchanged vector is idempotent even after successful publication."""
    plan, sources = _plan(hass)
    entry = await _setup(hass, plan)
    await _baseline(hass, sources, timers)
    _vector(hass, sources, _INTERVAL, cycles=1)
    _grid(hass, sources, _INTERVAL)
    await _tick(hass, timers, _INTERVAL)
    saved = entry.runtime_data.state
    original_save = Store.async_save
    writes = 0

    async def count_save(
        store: Store[dict[str, object]], data: dict[str, object]
    ) -> None:
        nonlocal writes
        if store.key == entry.runtime_data.store.store_key:
            writes += 1
        await original_save(store, data)

    monkeypatch.setattr(Store, "async_save", count_save)
    await _tick(hass, timers, _INTERVAL + timedelta(minutes=1))
    assert entry.runtime_data.state == saved
    assert writes == 0


@pytest.mark.parametrize(
    "fault", ["missing", "unavailable", "future", "stale", "bad_unit"]
)
async def test_invalid_current_grid_preserves_energy_without_revaluation(
    hass: HomeAssistant, timers: list[_Timer], fault: str
) -> None:
    """Physical energy progresses, while unvalued intervals stay unvalued forever."""
    plan, sources = _plan(hass)
    entry = await _setup(hass, plan)
    await _baseline(hass, sources, timers)
    _vector(hass, sources, _INTERVAL, cycles=1)
    if fault == "missing":
        hass.states.async_remove(sources["grid_co2"].entity_id)
    elif fault == "future":
        _grid(hass, sources, _INTERVAL + timedelta(seconds=1))
    elif fault == "stale":
        _grid(hass, sources, _INTERVAL - timedelta(minutes=60, microseconds=1))
    elif fault == "bad_unit":
        _grid(hass, sources, _INTERVAL, unit="ppm")
    else:
        _grid(hass, sources, _INTERVAL, value="unavailable")
    await _tick(hass, timers, _INTERVAL)
    unvalued = entry.runtime_data.state
    assert unvalued.measurement.phase is MeasurementPhase.ACTIVE
    assert unvalued.totals.direct_pv_kwh == 2
    assert unvalued.totals.unvalued_direct_kwh == 2
    assert unvalued.totals.direct_gross_g == unvalued.totals.direct_pv_burden_g == 0
    assert not entry.runtime_data.available
    later = _INTERVAL + timedelta(minutes=1)
    _vector(hass, sources, later, cycles=2)
    _grid(hass, sources, later)
    await _tick(hass, timers, later)
    recovered = entry.runtime_data.state
    assert recovered.totals.direct_pv_kwh == 4
    assert recovered.totals.unvalued_direct_kwh == 2
    assert recovered.totals.direct_gross_g == 800
    assert recovered.totals.direct_pv_burden_g == 80
    assert dict(recovered.diagnostics)["missing_grid_intensity"] == 1
    assert entry.runtime_data.available


async def test_completed_candidate_uses_current_sample_without_older_fallback(
    hass: HomeAssistant, timers: list[_Timer]
) -> None:
    """A previously observed eligible CO₂ value cannot rescue a newer future sample."""
    plan, sources = _plan(hass)
    entry = await _setup(hass, plan)
    await _baseline(hass, sources, timers)
    _vector(hass, sources, _INTERVAL, cycles=1, missing="grid_export")
    _grid(hass, sources, _INTERVAL)
    await _tick(hass, timers, _INTERVAL)
    assert entry.runtime_data.state.measurement.candidate is not None
    assert entry.runtime_data.state.totals.direct_pv_kwh == 0
    later = _INTERVAL + timedelta(minutes=1)
    _energy(hass, sources["grid_export"], "102", _INTERVAL, reported_at=later)
    _grid(hass, sources, _INTERVAL + timedelta(seconds=30), value="900")
    await _tick(hass, timers, later)
    state = entry.runtime_data.state
    assert state.measurement.candidate is None
    assert state.totals.direct_pv_kwh == state.totals.unvalued_direct_kwh == 2
    assert state.totals.direct_gross_g == state.totals.direct_pv_burden_g == 0
    assert not entry.runtime_data.available


async def test_invalid_energy_enters_recovery_once_and_skips_crossing_interval(
    hass: HomeAssistant, timers: list[_Timer]
) -> None:
    """Repeated faults count once, then a strict recovery baseline books nothing."""
    plan, sources = _plan(hass)
    entry = await _setup(hass, plan)
    await _baseline(hass, sources, timers)
    _vector(hass, sources, _INTERVAL, cycles=1)
    _energy(hass, sources["grid_import"], "unavailable", _INTERVAL)
    _grid(hass, sources, _INTERVAL)
    await _tick(hass, timers, _INTERVAL)
    first_failure = entry.runtime_data.state
    assert first_failure.measurement.phase is MeasurementPhase.AWAITING_REBASELINE
    assert dict(first_failure.diagnostics)["discarded_intervals"] == 1
    await _tick(hass, timers, _INTERVAL + timedelta(minutes=1))
    assert entry.runtime_data.state == first_failure
    recovery_at = _INTERVAL + timedelta(minutes=2)
    _vector(hass, sources, recovery_at, cycles=2)
    _grid(hass, sources, recovery_at)
    await _tick(hass, timers, recovery_at)
    assert entry.runtime_data.state.measurement.phase is MeasurementPhase.ACTIVE
    assert entry.runtime_data.state.totals.direct_pv_kwh == 0
    following = recovery_at + timedelta(minutes=1)
    _vector(hass, sources, following, cycles=3)
    _grid(hass, sources, following)
    await _tick(hass, timers, following)
    assert entry.runtime_data.state.totals.direct_pv_kwh == 2


async def test_reload_restores_exact_state_and_keeps_utc_anchor(
    hass: HomeAssistant, timers: list[_Timer], reads: _Reads
) -> None:
    """Reload keeps a verified baseline and creates only the next UTC timer."""
    plan, sources = _plan(hass)
    entry = await _setup(hass, plan)
    await _baseline(hass, sources, timers)
    _vector(hass, sources, _INTERVAL, cycles=1)
    _grid(hass, sources, _INTERVAL)
    await _tick(hass, timers, _INTERVAL)
    saved = entry.runtime_data.state
    original = timers[-1]
    for _ in range(3):
        assert await hass.config_entries.async_reload(entry.entry_id)
        assert entry.runtime_data.state == saved
    assert len(timers) == 4
    assert all(timer.cancelled.is_set() for timer in timers[:-1])
    assert not timers[-1].cancelled.is_set()
    assert reads.energy == reads.grid == 2
    await original.action(_INTERVAL + timedelta(minutes=1))
    assert reads.energy == reads.grid == 2
    await _tick(hass, timers, _INTERVAL + timedelta(minutes=1))
    assert entry.runtime_data.state == saved
    following = _INTERVAL + timedelta(minutes=2)
    _vector(hass, sources, following, cycles=2)
    _grid(hass, sources, following)
    await _tick(hass, timers, following)
    assert entry.runtime_data.state.totals.direct_pv_kwh == 4


async def test_battery_entry_starts_one_runner_without_immediate_reads(
    hass: HomeAssistant, timers: list[_Timer], reads: _Reads
) -> None:
    """#10 activates battery polls after bootstrap without inventing PV provenance."""
    plan, sources = _plan(hass, battery=True)
    entry = await _setup(hass, plan)
    assert entry.runtime_data.runner is not None
    assert not entry.runtime_data.available
    assert len(timers) == 1
    _vector(hass, sources, _BASELINE)
    _grid(hass, sources, _BASELINE)
    await hass.async_block_till_done()
    assert reads.energy == reads.grid == 0
    assert entry.runtime_data.state.ledger is not None
    assert entry.runtime_data.state.ledger.pv_lower.kwh == 0
    await _tick(hass, timers, _BASELINE)
    assert reads.energy == reads.grid == 1
    assert entry.runtime_data.state.ledger.pv_lower.kwh == 0


@pytest.mark.parametrize("failure", ["raise", "swallow", "readback"])
async def test_store_failure_stops_reads_and_publishes_only_after_verified_reload(
    hass: HomeAssistant,
    timers: list[_Timer],
    reads: _Reads,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """Uncertain writes publish nothing; replay after reload credits exactly once."""
    plan, sources = _plan(hass)
    entry = await _setup(hass, plan)
    await _baseline(hass, sources, timers)
    previous = entry.runtime_data.state
    key = entry.runtime_data.store.store_key
    original_save = Store.async_save
    original_load = Store.async_load
    loads = 0

    async def save(store: Store[dict[str, object]], data: dict[str, object]) -> None:
        if store.key == key:
            if failure == "raise":
                message = "generation disk failure"
                raise OSError(message)
            if failure == "swallow":
                return
        await original_save(store, data)

    async def load(store: Store[dict[str, object]]) -> dict[str, object] | None:
        nonlocal loads
        if store.key == key:
            loads += 1
            if failure == "readback" and loads == 2:
                message = "generation read-back failure"
                raise OSError(message)
        return await original_load(store)

    _vector(hass, sources, _INTERVAL, cycles=1)
    _grid(hass, sources, _INTERVAL)
    failed_timer = timers[-1]
    with monkeypatch.context() as patch:
        patch.setattr(Store, "async_save", save)
        patch.setattr(Store, "async_load", load)
        await _tick(hass, timers, _INTERVAL)
        assert entry.runtime_data.state == previous
        assert not entry.runtime_data.available
        assert failed_timer.cancelled.is_set()
        assert reads.energy == reads.grid == 2
        await failed_timer.action(_INTERVAL + timedelta(minutes=1))
        assert reads.energy == reads.grid == 2
    assert await hass.config_entries.async_reload(entry.entry_id)
    await _tick(hass, timers, _INTERVAL + timedelta(minutes=1))
    assert entry.runtime_data.state.totals.direct_pv_kwh == 2
    assert entry.runtime_data.state.totals.direct_net_g == 720
    assert entry.runtime_data.state == await entry.runtime_data.store.async_load()


async def test_unload_cancels_timer_before_draining_inflight_generation_commit(
    hass: HomeAssistant,
    timers: list[_Timer],
    reads: _Reads,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unload waits for the sole atomic write and suppresses queued late reads."""
    plan, sources = _plan(hass)
    entry = await _setup(hass, plan)
    await _baseline(hass, sources, timers)
    runtime = entry.runtime_data
    key = runtime.store.store_key
    original_save = Store.async_save
    started = asyncio.Event()
    allow = asyncio.Event()
    writes = 0

    async def blocked_save(
        store: Store[dict[str, object]], data: dict[str, object]
    ) -> None:
        nonlocal writes
        if store.key == key:
            writes += 1
            started.set()
            await allow.wait()
        await original_save(store, data)

    monkeypatch.setattr(Store, "async_save", blocked_save)
    _vector(hass, sources, _INTERVAL, cycles=1)
    _grid(hass, sources, _INTERVAL)
    timer = timers[-1]
    active = asyncio.create_task(timer.action(_INTERVAL))
    await started.wait()
    unload = asyncio.create_task(hass.config_entries.async_unload(entry.entry_id))
    await timer.cancelled.wait()
    assert not unload.done()
    await timer.action(_INTERVAL + timedelta(minutes=1))
    assert reads.energy == reads.grid == 2
    assert runtime.state.totals.direct_pv_kwh == 0
    allow.set()
    await active
    assert await unload
    assert runtime.state.totals.direct_pv_kwh == 2
    assert entry.state is ConfigEntryState.NOT_LOADED
    await timer.action(_INTERVAL + timedelta(minutes=2))
    assert reads.energy == reads.grid == 2
    assert writes == 1


async def test_energy_and_grid_are_captured_before_the_first_store_await(
    hass: HomeAssistant,
    timers: list[_Timer],
    reads: _Reads,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent state events cannot alter an already copied physical poll."""
    plan, sources = _plan(hass)
    entry = await _setup(hass, plan)
    await _baseline(hass, sources, timers)
    key = entry.runtime_data.store.store_key
    original_load = Store.async_load
    started = asyncio.Event()
    allow = asyncio.Event()

    async def blocked_load(store: Store[dict[str, object]]) -> dict[str, object] | None:
        if store.key == key:
            started.set()
            await allow.wait()
        return await original_load(store)

    monkeypatch.setattr(Store, "async_load", blocked_load)
    _vector(hass, sources, _INTERVAL, cycles=1)
    _grid(hass, sources, _INTERVAL)
    active = asyncio.create_task(timers[-1].action(_INTERVAL))
    await started.wait()
    assert reads.energy == reads.grid == 2
    _energy(hass, sources["grid_import"], "unavailable", _INTERVAL)
    _grid(hass, sources, _INTERVAL + timedelta(seconds=1), "2000")
    allow.set()
    await active
    assert reads.energy == reads.grid == 2
    assert entry.runtime_data.state.totals.direct_pv_kwh == 2
    assert entry.runtime_data.state.totals.direct_gross_g == 800
    assert entry.runtime_data.available


async def test_new_home_assistant_instance_restores_totals_and_replay_baseline(
    hass: HomeAssistant, timers: list[_Timer], reads: _Reads
) -> None:
    """A fresh HA instance resumes the same generation without in-memory caches."""
    plan, sources = _plan(hass)
    entry = await _setup(hass, plan)
    await _baseline(hass, sources, timers)
    _vector(hass, sources, _INTERVAL, cycles=1)
    _grid(hass, sources, _INTERVAL)
    await _tick(hass, timers, _INTERVAL)
    saved = entry.runtime_data.state
    assert await hass.config_entries.async_unload(entry.entry_id)
    async with async_test_home_assistant(
        config_dir=hass.config.config_dir
    ) as restarted:
        restarted.data.pop(loader.DATA_CUSTOM_COMPONENTS)
        # Re-stage the source registry as restored by HA itself; the integration's
        # actual manifest and generation continue using the shared Store fixture.
        mock_registry(
            restarted, {source.entity_id: source for source in sources.values()}
        )
        restored_entry = MockConfigEntry(
            domain=DOMAIN, entry_id=entry.entry_id, data=dict(entry.data)
        )
        restored_entry.add_to_hass(restarted)
        assert await restarted.config_entries.async_setup(restored_entry.entry_id)
        assert restored_entry.runtime_data.state == saved
        assert reads.energy == reads.grid == 2
        _vector(restarted, sources, _INTERVAL, cycles=1)
        _grid(restarted, sources, _INTERVAL)
        await _tick(restarted, timers, _INTERVAL + timedelta(minutes=1))
        assert restored_entry.runtime_data.state == saved
        next_period = _INTERVAL + timedelta(minutes=2)
        _vector(restarted, sources, next_period, cycles=2)
        _grid(restarted, sources, next_period)
        await _tick(restarted, timers, next_period)
        assert restored_entry.runtime_data.state.totals.direct_pv_kwh == 4
        assert restored_entry.runtime_data.state.totals.direct_net_g == 1440
        assert await restarted.config_entries.async_unload(restored_entry.entry_id)
        await restarted.async_stop()
