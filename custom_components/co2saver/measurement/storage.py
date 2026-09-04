# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Exact measurement codec and verified generic Home Assistant persistence."""

from __future__ import annotations

import asyncio
import inspect
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from typing import TYPE_CHECKING, Never, Protocol, cast

from homeassistant.core import CoreState
from homeassistant.exceptions import UnsupportedStorageVersionError
from homeassistant.helpers.storage import Store
from homeassistant.util.hass_dict import HassKey

from custom_components.co2saver.domain import Energy

from .models import (
    CandidateBuffer,
    CounterSnapshot,
    EnergyCounterSample,
    EnergySourceIdentity,
    EnergyUnit,
    MeasurementPhase,
    MeasurementPipelineState,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant

_STORE_MAJOR_VERSION = 1
_STORE_MINOR_VERSION = 1
_SAMPLE_KEYS = frozenset(
    {
        "source",
        "cumulative_kwh",
        "source_unit",
        "period_end",
        "last_reported",
    }
)
_SOURCE_KEYS = frozenset({"role", "registry_id"})
_FRACTION_KEYS = frozenset({"numerator", "denominator"})
_SNAPSHOT_KEYS = frozenset({"samples"})
_CANDIDATE_KEYS = frozenset({"period_end", "samples"})
_STATE_KEYS = frozenset(
    {
        "revision",
        "phase",
        "sources",
        "segment_transition_at",
        "baseline",
        "candidate",
        "recovery_after_period_end",
    }
)
_STORE_LOCKS: HassKey[dict[str, asyncio.Lock]] = HassKey("co2saver_store_locks")


class StateCodec[T](Protocol):
    """Encode and decode one complete logical state at a physical Store key."""

    def encode(self, state: T, /) -> dict[str, object]:
        """Encode a complete state into its canonical Store payload."""

    def decode(self, payload: object, /) -> T:
        """Decode and validate one complete Store payload."""


class RevisionPolicy[T](Protocol):
    """Define valid initialization and sequential transitions for a state."""

    def revision(self, state: T, /) -> int:
        """Return the state's non-negative monotonic revision."""

    def validate_initial(self, state: T, /) -> None:
        """Reject a state that is not a valid new-store value."""

    def validate_transition(self, before: T, after: T, /) -> None:
        """Reject a semantically invalid transition between adjacent revisions."""


class MeasurementStateCodecError(ValueError):
    """Reject a structurally or semantically invalid persisted payload."""


class VerifiedAtomicStoreError(RuntimeError):
    """Base error for a rejected verified-store operation."""


class VerifiedAtomicStoreVersionError(VerifiedAtomicStoreError):
    """Reject a store version for which no migration exists."""


class VerifiedAtomicStoreConflictError(VerifiedAtomicStoreError):
    """Reject initialization or a non-sequential state revision."""


class VerifiedAtomicStoreVerificationError(VerifiedAtomicStoreError):
    """Reject a save whose fresh read-back is absent or different."""


def _raise_codec(
    path: str,
    detail: str,
    *,
    cause: BaseException | None = None,
) -> Never:
    """Raise one path-specific codec error with an optional explicit cause."""
    error = MeasurementStateCodecError(f"{path}: {detail}")
    if cause is None:
        raise error
    raise error from cause


def _as_object(
    value: object,
    *,
    path: str,
    keys: frozenset[str],
) -> dict[str, object]:
    """Require one exact JSON object schema without extension fields."""
    if type(value) is not dict:
        _raise_codec(path, "must be an object")
    result = cast("dict[object, object]", value)
    if any(type(key) is not str for key in result):
        _raise_codec(path, "keys must be strings")
    string_result = cast("dict[str, object]", result)
    if string_result.keys() != keys:
        missing = sorted(keys - string_result.keys())
        extra = sorted(string_result.keys() - keys)
        _raise_codec(path, f"unexpected keys (missing={missing}, extra={extra})")
    return string_result


def _as_list(value: object, *, path: str) -> list[object]:
    """Require a canonical JSON array."""
    if type(value) is not list:
        _raise_codec(path, "must be an array")
    return cast("list[object]", value)


def _as_nonempty_string(value: object, *, path: str) -> str:
    """Require a non-empty canonical identifier string."""
    if type(value) is not str:
        _raise_codec(path, "must be a string")
    result = value
    if not result or result != result.strip():
        _raise_codec(path, "must be non-empty and contain no edge whitespace")
    return result


def _as_nonnegative_int(value: object, *, path: str) -> int:
    """Require a non-negative integer while excluding booleans."""
    if type(value) is not int:
        _raise_codec(path, "must be an integer")
    result = value
    if result < 0:
        _raise_codec(path, "must not be negative")
    return result


def _encode_fraction(value: Fraction) -> dict[str, object]:
    """Encode a canonical exact rational number."""
    return {"numerator": value.numerator, "denominator": value.denominator}


def _decode_fraction(value: object, *, path: str) -> Fraction:
    """Decode only a positive-denominator, reduced rational number."""
    payload = _as_object(value, path=path, keys=_FRACTION_KEYS)
    numerator_value = payload["numerator"]
    denominator_value = payload["denominator"]
    if type(numerator_value) is not int or type(denominator_value) is not int:
        _raise_codec(path, "numerator and denominator must be integers")
    numerator = numerator_value
    denominator = denominator_value
    if denominator <= 0:
        _raise_codec(path, "denominator must be positive")
    result = Fraction(numerator, denominator)
    if result.numerator != numerator or result.denominator != denominator:
        _raise_codec(path, "fraction must be reduced and canonical")
    return result


def _encode_utc(value: datetime) -> str:
    """Encode one canonical UTC timestamp with a literal ``Z`` suffix."""
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        message = "timestamp must be timezone-aware UTC"
        raise MeasurementStateCodecError(message)
    return value.astimezone(UTC).isoformat().removesuffix("+00:00") + "Z"


def _decode_utc(value: object, *, path: str) -> datetime:
    """Decode only the canonical timestamp form emitted by :func:`_encode_utc`."""
    if type(value) is not str:
        _raise_codec(path, "must be a UTC-Z string")
    raw = value
    if not raw.endswith("Z"):
        _raise_codec(path, "must end in Z")
    try:
        result = datetime.fromisoformat(f"{raw[:-1]}+00:00")
    except ValueError as err:
        _raise_codec(path, "must be a valid UTC-Z timestamp", cause=err)
    if _encode_utc(result) != raw:
        _raise_codec(path, "must use canonical UTC-Z representation")
    return result


def _encode_source(source: EnergySourceIdentity) -> dict[str, object]:
    """Encode stable role and registry identity."""
    return {"role": source.role, "registry_id": source.registry_id}


def _decode_source(value: object, *, path: str) -> EnergySourceIdentity:
    """Decode a canonical source identity."""
    payload = _as_object(value, path=path, keys=_SOURCE_KEYS)
    role = _as_nonempty_string(payload["role"], path=f"{path}.role")
    registry_id = _as_nonempty_string(
        payload["registry_id"], path=f"{path}.registry_id"
    )
    return EnergySourceIdentity(role=role, registry_id=registry_id)


def _encode_sample(sample: EnergyCounterSample) -> dict[str, object]:
    """Encode an exact cumulative counter sample."""
    return {
        "source": _encode_source(sample.source),
        "cumulative_kwh": _encode_fraction(sample.cumulative.kwh),
        "source_unit": sample.source_unit.value,
        "period_end": _encode_utc(sample.period_end),
        "last_reported": _encode_utc(sample.last_reported),
    }


def _decode_sample(
    value: object,
    *,
    path: str,
    known_sources: dict[EnergySourceIdentity, EnergySourceIdentity],
) -> EnergyCounterSample:
    """Decode one sample and bind it to a configured source identity."""
    payload = _as_object(value, path=path, keys=_SAMPLE_KEYS)
    decoded_source = _decode_source(payload["source"], path=f"{path}.source")
    try:
        source = known_sources[decoded_source]
    except KeyError as err:
        _raise_codec(path, "source is not present in configured sources", cause=err)

    unit_path = f"{path}.source_unit"
    unit_value = payload["source_unit"]
    if type(unit_value) is not str:
        _raise_codec(unit_path, "must be a string")
    try:
        source_unit = EnergyUnit(unit_value)
    except ValueError as err:
        _raise_codec(unit_path, "must be Wh, kWh, or MWh", cause=err)

    cumulative_path = f"{path}.cumulative_kwh"
    cumulative_fraction = _decode_fraction(
        payload["cumulative_kwh"], path=cumulative_path
    )
    try:
        cumulative = Energy(cumulative_fraction)
    except ValueError as err:
        _raise_codec(cumulative_path, "must not be negative", cause=err)

    period_end = _decode_utc(payload["period_end"], path=f"{path}.period_end")
    last_reported = _decode_utc(payload["last_reported"], path=f"{path}.last_reported")
    if period_end > last_reported:
        _raise_codec(path, "period_end must not follow last_reported")
    if last_reported - period_end > timedelta(seconds=60):
        _raise_codec(path, "publication delay must not exceed 60 seconds")

    return EnergyCounterSample(
        source=source,
        cumulative=cumulative,
        source_unit=source_unit,
        period_end=period_end,
        last_reported=last_reported,
    )


def _encode_snapshot(snapshot: CounterSnapshot) -> dict[str, object]:
    """Encode one complete accepted counter vector."""
    return {"samples": [_encode_sample(sample) for sample in snapshot.samples]}


def _decode_snapshot(
    value: object,
    *,
    path: str,
    known_sources: dict[EnergySourceIdentity, EnergySourceIdentity],
) -> CounterSnapshot:
    """Decode one complete accepted counter vector."""
    payload = _as_object(value, path=path, keys=_SNAPSHOT_KEYS)
    samples = tuple(
        _decode_sample(
            sample,
            path=f"{path}.samples[{index}]",
            known_sources=known_sources,
        )
        for index, sample in enumerate(
            _as_list(payload["samples"], path=f"{path}.samples")
        )
    )
    try:
        snapshot = CounterSnapshot(samples=samples)
    except (TypeError, ValueError) as err:
        _raise_codec(path, str(err), cause=err)
    return snapshot


def _encode_candidate(candidate: CandidateBuffer) -> dict[str, object]:
    """Encode one persisted partial candidate."""
    return {
        "period_end": _encode_utc(candidate.period_end),
        "samples": [_encode_sample(sample) for sample in candidate.samples],
    }


def _decode_candidate(
    value: object,
    *,
    path: str,
    known_sources: dict[EnergySourceIdentity, EnergySourceIdentity],
) -> CandidateBuffer:
    """Decode one persisted partial candidate."""
    payload = _as_object(value, path=path, keys=_CANDIDATE_KEYS)
    period_end = _decode_utc(payload["period_end"], path=f"{path}.period_end")
    samples = tuple(
        _decode_sample(
            sample,
            path=f"{path}.samples[{index}]",
            known_sources=known_sources,
        )
        for index, sample in enumerate(
            _as_list(payload["samples"], path=f"{path}.samples")
        )
    )
    try:
        return CandidateBuffer(period_end=period_end, samples=samples)
    except (TypeError, ValueError) as err:
        _raise_codec(path, str(err), cause=err)


def encode_measurement_state(state: MeasurementPipelineState) -> dict[str, object]:
    """Encode all restart-critical measurement state without precision loss."""
    if not isinstance(state, MeasurementPipelineState):
        message = "state must be MeasurementPipelineState"
        raise MeasurementStateCodecError(message)
    return {
        "revision": state.revision,
        "phase": state.phase.value,
        "sources": [_encode_source(source) for source in state.sources],
        "segment_transition_at": _encode_utc(state.segment_transition_at),
        "baseline": (
            _encode_snapshot(state.baseline) if state.baseline is not None else None
        ),
        "candidate": (
            _encode_candidate(state.candidate) if state.candidate is not None else None
        ),
        "recovery_after_period_end": (
            _encode_utc(state.recovery_after_period_end)
            if state.recovery_after_period_end is not None
            else None
        ),
    }


def decode_measurement_state(value: object) -> MeasurementPipelineState:
    """Decode and fully validate restart-critical measurement state."""
    payload = _as_object(value, path="state", keys=_STATE_KEYS)
    revision = _as_nonnegative_int(payload["revision"], path="state.revision")

    phase_value = payload["phase"]
    if type(phase_value) is not str:
        _raise_codec("state.phase", "must be a string")
    try:
        phase = MeasurementPhase(phase_value)
    except ValueError as err:
        _raise_codec("state.phase", "is not a supported phase", cause=err)

    source_values = _as_list(payload["sources"], path="state.sources")
    sources = tuple(
        _decode_source(source, path=f"state.sources[{index}]")
        for index, source in enumerate(source_values)
    )
    if not sources:
        _raise_codec("state.sources", "must not be empty")
    roles = [source.role for source in sources]
    registry_ids = [source.registry_id for source in sources]
    if len(roles) != len(set(roles)):
        _raise_codec("state.sources", "roles must be unique")
    if len(registry_ids) != len(set(registry_ids)):
        _raise_codec("state.sources", "registry IDs must be unique")
    known_sources = {source: source for source in sources}

    segment_transition_at = _decode_utc(
        payload["segment_transition_at"], path="state.segment_transition_at"
    )
    baseline_value = payload["baseline"]
    baseline = (
        None
        if baseline_value is None
        else _decode_snapshot(
            baseline_value,
            path="state.baseline",
            known_sources=known_sources,
        )
    )
    candidate_value = payload["candidate"]
    candidate = (
        None
        if candidate_value is None
        else _decode_candidate(
            candidate_value,
            path="state.candidate",
            known_sources=known_sources,
        )
    )
    recovery_value = payload["recovery_after_period_end"]
    recovery_after_period_end = (
        None
        if recovery_value is None
        else _decode_utc(
            recovery_value,
            path="state.recovery_after_period_end",
        )
    )

    try:
        state = MeasurementPipelineState(
            revision=revision,
            phase=phase,
            sources=sources,
            segment_transition_at=segment_transition_at,
            baseline=baseline,
            candidate=candidate,
            recovery_after_period_end=recovery_after_period_end,
        )
    except (TypeError, ValueError) as err:
        _raise_codec("state", str(err), cause=err)

    return state


class _StrictAtomicStore(Store[dict[str, object]]):
    """Home Assistant Store which rejects every version mismatch."""

    async def _async_migrate_func(
        self,
        old_major_version: int,
        old_minor_version: int,
        old_data: dict[str, object],
    ) -> dict[str, object]:
        """Reject migration because Issue #4 defines no migration path."""
        del old_data
        message = (
            f"store version {old_major_version}.{old_minor_version} is not "
            f"supported; expected {_STORE_MAJOR_VERSION}.{_STORE_MINOR_VERSION}"
        )
        raise VerifiedAtomicStoreVersionError(message)


class VerifiedAtomicStore[T]:
    """Serialize and verify complete-state mutations at one physical Store key."""

    def __init__(
        self,
        hass: HomeAssistant,
        store_key: str,
        *,
        codec: StateCodec[T],
        revision_policy: RevisionPolicy[T],
    ) -> None:
        """Bind a complete-state codec and policy to one injected physical key."""
        if (
            type(store_key) is not str
            or not store_key
            or store_key != store_key.strip()
        ):
            message = "store_key must be a non-empty canonical string"
            raise ValueError(message)
        self._hass = hass
        self._codec = codec
        self._revision_policy = revision_policy
        self.store_key = store_key
        locks = hass.data.setdefault(_STORE_LOCKS, {})
        self._lock = locks.setdefault(store_key, asyncio.Lock())

    def _new_store(self) -> _StrictAtomicStore:
        """Return a fresh, strict and atomically writing Store instance."""
        return _StrictAtomicStore(
            self._hass,
            _STORE_MAJOR_VERSION,
            self.store_key,
            atomic_writes=True,
            max_readable_version=_STORE_MAJOR_VERSION,
            minor_version=_STORE_MINOR_VERSION,
        )

    async def _load_payload_unlocked(self) -> object | None:
        """Load one raw payload without interpreting absence as initialization."""
        try:
            return await self._new_store().async_load()
        except UnsupportedStorageVersionError as err:
            message = (
                "store major version exceeds the maximum readable version "
                f"{_STORE_MAJOR_VERSION}"
            )
            raise VerifiedAtomicStoreVersionError(message) from err

    def _revision(self, state: T) -> int:
        """Read one valid revision from the injected policy."""
        revision = self._revision_policy.revision(state)
        if type(revision) is not int or revision < 0:
            message = "revision policy must return a non-negative integer"
            raise VerifiedAtomicStoreVerificationError(message)
        return revision

    def _decode_canonical(self, payload: object) -> T:
        """Decode a payload and require its exact canonical representation."""
        unchanged_payload = deepcopy(payload)
        state = self._codec.decode(deepcopy(unchanged_payload))
        canonical_payload = self._codec.encode(state)
        if type(canonical_payload) is not dict:
            message = "state codec must encode a JSON object"
            raise VerifiedAtomicStoreVerificationError(message)
        if canonical_payload != unchanged_payload:
            message = "loaded state payload is not canonical"
            raise VerifiedAtomicStoreVerificationError(message)
        round_tripped = self._codec.decode(deepcopy(canonical_payload))
        if round_tripped != state:
            message = "loaded state does not round-trip through its codec"
            raise VerifiedAtomicStoreVerificationError(message)
        if self._revision(round_tripped) != self._revision(state):
            message = "loaded state revision changes during codec round-trip"
            raise VerifiedAtomicStoreVerificationError(message)
        return state

    def _preflight(self, state: T) -> tuple[dict[str, object], T, int]:
        """Prove exact codec round-trip and revision stability before saving."""
        expected_revision = self._revision(state)
        payload = self._codec.encode(state)
        if type(payload) is not dict:
            message = "state codec must encode a JSON object"
            raise VerifiedAtomicStoreVerificationError(message)
        unchanged_payload = deepcopy(payload)
        canonical_state = self._codec.decode(deepcopy(unchanged_payload))
        if canonical_state != state:
            message = "state does not round-trip through its codec before save"
            raise VerifiedAtomicStoreVerificationError(message)
        if self._revision(canonical_state) != expected_revision:
            message = "state revision changes during codec preflight"
            raise VerifiedAtomicStoreVerificationError(message)
        if self._codec.encode(canonical_state) != unchanged_payload:
            message = "state codec does not emit a stable canonical payload"
            raise VerifiedAtomicStoreVerificationError(message)
        return unchanged_payload, canonical_state, expected_revision

    async def _save_and_verify_unlocked(
        self,
        expected_payload: dict[str, object],
        expected_state: T,
        expected_revision: int,
    ) -> T:
        """Immediately save and compare a fresh full-state Store read-back."""
        if self._hass.state is CoreState.stopping:
            message = "state cannot be saved while Home Assistant stops"
            raise VerifiedAtomicStoreError(message)
        await self._new_store().async_save(expected_payload)
        actual_payload = await self._load_payload_unlocked()
        if actual_payload is None:
            message = "state was absent after save"
            raise VerifiedAtomicStoreVerificationError(message)
        if actual_payload != expected_payload:
            message = "full-state read-back differs from the saved payload"
            raise VerifiedAtomicStoreVerificationError(message)
        actual_state = self._decode_canonical(actual_payload)
        if (
            self._revision(actual_state) != expected_revision
            or actual_state != expected_state
        ):
            message = "full-state read-back differs from the saved revision"
            raise VerifiedAtomicStoreVerificationError(message)
        return actual_state

    async def async_load(self) -> T | None:
        """Load a state, returning ``None`` without creating a replacement."""
        async with self._lock:
            payload = await self._load_payload_unlocked()
            return None if payload is None else self._decode_canonical(payload)

    async def async_initialize_confirmed_absent(
        self,
        state: T,
    ) -> T:
        """Initialize only after Issue #8 confirms physical key absence.

        A preceding :meth:`async_load` result of ``None`` does not prove physical
        absence. The caller must first check the main and ``.corrupt`` keys under
        the Issue #8 bootstrap reservation lock.
        """
        async with self._lock:
            if await self._load_payload_unlocked() is not None:
                message = "store already exists"
                raise VerifiedAtomicStoreConflictError(message)
            self._revision_policy.validate_initial(state)
            payload, canonical_state, revision = self._preflight(state)
            return await self._save_and_verify_unlocked(
                payload,
                canonical_state,
                revision,
            )

    async def async_transact(
        self,
        transform: Callable[[T], T],
    ) -> T:
        """Apply one pure synchronous transition and publish only after verify."""
        async with self._lock:
            current_payload = await self._load_payload_unlocked()
            if current_payload is None:
                message = "store is missing; initialize it explicitly"
                raise VerifiedAtomicStoreConflictError(message)
            unchanged_payload = deepcopy(current_payload)
            before = self._decode_canonical(deepcopy(unchanged_payload))
            transform_input = self._decode_canonical(deepcopy(unchanged_payload))
            proposed_object: object = transform(transform_input)
            if inspect.isawaitable(proposed_object):
                if inspect.iscoroutine(proposed_object):
                    proposed_object.close()
                message = "transform must be synchronous"
                raise TypeError(message)
            proposed = cast("T", proposed_object)
            if self._codec.encode(transform_input) != unchanged_payload:
                message = "transform must not mutate its input state"
                raise VerifiedAtomicStoreConflictError(message)
            before_revision = self._revision(before)
            proposed_revision = self._revision(proposed)
            if proposed == before and proposed_revision == before_revision:
                return before
            if proposed_revision != before_revision + 1:
                message = (
                    f"state revision must be {before_revision + 1}, "
                    f"got {proposed_revision}"
                )
                raise VerifiedAtomicStoreConflictError(message)
            self._revision_policy.validate_transition(before, proposed)
            payload, canonical_state, revision = self._preflight(proposed)
            return await self._save_and_verify_unlocked(
                payload,
                canonical_state,
                revision,
            )


__all__ = (
    "MeasurementStateCodecError",
    "RevisionPolicy",
    "StateCodec",
    "VerifiedAtomicStore",
    "VerifiedAtomicStoreConflictError",
    "VerifiedAtomicStoreError",
    "VerifiedAtomicStoreVerificationError",
    "VerifiedAtomicStoreVersionError",
    "decode_measurement_state",
    "encode_measurement_state",
)
