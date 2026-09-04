# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Immutable models for cumulative energy measurement intake."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Protocol

from custom_components.co2saver.domain import (
    Energy,
    IntervalRejectionReason,
    IntervalWindow,
    NormalizedInterval,
    RejectedInterval,
)

_MAX_PUBLICATION_DELAY = timedelta(seconds=60)


class MeasurementPhase(StrEnum):
    """Persisted phases of the cumulative-counter pipeline."""

    AWAITING_SEGMENT_BASELINE = "awaiting_segment_baseline"
    ACTIVE = "active"
    AWAITING_REBASELINE = "awaiting_rebaseline"


class EnergyUnit(StrEnum):
    """Supported cumulative-counter units at the Home Assistant boundary."""

    WATT_HOUR = "Wh"
    KILOWATT_HOUR = "kWh"
    MEGAWATT_HOUR = "MWh"


class MeasurementRejectionReason(StrEnum):
    """Fail-closed reasons reported by the measurement boundary."""

    SOURCE_MISSING = "source_missing"
    SOURCE_UNAVAILABLE = "source_unavailable"
    INVALID_VALUE = "invalid_value"
    INVALID_UNIT = "invalid_unit"
    UNIT_CHANGED = "unit_changed"
    INVALID_DEVICE_CLASS = "invalid_device_class"
    INVALID_STATE_CLASS = "invalid_state_class"
    INVALID_PERIOD_END = "invalid_period_end"
    INVALID_LAST_REPORTED = "invalid_last_reported"
    SOURCE_BINDING_MISMATCH = "source_binding_mismatch"
    INVALID_VECTOR = "invalid_vector"
    FUTURE_PERIOD_END = "future_period_end"
    FUTURE_LAST_REPORTED = "future_last_reported"
    PERIOD_AFTER_PUBLICATION = "period_after_publication"
    PUBLICATION_DELAY = "publication_delay"
    NEW_SAMPLE_STALE = "new_sample_stale"
    BASELINE_STALE = "baseline_stale"
    ACCEPTED_SAMPLE_CONFLICT = "accepted_sample_conflict"
    PERIOD_ROLLBACK = "period_rollback"
    CANDIDATE_SAMPLE_CONFLICT = "candidate_sample_conflict"
    CANDIDATE_PERIOD_MISMATCH = "candidate_period_mismatch"
    CANDIDATE_TIMEOUT = "candidate_timeout"
    CANDIDATE_STALE = "candidate_stale"
    PUBLICATION_NOT_NEWER = "publication_not_newer"
    PUBLICATION_SKEW = "publication_skew"
    INTERVAL_TOO_LONG = "interval_too_long"
    COUNTER_RESET = "counter_reset"
    INTERVAL_REJECTED = "interval_rejected"


def _require_utc(value: datetime, *, field_name: str) -> None:
    """Require a timezone-aware UTC timestamp."""
    if not isinstance(value, datetime):
        message = f"{field_name} must be a datetime"
        raise TypeError(message)
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        message = f"{field_name} must be a timezone-aware UTC timestamp"
        raise ValueError(message)


@dataclass(frozen=True, slots=True)
class EnergySourceIdentity:
    """Stable role and entity-registry identity of one cumulative counter."""

    role: str
    registry_id: str

    def __post_init__(self) -> None:
        """Reject identities that cannot be persisted unambiguously."""
        if (
            type(self.role) is not str
            or not self.role
            or self.role != self.role.strip()
        ):
            message = "source role must be non-empty without surrounding whitespace"
            raise ValueError(message)
        if (
            type(self.registry_id) is not str
            or not self.registry_id
            or self.registry_id != self.registry_id.strip()
        ):
            message = (
                "source registry_id must be non-empty without surrounding whitespace"
            )
            raise ValueError(message)


def _validate_source_identities(sources: tuple[EnergySourceIdentity, ...]) -> None:
    """Require at least one role and one-to-one source ownership."""
    if not sources:
        message = "at least one energy source is required"
        raise ValueError(message)
    if any(not isinstance(source, EnergySourceIdentity) for source in sources):
        message = "every source must be an EnergySourceIdentity"
        raise TypeError(message)
    roles = [source.role for source in sources]
    registry_ids = [source.registry_id for source in sources]
    if len(roles) != len(set(roles)):
        message = "energy source roles must be unique"
        raise ValueError(message)
    if len(registry_ids) != len(set(registry_ids)):
        message = "energy source registry ids must be unique"
        raise ValueError(message)


@dataclass(frozen=True, slots=True)
class EnergyCounterSample:
    """One normalized cumulative counter sample in exact kWh."""

    source: EnergySourceIdentity
    cumulative: Energy
    source_unit: EnergyUnit
    period_end: datetime
    last_reported: datetime

    def __post_init__(self) -> None:
        """Keep quantity, unit identity, and timestamps canonical."""
        if not isinstance(self.source, EnergySourceIdentity):
            message = "sample source must be an EnergySourceIdentity"
            raise TypeError(message)
        if not isinstance(self.cumulative, Energy):
            message = "sample cumulative value must be exact Energy"
            raise TypeError(message)
        if not isinstance(self.source_unit, EnergyUnit):
            message = "source_unit must be a supported EnergyUnit"
            raise TypeError(message)
        _require_utc(self.period_end, field_name="period_end")
        _require_utc(self.last_reported, field_name="last_reported")


@dataclass(frozen=True, slots=True)
class InvalidEnergySample:
    """A source observation that failed adapter validation."""

    source: EnergySourceIdentity
    reason: MeasurementRejectionReason

    def __post_init__(self) -> None:
        """Require typed source ownership and rejection semantics."""
        if not isinstance(self.source, EnergySourceIdentity):
            message = "invalid sample source must be an EnergySourceIdentity"
            raise TypeError(message)
        if not isinstance(self.reason, MeasurementRejectionReason):
            message = "invalid sample reason must be a MeasurementRejectionReason"
            raise TypeError(message)


type EnergyObservation = EnergyCounterSample | InvalidEnergySample


def _validate_snapshot_sample_times(
    samples: tuple[EnergyCounterSample, ...],
) -> None:
    """Require the intrinsic timestamp shape of an accepted full vector."""
    for sample in samples:
        _validate_sample_time_shape(sample)
    publications = [sample.last_reported for sample in samples]
    if max(publications) - min(publications) > _MAX_PUBLICATION_DELAY:
        message = "snapshot publication skew must not exceed 60 seconds"
        raise ValueError(message)


def _validate_sample_time_shape(sample: EnergyCounterSample) -> None:
    """Require period/publication ordering that is independent of poll time."""
    if sample.period_end > sample.last_reported:
        message = "sample period_end must not follow last_reported"
        raise ValueError(message)
    if sample.last_reported - sample.period_end > _MAX_PUBLICATION_DELAY:
        message = "sample publication delay must not exceed 60 seconds"
        raise ValueError(message)


@dataclass(frozen=True, slots=True)
class CounterSnapshot:
    """A complete, synchronized cumulative-counter vector."""

    samples: tuple[EnergyCounterSample, ...]

    def __post_init__(self) -> None:
        """Require a non-empty vector with one common physical period."""
        object.__setattr__(self, "samples", tuple(self.samples))
        if not self.samples:
            message = "counter snapshot must not be empty"
            raise ValueError(message)
        if any(not isinstance(sample, EnergyCounterSample) for sample in self.samples):
            message = "counter snapshot entries must be EnergyCounterSample"
            raise TypeError(message)
        sources = [sample.source for sample in self.samples]
        if len(sources) != len(set(sources)):
            message = "counter snapshot sources must be unique"
            raise ValueError(message)
        periods = {sample.period_end for sample in self.samples}
        if len(periods) != 1:
            message = "counter snapshot samples must share one period_end"
            raise ValueError(message)
        _validate_snapshot_sample_times(self.samples)

    @property
    def period_end(self) -> datetime:
        """Return the common physical measurement-period end."""
        return self.samples[0].period_end


@dataclass(frozen=True, slots=True)
class CandidateBuffer:
    """An immutable partial vector for exactly one newer period."""

    period_end: datetime
    samples: tuple[EnergyCounterSample, ...]

    def __post_init__(self) -> None:
        """Require unique samples that all belong to the buffer period."""
        _require_utc(self.period_end, field_name="candidate period_end")
        object.__setattr__(self, "samples", tuple(self.samples))
        if not self.samples:
            message = "candidate buffer must not be empty"
            raise ValueError(message)
        if any(not isinstance(sample, EnergyCounterSample) for sample in self.samples):
            message = "candidate entries must be EnergyCounterSample"
            raise TypeError(message)
        sources = [sample.source for sample in self.samples]
        if len(sources) != len(set(sources)):
            message = "candidate buffer sources must be unique"
            raise ValueError(message)
        if any(sample.period_end != self.period_end for sample in self.samples):
            message = "candidate samples must match candidate period_end"
            raise ValueError(message)
        for sample in self.samples:
            _validate_sample_time_shape(sample)


def _validate_state_membership(
    sources: tuple[EnergySourceIdentity, ...],
    baseline: CounterSnapshot | None,
    candidate: CandidateBuffer | None,
) -> None:
    """Require persisted vectors to use exactly the configured identities."""
    expected_sources = set(sources)
    if baseline is not None:
        baseline_sources = {sample.source for sample in baseline.samples}
        if baseline_sources != expected_sources:
            message = "baseline must contain every configured source exactly once"
            raise ValueError(message)
    if candidate is None:
        return
    candidate_sources = {sample.source for sample in candidate.samples}
    if not candidate_sources <= expected_sources:
        message = "candidate contains an unconfigured source"
        raise ValueError(message)
    if candidate_sources == expected_sources:
        message = "a complete candidate must be accepted in the same transition"
        raise ValueError(message)


def _validate_phase_state(state: MeasurementPipelineState) -> None:
    """Require baseline and recovery fields appropriate to the current phase."""
    if state.phase is MeasurementPhase.AWAITING_SEGMENT_BASELINE:
        if state.baseline is not None or state.recovery_after_period_end is not None:
            message = "initial baseline phase cannot retain an old baseline"
            raise ValueError(message)
        return
    if state.phase is MeasurementPhase.ACTIVE:
        if state.baseline is None or state.recovery_after_period_end is not None:
            message = "active phase requires exactly one current baseline"
            raise ValueError(message)
        return
    if state.baseline is None or state.recovery_after_period_end is None:
        message = "recovery phase requires an old baseline and recovery barrier"
        raise ValueError(message)
    _require_utc(
        state.recovery_after_period_end,
        field_name="recovery_after_period_end",
    )
    if state.recovery_after_period_end != state.baseline.period_end:
        message = "recovery barrier must equal the last accepted period_end"
        raise ValueError(message)


def _validate_candidate_position(state: MeasurementPipelineState) -> None:
    """Require a partial candidate strictly inside the current phase window."""
    if (
        state.baseline is not None
        and state.baseline.period_end < state.segment_transition_at
    ):
        message = "baseline cannot precede segment_transition_at"
        raise ValueError(message)
    if state.candidate is None:
        return
    if (
        state.phase is MeasurementPhase.AWAITING_SEGMENT_BASELINE
        and state.candidate.period_end < state.segment_transition_at
    ):
        message = "segment candidate cannot precede segment_transition_at"
        raise ValueError(message)
    if (
        state.phase is MeasurementPhase.ACTIVE
        and state.baseline is not None
        and state.candidate.period_end <= state.baseline.period_end
    ):
        message = "active candidate must be newer than the baseline"
        raise ValueError(message)
    if state.phase is MeasurementPhase.ACTIVE and state.baseline is not None:
        baseline_by_source = {
            sample.source: sample for sample in state.baseline.samples
        }
        if any(
            sample.source_unit is not baseline_by_source[sample.source].source_unit
            for sample in state.candidate.samples
        ):
            message = "active candidate units must match the baseline"
            raise ValueError(message)
    if (
        state.phase is MeasurementPhase.AWAITING_REBASELINE
        and state.recovery_after_period_end is not None
        and state.candidate.period_end <= state.recovery_after_period_end
    ):
        message = "recovery candidate must be newer than its barrier"
        raise ValueError(message)


@dataclass(frozen=True, slots=True)
class MeasurementPipelineState:
    """All measurement state required for deterministic restart recovery."""

    revision: int
    phase: MeasurementPhase
    sources: tuple[EnergySourceIdentity, ...]
    segment_transition_at: datetime
    baseline: CounterSnapshot | None = None
    candidate: CandidateBuffer | None = None
    recovery_after_period_end: datetime | None = None

    def __post_init__(self) -> None:
        """Validate phase-specific fail-closed state invariants."""
        object.__setattr__(self, "sources", tuple(self.sources))
        if type(self.revision) is not int or self.revision < 0:
            message = "measurement revision must be a non-negative integer"
            raise ValueError(message)
        if not isinstance(self.phase, MeasurementPhase):
            message = "phase must be a MeasurementPhase"
            raise TypeError(message)
        _validate_source_identities(self.sources)
        _require_utc(self.segment_transition_at, field_name="segment_transition_at")
        if self.baseline is not None and not isinstance(self.baseline, CounterSnapshot):
            message = "baseline must be a CounterSnapshot"
            raise TypeError(message)
        if self.candidate is not None and not isinstance(
            self.candidate,
            CandidateBuffer,
        ):
            message = "candidate must be a CandidateBuffer"
            raise TypeError(message)
        _validate_state_membership(self.sources, self.baseline, self.candidate)
        _validate_phase_state(self)
        _validate_candidate_position(self)

    @classmethod
    def initial(
        cls,
        sources: tuple[EnergySourceIdentity, ...],
        segment_transition_at: datetime,
    ) -> MeasurementPipelineState:
        """Create an uninitialized, persistable measurement state."""
        return cls(
            revision=0,
            phase=MeasurementPhase.AWAITING_SEGMENT_BASELINE,
            sources=tuple(sources),
            segment_transition_at=segment_transition_at,
        )


@dataclass(frozen=True, slots=True)
class EnergyDelta:
    """Exact interval delta owned by one configured source."""

    source: EnergySourceIdentity
    energy: Energy

    def __post_init__(self) -> None:
        """Require explicit source ownership and exact non-negative energy."""
        if not isinstance(self.source, EnergySourceIdentity):
            message = "delta source must be an EnergySourceIdentity"
            raise TypeError(message)
        if not isinstance(self.energy, Energy):
            message = "delta energy must be exact Energy"
            raise TypeError(message)


@dataclass(frozen=True, slots=True)
class RawEnergyDeltaBatch:
    """Complete source-owned deltas awaiting topology-specific assembly."""

    window: IntervalWindow
    deltas: tuple[EnergyDelta, ...]

    def __post_init__(self) -> None:
        """Require an immutable batch with unique role and registry ownership."""
        if not isinstance(self.window, IntervalWindow):
            message = "raw delta window must be an IntervalWindow"
            raise TypeError(message)
        object.__setattr__(self, "deltas", tuple(self.deltas))
        if not self.deltas:
            message = "raw energy delta batch must not be empty"
            raise ValueError(message)
        if any(not isinstance(delta, EnergyDelta) for delta in self.deltas):
            message = "raw delta entries must be EnergyDelta"
            raise TypeError(message)
        sources = tuple(delta.source for delta in self.deltas)
        _validate_source_identities(sources)

    def energy_for(self, role: str) -> Energy:
        """Return one role's exact interval energy."""
        for delta in self.deltas:
            if delta.source.role == role:
                return delta.energy
        message = f"unknown energy role: {role}"
        raise KeyError(message)


class IntervalAssembler(Protocol):
    """Build one topology-specific domain interval from exact source deltas."""

    def __call__(
        self,
        batch: RawEnergyDeltaBatch,
        /,
    ) -> NormalizedInterval | RejectedInterval:
        """Assemble or conservatively reject one complete raw delta batch."""
        ...


@dataclass(frozen=True, slots=True)
class MeasurementFault:
    """One fail-closed observation or timeline failure."""

    reason: MeasurementRejectionReason
    source: EnergySourceIdentity | None = None
    interval_reason: IntervalRejectionReason | None = None

    def __post_init__(self) -> None:
        """Keep boundary and optional domain rejection reasons consistent."""
        if not isinstance(self.reason, MeasurementRejectionReason):
            message = "fault reason must be a MeasurementRejectionReason"
            raise TypeError(message)
        if self.source is not None and not isinstance(
            self.source,
            EnergySourceIdentity,
        ):
            message = "fault source must be an EnergySourceIdentity"
            raise TypeError(message)
        if self.reason is MeasurementRejectionReason.INTERVAL_REJECTED:
            if not isinstance(self.interval_reason, IntervalRejectionReason):
                message = "interval rejection fault requires its domain reason"
                raise ValueError(message)
        elif self.interval_reason is not None:
            message = "only interval rejection faults may carry a domain reason"
            raise ValueError(message)


@dataclass(frozen=True, slots=True)
class MeasurementTransition:
    """Pure result of advancing one immutable observation vector."""

    state: MeasurementPipelineState
    interval: NormalizedInterval | None = None
    fault: MeasurementFault | None = None
    interruption_started: bool = False

    def __post_init__(self) -> None:
        """Reject mutually inconsistent reducer outputs."""
        if not isinstance(self.state, MeasurementPipelineState):
            message = "transition state must be a MeasurementPipelineState"
            raise TypeError(message)
        if self.interval is not None and not isinstance(
            self.interval,
            NormalizedInterval,
        ):
            message = "transition interval must be a NormalizedInterval"
            raise TypeError(message)
        if self.fault is not None and not isinstance(self.fault, MeasurementFault):
            message = "transition fault must be a MeasurementFault"
            raise TypeError(message)
        if self.interval is not None and self.fault is not None:
            message = "a transition cannot emit an interval and a fault"
            raise ValueError(message)
        if type(self.interruption_started) is not bool:
            message = "interruption_started must be bool"
            raise TypeError(message)
        if self.interruption_started and self.fault is None:
            message = "an interruption marker requires a fault"
            raise ValueError(message)


__all__ = (
    "CandidateBuffer",
    "CounterSnapshot",
    "EnergyCounterSample",
    "EnergyDelta",
    "EnergyObservation",
    "EnergySourceIdentity",
    "EnergyUnit",
    "IntervalAssembler",
    "InvalidEnergySample",
    "MeasurementFault",
    "MeasurementPhase",
    "MeasurementPipelineState",
    "MeasurementRejectionReason",
    "MeasurementTransition",
    "RawEnergyDeltaBatch",
)
