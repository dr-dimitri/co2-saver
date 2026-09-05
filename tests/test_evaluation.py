# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Pure direct-PV generation transitions and current-poll CO₂ attribution."""

from __future__ import annotations

import json
import subprocess
import sys
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from custom_components.co2saver.config_factors import GridIntensitySample
from custom_components.co2saver.domain import (
    Energy,
    IntervalRejectionReason,
    IntervalWindow,
    NormalizedInterval,
    StorageLedger,
)
from custom_components.co2saver.evaluation import (
    DirectEvaluationPlan,
    EvaluationOutcome,
    evaluate_observations,
)
from custom_components.co2saver.measurement.models import (
    EnergyCounterSample,
    EnergyDelta,
    EnergyObservation,
    EnergySourceIdentity,
    EnergyUnit,
    InvalidEnergySample,
    MeasurementPhase,
    MeasurementPipelineState,
    MeasurementRejectionReason,
    RawEnergyDeltaBatch,
)
from custom_components.co2saver.persistence import (
    CumulativeTotals,
    GenerationCodec,
    GenerationRevisionPolicy,
    GenerationState,
)

_START = datetime(2026, 9, 5, 12, tzinfo=UTC)
_END = _START + timedelta(minutes=1)
_HOUSE = "a" * 32
_WALLBOX = "b" * 32
_GRID = "7" * 32
_STORE = "e" * 32
_GENERATION = "f" * 32


def _config(
    topology: str = "inverter",
    mode: str = "aggregate_shares",
    *,
    additional: bool = True,
) -> dict[str, Any]:
    """Build one serializable battery-free setup with explicit load ownership."""
    sources = {"grid_import": "2" * 32, "grid_export": "3" * 32}
    if topology == "inverter":
        sources["pv_generation"] = "1" * 32
    return {
        "topology": topology,
        "sources": sources,
        "plant_key": f"grid:{'2' * 32}:{'3' * 32}",
        "synchronous_sources_confirmed": True,
        "battery": None,
        "consumption": {
            "mode": mode,
            "household_id": _HOUSE,
            "household_source": "4" * 32,
            "consumers": [
                {
                    "consumer_id": _WALLBOX,
                    "name": "Wallbox",
                    **(
                        {"share": "0.25"}
                        if mode == "aggregate_shares"
                        else {"source": "5" * 32}
                    ),
                }
            ]
            if additional
            else [],
        },
        "factors": {
            "grid_intensity_source": _GRID,
            "grid_max_age_minutes": 60,
            "pv_factor": "40",
        },
        "storage_id": _STORE,
    }


def _initial(plan: DirectEvaluationPlan) -> GenerationState:
    """Create the same conservative initial generation as the verified bootstrap."""
    return GenerationState(
        storage_id=_STORE,
        owner_entry_id="entry",
        generation=_GENERATION,
        commit_revision=1,
        segment_fingerprint=plan.segment_fingerprint,
        measurement=MeasurementPipelineState.initial(plan.sources, _START),
        ledger=None,
        totals=CumulativeTotals(),
        consumer_totals=tuple(
            (identity, CumulativeTotals()) for identity in plan.consumer_ids
        ),
    )


def _vector(
    plan: DirectEvaluationPlan,
    period: datetime,
    increments: dict[str, str] | None = None,
    *,
    reported_at: datetime | None = None,
) -> tuple[EnergyCounterSample, ...]:
    """Copy a whole synchronized counter vector, starting from 100 exact kWh."""
    amounts = increments or {}
    return tuple(
        EnergyCounterSample(
            source=source,
            cumulative=Energy(Fraction(100) + Fraction(amounts.get(source.role, "0"))),
            source_unit=EnergyUnit.KILOWATT_HOUR,
            period_end=period,
            last_reported=reported_at or period,
        )
        for source in plan.sources
    )


def _sample(time: datetime = _END, value: str = "400") -> GridIntensitySample:
    """Build the sole current-poll CO₂ sample from an exact factor."""
    return GridIntensitySample(Fraction(value), time, _GRID)


def _poll(
    state: GenerationState,
    plan: DirectEvaluationPlan,
    vector: tuple[EnergyObservation, ...],
    observed_at: datetime,
    sample: GridIntensitySample | None,
) -> EvaluationOutcome:
    """Check full codec, revision, and conservation invariants after each proposal."""
    before = deepcopy(state)
    outcome = evaluate_observations(
        state, vector, observed_at, plan=plan, current_grid_sample=sample
    )
    assert state == before
    codec = GenerationCodec(_STORE, "entry", _GENERATION)
    assert codec.decode(codec.encode(outcome.state)) == outcome.state
    if outcome.state == state:
        assert outcome.state is state
    else:
        assert outcome.state.commit_revision == state.commit_revision + 1
        GenerationRevisionPolicy.validate_transition(state, outcome.state)
    return outcome


def _baseline(plan: DirectEvaluationPlan) -> GenerationState:
    """Accept an initial vector without accounting its crossed installation interval."""
    result = _poll(_initial(plan), plan, _vector(plan, _START), _START, _sample(_START))
    assert not result.interval_processed
    assert result.measurement_fault is None
    assert result.grid_error is None
    assert result.state.totals == CumulativeTotals()
    return result.state


def _reference_increments() -> dict[str, str]:
    """Match ADR smartmeter reference PV4, import1, export2, household2, wallbox1."""
    return {
        "pv_generation": "4",
        "pv_plausibility": "4",
        "grid_import": "1",
        "grid_export": "2",
        "local_load": "3",
        "household": "2",
        f"consumer:{_WALLBOX}": "1",
    }


def test_evaluator_module_imports_without_home_assistant() -> None:
    """Evaluation itself requires no HA package or live state access."""
    result = subprocess.run(
        [sys.executable, "-S", "-c", "import custom_components.co2saver.evaluation"],
        cwd=Path(__file__).parents[1],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_plan_and_outcome_are_immutable_and_detached() -> None:
    """Mutating a UI mapping cannot change already-selected accounting parameters."""
    config = _config()
    plan = DirectEvaluationPlan.from_config(config)
    config["factors"]["pv_factor"] = "123"
    config["consumption"]["consumers"][0]["share"] = "0.9"
    assert plan.pv_lifecycle.grams_per_kwh == 40
    assert plan.shares[0].share.value == Fraction(1, 4)
    with pytest.raises(FrozenInstanceError):
        plan.grid_max_age_minutes = 10
    outcome = EvaluationOutcome(_initial(plan))
    with pytest.raises(FrozenInstanceError):
        outcome.interval_processed = True


def test_battery_configurations_cannot_reach_the_direct_evaluator() -> None:
    """Storage remains inactive until its separate accepted implementation issue."""
    config = _config()
    config["battery"] = {
        "battery_id": "6" * 32,
        "charge_source": "8" * 32,
        "discharge_source": "9" * 32,
        "usable_capacity_kwh": "10",
        "round_trip_efficiency": "0.9",
    }
    config["factors"]["battery_factor"] = "12"
    with pytest.raises(ValueError, match="without storage"):
        DirectEvaluationPlan.from_config(config)


@pytest.mark.parametrize("topology", ["inverter", "smart_meter"])
@pytest.mark.parametrize("mode", ["aggregate_shares", "separate_meters"])
def test_complete_reference_interval_preserves_system_and_consumer_bounds(
    topology: str, mode: str
) -> None:
    """Consumers receive independently proven PV, with exact unassigned remainder."""
    plan = DirectEvaluationPlan.from_config(_config(topology, mode))
    before = _baseline(plan)
    outcome = _poll(
        before, plan, _vector(plan, _END, _reference_increments()), _END, _sample()
    )
    assert outcome.interval_processed
    assert outcome.measurement_fault is None
    assert outcome.grid_error is None
    assert outcome.state.measurement.phase is MeasurementPhase.ACTIVE
    totals = outcome.state.totals
    assert (
        totals.direct_pv_kwh,
        totals.direct_gross_g,
        totals.direct_pv_burden_g,
        totals.direct_net_g,
    ) == (2, 800, 80, 720)
    consumers = dict(outcome.state.consumer_totals)
    household_energy = Fraction(5, 4) if mode == "aggregate_shares" else Fraction(1)
    assert consumers[_HOUSE].direct_pv_kwh == household_energy
    assert consumers[_HOUSE].direct_net_g == household_energy * 360
    assert consumers[_WALLBOX] == CumulativeTotals()
    assert outcome.state.unassigned_direct_kwh == 2 - household_energy
    assert all(count == 0 for _, count in outcome.state.diagnostics)


@pytest.mark.parametrize("topology", ["inverter", "smart_meter"])
@pytest.mark.parametrize(
    ("pv", "load", "grid_import", "grid_export", "expected"),
    [
        ("3", "3", "0", "0", "3"),
        ("5", "3", "0", "2", "3"),
        ("3", "0", "0", "3", "0"),
        ("0", "3", "3", "0", "0"),
        ("4", "3", "1", "2", "2"),
        ("0", "0", "0", "0", "0"),
        (
            "0.333333333333333333333333333333333",
            "0.333333333333333333333333333333333",
            "0",
            "0",
            "0.333333333333333333333333333333333",
        ),
    ],
)
def test_supported_household_scenarios_never_credit_export_or_grid(  # noqa: PLR0913
    *,
    topology: str,
    pv: str,
    load: str,
    grid_import: str,
    grid_export: str,
    expected: str,
) -> None:
    """Only guaranteed self-consumed PV receives a once-only exact lifecycle burden."""
    plan = DirectEvaluationPlan.from_config(_config(topology, additional=False))
    increments = {
        "pv_generation": pv,
        "local_load": load,
        "grid_import": grid_import,
        "grid_export": grid_export,
    }
    outcome = _poll(
        _baseline(plan), plan, _vector(plan, _END, increments), _END, _sample()
    )
    assert outcome.interval_processed
    assert outcome.state.totals.direct_pv_kwh == Fraction(expected)
    assert outcome.state.totals.direct_gross_g == Fraction(expected) * 400
    assert outcome.state.totals.direct_pv_burden_g == Fraction(expected) * 40
    assert outcome.state.unassigned_direct_kwh == 0
    assert outcome.state.consumer_totals == ((_HOUSE, outcome.state.totals),)


def test_negative_net_and_exact_factors_are_not_clamped_or_rounded() -> None:
    """All exact lifecycle digits survive a negative net result."""
    config = _config(additional=False)
    config["factors"]["pv_factor"] = "40.000000000000000000000000000000001"
    plan = DirectEvaluationPlan.from_config(config)
    outcome = _poll(
        _baseline(plan),
        plan,
        _vector(plan, _END, _reference_increments()),
        _END,
        _sample(value="20"),
    )
    expected = 2 * (Fraction(20) - Fraction(config["factors"]["pv_factor"]))
    assert outcome.state.totals.direct_net_g == expected
    assert expected < -40


@pytest.mark.parametrize(
    ("sample_time", "error"),
    [
        (_END, None),
        (_END - timedelta(minutes=60), None),
        (_END - timedelta(minutes=60, microseconds=1), "grid_source_stale"),
        (_END + timedelta(microseconds=1), "future_last_reported"),
        (_END + timedelta(seconds=45), "future_last_reported"),
    ],
)
def test_current_grid_sample_uses_physical_interval_end_with_inclusive_limits(
    sample_time: datetime, error: str | None
) -> None:
    """A sample before the poll may still be future relative to the actual interval."""
    plan = DirectEvaluationPlan.from_config(_config())
    outcome = _poll(
        _baseline(plan),
        plan,
        _vector(plan, _END, _reference_increments()),
        _END + timedelta(minutes=1),
        _sample(sample_time),
    )
    assert outcome.interval_processed
    assert outcome.grid_error == error
    assert outcome.state.totals.direct_pv_kwh == 2
    assert outcome.state.totals.direct_gross_g == (800 if error is None else 0)
    assert outcome.state.totals.direct_pv_burden_g == (80 if error is None else 0)
    assert outcome.state.totals.unvalued_direct_kwh == (0 if error is None else 2)
    assert dict(outcome.state.diagnostics)["missing_grid_intensity"] == (
        0 if error is None else 1
    )


def test_missing_grid_never_revalues_energy_on_later_valid_duplicate_or_interval() -> (
    None
):
    """Physical energy advances while all CO₂ components of the gap remain zero."""
    plan = DirectEvaluationPlan.from_config(_config())
    first_vector = _vector(plan, _END, _reference_increments())
    first = _poll(_baseline(plan), plan, first_vector, _END, None)
    assert first.grid_error == "source_unavailable"
    assert first.state.totals.unvalued_direct_kwh == 2
    assert dict(first.state.consumer_totals)[_HOUSE].unvalued_direct_kwh == Fraction(
        5, 4
    )
    restored = GenerationCodec(_STORE, "entry", _GENERATION).decode(
        GenerationCodec.encode(first.state)
    )
    duplicate = _poll(restored, plan, first_vector, _END, _sample())
    assert not duplicate.interval_processed
    assert duplicate.state == first.state
    second_end = _END + timedelta(minutes=1)
    doubled = {
        role: str(Fraction(value) * 2)
        for role, value in _reference_increments().items()
    }
    second = _poll(
        duplicate.state,
        plan,
        _vector(plan, second_end, doubled),
        second_end,
        _sample(second_end),
    )
    assert second.state.totals.direct_pv_kwh == 4
    assert second.state.totals.unvalued_direct_kwh == 2
    assert second.state.totals.direct_gross_g == 800
    assert second.state.totals.direct_pv_burden_g == 80
    assert dict(second.state.diagnostics)["missing_grid_intensity"] == 1


def test_partial_candidate_uses_only_completion_poll_grid_sample_after_restore() -> (
    None
):
    """A favorable factor read with the first partial vector is never cached."""
    plan = DirectEvaluationPlan.from_config(_config())
    state = _baseline(plan)
    baseline_vector = _vector(plan, _START)
    first_vector = _vector(
        plan, _END, _reference_increments(), reported_at=_END + timedelta(seconds=1)
    )
    partial = (first_vector[0], *baseline_vector[1:])
    waiting = _poll(
        state,
        plan,
        partial,
        _END + timedelta(seconds=10),
        _sample(_END - timedelta(seconds=1)),
    )
    assert waiting.state.measurement.candidate is not None
    assert not waiting.interval_processed
    assert waiting.state.totals == CumulativeTotals()
    payload = GenerationCodec.encode(waiting.state)
    assert _GRID not in json.dumps(payload)
    restored = GenerationCodec(_STORE, "entry", _GENERATION).decode(payload)
    complete_vector = _vector(
        plan, _END, _reference_increments(), reported_at=_END + timedelta(seconds=30)
    )
    completed = _poll(
        restored,
        plan,
        complete_vector,
        _END + timedelta(minutes=1),
        _sample(_END + timedelta(seconds=45), "600"),
    )
    assert completed.interval_processed
    assert completed.grid_error == "future_last_reported"
    assert completed.state.totals.direct_pv_kwh == 2
    assert completed.state.totals.unvalued_direct_kwh == 2
    assert completed.state.totals.direct_gross_g == 0
    assert completed.state.measurement.candidate is None


def test_baseline_duplicate_and_pending_polls_report_grid_quality_without_commits() -> (
    None
):
    """Current availability updates do not create emissions or diagnostic history."""
    plan = DirectEvaluationPlan.from_config(_config())
    baseline = _poll(_initial(plan), plan, _vector(plan, _START), _START, None)
    assert baseline.grid_error == "source_unavailable"
    assert all(count == 0 for _, count in baseline.state.diagnostics)
    duplicate = _poll(
        baseline.state,
        plan,
        _vector(plan, _START),
        _START + timedelta(seconds=20),
        _sample(_START + timedelta(seconds=10)),
    )
    assert duplicate.state is baseline.state
    assert duplicate.grid_error is None
    changed_report = tuple(
        replace(sample, last_reported=_START + timedelta(seconds=30))
        for sample in _vector(plan, _START)
    )
    stale_grid = _poll(
        duplicate.state,
        plan,
        changed_report,
        _START + timedelta(seconds=30),
        _sample(_START - timedelta(minutes=60)),
    )
    assert stale_grid.state is duplicate.state
    assert stale_grid.grid_error == "grid_source_stale"
    partial_vector = (
        _vector(plan, _END, _reference_increments())[0],
        *_vector(plan, _START)[1:],
    )
    waiting = _poll(stale_grid.state, plan, partial_vector, _END, None)
    assert waiting.grid_error == "source_unavailable"
    repeat = _poll(waiting.state, plan, partial_vector, _END, _sample())
    assert repeat.state is waiting.state
    assert repeat.grid_error is None
    assert all(count == 0 for _, count in repeat.state.diagnostics)


@pytest.mark.parametrize(
    "fault_kind", ["unavailable", "reset", "unit", "rollback", "imbalance", "long_gap"]
)
def test_energy_interruptions_count_once_and_recovery_never_books_gap(
    fault_kind: str,
) -> None:
    """Rejected energy cannot create results or repeatedly increment interruptions."""
    plan = DirectEvaluationPlan.from_config(_config())
    state = _baseline(plan)
    period = _END if fault_kind != "long_gap" else _START + timedelta(seconds=901)
    vector: tuple[EnergyObservation, ...] = _vector(
        plan, period, _reference_increments()
    )
    bad = cast("EnergyCounterSample", vector[0])
    if fault_kind == "unavailable":
        observation: EnergyObservation = InvalidEnergySample(
            bad.source, MeasurementRejectionReason.SOURCE_UNAVAILABLE
        )
    elif fault_kind == "reset":
        observation = replace(bad, cumulative=Energy.from_kwh("0"))
    elif fault_kind == "unit":
        observation = replace(bad, source_unit=EnergyUnit.WATT_HOUR)
    elif fault_kind == "rollback":
        observation = replace(
            bad,
            period_end=_START - timedelta(seconds=1),
            last_reported=_START - timedelta(seconds=1),
        )
    elif fault_kind == "imbalance":
        observation = replace(bad, cumulative=Energy.from_kwh("199"))
    else:
        observation = bad
    vector = (observation, *vector[1:])
    rejected = _poll(state, plan, vector, period, _sample(period))
    assert rejected.measurement_fault is not None
    assert not rejected.interval_processed
    assert rejected.state.measurement.phase is MeasurementPhase.AWAITING_REBASELINE
    assert rejected.state.totals == CumulativeTotals()
    assert dict(rejected.state.diagnostics)["discarded_intervals"] == 1
    # An accepted-old-period replay cannot recover the interrupted segment.
    # The rejected newer vector itself may serve as a later recovery baseline:
    # recovery deliberately skips the contextual delta/balance validation.
    repeated = _poll(
        rejected.state, plan, _vector(plan, _START), period, _sample(period)
    )
    assert dict(repeated.state.diagnostics)["discarded_intervals"] == 1
    assert repeated.state.totals == CumulativeTotals()
    recovery_period = period + timedelta(minutes=1)
    recovered = _poll(
        repeated.state,
        plan,
        _vector(plan, recovery_period),
        recovery_period,
        _sample(recovery_period),
    )
    assert recovered.state.measurement.phase is MeasurementPhase.ACTIVE
    assert not recovered.interval_processed
    next_period = recovery_period + timedelta(minutes=1)
    good = _poll(
        recovered.state,
        plan,
        _vector(plan, next_period, _reference_increments()),
        next_period,
        _sample(next_period),
    )
    assert good.state.totals.direct_pv_kwh == 2
    assert dict(good.state.diagnostics)["discarded_intervals"] == 1


def test_invalid_initial_observation_does_not_count_an_unstarted_interval() -> None:
    """A missing first baseline neither invents an interval nor a discard count."""
    plan = DirectEvaluationPlan.from_config(_config())
    state = _initial(plan)
    initial = _vector(plan, _START)
    invalid = (
        InvalidEnergySample(
            initial[0].source, MeasurementRejectionReason.INVALID_VALUE
        ),
        *initial[1:],
    )
    outcome = _poll(state, plan, invalid, _START, _sample(_START))
    assert outcome.measurement_fault is not None
    assert outcome.state is state
    assert all(count == 0 for _, count in outcome.state.diagnostics)


def test_smartmeter_optional_pv_check_is_not_a_second_truth_source() -> None:
    """A contradictory optional meter invalidates the authoritative site balance."""
    config = _config("smart_meter")
    config["sources"]["pv_plausibility"] = "1" * 32
    plan = DirectEvaluationPlan.from_config(config)
    valid = _poll(
        _baseline(plan),
        plan,
        _vector(plan, _END, _reference_increments()),
        _END,
        _sample(),
    )
    assert valid.state.totals.direct_pv_kwh == 2
    increments = {**_reference_increments(), "pv_plausibility": "8"}
    invalid = _poll(
        _baseline(plan), plan, _vector(plan, _END, increments), _END, _sample()
    )
    assert invalid.measurement_fault is not None
    assert (
        invalid.measurement_fault.interval_reason
        is IntervalRejectionReason.PV_PLAUSIBILITY_MISMATCH
    )
    assert invalid.state.totals == CumulativeTotals()


@pytest.mark.parametrize("mismatch", ["fingerprint", "sources", "consumer", "ledger"])
def test_miswired_generation_is_rejected_before_advancing(mismatch: str) -> None:
    """A restored generation must belong to this exact segment and consumer plan."""
    plan = DirectEvaluationPlan.from_config(_config())
    state = _baseline(plan)
    if mismatch == "fingerprint":
        state = replace(state, segment_fingerprint="0" * 64)
    elif mismatch == "sources":
        state = replace(
            state,
            measurement=MeasurementPipelineState.initial(
                (EnergySourceIdentity("foreign", "0" * 32),), _START
            ),
        )
    elif mismatch == "consumer":
        state = replace(state, consumer_totals=((_HOUSE, CumulativeTotals()),))
    else:
        state = replace(state, ledger=StorageLedger.quarantined(Energy.from_kwh("10")))
    with pytest.raises(ValueError, match="evaluation plan"):
        evaluate_observations(
            state,
            _vector(plan, _END, _reference_increments()),
            _END,
            plan=plan,
            current_grid_sample=_sample(),
        )


def test_assembler_requires_registry_and_role_identity_but_not_input_order() -> None:
    """Equal numerical deltas from a different physical source are inadmissible."""
    plan = DirectEvaluationPlan.from_config(_config())
    amounts = _reference_increments()
    deltas = tuple(
        EnergyDelta(source, Energy.from_kwh(amounts[source.role]))
        for source in plan.sources
    )
    batch = RawEnergyDeltaBatch(IntervalWindow(_START, _END), tuple(reversed(deltas)))
    assert isinstance(plan.assemble_interval(batch), NormalizedInterval)
    foreign = replace(
        deltas[0], source=EnergySourceIdentity(deltas[0].source.role, "0" * 32)
    )
    with pytest.raises(ValueError, match="sources do not match"):
        plan.assemble_interval(replace(batch, deltas=(foreign, *deltas[1:])))


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("source_registry_id", "0" * 32, "grid_source_mismatch"),
        ("value_g_co2e_per_kwh", Fraction(-1), "invalid_grid_value"),
        ("value_g_co2e_per_kwh", 400.0, "invalid_grid_value"),
        ("observed_at", _END.replace(tzinfo=None), "invalid_last_reported"),
    ],
)
def test_malformed_current_grid_observation_never_values_energy(
    field: str, value: object, error: str
) -> None:
    """A defensive evaluator guard rejects a miswired or malformed adapter copy."""
    plan = DirectEvaluationPlan.from_config(_config())
    raw: dict[str, object] = {
        "source_registry_id": _GRID,
        "value_g_co2e_per_kwh": Fraction(400),
        "observed_at": _END,
    }
    raw[field] = value
    sample = cast("GridIntensitySample", SimpleNamespace(**raw))
    outcome = _poll(
        _baseline(plan),
        plan,
        _vector(plan, _END, _reference_increments()),
        _END,
        sample,
    )
    assert outcome.grid_error == error
    assert outcome.state.totals.direct_pv_kwh == 2
    assert outcome.state.totals.unvalued_direct_kwh == 2
    assert outcome.state.totals.direct_gross_g == 0


def test_archived_consumer_and_storage_history_are_preserved_without_revaluation() -> (
    None
):
    """Battery-free future processing does not erase already recorded old segments."""
    plan = DirectEvaluationPlan.from_config(_config())
    state = _baseline(plan)
    historical = CumulativeTotals(
        direct_pv_kwh=Fraction(1),
        direct_gross_g=Fraction(100),
        direct_pv_burden_g=Fraction(120),
        storage_pv_kwh=Fraction(2),
        storage_gross_g=Fraction(800),
        storage_pv_burden_g=Fraction(80),
        storage_burden_g=Fraction(24),
    )
    state = replace(
        state,
        totals=historical,
        consumer_totals=tuple(sorted((*state.consumer_totals, ("c" * 32, historical)))),
    )
    outcome = _poll(
        state, plan, _vector(plan, _END, _reference_increments()), _END, _sample()
    )
    assert dict(outcome.state.consumer_totals)["c" * 32] == historical
    assert outcome.state.totals.direct_pv_kwh == 3
    assert outcome.state.totals.direct_gross_g == 900
    assert outcome.state.totals.direct_pv_burden_g == 200
    assert outcome.state.totals.storage_pv_kwh == 2
    assert outcome.state.totals.storage_net_g == historical.storage_net_g
