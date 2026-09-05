# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""End-to-end storage provenance, delayed emissions, and atomic restart tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from fractions import Fraction
from typing import TYPE_CHECKING

import pytest
from homeassistant import loader
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_test_home_assistant,
    mock_registry,
)

from custom_components.co2saver.const import DOMAIN
from custom_components.co2saver.domain import Energy, StorageLedger
from custom_components.co2saver.measurement.models import MeasurementPhase

from .test_runtime import (
    _BASELINE,
    _HOUSE,
    _WALLBOX,
    _baseline,
    _energy,
    _grid,
    _plan,
    _setup,
    _tick,
    reads,
    runtime_environment,
    timers,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import entity_registry as er

    from custom_components.co2saver.persistence import GenerationState

    from .test_runtime import _Reads, _Timer

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")
__all__ = ("reads", "runtime_environment", "timers")


@dataclass
class _StorageSite:
    """Publish exact cumulative physical flows through real registered sources."""

    hass: HomeAssistant
    entry: MockConfigEntry
    sources: dict[str, er.RegistryEntry]
    timers: list[_Timer]
    mode: str
    counters: dict[str, Decimal]
    period: datetime = _BASELINE

    def publish(
        self, flows: Mapping[str, str | int], *, grid: str | None = "400"
    ) -> None:
        """Advance source meters one minute, including the wallbox within local load."""
        self.period += timedelta(minutes=1)
        for role in self.counters:
            increment = Decimal(flows.get(role, 0))
            if role == "load" and self.mode == "separate_meters":
                increment -= Decimal(flows.get("wallbox", 0))
            self.counters[role] += increment
            _energy(
                self.hass, self.sources[role], str(self.counters[role]), self.period
            )
        if grid is None:
            self.hass.states.async_remove(self.sources["grid_co2"].entity_id)
        else:
            _grid(self.hass, self.sources, self.period, value=grid)

    async def step(
        self, flows: Mapping[str, str | int], *, grid: str | None = "400"
    ) -> GenerationState:
        """Observe the newly published current vector and verify its complete state."""
        self.publish(flows, grid=grid)
        await _tick(self.hass, self.timers, self.period)
        state = self.entry.runtime_data.state
        assert state == await self.entry.runtime_data.store.async_load()
        _assert_conservation(state)
        return state


def _assert_conservation(state: GenerationState) -> None:
    """Assert stored provenance bounds and exact system/consumer energy closure."""
    ledger = state.ledger
    assert ledger is not None
    assert (
        0
        <= ledger.pv_lower.kwh
        <= ledger.stored_lower.kwh
        <= ledger.stored_upper.kwh
        <= ledger.capacity.kwh
    )
    assert ledger.non_pv_upper.kwh == ledger.stored_upper.kwh - ledger.pv_lower.kwh
    assert (
        0
        <= ledger.pv_burden.grams
        <= ledger.pv_density_upper.grams_per_kwh * ledger.pv_lower.kwh
    )
    for path in ("direct", "storage"):
        assigned = sum(
            (getattr(total, f"{path}_pv_kwh") for _, total in state.consumer_totals),
            Fraction(),
        )
        assert assigned + getattr(state, f"unassigned_{path}_kwh") == getattr(
            state.totals, f"{path}_pv_kwh"
        )


async def _site(
    hass: HomeAssistant,
    clocks: list[_Timer],
    *,
    topology: str = "inverter",
    mode: str = "aggregate_shares",
    observe_empty: bool = True,
) -> _StorageSite:
    """Create a quarantined battery, then prove emptiness by real measured discharge."""
    plan, sources = _plan(hass, topology=topology, mode=mode, battery=True)
    entry = await _setup(hass, plan)
    assert entry.runtime_data.runner is not None
    await _baseline(hass, sources, clocks, mode=mode)
    site = _StorageSite(
        hass,
        entry,
        sources,
        clocks,
        mode,
        {
            role: Decimal(100)
            for role in (
                "pv_generation",
                "grid_import",
                "grid_export",
                "load",
                "wallbox",
                "charge",
                "discharge",
            )
        },
    )
    assert site.entry.runtime_data.state.ledger == StorageLedger.quarantined(
        Energy(Fraction(10))
    )
    if observe_empty:
        state = await site.step({"discharge": 10, "load": 10})
        assert state.ledger is not None
        assert state.ledger.stored_lower.kwh == state.ledger.stored_upper.kwh == 0
        assert state.totals.storage_pv_kwh == state.totals.storage_net_g == 0
    return site


async def _pv_charge(site: _StorageSite) -> GenerationState:
    """Apply the charge side of the accepted ADR 9.3 reference cycle."""
    return await site.step(
        {"pv_generation": 6, "load": 2, "charge": 3, "grid_export": 1}
    )


@pytest.mark.parametrize("topology", ["inverter", "smart_meter"])
@pytest.mark.parametrize("mode", ["aggregate_shares", "separate_meters"])
async def test_reference_pv_cycle_books_only_delivered_energy_and_exact_burdens(
    hass: HomeAssistant, timers: list[_Timer], topology: str, mode: str
) -> None:
    """ADR 9.3 matches hand calculation through every supported meter topology."""
    site = await _site(hass, timers, topology=topology, mode=mode)
    charged = await _pv_charge(site)
    assert charged.ledger is not None
    assert (
        charged.ledger.stored_lower.kwh
        == charged.ledger.stored_upper.kwh
        == charged.ledger.pv_lower.kwh
        == Fraction(27, 10)
    )
    assert charged.ledger.non_pv_upper.kwh == 0
    assert charged.ledger.pv_burden.grams == 120
    assert charged.ledger.pv_density_upper.grams_per_kwh == Fraction(400, 9)
    assert charged.totals.direct_pv_kwh == 2
    assert charged.totals.storage_pv_kwh == charged.totals.storage_net_g == 0
    discharged = await site.step({"discharge": 2, "load": 2}, grid="500")
    assert discharged.totals.storage_pv_kwh == 2
    assert discharged.totals.storage_gross_g == 1000
    assert discharged.totals.storage_pv_burden_g == Fraction(800, 9)
    assert discharged.totals.storage_burden_g == 40
    assert discharged.totals.storage_net_g == Fraction(7840, 9)
    assert discharged.totals.direct_pv_kwh == 2
    house_energy = Fraction(3, 2) if mode == "aggregate_shares" else Fraction(2)
    assert dict(discharged.consumer_totals)[_HOUSE].storage_pv_kwh == house_energy
    assert dict(discharged.consumer_totals)[_WALLBOX].storage_pv_kwh == 2 - house_energy
    assert discharged.ledger is not None
    assert discharged.ledger.pv_lower.kwh == Fraction(7, 10)
    assert discharged.ledger.pv_burden.grams == Fraction(280, 9)
    rest = await site.step({"discharge": "0.7", "load": "0.7"}, grid="500")
    assert rest.totals.storage_pv_kwh == Fraction(27, 10)
    assert rest.totals.storage_pv_burden_g == 120
    assert rest.totals.storage_burden_g == 54
    assert rest.ledger is not None
    assert rest.ledger.pv_lower.kwh == rest.ledger.pv_burden.grams == 0
    # 0.3 kWh conversion loss never becomes PV delivery or savings.
    assert rest.totals.storage_pv_kwh < 3


@pytest.mark.parametrize("topology", ["inverter", "smart_meter"])
@pytest.mark.parametrize(
    ("local", "export", "eligible"),
    [("2", "0", Fraction(11, 10)), ("1", "1", Fraction(1, 10)), ("0", "2", Fraction())],
)
async def test_mixed_charge_uses_guaranteed_origin_and_local_intersection(  # noqa: PLR0913
    hass: HomeAssistant,
    timers: list[_Timer],
    *,
    topology: str,
    local: str,
    export: str,
    eligible: Fraction,
) -> None:
    """ADR 9.4 excludes grid origin and export without proportional attribution."""
    site = await _site(hass, timers, topology=topology)
    charged = await site.step({"pv_generation": 3, "grid_import": 1, "charge": 4})
    assert charged.ledger is not None
    assert (
        charged.ledger.stored_lower.kwh
        == charged.ledger.stored_upper.kwh
        == Fraction(18, 5)
    )
    assert charged.ledger.pv_lower.kwh == Fraction(27, 10)
    assert charged.ledger.non_pv_upper.kwh == Fraction(9, 10)
    assert charged.totals.direct_pv_kwh == charged.totals.storage_pv_kwh == 0
    discharged = await site.step(
        {"discharge": 2, "load": local, "grid_export": export}, grid="500"
    )
    assert discharged.totals.storage_pv_kwh == eligible
    assert discharged.totals.storage_gross_g == eligible * 500
    assert discharged.totals.storage_pv_burden_g == eligible * Fraction(400, 9)
    assert discharged.totals.storage_burden_g == eligible * 20
    assert discharged.ledger is not None
    assert discharged.ledger.pv_lower.kwh == Fraction(7, 10)
    assert discharged.ledger.pv_burden.grams == Fraction(280, 9)


async def test_pure_grid_charge_never_receives_pv_credit(
    hass: HomeAssistant, timers: list[_Timer]
) -> None:
    """Observed stored grid energy remains non-PV throughout a complete cycle."""
    site = await _site(hass, timers)
    charged = await site.step({"grid_import": 3, "charge": 3})
    assert charged.ledger is not None
    assert charged.ledger.pv_lower.kwh == 0
    assert charged.ledger.non_pv_upper.kwh == Fraction(27, 10)
    discharged = await site.step({"discharge": "2.7", "load": "2.7"}, grid="1000")
    assert discharged.totals.storage_pv_kwh == 0
    assert discharged.totals.storage_gross_g == 0
    assert (
        discharged.totals.storage_pv_burden_g == discharged.totals.storage_burden_g == 0
    )


async def test_unobserved_initial_content_cannot_be_reclassified_as_pv(
    hass: HomeAssistant, timers: list[_Timer]
) -> None:
    """Charging an initially unknown battery cannot prove early discharge origin."""
    site = await _site(hass, timers, observe_empty=False)
    await site.step({"pv_generation": 3, "charge": 3})
    discharged = await site.step({"discharge": 2, "load": 2}, grid="500")
    assert discharged.totals.storage_pv_kwh == 0
    assert discharged.totals.storage_net_g == 0
    assert discharged.ledger is not None
    assert discharged.ledger.pv_lower.kwh == Fraction(7, 10)


async def test_direct_and_storage_results_share_one_verified_generation(
    hass: HomeAssistant, timers: list[_Timer]
) -> None:
    """Concurrent PV and storage supply book distinct energy in one revision."""
    site = await _site(hass, timers)
    charged = await _pv_charge(site)
    combined = await site.step(
        {"pv_generation": 1, "discharge": 2, "load": 3}, grid="500"
    )
    assert combined.commit_revision == charged.commit_revision + 1
    assert combined.totals.direct_pv_kwh - charged.totals.direct_pv_kwh == 1
    assert combined.totals.storage_pv_kwh == 2
    assert combined.totals.direct_gross_g - charged.totals.direct_gross_g == 500
    assert combined.totals.storage_gross_g == 1000
    assert combined.measurement.baseline is not None
    assert combined.measurement.baseline.period_end == site.period
    assert combined.ledger is not None
    assert combined.ledger.pv_lower.kwh == Fraction(7, 10)


async def test_storage_net_can_be_negative_without_clamping(
    hass: HomeAssistant, timers: list[_Timer]
) -> None:
    """Lifecycle burdens can exceed avoided emissions at a low discharge intensity."""
    site = await _site(hass, timers)
    await _pv_charge(site)
    discharged = await site.step({"discharge": 2, "load": 2}, grid="10")
    assert discharged.totals.storage_gross_g == 20
    assert discharged.totals.storage_net_g == -Fraction(980, 9)


async def test_missing_current_grid_consumes_provenance_without_later_revaluation(
    hass: HomeAssistant, timers: list[_Timer]
) -> None:
    """An unvalued discharge removes its burden and energy before future valid polls."""
    site = await _site(hass, timers)
    await _pv_charge(site)
    unvalued = await site.step({"discharge": 2, "load": 2}, grid=None)
    assert unvalued.totals.storage_pv_kwh == unvalued.totals.unvalued_storage_kwh == 2
    assert (
        unvalued.totals.storage_gross_g
        == unvalued.totals.storage_pv_burden_g
        == unvalued.totals.storage_burden_g
        == 0
    )
    assert unvalued.ledger is not None
    assert unvalued.ledger.pv_lower.kwh == Fraction(7, 10)
    assert unvalued.ledger.pv_burden.grams == Fraction(280, 9)
    valued = await site.step({"discharge": "0.7", "load": "0.7"}, grid="500")
    assert valued.totals.storage_pv_kwh == Fraction(27, 10)
    assert valued.totals.unvalued_storage_kwh == 2
    assert valued.totals.storage_gross_g == 350
    assert valued.totals.storage_pv_burden_g == Fraction(280, 9)
    assert valued.totals.storage_burden_g == 14


@pytest.mark.parametrize(
    "fault",
    ["counter_reset", "unavailable", "simultaneous", "over_capacity", "over_discharge"],
)
async def test_interruption_quarantines_storage_and_skips_all_direct_results(
    hass: HomeAssistant, timers: list[_Timer], fault: str
) -> None:
    """Rejected physical intervals erase provenance once while preserving all totals."""
    site = await _site(hass, timers)
    charged = await _pv_charge(site)
    if fault == "simultaneous":
        interrupted = await site.step(
            {"pv_generation": 2, "charge": 1, "discharge": 1, "load": 2}
        )
    elif fault == "over_capacity":
        interrupted = await site.step({"pv_generation": 22, "charge": 20, "load": 2})
    elif fault == "over_discharge":
        interrupted = await site.step({"pv_generation": 1, "discharge": 11, "load": 12})
    else:
        site.period += timedelta(minutes=1)
        value = "0" if fault == "counter_reset" else "unavailable"
        for role, counter in site.counters.items():
            _energy(
                hass,
                site.sources[role],
                value if role == "grid_import" else str(counter),
                site.period,
            )
        _grid(hass, site.sources, site.period)
        await _tick(hass, timers, site.period)
        interrupted = site.entry.runtime_data.state
        if fault == "counter_reset":
            site.counters["grid_import"] = Decimal()
    assert interrupted.measurement.phase is MeasurementPhase.AWAITING_REBASELINE
    assert interrupted.totals == charged.totals
    assert interrupted.consumer_totals == charged.consumer_totals
    assert interrupted.ledger == StorageLedger.quarantined(Energy(Fraction(10)))
    assert dict(interrupted.diagnostics)["discarded_intervals"] == 1
    # An old accepted period is an inadmissible recovery replay. A newer complete
    # numeric vector could legitimately establish a baseline even if its delta
    # was previously rejected; recovery deliberately does not evaluate that delta.
    assert charged.measurement.baseline is not None
    registered = {source.id: source for source in site.sources.values()}
    for sample in charged.measurement.baseline.samples:
        _energy(
            hass,
            registered[sample.source.registry_id],
            str(sample.cumulative.kwh),
            sample.period_end,
            reported_at=sample.last_reported,
        )
    await _tick(hass, timers, site.period)
    assert site.entry.runtime_data.state == interrupted
    recovered = await site.step({})
    assert recovered.measurement.phase is MeasurementPhase.ACTIVE
    assert recovered.totals == charged.totals
    assert recovered.ledger == StorageLedger.quarantined(Energy(Fraction(10)))


async def test_filled_storage_survives_a_new_home_assistant_instance(
    hass: HomeAssistant, timers: list[_Timer]
) -> None:
    """Restart preserves exact output inventory and its charge-time burden density."""
    site = await _site(hass, timers)
    charged = await _pv_charge(site)
    assert await hass.config_entries.async_unload(site.entry.entry_id)
    async with async_test_home_assistant(
        config_dir=hass.config.config_dir
    ) as restarted:
        restarted.data.pop(loader.DATA_CUSTOM_COMPONENTS)
        mock_registry(
            restarted, {source.entity_id: source for source in site.sources.values()}
        )
        entry = MockConfigEntry(
            domain=DOMAIN, entry_id=site.entry.entry_id, data=dict(site.entry.data)
        )
        entry.add_to_hass(restarted)
        assert await restarted.config_entries.async_setup(entry.entry_id)
        assert entry.runtime_data.state == charged
        site.hass = restarted
        site.entry = entry
        discharged = await site.step({"discharge": 2, "load": 2}, grid="500")
        assert discharged.totals.storage_net_g == Fraction(7840, 9)
        assert discharged.ledger is not None
        assert discharged.ledger.pv_lower.kwh == Fraction(7, 10)
        assert await restarted.config_entries.async_unload(entry.entry_id)
        await restarted.async_stop()


@pytest.mark.parametrize("failure", ["swallowed_save", "readback"])
async def test_atomic_failure_never_publishes_partial_direct_or_storage_state(
    hass: HomeAssistant,
    timers: list[_Timer],
    reads: _Reads,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """A simultaneous direct/storage interval recovers wholly and exactly once."""
    site = await _site(hass, timers)
    charged = await _pv_charge(site)
    runtime = site.entry.runtime_data
    key = runtime.store.store_key
    original_save = Store.async_save
    original_load = Store.async_load
    loads = 0

    async def save(store: Store[dict[str, object]], data: dict[str, object]) -> None:
        if store.key == key and failure == "swallowed_save":
            return
        await original_save(store, data)

    async def load(store: Store[dict[str, object]]) -> dict[str, object] | None:
        nonlocal loads
        if store.key == key:
            loads += 1
            if failure == "readback" and loads == 2:
                message = "storage generation verification failed"
                raise OSError(message)
        return await original_load(store)

    site.period += timedelta(minutes=1)
    for role, increment in {"pv_generation": 1, "discharge": 2, "load": 3}.items():
        site.counters[role] += Decimal(increment)
    for role, counter in site.counters.items():
        _energy(hass, site.sources[role], str(counter), site.period)
    _grid(hass, site.sources, site.period, value="500")
    timer = timers[-1]
    with monkeypatch.context() as patch:
        patch.setattr(Store, "async_save", save)
        patch.setattr(Store, "async_load", load)
        await _tick(hass, timers, site.period)
        assert runtime.state == charged
        assert runtime.failed
        assert not runtime.available
        copies = (reads.energy, reads.grid)
        await timer.action(site.period + timedelta(minutes=1))
        assert (reads.energy, reads.grid) == copies
    assert await hass.config_entries.async_reload(site.entry.entry_id)
    await _tick(hass, timers, site.period + timedelta(minutes=1))
    recovered = site.entry.runtime_data.state
    assert recovered.totals.direct_pv_kwh == 3
    assert recovered.totals.storage_pv_kwh == 2
    assert recovered.totals.storage_net_g == Fraction(7840, 9)
    assert recovered.ledger is not None
    assert recovered.ledger.pv_lower.kwh == Fraction(7, 10)
    assert recovered == await site.entry.runtime_data.store.async_load()


async def test_storage_unload_waits_for_one_complete_ledger_and_emissions_commit(
    hass: HomeAssistant, timers: list[_Timer], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unload cancels future polls, then lets an active storage transaction finish."""
    site = await _site(hass, timers)
    charged = await _pv_charge(site)
    runtime = site.entry.runtime_data
    original_save = Store.async_save
    started = asyncio.Event()
    allow = asyncio.Event()

    async def blocked_save(
        store: Store[dict[str, object]], data: dict[str, object]
    ) -> None:
        if store.key == runtime.store.store_key:
            started.set()
            await allow.wait()
        await original_save(store, data)

    monkeypatch.setattr(Store, "async_save", blocked_save)
    timer = timers[-1]
    site.publish({"discharge": 2, "load": 2}, grid="500")
    task = asyncio.create_task(timer.action(site.period))
    await started.wait()
    unload = asyncio.create_task(hass.config_entries.async_unload(site.entry.entry_id))
    await timer.cancelled.wait()
    assert not unload.done()
    assert runtime.state == charged
    allow.set()
    await task
    assert await unload
    assert runtime.state.totals.storage_pv_kwh == 2
    assert runtime.state.ledger is not None
    assert runtime.state.ledger.pv_lower.kwh == Fraction(7, 10)
