# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Constructive model tests for cumulative measurement state."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone
from fractions import Fraction
from typing import cast

import pytest

from custom_components.co2saver.domain import (
    ConsumerLoad,
    Energy,
    IntervalRejectionReason,
    IntervalWindow,
    InverterIntervalInput,
    NormalizedInterval,
    loads_from_meters,
    normalize_interval,
)
from custom_components.co2saver.measurement.models import (
    CandidateBuffer,
    CounterSnapshot,
    EnergyCounterSample,
    EnergyDelta,
    EnergySourceIdentity,
    EnergyUnit,
    InvalidEnergySample,
    MeasurementFault,
    MeasurementPhase,
    MeasurementPipelineState,
    MeasurementRejectionReason,
    MeasurementTransition,
    RawEnergyDeltaBatch,
)

_START = datetime(2026, 9, 4, 12, tzinfo=UTC)
_PV = EnergySourceIdentity("pv_generation", "sensor-pv-registry")
_LOAD = EnergySourceIdentity("local_load", "sensor-load-registry")
_SOURCES = (_PV, _LOAD)


class _StringSubclass(str):
    """A string subtype that the strict persistence codec cannot encode."""

    __slots__ = ()


def _sample(
    source: EnergySourceIdentity,
    value: int | Fraction,
    period_end: datetime = _START,
    *,
    last_reported: datetime | None = None,
    unit: EnergyUnit = EnergyUnit.KILOWATT_HOUR,
) -> EnergyCounterSample:
    """Build one exact canonical counter sample."""
    return EnergyCounterSample(
        source=source,
        cumulative=Energy(Fraction(value)),
        source_unit=unit,
        period_end=period_end,
        last_reported=last_reported or period_end,
    )


def _snapshot(period_end: datetime = _START) -> CounterSnapshot:
    """Build a complete synchronized two-role snapshot."""
    return CounterSnapshot(
        (
            _sample(_PV, 10, period_end),
            _sample(_LOAD, 10, period_end),
        )
    )


def _normalized_interval() -> NormalizedInterval:
    """Build one zero-energy domain interval for output-model tests."""
    window = IntervalWindow(_START, _START + timedelta(seconds=1))
    result = normalize_interval(
        InverterIntervalInput(
            window=window,
            consumers=loads_from_meters(
                ConsumerLoad("house", Energy.zero()),
                (),
            ),
            pv_generation=Energy.zero(),
            grid_import=Energy.zero(),
            grid_export=Energy.zero(),
            battery_charge=Energy.zero(),
            battery_discharge=Energy.zero(),
        )
    )
    assert isinstance(result, NormalizedInterval)
    return result


@pytest.mark.parametrize(
    ("role", "registry_id"),
    [
        pytest.param("", "registry", id="empty-role"),
        pytest.param(" ", "registry", id="blank-role"),
        pytest.param(" pv", "registry", id="leading-role-whitespace"),
        pytest.param("pv ", "registry", id="trailing-role-whitespace"),
        pytest.param("pv", "", id="empty-registry-id"),
        pytest.param("pv", " ", id="blank-registry-id"),
        pytest.param("pv", " registry", id="leading-registry-whitespace"),
        pytest.param("pv", "registry ", id="trailing-registry-whitespace"),
        pytest.param(_StringSubclass("pv"), "registry", id="subclass-role"),
        pytest.param("pv", _StringSubclass("registry"), id="subclass-registry"),
    ],
)
def test_source_identity_requires_canonical_nonempty_strings(
    role: str,
    registry_id: str,
) -> None:
    """Every model-valid source identity can round-trip through the codec."""
    with pytest.raises(ValueError, match=r"without surrounding whitespace|empty"):
        EnergySourceIdentity(role, registry_id)


def test_sample_retains_exact_kwh_and_declared_source_unit() -> None:
    """Normalization does not discard the source-unit identity."""
    sample = _sample(
        _PV,
        Fraction(3, 2),
        unit=EnergyUnit.WATT_HOUR,
    )

    assert sample.cumulative.kwh == Fraction(3, 2)
    assert sample.source_unit is EnergyUnit.WATT_HOUR


@pytest.mark.parametrize(
    "timestamp",
    [
        pytest.param(_START.replace(tzinfo=None), id="naive"),
        pytest.param(
            _START.astimezone(timezone(timedelta(hours=1))),
            id="non-utc-offset",
        ),
    ],
)
def test_sample_rejects_non_utc_timestamp(timestamp: datetime) -> None:
    """Persisted timestamps must use an aware UTC representation."""
    with pytest.raises(ValueError, match="timezone-aware UTC"):
        _sample(_PV, 1, timestamp)


def test_sample_rejects_non_datetime_timestamp() -> None:
    """A non-datetime cannot enter persisted measurement state."""
    with pytest.raises(TypeError, match="must be a datetime"):
        replace(
            _sample(_PV, 1),
            period_end=cast("datetime", "2026-09-04T12:00:00Z"),
        )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        pytest.param(
            {"source": object()},
            "sample source",
            id="source",
        ),
        pytest.param(
            {"cumulative": Fraction(1)},
            "exact Energy",
            id="energy",
        ),
        pytest.param(
            {"source_unit": "kWh"},
            "supported EnergyUnit",
            id="unit",
        ),
    ],
)
def test_sample_rejects_wrong_runtime_field_types(
    changes: dict[str, object],
    message: str,
) -> None:
    """Typed samples cannot be forged with dimensionless or raw values."""
    with pytest.raises(TypeError, match=message):
        replace(_sample(_PV, 1), **changes)


@pytest.mark.parametrize(
    ("source", "reason", "message"),
    [
        pytest.param(
            object(),
            MeasurementRejectionReason.INVALID_VALUE,
            "sample source",
            id="source",
        ),
        pytest.param(
            _PV,
            "invalid_value",
            "sample reason",
            id="reason",
        ),
    ],
)
def test_invalid_sample_rejects_wrong_runtime_field_types(
    source: object,
    reason: object,
    message: str,
) -> None:
    """Adapter failures retain typed ownership and stable reason codes."""
    with pytest.raises(TypeError, match=message):
        InvalidEnergySample(
            cast("EnergySourceIdentity", source),
            cast("MeasurementRejectionReason", reason),
        )


def test_snapshot_accepts_publication_delay_and_skew_at_sixty_seconds() -> None:
    """Both exact ADR publication boundaries are inclusive."""
    snapshot = CounterSnapshot(
        (
            _sample(_PV, 1, last_reported=_START),
            _sample(
                _LOAD,
                1,
                last_reported=_START + timedelta(seconds=60),
            ),
        )
    )

    assert snapshot.period_end == _START


def test_snapshot_rejects_duplicate_sources() -> None:
    """A complete vector cannot give one counter two values."""
    with pytest.raises(ValueError, match="sources must be unique"):
        CounterSnapshot((_sample(_PV, 1), _sample(_PV, 2)))


def test_snapshot_rejects_empty_or_non_sample_entries() -> None:
    """Accepted baselines always contain actual counter samples."""
    with pytest.raises(ValueError, match="must not be empty"):
        CounterSnapshot(())
    with pytest.raises(TypeError, match="must be EnergyCounterSample"):
        CounterSnapshot(cast("tuple[EnergyCounterSample, ...]", (object(),)))


def test_snapshot_rejects_mixed_physical_periods() -> None:
    """No model can claim synchronization across distinct physical periods."""
    with pytest.raises(ValueError, match="share one period_end"):
        CounterSnapshot(
            (
                _sample(_PV, 1),
                _sample(_LOAD, 1, _START + timedelta(seconds=1)),
            )
        )


def test_snapshot_rejects_intrinsically_invalid_publication_order() -> None:
    """A persisted accepted snapshot cannot precede its own measurement."""
    with pytest.raises(ValueError, match="must not follow"):
        CounterSnapshot(
            (
                _sample(
                    _PV,
                    1,
                    last_reported=_START - timedelta(microseconds=1),
                ),
                _sample(_LOAD, 1),
            )
        )


def test_snapshot_rejects_publication_delay_above_sixty_seconds() -> None:
    """A restored baseline cannot contain a source-contract violation."""
    with pytest.raises(ValueError, match="publication delay"):
        CounterSnapshot(
            (
                _sample(
                    _PV,
                    1,
                    last_reported=_START + timedelta(seconds=61),
                ),
                _sample(_LOAD, 1),
            )
        )


def test_candidate_is_immutable_and_requires_one_period() -> None:
    """Buffered observations cannot mutate or cross physical periods."""
    candidate = CandidateBuffer(_START, (_sample(_PV, 1),))

    with pytest.raises(FrozenInstanceError):
        candidate.period_end = _START + timedelta(seconds=1)  # type: ignore[misc]
    with pytest.raises(ValueError, match="match candidate period_end"):
        CandidateBuffer(
            _START,
            (_sample(_PV, 1, _START + timedelta(seconds=1)),),
        )


@pytest.mark.parametrize(
    "last_reported",
    [
        pytest.param(_START - timedelta(microseconds=1), id="before-period"),
        pytest.param(_START + timedelta(seconds=60, microseconds=1), id="too-late"),
    ],
)
def test_candidate_rejects_intrinsically_invalid_sample_time(
    last_reported: datetime,
) -> None:
    """Every model-valid candidate is safe for exact codec round-tripping."""
    with pytest.raises(ValueError, match=r"period_end|publication delay"):
        CandidateBuffer(
            _START,
            (_sample(_PV, 1, last_reported=last_reported),),
        )


def test_candidate_rejects_empty_non_sample_and_duplicate_entries() -> None:
    """Only a non-empty set of unique counter samples can be buffered."""
    with pytest.raises(ValueError, match="must not be empty"):
        CandidateBuffer(_START, ())
    with pytest.raises(TypeError, match="must be EnergyCounterSample"):
        CandidateBuffer(
            _START,
            cast("tuple[EnergyCounterSample, ...]", (object(),)),
        )
    with pytest.raises(ValueError, match="sources must be unique"):
        CandidateBuffer(_START, (_sample(_PV, 1), _sample(_PV, 1)))


def test_state_rejects_duplicate_roles_without_needing_a_candidate() -> None:
    """The source-identity check is independent from candidate presence."""
    duplicate_role = EnergySourceIdentity("pv_generation", "another-registry")

    with pytest.raises(ValueError, match="roles must be unique"):
        MeasurementPipelineState.initial((_PV, duplicate_role), _START)


def test_state_rejects_duplicate_registry_ids_without_needing_a_candidate() -> None:
    """One physical counter cannot silently own two configured roles."""
    duplicate_registry = EnergySourceIdentity("other", _PV.registry_id)

    with pytest.raises(ValueError, match="registry ids must be unique"):
        MeasurementPipelineState.initial((_PV, duplicate_registry), _START)


def test_state_rejects_empty_or_non_identity_source_collection() -> None:
    """A measurement plan has at least one typed, uniquely owned source."""
    with pytest.raises(ValueError, match="at least one"):
        MeasurementPipelineState.initial((), _START)
    with pytest.raises(TypeError, match="EnergySourceIdentity"):
        MeasurementPipelineState.initial(
            cast("tuple[EnergySourceIdentity, ...]", (object(),)),
            _START,
        )


def test_initial_state_is_segment_guarded_and_empty() -> None:
    """The first read starts only after a persisted UTC segment boundary."""
    state = MeasurementPipelineState.initial(_SOURCES, _START)

    assert state == MeasurementPipelineState(
        revision=0,
        phase=MeasurementPhase.AWAITING_SEGMENT_BASELINE,
        sources=_SOURCES,
        segment_transition_at=_START,
    )


def test_active_state_requires_complete_baseline() -> None:
    """No active timeline can exist without its accepted counter vector."""
    with pytest.raises(ValueError, match="active phase requires"):
        MeasurementPipelineState(
            revision=1,
            phase=MeasurementPhase.ACTIVE,
            sources=_SOURCES,
            segment_transition_at=_START,
        )


def test_active_state_rejects_recovery_barrier() -> None:
    """An active state cannot retain an interruption-only barrier."""
    with pytest.raises(ValueError, match="active phase requires"):
        MeasurementPipelineState(
            revision=1,
            phase=MeasurementPhase.ACTIVE,
            sources=_SOURCES,
            segment_transition_at=_START,
            baseline=_snapshot(),
            recovery_after_period_end=_START,
        )


def test_recovery_state_retains_baseline_and_equal_barrier() -> None:
    """Recovery preserves the inactive baseline needed for publication novelty."""
    baseline = _snapshot()
    state = MeasurementPipelineState(
        revision=2,
        phase=MeasurementPhase.AWAITING_REBASELINE,
        sources=_SOURCES,
        segment_transition_at=_START,
        baseline=baseline,
        recovery_after_period_end=baseline.period_end,
    )

    assert state.baseline is baseline
    assert state.recovery_after_period_end == baseline.period_end


def test_recovery_state_rejects_barrier_different_from_retained_baseline() -> None:
    """A restart cannot weaken the last accepted recovery barrier."""
    with pytest.raises(ValueError, match="barrier must equal"):
        MeasurementPipelineState(
            revision=2,
            phase=MeasurementPhase.AWAITING_REBASELINE,
            sources=_SOURCES,
            segment_transition_at=_START,
            baseline=_snapshot(),
            recovery_after_period_end=_START + timedelta(seconds=1),
        )


def test_recovery_state_requires_both_retained_baseline_and_barrier() -> None:
    """Neither half of the recovery proof may be omitted on restart."""
    with pytest.raises(ValueError, match="recovery phase requires"):
        MeasurementPipelineState(
            revision=1,
            phase=MeasurementPhase.AWAITING_REBASELINE,
            sources=_SOURCES,
            segment_transition_at=_START,
        )


def test_initial_state_rejects_old_baseline_or_recovery_barrier() -> None:
    """A new segment cannot retain any state from its predecessor."""
    with pytest.raises(ValueError, match="initial baseline phase"):
        MeasurementPipelineState(
            revision=1,
            phase=MeasurementPhase.AWAITING_SEGMENT_BASELINE,
            sources=_SOURCES,
            segment_transition_at=_START,
            baseline=_snapshot(),
        )


def test_state_rejects_baseline_before_segment_transition() -> None:
    """Restored state cannot reactivate a pre-segment baseline."""
    with pytest.raises(ValueError, match="cannot precede"):
        MeasurementPipelineState(
            revision=1,
            phase=MeasurementPhase.ACTIVE,
            sources=_SOURCES,
            segment_transition_at=_START + timedelta(seconds=1),
            baseline=_snapshot(),
        )


def test_state_rejects_complete_persisted_candidate() -> None:
    """A complete candidate must be consumed in the same reducer transition."""
    candidate = CandidateBuffer(_START, _snapshot().samples)

    with pytest.raises(ValueError, match="complete candidate"):
        MeasurementPipelineState(
            revision=1,
            phase=MeasurementPhase.AWAITING_SEGMENT_BASELINE,
            sources=_SOURCES,
            segment_transition_at=_START,
            candidate=candidate,
        )


def test_state_rejects_baseline_or_candidate_with_wrong_membership() -> None:
    """Restored snapshots cannot add, drop, or replace configured identities."""
    other = EnergySourceIdentity("other", "other-registry")
    with pytest.raises(ValueError, match="baseline must contain every"):
        MeasurementPipelineState(
            revision=1,
            phase=MeasurementPhase.ACTIVE,
            sources=_SOURCES,
            segment_transition_at=_START,
            baseline=CounterSnapshot((_sample(_PV, 1), _sample(other, 1))),
        )
    with pytest.raises(ValueError, match="unconfigured source"):
        MeasurementPipelineState(
            revision=1,
            phase=MeasurementPhase.AWAITING_SEGMENT_BASELINE,
            sources=_SOURCES,
            segment_transition_at=_START,
            candidate=CandidateBuffer(_START, (_sample(other, 1),)),
        )


@pytest.mark.parametrize(
    ("changes", "error", "message"),
    [
        pytest.param({"revision": -1}, ValueError, "non-negative", id="revision"),
        pytest.param(
            {"revision": True},
            ValueError,
            "non-negative",
            id="bool-revision",
        ),
        pytest.param({"phase": "active"}, TypeError, "MeasurementPhase", id="phase"),
        pytest.param(
            {"segment_transition_at": "2026-09-04T12:00:00Z"},
            TypeError,
            "must be a datetime",
            id="transition-time",
        ),
        pytest.param(
            {"baseline": object()},
            TypeError,
            "CounterSnapshot",
            id="baseline",
        ),
        pytest.param(
            {"candidate": object()},
            TypeError,
            "CandidateBuffer",
            id="candidate",
        ),
    ],
)
def test_state_rejects_wrong_scalar_or_nested_types(
    changes: dict[str, object],
    error: type[Exception],
    message: str,
) -> None:
    """Persisted state rejects malformed scalar and nested values."""
    state = MeasurementPipelineState.initial(_SOURCES, _START)
    with pytest.raises(error, match=message):
        replace(state, **changes)


@pytest.mark.parametrize(
    ("phase", "candidate_period", "message"),
    [
        pytest.param(
            MeasurementPhase.AWAITING_SEGMENT_BASELINE,
            _START - timedelta(microseconds=1),
            "segment candidate",
            id="before-segment",
        ),
        pytest.param(
            MeasurementPhase.ACTIVE,
            _START,
            "newer than the baseline",
            id="not-newer-than-active-baseline",
        ),
        pytest.param(
            MeasurementPhase.AWAITING_REBASELINE,
            _START,
            "newer than its barrier",
            id="not-newer-than-recovery-barrier",
        ),
    ],
)
def test_state_rejects_candidate_outside_phase_window(
    phase: MeasurementPhase,
    candidate_period: datetime,
    message: str,
) -> None:
    """Persisted candidates stay strictly within their phase's time window."""
    baseline = (
        None if phase is MeasurementPhase.AWAITING_SEGMENT_BASELINE else _snapshot()
    )
    barrier = _START if phase is MeasurementPhase.AWAITING_REBASELINE else None
    candidate = CandidateBuffer(
        candidate_period,
        (_sample(_PV, 11, candidate_period),),
    )

    with pytest.raises(ValueError, match=message):
        MeasurementPipelineState(
            revision=2,
            phase=phase,
            sources=_SOURCES,
            segment_transition_at=_START,
            baseline=baseline,
            candidate=candidate,
            recovery_after_period_end=barrier,
        )


def test_active_state_rejects_candidate_unit_change() -> None:
    """No persisted active candidate may bypass the unit-change interruption."""
    candidate_period = _START + timedelta(seconds=1)
    candidate = CandidateBuffer(
        candidate_period,
        (
            _sample(
                _PV,
                11,
                candidate_period,
                unit=EnergyUnit.MEGAWATT_HOUR,
            ),
        ),
    )

    with pytest.raises(ValueError, match="units must match"):
        MeasurementPipelineState(
            revision=2,
            phase=MeasurementPhase.ACTIVE,
            sources=_SOURCES,
            segment_transition_at=_START,
            baseline=_snapshot(),
            candidate=candidate,
        )


def test_raw_delta_batch_preserves_exact_energy_and_role_lookup() -> None:
    """The assembler receives one exact, source-owned kWh delta per role."""
    batch = RawEnergyDeltaBatch(
        window=IntervalWindow(_START, _START + timedelta(seconds=1)),
        deltas=(
            EnergyDelta(_PV, Energy(Fraction(1, 3))),
            EnergyDelta(_LOAD, Energy(Fraction(1, 3))),
        ),
    )

    assert batch.energy_for("pv_generation").kwh == Fraction(1, 3)
    with pytest.raises(KeyError, match="unknown energy role"):
        batch.energy_for("missing")


def test_raw_delta_batch_rejects_duplicate_role_or_registry_ownership() -> None:
    """Topology assembly cannot receive aliased raw measurements."""
    duplicate_role = EnergySourceIdentity("pv_generation", "another-registry")
    with pytest.raises(ValueError, match="roles must be unique"):
        RawEnergyDeltaBatch(
            IntervalWindow(_START, _START + timedelta(seconds=1)),
            (
                EnergyDelta(_PV, Energy.zero()),
                EnergyDelta(duplicate_role, Energy.zero()),
            ),
        )


@pytest.mark.parametrize(
    ("source", "energy", "message"),
    [
        pytest.param(object(), Energy.zero(), "delta source", id="source"),
        pytest.param(_PV, Fraction(), "exact Energy", id="energy"),
    ],
)
def test_energy_delta_rejects_wrong_runtime_types(
    source: object,
    energy: object,
    message: str,
) -> None:
    """Raw deltas keep source ownership and physical dimensions explicit."""
    with pytest.raises(TypeError, match=message):
        EnergyDelta(
            cast("EnergySourceIdentity", source),
            cast("Energy", energy),
        )


def test_raw_delta_batch_rejects_wrong_window_or_entries() -> None:
    """The topology callback cannot receive a malformed batch container."""
    window = IntervalWindow(_START, _START + timedelta(seconds=1))
    delta = EnergyDelta(_PV, Energy.zero())
    with pytest.raises(TypeError, match="IntervalWindow"):
        RawEnergyDeltaBatch(cast("IntervalWindow", object()), (delta,))
    with pytest.raises(ValueError, match="must not be empty"):
        RawEnergyDeltaBatch(window, ())
    with pytest.raises(TypeError, match="must be EnergyDelta"):
        RawEnergyDeltaBatch(
            window,
            cast("tuple[EnergyDelta, ...]", (object(),)),
        )


def test_raw_delta_batch_rejects_duplicate_registry_ownership() -> None:
    """Registry aliases are rejected independently from role aliases."""
    duplicate_registry = EnergySourceIdentity("other", _PV.registry_id)
    with pytest.raises(ValueError, match="registry ids must be unique"):
        RawEnergyDeltaBatch(
            IntervalWindow(_START, _START + timedelta(seconds=1)),
            (
                EnergyDelta(_PV, Energy.zero()),
                EnergyDelta(duplicate_registry, Energy.zero()),
            ),
        )


def test_interval_fault_requires_exact_domain_reason() -> None:
    """A generic measurement fault cannot lose a domain rejection reason."""
    with pytest.raises(ValueError, match="requires its domain reason"):
        MeasurementFault(MeasurementRejectionReason.INTERVAL_REJECTED)
    with pytest.raises(ValueError, match="only interval rejection"):
        MeasurementFault(
            MeasurementRejectionReason.COUNTER_RESET,
            interval_reason=IntervalRejectionReason.SITE_IMBALANCE,
        )


def test_fault_rejects_wrong_reason_or_source_type() -> None:
    """Fault diagnostics cannot contain untyped codes or source aliases."""
    with pytest.raises(TypeError, match="fault reason"):
        MeasurementFault(cast("MeasurementRejectionReason", "invalid_vector"))
    with pytest.raises(TypeError, match="fault source"):
        MeasurementFault(
            MeasurementRejectionReason.INVALID_VECTOR,
            source=cast("EnergySourceIdentity", object()),
        )


def test_transition_rejects_non_domain_interval() -> None:
    """Reducer output cannot expose the raw batch as a normalized interval."""
    state = MeasurementPipelineState.initial(_SOURCES, _START)

    with pytest.raises(TypeError, match="NormalizedInterval"):
        MeasurementTransition(
            state=state,
            interval=cast(
                "object",
                IntervalWindow(_START, _START + timedelta(seconds=1)),
            ),
        )


def test_transition_rejects_wrong_state_fault_and_marker_types() -> None:
    """A transition cannot be forged around invalid nested output values."""
    state = MeasurementPipelineState.initial(_SOURCES, _START)
    with pytest.raises(TypeError, match="transition state"):
        MeasurementTransition(cast("MeasurementPipelineState", object()))
    with pytest.raises(TypeError, match="transition fault"):
        MeasurementTransition(
            state,
            fault=cast("MeasurementFault", object()),
        )
    with pytest.raises(TypeError, match="must be bool"):
        MeasurementTransition(
            state,
            interruption_started=cast("bool", 1),
        )


def test_transition_rejects_conflicting_interval_fault_or_bare_interruption() -> None:
    """Each reducer result has exactly one accepted or rejected meaning."""
    state = MeasurementPipelineState.initial(_SOURCES, _START)
    fault = MeasurementFault(MeasurementRejectionReason.INVALID_VECTOR)
    with pytest.raises(ValueError, match="interval and a fault"):
        MeasurementTransition(state, interval=_normalized_interval(), fault=fault)
    with pytest.raises(ValueError, match="marker requires a fault"):
        MeasurementTransition(state, interruption_started=True)
