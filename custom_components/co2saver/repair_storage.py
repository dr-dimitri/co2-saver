# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Switch an explicitly confirmed repair to one verified, fresh generation."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from homeassistant.config_entries import ConfigEntryState
from homeassistant.util import dt as dt_util

from .bootstrap import (
    PersistedRuntime,
    _async_physical_files,
    _check_entry_collision,
    generation_key,
    manifest_key,
    manifest_lock,
    manifest_store,
)
from .measurement.storage import (
    VerifiedAtomicStore,
    VerifiedAtomicStoreConflictError,
    VerifiedAtomicStoreError,
    VerifiedAtomicStorePayloadError,
    VerifiedAtomicStoreVersionError,
)
from .persistence import Manifest, ManifestCodec, storage_identifier

if TYPE_CHECKING:
    from datetime import datetime

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant


class _RepairRevisionPolicy:
    """Authorize exactly the caller's already checked replacement or switch."""

    def __init__(self, target: Manifest, before: Manifest | None) -> None:
        self.target = target
        self.before = before

    @staticmethod
    def revision(state: Manifest) -> int:
        """Return the same monotonic revision as the ordinary manifest policy."""
        return state.commit_revision

    def validate_initial(self, state: Manifest) -> None:
        """Allow only the exact entry-bound new epoch prepared for manifest loss."""
        if (
            self.before is not None
            or state != self.target
            or state.commit_revision != 1
            or state.owner_entry_id is None
            or state.previous_generations
            or state.initialized
            or not state.manifest_lost
            or state.repair_reset_at is None
            or not state.repair_pending
            or state.repair_issue_token is None
        ):
            message = "invalid confirmed manifest replacement"
            raise VerifiedAtomicStoreConflictError(message)

    def validate_transition(self, before: Manifest, after: Manifest) -> None:
        """Permit exactly one approved repair change from its checked revision."""
        if self.before is None or before != self.before or after != self.target:
            message = "manifest changed while preparing repair"
            raise VerifiedAtomicStoreConflictError(message)


def _read_manifest(path: str) -> bytes | None:
    """Snapshot exact source bytes before Home Assistant may quarantine JSON."""
    try:
        return Path(path).read_bytes()
    except FileNotFoundError:
        return None


def _check_raw_owner(
    hass: HomeAssistant, entry: ConfigEntry, raw: bytes | None
) -> None:
    """Preserve another existing owner's data even when the schema is invalid."""
    if raw is None:
        return
    try:
        envelope = json.loads(raw)
    except UnicodeDecodeError, json.JSONDecodeError:
        return
    if not isinstance(envelope, dict):
        return
    payload = envelope.get("data")
    if not isinstance(payload, dict):
        return
    owner = payload.get("owner_entry_id")
    if (
        isinstance(owner, str)
        and owner != entry.entry_id
        and hass.config_entries.async_get_entry(owner) is not None
    ):
        message = "manifest references another existing owner"
        raise VerifiedAtomicStoreConflictError(message)


def _backup_manifest(path: str, raw: bytes, reset_at: datetime) -> None:
    """Preserve and verify byte-exact source data under an exclusive backup name."""
    timestamp = reset_at.strftime("%Y%m%dT%H%M%S.%fZ")
    while True:
        backup = Path(f"{path}.repair-backup.{timestamp}.{uuid4().hex}")
        try:
            with backup.open("xb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            continue
        if backup.read_bytes() != raw:
            message = "repair backup read-back differs from the original bytes"
            raise VerifiedAtomicStoreError(message)
        return


async def _new_generation(
    hass: HomeAssistant, storage_id: str, current: Manifest | None
) -> str:
    """Reject every physical collision, including orphaned and corrupt files."""
    known = (
        set()
        if current is None
        else {current.active_generation, *current.previous_generations}
    )
    while True:
        generation = uuid4().hex
        if generation not in known and not await _async_physical_files(
            hass, generation_key(storage_id, generation)
        ):
            return generation


def _check_loaded_owner(
    hass: HomeAssistant, entry: ConfigEntry, current: Manifest
) -> None:
    """Apply owner collision checks also to a valid loaded Store payload."""
    owner = current.owner_entry_id
    if (
        owner is not None
        and owner != entry.entry_id
        and hass.config_entries.async_get_entry(owner) is not None
    ):
        message = "manifest references another existing owner"
        raise VerifiedAtomicStoreConflictError(message)


def _resume_prepared(current: Manifest | None, prepared: Manifest) -> Manifest:
    """Resume only the exact repair identity even if setup already initialized it."""
    if current is None or (
        current.storage_id,
        current.manifest_epoch,
        current.owner_entry_id,
        current.active_generation,
        current.repair_reset_at,
    ) != (
        prepared.storage_id,
        prepared.manifest_epoch,
        prepared.owner_entry_id,
        prepared.active_generation,
        prepared.repair_reset_at,
    ):
        message = "prepared repair no longer matches the authoritative manifest"
        raise VerifiedAtomicStoreConflictError(message)
    return current


async def _resume_pending_repair(
    hass: HomeAssistant,
    entry: ConfigEntry,
    current: Manifest,
    issue_token: str | None,
) -> Manifest:
    """Keep the generation while binding a reconstructed issue to pending repair."""
    if (
        not current.repair_pending
        or issue_token is None
        or current.repair_issue_token == issue_token
    ):
        return current
    target = replace(
        current,
        repair_issue_token=issue_token,
        commit_revision=current.commit_revision + 1,
    )
    _check_entry_collision(hass, entry, current.storage_id)
    adapter = VerifiedAtomicStore(
        hass,
        manifest_key(current.storage_id),
        codec=ManifestCodec(current.storage_id),
        revision_policy=_RepairRevisionPolicy(target, current),
    )
    return await adapter.async_transact(lambda _: target)


async def _preserve_unreadable_manifest(
    hass: HomeAssistant, entry: ConfigEntry, raw: bytes | None, target: Manifest
) -> None:
    """Check remaining owner evidence, then preserve bytes before replacement."""
    key = manifest_key(target.storage_id)
    path = hass.config.path(".storage", key)
    # Existing HA quarantine files remain untouched, but a recoverable owner
    # mention in them still prevents replacing another entry.
    names = await _async_physical_files(hass, f"{key}.corrupt")
    for name in names:
        retained = await hass.async_add_executor_job(
            _read_manifest, hass.config.path(".storage", name)
        )
        _check_raw_owner(hass, entry, retained)
    if raw is not None:
        await hass.async_add_executor_job(
            _backup_manifest, path, raw, cast("datetime", target.repair_reset_at)
        )
    latest_raw = await hass.async_add_executor_job(_read_manifest, path)
    _check_raw_owner(hass, entry, latest_raw)
    if latest_raw is not None and latest_raw != raw:
        message = "manifest bytes changed after the repair backup"
        raise VerifiedAtomicStoreConflictError(message)


async def async_prepare_repair(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    prepared: Manifest | None = None,
    issue_token: str | None = None,
) -> Manifest:
    """Prepare a confirmed, unloaded entry for repair; preserve all old generations.

    The flow keeps the returned manifest and supplies it again after a failed
    reload. The persisted pending marker also resumes repairs across dialogs and
    restarts, including when setup initialized the generation before reload failed.
    A stable issue token also resumes an uncertain completion write that cleared
    pending before its verification failed and left the same repair issue open.
    """
    storage_id = storage_identifier(entry.data.get("storage_id"))
    if issue_token is not None:
        storage_identifier(issue_token)
    async with manifest_lock(hass):
        _check_entry_collision(hass, entry, storage_id)
        if entry.state in (
            ConfigEntryState.LOADED,
            ConfigEntryState.SETUP_IN_PROGRESS,
            ConfigEntryState.UNLOAD_IN_PROGRESS,
        ):
            message = "repair requires a fully unloaded config entry"
            raise VerifiedAtomicStoreConflictError(message)
        path = hass.config.path(".storage", manifest_key(storage_id))
        raw = await hass.async_add_executor_job(_read_manifest, path)
        _check_raw_owner(hass, entry, raw)
        try:
            current = await manifest_store(
                hass, storage_id, owner_entry_id=entry.entry_id
            ).async_load()
        except (
            ValueError,
            VerifiedAtomicStoreVersionError,
            VerifiedAtomicStorePayloadError,
        ):
            current = None
        if current is not None:
            _check_loaded_owner(hass, entry, current)
        if prepared is not None:
            return await _resume_pending_repair(
                hass, entry, _resume_prepared(current, prepared), issue_token
            )
        if (
            current is not None
            and current.owner_entry_id == entry.entry_id
            and (
                current.repair_pending
                or (
                    issue_token is not None
                    and current.repair_issue_token == issue_token
                )
            )
        ):
            return await _resume_pending_repair(hass, entry, current, issue_token)
        reset_at = dt_util.utcnow()
        generation = await _new_generation(hass, storage_id, current)
        if current is None:
            target = Manifest(
                storage_id=storage_id,
                manifest_epoch=uuid4().hex,
                owner_entry_id=entry.entry_id,
                active_generation=generation,
                repair_reset_at=reset_at,
                manifest_lost=True,
                repair_pending=True,
                repair_issue_token=issue_token or uuid4().hex,
            )
            await _preserve_unreadable_manifest(hass, entry, raw, target)
        else:
            target = replace(
                current,
                owner_entry_id=entry.entry_id,
                active_generation=generation,
                previous_generations=(
                    *current.previous_generations,
                    current.active_generation,
                ),
                initialized=False,
                commit_revision=current.commit_revision + 1,
                repair_reset_at=reset_at,
                repair_pending=True,
                repair_issue_token=issue_token or uuid4().hex,
            )
        _check_entry_collision(hass, entry, storage_id)
        if current is not None:
            _check_loaded_owner(hass, entry, current)
        adapter = VerifiedAtomicStore(
            hass,
            manifest_key(storage_id),
            codec=ManifestCodec(storage_id),
            revision_policy=_RepairRevisionPolicy(target, current),
        )
        if current is None:
            return await adapter.async_replace_confirmed_unreadable(target)
        return await adapter.async_transact(lambda _: target)


def _require_loaded_repair(entry: ConfigEntry, manifest: Manifest) -> None:
    """Require the successfully loaded runtime to prove this repair's identity."""
    runtime = getattr(entry, "runtime_data", None)
    if (
        entry.state is not ConfigEntryState.LOADED
        or not manifest.initialized
        or manifest.owner_entry_id != entry.entry_id
        or not isinstance(runtime, PersistedRuntime)
        or getattr(runtime, "failed", False)
        or runtime.state.storage_id != manifest.storage_id
        or runtime.state.owner_entry_id != entry.entry_id
        or runtime.state.generation != manifest.active_generation
        or runtime.state.repair_reset_at != manifest.repair_reset_at
    ):
        message = "repair completion requires its initialized and loaded generation"
        raise VerifiedAtomicStoreConflictError(message)


async def async_complete_repair(
    hass: HomeAssistant,
    entry: ConfigEntry,
    *,
    prepared: Manifest | None = None,
) -> Manifest:
    """Clear pending only after this repaired generation loaded; retries are no-ops."""
    storage_id = storage_identifier(entry.data.get("storage_id"))
    async with manifest_lock(hass):
        _check_entry_collision(hass, entry, storage_id)
        current = await manifest_store(
            hass, storage_id, owner_entry_id=entry.entry_id
        ).async_load()
        if current is None:
            message = "authoritative repair manifest is missing"
            raise VerifiedAtomicStoreConflictError(message)
        if prepared is not None:
            current = _resume_prepared(current, prepared)
        _require_loaded_repair(entry, current)
        if not current.repair_pending:
            return current
        target = replace(
            current, repair_pending=False, commit_revision=current.commit_revision + 1
        )
        adapter = VerifiedAtomicStore(
            hass,
            manifest_key(storage_id),
            codec=ManifestCodec(storage_id),
            revision_policy=_RepairRevisionPolicy(target, current),
        )

        def complete(state: Manifest) -> Manifest:
            _check_entry_collision(hass, entry, storage_id)
            _require_loaded_repair(entry, state)
            return target

        return await adapter.async_transact(complete)
