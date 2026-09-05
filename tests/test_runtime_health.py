# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Bounded runtime diagnostics, entry isolation and non-destructive source failures."""

from __future__ import annotations

import logging
from copy import deepcopy
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store

from custom_components.co2saver.const import DOMAIN
from custom_components.co2saver.domain import StorageLedger
from custom_components.co2saver.measurement.models import MeasurementPhase
from custom_components.co2saver.repair_issues import storage_issue_id

from .test_runtime import (
    _INTERVAL,
    _baseline,
    _energy,
    _grid,
    _plan,
    _setup,
    _tick,
    _vector,
    reads,
    runtime_environment,
    timers,
)
from .test_storage_runtime import _pv_charge, _site

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .test_runtime import _Reads, _Timer

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")
__all__ = ("reads", "runtime_environment", "timers")
_RUNTIME_LOGGER = "custom_components.co2saver.runtime"


def _logs(caplog: pytest.LogCaptureFixture, level: int) -> list[logging.LogRecord]:
    """Inspect only the integration's own runtime records, excluding HA internals."""
    return [
        record
        for record in caplog.records
        if record.name == _RUNTIME_LOGGER and record.levelno == level
    ]


async def test_repeated_unavailable_polls_warn_at_most_every_fifteen_minutes(
    hass: HomeAssistant,
    timers: list[_Timer],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Repeated observed faults stay bounded, and recovery emits one helpful message."""
    caplog.set_level(logging.INFO, logger=_RUNTIME_LOGGER)
    plan, sources = _plan(hass)
    entry = await _setup(hass, plan)
    await _baseline(hass, sources, timers)
    for minute in range(16):
        observed_at = _INTERVAL + timedelta(minutes=minute)
        _vector(hass, sources, observed_at, cycles=1)
        _energy(hass, sources["grid_import"], "unavailable", observed_at)
        _grid(hass, sources, observed_at)
        await _tick(hass, timers, observed_at)
        warnings = _logs(caplog, logging.WARNING)
        assert len(warnings) == (1 if minute < 15 else 2)
        assert entry.runtime_data.status == "source_unavailable"
        assert not entry.runtime_data.available
    assert dict(entry.runtime_data.state.diagnostics)["discarded_intervals"] == 1
    assert all("source_unavailable" in record.getMessage() for record in warnings)
    assert all(record.exc_info is None for record in warnings)

    recovery_at = _INTERVAL + timedelta(minutes=16)
    _vector(hass, sources, recovery_at, cycles=2)
    _grid(hass, sources, recovery_at)
    await _tick(hass, timers, recovery_at)
    assert entry.runtime_data.available
    assert entry.runtime_data.state.totals.direct_pv_kwh == 0
    following = recovery_at + timedelta(minutes=1)
    _vector(hass, sources, following, cycles=3)
    _grid(hass, sources, following)
    await _tick(hass, timers, following)
    assert entry.runtime_data.state.totals.direct_pv_kwh == 2
    assert len(_logs(caplog, logging.INFO)) == 1
    assert "valid again" in _logs(caplog, logging.INFO)[0].getMessage()
    assert not _logs(caplog, logging.ERROR)
    for source in sources.values():
        assert source.entity_id not in " ".join(
            record.getMessage() for record in (*warnings, *_logs(caplog, logging.INFO))
        )


async def test_fatal_store_failure_reports_once_stops_reads_and_redacts_error_detail(
    hass: HomeAssistant,
    timers: list[_Timer],
    reads: _Reads,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Only an error type escapes a failed commit, without repeated repair or reads."""
    plan, sources = _plan(hass)
    entry = await _setup(hass, plan)
    await _baseline(hass, sources, timers)
    previous = entry.runtime_data.state
    key = entry.runtime_data.store.store_key
    original_save = Store.async_save
    sensitive = "private_device=sensor.home_address token=secret_meter_payload"

    async def fail_selected_store(
        store: Store[dict[str, object]], payload: dict[str, object]
    ) -> None:
        if store.key == key:
            raise OSError(sensitive)
        await original_save(store, payload)

    _vector(hass, sources, _INTERVAL, cycles=1)
    _grid(hass, sources, _INTERVAL)
    with (
        patch.object(Store, "async_save", fail_selected_store),
        patch.object(ir, "async_create_issue", wraps=ir.async_create_issue) as create,
    ):
        await _tick(hass, timers, _INTERVAL)
        assert entry.runtime_data.failed
        assert entry.runtime_data.status == "storage_error"
        assert entry.runtime_data.state is previous
        assert not entry.runtime_data.available
        assert timers[-1].cancelled.is_set()
        for minute in range(1, 4):
            await _tick(hass, timers, _INTERVAL + timedelta(minutes=minute))
        matching = [
            call
            for call in create.call_args_list
            if call.args[1:3] == (DOMAIN, storage_issue_id(entry.entry_id))
        ]
        assert len(matching) == 1
    assert reads.energy == reads.grid == 2
    issue = ir.async_get(hass).async_get_issue(DOMAIN, storage_issue_id(entry.entry_id))
    assert issue is not None
    assert issue.is_fixable
    assert issue.is_persistent
    assert issue.severity is ir.IssueSeverity.ERROR
    errors = _logs(caplog, logging.ERROR)
    assert len(errors) == 1
    assert "OSError" in errors[0].getMessage()
    assert errors[0].exc_info is None
    assert sensitive not in caplog.text
    assert "secret_meter_payload" not in caplog.text
    assert await entry.runtime_data.store.async_load() == previous


@pytest.mark.parametrize("change", ["removed", "disabled"])
async def test_registry_source_failure_creates_nonfixable_issue_before_store_mutation(
    hass: HomeAssistant,
    hass_storage: dict[str, object],
    timers: list[_Timer],
    reads: _Reads,
    change: str,
) -> None:
    """Lost source bindings fail setup while preserving the existing generation."""
    plan, sources = _plan(hass)
    entry = await _setup(hass, plan)
    await _baseline(hass, sources, timers)
    storage_prefix = f"{DOMAIN}.{entry.data['storage_id']}."
    before = {
        key: deepcopy(value)
        for key, value in hass_storage.items()
        if key.startswith(storage_prefix)
    }
    original_save = Store.async_save

    async def forbid_accounting_mutation(
        store: Store[dict[str, object]], payload: dict[str, object]
    ) -> None:
        assert not store.key.startswith(storage_prefix)
        await original_save(store, payload)

    registry = er.async_get(hass)
    source = sources["pv_generation"]
    with patch.object(Store, "async_save", forbid_accounting_mutation):
        if change == "removed":
            registry.async_remove(source.entity_id)
        else:
            registry.async_update_entity(
                source.entity_id, disabled_by=er.RegistryEntryDisabler.USER
            )
            assert not await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert timers[-1].cancelled.is_set()
    assert reads.energy == reads.grid == 1
    assert {
        key: value
        for key, value in hass_storage.items()
        if key.startswith(storage_prefix)
    } == before
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"sources_changed:{entry.entry_id}"
    )
    assert issue is not None
    assert not issue.is_fixable
    assert issue.severity is ir.IssueSeverity.ERROR
    assert (
        ir.async_get(hass).async_get_issue(DOMAIN, storage_issue_id(entry.entry_id))
        is None
    )
    if change == "disabled":
        registry.async_update_entity(source.entity_id, disabled_by=None)
        assert await hass.config_entries.async_reload(entry.entry_id)
        assert (
            ir.async_get(hass).async_get_issue(
                DOMAIN, f"sources_changed:{entry.entry_id}"
            )
            is None
        )


async def test_disabled_energy_source_is_rejected_at_next_poll_and_quarantines_storage(
    hass: HomeAssistant, timers: list[_Timer], reads: _Reads
) -> None:
    """A lingering numeric state cannot keep a disabled registry binding eligible."""
    site = await _site(hass, timers)
    charged = await _pv_charge(site)
    assert charged.ledger is not None
    assert charged.ledger.pv_lower.kwh > 0
    registry = er.async_get(hass)
    source = site.sources["pv_generation"]
    before_reads = reads.energy
    registry.async_update_entity(
        source.entity_id, disabled_by=er.RegistryEntryDisabler.USER
    )
    await hass.async_block_till_done()
    assert reads.energy == reads.grid == before_reads
    assert hass.states.get(source.entity_id) is not None
    rejected = await site.step({"load": 2, "discharge": 2})
    assert site.entry.runtime_data.status == "source_binding_mismatch"
    assert not site.entry.runtime_data.available
    assert rejected.totals == charged.totals
    assert rejected.measurement.phase is MeasurementPhase.AWAITING_REBASELINE
    assert rejected.ledger == StorageLedger.quarantined(charged.ledger.capacity)
    assert dict(rejected.diagnostics)["discarded_intervals"] == 1
    repeated = await site.step({})
    assert repeated == rejected
    registry.async_update_entity(source.entity_id, disabled_by=None)
    await hass.async_block_till_done()
    assert reads.energy == reads.grid == before_reads + 2
    recovery = await site.step({})
    assert site.entry.runtime_data.available
    assert recovery.measurement.phase is MeasurementPhase.ACTIVE
    assert recovery.totals == charged.totals
    assert recovery.ledger == rejected.ledger
    following = await site.step({"pv_generation": 1, "load": 2, "discharge": 1})
    assert following.totals.direct_pv_kwh == charged.totals.direct_pv_kwh + 1
    assert following.totals.storage_pv_kwh == charged.totals.storage_pv_kwh
    assert dict(following.diagnostics)["discarded_intervals"] == 1


def _independent_plan(
    hass: HomeAssistant,
) -> tuple[dict[str, Any], dict[str, er.RegistryEntry]]:
    """Give a second plant distinct physical source bindings and its own plant key."""
    plan, original_sources = _plan(hass)
    registry = er.async_get(hass)
    sources = {
        role: registry.async_get_or_create(
            "sensor",
            "runtime_test",
            f"second_{role}",
            suggested_object_id=f"second_{role}",
        )
        for role in original_sources
    }
    plan["sources"] = {role: sources[role].id for role in plan["sources"]}
    pair = sorted((sources["grid_import"].id, sources["grid_export"].id))
    plan["plant_key"] = f"grid:{pair[0]}:{pair[1]}"
    plan["consumption"]["household_source"] = sources["load"].id
    plan["factors"]["grid_intensity_source"] = sources["grid_co2"].id
    return plan, sources


async def test_one_fatal_entry_does_not_stop_an_independent_plant(
    hass: HomeAssistant, timers: list[_Timer], reads: _Reads
) -> None:
    """Different locators and sources keep healthy commits and repair scope isolated."""
    first_plan, first_sources = _plan(hass)
    first = await _setup(hass, first_plan)
    first_timer = timers[-1]
    second_plan, second_sources = _independent_plan(hass)
    second = await _setup(hass, second_plan)
    second_timer = timers[-1]
    assert first.data["storage_id"] != second.data["storage_id"]
    assert not set(first_plan["sources"].values()) & set(
        second_plan["sources"].values()
    )
    await _baseline(hass, first_sources, [first_timer])
    await _baseline(hass, second_sources, [second_timer])
    first_previous = first.runtime_data.state
    original_save = Store.async_save

    async def fail_first_plant(
        store: Store[dict[str, object]], payload: dict[str, object]
    ) -> None:
        if store.key == first.runtime_data.store.store_key:
            message = "first plant disk error"
            raise OSError(message)
        await original_save(store, payload)

    with patch.object(Store, "async_save", fail_first_plant):
        for cycle in (1, 2):
            when = _INTERVAL + timedelta(minutes=cycle - 1)
            for sources in (first_sources, second_sources):
                _vector(hass, sources, when, cycles=cycle)
                _grid(hass, sources, when)
            await _tick(hass, [first_timer], when)
            await _tick(hass, [second_timer], when)
    assert first.runtime_data.failed
    assert first.runtime_data.state is first_previous
    assert first_timer.cancelled.is_set()
    assert not second.runtime_data.failed
    assert second.runtime_data.available
    assert not second_timer.cancelled.is_set()
    assert second.runtime_data.state.totals.direct_pv_kwh == 4
    assert second.runtime_data.state.totals.direct_net_g == 1440
    assert second.runtime_data.state == await second.runtime_data.store.async_load()
    assert reads.energy == reads.grid == 5
    registry = ir.async_get(hass)
    assert (
        registry.async_get_issue(DOMAIN, storage_issue_id(first.entry_id)) is not None
    )
    assert registry.async_get_issue(DOMAIN, storage_issue_id(second.entry_id)) is None
