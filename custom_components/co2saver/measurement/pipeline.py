# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Pure cumulative-counter state machine for normalized energy intervals."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from custom_components.co2saver.domain import (
    Energy,
    IntervalWindow,
    NormalizedInterval,
    RejectedInterval,
)
from custom_components.co2saver.measurement.models import (
    CandidateBuffer,
    CounterSnapshot,
    EnergyCounterSample,
    EnergyDelta,
    EnergyObservation,
    EnergySourceIdentity,
    IntervalAssembler,
    InvalidEnergySample,
    MeasurementFault,
    MeasurementPhase,
    MeasurementPipelineState,
    MeasurementRejectionReason,
    MeasurementTransition,
    RawEnergyDeltaBatch,
)

_MAX_PUBLICATION_DELAY = timedelta(seconds=60)
_MAX_NEW_SAMPLE_AGE = timedelta(seconds=300)
_MAX_BASELINE_AGE = timedelta(seconds=360)
_MAX_CANDIDATE_WINDOW = timedelta(seconds=60)
_MAX_INTERVAL_DURATION = timedelta(seconds=900)


def _fault(
    reason: MeasurementRejectionReason,
    source: EnergySourceIdentity | None = None,
) -> MeasurementFault:
    """Construct a typed fail-closed fault."""
    return MeasurementFault(reason=reason, source=source)


def _interval_fault(rejected: RejectedInterval) -> MeasurementFault:
    """Preserve one topology normalizer's exact rejection reason."""
    return MeasurementFault(
        reason=MeasurementRejectionReason.INTERVAL_REJECTED,
        interval_reason=rejected.reason,
    )


def _reject(
    state: MeasurementPipelineState,
    fault: MeasurementFault,
) -> MeasurementTransition:
    """Enter recovery once, or clear an invalid waiting candidate."""
    if state.phase is MeasurementPhase.ACTIVE:
        if state.baseline is None:  # pragma: no cover - guarded by model invariant
            message = "active measurement state has no baseline"
            raise RuntimeError(message)
        interrupted = replace(
            state,
            revision=state.revision + 1,
            phase=MeasurementPhase.AWAITING_REBASELINE,
            candidate=None,
            recovery_after_period_end=state.baseline.period_end,
        )
        return MeasurementTransition(
            state=interrupted,
            fault=fault,
            interruption_started=True,
        )

    if state.candidate is None:
        return MeasurementTransition(state=state, fault=fault)
    return MeasurementTransition(
        state=replace(
            state,
            revision=state.revision + 1,
            candidate=None,
        ),
        fault=fault,
    )


def _as_observation_map(
    state: MeasurementPipelineState,
    observations: tuple[EnergyObservation, ...],
) -> tuple[
    dict[EnergySourceIdentity, EnergyObservation] | None,
    MeasurementFault | None,
]:
    """Validate exact vector membership without trusting caller ordering."""
    expected = set(state.sources)
    observed: dict[EnergySourceIdentity, EnergyObservation] = {}
    for observation in observations:
        if not isinstance(observation, (EnergyCounterSample, InvalidEnergySample)):
            return None, _fault(MeasurementRejectionReason.INVALID_VECTOR)
        if observation.source not in expected or observation.source in observed:
            return None, _fault(
                MeasurementRejectionReason.INVALID_VECTOR,
                observation.source,
            )
        observed[observation.source] = observation
    if set(observed) != expected:
        return None, _fault(MeasurementRejectionReason.INVALID_VECTOR)
    return observed, None


def _validate_sample_time(
    sample: EnergyCounterSample,
    observed_at: datetime,
) -> MeasurementFault | None:
    """Apply per-sample temporal validity before age classification."""
    if sample.period_end > observed_at:
        return _fault(MeasurementRejectionReason.FUTURE_PERIOD_END, sample.source)
    if sample.last_reported > observed_at:
        return _fault(MeasurementRejectionReason.FUTURE_LAST_REPORTED, sample.source)
    if sample.period_end > sample.last_reported:
        return _fault(
            MeasurementRejectionReason.PERIOD_AFTER_PUBLICATION,
            sample.source,
        )
    if sample.last_reported - sample.period_end > _MAX_PUBLICATION_DELAY:
        return _fault(MeasurementRejectionReason.PUBLICATION_DELAY, sample.source)
    return None


def _is_fresh_new_sample(
    sample: EnergyCounterSample,
    observed_at: datetime,
) -> bool:
    """Return whether a strictly new sample meets both five-minute ages."""
    return (
        observed_at - sample.period_end <= _MAX_NEW_SAMPLE_AGE
        and observed_at - sample.last_reported <= _MAX_NEW_SAMPLE_AGE
    )


def _candidate_expiry_fault(
    candidate: CandidateBuffer,
    observed_at: datetime,
) -> MeasurementFault | None:
    """Check persisted partial-buffer deadlines with inclusive boundaries."""
    if candidate.period_end > observed_at:
        return _fault(MeasurementRejectionReason.FUTURE_PERIOD_END)
    for sample in candidate.samples:
        if sample.last_reported > observed_at:
            return _fault(
                MeasurementRejectionReason.FUTURE_LAST_REPORTED,
                sample.source,
            )
    if observed_at - candidate.period_end > _MAX_NEW_SAMPLE_AGE:
        return _fault(MeasurementRejectionReason.CANDIDATE_STALE)
    earliest_publication = min(sample.last_reported for sample in candidate.samples)
    if observed_at - earliest_publication > _MAX_CANDIDATE_WINDOW:
        return _fault(MeasurementRejectionReason.CANDIDATE_TIMEOUT)
    return None


def _same_counter_reading(
    left: EnergyCounterSample,
    right: EnergyCounterSample,
) -> bool:
    """Compare immutable counter identity while ignoring repeat publication time."""
    return (
        left.source == right.source
        and left.cumulative == right.cumulative
        and left.source_unit == right.source_unit
        and left.period_end == right.period_end
    )


def _merge_candidate(
    state: MeasurementPipelineState,
    samples: tuple[EnergyCounterSample, ...],
    observed_at: datetime,
) -> tuple[CandidateBuffer | None, MeasurementFault | None, bool]:
    """Immutably admit same-period samples into the one allowed candidate."""
    candidate = state.candidate
    periods = {sample.period_end for sample in samples}
    if candidate is not None:
        periods.add(candidate.period_end)
    if len(periods) != 1:
        return (
            None,
            _fault(MeasurementRejectionReason.CANDIDATE_PERIOD_MISMATCH),
            False,
        )

    for sample in samples:
        if not _is_fresh_new_sample(sample, observed_at):
            return (
                None,
                _fault(MeasurementRejectionReason.NEW_SAMPLE_STALE, sample.source),
                False,
            )

    existing = (
        {sample.source: sample for sample in candidate.samples}
        if candidate is not None
        else {}
    )
    changed = candidate is None
    for sample in samples:
        prior = existing.get(sample.source)
        if prior is not None:
            if not _same_counter_reading(prior, sample):
                return (
                    None,
                    _fault(
                        MeasurementRejectionReason.CANDIDATE_SAMPLE_CONFLICT,
                        sample.source,
                    ),
                    False,
                )
            continue
        existing[sample.source] = sample
        changed = True

    period_end = next(iter(periods))
    ordered = tuple(existing[source] for source in state.sources if source in existing)
    return CandidateBuffer(period_end=period_end, samples=ordered), None, changed


def _candidate_freshness_fault(
    candidate: CandidateBuffer,
    observed_at: datetime,
) -> MeasurementFault | None:
    """Validate the inclusive age ceilings of a complete candidate."""
    if candidate.period_end > observed_at:
        return _fault(MeasurementRejectionReason.FUTURE_PERIOD_END)
    for sample in candidate.samples:
        if sample.last_reported > observed_at:
            return _fault(
                MeasurementRejectionReason.FUTURE_LAST_REPORTED,
                sample.source,
            )
    if observed_at - candidate.period_end > _MAX_NEW_SAMPLE_AGE:
        return _fault(MeasurementRejectionReason.CANDIDATE_STALE)
    for sample in candidate.samples:
        if observed_at - sample.last_reported > _MAX_NEW_SAMPLE_AGE:
            return _fault(
                MeasurementRejectionReason.CANDIDATE_STALE,
                sample.source,
            )

    return None


def _publication_skew_fault(
    candidate: CandidateBuffer,
) -> MeasurementFault | None:
    """Validate the inclusive publication-time skew ceiling."""
    publication_times = tuple(sample.last_reported for sample in candidate.samples)
    if max(publication_times) - min(publication_times) <= _MAX_PUBLICATION_DELAY:
        return None
    return _fault(MeasurementRejectionReason.PUBLICATION_SKEW)


def _publication_novelty_fault(
    baseline: CounterSnapshot,
    candidate: CandidateBuffer,
) -> MeasurementFault | None:
    """Require each new or recovery publication to follow its retained baseline."""
    baseline_by_source = {sample.source: sample for sample in baseline.samples}
    for sample in candidate.samples:
        if sample.last_reported <= baseline_by_source[sample.source].last_reported:
            return _fault(
                MeasurementRejectionReason.PUBLICATION_NOT_NEWER,
                sample.source,
            )
    return None


def _active_interval_fault(
    baseline: CounterSnapshot,
    candidate: CandidateBuffer,
) -> MeasurementFault | None:
    """Validate active-only duration and cumulative monotonicity."""
    duration = candidate.period_end - baseline.period_end
    if duration <= timedelta(0) or duration > _MAX_INTERVAL_DURATION:
        return _fault(MeasurementRejectionReason.INTERVAL_TOO_LONG)

    baseline_by_source = {sample.source: sample for sample in baseline.samples}
    for sample in candidate.samples:
        if sample.cumulative.kwh < baseline_by_source[sample.source].cumulative.kwh:
            return _fault(
                MeasurementRejectionReason.COUNTER_RESET,
                sample.source,
            )
    return None


def _complete_candidate_fault(
    state: MeasurementPipelineState,
    candidate: CandidateBuffer,
    observed_at: datetime,
) -> MeasurementFault | None:
    """Validate a complete synchronized batch in the ADR-prescribed order."""
    if fault := _candidate_freshness_fault(candidate, observed_at):
        return fault
    if fault := _publication_skew_fault(candidate):
        return fault
    if state.baseline is None:
        return None
    if fault := _publication_novelty_fault(state.baseline, candidate):
        return fault
    if state.phase is MeasurementPhase.AWAITING_REBASELINE:
        return None
    return _active_interval_fault(state.baseline, candidate)


def _ordered_candidate_samples(
    state: MeasurementPipelineState,
    candidate: CandidateBuffer,
) -> tuple[EnergyCounterSample, ...]:
    """Order a complete candidate by stable configured role order."""
    by_source = {sample.source: sample for sample in candidate.samples}
    return tuple(by_source[source] for source in state.sources)


def _accept_active_candidate(
    state: MeasurementPipelineState,
    candidate: CandidateBuffer,
    assemble_interval: IntervalAssembler,
) -> MeasurementTransition:
    """Assemble one exact interval and atomically advance its baseline."""
    if state.baseline is None:  # pragma: no cover - guarded by model invariant
        message = "active measurement state has no baseline"
        raise RuntimeError(message)
    previous = {sample.source: sample for sample in state.baseline.samples}
    ordered = _ordered_candidate_samples(state, candidate)
    raw_batch = RawEnergyDeltaBatch(
        window=IntervalWindow(
            start=state.baseline.period_end,
            end=candidate.period_end,
        ),
        deltas=tuple(
            EnergyDelta(
                source=sample.source,
                energy=Energy(
                    sample.cumulative.kwh - previous[sample.source].cumulative.kwh
                ),
            )
            for sample in ordered
        ),
    )
    interval = assemble_interval(raw_batch)
    if isinstance(interval, RejectedInterval):
        return _reject(state, _interval_fault(interval))
    if not isinstance(interval, NormalizedInterval):
        message = (
            "interval assembler must return NormalizedInterval or RejectedInterval"
        )
        raise TypeError(message)
    if interval.window != raw_batch.window:
        message = "assembled interval must preserve the raw delta window"
        raise ValueError(message)
    return MeasurementTransition(
        state=replace(
            state,
            revision=state.revision + 1,
            baseline=CounterSnapshot(ordered),
            candidate=None,
        ),
        interval=interval,
    )


def _classify_active_samples(
    baseline: CounterSnapshot,
    samples: tuple[EnergyCounterSample, ...],
) -> tuple[tuple[EnergyCounterSample, ...], MeasurementFault | None]:
    """Classify active observations before candidate age handling."""
    baseline_by_source = {sample.source: sample for sample in baseline.samples}
    newer: list[EnergyCounterSample] = []
    for sample in samples:
        baseline_sample = baseline_by_source[sample.source]
        if sample.period_end < baseline_sample.period_end:
            return (), _fault(
                MeasurementRejectionReason.PERIOD_ROLLBACK,
                sample.source,
            )
        if sample.period_end == baseline_sample.period_end:
            if not _same_counter_reading(sample, baseline_sample):
                return (), _fault(
                    MeasurementRejectionReason.ACCEPTED_SAMPLE_CONFLICT,
                    sample.source,
                )
            continue
        if sample.source_unit != baseline_sample.source_unit:
            return (), _fault(
                MeasurementRejectionReason.UNIT_CHANGED,
                sample.source,
            )
        newer.append(sample)
    return tuple(newer), None


def _idle_active_transition(
    state: MeasurementPipelineState,
    observed_at: datetime,
) -> MeasurementTransition:
    """Handle a poll containing no sample newer than the active baseline."""
    if state.candidate is not None:
        if expiry := _candidate_expiry_fault(state.candidate, observed_at):
            return _reject(state, expiry)
        return MeasurementTransition(state=state)
    if state.baseline is None:  # pragma: no cover - guarded by model invariant
        message = "active measurement state has no baseline"
        raise RuntimeError(message)
    if observed_at - state.baseline.period_end > _MAX_BASELINE_AGE:
        return _reject(
            state,
            _fault(MeasurementRejectionReason.BASELINE_STALE),
        )
    return MeasurementTransition(state=state)


def _resolve_active_candidate(
    state: MeasurementPipelineState,
    candidate: CandidateBuffer,
    observed_at: datetime,
    *,
    changed: bool,
    assemble_interval: IntervalAssembler,
) -> MeasurementTransition:
    """Validate, accept, persist, or expire an already merged candidate."""
    if len(candidate.samples) == len(state.sources):
        if complete_fault := _complete_candidate_fault(state, candidate, observed_at):
            return _reject(state, complete_fault)
        return _accept_active_candidate(state, candidate, assemble_interval)
    if expiry := _candidate_expiry_fault(candidate, observed_at):
        return _reject(state, expiry)
    if not changed:
        return MeasurementTransition(state=state)
    return MeasurementTransition(
        state=replace(
            state,
            revision=state.revision + 1,
            candidate=candidate,
        )
    )


def _advance_active(
    state: MeasurementPipelineState,
    samples: tuple[EnergyCounterSample, ...],
    observed_at: datetime,
    assemble_interval: IntervalAssembler,
) -> MeasurementTransition:
    """Advance a healthy baseline or enter fail-closed recovery."""
    if state.baseline is None:  # pragma: no cover - guarded by model invariant
        message = "active measurement state has no baseline"
        raise RuntimeError(message)
    newer, classification_fault = _classify_active_samples(state.baseline, samples)
    if classification_fault is not None:
        return _reject(state, classification_fault)
    if not newer:
        return _idle_active_transition(state, observed_at)

    candidate, fault, changed = _merge_candidate(
        state,
        newer,
        observed_at,
    )
    if fault is not None or candidate is None:
        return _reject(
            state,
            fault or _fault(MeasurementRejectionReason.INVALID_VECTOR),
        )

    return _resolve_active_candidate(
        state,
        candidate,
        observed_at,
        changed=changed,
        assemble_interval=assemble_interval,
    )


def _accept_baseline(
    state: MeasurementPipelineState,
    candidate: CandidateBuffer,
) -> MeasurementTransition:
    """Accept an initial or recovery vector without emitting its crossed interval."""
    ordered = _ordered_candidate_samples(state, candidate)
    return MeasurementTransition(
        state=replace(
            state,
            revision=state.revision + 1,
            phase=MeasurementPhase.ACTIVE,
            baseline=CounterSnapshot(ordered),
            candidate=None,
            recovery_after_period_end=None,
        )
    )


def _eligible_waiting_samples(
    state: MeasurementPipelineState,
    samples: tuple[EnergyCounterSample, ...],
) -> tuple[EnergyCounterSample, ...]:
    """Filter samples that may participate in this waiting phase."""
    if state.phase is MeasurementPhase.AWAITING_SEGMENT_BASELINE:
        return tuple(
            sample
            for sample in samples
            if sample.period_end >= state.segment_transition_at
        )
    barrier = state.recovery_after_period_end
    if barrier is None:  # pragma: no cover - guarded by model invariant
        message = "recovery phase has no recovery barrier"
        raise RuntimeError(message)
    return tuple(sample for sample in samples if sample.period_end > barrier)


def _idle_waiting_transition(
    state: MeasurementPipelineState,
    observed_at: datetime,
) -> MeasurementTransition:
    """Retain waiting state, expiring only a still-incomplete candidate."""
    if state.candidate is not None and (
        expiry := _candidate_expiry_fault(state.candidate, observed_at)
    ):
        return _reject(state, expiry)
    return MeasurementTransition(state=state)


def _resolve_waiting_candidate(
    state: MeasurementPipelineState,
    candidate: CandidateBuffer,
    observed_at: datetime,
    *,
    changed: bool,
) -> MeasurementTransition:
    """Validate, accept, persist, or expire a merged baseline candidate."""
    if len(candidate.samples) == len(state.sources):
        if complete_fault := _complete_candidate_fault(state, candidate, observed_at):
            return _reject(state, complete_fault)
        return _accept_baseline(state, candidate)
    if expiry := _candidate_expiry_fault(candidate, observed_at):
        return _reject(state, expiry)
    if not changed:
        return MeasurementTransition(state=state)
    return MeasurementTransition(
        state=replace(
            state,
            revision=state.revision + 1,
            candidate=candidate,
        )
    )


def _advance_waiting(
    state: MeasurementPipelineState,
    samples: tuple[EnergyCounterSample, ...],
    observed_at: datetime,
) -> MeasurementTransition:
    """Collect the first valid synchronized vector after startup or interruption."""
    eligible = _eligible_waiting_samples(state, samples)
    if not eligible:
        return _idle_waiting_transition(state, observed_at)

    candidate, fault, changed = _merge_candidate(state, eligible, observed_at)
    if fault is not None or candidate is None:
        return _reject(
            state,
            fault or _fault(MeasurementRejectionReason.INVALID_VECTOR),
        )
    return _resolve_waiting_candidate(
        state,
        candidate,
        observed_at,
        changed=changed,
    )


def advance_measurements(
    state: MeasurementPipelineState,
    observations: tuple[EnergyObservation, ...],
    observed_at: datetime,
    *,
    assemble_interval: IntervalAssembler,
) -> MeasurementTransition:
    """Advance one immutable vector and emit at most one normalized interval."""
    if not isinstance(observed_at, datetime):
        message = "observed_at must be a datetime"
        raise TypeError(message)
    if observed_at.tzinfo is None or observed_at.utcoffset() != timedelta(0):
        message = "observed_at must be a timezone-aware UTC timestamp"
        raise ValueError(message)
    if not callable(assemble_interval):
        message = "assemble_interval must be callable"
        raise TypeError(message)

    observation_map, vector_fault = _as_observation_map(state, observations)
    if vector_fault is not None or observation_map is None:
        return _reject(
            state,
            vector_fault or _fault(MeasurementRejectionReason.INVALID_VECTOR),
        )

    samples: list[EnergyCounterSample] = []
    for source in state.sources:
        observation = observation_map[source]
        if isinstance(observation, InvalidEnergySample):
            return _reject(state, _fault(observation.reason, source))
        if time_fault := _validate_sample_time(observation, observed_at):
            return _reject(state, time_fault)
        samples.append(observation)

    ordered_samples = tuple(samples)
    if state.phase is MeasurementPhase.ACTIVE:
        return _advance_active(
            state,
            ordered_samples,
            observed_at,
            assemble_interval,
        )
    return _advance_waiting(state, ordered_samples, observed_at)


__all__ = ("advance_measurements",)
