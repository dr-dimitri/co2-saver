# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""ADR 2.1 contract tests for the pure cumulative-counter reducer."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from typing import TYPE_CHECKING, cast

import pytest

from custom_components.co2saver.domain import (
    ConsumerLoad,
    Energy,
    IntervalRejectionReason,
    IntervalWindow,
    InverterIntervalInput,
    NormalizedInterval,
    RejectedInterval,
    loads_from_meters,
    normalize_interval,
)
from custom_components.co2saver.measurement.models import (
    CandidateBuffer,
    CounterSnapshot,
    EnergyCounterSample,
    EnergyObservation,
    EnergySourceIdentity,
    EnergyUnit,
    IntervalAssembler,
    InvalidEnergySample,
    MeasurementPhase,
    MeasurementPipelineState,
    MeasurementRejectionReason,
    MeasurementTransition,
    RawEnergyDeltaBatch,
)
from custom_components.co2saver.measurement.pipeline import advance_measurements

if TYPE_CHECKING:
    from collections.abc import Sequence

_START = datetime(2026, 9, 4, 12, tzinfo=UTC)
_PV = EnergySourceIdentity("pv_generation", "sensor-pv-registry")
_LOAD = EnergySourceIdentity("local_load", "sensor-load-registry")
_SOURCES = (_PV, _LOAD)


def _energy(value: int | Fraction | Energy) -> Energy:
    """Return an exact energy without converting through binary float."""
    return value if isinstance(value, Energy) else Energy(Fraction(value))


def _sample(
    source: EnergySourceIdentity,
    value: int | Fraction | Energy,
    period_end: datetime,
) -> EnergyCounterSample:
    """Build one valid sample with publication at its physical period end."""
    return EnergyCounterSample(
        source=source,
        cumulative=_energy(value),
        source_unit=EnergyUnit.KILOWATT_HOUR,
        period_end=period_end,
        last_reported=period_end,
    )


def _vector(
    period_end: datetime,
    pv: int | Fraction | Energy = 10,
    load: int | Fraction | Energy = 10,
) -> tuple[EnergyCounterSample, EnergyCounterSample]:
    """Build a complete balanced vector in stable configured order."""
    return (_sample(_PV, pv, period_end), _sample(_LOAD, load, period_end))


def _assemble_inverter(
    batch: RawEnergyDeltaBatch,
) -> NormalizedInterval | RejectedInterval:
    """Use the real domain normalizer behind an injected topology closure."""
    return normalize_interval(
        InverterIntervalInput(
            window=batch.window,
            consumers=loads_from_meters(
                ConsumerLoad("house", batch.energy_for(_LOAD.role)),
                (),
            ),
            pv_generation=batch.energy_for(_PV.role),
            grid_import=Energy.zero(),
            grid_export=Energy.zero(),
            battery_charge=Energy.zero(),
            battery_discharge=Energy.zero(),
        )
    )


class _CapturingAssembler:
    """Record raw callback inputs before using the real domain normalizer."""

    def __init__(self) -> None:
        self.batches: list[RawEnergyDeltaBatch] = []

    def __call__(
        self,
        batch: RawEnergyDeltaBatch,
        /,
    ) -> NormalizedInterval | RejectedInterval:
        """Capture and normalize one batch."""
        self.batches.append(batch)
        return _assemble_inverter(batch)


def _initial(transition_at: datetime = _START) -> MeasurementPipelineState:
    """Create the persisted segment-baseline phase."""
    return MeasurementPipelineState.initial(_SOURCES, transition_at)


def _active(
    period_end: datetime = _START,
    pv: int | Fraction | Energy = 10,
    load: int | Fraction | Energy = 10,
) -> MeasurementPipelineState:
    """Create a structurally valid active state for focused transitions."""
    return MeasurementPipelineState(
        revision=1,
        phase=MeasurementPhase.ACTIVE,
        sources=_SOURCES,
        segment_transition_at=_START,
        baseline=CounterSnapshot(_vector(period_end, pv, load)),
    )


def _advance(
    state: MeasurementPipelineState,
    observations: Sequence[EnergyObservation],
    observed_at: datetime,
    assembler: IntervalAssembler = _assemble_inverter,
) -> MeasurementTransition:
    """Run the reducer with the test topology adapter."""
    return advance_measurements(
        state,
        tuple(observations),
        observed_at,
        assemble_interval=assembler,
    )


def _assert_fault(
    transition: MeasurementTransition,
    reason: MeasurementRejectionReason,
) -> None:
    """Assert one typed fault without obscuring static narrowing."""
    assert transition.fault is not None
    assert transition.fault.reason is reason
    assert transition.interval is None


def _interrupt(
    state: MeasurementPipelineState,
    observed_at: datetime,
) -> MeasurementPipelineState:
    """Enter recovery through an unavailable required source."""
    result = _advance(
        state,
        (
            InvalidEnergySample(
                _PV,
                MeasurementRejectionReason.SOURCE_UNAVAILABLE,
            ),
            _sample(_LOAD, 10, state.baseline.period_end),  # type: ignore[union-attr]
        ),
        observed_at,
    )
    assert result.state.phase is MeasurementPhase.AWAITING_REBASELINE
    return result.state


def test_segment_baseline_ignores_complete_pretransition_vector() -> None:
    """A pre-segment vector cannot establish the new segment baseline."""
    state = _initial()
    old_period = _START - timedelta(seconds=60)

    transition = _advance(state, _vector(old_period), _START)

    assert transition.state is state
    assert transition.interval is None
    assert transition.fault is None


def test_segment_baseline_buffers_only_posttransition_roles() -> None:
    """A mixed old/new vector retains only the eligible role candidate."""
    state = _initial()
    observations = list(_vector(_START))
    observations[1] = _sample(_LOAD, 9, _START - timedelta(seconds=60))

    transition = _advance(state, observations, _START)

    assert transition.state.phase is MeasurementPhase.AWAITING_SEGMENT_BASELINE
    assert transition.state.candidate == CandidateBuffer(_START, (observations[0],))
    assert transition.state.segment_transition_at == _START
    assert transition.interval is None


def test_first_snapshot_at_segment_boundary_is_baseline_only() -> None:
    """The first full vector at the inclusive boundary emits no crossed interval."""
    assembler = _CapturingAssembler()

    transition = _advance(_initial(), _vector(_START), _START, assembler)

    assert transition.state.phase is MeasurementPhase.ACTIVE
    assert transition.state.baseline == CounterSnapshot(_vector(_START))
    assert transition.state.candidate is None
    assert transition.interval is None
    assert assembler.batches == []


def test_segment_candidate_timeout_boundary_is_inclusive() -> None:
    """A still-partial candidate expires only strictly after sixty seconds."""
    observations = list(_vector(_START))
    observations[1] = _sample(_LOAD, 9, _START - timedelta(seconds=60))
    buffered = _advance(_initial(), observations, _START).state

    at_boundary = _advance(
        buffered,
        observations,
        _START + timedelta(seconds=60),
    )
    expired = _advance(
        at_boundary.state,
        observations,
        _START + timedelta(seconds=60, microseconds=1),
    )

    assert at_boundary.state is buffered
    assert at_boundary.fault is None
    _assert_fault(expired, MeasurementRejectionReason.CANDIDATE_TIMEOUT)
    assert expired.state.phase is MeasurementPhase.AWAITING_SEGMENT_BASELINE
    assert expired.state.candidate is None
    assert expired.state.segment_transition_at == _START


def test_current_vector_completes_candidate_before_timeout() -> None:
    """A complete batch uses stored publication skew before wall-clock timeout."""
    first = list(_vector(_START))
    first[1] = _sample(_LOAD, 9, _START - timedelta(seconds=60))
    buffered = _advance(_initial(), first, _START).state
    complete = list(_vector(_START))
    complete[1] = replace(
        complete[1],
        last_reported=_START + timedelta(seconds=60),
    )

    transition = _advance(
        buffered,
        complete,
        _START + timedelta(seconds=61),
    )

    assert transition.state.phase is MeasurementPhase.ACTIVE
    assert transition.state.baseline == CounterSnapshot(tuple(complete))
    assert transition.fault is None


@pytest.mark.parametrize(
    ("replacement", "expected_reason"),
    [
        pytest.param(
            {"cumulative": Energy(Fraction(11))},
            MeasurementRejectionReason.CANDIDATE_SAMPLE_CONFLICT,
            id="value",
        ),
        pytest.param(
            {"source_unit": EnergyUnit.MEGAWATT_HOUR},
            MeasurementRejectionReason.CANDIDATE_SAMPLE_CONFLICT,
            id="unit",
        ),
    ],
)
def test_segment_candidate_is_immutable_per_role(
    replacement: dict[str, object],
    expected_reason: MeasurementRejectionReason,
) -> None:
    """Neither value nor unit uses first-or-last-write wins inside a candidate."""
    first = list(_vector(_START))
    first[1] = _sample(_LOAD, 9, _START - timedelta(seconds=60))
    buffered = _advance(_initial(), first, _START).state
    changed = list(first)
    changed[0] = replace(changed[0], **replacement)

    transition = _advance(buffered, changed, _START + timedelta(seconds=1))

    _assert_fault(transition, expected_reason)
    assert transition.state.candidate is None


def test_complete_new_sample_age_at_300_seconds_is_inclusive() -> None:
    """The exact five-minute age ceiling remains acceptable."""
    transition = _advance(
        _initial(),
        _vector(_START),
        _START + timedelta(seconds=300),
    )

    assert transition.state.phase is MeasurementPhase.ACTIVE


def test_complete_new_sample_age_above_300_seconds_is_rejected() -> None:
    """The first instant beyond the age ceiling fails closed."""
    transition = _advance(
        _initial(),
        _vector(_START),
        _START + timedelta(seconds=300, microseconds=1),
    )

    _assert_fault(transition, MeasurementRejectionReason.NEW_SAMPLE_STALE)
    assert transition.state.phase is MeasurementPhase.AWAITING_SEGMENT_BASELINE


def test_publication_delay_at_60_seconds_is_inclusive() -> None:
    """A sample published exactly sixty seconds after measurement is valid."""
    observations = list(_vector(_START))
    observations[1] = replace(
        observations[1],
        last_reported=_START + timedelta(seconds=60),
    )

    transition = _advance(
        _initial(),
        observations,
        _START + timedelta(seconds=60),
    )

    assert transition.state.phase is MeasurementPhase.ACTIVE


def test_publication_delay_above_60_seconds_is_rejected() -> None:
    """A publication beyond the exact source deadline is never buffered."""
    observations = list(_vector(_START))
    observations[1] = replace(
        observations[1],
        last_reported=_START + timedelta(seconds=60, microseconds=1),
    )

    transition = _advance(
        _initial(),
        observations,
        _START + timedelta(seconds=61),
    )

    _assert_fault(transition, MeasurementRejectionReason.PUBLICATION_DELAY)


@pytest.mark.parametrize(
    ("sample", "observed_at", "reason"),
    [
        pytest.param(
            replace(_sample(_PV, 1, _START), period_end=_START + timedelta(1)),
            _START,
            MeasurementRejectionReason.FUTURE_PERIOD_END,
            id="future-period",
        ),
        pytest.param(
            replace(
                _sample(_PV, 1, _START),
                last_reported=_START + timedelta(microseconds=1),
            ),
            _START,
            MeasurementRejectionReason.FUTURE_LAST_REPORTED,
            id="future-publication",
        ),
        pytest.param(
            replace(
                _sample(_PV, 1, _START),
                last_reported=_START - timedelta(microseconds=1),
            ),
            _START,
            MeasurementRejectionReason.PERIOD_AFTER_PUBLICATION,
            id="period-after-publication",
        ),
    ],
)
def test_sample_time_form_fails_closed_before_candidate_logic(
    sample: EnergyCounterSample,
    observed_at: datetime,
    reason: MeasurementRejectionReason,
) -> None:
    """Future and reversed timestamps never enter a candidate."""
    transition = _advance(
        _initial(),
        (sample, _sample(_LOAD, 1, _START)),
        observed_at,
    )

    _assert_fault(transition, reason)
    assert transition.state.candidate is None


def test_active_duplicate_ignores_new_last_reported_without_mutating_baseline() -> None:
    """Repeat publication time is not part of accepted-sample identity."""
    state = _active()
    duplicate = tuple(
        replace(sample, last_reported=_START + timedelta(seconds=30))
        for sample in _vector(_START)
    )

    transition = _advance(
        state,
        duplicate,
        _START + timedelta(seconds=100),
    )

    assert transition.state is state
    assert transition.state.baseline == state.baseline
    assert transition.interval is None


def test_baseline_staleness_boundary_is_inclusive() -> None:
    """An unchanged baseline remains valid through exactly 360 seconds."""
    state = _active()

    at_boundary = _advance(
        state,
        _vector(_START),
        _START + timedelta(seconds=360),
    )
    expired = _advance(
        at_boundary.state,
        _vector(_START),
        _START + timedelta(seconds=360, microseconds=1),
    )

    assert at_boundary.state is state
    _assert_fault(expired, MeasurementRejectionReason.BASELINE_STALE)
    assert expired.interruption_started


def test_900_second_interval_and_fractional_kwh_delta_are_exact() -> None:
    """The active duration ceiling and exact rational delta are inclusive."""
    baseline = _active(
        pv=Energy.from_wh(1_000),
        load=Energy.from_wh(1_000),
    )
    assert baseline.baseline is not None
    watt_hour_baseline = CounterSnapshot(
        tuple(
            replace(sample, source_unit=EnergyUnit.WATT_HOUR)
            for sample in baseline.baseline.samples
        )
    )
    baseline = replace(baseline, baseline=watt_hour_baseline)
    end = _START + timedelta(seconds=900)
    observations = tuple(
        replace(
            sample,
            cumulative=Energy.from_wh(1_500),
            period_end=end,
            last_reported=end,
        )
        for sample in watt_hour_baseline.samples
    )
    assembler = _CapturingAssembler()

    transition = _advance(baseline, observations, end, assembler)

    assert isinstance(transition.interval, NormalizedInterval)
    assert transition.interval.pv.kwh == Fraction(1, 2)
    assert transition.interval.local_load.kwh == Fraction(1, 2)
    assert assembler.batches[0].energy_for(_PV.role).kwh == Fraction(1, 2)
    assert assembler.batches[0].window == IntervalWindow(_START, end)


def test_interval_above_900_seconds_starts_recovery() -> None:
    """The first instant beyond the interval ceiling emits no delta."""
    end = _START + timedelta(seconds=900, microseconds=1)

    transition = _advance(_active(), _vector(end, 11, 11), end)

    _assert_fault(transition, MeasurementRejectionReason.INTERVAL_TOO_LONG)
    assert transition.state.phase is MeasurementPhase.AWAITING_REBASELINE
    assert transition.state.recovery_after_period_end == _START


@pytest.mark.parametrize(
    "next_value",
    [
        pytest.param(11, id="normalized-monotonic"),
        pytest.param(1, id="inconsistently-scaled"),
    ],
)
def test_newer_supported_unit_change_interrupts_without_delta(
    next_value: int,
) -> None:
    """Even a seemingly consistent supported unit change requires recovery."""
    state = _active()
    end = _START + timedelta(seconds=60)
    observations = list(_vector(end, next_value, 11))
    observations[0] = replace(
        observations[0],
        source_unit=EnergyUnit.MEGAWATT_HOUR,
    )
    assembler = _CapturingAssembler()

    transition = _advance(state, observations, end, assembler)

    _assert_fault(transition, MeasurementRejectionReason.UNIT_CHANGED)
    assert transition.state.baseline is state.baseline
    assert transition.state.recovery_after_period_end == _START
    assert transition.interruption_started
    assert assembler.batches == []


def test_same_period_unit_change_is_an_accepted_sample_conflict() -> None:
    """A corrected unit cannot rewrite an already accepted sample identity."""
    observations = list(_vector(_START))
    observations[0] = replace(
        observations[0],
        source_unit=EnergyUnit.WATT_HOUR,
    )

    transition = _advance(_active(), observations, _START + timedelta(seconds=1))

    _assert_fault(
        transition,
        MeasurementRejectionReason.ACCEPTED_SAMPLE_CONFLICT,
    )


def test_counter_reset_starts_recovery_without_negative_energy() -> None:
    """A true cumulative decrease is never exposed as an interval."""
    end = _START + timedelta(seconds=60)

    transition = _advance(_active(), _vector(end, 9, 11), end)

    _assert_fault(transition, MeasurementRejectionReason.COUNTER_RESET)
    assert transition.state.phase is MeasurementPhase.AWAITING_REBASELINE
    assert transition.state.baseline == _active().baseline


def test_period_rollback_starts_recovery_and_retains_last_accepted_barrier() -> None:
    """An active older timestamp never enters the candidate buffer."""
    state = _active()
    old = _START - timedelta(seconds=1)

    transition = _advance(state, _vector(old), _START)

    _assert_fault(transition, MeasurementRejectionReason.PERIOD_ROLLBACK)
    assert transition.state.candidate is None
    assert transition.state.recovery_after_period_end == _START


def test_required_source_unavailable_interrupts_only_once() -> None:
    """Repeated invalid observations preserve the inactive recovery state."""
    state = _active()
    invalid_vector: tuple[EnergyObservation, ...] = (
        InvalidEnergySample(
            _PV,
            MeasurementRejectionReason.SOURCE_UNAVAILABLE,
        ),
        _sample(_LOAD, 10, _START),
    )

    first = _advance(state, invalid_vector, _START + timedelta(seconds=1))
    second = _advance(
        first.state,
        invalid_vector,
        _START + timedelta(seconds=2),
    )

    _assert_fault(first, MeasurementRejectionReason.SOURCE_UNAVAILABLE)
    _assert_fault(second, MeasurementRejectionReason.SOURCE_UNAVAILABLE)
    assert first.interruption_started
    assert not second.interruption_started
    assert second.state is first.state


def test_recovery_ignores_equal_period_correction_and_unit_change() -> None:
    """Old or equal replays cannot replace the retained inactive baseline."""
    recovery = _interrupt(_active(), _START + timedelta(seconds=1))
    replay = list(_vector(_START, 999, 999))
    replay[0] = replace(replay[0], source_unit=EnergyUnit.MEGAWATT_HOUR)

    transition = _advance(
        recovery,
        replay,
        _START + timedelta(seconds=2),
    )

    assert transition.state is recovery
    assert transition.fault is None
    assert transition.interval is None


def test_recovery_requires_last_reported_newer_than_retained_baseline() -> None:
    """Restarted recovery retains publication novelty, not only its period barrier."""
    baseline_samples = tuple(
        replace(sample, last_reported=_START + timedelta(seconds=60))
        for sample in _vector(_START)
    )
    baseline = CounterSnapshot(baseline_samples)
    recovery = MeasurementPipelineState(
        revision=2,
        phase=MeasurementPhase.AWAITING_REBASELINE,
        sources=_SOURCES,
        segment_transition_at=_START,
        baseline=baseline,
        recovery_after_period_end=_START,
    )
    candidate_period = _START + timedelta(seconds=30)

    transition = _advance(
        recovery,
        _vector(candidate_period, 1, 1),
        candidate_period,
    )

    _assert_fault(transition, MeasurementRejectionReason.PUBLICATION_NOT_NEWER)
    assert transition.state.phase is MeasurementPhase.AWAITING_REBASELINE
    assert transition.state.baseline is baseline
    assert transition.state.recovery_after_period_end == _START
    assert not transition.interruption_started


def test_recovery_accepts_new_unit_reset_and_long_gap_as_baseline_only() -> None:
    """Only duration and counter monotonicity are skipped by valid recovery."""
    recovery = _interrupt(_active(), _START + timedelta(seconds=1))
    end = _START + timedelta(seconds=901)
    observations = tuple(
        replace(
            sample,
            source_unit=EnergyUnit.MEGAWATT_HOUR,
        )
        for sample in _vector(end, 1, 1)
    )
    assembler = _CapturingAssembler()

    transition = _advance(recovery, observations, end, assembler)

    assert transition.state.phase is MeasurementPhase.ACTIVE
    assert transition.state.baseline == CounterSnapshot(observations)
    assert transition.state.recovery_after_period_end is None
    assert transition.interval is None
    assert assembler.batches == []


def test_partial_candidate_roundtrip_merges_once_without_double_counting() -> None:
    """A persisted partial batch survives restart and emits exactly once."""
    state = _active()
    end = _START + timedelta(seconds=60)
    partial = (_sample(_PV, 11, end), _sample(_LOAD, 10, _START))
    buffered = _advance(state, partial, end).state
    restored = MeasurementPipelineState(
        revision=buffered.revision,
        phase=buffered.phase,
        sources=buffered.sources,
        segment_transition_at=buffered.segment_transition_at,
        baseline=buffered.baseline,
        candidate=buffered.candidate,
        recovery_after_period_end=buffered.recovery_after_period_end,
    )
    assembler = _CapturingAssembler()

    completed = _advance(restored, _vector(end, 11, 11), end, assembler)
    replayed = _advance(
        completed.state,
        _vector(end, 11, 11),
        end + timedelta(seconds=1),
        assembler,
    )

    assert isinstance(completed.interval, NormalizedInterval)
    assert replayed.interval is None
    assert replayed.state is completed.state
    assert len(assembler.batches) == 1


def test_active_candidate_period_skip_interrupts_without_mixing() -> None:
    """A persisted period cannot be combined with another role's later period."""
    first_end = _START + timedelta(seconds=60)
    buffered = _advance(
        _active(),
        (_sample(_PV, 11, first_end), _sample(_LOAD, 10, _START)),
        first_end,
    ).state
    later = first_end + timedelta(seconds=60)

    transition = _advance(
        buffered,
        (_sample(_PV, 12, later), _sample(_LOAD, 11, first_end)),
        later,
    )

    _assert_fault(
        transition,
        MeasurementRejectionReason.CANDIDATE_PERIOD_MISMATCH,
    )
    assert transition.state.phase is MeasurementPhase.AWAITING_REBASELINE


def test_domain_interval_rejection_interrupts_before_baseline_advances() -> None:
    """A topology/balance rejection is one atomic timeline interruption."""
    state = _active()
    end = _START + timedelta(seconds=60)
    assembler = _CapturingAssembler()

    transition = _advance(state, _vector(end, 12, 11), end, assembler)

    _assert_fault(transition, MeasurementRejectionReason.INTERVAL_REJECTED)
    assert transition.fault is not None
    assert transition.fault.interval_reason is IntervalRejectionReason.SITE_IMBALANCE
    assert transition.state.baseline is state.baseline
    assert transition.state.recovery_after_period_end == _START
    assert transition.interruption_started
    assert len(assembler.batches) == 1


def test_invalid_vector_membership_fails_closed() -> None:
    """The reducer requires exactly one observation for every configured source."""
    transition = _advance(
        _active(),
        (_sample(_PV, 10, _START),),
        _START + timedelta(seconds=1),
    )

    _assert_fault(transition, MeasurementRejectionReason.INVALID_VECTOR)
    assert transition.interruption_started


@pytest.mark.parametrize(
    "observations",
    [
        pytest.param(
            (_sample(_PV, 10, _START), _sample(_PV, 10, _START)),
            id="duplicate-source",
        ),
        pytest.param(
            (
                _sample(
                    EnergySourceIdentity("other", "other-registry"),
                    10,
                    _START,
                ),
                _sample(_LOAD, 10, _START),
            ),
            id="unconfigured-source",
        ),
        pytest.param(
            (object(), _sample(_LOAD, 10, _START)),
            id="untyped-observation",
        ),
    ],
)
def test_invalid_vector_aliases_and_runtime_types_fail_closed(
    observations: tuple[object, ...],
) -> None:
    """Duplicate, unconfigured, and untyped readings cannot enter state."""
    transition = _advance(
        _active(),
        cast("Sequence[EnergyObservation]", observations),
        _START + timedelta(seconds=1),
    )

    _assert_fault(transition, MeasurementRejectionReason.INVALID_VECTOR)
    assert transition.interruption_started


@pytest.mark.parametrize(
    ("candidate", "observed_at", "reason"),
    [
        pytest.param(
            CandidateBuffer(
                _START + timedelta(seconds=1),
                (_sample(_PV, 11, _START + timedelta(seconds=1)),),
            ),
            _START,
            MeasurementRejectionReason.FUTURE_PERIOD_END,
            id="future-period",
        ),
        pytest.param(
            CandidateBuffer(
                _START + timedelta(seconds=1),
                (
                    replace(
                        _sample(_PV, 11, _START + timedelta(seconds=1)),
                        last_reported=_START + timedelta(seconds=2),
                    ),
                ),
            ),
            _START + timedelta(seconds=1),
            MeasurementRejectionReason.FUTURE_LAST_REPORTED,
            id="future-publication",
        ),
        pytest.param(
            CandidateBuffer(
                _START + timedelta(seconds=1),
                (_sample(_PV, 11, _START + timedelta(seconds=1)),),
            ),
            _START + timedelta(seconds=301, microseconds=1),
            MeasurementRejectionReason.CANDIDATE_STALE,
            id="stale-period",
        ),
    ],
)
def test_restored_active_candidate_revalidates_absolute_freshness(
    candidate: CandidateBuffer,
    observed_at: datetime,
    reason: MeasurementRejectionReason,
) -> None:
    """Restart cannot bypass future-time or candidate-age checks."""
    state = replace(_active(), revision=2, candidate=candidate)

    transition = _advance(state, _vector(_START), observed_at)

    _assert_fault(transition, reason)
    assert transition.state.phase is MeasurementPhase.AWAITING_REBASELINE


def test_restored_candidate_cannot_complete_with_future_publication() -> None:
    """A clock rollback cannot admit a persisted future publication on merge."""
    period_end = _START + timedelta(seconds=60)
    candidate = CandidateBuffer(
        period_end,
        (
            replace(
                _sample(_PV, 11, period_end),
                last_reported=period_end + timedelta(seconds=10),
            ),
        ),
    )
    state = replace(_active(), revision=2, candidate=candidate)
    observations = (
        _sample(_PV, 10, _START),
        _sample(_LOAD, 11, period_end),
    )

    transition = _advance(
        state,
        observations,
        period_end + timedelta(seconds=5),
    )

    _assert_fault(transition, MeasurementRejectionReason.FUTURE_LAST_REPORTED)
    assert transition.state.phase is MeasurementPhase.AWAITING_REBASELINE
    assert transition.interval is None


def test_repeated_active_candidate_waits_through_exact_timeout_boundary() -> None:
    """An unchanged partial active candidate expires only after sixty seconds."""
    end = _START + timedelta(seconds=60)
    observations = (_sample(_PV, 11, end), _sample(_LOAD, 10, _START))
    buffered = _advance(_active(), observations, end).state

    at_boundary = _advance(
        buffered,
        observations,
        end + timedelta(seconds=60),
    )
    expired = _advance(
        at_boundary.state,
        observations,
        end + timedelta(seconds=60, microseconds=1),
    )

    assert at_boundary.state is buffered
    assert at_boundary.fault is None
    _assert_fault(expired, MeasurementRejectionReason.CANDIDATE_TIMEOUT)
    assert expired.state.phase is MeasurementPhase.AWAITING_REBASELINE


def test_restored_active_candidate_expires_while_states_show_baseline() -> None:
    """A restart keeps the candidate deadline even if HA shows old states."""
    end = _START + timedelta(seconds=60)
    candidate = CandidateBuffer(end, (_sample(_PV, 11, end),))
    state = replace(_active(), revision=2, candidate=candidate)

    transition = _advance(
        state,
        _vector(_START),
        end + timedelta(seconds=60, microseconds=1),
    )

    _assert_fault(transition, MeasurementRejectionReason.CANDIDATE_TIMEOUT)
    assert transition.state.phase is MeasurementPhase.AWAITING_REBASELINE


def test_restored_segment_candidate_expires_without_eligible_sample() -> None:
    """Pre-transition states cannot keep a restored segment candidate alive."""
    current = list(_vector(_START))
    current[1] = _sample(_LOAD, 9, _START - timedelta(seconds=60))
    buffered = _advance(_initial(), current, _START).state
    pretransition = _vector(_START - timedelta(seconds=60), 9, 9)

    transition = _advance(
        buffered,
        pretransition,
        _START + timedelta(seconds=60, microseconds=1),
    )

    _assert_fault(transition, MeasurementRejectionReason.CANDIDATE_TIMEOUT)
    assert transition.state.phase is MeasurementPhase.AWAITING_SEGMENT_BASELINE
    assert transition.state.candidate is None


def test_assembler_must_preserve_raw_interval_window() -> None:
    """A topology callback cannot move the physical delta boundaries."""

    def wrong_window(
        batch: RawEnergyDeltaBatch,
    ) -> NormalizedInterval | RejectedInterval:
        interval = _assemble_inverter(batch)
        assert isinstance(interval, NormalizedInterval)
        return replace(
            interval,
            window=IntervalWindow(
                batch.window.start,
                batch.window.end + timedelta(microseconds=1),
            ),
        )

    end = _START + timedelta(seconds=1)
    with pytest.raises(ValueError, match="preserve the raw delta window"):
        _advance(_active(), _vector(end, 11, 11), end, wrong_window)


def test_non_utc_poll_time_is_rejected_at_api_boundary() -> None:
    """The pure reducer never accepts an ambiguous observation instant."""
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        _advance(
            _initial(),
            _vector(_START),
            _START.replace(tzinfo=None),
        )


def test_poll_time_and_assembler_are_runtime_checked() -> None:
    """The public reducer rejects malformed injected boundary values."""
    with pytest.raises(TypeError, match="observed_at must be a datetime"):
        _advance(
            _initial(),
            _vector(_START),
            cast("datetime", object()),
        )
    with pytest.raises(TypeError, match="assemble_interval must be callable"):
        _advance(
            _initial(),
            _vector(_START),
            _START,
            cast("IntervalAssembler", object()),
        )


def test_assembler_must_return_declared_domain_result() -> None:
    """An injected programming error cannot be mistaken for a measurement result."""

    def invalid_assembler(_batch: RawEnergyDeltaBatch) -> object:
        return object()

    with pytest.raises(TypeError, match="NormalizedInterval or RejectedInterval"):
        _advance(
            _active(),
            _vector(_START + timedelta(seconds=1), 11, 11),
            _START + timedelta(seconds=1),
            cast("IntervalAssembler", invalid_assembler),
        )
