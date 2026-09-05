# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Reserve, bind, and restore authoritative storage without starting a runner."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from homeassistant.util import dt as dt_util
from homeassistant.util.hass_dict import HassKey

from .config_plan import (
    canonical_plan,
    consumer_ids,
    segment_fingerprint,
    source_bindings,
)
from .const import DOMAIN
from .domain import Energy, StorageLedger
from .measurement.models import MeasurementPipelineState
from .measurement.storage import VerifiedAtomicStore, VerifiedAtomicStoreError
from .migration import ManifestPayloadMigrator
from .persistence import (
    CumulativeTotals,
    GenerationCodec,
    GenerationRevisionPolicy,
    GenerationState,
    Manifest,
    ManifestCodec,
    ManifestRevisionPolicy,
    storage_identifier,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

_MANIFEST_LOCK: HassKey[asyncio.Lock] = HassKey("co2saver_manifest_lock")


@dataclass(slots=True)
class PersistedRuntime:
    """Verified complete state and its atomic store, with no active runner."""

    store: VerifiedAtomicStore[GenerationState]
    state: GenerationState


def manifest_lock(hass: HomeAssistant) -> asyncio.Lock:
    """Return the integration-wide lock shared by config commits and setup."""
    return hass.data.setdefault(_MANIFEST_LOCK, asyncio.Lock())


def manifest_key(storage_id: str) -> str:
    """Return the path-safe authoritative manifest key."""
    return f"{DOMAIN}.{storage_identifier(storage_id)}.manifest"


def generation_key(storage_id: str, generation: str) -> str:
    """Return the sole physical key selected by a validated manifest."""
    return f"{DOMAIN}.{storage_identifier(storage_id)}.{storage_identifier(generation)}"


def manifest_store(
    hass: HomeAssistant, storage_id: str, *, owner_entry_id: str | None = None
) -> VerifiedAtomicStore[Manifest]:
    """Build a strict atomic store for the expected manifest locator."""
    return VerifiedAtomicStore(
        hass,
        manifest_key(storage_id),
        codec=ManifestCodec(storage_id),
        revision_policy=ManifestRevisionPolicy(),
        payload_migrator=ManifestPayloadMigrator(storage_id, owner_entry_id),
    )


def generation_store(
    hass: HomeAssistant, manifest: Manifest, entry_id: str
) -> VerifiedAtomicStore[GenerationState]:
    """Build a strict atomic store bound to the authoritative generation."""
    return VerifiedAtomicStore(
        hass,
        generation_key(manifest.storage_id, manifest.active_generation),
        codec=GenerationCodec(
            manifest.storage_id, entry_id, manifest.active_generation
        ),
        revision_policy=GenerationRevisionPolicy(),
    )


def _physical_files(directory: str, prefix: str) -> tuple[str, ...]:
    """Snapshot physical names without reading, renaming, or replacing bytes."""
    root = Path(directory)
    try:
        return tuple(
            path.name for path in root.iterdir() if path.name.startswith(prefix)
        )
    except FileNotFoundError:
        return ()


async def _async_physical_files(hass: HomeAssistant, prefix: str) -> tuple[str, ...]:
    """Run the required physical collision check outside the event loop."""
    return await hass.async_add_executor_job(
        _physical_files, hass.config.path(".storage"), prefix
    )


async def async_reserve_bootstrap(hass: HomeAssistant) -> str:
    """Reserve and verify one unused locator while the caller holds its lock."""
    if not manifest_lock(hass).locked():
        message = "bootstrap reservation requires the manifest lock"
        raise RuntimeError(message)
    existing = {
        entry.data.get("storage_id")
        for entry in hass.config_entries.async_entries(DOMAIN)
    }
    while True:
        storage_id = uuid4().hex
        if storage_id in existing or await _async_physical_files(
            hass, f"{DOMAIN}.{storage_id}."
        ):
            continue
        manifest = Manifest(
            storage_id=storage_id,
            manifest_epoch=uuid4().hex,
            owner_entry_id=None,
            active_generation=uuid4().hex,
        )
        # The physical existence proof above and the caller's lock jointly make
        # this create-if-absent; an orphan remains occupied if create later fails.
        await manifest_store(hass, storage_id).async_initialize_confirmed_absent(
            manifest
        )
        return storage_id


def _quarantined_ledger(plan: Mapping[str, object]) -> StorageLedger | None:
    """Apply the accepted completely unknown initial storage envelope."""
    battery = plan.get("battery")
    if battery is None:
        return None
    parameters = cast("Mapping[str, str]", battery)
    return StorageLedger.quarantined(
        Energy(Fraction(parameters["usable_capacity_kwh"]))
    )


def _initial_generation(
    entry: ConfigEntry, manifest: Manifest, plan: Mapping[str, object]
) -> GenerationState:
    """Create the full zero state only after physical absence was established."""
    diagnostics = {
        "discarded_intervals": 0,
        "missing_grid_intensity": 0,
        "segment_transitions": 0,
    }
    if manifest.manifest_lost:
        diagnostics["manifest_losses"] = 1
    return GenerationState(
        storage_id=manifest.storage_id,
        owner_entry_id=entry.entry_id,
        generation=manifest.active_generation,
        commit_revision=1,
        segment_fingerprint=segment_fingerprint(entry.data),
        measurement=MeasurementPipelineState.initial(
            source_bindings(entry.data), dt_util.utcnow()
        ),
        ledger=_quarantined_ledger(plan),
        totals=CumulativeTotals(),
        consumer_totals=tuple(
            (consumer_id, CumulativeTotals())
            for consumer_id in consumer_ids(entry.data)
        ),
        diagnostics=tuple(sorted(diagnostics.items())),
        repair_reset_at=manifest.repair_reset_at,
    )


def _transition_segment(
    state: GenerationState,
    data: Mapping[str, object],
    plan: Mapping[str, object],
    transition_at: datetime,
) -> GenerationState:
    """Preserve historical totals and drop all unprovable boundary provenance."""
    totals = dict(state.consumer_totals)
    for consumer_id in consumer_ids(data):
        totals.setdefault(consumer_id, CumulativeTotals())
    diagnostics = dict(state.diagnostics)
    diagnostics["segment_transitions"] = diagnostics.get("segment_transitions", 0) + 1
    measurement = MeasurementPipelineState.initial(source_bindings(data), transition_at)
    return replace(
        state,
        commit_revision=state.commit_revision + 1,
        segment_fingerprint=segment_fingerprint(data),
        measurement=replace(measurement, revision=state.measurement.revision + 1),
        ledger=_quarantined_ledger(plan),
        consumer_totals=tuple(sorted(totals.items())),
        diagnostics=tuple(sorted(diagnostics.items())),
    )


def _check_entry_collision(
    hass: HomeAssistant, entry: ConfigEntry, storage_id: str
) -> None:
    """Refuse duplicate locators before the first possible Store mutation."""
    if any(
        other.entry_id != entry.entry_id and other.data.get("storage_id") == storage_id
        for other in hass.config_entries.async_entries(DOMAIN)
    ):
        message = "another config entry references this storage_id"
        raise VerifiedAtomicStoreError(message)


async def _load_manifest(
    hass: HomeAssistant, entry: ConfigEntry, storage_id: str
) -> tuple[VerifiedAtomicStore[Manifest], Manifest]:
    """Require an existing valid manifest, then bind its collision-free owner."""
    adapter = manifest_store(hass, storage_id, owner_entry_id=entry.entry_id)
    names = await _async_physical_files(hass, adapter.store_key)
    if any(name.startswith(f"{adapter.store_key}.corrupt") for name in names):
        # Retained corruption is superseded only by a canonical manifest with
        # evidence of confirmed repair. A legacy migration cannot grant that
        # authority and must not write while the corruption marker is active.
        manifest = None
        if adapter.store_key in names:
            manifest = await VerifiedAtomicStore(
                hass,
                adapter.store_key,
                codec=ManifestCodec(storage_id),
                revision_policy=ManifestRevisionPolicy(),
            ).async_load()
        if (
            manifest is None
            or manifest.repair_reset_at is None
            or not (manifest.manifest_lost or manifest.previous_generations)
        ):
            message = "manifest has a corrupt-file marker"
            raise VerifiedAtomicStoreError(message)
    else:
        manifest = await adapter.async_load()
    if manifest is None:
        message = "authoritative manifest is missing or corrupt"
        raise VerifiedAtomicStoreError(message)
    if manifest.owner_entry_id not in (None, entry.entry_id):
        message = "manifest is bound to another owner"
        raise VerifiedAtomicStoreError(message)
    if manifest.owner_entry_id is None:
        manifest = await adapter.async_transact(
            lambda current: replace(
                current,
                owner_entry_id=entry.entry_id,
                commit_revision=current.commit_revision + 1,
            )
        )
    return adapter, manifest


async def _load_generation(
    hass: HomeAssistant,
    entry: ConfigEntry,
    manifest: Manifest,
    plan: Mapping[str, object],
) -> tuple[VerifiedAtomicStore[GenerationState], GenerationState]:
    """Distinguish proven first start from every form of lost generation."""
    adapter = generation_store(hass, manifest, entry.entry_id)
    names = await _async_physical_files(hass, adapter.store_key)
    had_main = adapter.store_key in names
    if any(name.startswith(f"{adapter.store_key}.corrupt") for name in names):
        message = "active generation has a corrupt-file marker"
        raise VerifiedAtomicStoreError(message)
    state = await adapter.async_load()
    if state is None:
        if manifest.initialized or had_main:
            message = "active generation is missing or corrupt"
            raise VerifiedAtomicStoreError(message)
        state = await adapter.async_initialize_confirmed_absent(
            _initial_generation(entry, manifest, plan)
        )
    if state.repair_reset_at != manifest.repair_reset_at or dict(state.diagnostics).get(
        "manifest_losses", 0
    ) != int(manifest.manifest_lost):
        message = "generation repair metadata does not match its manifest"
        raise VerifiedAtomicStoreError(message)
    return adapter, state


async def async_setup_storage(
    hass: HomeAssistant, entry: ConfigEntry
) -> PersistedRuntime:
    """Bind, restore and segment complete state before any listeners exist."""
    storage_id = storage_identifier(entry.data.get("storage_id"))
    async with manifest_lock(hass):
        plan = canonical_plan(entry.data)
        _check_entry_collision(hass, entry, storage_id)
        manifest_adapter, manifest = await _load_manifest(hass, entry, storage_id)
        adapter, state = await _load_generation(hass, entry, manifest, plan)
        fingerprint_matches = state.segment_fingerprint == segment_fingerprint(
            entry.data
        )
        if fingerprint_matches:
            _validate_current_segment(state, entry.data, plan)
        if not manifest.initialized:
            await manifest_adapter.async_transact(
                lambda current: replace(
                    current,
                    initialized=True,
                    commit_revision=current.commit_revision + 1,
                )
            )
        if not fingerprint_matches:
            transition_at = dt_util.utcnow()
            state = await adapter.async_transact(
                lambda current: _transition_segment(
                    current, entry.data, plan, transition_at
                )
            )
        return PersistedRuntime(store=adapter, state=state)


def _validate_current_segment(
    state: GenerationState, data: Mapping[str, object], plan: Mapping[str, object]
) -> None:
    """Cross-check an unchanged segment against its canonical current plan."""
    expected_ledger = _quarantined_ledger(plan)
    if (
        state.measurement.sources != source_bindings(data)
        or (state.ledger is None) != (expected_ledger is None)
        or (
            state.ledger is not None
            and expected_ledger is not None
            and state.ledger.capacity != expected_ledger.capacity
        )
        or not set(consumer_ids(data)) <= dict(state.consumer_totals).keys()
    ):
        message = "generation state does not match its current segment plan"
        raise VerifiedAtomicStoreError(message)
