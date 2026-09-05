# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Complete, exact manifest and generation state for the verified Store."""

from __future__ import annotations

import re
from dataclasses import dataclass, fields
from fractions import Fraction
from typing import TYPE_CHECKING, cast

from .domain import EmissionDensity, Emissions, Energy, StorageLedger
from .measurement.models import MeasurementPhase, MeasurementPipelineState
from .measurement.storage import (
    VerifiedAtomicStoreConflictError,
    _as_list,
    _as_nonempty_string,
    _as_nonnegative_int,
    _as_object,
    _decode_fraction,
    _decode_utc,
    _encode_fraction,
    _encode_utc,
    decode_measurement_state,
    encode_measurement_state,
)

if TYPE_CHECKING:
    from datetime import datetime

_UUID = re.compile(r"[0-9a-f]{32}\Z")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
MANIFEST_SCHEMA_VERSION = 1
MANIFEST_MINOR_VERSION = 2
GENERATION_SCHEMA_VERSION = 1
GENERATION_MINOR_VERSION = 1
_MANIFEST_SCHEMA = {
    "schema_version": MANIFEST_SCHEMA_VERSION,
    "minor_version": MANIFEST_MINOR_VERSION,
}
_GENERATION_SCHEMA = {
    "schema_version": GENERATION_SCHEMA_VERSION,
    "minor_version": GENERATION_MINOR_VERSION,
}


def _invalid(message: str) -> None:
    """Reject malformed persisted state before any mutation."""
    raise ValueError(message)


def storage_identifier(value: object) -> str:
    """Validate a path-safe cryptographic ID without accepting path fragments."""
    if type(value) is not str or _UUID.fullmatch(value) is None:
        _invalid("storage identifier must be lowercase UUID hex")
    return cast("str", value)


def _revision(value: object) -> int:
    """Require the one-based revision used by authoritative payloads."""
    revision = _as_nonnegative_int(value, path="commit_revision")
    if revision < 1:
        _invalid("commit_revision must be positive")
    return revision


def _schema(payload: dict[str, object], expected: dict[str, int]) -> None:
    """Reject all unknown major and minor payload versions explicitly."""
    for key, value in expected.items():
        if type(payload[key]) is not int or payload[key] != value:
            _invalid("unsupported persisted schema version")


@dataclass(frozen=True, slots=True)
class Manifest:
    """The authoritative owner and generation pointer for one Store locator."""

    storage_id: str
    manifest_epoch: str
    owner_entry_id: str | None
    active_generation: str
    previous_generations: tuple[str, ...] = ()
    initialized: bool = False
    commit_revision: int = 1
    repair_reset_at: datetime | None = None
    manifest_lost: bool = False
    repair_pending: bool = False
    repair_issue_token: str | None = None


class ManifestCodec:
    """Validate an exact manifest schema against its physical locator."""

    def __init__(self, storage_id: str) -> None:
        """Bind this codec to the expected immutable locator."""
        self.storage_id = storage_identifier(storage_id)

    @staticmethod
    def encode(state: Manifest) -> dict[str, object]:
        """Serialize the complete authoritative pointer."""
        return {
            **_MANIFEST_SCHEMA,
            "storage_id": state.storage_id,
            "manifest_epoch": state.manifest_epoch,
            "owner_entry_id": state.owner_entry_id,
            "active_generation": state.active_generation,
            "previous_generations": list(state.previous_generations),
            "initialized": state.initialized,
            "commit_revision": state.commit_revision,
            "repair_reset_at": None
            if state.repair_reset_at is None
            else _encode_utc(state.repair_reset_at),
            "manifest_lost": state.manifest_lost,
            "repair_pending": state.repair_pending,
            "repair_issue_token": state.repair_issue_token,
        }

    def decode(self, value: object) -> Manifest:
        """Reject foreign ownership structure and invalid generation pointers."""
        payload = _as_object(
            value,
            path="manifest",
            keys=frozenset(
                {*_MANIFEST_SCHEMA, *(field.name for field in fields(Manifest))}
            ),
        )
        _schema(payload, _MANIFEST_SCHEMA)
        storage_id = storage_identifier(payload["storage_id"])
        if storage_id != self.storage_id:
            _invalid("foreign manifest storage_id")
        owner_value = payload["owner_entry_id"]
        owner = (
            None
            if owner_value is None
            else _as_nonempty_string(owner_value, path="owner_entry_id")
        )
        generation = storage_identifier(payload["active_generation"])
        previous = tuple(
            storage_identifier(item)
            for item in _as_list(
                payload["previous_generations"], path="previous_generations"
            )
        )
        if len(previous) != len(set(previous)) or generation in previous:
            _invalid("generation pointers must be unique")
        initialized = payload["initialized"]
        if type(initialized) is not bool or (initialized and owner is None):
            _invalid("initialized manifest requires an owner and boolean marker")
        reset_value = payload["repair_reset_at"]
        reset = (
            None
            if reset_value is None
            else _decode_utc(reset_value, path="repair_reset_at")
        )
        lost = payload["manifest_lost"]
        if type(lost) is not bool:
            _invalid("manifest_lost must be boolean")
        if (reset is not None and owner is None) or (lost and reset is None):
            _invalid("manifest repair requires an owner and reset timestamp")
        pending = payload["repair_pending"]
        if type(pending) is not bool:
            _invalid("repair_pending must be boolean")
        if pending and reset is None:
            _invalid("pending repair requires a reset timestamp")
        token_value = payload["repair_issue_token"]
        token = None if token_value is None else storage_identifier(token_value)
        if (pending and token is None) or (token is not None and reset is None):
            _invalid(
                "repair issue token requires a reset and identifies pending repair"
            )
        return Manifest(
            storage_id=storage_id,
            manifest_epoch=storage_identifier(payload["manifest_epoch"]),
            owner_entry_id=owner,
            active_generation=generation,
            previous_generations=previous,
            initialized=cast("bool", initialized),
            commit_revision=_revision(payload["commit_revision"]),
            repair_reset_at=reset,
            manifest_lost=cast("bool", lost),
            repair_pending=cast("bool", pending),
            repair_issue_token=token,
        )


class ManifestRevisionPolicy:
    """Allow only initial reservation, owner binding, and initialization."""

    @staticmethod
    def revision(state: Manifest) -> int:
        """Return the manifest revision."""
        return state.commit_revision

    @staticmethod
    def validate_initial(state: Manifest) -> None:
        """Require an unbound pristine bootstrap reservation."""
        if (
            state.commit_revision != 1
            or state.owner_entry_id is not None
            or state.initialized
            or state.previous_generations
            or state.repair_reset_at is not None
            or state.manifest_lost
            or state.repair_pending
            or state.repair_issue_token is not None
        ):
            _invalid("initial manifest must be an unbound bootstrap")

    @staticmethod
    def validate_transition(before: Manifest, after: Manifest) -> None:
        """Protect owner and generation from unrequested replacement."""
        if (
            before.storage_id != after.storage_id
            or before.manifest_epoch != after.manifest_epoch
            or before.active_generation != after.active_generation
            or before.previous_generations != after.previous_generations
            or before.repair_reset_at != after.repair_reset_at
            or before.manifest_lost != after.manifest_lost
            or before.repair_pending != after.repair_pending
            or before.repair_issue_token != after.repair_issue_token
            or (before.initialized and not after.initialized)
            or after.owner_entry_id is None
            or (
                before.owner_entry_id is not None
                and before.owner_entry_id != after.owner_entry_id
            )
        ):
            message = "manifest transition changes authoritative identity"
            raise VerifiedAtomicStoreConflictError(message)


@dataclass(frozen=True, slots=True)
class CumulativeTotals:
    """Exact energy and independent emissions components, never rounded."""

    direct_pv_kwh: Fraction = Fraction()
    storage_pv_kwh: Fraction = Fraction()
    direct_gross_g: Fraction = Fraction()
    direct_pv_burden_g: Fraction = Fraction()
    storage_gross_g: Fraction = Fraction()
    storage_pv_burden_g: Fraction = Fraction()
    storage_burden_g: Fraction = Fraction()
    unvalued_direct_kwh: Fraction = Fraction()
    unvalued_storage_kwh: Fraction = Fraction()

    def __post_init__(self) -> None:
        """Require non-negative exact cumulative components."""
        for field in fields(self):
            value = getattr(self, field.name)
            if type(value) is not Fraction or value < 0:
                _invalid("cumulative components must be non-negative Fractions")
        if (
            self.unvalued_direct_kwh > self.direct_pv_kwh
            or self.unvalued_storage_kwh > self.storage_pv_kwh
        ):
            _invalid("unvalued energy cannot exceed delivered PV energy")

    @property
    def direct_net_g(self) -> Fraction:
        """Derive net direct emissions without clamping negative savings."""
        return self.direct_gross_g - self.direct_pv_burden_g

    @property
    def storage_net_g(self) -> Fraction:
        """Derive net storage emissions from once-recorded components."""
        return self.storage_gross_g - self.storage_pv_burden_g - self.storage_burden_g


@dataclass(frozen=True, slots=True)
class GenerationState:
    """One atomic, complete measurement, ledger, and cumulative generation."""

    storage_id: str
    owner_entry_id: str
    generation: str
    commit_revision: int
    segment_fingerprint: str
    measurement: MeasurementPipelineState
    ledger: StorageLedger | None
    totals: CumulativeTotals
    consumer_totals: tuple[tuple[str, CumulativeTotals], ...]
    unassigned_direct_kwh: Fraction = Fraction()
    unassigned_storage_kwh: Fraction = Fraction()
    diagnostics: tuple[tuple[str, int], ...] = (
        ("discarded_intervals", 0),
        ("missing_grid_intensity", 0),
        ("segment_transitions", 0),
    )
    repair_reset_at: datetime | None = None


def _encode_totals(totals: CumulativeTotals) -> dict[str, object]:
    """Encode all exact cumulative components."""
    return {
        field.name: _encode_fraction(getattr(totals, field.name))
        for field in fields(totals)
    }


def _decode_totals(value: object) -> CumulativeTotals:
    """Validate all exact cumulative components."""
    payload = _as_object(
        value,
        path="totals",
        keys=frozenset(field.name for field in fields(CumulativeTotals)),
    )
    return CumulativeTotals(
        **{key: _decode_fraction(raw, path=key) for key, raw in payload.items()}
    )


def _encode_ledger(ledger: StorageLedger | None) -> dict[str, object] | None:
    """Persist the complete conservative storage provenance envelope."""
    if ledger is None:
        return None
    return {
        "capacity_kwh": _encode_fraction(ledger.capacity.kwh),
        "stored_lower_kwh": _encode_fraction(ledger.stored_lower.kwh),
        "stored_upper_kwh": _encode_fraction(ledger.stored_upper.kwh),
        "pv_lower_kwh": _encode_fraction(ledger.pv_lower.kwh),
        "non_pv_upper_kwh": _encode_fraction(ledger.non_pv_upper.kwh),
        "pv_burden_g": _encode_fraction(ledger.pv_burden.grams),
        "pv_density_upper_g_per_kwh": _encode_fraction(
            ledger.pv_density_upper.grams_per_kwh
        ),
    }


def _decode_ledger(value: object) -> StorageLedger | None:
    """Validate energy and burden bounds using domain invariants."""
    if value is None:
        return None
    payload = _as_object(
        value,
        path="ledger",
        keys=frozenset(
            {
                "capacity_kwh",
                "stored_lower_kwh",
                "stored_upper_kwh",
                "pv_lower_kwh",
                "non_pv_upper_kwh",
                "pv_burden_g",
                "pv_density_upper_g_per_kwh",
            }
        ),
    )
    quantities = {key: _decode_fraction(raw, path=key) for key, raw in payload.items()}
    ledger = StorageLedger(
        capacity=Energy(quantities["capacity_kwh"]),
        stored_lower=Energy(quantities["stored_lower_kwh"]),
        stored_upper=Energy(quantities["stored_upper_kwh"]),
        pv_lower=Energy(quantities["pv_lower_kwh"]),
        pv_burden=Emissions(quantities["pv_burden_g"]),
        pv_density_upper=EmissionDensity(quantities["pv_density_upper_g_per_kwh"]),
    )
    if ledger.non_pv_upper.kwh != quantities["non_pv_upper_kwh"]:
        _invalid("stored non-PV upper bound is inconsistent")
    return ledger


def _decode_consumers(value: object) -> tuple[tuple[str, CumulativeTotals], ...]:
    """Keep historical consumers ordered and uniquely identified."""
    rows = []
    for raw in _as_list(value, path="consumer_totals"):
        row = _as_object(
            raw, path="consumer_totals.row", keys=frozenset({"consumer_id", "totals"})
        )
        rows.append(
            (storage_identifier(row["consumer_id"]), _decode_totals(row["totals"]))
        )
    ids = [row[0] for row in rows]
    if not ids or ids != sorted(set(ids)):
        _invalid("historical consumer IDs must be nonempty, unique and sorted")
    return tuple(rows)


def _decode_diagnostics(value: object) -> tuple[tuple[str, int], ...]:
    """Validate sorted bounded-counter names without persisting history."""
    if type(value) is not dict:
        _invalid("diagnostics must be an object")
    raw = cast("dict[str, object]", value)
    return tuple(
        sorted(
            (
                _as_nonempty_string(key, path="diagnostics.key"),
                _as_nonnegative_int(count, path="diagnostics.count"),
            )
            for key, count in raw.items()
        )
    )


class GenerationCodec:
    """Validate a complete generation against its expected physical owner."""

    def __init__(self, storage_id: str, owner_entry_id: str, generation: str) -> None:
        """Bind the expected locator, owner and manifest generation."""
        self.identity = (
            storage_identifier(storage_id),
            owner_entry_id,
            storage_identifier(generation),
        )

    @staticmethod
    def encode(state: GenerationState) -> dict[str, object]:
        """Serialize every restart-critical part in one complete payload."""
        return {
            **_GENERATION_SCHEMA,
            "storage_id": state.storage_id,
            "owner_entry_id": state.owner_entry_id,
            "generation": state.generation,
            "commit_revision": state.commit_revision,
            "segment_fingerprint": state.segment_fingerprint,
            "measurement": encode_measurement_state(state.measurement),
            "ledger": _encode_ledger(state.ledger),
            "totals": _encode_totals(state.totals),
            "consumer_totals": [
                {"consumer_id": consumer_id, "totals": _encode_totals(totals)}
                for consumer_id, totals in state.consumer_totals
            ],
            "unassigned_direct_kwh": _encode_fraction(state.unassigned_direct_kwh),
            "unassigned_storage_kwh": _encode_fraction(state.unassigned_storage_kwh),
            "diagnostics": dict(state.diagnostics),
            "repair_reset_at": None
            if state.repair_reset_at is None
            else _encode_utc(state.repair_reset_at),
        }

    def decode(self, value: object) -> GenerationState:
        """Reject malformed and foreign generations, never silently repair."""
        payload = _as_object(
            value,
            path="generation",
            keys=frozenset(
                {
                    *_GENERATION_SCHEMA,
                    *(field.name for field in fields(GenerationState)),
                }
            ),
        )
        _schema(payload, _GENERATION_SCHEMA)
        identity = (
            storage_identifier(payload["storage_id"]),
            _as_nonempty_string(payload["owner_entry_id"], path="owner_entry_id"),
            storage_identifier(payload["generation"]),
        )
        if identity != self.identity:
            _invalid("foreign generation identity")
        fingerprint = payload["segment_fingerprint"]
        if type(fingerprint) is not str or _FINGERPRINT.fullmatch(fingerprint) is None:
            _invalid("invalid segment fingerprint")
        reset = payload["repair_reset_at"]
        result = GenerationState(
            storage_id=identity[0],
            owner_entry_id=identity[1],
            generation=identity[2],
            commit_revision=_revision(payload["commit_revision"]),
            segment_fingerprint=cast("str", fingerprint),
            measurement=decode_measurement_state(payload["measurement"]),
            ledger=_decode_ledger(payload["ledger"]),
            totals=_decode_totals(payload["totals"]),
            consumer_totals=_decode_consumers(payload["consumer_totals"]),
            unassigned_direct_kwh=_decode_fraction(
                payload["unassigned_direct_kwh"], path="unassigned_direct_kwh"
            ),
            unassigned_storage_kwh=_decode_fraction(
                payload["unassigned_storage_kwh"], path="unassigned_storage_kwh"
            ),
            diagnostics=_decode_diagnostics(payload["diagnostics"]),
            repair_reset_at=None
            if reset is None
            else _decode_utc(reset, path="repair_reset_at"),
        )
        for field in ("direct", "storage"):
            assigned = sum(
                (
                    getattr(totals, f"{field}_pv_kwh")
                    for _, totals in result.consumer_totals
                ),
                Fraction(),
            )
            unassigned = getattr(result, f"unassigned_{field}_kwh")
            if unassigned < 0 or assigned + unassigned != getattr(
                result.totals, f"{field}_pv_kwh"
            ):
                _invalid(
                    "consumer energy and unassigned remainder must equal system energy"
                )
        return result


class GenerationRevisionPolicy:
    """Enforce durable identity and monotonic cumulative components."""

    @staticmethod
    def revision(state: GenerationState) -> int:
        """Return the complete generation's revision."""
        return state.commit_revision

    @staticmethod
    def validate_initial(state: GenerationState) -> None:
        """Require an empty, conservative first segment before reading sources."""
        if (
            state.commit_revision != 1
            or state.measurement
            != MeasurementPipelineState.initial(
                state.measurement.sources, state.measurement.segment_transition_at
            )
            or state.totals != CumulativeTotals()
            or any(total != CumulativeTotals() for _, total in state.consumer_totals)
            or any(
                count
                and not (
                    name == "manifest_losses"
                    and count == 1
                    and state.repair_reset_at is not None
                )
                for name, count in state.diagnostics
            )
            or state.unassigned_direct_kwh
            or state.unassigned_storage_kwh
            or (
                state.ledger is not None
                and state.ledger != StorageLedger.quarantined(state.ledger.capacity)
            )
        ):
            _invalid("new generation must begin empty and quarantined")

    @staticmethod
    def validate_transition(before: GenerationState, after: GenerationState) -> None:
        """Never erase history or carry provenance across a new segment."""
        if (
            before.storage_id,
            before.owner_entry_id,
            before.generation,
            before.repair_reset_at,
        ) != (
            after.storage_id,
            after.owner_entry_id,
            after.generation,
            after.repair_reset_at,
        ):
            _invalid("generation transition changes durable identity")
        _validate_monotonic_totals(before.totals, after.totals)
        after_consumers = dict(after.consumer_totals)
        for consumer_id, totals in before.consumer_totals:
            if consumer_id not in after_consumers:
                _invalid("historical consumer totals must be retained")
            _validate_monotonic_totals(totals, after_consumers[consumer_id])
        after_diagnostics = dict(after.diagnostics)
        if any(
            after_diagnostics.get(key, -1) < count for key, count in before.diagnostics
        ):
            _invalid("diagnostic counters must be monotonic")
        if (
            after.unassigned_direct_kwh < before.unassigned_direct_kwh
            or after.unassigned_storage_kwh < before.unassigned_storage_kwh
        ):
            _invalid("unassigned energy must be monotonic")
        if before.segment_fingerprint != after.segment_fingerprint:
            _validate_segment_transition(before, after)


def _validate_monotonic_totals(
    before: CumulativeTotals, after: CumulativeTotals
) -> None:
    """Keep every positive component monotonic while allowing negative nets."""
    if any(
        getattr(after, field.name) < getattr(before, field.name)
        for field in fields(before)
    ):
        _invalid("cumulative components must be monotonic")


def _validate_segment_transition(
    before: GenerationState, after: GenerationState
) -> None:
    """Require a new prospective baseline, quarantine, and unchanged history."""
    measurement = after.measurement
    if (
        measurement.phase is not MeasurementPhase.AWAITING_SEGMENT_BASELINE
        or measurement.baseline is not None
        or measurement.candidate is not None
        or measurement.recovery_after_period_end is not None
        or measurement.segment_transition_at < before.measurement.segment_transition_at
        or (
            after.ledger is not None
            and after.ledger != StorageLedger.quarantined(after.ledger.capacity)
        )
        or before.totals != after.totals
        or any(
            dict(after.consumer_totals)[consumer_id] != totals
            for consumer_id, totals in before.consumer_totals
        )
    ):
        _invalid("segment changes must preserve history and quarantine provenance")
