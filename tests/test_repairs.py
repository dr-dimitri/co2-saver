# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Public repair flows preserve history until an explicit, verified reset."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import timedelta
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.co2saver.bootstrap import manifest_store
from custom_components.co2saver.const import DOMAIN
from custom_components.co2saver.measurement.storage import (
    VerifiedAtomicStoreVerificationError,
)
from custom_components.co2saver.repair_issues import (
    async_report_storage_issue,
    storage_issue_id,
)
from custom_components.co2saver.repair_storage import async_complete_repair
from custom_components.co2saver.repairs import async_create_fix_flow

from .test_runtime import (
    _INTERVAL,
    _baseline,
    _grid,
    _plan,
    _setup,
    _tick,
    _vector,
    runtime_environment,
    timers,
)

if TYPE_CHECKING:
    from homeassistant.components.repairs.issue_handler import RepairsFlowManager
    from homeassistant.components.repairs.models import RepairsFlowResult
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    from custom_components.co2saver.persistence import Manifest

    from .test_runtime import _Timer

__all__ = ("runtime_environment", "timers")
pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "timers")


async def _manager(hass: HomeAssistant) -> RepairsFlowManager:
    """Load the real Repairs integration and its public flow manager."""
    assert await async_setup_component(hass, "repairs", {})
    return cast("RepairsFlowManager", hass.data["repairs"]["flow_manager"])


async def _flow(
    hass: HomeAssistant, entry: MockConfigEntry
) -> tuple[RepairsFlowManager, RepairsFlowResult]:
    """Start the registered issue's actual repair flow."""
    manager = await _manager(hass)
    result = await manager.async_init(
        DOMAIN, data={"issue_id": storage_issue_id(entry.entry_id)}
    )
    assert result["type"] is FlowResultType.MENU
    return manager, result


def _accounting_files(hass_storage: dict[str, Any]) -> dict[str, Any]:
    """Ignore unrelated HA registry writes when proving accounting immutability."""
    return deepcopy(
        {
            key: value
            for key, value in hass_storage.items()
            if key.startswith("co2saver.")
        }
    )


async def test_cancel_and_missing_confirmation_never_change_accounting(
    hass: HomeAssistant, hass_storage: dict[str, Any], timers: list[_Timer]
) -> None:
    """Opening a repair or rejecting its checkbox changes no pointer or balance."""
    plan, _ = _plan(hass)
    entry = await _setup(hass, plan)
    async_report_storage_issue(hass, entry)
    before = _accounting_files(hass_storage)
    manager, menu = await _flow(hass, entry)
    result = await manager.async_configure(menu["flow_id"], {"next_step_id": "confirm"})
    assert result["step_id"] == "confirm"
    result = await manager.async_configure(result["flow_id"], {"confirm_reset": False})
    assert result["errors"] == {"base": "confirmation_required"}
    manager.async_abort(result["flow_id"])
    assert _accounting_files(hass_storage) == before
    assert not timers[-1].cancelled.is_set()
    assert ir.async_get(hass).async_get_issue(DOMAIN, storage_issue_id(entry.entry_id))
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_retry_verifies_existing_generation_without_reset(
    hass: HomeAssistant, hass_storage: dict[str, Any], timers: list[_Timer]
) -> None:
    """An uncertain write can recover its intact authoritative state by reload."""
    plan, sources = _plan(hass)
    entry = await _setup(hass, plan)
    await _baseline(hass, sources, timers)
    before = entry.runtime_data.state
    async_report_storage_issue(hass, entry)
    files = _accounting_files(hass_storage)
    manager, menu = await _flow(hass, entry)
    result = await manager.async_configure(menu["flow_id"], {"next_step_id": "retry"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.state == before
    assert _accounting_files(hass_storage) == files
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, storage_issue_id(entry.entry_id))
        is None
    )
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("battery", [False, True])
async def test_confirmed_reset_preserves_old_generation_and_sensor_identity(
    hass: HomeAssistant,
    hass_storage: dict[str, Any],
    timers: list[_Timer],
    monkeypatch: pytest.MonkeyPatch,
    *,
    battery: bool,
) -> None:
    """A fresh confirmed generation resets only at a post-repair physical boundary."""
    plan, sources = _plan(hass, battery=battery)
    entry = await _setup(hass, plan)
    await _baseline(hass, sources, timers)
    _vector(hass, sources, _INTERVAL, cycles=1)
    _grid(hass, sources, _INTERVAL)
    await _tick(hass, timers, _INTERVAL)
    previous = entry.runtime_data.state
    old_key = entry.runtime_data.store.store_key
    old_payload = deepcopy(hass_storage[old_key])
    registry = er.async_get(hass)
    net_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}:net_savings"
    )
    assert net_id is not None
    async_report_storage_issue(hass, entry)
    reset_at = _INTERVAL + timedelta(minutes=1)
    monkeypatch.setattr(dt_util, "utcnow", lambda: reset_at)
    manager, menu = await _flow(hass, entry)
    await manager.async_configure(menu["flow_id"], {"next_step_id": "confirm"})
    result = await manager.async_configure(menu["flow_id"], {"confirm_reset": True})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.state is ConfigEntryState.LOADED
    current = entry.runtime_data.state
    assert current.generation != previous.generation
    assert current.totals.direct_pv_kwh == current.totals.storage_pv_kwh == 0
    assert (
        current.repair_reset_at == current.measurement.segment_transition_at == reset_at
    )
    assert hass_storage[old_key] == old_payload
    manifest = await manifest_store(hass, current.storage_id).async_load()
    assert manifest is not None
    assert manifest.initialized
    assert not manifest.repair_pending
    assert previous.generation in manifest.previous_generations
    assert (
        registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}:net_savings")
        == net_id
    )
    period = reset_at + timedelta(minutes=1)
    _vector(hass, sources, period, cycles=2)
    _grid(hass, sources, period)
    await _tick(hass, timers, period)
    sensor = hass.states.get(net_id)
    assert sensor is not None
    assert float(sensor.state) == 0
    assert sensor.attributes["last_reset"] == reset_at.isoformat()
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_damaged_setup_exposes_fixable_issue_without_runner_or_entities(
    hass: HomeAssistant, hass_storage: dict[str, Any], timers: list[_Timer]
) -> None:
    """A missing initialized generation offers repair instead of an empty first run."""
    plan, _ = _plan(hass)
    entry = await _setup(hass, plan)
    key = entry.runtime_data.store.store_key
    assert await hass.config_entries.async_unload(entry.entry_id)
    hass_storage.pop(key)
    count = len(timers)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert len(timers) == count
    assert not hasattr(entry, "runtime_data")
    issue = ir.async_get(hass).async_get_issue(DOMAIN, storage_issue_id(entry.entry_id))
    assert issue is not None
    assert issue.is_fixable
    assert issue.is_persistent
    assert issue.severity is ir.IssueSeverity.ERROR
    manager, menu = await _flow(hass, entry)
    result = await manager.async_configure(menu["flow_id"], {"next_step_id": "retry"})
    assert result["errors"] == {"base": "reload_failed"}
    assert key not in hass_storage
    result = await manager.async_configure(menu["flow_id"], {"confirm_reset": True})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("failure", ["prepare", "reload_false", "not_loaded"])
async def test_failed_repair_remains_open_and_resumes_same_prepared_generation(
    hass: HomeAssistant, failure: str
) -> None:
    """Failed reload cannot announce success or replace a prepared generation twice."""
    plan, _ = _plan(hass)
    entry = await _setup(hass, plan)
    async_report_storage_issue(hass, entry)
    manager, menu = await _flow(hass, entry)
    await manager.async_configure(menu["flow_id"], {"next_step_id": "confirm"})
    if failure == "prepare":
        boundary = patch(
            "custom_components.co2saver.repairs.async_prepare_repair",
            side_effect=OSError("disk unavailable"),
        )
    else:
        boundary = patch.object(
            hass.config_entries,
            "async_reload",
            new=AsyncMock(return_value=failure == "not_loaded"),
        )
    with boundary:
        result = await manager.async_configure(menu["flow_id"], {"confirm_reset": True})
    assert result["type"] is FlowResultType.FORM
    assert ir.async_get(hass).async_get_issue(DOMAIN, storage_issue_id(entry.entry_id))
    before = await manifest_store(hass, entry.data["storage_id"]).async_load()
    assert before is not None
    result = await manager.async_configure(menu["flow_id"], {"confirm_reset": True})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    if failure != "prepare":
        assert entry.runtime_data.state.generation == before.active_generation
        assert entry.runtime_data.state.repair_reset_at == before.repair_reset_at
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_parallel_confirmations_reset_once_and_close_stale_flow(
    hass: HomeAssistant,
) -> None:
    """The entry lock covers prepare, reload and issue removal as a single repair."""
    plan, _ = _plan(hass)
    entry = await _setup(hass, plan)
    old = entry.runtime_data.state.generation
    async_report_storage_issue(hass, entry)
    manager, first = await _flow(hass, entry)
    _, second = await _flow(hass, entry)
    for result in (first, second):
        await manager.async_configure(result["flow_id"], {"next_step_id": "confirm"})
    results = await asyncio.gather(
        *(
            manager.async_configure(result["flow_id"], {"confirm_reset": True})
            for result in (first, second)
        )
    )
    assert {result["type"] for result in results} == {
        FlowResultType.CREATE_ENTRY,
        FlowResultType.ABORT,
    }
    assert any(result.get("reason") == "already_repaired" for result in results)
    manifest = await manifest_store(hass, entry.data["storage_id"]).async_load()
    assert manifest is not None
    assert manifest.previous_generations == (old,)
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("resume", ["parallel_confirm", "new_retry"])
async def test_initialized_failed_repair_resumes_durable_pending_generation(
    hass: HomeAssistant, timers: list[_Timer], resume: str
) -> None:
    """A different dialog cannot reset an initialized but unfinished repair again."""
    plan, _ = _plan(hass)
    entry = await _setup(hass, plan)
    old = entry.runtime_data.state.generation
    async_report_storage_issue(hass, entry)
    manager, first = await _flow(hass, entry)
    _, second = await _flow(hass, entry)
    await manager.async_configure(first["flow_id"], {"next_step_id": "confirm"})
    reload_entry = hass.config_entries.async_reload

    async def initialize_then_fail(entry_id: str) -> bool:
        """Model a failure after the replacement has been durably initialized."""
        assert await reload_entry(entry_id)
        return False

    with patch.object(
        hass.config_entries, "async_reload", side_effect=initialize_then_fail
    ):
        result = await manager.async_configure(
            first["flow_id"], {"confirm_reset": True}
        )
    assert result["errors"] == {"base": "reload_failed"}
    pending = await manifest_store(hass, entry.data["storage_id"]).async_load()
    assert pending is not None
    assert pending.initialized
    assert pending.repair_pending
    assert pending.previous_generations == (old,)
    assert timers[-1].cancelled.is_set()
    if resume == "parallel_confirm":
        await manager.async_configure(second["flow_id"], {"next_step_id": "confirm"})
        result = await manager.async_configure(
            second["flow_id"], {"confirm_reset": True}
        )
    else:
        manager.async_abort(first["flow_id"])
        manager.async_abort(second["flow_id"])
        _, third = await _flow(hass, entry)
        result = await manager.async_configure(
            third["flow_id"], {"next_step_id": "retry"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    complete = await manifest_store(hass, entry.data["storage_id"]).async_load()
    assert complete is not None
    assert not complete.repair_pending
    assert complete.active_generation == pending.active_generation
    assert complete.repair_reset_at == pending.repair_reset_at
    assert complete.previous_generations == (old,)
    assert not timers[-1].cancelled.is_set()
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("completion_applied", [False, True])
async def test_unverifiable_repair_completion_stops_writer_and_preserves_issue(
    hass: HomeAssistant, timers: list[_Timer], *, completion_applied: bool
) -> None:
    """Reload alone cannot close a repair with an uncertain completion commit."""
    plan, _ = _plan(hass)
    entry = await _setup(hass, plan)
    async_report_storage_issue(hass, entry)
    manager, menu = await _flow(hass, entry)
    await manager.async_configure(menu["flow_id"], {"next_step_id": "confirm"})

    async def uncertain_completion(
        repair_hass: HomeAssistant,
        repair_entry: ConfigEntry,
        *,
        prepared: Manifest | None = None,
    ) -> None:
        """Model uncertainty both before and after the durable completion save."""
        if completion_applied:
            await async_complete_repair(repair_hass, repair_entry, prepared=prepared)
        message = "unverified completion"
        raise VerifiedAtomicStoreVerificationError(message)

    with patch(
        "custom_components.co2saver.repairs.async_complete_repair",
        side_effect=uncertain_completion,
    ):
        result = await manager.async_configure(menu["flow_id"], {"confirm_reset": True})
    assert result["errors"] == {"base": "repair_failed"}
    assert entry.runtime_data.failed
    assert not entry.runtime_data.available
    assert timers[-1].cancelled.is_set()
    assert ir.async_get(hass).async_get_issue(DOMAIN, storage_issue_id(entry.entry_id))
    pending = await manifest_store(hass, entry.data["storage_id"]).async_load()
    assert pending is not None
    assert pending.repair_pending is not completion_applied
    manager.async_abort(menu["flow_id"])
    _, fresh = await _flow(hass, entry)
    await manager.async_configure(fresh["flow_id"], {"next_step_id": "confirm"})
    result = await manager.async_configure(fresh["flow_id"], {"confirm_reset": True})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.runtime_data.state.generation == pending.active_generation
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_failed_repair_stays_unavailable_after_draining_successful_poll(  # noqa: PLR0915
    hass: HomeAssistant, timers: list[_Timer], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A verified in-flight commit cannot reopen availability after repair failure."""
    plan, sources = _plan(hass)
    entry = await _setup(hass, plan)
    await _baseline(hass, sources, timers)
    key = entry.runtime_data.store.store_key
    net_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}:net_savings"
    )
    assert net_id is not None
    async_report_storage_issue(hass, entry)
    manager, menu = await _flow(hass, entry)
    started = asyncio.Event()
    allow = asyncio.Event()
    active: asyncio.Task[None] | None = None
    original_save = Store.async_save
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

    async def fail_with_poll_in_flight(
        _repair_hass: HomeAssistant,
        _repair_entry: ConfigEntry,
        *,
        prepared: Manifest | None = None,
    ) -> None:
        """Fail completion only after the newly loaded runner began its real write."""
        nonlocal active
        assert prepared is None
        active = asyncio.create_task(timers[-1].action(_INTERVAL))
        await started.wait()
        message = "unverified completion"
        raise VerifiedAtomicStoreVerificationError(message)

    monkeypatch.setattr(Store, "async_save", blocked_save)
    _vector(hass, sources, _INTERVAL, cycles=1)
    _grid(hass, sources, _INTERVAL)
    with patch(
        "custom_components.co2saver.repairs.async_complete_repair",
        side_effect=fail_with_poll_in_flight,
    ):
        repair = asyncio.create_task(
            manager.async_configure(menu["flow_id"], {"next_step_id": "retry"})
        )
        await started.wait()
        timer = timers[-1]
        try:
            await timer.cancelled.wait()
            assert entry.runtime_data.failed
            assert not repair.done()
            assert entry.runtime_data.state.totals.direct_pv_kwh == 0
        finally:
            allow.set()
        assert active is not None
        await active
        result = await repair
    await hass.async_block_till_done()
    assert result["errors"] == {"base": "reload_failed"}
    runtime = entry.runtime_data
    assert runtime.state.totals.direct_pv_kwh == 2
    assert runtime.state == await runtime.store.async_load()
    assert runtime.failed
    assert not runtime.available
    assert runtime.status == "storage_error"
    sensor = hass.states.get(net_id)
    assert sensor is not None
    assert sensor.state == "unavailable"
    assert ir.async_get(hass).async_get_issue(DOMAIN, storage_issue_id(entry.entry_id))
    await timer.action(_INTERVAL + timedelta(minutes=1))
    assert writes == 1
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_refused_unload_never_prepares_a_replacement(
    hass: HomeAssistant, hass_storage: dict[str, Any]
) -> None:
    """An entry which cannot stop cannot switch its authoritative generation."""
    plan, _ = _plan(hass)
    entry = await _setup(hass, plan)
    async_report_storage_issue(hass, entry)
    manager, menu = await _flow(hass, entry)
    await manager.async_configure(menu["flow_id"], {"next_step_id": "confirm"})
    before = _accounting_files(hass_storage)
    with patch.object(hass.config_entries, "async_unload", return_value=False):
        result = await manager.async_configure(menu["flow_id"], {"confirm_reset": True})
    assert result["errors"] == {"base": "unload_failed"}
    assert _accounting_files(hass_storage) == before
    manager.async_abort(menu["flow_id"])
    assert await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.parametrize("data", [None, {}, {"entry_id": 1}, {"entry_id": "missing"}])
async def test_unknown_issue_binding_cannot_select_another_entry(
    hass: HomeAssistant, data: dict[str, Any] | None
) -> None:
    """Malformed issue metadata cannot address arbitrary configuration or files."""
    flow = await async_create_fix_flow(hass, "unknown", data)
    flow.hass = hass
    result = await flow.async_step_init()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "unknown_issue"


async def test_removed_entry_flow_aborts_without_a_replacement(
    hass: HomeAssistant,
) -> None:
    """An otherwise well-formed stale issue cannot recreate a removed config entry."""
    flow = await async_create_fix_flow(
        hass, storage_issue_id("missing"), {"entry_id": "missing"}
    )
    flow.hass = hass
    assert (await flow.async_step_init())["reason"] == "entry_missing"
    assert (await flow.async_step_confirm({"confirm_reset": True}))[
        "reason"
    ] == "entry_missing"
