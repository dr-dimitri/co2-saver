# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Confirmed repair switches preserve bytes, collision boundaries, and retries."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.co2saver import repair_storage
from custom_components.co2saver.bootstrap import (
    async_setup_storage,
    generation_key,
    generation_store,
    manifest_key,
)
from custom_components.co2saver.const import DOMAIN
from custom_components.co2saver.domain import Energy, StorageLedger
from custom_components.co2saver.measurement.models import MeasurementPhase
from custom_components.co2saver.measurement.storage import (
    VerifiedAtomicStoreConflictError,
    VerifiedAtomicStoreError,
    VerifiedAtomicStorePayloadError,
    VerifiedAtomicStoreVerificationError,
)
from custom_components.co2saver.persistence import (
    CumulativeTotals,
    GenerationCodec,
    ManifestCodec,
)
from custom_components.co2saver.repair_storage import (
    async_complete_repair,
    async_prepare_repair,
)
from custom_components.co2saver.runtime import EntryRuntime

from .test_bootstrap import _manifest, _reserved_entry, storage_directory

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

__all__ = ["storage_directory"]
_NOW = datetime(2026, 9, 5, 15, 12, 13, 456789, tzinfo=UTC)


@pytest.fixture(autouse=True)
def deterministic_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep repair and baseline timestamps observable without sleeping."""
    monkeypatch.setattr(dt_util, "utcnow", lambda: _NOW)


def _physical_manifest(
    directory: Path, key: str, hass_storage: dict[str, object]
) -> bytes:
    """Mirror one HA envelope's exact bytes for backup and existence checks."""
    raw = json.dumps(hass_storage[key], indent=3).encode() + b"\n"
    (directory / key).write_bytes(raw)
    return raw


@pytest.mark.parametrize("battery", [False, True])
async def test_valid_repair_switches_once_and_initializes_zero_generation(
    hass: HomeAssistant,
    hass_storage: dict[str, object],
    storage_directory: Path,
    *,
    battery: bool,
) -> None:
    """Only the pointer changes; old totals, provenance and physical bytes survive."""
    entry = await _reserved_entry(hass, battery=battery)
    runtime = await async_setup_storage(hass, entry)
    totals = CumulativeTotals(direct_pv_kwh=Fraction(1), direct_gross_g=Fraction(400))
    await runtime.store.async_transact(
        lambda state: replace(
            state,
            commit_revision=state.commit_revision + 1,
            totals=totals,
            consumer_totals=(
                (state.consumer_totals[0][0], totals),
                *state.consumer_totals[1:],
            ),
        )
    )
    previous = await _manifest(hass, entry)
    old_payload = deepcopy(hass_storage[runtime.store.store_key])
    old_file = storage_directory / runtime.store.store_key
    old_file.write_bytes(b"original generation backup bytes")
    prepared = await async_prepare_repair(hass, entry)
    assert prepared.commit_revision == previous.commit_revision + 1
    assert prepared.manifest_epoch == previous.manifest_epoch
    assert prepared.previous_generations == (previous.active_generation,)
    assert prepared.active_generation != previous.active_generation
    assert not prepared.initialized
    assert prepared.repair_pending
    assert not prepared.manifest_lost
    assert prepared.repair_reset_at == _NOW
    assert hass_storage[runtime.store.store_key] == old_payload
    assert old_file.read_bytes() == b"original generation backup bytes"
    restored = await async_setup_storage(hass, entry)
    assert restored.state.generation == prepared.active_generation
    assert restored.state.totals == CumulativeTotals()
    assert all(
        total == CumulativeTotals() for _, total in restored.state.consumer_totals
    )
    assert (
        restored.state.measurement.phase is MeasurementPhase.AWAITING_SEGMENT_BASELINE
    )
    assert restored.state.measurement.segment_transition_at == _NOW
    assert restored.state.repair_reset_at == _NOW
    assert restored.state.ledger == (
        StorageLedger.quarantined(Energy(Fraction(10))) if battery else None
    )
    assert (await _manifest(hass, entry)).initialized
    assert hass_storage[runtime.store.store_key] == old_payload


@pytest.mark.parametrize("kind", ["missing", "semantic", "future", "syntax"])
async def test_lost_manifest_creates_new_epoch_and_archives_only_original_bytes(
    hass: HomeAssistant,
    hass_storage: dict[str, object],
    storage_directory: Path,
    kind: str,
) -> None:
    """No file discovery guesses ancestry; exact source bytes precede replacement."""
    entry = await _reserved_entry(hass, battery=True)
    old = await async_setup_storage(hass, entry)
    previous = await _manifest(hass, entry)
    key = manifest_key(previous.storage_id)
    raw = None
    if kind in ("semantic", "future"):
        envelope = cast("dict[str, object]", hass_storage[key])
        payload = cast("dict[str, object]", envelope["data"])
        payload["storage_id" if kind == "semantic" else "minor_version"] = (
            "f" * 32 if kind == "semantic" else 99
        )
        raw = _physical_manifest(storage_directory, key, hass_storage)
    else:
        del hass_storage[key]
        if kind == "syntax":
            raw = b"{malformed original\x00\xff\r\n"
            (storage_directory / key).write_bytes(raw)
    old_payload = deepcopy(hass_storage[old.store.store_key])
    orphan = storage_directory / generation_key(previous.storage_id, "d" * 32)
    orphan.write_bytes(b"unattributed old generation")
    marker = storage_directory / f"{key}.corrupt.2026-01-01"
    marker.write_bytes(b"older corrupt data")
    prepared = await async_prepare_repair(hass, entry)
    assert prepared.manifest_epoch != previous.manifest_epoch
    assert prepared.active_generation not in (previous.active_generation, "d" * 32)
    assert prepared.previous_generations == ()
    assert prepared.commit_revision == 1
    assert prepared.owner_entry_id == entry.entry_id
    assert prepared.manifest_lost
    assert prepared.repair_pending
    assert prepared.repair_reset_at == _NOW
    backups = await hass.async_add_executor_job(
        lambda: tuple(storage_directory.glob(f"{key}.repair-backup.*"))
    )
    assert len(backups) == int(raw is not None)
    if raw is not None:
        assert backups[0].read_bytes() == raw
        assert "20260905T151213.456789Z" in backups[0].name
    assert marker.read_bytes() == b"older corrupt data"
    assert orphan.read_bytes() == b"unattributed old generation"
    assert hass_storage[old.store.store_key] == old_payload
    # The in-memory Store fixture does not create physical files itself.
    _physical_manifest(storage_directory, key, hass_storage)
    current = await async_setup_storage(hass, entry)
    assert dict(current.state.diagnostics)["manifest_losses"] == 1
    assert current.state.repair_reset_at == _NOW
    assert current.state.totals == CumulativeTotals()
    assert marker.read_bytes() == b"older corrupt data"
    assert hass_storage[old.store.store_key] == old_payload


@pytest.mark.parametrize("source", ["entry_locator", "valid_owner", "invalid_owner"])
async def test_collisions_preserve_every_file_and_manifest(
    hass: HomeAssistant,
    hass_storage: dict[str, object],
    storage_directory: Path,
    source: str,
) -> None:
    """Confirmation never permits replacing another existing entry's state."""
    entry = await _reserved_entry(hass)
    current = await _manifest(hass, entry)
    key = manifest_key(current.storage_id)
    other = MockConfigEntry(
        domain=DOMAIN,
        data={
            "storage_id": current.storage_id if source == "entry_locator" else "e" * 32
        },
    )
    other.add_to_hass(hass)
    if source != "entry_locator":
        payload = ManifestCodec.encode(replace(current, owner_entry_id=other.entry_id))
        if source == "invalid_owner":
            payload["minor_version"] = 99
        await Store(hass, 1, key, minor_version=1).async_save(payload)
    raw = _physical_manifest(storage_directory, key, hass_storage)
    before = deepcopy(hass_storage)
    with pytest.raises(
        VerifiedAtomicStoreError, match=r"another.*(?:owner|storage_id)"
    ):
        await async_prepare_repair(hass, entry)
    assert hass_storage == before
    assert (storage_directory / key).read_bytes() == raw
    assert not await hass.async_add_executor_job(
        lambda: tuple(storage_directory.glob("*.repair-backup.*"))
    )


async def test_generation_uuid_retries_active_previous_orphan_and_corrupt_names(
    hass: HomeAssistant,
    hass_storage: dict[str, object],
    storage_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unused bytes are enough to reserve a generation ID without loading them."""
    entry = await _reserved_entry(hass)
    current = await _manifest(hass, entry)
    current = replace(current, previous_generations=("a" * 32,))
    await Store(hass, 1, manifest_key(current.storage_id), minor_version=1).async_save(
        ManifestCodec.encode(current)
    )
    paths = [
        storage_directory / generation_key(current.storage_id, "b" * 32),
        storage_directory / f"{generation_key(current.storage_id, 'c' * 32)}.corrupt.1",
    ]
    for path in paths:
        path.write_bytes(b"occupied bytes")
    before = deepcopy(hass_storage)
    choices = iter((current.active_generation, "a" * 32, "b" * 32, "c" * 32, "d" * 32))
    monkeypatch.setattr(
        repair_storage, "uuid4", lambda: SimpleNamespace(hex=next(choices))
    )
    prepared = await async_prepare_repair(hass, entry, issue_token="e" * 32)
    assert prepared.active_generation == "d" * 32
    assert prepared.previous_generations == ("a" * 32, current.active_generation)
    assert all(path.read_bytes() == b"occupied bytes" for path in paths)
    assert set(hass_storage) == set(before)


async def test_prepared_token_resumes_after_initialized_reload_failure(
    hass: HomeAssistant,
    hass_storage: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated flow confirmation uses the same generation after either crash point."""
    entry = await _reserved_entry(hass)
    await async_setup_storage(hass, entry)
    prepared = await async_prepare_repair(hass, entry)
    before = deepcopy(hass_storage)
    assert await async_prepare_repair(hass, entry) == prepared
    assert await async_prepare_repair(hass, entry, prepared=prepared) == prepared
    assert hass_storage == before
    restored = await async_setup_storage(hass, entry)
    before = deepcopy(hass_storage)
    monkeypatch.setattr(dt_util, "utcnow", lambda: _NOW + timedelta(hours=1))
    resumed = await async_prepare_repair(hass, entry, prepared=prepared)
    assert resumed.initialized
    assert resumed.active_generation == prepared.active_generation
    assert resumed.repair_reset_at == _NOW
    assert resumed.repair_pending
    assert await async_prepare_repair(hass, entry) == resumed
    assert (await async_setup_storage(hass, entry)).state == restored.state
    assert hass_storage == before
    # Only verified completion enables a subsequent explicitly requested cycle.
    entry.runtime_data = restored
    entry.mock_state(hass, ConfigEntryState.LOADED)
    completed = await async_complete_repair(hass, entry, prepared=prepared)
    assert not completed.repair_pending
    entry.mock_state(hass, ConfigEntryState.NOT_LOADED)
    later = await async_prepare_repair(hass, entry)
    assert later.active_generation != prepared.active_generation
    assert later.repair_reset_at == _NOW + timedelta(hours=1)
    with pytest.raises(VerifiedAtomicStoreConflictError, match="no longer matches"):
        await async_prepare_repair(hass, entry, prepared=prepared)


@pytest.mark.parametrize("phase", ["generation_save", "initialized_save"])
async def test_repair_setup_failure_preserves_pointer_and_resumes_only_new_generation(
    hass: HomeAssistant,
    hass_storage: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    """A setup crash cannot restore the old pointer or erase a saved new baseline."""
    entry = await _reserved_entry(hass, battery=True)
    old = await async_setup_storage(hass, entry)
    previous = deepcopy(hass_storage[old.store.store_key])
    prepared = await async_prepare_repair(hass, entry)
    original_save = Store.async_save

    async def fail(store: Store[dict[str, object]], data: dict[str, object]) -> None:
        if (phase == "generation_save" and not store.key.endswith(".manifest")) or (
            phase == "initialized_save" and data.get("initialized") is True
        ):
            message = "simulated setup crash"
            raise OSError(message)
        await original_save(store, data)

    with monkeypatch.context() as patch:
        patch.setattr(Store, "async_save", fail)
        with pytest.raises(OSError, match="simulated setup crash"):
            await async_setup_storage(hass, entry)
    pointer = await _manifest(hass, entry)
    assert pointer == prepared
    assert hass_storage[old.store.store_key] == previous
    saved = await generation_store(hass, prepared, entry.entry_id).async_load()
    monkeypatch.setattr(dt_util, "utcnow", lambda: _NOW + timedelta(minutes=10))
    restored = await async_setup_storage(hass, entry)
    assert restored.state.generation == prepared.active_generation
    assert restored.state.repair_reset_at == _NOW
    if saved is not None:
        assert restored.state == saved
    assert hass_storage[old.store.store_key] == previous


@pytest.mark.parametrize("failure", ["raise", "swallow", "readback"])
async def test_pointer_write_failures_publish_nothing_and_retry_is_idempotent(
    hass: HomeAssistant,
    hass_storage: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """An uncertain successful save is resumed, while unsaved pointers stay old."""
    entry = await _reserved_entry(hass)
    await async_setup_storage(hass, entry)
    previous = await _manifest(hass, entry)
    original_save = Store.async_save
    original_load = Store.async_load
    has_saved = False

    async def fail_save(
        store: Store[dict[str, object]], data: dict[str, object]
    ) -> None:
        nonlocal has_saved
        if failure == "raise":
            message = "simulated write failure"
            raise OSError(message)
        if failure == "readback":
            await original_save(store, data)
            has_saved = True

    async def fail_load(store: Store[dict[str, object]]) -> dict[str, object] | None:
        if has_saved:
            message = "simulated readback failure"
            raise OSError(message)
        return await original_load(store)

    with monkeypatch.context() as patch:
        patch.setattr(Store, "async_save", fail_save)
        patch.setattr(Store, "async_load", fail_load)
        with pytest.raises((OSError, VerifiedAtomicStoreVerificationError)):
            await async_prepare_repair(hass, entry)
    after = await _manifest(hass, entry)
    if failure == "readback":
        before = deepcopy(hass_storage)
        assert after.active_generation != previous.active_generation
        assert await async_prepare_repair(hass, entry) == after
        assert hass_storage == before
    else:
        assert after == previous


@pytest.mark.parametrize("stage", ["read", "backup_write", "backup_readback"])
async def test_backup_faults_do_not_replace_unreadable_manifest(
    hass: HomeAssistant,
    hass_storage: dict[str, object],
    storage_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    """Existing bytes require a successful exact backup before manifest replacement."""
    entry = await _reserved_entry(hass)
    current = await _manifest(hass, entry)
    key = manifest_key(current.storage_id)
    envelope = cast("dict[str, object]", hass_storage[key])
    cast("dict[str, object]", envelope["data"])["minor_version"] = 99
    raw = _physical_manifest(storage_directory, key, hass_storage)
    before = deepcopy(hass_storage)
    original_read = Path.read_bytes
    original_open = Path.open

    def failing_read(path: Path) -> bytes:
        if (stage == "read" and path.name == key) or (
            stage == "backup_readback" and ".repair-backup." in path.name
        ):
            message = "simulated read failure"
            raise OSError(message)
        return original_read(path)

    def failing_open(path: Path, *args: object, **kwargs: object) -> object:
        if stage == "backup_write" and ".repair-backup." in path.name:
            message = "simulated backup write failure"
            raise OSError(message)
        return original_open(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", failing_read)
        patch.setattr(Path, "open", failing_open)
        with pytest.raises(OSError, match="simulated"):
            await async_prepare_repair(hass, entry)
    assert hass_storage == before
    assert (storage_directory / key).read_bytes() == raw


@pytest.mark.parametrize(
    "state",
    [
        ConfigEntryState.LOADED,
        ConfigEntryState.SETUP_IN_PROGRESS,
        ConfigEntryState.UNLOAD_IN_PROGRESS,
    ],
)
async def test_repair_requires_confirmed_unload(
    hass: HomeAssistant, hass_storage: dict[str, object], state: ConfigEntryState
) -> None:
    """A live entry cannot race the manifest switch with old-generation writes."""
    entry = await _reserved_entry(hass)
    entry.mock_state(hass, state)
    before = deepcopy(hass_storage)
    with pytest.raises(VerifiedAtomicStoreConflictError, match="fully unloaded"):
        await async_prepare_repair(hass, entry)
    assert hass_storage == before


async def test_nonrepair_manifest_cannot_override_existing_corrupt_marker(
    hass: HomeAssistant, hass_storage: dict[str, object], storage_directory: Path
) -> None:
    """Only a proven replacement epoch supersedes retained corruption evidence."""
    entry = await _reserved_entry(hass)
    current = await _manifest(hass, entry)
    key = manifest_key(current.storage_id)
    _physical_manifest(storage_directory, key, hass_storage)
    (storage_directory / f"{key}.corrupt.old").write_bytes(b"corrupt evidence")
    before = deepcopy(hass_storage)
    with pytest.raises(VerifiedAtomicStoreError, match="corrupt-file marker"):
        await async_setup_storage(hass, entry)
    assert hass_storage == before


async def test_confirmed_valid_repair_supersedes_retained_corrupt_marker(
    hass: HomeAssistant, hass_storage: dict[str, object], storage_directory: Path
) -> None:
    """An explicit valid-manifest switch also proves older corruption superseded."""
    entry = await _reserved_entry(hass)
    previous = await _manifest(hass, entry)
    key = manifest_key(previous.storage_id)
    marker = storage_directory / f"{key}.corrupt.old"
    marker.write_bytes(b"retained corruption")
    _physical_manifest(storage_directory, key, hass_storage)
    prepared = await async_prepare_repair(hass, entry)
    assert prepared.previous_generations == (previous.active_generation,)
    assert not prepared.manifest_lost
    _physical_manifest(storage_directory, key, hass_storage)
    restored = await async_setup_storage(hass, entry)
    assert restored.state.generation == prepared.active_generation
    assert restored.state.repair_reset_at == _NOW
    assert marker.read_bytes() == b"retained corruption"


@pytest.mark.parametrize("kind", ["reset_time", "loss_diagnostic"])
async def test_generation_must_agree_with_authoritative_repair_metadata(
    hass: HomeAssistant, hass_storage: dict[str, object], kind: str
) -> None:
    """A foreign repair cycle cannot hide behind an otherwise matching generation."""
    entry = await _reserved_entry(hass)
    await async_prepare_repair(hass, entry)
    current = await async_setup_storage(hass, entry)
    changed = (
        replace(current.state, repair_reset_at=None)
        if kind == "reset_time"
        else replace(
            current.state,
            diagnostics=tuple(
                sorted((*current.state.diagnostics, ("manifest_losses", 1)))
            ),
        )
    )
    await Store(hass, 1, current.store.store_key, minor_version=1).async_save(
        GenerationCodec.encode(changed)
    )
    before = deepcopy(hass_storage)
    with pytest.raises(VerifiedAtomicStoreError, match="repair metadata"):
        await async_setup_storage(hass, entry)
    assert hass_storage == before


@pytest.mark.parametrize("location", ["loaded_only", "corrupt_file"])
async def test_owner_evidence_without_main_file_still_blocks_repair(
    hass: HomeAssistant,
    hass_storage: dict[str, object],
    storage_directory: Path,
    location: str,
) -> None:
    """A cached Store or retained manifest can still prove an existing owner."""
    entry = await _reserved_entry(hass)
    current = await _manifest(hass, entry)
    other = MockConfigEntry(domain=DOMAIN, data={"storage_id": "f" * 32})
    other.add_to_hass(hass)
    key = manifest_key(current.storage_id)
    payload = ManifestCodec.encode(replace(current, owner_entry_id=other.entry_id))
    await Store(hass, 1, key, minor_version=1).async_save(payload)
    marker = storage_directory / f"{key}.corrupt.old"
    if location == "corrupt_file":
        marker.write_bytes(json.dumps(hass_storage.pop(key)).encode())
    before = deepcopy(hass_storage)
    with pytest.raises(
        VerifiedAtomicStoreConflictError, match="another existing owner"
    ):
        await async_prepare_repair(hass, entry)
    assert hass_storage == before


async def test_backup_names_are_exclusive_even_at_identical_timestamps(
    hass: HomeAssistant,
    hass_storage: dict[str, object],
    storage_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An occupied backup name is retried without touching its existing content."""
    entry = await _reserved_entry(hass)
    current = await _manifest(hass, entry)
    key = manifest_key(current.storage_id)
    envelope = cast("dict[str, object]", hass_storage[key])
    cast("dict[str, object]", envelope["data"])["minor_version"] = 99
    raw = _physical_manifest(storage_directory, key, hass_storage)
    occupied = storage_directory / (
        f"{key}.repair-backup.20260905T151213.456789Z.{'c' * 32}"
    )
    occupied.write_bytes(b"existing backup")
    choices = iter(("a" * 32, "b" * 32, "c" * 32, "d" * 32))
    monkeypatch.setattr(
        repair_storage, "uuid4", lambda: SimpleNamespace(hex=next(choices))
    )
    prepared = await async_prepare_repair(hass, entry, issue_token="e" * 32)
    assert prepared.active_generation == "a" * 32
    assert occupied.read_bytes() == b"existing backup"
    assert (
        storage_directory / f"{key}.repair-backup.20260905T151213.456789Z.{'d' * 32}"
    ).read_bytes() == raw


@pytest.mark.parametrize("failure", ["backup_mismatch", "changed_source"])
async def test_backup_or_source_mismatch_blocks_replacement(
    hass: HomeAssistant,
    hass_storage: dict[str, object],
    storage_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """Successful I/O alone does not prove that the preserved bytes are complete."""
    entry = await _reserved_entry(hass)
    current = await _manifest(hass, entry)
    key = manifest_key(current.storage_id)
    envelope = cast("dict[str, object]", hass_storage[key])
    cast("dict[str, object]", envelope["data"])["minor_version"] = 99
    raw = _physical_manifest(storage_directory, key, hass_storage)
    before = deepcopy(hass_storage)
    original_read = Path.read_bytes
    source_reads = 0

    def changed_read(path: Path) -> bytes:
        nonlocal source_reads
        if failure == "backup_mismatch" and ".repair-backup." in path.name:
            return b"incomplete backup"
        if path.name == key:
            source_reads += 1
            if failure == "changed_source" and source_reads == 2:
                return b"new unsaved source content"
        return original_read(path)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "read_bytes", changed_read)
        with pytest.raises(VerifiedAtomicStoreError, match=r"differs|bytes changed"):
            await async_prepare_repair(hass, entry)
    assert hass_storage == before
    assert (storage_directory / key).read_bytes() == raw


@pytest.mark.parametrize("raw", [b"[]", b'{"data": []}'])
async def test_unreadable_json_shapes_are_backed_up_without_guessing_owner(
    hass: HomeAssistant,
    hass_storage: dict[str, object],
    storage_directory: Path,
    raw: bytes,
) -> None:
    """Absent ownership evidence cannot supply an invented ancestor or owner."""
    entry = await _reserved_entry(hass)
    current = await _manifest(hass, entry)
    key = manifest_key(current.storage_id)
    del hass_storage[key]
    (storage_directory / key).write_bytes(raw)
    prepared = await async_prepare_repair(hass, entry)
    assert prepared.manifest_lost
    assert prepared.previous_generations == ()
    backups = await hass.async_add_executor_job(
        lambda: tuple(storage_directory.glob(f"{key}.repair-backup.*"))
    )
    assert len(backups) == 1
    assert backups[0].read_bytes() == raw


async def test_missing_prepared_manifest_does_not_authorize_another_replacement(
    hass: HomeAssistant, hass_storage: dict[str, object]
) -> None:
    """An explicit retry token never turns missing state into a fresh repair."""
    entry = await _reserved_entry(hass)
    prepared = await async_prepare_repair(hass, entry)
    del hass_storage[manifest_key(prepared.storage_id)]
    before = deepcopy(hass_storage)
    with pytest.raises(VerifiedAtomicStoreConflictError, match="no longer matches"):
        await async_prepare_repair(hass, entry, prepared=prepared)
    assert hass_storage == before


@pytest.mark.parametrize(
    "error_type",
    [VerifiedAtomicStorePayloadError, VerifiedAtomicStoreVerificationError, OSError],
)
async def test_unreadable_payload_is_distinct_from_uncertain_store_operation(
    hass: HomeAssistant,
    hass_storage: dict[str, object],
    storage_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
) -> None:
    """Only an identified damaged payload authorizes backup and replacement."""
    entry = await _reserved_entry(hass)
    current = await _manifest(hass, entry)
    key = manifest_key(current.storage_id)
    envelope = cast("dict[str, object]", hass_storage[key])
    cast("dict[str, object]", envelope["data"])["minor_version"] = 99
    raw = _physical_manifest(storage_directory, key, hass_storage)
    before = deepcopy(hass_storage)
    monkeypatch.setattr(
        repair_storage,
        "manifest_store",
        lambda *_args, **_kwargs: SimpleNamespace(
            async_load=AsyncMock(side_effect=error_type("unreadable source"))
        ),
    )
    if error_type is VerifiedAtomicStorePayloadError:
        prepared = await async_prepare_repair(hass, entry)
        assert prepared.manifest_lost
    else:
        with pytest.raises(error_type, match="unreadable source"):
            await async_prepare_repair(hass, entry)
        assert hass_storage == before
    backups = await hass.async_add_executor_job(
        lambda: tuple(storage_directory.glob(f"{key}.repair-backup.*"))
    )
    assert len(backups) == int(error_type is VerifiedAtomicStorePayloadError)
    if backups:
        assert backups[0].read_bytes() == raw


async def _loaded_entry(hass: HomeAssistant, *, repair: bool = True) -> MockConfigEntry:
    """Attach the exact verified generation and model the public LOADED boundary."""
    entry = await _reserved_entry(hass)
    if repair:
        await async_prepare_repair(hass, entry)
    loaded = await async_setup_storage(hass, entry)
    entry.runtime_data = EntryRuntime(store=loaded.store, state=loaded.state)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    return entry


async def test_completion_clears_only_pending_after_matching_loaded_generation(
    hass: HomeAssistant, hass_storage: dict[str, object]
) -> None:
    """One verified completion revision survives fresh adapters and repeated calls."""
    entry = await _loaded_entry(hass)
    before = await _manifest(hass, entry)
    generation = deepcopy(hass_storage[entry.runtime_data.store.store_key])
    assert before.repair_pending
    completed = await async_complete_repair(hass, entry, prepared=before)
    assert completed == replace(
        before, repair_pending=False, commit_revision=before.commit_revision + 1
    )
    after = deepcopy(hass_storage)
    assert await async_complete_repair(hass, entry, prepared=before) == completed
    assert await async_complete_repair(hass, entry) == completed
    assert hass_storage == after
    assert hass_storage[entry.runtime_data.store.store_key] == generation


async def test_retry_of_original_loaded_generation_is_a_write_free_completion(
    hass: HomeAssistant, hass_storage: dict[str, object]
) -> None:
    """Retrying intact original state does not invent a repair or reset timestamp."""
    entry = await _loaded_entry(hass, repair=False)
    before = deepcopy(hass_storage)
    completed = await async_complete_repair(hass, entry)
    assert completed.repair_reset_at is None
    assert not completed.repair_pending
    assert hass_storage == before


@pytest.mark.parametrize(
    "mismatch",
    [
        "entry_state",
        "runtime_missing",
        "runtime_failed",
        "storage_id",
        "owner_entry_id",
        "generation",
        "repair_reset_at",
        "manifest_owner",
        "uninitialized",
    ],
)
async def test_completion_requires_the_loaded_runtime_and_manifest_to_agree(
    hass: HomeAssistant, hass_storage: dict[str, object], mismatch: str
) -> None:
    """LOADED alone cannot clear another runtime's or an incomplete repair's marker."""
    entry = await _loaded_entry(hass)
    current = await _manifest(hass, entry)
    if mismatch == "entry_state":
        entry.mock_state(hass, ConfigEntryState.NOT_LOADED)
    elif mismatch == "runtime_missing":
        entry.runtime_data = None
    elif mismatch == "runtime_failed":
        entry.runtime_data.failed = True
    elif mismatch in ("manifest_owner", "uninitialized"):
        wrong = (
            replace(current, owner_entry_id="another owner")
            if mismatch == "manifest_owner"
            else replace(current, initialized=False)
        )
        await Store(
            hass, 1, manifest_key(current.storage_id), minor_version=1
        ).async_save(ManifestCodec.encode(wrong))
    else:
        entry.runtime_data.state = replace(
            entry.runtime_data.state,
            **{mismatch: None if mismatch == "repair_reset_at" else "f" * 32},
        )
    before = deepcopy(hass_storage)
    with pytest.raises(VerifiedAtomicStoreConflictError, match="loaded generation"):
        await async_complete_repair(hass, entry)
    assert hass_storage == before


@pytest.mark.parametrize("failure", ["raise", "swallow", "readback"])
async def test_failed_completion_can_retry_without_switching_generation(
    hass: HomeAssistant,
    hass_storage: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    """Even an uncertain completion save is retried against its original identity."""
    entry = await _loaded_entry(hass)
    prepared = await _manifest(hass, entry)
    original_save = Store.async_save
    original_load = Store.async_load
    has_saved = False

    async def failed_save(
        store: Store[dict[str, object]], data: dict[str, object]
    ) -> None:
        nonlocal has_saved
        if failure == "raise":
            message = "completion write failed"
            raise OSError(message)
        if failure == "readback":
            await original_save(store, data)
            has_saved = True

    async def failed_load(store: Store[dict[str, object]]) -> dict[str, object] | None:
        if has_saved:
            message = "completion readback failed"
            raise OSError(message)
        return await original_load(store)

    with monkeypatch.context() as patch:
        patch.setattr(Store, "async_save", failed_save)
        patch.setattr(Store, "async_load", failed_load)
        with pytest.raises((OSError, VerifiedAtomicStoreVerificationError)):
            await async_complete_repair(hass, entry, prepared=prepared)
    current = await _manifest(hass, entry)
    assert current.active_generation == prepared.active_generation
    assert current.repair_pending == (failure != "readback")
    completed = await async_complete_repair(hass, entry, prepared=prepared)
    assert not completed.repair_pending
    assert completed.active_generation == prepared.active_generation
    assert completed.commit_revision == prepared.commit_revision + 1
    assert len(hass_storage) == 2


async def test_completion_refuses_missing_manifest_or_foreign_retry_token(
    hass: HomeAssistant, hass_storage: dict[str, object]
) -> None:
    """A completion attempt has no authority to reconstruct or change a pointer."""
    entry = await _loaded_entry(hass)
    current = await _manifest(hass, entry)
    before = deepcopy(hass_storage)
    with pytest.raises(VerifiedAtomicStoreConflictError, match="no longer matches"):
        await async_complete_repair(
            hass, entry, prepared=replace(current, active_generation="f" * 32)
        )
    assert hass_storage == before
    del hass_storage[manifest_key(current.storage_id)]
    before = deepcopy(hass_storage)
    with pytest.raises(VerifiedAtomicStoreConflictError, match="manifest is missing"):
        await async_complete_repair(hass, entry)
    assert hass_storage == before


async def test_issue_identity_resumes_across_flows_after_uncertain_completion_write(
    hass: HomeAssistant,
    hass_storage: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The persistent issue ID survives a completion write whose read-back failed."""
    issue_token = "e" * 32
    entry = await _reserved_entry(hass)
    prepared = await async_prepare_repair(hass, entry, issue_token=issue_token)
    assert prepared.repair_issue_token == issue_token
    entry.runtime_data = await async_setup_storage(hass, entry)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    original_save = Store.async_save
    original_load = Store.async_load
    has_saved = False

    async def save_then_fail_readback(
        store: Store[dict[str, object]], data: dict[str, object]
    ) -> None:
        nonlocal has_saved
        await original_save(store, data)
        has_saved = True

    async def fail_readback(
        store: Store[dict[str, object]],
    ) -> dict[str, object] | None:
        if has_saved:
            message = "completion readback failed"
            raise OSError(message)
        return await original_load(store)

    with monkeypatch.context() as patch:
        patch.setattr(Store, "async_save", save_then_fail_readback)
        patch.setattr(Store, "async_load", fail_readback)
        with pytest.raises(OSError, match="readback failed"):
            await async_complete_repair(hass, entry, prepared=prepared)
    entry.mock_state(hass, ConfigEntryState.NOT_LOADED)
    entry.runtime_data = None
    before = deepcopy(hass_storage)
    # A newly created flow has no in-memory Manifest token after a restart.
    resumed = await async_prepare_repair(hass, entry, issue_token=issue_token)
    assert not resumed.repair_pending
    assert resumed.active_generation == prepared.active_generation
    assert resumed.repair_issue_token == issue_token
    assert hass_storage == before
    entry.runtime_data = await async_setup_storage(hass, entry)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    assert await async_complete_repair(hass, entry) == resumed
    assert hass_storage == before
    # A separately reported later incident retains the ability to request repair.
    entry.mock_state(hass, ConfigEntryState.NOT_LOADED)
    subsequent = await async_prepare_repair(hass, entry, issue_token="f" * 32)
    assert subsequent.active_generation != prepared.active_generation
    assert subsequent.repair_issue_token == "f" * 32
    assert subsequent.repair_pending


async def test_new_issue_does_not_supersede_an_unfinished_repair(
    hass: HomeAssistant, hass_storage: dict[str, object]
) -> None:
    """Pending repairs continue even when a recovered issue has a different token."""
    entry = await _reserved_entry(hass)
    prepared = await async_prepare_repair(hass, entry, issue_token="a" * 32)
    runtime = await async_setup_storage(hass, entry)
    initialized = await _manifest(hass, entry)
    before = deepcopy(hass_storage)
    resumed = await async_prepare_repair(hass, entry, issue_token="b" * 32)
    assert resumed.repair_pending
    assert resumed == replace(
        initialized,
        repair_issue_token="b" * 32,
        commit_revision=initialized.commit_revision + 1,
    )
    assert resumed.active_generation == prepared.active_generation
    assert hass_storage[runtime.store.store_key] == before[runtime.store.store_key]
    rebound = deepcopy(hass_storage)
    assert (
        await async_prepare_repair(hass, entry, prepared=prepared, issue_token="b" * 32)
        == resumed
    )
    assert hass_storage == rebound
    entry.runtime_data = runtime
    entry.mock_state(hass, ConfigEntryState.LOADED)
    completed = await async_complete_repair(hass, entry, prepared=prepared)
    entry.mock_state(hass, ConfigEntryState.NOT_LOADED)
    after = deepcopy(hass_storage)
    # The reconstructed issue remains associated even after pending was cleared.
    assert await async_prepare_repair(hass, entry, issue_token="b" * 32) == completed
    assert hass_storage == after
