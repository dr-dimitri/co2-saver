# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Explicit version gates, atomic manifest migration and narrow repair replacement."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.co2saver import async_migrate_entry
from custom_components.co2saver.const import DOMAIN
from custom_components.co2saver.measurement.storage import (
    PayloadMigration,
    VerifiedAtomicStore,
    VerifiedAtomicStoreConflictError,
    VerifiedAtomicStorePayloadError,
    VerifiedAtomicStoreVerificationError,
)
from custom_components.co2saver.migration import ManifestPayloadMigrator
from custom_components.co2saver.persistence import (
    GenerationRevisionPolicy,
    Manifest,
    ManifestCodec,
    ManifestRevisionPolicy,
)

from .test_evaluation import _config
from .test_persistence import _BOUNDARY, _STORAGE, _manifest, _state

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant

    from custom_components.co2saver.runtime import Co2SaverConfigEntry

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")
_KEY = f"co2saver.{_STORAGE}.manifest"


def _legacy(state: Manifest) -> dict[str, object]:
    """Represent the exact previously introduced manifest 1.1 payload."""
    payload = ManifestCodec.encode(state)
    payload["minor_version"] = 1
    del payload["repair_reset_at"]
    del payload["manifest_lost"]
    del payload["repair_pending"]
    del payload["repair_issue_token"]
    return payload


def _adapter(hass: HomeAssistant) -> VerifiedAtomicStore[Manifest]:
    """Bind the physical locator and owner before any automatic migration."""
    return VerifiedAtomicStore(
        hass,
        _KEY,
        codec=ManifestCodec(_STORAGE),
        revision_policy=ManifestRevisionPolicy(),
        payload_migrator=ManifestPayloadMigrator(_STORAGE, "owner"),
    )


async def _write(
    hass: HomeAssistant, payload: dict[str, object], *, major: int = 1, minor: int = 1
) -> None:
    """Seed a historical envelope using the same atomic Store container contract."""
    await Store[dict[str, object]](
        hass, major, _KEY, minor_version=minor, atomic_writes=True
    ).async_save(payload)


@pytest.mark.parametrize(
    "previous",
    [
        _manifest(),
        replace(_manifest(), owner_entry_id="owner", commit_revision=2),
        replace(
            _manifest(),
            owner_entry_id="owner",
            initialized=True,
            previous_generations=("d" * 32,),
            commit_revision=9,
        ),
    ],
)
async def test_known_manifest_migration_preserves_every_field_once(
    hass: HomeAssistant, hass_storage: dict[str, object], previous: Manifest
) -> None:
    """Only new repair defaults and one revision distinguish payload 1.2 from 1.1."""
    await _write(hass, _legacy(previous))
    expected = replace(previous, commit_revision=previous.commit_revision + 1)
    migrated = await _adapter(hass).async_load()
    assert migrated == expected
    assert not migrated.repair_pending
    assert migrated.repair_issue_token is None
    envelope = cast("dict[str, object]", hass_storage[_KEY])
    assert envelope["version"] == envelope["minor_version"] == 1
    assert envelope["data"] == ManifestCodec.encode(expected)
    with patch.object(
        Store, "async_save", side_effect=AssertionError("repeat migration")
    ):
        assert await _adapter(hass).async_load() == expected


async def test_concurrent_manifest_loaders_share_one_migration_revision(
    hass: HomeAssistant,
) -> None:
    """Two independently constructed adapters serialize the old-format mutation."""
    previous = replace(
        _manifest(), owner_entry_id="owner", initialized=True, commit_revision=8
    )
    await _write(hass, _legacy(previous))
    original_save = Store.async_save
    save_calls = 0

    async def counted_save(
        store: Store[dict[str, object]], payload: dict[str, object]
    ) -> None:
        nonlocal save_calls
        save_calls += 1
        await original_save(store, payload)

    with patch.object(Store, "async_save", counted_save):
        results = await asyncio.gather(
            _adapter(hass).async_load(), _adapter(hass).async_load()
        )
    assert results == [replace(previous, commit_revision=9)] * 2
    assert save_calls == 1


async def test_transact_verifies_migration_before_normal_transition(
    hass: HomeAssistant,
) -> None:
    """Migrating and binding are separate consecutive fully verified revisions."""
    await _write(hass, _legacy(_manifest()))
    observed: list[int] = []

    def bind(current: Manifest) -> Manifest:
        observed.append(current.commit_revision)
        return replace(
            current, owner_entry_id="owner", commit_revision=current.commit_revision + 1
        )

    result = await _adapter(hass).async_transact(bind)
    assert observed == [2]
    assert result == replace(_manifest(), owner_entry_id="owner", commit_revision=3)
    assert await _adapter(hass).async_load() == result


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 0),
        ("schema_version", 2),
        ("schema_version", True),
        ("minor_version", 0),
        ("minor_version", 3),
        ("minor_version", True),
        ("commit_revision", 0),
        ("initialized", 1),
        ("owner_entry_id", "foreign"),
        ("storage_id", "e" * 32),
        ("previous_generations", ["2" * 32]),
        ("repair_reset_at", None),
        ("extra", "unsupported"),
    ],
)
async def test_unknown_or_invalid_manifest_is_never_migrated(
    hass: HomeAssistant, hass_storage: dict[str, object], field: str, value: object
) -> None:
    """Foreign owners, malformed state and unknown versions retain original bytes."""
    payload = _legacy(_manifest())
    payload[field] = value
    await _write(hass, payload)
    before = deepcopy(hass_storage[_KEY])
    with (
        patch.object(
            Store, "async_save", side_effect=AssertionError("unexpected save")
        ),
        pytest.raises(
            ValueError, match=r"manifest|schema|commit_revision|initialized|generation"
        ),
    ):
        await _adapter(hass).async_load()
    assert hass_storage[_KEY] == before


async def test_migration_does_not_guess_or_replace_missing_state(
    hass: HomeAssistant,
) -> None:
    """A migrator cannot turn storage absence into a new manifest."""
    with patch.object(
        Store, "async_save", side_effect=AssertionError("unexpected save")
    ):
        assert await _adapter(hass).async_load() is None


async def test_failed_migration_readback_never_migrates_the_verification_payload(
    hass: HomeAssistant, hass_storage: dict[str, object]
) -> None:
    """A swallowed save leaves 1.1 on disk and fails instead of recursive migration."""
    previous = _legacy(_manifest())
    await _write(hass, previous)
    calls = 0
    migrate = ManifestPayloadMigrator(_STORAGE)

    def counted_migrate(payload: object) -> PayloadMigration[Manifest] | None:
        nonlocal calls
        calls += 1
        return migrate(payload)

    adapter = VerifiedAtomicStore(
        hass,
        _KEY,
        codec=ManifestCodec(_STORAGE),
        revision_policy=ManifestRevisionPolicy(),
        payload_migrator=counted_migrate,
    )
    with (
        patch.object(Store, "async_save", new=AsyncMock()),
        pytest.raises(
            VerifiedAtomicStoreVerificationError, match="read-back differs"
        ) as failure,
    ):
        await adapter.async_load()
    assert calls == 1
    assert not isinstance(failure.value, VerifiedAtomicStorePayloadError)
    assert cast("dict[str, object]", hass_storage[_KEY])["data"] == previous
    assert await _adapter(hass).async_load() == replace(_manifest(), commit_revision=2)


async def test_crash_after_migration_save_restores_exact_successor(
    hass: HomeAssistant,
) -> None:
    """Restart accepts the saved payload after an unobserved read-back failure."""
    await _write(hass, _legacy(_manifest()))
    original_load = Store.async_load
    loads = 0

    async def interrupted_load(
        store: Store[dict[str, object]],
    ) -> dict[str, object] | None:
        nonlocal loads
        loads += 1
        if loads == 2:
            message = "readback interrupted"
            raise OSError(message)
        return await original_load(store)

    with (
        patch.object(Store, "async_load", interrupted_load),
        pytest.raises(OSError, match="readback interrupted"),
    ):
        await _adapter(hass).async_load()
    with patch.object(
        Store, "async_save", side_effect=AssertionError("double migration")
    ):
        assert await _adapter(hass).async_load() == replace(
            _manifest(), commit_revision=2
        )


@pytest.mark.parametrize(
    "fault", ["async", "mutating", "return_type", "boolean", "negative", "revision"]
)
async def test_store_rejects_invalid_migrator_contract_before_save(
    hass: HomeAssistant, hass_storage: dict[str, object], fault: str
) -> None:
    """Injected migrations must be pure synchronous adjacent-revision proposals."""
    payload = _legacy(_manifest())
    await _write(hass, payload)

    async def asynchronous(_value: object) -> None:
        return None

    def invalid(value: object) -> object:
        if fault == "async":
            return asynchronous(value)
        if fault == "mutating":
            cast("dict[str, object]", value)["mutation"] = True
            return None
        if fault == "return_type":
            return object()
        previous = {"boolean": True, "negative": -1, "revision": 6}[fault]
        return PayloadMigration(previous, replace(_manifest(), commit_revision=2))

    adapter = VerifiedAtomicStore(
        hass,
        _KEY,
        codec=ManifestCodec(_STORAGE),
        revision_policy=ManifestRevisionPolicy(),
        payload_migrator=cast(
            "Callable[[object], PayloadMigration[Manifest] | None]", invalid
        ),
    )
    with (
        patch.object(
            Store, "async_save", side_effect=AssertionError("unexpected save")
        ),
        pytest.raises(
            (
                TypeError,
                VerifiedAtomicStoreConflictError,
                VerifiedAtomicStoreVerificationError,
            )
        ),
    ):
        await adapter.async_load()
    assert cast("dict[str, object]", hass_storage[_KEY])["data"] == payload


class _RepairInitialPolicy(ManifestRevisionPolicy):
    """Authorize only one explicit fresh-epoch owner-bound repair manifest."""

    @staticmethod
    def validate_initial(state: Manifest) -> None:
        if (
            state.owner_entry_id != "owner"
            or state.commit_revision != 1
            or state.repair_reset_at is None
            or not state.manifest_lost
            or state.initialized
            or state.previous_generations
        ):
            message = "invalid confirmed repair manifest"
            raise ValueError(message)


def _replacement() -> Manifest:
    """Build the explicitly confirmed new owner-bound repair epoch."""
    return replace(
        _manifest(),
        owner_entry_id="owner",
        repair_reset_at=_BOUNDARY,
        manifest_lost=True,
    )


def _repair_adapter(hass: HomeAssistant) -> VerifiedAtomicStore[Manifest]:
    """Keep the unusual initialization isolated from normal runtime policies."""
    return VerifiedAtomicStore(
        hass,
        _KEY,
        codec=ManifestCodec(_STORAGE),
        revision_policy=_RepairInitialPolicy(),
    )


@pytest.mark.parametrize(
    "source", ["missing", "malformed", "future_payload", "future_container"]
)
async def test_confirmed_repair_can_replace_only_unreadable_state(
    hass: HomeAssistant, source: str
) -> None:
    """The dedicated API can repair absence or corruption after caller-side backup."""
    if source == "malformed":
        await _write(hass, {"invalid": "manifest"})
    elif source == "future_payload":
        await _write(hass, {**ManifestCodec.encode(_manifest()), "minor_version": 3})
    elif source == "future_container":
        await _write(hass, ManifestCodec.encode(_manifest()), major=2)
    result = await _repair_adapter(hass).async_replace_confirmed_unreadable(
        _replacement()
    )
    assert result == _replacement()
    assert await _adapter(hass).async_load() == result


async def test_readable_manifest_blocks_confirmed_unreadable_repair(
    hass: HomeAssistant, hass_storage: dict[str, object]
) -> None:
    """A current manifest that became readable after confirmation is never erased."""
    await _write(hass, ManifestCodec.encode(_manifest()))
    before = deepcopy(hass_storage[_KEY])
    with pytest.raises(VerifiedAtomicStoreConflictError, match="readable state"):
        await _repair_adapter(hass).async_replace_confirmed_unreadable(_replacement())
    assert hass_storage[_KEY] == before


async def test_noncanonical_existing_payload_can_be_explicitly_repaired(
    hass: HomeAssistant,
) -> None:
    """Canonicality failures are distinct from failures to verify a new write."""

    class NormalizingCodec(ManifestCodec):
        """Simulate a legacy decoder which interprets a noncanonical representation."""

        def decode(self, value: object) -> Manifest:
            normalized = dict(cast("dict[str, object]", value))
            normalized.pop("noncanonical", None)
            return super().decode(normalized)

    await _write(hass, {**ManifestCodec.encode(_manifest()), "noncanonical": True})
    adapter = VerifiedAtomicStore(
        hass,
        _KEY,
        codec=NormalizingCodec(_STORAGE),
        revision_policy=_RepairInitialPolicy(),
    )
    with pytest.raises(VerifiedAtomicStorePayloadError, match="not canonical"):
        await adapter.async_load()
    assert (
        await adapter.async_replace_confirmed_unreadable(_replacement())
        == _replacement()
    )


async def test_repair_replacement_never_interprets_io_failure_as_permission(
    hass: HomeAssistant,
) -> None:
    """A read error does not prove that state is absent or corrupt."""
    with (
        patch.object(Store, "async_load", side_effect=OSError("disk unavailable")),
        patch.object(
            Store, "async_save", side_effect=AssertionError("unexpected save")
        ),
        pytest.raises(OSError, match="disk unavailable"),
    ):
        await _repair_adapter(hass).async_replace_confirmed_unreadable(_replacement())


async def test_normal_store_policy_cannot_initialize_repair_payload(
    hass: HomeAssistant,
) -> None:
    """The exceptional API leaves the ordinary bootstrap invariant intact."""
    with pytest.raises(ValueError, match="unbound bootstrap"):
        await _adapter(hass).async_replace_confirmed_unreadable(_replacement())


async def test_repair_replacement_requires_fresh_verified_readback(
    hass: HomeAssistant,
) -> None:
    """An acknowledged but absent replacement cannot be published as repaired."""
    with (
        patch.object(Store, "async_save", new=AsyncMock()),
        pytest.raises(VerifiedAtomicStoreVerificationError, match="absent after save"),
    ):
        await _repair_adapter(hass).async_replace_confirmed_unreadable(_replacement())


@pytest.mark.parametrize(
    ("count", "reset", "accepted"),
    [(0, False, True), (1, True, True), (1, False, False), (2, True, False)],
)
def test_generation_initialization_allows_only_one_confirmed_manifest_loss(
    count: int, *, reset: bool, accepted: bool
) -> None:
    """The repair marker does not permit arbitrary initial diagnostic history."""
    state = replace(
        _state(),
        repair_reset_at=_BOUNDARY if reset else None,
        diagnostics=tuple(sorted((*_state().diagnostics, ("manifest_losses", count)))),
    )
    if accepted:
        GenerationRevisionPolicy.validate_initial(state)
    else:
        with pytest.raises(ValueError, match="empty and quarantined"):
            GenerationRevisionPolicy.validate_initial(state)


@pytest.mark.parametrize(("major", "minor"), [(0, 1), (1, 0), (1, 2), (2, 1)])
async def test_config_unknown_version_refuses_migration_without_mutation(
    hass: HomeAssistant, major: int, minor: int
) -> None:
    """No nonexistent historical format is normalized or silently downgraded."""
    entry = MockConfigEntry(
        domain=DOMAIN, version=major, minor_version=minor, data=_config()
    )
    entry.add_to_hass(hass)
    before = deepcopy(entry.as_dict())
    assert not await async_migrate_entry(hass, cast("Co2SaverConfigEntry", entry))
    assert entry.as_dict() == before
    issue = ir.async_get(hass).async_get_issue(
        DOMAIN, f"configuration_invalid:{entry.entry_id}"
    )
    assert issue is not None
    assert not issue.is_fixable


@pytest.mark.parametrize("invalid", [False, True])
async def test_config_current_version_is_validated_without_rewriting(
    hass: HomeAssistant, *, invalid: bool
) -> None:
    """Known configuration has the accepted shape and needs no artificial rewrite."""
    config = _config()
    if invalid:
        config.pop("storage_id")
    entry = MockConfigEntry(domain=DOMAIN, version=1, minor_version=1, data=config)
    entry.add_to_hass(hass)
    before = deepcopy(entry.as_dict())
    assert (
        await async_migrate_entry(hass, cast("Co2SaverConfigEntry", entry))
        is not invalid
    )
    assert entry.as_dict() == before
