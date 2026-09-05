# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Record real sensor states and compile Home Assistant's native statistics."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from homeassistant.components.recorder.statistics import (
    async_list_statistic_ids,
    statistics_during_period,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
    do_adhoc_statistics,
)

from custom_components.co2saver.const import DOMAIN
from custom_components.co2saver.repair_issues import async_report_storage_issue

from .test_repairs import _flow
from .test_runtime import (
    _BASELINE,
    _INTERVAL,
    _START,
    _baseline,
    _grid,
    _plan,
    _setup,
    _tick,
    _vector,
    timers,
)

if TYPE_CHECKING:
    from pathlib import Path

    from homeassistant.components.recorder import Recorder
    from homeassistant.core import HomeAssistant
    from pytest_freezer import FrozenDateTimeFactory

    from .test_runtime import _Timer

__all__ = ("timers",)
pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "recorder_mock")


@pytest.fixture(autouse=True)
def statistics_environment(
    recorder_mock: Recorder,  # noqa: ARG001 - recorder must precede the hass fixture
    hass: HomeAssistant,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Initialize Recorder before HA, then freeze the integration's installation."""
    hass.config.config_dir = str(tmp_path)
    monkeypatch.setattr(dt_util, "utcnow", lambda: _START)


async def test_native_statistics_keep_signed_net_and_monotone_components(
    hass: HomeAssistant,
    timers: list[_Timer],
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Native statistics preserve a negative interval without resetting net totals."""
    freezer.move_to(_START)
    await hass.async_start()
    plan, sources = _plan(hass)
    entry = await _setup(hass, plan)
    registry = er.async_get(hass)
    metrics = ("net_savings", "gross_avoided", "pv_lifecycle", "direct_pv_energy")
    ids = {
        metric: registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}:{metric}"
        )
        for metric in metrics
    }
    assert all(ids.values())
    freezer.move_to(_BASELINE)
    await _baseline(hass, sources, timers)
    freezer.move_to(_INTERVAL)
    _vector(hass, sources, _INTERVAL, cycles=1)
    _grid(hass, sources, _INTERVAL)
    await _tick(hass, timers, _INTERVAL)
    await async_wait_recording_done(hass)
    start = _START.replace(second=0)
    freezer.move_to(start + timedelta(minutes=5))
    do_adhoc_statistics(hass, start=start)
    await async_wait_recording_done(hass)

    second = start + timedelta(minutes=6)
    freezer.move_to(second)
    _vector(hass, sources, second, cycles=2)
    _grid(hass, sources, second, value="10")
    await _tick(hass, timers, second)
    await async_wait_recording_done(hass)
    freezer.move_to(start + timedelta(minutes=10))
    do_adhoc_statistics(hass, start=start + timedelta(minutes=5))
    await async_wait_recording_done(hass)

    entity_ids = {entity_id for entity_id in ids.values() if entity_id is not None}
    rows = await hass.async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        start + timedelta(minutes=10),
        entity_ids,
        "5minute",
        None,
        {"state", "sum"},
    )
    expected = {
        "net_savings": (0.72, 0.66),
        "gross_avoided": (0.8, 0.82),
        "pv_lifecycle": (0.08, 0.16),
        "direct_pv_energy": (2, 4),
    }
    for metric, entity_id in ids.items():
        assert entity_id is not None
        assert [row["state"] for row in rows[entity_id]] == pytest.approx(
            expected[metric]
        )
        assert [row["sum"] for row in rows[entity_id]] == pytest.approx(
            expected[metric]
        )
    metadata = await async_list_statistic_ids(hass, entity_ids, "sum")
    assert len(metadata) == len(metrics)
    assert all(item["has_sum"] for item in metadata)
    assert {item["statistics_unit_of_measurement"] for item in metadata} == {
        "kgCO₂e",
        "kWh",
    }
    assert not [
        record.message
        for record in caplog.records
        if record.levelname in {"WARNING", "ERROR"}
        and record.name.startswith("homeassistant.components.sensor")
    ]
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_confirmed_repair_starts_a_new_statistics_cycle(
    hass: HomeAssistant,
    timers: list[_Timer],
    freezer: FrozenDateTimeFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real repair preserves Recorder sums for both signed and monotone entities."""
    freezer.move_to(_START)
    await hass.async_start()
    plan, sources = _plan(hass)
    entry = await _setup(hass, plan)
    registry = er.async_get(hass)
    metrics = ("net_savings", "gross_avoided", "direct_pv_energy")
    ids = {
        metric: registry.async_get_entity_id(
            "sensor", DOMAIN, f"{entry.entry_id}:{metric}"
        )
        for metric in metrics
    }
    freezer.move_to(_BASELINE)
    await _baseline(hass, sources, timers)
    freezer.move_to(_INTERVAL)
    _vector(hass, sources, _INTERVAL, cycles=1)
    _grid(hass, sources, _INTERVAL)
    await _tick(hass, timers, _INTERVAL)
    await async_wait_recording_done(hass)
    start = _START.replace(second=0)
    freezer.move_to(start + timedelta(minutes=5))
    do_adhoc_statistics(hass, start=start)
    await async_wait_recording_done(hass)

    reset_at = start + timedelta(minutes=6)
    freezer.move_to(reset_at)
    monkeypatch.setattr(dt_util, "utcnow", lambda: reset_at)
    async_report_storage_issue(hass, entry)
    manager, menu = await _flow(hass, entry)
    await manager.async_configure(menu["flow_id"], {"next_step_id": "confirm"})
    await manager.async_configure(menu["flow_id"], {"confirm_reset": True})
    assert entry.runtime_data.state.repair_reset_at == reset_at
    for minutes, cycles in ((7, 2), (8, 3)):
        period = start + timedelta(minutes=minutes)
        freezer.move_to(period)
        _vector(hass, sources, period, cycles=cycles)
        _grid(hass, sources, period)
        await _tick(hass, timers, period)
        await async_wait_recording_done(hass)
    freezer.move_to(start + timedelta(minutes=10))
    do_adhoc_statistics(hass, start=start + timedelta(minutes=5))
    await async_wait_recording_done(hass)
    entity_ids = {entity_id for entity_id in ids.values() if entity_id is not None}
    rows = await hass.async_add_executor_job(
        statistics_during_period,
        hass,
        start,
        start + timedelta(minutes=10),
        entity_ids,
        "5minute",
        None,
        {"state", "sum"},
    )
    amounts = {"net_savings": 0.72, "gross_avoided": 0.8, "direct_pv_energy": 2}
    for metric, amount in amounts.items():
        entity_id = ids[metric]
        assert entity_id is not None
        assert [row["state"] for row in rows[entity_id]] == pytest.approx(
            (amount, amount)
        )
        assert [row["sum"] for row in rows[entity_id]] == pytest.approx(
            (amount, amount * 2)
        )
    assert await hass.config_entries.async_unload(entry.entry_id)
