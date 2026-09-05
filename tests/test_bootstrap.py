# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Contract tests for reservation, owner binding, recovery, and segmentation."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import pytest
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.co2saver import bootstrap
from custom_components.co2saver.bootstrap import (
    async_reserve_bootstrap,
    async_setup_storage,
    generation_key,
    generation_store,
    manifest_key,
    manifest_lock,
    manifest_store,
)
from custom_components.co2saver.config_plan import segment_fingerprint, source_bindings
from custom_components.co2saver.const import DOMAIN
from custom_components.co2saver.domain import (
    EmissionDensity,
    Emissions,
    Energy,
    StorageLedger,
)
from custom_components.co2saver.measurement.models import (
    CandidateBuffer,
    CounterSnapshot,
    EnergyCounterSample,
    EnergyUnit,
    MeasurementPhase,
)
from custom_components.co2saver.measurement.storage import (
    VerifiedAtomicStoreError,
    VerifiedAtomicStoreVerificationError,
)
from custom_components.co2saver.persistence import (
    CumulativeTotals,
    GenerationCodec,
    ManifestCodec,
)

if TYPE_CHECKING:
    from pathlib import Path

    from homeassistant.core import HomeAssistant

    from custom_components.co2saver.persistence import Manifest

_NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)
_HOUSE = "a" * 32
_WALLBOX = "b" * 32


@pytest.fixture(autouse=True)
def deterministic_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """Freeze segment initialization without freezing async scheduling."""
    monkeypatch.setattr(dt_util, "utcnow", lambda: _NOW)


@pytest.fixture
def storage_directory(hass: HomeAssistant, tmp_path: Path) -> Path:
    """Use real physical names alongside HA's in-memory Store test fixture."""
    hass.config.config_dir = str(tmp_path)
    directory = tmp_path / ".storage"
    directory.mkdir()
    return directory


def _data(*, battery: bool = False, topology: str = "inverter") -> dict[str, object]:
    """Build a complete source-registry-based configuration for bootstrap."""
    factors: dict[str, object] = {
        "grid_intensity_source": "5" * 32,
        "grid_max_age_minutes": 120,
        "pv_factor": "40",
    }
    if battery:
        factors["battery_factor"] = "20"
    sources = {"grid_import": "2" * 32, "grid_export": "3" * 32}
    if topology == "inverter":
        sources["pv_generation"] = "1" * 32
    return {
        "topology": topology,
        "sources": sources,
        "plant_key": f"grid:{'2' * 32}:{'3' * 32}",
        "synchronous_sources_confirmed": True,
        "battery": {
            "battery_id": "8" * 32,
            "charge_source": "6" * 32,
            "discharge_source": "7" * 32,
            "usable_capacity_kwh": "10",
            "round_trip_efficiency": "0.9",
        }
        if battery
        else None,
        "consumption": {
            "mode": "aggregate_shares",
            "household_id": _HOUSE,
            "household_source": "4" * 32,
            "consumers": [
                {"consumer_id": _WALLBOX, "name": "Wallbox", "share": "0.25"}
            ],
        },
        "factors": factors,
    }


async def _reserved_entry(
    hass: HomeAssistant, *, battery: bool = False, topology: str = "inverter"
) -> MockConfigEntry:
    """Create a visible entry only after a verified bootstrap reservation."""
    async with manifest_lock(hass):
        storage_id = await async_reserve_bootstrap(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**_data(battery=battery, topology=topology), "storage_id": storage_id},
    )
    entry.add_to_hass(hass)
    return entry


async def _manifest(hass: HomeAssistant, entry: MockConfigEntry) -> Manifest:
    """Read the authoritative pointer using a fresh verified adapter."""
    manifest = await manifest_store(hass, entry.data["storage_id"]).async_load()
    assert manifest is not None
    return manifest


@pytest.mark.parametrize("battery", [False, True])
@pytest.mark.parametrize("topology", ["inverter", "smart_meter"])
async def test_bootstrap_initializes_complete_owned_generation(
    hass: HomeAssistant, *, battery: bool, topology: str
) -> None:
    """Both accepted topologies start with zero results and conservative state."""
    entry = await _reserved_entry(hass, battery=battery, topology=topology)
    reserved = await _manifest(hass, entry)
    assert reserved.owner_entry_id is None
    assert not reserved.initialized
    assert reserved.commit_revision == 1
    assert await generation_store(hass, reserved, entry.entry_id).async_load() is None

    runtime = await async_setup_storage(hass, entry)
    manifest = await _manifest(hass, entry)
    state = runtime.state
    assert manifest.owner_entry_id == entry.entry_id
    assert manifest.initialized
    assert manifest.commit_revision == 3
    assert state.commit_revision == 1
    assert state.measurement.sources == source_bindings(entry.data)
    assert state.measurement.phase is MeasurementPhase.AWAITING_SEGMENT_BASELINE
    assert state.measurement.segment_transition_at == _NOW
    assert state.measurement.baseline is None
    assert state.measurement.candidate is None
    assert state.segment_fingerprint == segment_fingerprint(entry.data)
    assert state.totals == CumulativeTotals()
    assert state.consumer_totals == (
        (_HOUSE, CumulativeTotals()),
        (_WALLBOX, CumulativeTotals()),
    )
    assert all(count == 0 for _, count in state.diagnostics)
    assert state.ledger == (
        StorageLedger.quarantined(Energy(Fraction(10))) if battery else None
    )
    assert await runtime.store.async_load() == state
    assert (await async_setup_storage(hass, entry)).state == state
    assert await _manifest(hass, entry) == manifest


async def test_reservation_retries_all_occupied_physical_names_without_loading(
    hass: HomeAssistant, storage_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Entry, manifest, generation, and corrupt collisions preserve every byte."""
    candidates = [f"{number:032x}" for number in range(1, 8)]
    existing = MockConfigEntry(domain=DOMAIN, data={"storage_id": candidates[0]})
    existing.add_to_hass(hass)
    names = [
        manifest_key(candidates[1]),
        generation_key(candidates[2], "f" * 32),
        f"{manifest_key(candidates[3])}.corrupt.2026-09-05",
    ]
    for name in names:
        (storage_directory / name).write_bytes(b"untouched bytes")
    iterator = iter(candidates)
    monkeypatch.setattr(bootstrap, "uuid4", lambda: SimpleNamespace(hex=next(iterator)))
    original = Store.async_load
    loaded: list[str] = []

    async def track_load(store: Store[dict[str, object]]) -> dict[str, object] | None:
        loaded.append(store.key)
        return await original(store)

    monkeypatch.setattr(Store, "async_load", track_load)
    async with manifest_lock(hass):
        storage_id = await async_reserve_bootstrap(hass)
    assert storage_id == candidates[4]
    assert set(loaded) == {manifest_key(storage_id)}
    for name in names:
        assert (storage_directory / name).read_bytes() == b"untouched bytes"


async def test_reservation_requires_the_global_lock(hass: HomeAssistant) -> None:
    """A standalone caller cannot accidentally perform an unlocked create."""
    with pytest.raises(RuntimeError, match="manifest lock"):
        await async_reserve_bootstrap(hass)


async def test_duplicate_locator_rejected_before_owner_binding(
    hass: HomeAssistant, hass_storage: dict[str, object]
) -> None:
    """No manifest revision changes when two visible entries share a locator."""
    entry = await _reserved_entry(hass)
    duplicate = MockConfigEntry(domain=DOMAIN, data=dict(entry.data))
    duplicate.add_to_hass(hass)
    before = deepcopy(hass_storage)
    for target in (entry, duplicate):
        with pytest.raises(VerifiedAtomicStoreError, match="another config entry"):
            await async_setup_storage(hass, target)
    assert hass_storage == before


async def test_foreign_manifest_owner_is_never_rebound(
    hass: HomeAssistant, hass_storage: dict[str, object]
) -> None:
    """Even a stale owner mismatch is data loss, never a fresh installation."""
    entry = await _reserved_entry(hass)
    adapter = manifest_store(hass, entry.data["storage_id"])
    await adapter.async_transact(
        lambda current: replace(
            current,
            owner_entry_id="another-owner",
            commit_revision=current.commit_revision + 1,
        )
    )
    before = deepcopy(hass_storage)
    with pytest.raises(VerifiedAtomicStoreError, match="another owner"):
        await async_setup_storage(hass, entry)
    assert hass_storage == before


async def test_existing_entry_missing_manifest_fails_closed(
    hass: HomeAssistant,
) -> None:
    """A locator without its manifest can never initialize a replacement."""
    entry = MockConfigEntry(domain=DOMAIN, data={**_data(), "storage_id": "f" * 32})
    entry.add_to_hass(hass)
    with pytest.raises(VerifiedAtomicStoreError, match="manifest is missing"):
        await async_setup_storage(hass, entry)


@pytest.mark.parametrize("kind", ["manifest", "generation"])
async def test_corrupt_markers_fail_closed_without_loading(
    hass: HomeAssistant,
    storage_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """A previous syntax failure is never treated as physically absent state."""
    entry = await _reserved_entry(hass)
    manifest = await _manifest(hass, entry)
    key = (
        manifest_key(manifest.storage_id)
        if kind == "manifest"
        else generation_key(manifest.storage_id, manifest.active_generation)
    )
    marker = storage_directory / f"{key}.corrupt.2026-09-05"
    marker.write_bytes(b"damaged original")
    original_load = Store.async_load

    async def reject_marker_load(
        store: Store[dict[str, object]],
    ) -> dict[str, object] | None:
        assert store.key != key
        return await original_load(store)

    monkeypatch.setattr(Store, "async_load", reject_marker_load)
    with pytest.raises(VerifiedAtomicStoreError, match="corrupt-file marker"):
        await async_setup_storage(hass, entry)
    assert marker.read_bytes() == b"damaged original"


async def test_existing_invalid_generation_cannot_be_initialized(
    hass: HomeAssistant, storage_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Physical existence is remembered even when Store reports None after load."""
    entry = await _reserved_entry(hass)
    manifest = await _manifest(hass, entry)
    key = generation_key(manifest.storage_id, manifest.active_generation)
    physical = storage_directory / key
    physical.write_bytes(b"{bad JSON")
    original_save = Store.async_save

    async def forbid_generation_save(
        store: Store[dict[str, object]], data: dict[str, object]
    ) -> None:
        assert store.key != key
        await original_save(store, data)

    monkeypatch.setattr(Store, "async_save", forbid_generation_save)
    with pytest.raises(
        VerifiedAtomicStoreError, match="generation is missing or corrupt"
    ):
        await async_setup_storage(hass, entry)
    assert physical.read_bytes() == b"{bad JSON"


async def test_initialized_missing_generation_never_starts_over(
    hass: HomeAssistant, hass_storage: dict[str, object]
) -> None:
    """Losing initialized generation data cannot manufacture a zero state."""
    entry = await _reserved_entry(hass)
    runtime = await async_setup_storage(hass, entry)
    del hass_storage[runtime.store.store_key]
    before = deepcopy(hass_storage)
    with pytest.raises(VerifiedAtomicStoreError, match="generation is missing"):
        await async_setup_storage(hass, entry)
    assert hass_storage == before


async def test_interrupted_initialization_resumes_exact_saved_generation(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Crash after generation verification retains its timestamp and all state."""
    entry = await _reserved_entry(hass, battery=True)
    original_save = Store.async_save

    async def swallow_initialized_manifest(
        store: Store[dict[str, object]], data: dict[str, object]
    ) -> None:
        if store.key.endswith(".manifest") and data["initialized"] is True:
            return
        await original_save(store, data)

    with monkeypatch.context() as patch:
        patch.setattr(Store, "async_save", swallow_initialized_manifest)
        with pytest.raises(VerifiedAtomicStoreVerificationError, match="differs"):
            await async_setup_storage(hass, entry)
    manifest = await _manifest(hass, entry)
    assert manifest.owner_entry_id == entry.entry_id
    assert not manifest.initialized
    saved = await generation_store(hass, manifest, entry.entry_id).async_load()
    assert saved is not None
    monkeypatch.setattr(dt_util, "utcnow", lambda: _NOW + timedelta(hours=1))
    resumed = await async_setup_storage(hass, entry)
    assert resumed.state == saved
    assert (await _manifest(hass, entry)).initialized


@pytest.mark.parametrize("kind", ["manifest", "generation"])
@pytest.mark.parametrize(
    "mutation", ["schema_version", "minor_version", "storage_id", "bad_shape"]
)
async def test_invalid_payloads_never_mutate_stores(
    hass: HomeAssistant, hass_storage: dict[str, object], kind: str, mutation: str
) -> None:
    """Foreign, future, and malformed manifests or generations stay fail closed."""
    entry = await _reserved_entry(hass)
    runtime = await async_setup_storage(hass, entry)
    manifest = await _manifest(hass, entry)
    key = (
        manifest_key(manifest.storage_id)
        if kind == "manifest"
        else runtime.store.store_key
    )
    payload = (
        ManifestCodec.encode(manifest)
        if kind == "manifest"
        else GenerationCodec.encode(runtime.state)
    )
    payload[mutation] = "f" * 32 if mutation == "storage_id" else 2
    await Store(hass, 1, key, minor_version=1, atomic_writes=True).async_save(payload)
    before = deepcopy(hass_storage)
    with pytest.raises(ValueError, match=r"unsupported|foreign|unexpected"):
        await async_setup_storage(hass, entry)
    assert hass_storage == before


@pytest.mark.parametrize("failure", ["raise", "swallow", "different"])
async def test_bootstrap_publishes_no_locator_until_full_readback(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """All bootstrap save/read-back failure modes stop before creating entries."""
    original_save = Store.async_save

    async def failed_save(
        store: Store[dict[str, object]], data: dict[str, object]
    ) -> None:
        if failure == "raise":
            message = "disk write failed"
            raise OSError(message)
        if failure == "different":
            await original_save(store, {**data, "commit_revision": 999})

    monkeypatch.setattr(Store, "async_save", failed_save)
    with pytest.raises((OSError, VerifiedAtomicStoreVerificationError)):
        async with manifest_lock(hass):
            await async_reserve_bootstrap(hass)
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_all_writes_use_fresh_atomic_store_instances(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reservation, binding, generation, and initialized marker all read back."""
    original_save = Store.async_save
    original_load = Store.async_load
    saved: list[Store[dict[str, object]]] = []
    loaded: list[Store[dict[str, object]]] = []

    async def save(store: Store[dict[str, object]], data: dict[str, object]) -> None:
        assert store._atomic_writes  # noqa: SLF001
        saved.append(store)
        await original_save(store, data)

    async def load(store: Store[dict[str, object]]) -> dict[str, object] | None:
        assert store._atomic_writes  # noqa: SLF001
        loaded.append(store)
        return await original_load(store)

    monkeypatch.setattr(Store, "async_save", save)
    monkeypatch.setattr(Store, "async_load", load)
    entry = await _reserved_entry(hass)
    await async_setup_storage(hass, entry)
    assert len(saved) == 4
    assert all(writer is not reader for writer in saved for reader in loaded)
    for writer in saved:
        assert any(reader.key == writer.key for reader in loaded)


async def test_segment_change_preserves_history_and_quarantines_filled_ledger(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Factor, source, storage, and consumer edits never revalue old totals."""
    entry = await _reserved_entry(hass, battery=True)
    runtime = await async_setup_storage(hass, entry)
    samples = tuple(
        EnergyCounterSample(
            source, Energy(Fraction(10)), EnergyUnit.KILOWATT_HOUR, _NOW, _NOW
        )
        for source in runtime.state.measurement.sources
    )
    candidate_at = _NOW + timedelta(minutes=1)
    active_measurement = replace(
        runtime.state.measurement,
        revision=1,
        phase=MeasurementPhase.ACTIVE,
        baseline=CounterSnapshot(samples),
        candidate=CandidateBuffer(
            candidate_at,
            (replace(samples[0], period_end=candidate_at, last_reported=candidate_at),),
        ),
    )
    totals = CumulativeTotals(
        direct_pv_kwh=Fraction(1, 3),
        direct_gross_g=Fraction(100),
        direct_pv_burden_g=Fraction(120),
    )
    historical = await runtime.store.async_transact(
        lambda state: replace(
            state,
            commit_revision=state.commit_revision + 1,
            measurement=active_measurement,
            totals=totals,
            consumer_totals=((_HOUSE, totals), (_WALLBOX, CumulativeTotals())),
            ledger=StorageLedger(
                capacity=Energy(Fraction(10)),
                stored_lower=Energy(Fraction(4)),
                stored_upper=Energy(Fraction(4)),
                pv_lower=Energy(Fraction(4)),
                pv_burden=Emissions(Fraction(200)),
                pv_density_upper=EmissionDensity(Fraction(50)),
            ),
        )
    )
    changed = deepcopy(dict(entry.data))
    cast("dict[str, object]", changed["factors"])["pv_factor"] = "80"
    consumption = cast("dict[str, object]", changed["consumption"])
    consumption["consumers"] = [
        {"consumer_id": "c" * 32, "name": "New consumer", "share": "0.1"}
    ]
    hass.config_entries.async_update_entry(entry, data=changed)
    boundary = _NOW + timedelta(minutes=5)
    monkeypatch.setattr(dt_util, "utcnow", lambda: boundary)
    transitioned = await async_setup_storage(hass, entry)
    assert transitioned.state.commit_revision == historical.commit_revision + 1
    assert transitioned.state.measurement.segment_transition_at == boundary
    assert (
        transitioned.state.measurement.phase
        is MeasurementPhase.AWAITING_SEGMENT_BASELINE
    )
    assert transitioned.state.measurement.baseline is None
    assert transitioned.state.measurement.candidate is None
    assert transitioned.state.totals == totals
    assert transitioned.state.totals.direct_net_g == -20
    assert dict(transitioned.state.consumer_totals)[_HOUSE] == totals
    assert dict(transitioned.state.consumer_totals)[_WALLBOX] == CumulativeTotals()
    assert dict(transitioned.state.consumer_totals)["c" * 32] == CumulativeTotals()
    assert transitioned.state.ledger == StorageLedger.quarantined(Energy(Fraction(10)))
    assert (await async_setup_storage(hass, entry)).state == transitioned.state


async def test_name_only_change_preserves_segment_and_ledger(
    hass: HomeAssistant,
) -> None:
    """Display names have no physical or accounting effect."""
    entry = await _reserved_entry(hass, battery=True)
    runtime = await async_setup_storage(hass, entry)
    changed = deepcopy(dict(entry.data))
    consumption = cast("dict[str, object]", changed["consumption"])
    cast("list[dict[str, object]]", consumption["consumers"])[0]["name"] = "Renamed"
    hass.config_entries.async_update_entry(entry, data=changed)
    assert (await async_setup_storage(hass, entry)).state == runtime.state


async def test_setup_propagates_read_errors_without_overwriting(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Transient I/O failure is distinct from permission to initialize."""
    entry = await _reserved_entry(hass)
    monkeypatch.setattr(
        Store, "async_load", AsyncMock(side_effect=OSError("read failed"))
    )
    save = AsyncMock()
    monkeypatch.setattr(Store, "async_save", save)
    with pytest.raises(OSError, match="read failed"):
        await async_setup_storage(hass, entry)
    save.assert_not_called()


@pytest.mark.parametrize(
    "mismatch", ["sources", "storage_presence", "storage_capacity", "consumer"]
)
async def test_unchanged_fingerprint_requires_consistent_segment_state(
    hass: HomeAssistant, hass_storage: dict[str, object], mismatch: str
) -> None:
    """A claimed current fingerprint cannot conceal a foreign source or ledger."""
    entry = await _reserved_entry(hass, battery=True)
    runtime = await async_setup_storage(hass, entry)
    state = runtime.state
    if mismatch == "sources":
        changed_source = replace(state.measurement.sources[0], registry_id="f" * 32)
        measurement = replace(
            state.measurement, sources=(changed_source, *state.measurement.sources[1:])
        )
        state = replace(state, measurement=measurement)
    elif mismatch == "storage_presence":
        state = replace(state, ledger=None)
    elif mismatch == "storage_capacity":
        state = replace(state, ledger=StorageLedger.quarantined(Energy(Fraction(20))))
    else:
        state = replace(state, consumer_totals=((_WALLBOX, CumulativeTotals()),))
    await Store(
        hass, 1, runtime.store.store_key, minor_version=1, atomic_writes=True
    ).async_save(GenerationCodec.encode(state))
    before = deepcopy(hass_storage)
    with pytest.raises(VerifiedAtomicStoreError, match="current segment plan"):
        await async_setup_storage(hass, entry)
    assert hass_storage == before
