# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Exact multi-cycle hand calculation through both HA topologies and a restart."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import TYPE_CHECKING

import pytest
from homeassistant import loader
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_test_home_assistant,
    mock_registry,
)

from custom_components.co2saver.const import DOMAIN

from .test_runtime import _HOUSE, _WALLBOX, reads, runtime_environment, timers
from .test_storage_runtime import _site

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.core import HomeAssistant

    from custom_components.co2saver.persistence import GenerationState

    from .test_runtime import _Reads, _Timer
    from .test_storage_runtime import _StorageSite

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")
__all__ = ("reads", "runtime_environment", "timers")


@dataclass(frozen=True)
class _ReferenceRow:
    """Independent exact expectations from docs/accounting-reference.md."""

    flows: Mapping[str, str | int]
    grid: str
    stored: str
    pv: str
    deferred_pv_burden: str
    storage_energy: str
    storage_gross: str
    storage_pv_burden: str
    storage_battery_burden: str
    storage_net: str
    house_storage: str
    wallbox_storage: str
    storage_rest: str


_REFERENCE_ROWS = (
    _ReferenceRow(
        flows={
            "pv_generation": 6,
            "load": 2,
            "wallbox": "0.5",
            "charge": 3,
            "grid_export": 1,
        },
        grid="400",
        stored="2.7",
        pv="2.7",
        deferred_pv_burden="120",
        storage_energy="0",
        storage_gross="0",
        storage_pv_burden="0",
        storage_battery_burden="0",
        storage_net="0",
        house_storage="0",
        wallbox_storage="0",
        storage_rest="0",
    ),
    _ReferenceRow(
        flows={"discharge": 1, "load": 1, "wallbox": "0.25"},
        grid="500",
        stored="1.7",
        pv="1.7",
        deferred_pv_burden="680/9",
        storage_energy="1",
        storage_gross="500",
        storage_pv_burden="400/9",
        storage_battery_burden="20",
        storage_net="3920/9",
        house_storage="0.75",
        wallbox_storage="0.25",
        storage_rest="0",
    ),
    _ReferenceRow(
        flows={"pv_generation": 3, "grid_import": 1, "charge": 4},
        grid="100",
        stored="5.3",
        pv="4.4",
        deferred_pv_burden="1760/9",
        storage_energy="1",
        storage_gross="500",
        storage_pv_burden="400/9",
        storage_battery_burden="20",
        storage_net="3920/9",
        house_storage="0.75",
        wallbox_storage="0.25",
        storage_rest="0",
    ),
    _ReferenceRow(
        flows={"discharge": 2, "load": 1, "wallbox": "0.25", "grid_export": 1},
        grid="600",
        stored="3.3",
        pv="2.4",
        deferred_pv_burden="320/3",
        storage_energy="1.1",
        storage_gross="560",
        storage_pv_burden="440/9",
        storage_battery_burden="22",
        storage_net="4402/9",
        house_storage="0.75",
        wallbox_storage="0.25",
        storage_rest="0.1",
    ),
    _ReferenceRow(
        flows={"discharge": "3.3", "load": "3.3", "wallbox": "0.825"},
        grid="300",
        stored="0",
        pv="0",
        deferred_pv_burden="0",
        storage_energy="3.5",
        storage_gross="1280",
        storage_pv_burden="1400/9",
        storage_battery_burden="70",
        storage_net="9490/9",
        house_storage="93/40",
        wallbox_storage="0.25",
        storage_rest="37/40",
    ),
)


def _assert_reference_row(state: GenerationState, expected: _ReferenceRow) -> None:
    """Compare ledger, emissions and consumer energy with the written reference."""
    ledger = state.ledger
    assert ledger is not None
    assert (
        ledger.stored_lower.kwh == ledger.stored_upper.kwh == Fraction(expected.stored)
    )
    assert ledger.pv_lower.kwh == Fraction(expected.pv)
    assert ledger.non_pv_upper.kwh == Fraction(expected.stored) - Fraction(expected.pv)
    assert ledger.pv_burden.grams == Fraction(expected.deferred_pv_burden)
    assert ledger.pv_density_upper.grams_per_kwh == (
        Fraction(400, 9) if ledger.pv_burden.grams else 0
    )
    totals = state.totals
    assert totals.direct_pv_kwh == 2
    assert totals.direct_gross_g == 800
    assert totals.direct_pv_burden_g == 80
    assert totals.direct_net_g == 720
    assert totals.storage_pv_kwh == Fraction(expected.storage_energy)
    assert totals.storage_gross_g == Fraction(expected.storage_gross)
    assert totals.storage_pv_burden_g == Fraction(expected.storage_pv_burden)
    assert totals.storage_burden_g == Fraction(expected.storage_battery_burden)
    assert totals.storage_net_g == Fraction(expected.storage_net)
    assert totals.unvalued_direct_kwh == totals.unvalued_storage_kwh == 0
    consumers = dict(state.consumer_totals)
    assert consumers[_HOUSE].direct_pv_kwh == Fraction(3, 2)
    assert consumers[_WALLBOX].direct_pv_kwh == Fraction(1, 2)
    assert consumers[_HOUSE].storage_pv_kwh == Fraction(expected.house_storage)
    assert consumers[_WALLBOX].storage_pv_kwh == Fraction(expected.wallbox_storage)
    assert state.unassigned_direct_kwh == 0
    assert state.unassigned_storage_kwh == Fraction(expected.storage_rest)
    assert all(count == 0 for _, count in state.diagnostics)


async def _apply_row(site: _StorageSite, expected: _ReferenceRow) -> GenerationState:
    """Drive real HA states, the UTC poll, persistence and the sensor dispatcher."""
    previous = site.entry.runtime_data.state
    state = await site.step(expected.flows, grid=expected.grid)
    assert state.commit_revision == previous.commit_revision + 1
    assert site.entry.runtime_data.available
    _assert_reference_row(state, expected)
    return state


@pytest.mark.parametrize("topology", ["inverter", "smart_meter"])
@pytest.mark.parametrize("mode", ["aggregate_shares", "separate_meters"])
async def test_documented_overlapping_storage_cycles_survive_restart_exactly(
    hass: HomeAssistant,
    timers: list[_Timer],
    reads: _Reads,
    topology: str,
    mode: str,
) -> None:
    """Two charge cycles retain exact conservative results across a new HA instance."""
    site = await _site(hass, timers, topology=topology, mode=mode)
    for row in _REFERENCE_ROWS[:3]:
        await _apply_row(site, row)
    saved = site.entry.runtime_data.state
    read_count = reads.energy
    assert reads.grid == read_count
    original_timer = timers[-1]
    assert await hass.config_entries.async_unload(site.entry.entry_id)
    assert original_timer.cancelled.is_set()
    async with async_test_home_assistant(
        config_dir=hass.config.config_dir
    ) as restarted:
        restarted.data.pop(loader.DATA_CUSTOM_COMPONENTS)
        mock_registry(
            restarted, {source.entity_id: source for source in site.sources.values()}
        )
        entry = MockConfigEntry(
            domain=DOMAIN,
            entry_id=site.entry.entry_id,
            data=dict(site.entry.data),
        )
        entry.add_to_hass(restarted)
        assert await restarted.config_entries.async_setup(entry.entry_id)
        assert entry.runtime_data.state == saved
        assert reads.energy == reads.grid == read_count
        site.hass = restarted
        site.entry = entry
        for row in _REFERENCE_ROWS[3:]:
            final = await _apply_row(site, row)
        assert final.totals.direct_net_g + final.totals.storage_net_g == Fraction(
            15970, 9
        )
        assert final.totals.direct_pv_kwh + final.totals.storage_pv_kwh == Fraction(
            11, 2
        )
        assert final.totals.direct_pv_kwh + final.totals.storage_pv_kwh <= 9
        # The conservative origin guarantee lost at the export interval is 1.9 kWh;
        # its 760/9 g burden can never reappear in a later credited discharge.
        assert final.totals.storage_pv_burden_g + Fraction(760, 9) == 240
        assert await restarted.config_entries.async_unload(entry.entry_id)
        await restarted.async_stop()
