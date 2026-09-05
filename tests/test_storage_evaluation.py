# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Atomic storage provenance and discharge accounting through the pure evaluator."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from fractions import Fraction
from typing import TYPE_CHECKING, Any

import pytest

from custom_components.co2saver.domain import (
    EmissionDensity,
    Emissions,
    Energy,
    IntervalRejectionReason,
    StorageLedger,
    StorageRejectionReason,
)
from custom_components.co2saver.evaluation import EvaluationPlan
from custom_components.co2saver.measurement.models import (
    EnergyUnit,
    InvalidEnergySample,
    MeasurementPhase,
    MeasurementRejectionReason,
)
from custom_components.co2saver.persistence import (
    CumulativeTotals,
    GenerationCodec,
)

from .test_evaluation import (
    _GENERATION,
    _HOUSE,
    _START,
    _STORE,
    _WALLBOX,
    _config,
    _initial,
    _poll,
    _sample,
    _vector,
)

if TYPE_CHECKING:
    from datetime import datetime

    from custom_components.co2saver.evaluation import EvaluationOutcome
    from custom_components.co2saver.measurement.models import (
        EnergyCounterSample,
        EnergyObservation,
    )
    from custom_components.co2saver.persistence import GenerationState


def _storage_config(
    topology: str = "inverter", mode: str = "aggregate_shares"
) -> dict[str, Any]:
    """Extend the supported configured topology with exact storage parameters."""
    config = _config(topology, mode)
    config["battery"] = {
        "battery_id": "6" * 32,
        "charge_source": "8" * 32,
        "discharge_source": "9" * 32,
        "usable_capacity_kwh": "10",
        "round_trip_efficiency": "0.9",
    }
    config["factors"]["battery_factor"] = "20"
    return config


def _storage_baseline(plan: EvaluationPlan) -> GenerationState:
    """Start with unknown provenance, accepting the first vector without a delta."""
    assert plan.storage is not None
    initial = replace(
        _initial(plan), ledger=StorageLedger.quarantined(plan.storage.capacity)
    )
    result = _poll(initial, plan, _vector(plan, _START), _START, _sample(_START))
    assert not result.interval_processed
    assert result.storage_error is None
    assert result.state.ledger == initial.ledger
    return result.state


def _flows(  # noqa: PLR0913 - physical interval components are independently measured
    *,
    pv: str = "0",
    imported: str = "0",
    exported: str = "0",
    charge: str = "0",
    discharge: str = "0",
    household: str = "0",
    wallbox: str = "0",
) -> dict[str, Fraction]:
    """Provide one physical interval in either topology and load input mode."""
    return {
        "pv_generation": Fraction(pv),
        "pv_plausibility": Fraction(pv),
        "grid_import": Fraction(imported),
        "grid_export": Fraction(exported),
        "battery_charge": Fraction(charge),
        "battery_discharge": Fraction(discharge),
        "local_load": Fraction(household) + Fraction(wallbox),
        "household": Fraction(household),
        f"consumer:{_WALLBOX}": Fraction(wallbox),
    }


def _next_vector(
    state: GenerationState,
    increments: dict[str, Fraction],
    *,
    period: datetime | None = None,
) -> tuple[EnergyCounterSample, ...]:
    """Advance actual accepted cumulative values, retaining exact fractional kWh."""
    baseline = state.measurement.baseline
    assert baseline is not None
    endpoint = period or baseline.period_end + timedelta(minutes=1)
    return tuple(
        replace(
            sample,
            cumulative=Energy(
                sample.cumulative.kwh + increments.get(sample.source.role, Fraction())
            ),
            period_end=endpoint,
            last_reported=endpoint,
        )
        for sample in baseline.samples
    )


def _step(
    state: GenerationState,
    plan: EvaluationPlan,
    increments: dict[str, Fraction],
    *,
    grid: str | None = "400",
) -> EvaluationOutcome:
    """Evaluate a complete next-period vector and validate its persistence contract."""
    vector = _next_vector(state, increments)
    period = vector[0].period_end
    return _poll(state, plan, vector, period, _sample(period, grid) if grid else None)


def _empty(plan: EvaluationPlan) -> GenerationState:
    """Prove empty bounds through actual discharge of the full possible inventory."""
    assert plan.storage is not None
    capacity = str(plan.storage.capacity.kwh)
    initial = _storage_baseline(plan)
    result = _step(initial, plan, _flows(discharge=capacity, household=capacity))
    assert result.interval_processed
    assert result.state.totals == CumulativeTotals()
    assert result.state.ledger is not None
    assert result.state.ledger.stored_upper.kwh == 0
    assert result.state.ledger.pv_lower.kwh == 0
    return result.state


def _charged(plan: EvaluationPlan) -> GenerationState:
    """Create a proven PV inventory of 2.7 kWh with 120 g deferred burden."""
    return _step(_empty(plan), plan, _flows(pv="3", charge="3")).state


@pytest.mark.parametrize("topology", ["inverter", "smart_meter"])
@pytest.mark.parametrize("mode", ["aggregate_shares", "separate_meters"])
def test_adr_lossy_charge_and_discharge_commit_exact_components(
    topology: str, mode: str
) -> None:
    """ADR 9.3 preserves charge loss and defers benefit until local discharge."""
    plan = EvaluationPlan.from_config(_storage_config(topology, mode))
    state = _empty(plan)
    charge = _step(state, plan, _flows(pv="6", household="2", charge="3", exported="1"))
    assert charge.interval_processed
    assert charge.storage_error is None
    assert charge.state.ledger is not None
    assert charge.state.ledger.pv_lower.kwh == Fraction(27, 10)
    assert charge.state.ledger.pv_burden.grams == 120
    assert charge.state.ledger.pv_density_upper.grams_per_kwh == Fraction(400, 9)
    assert charge.state.totals.direct_pv_kwh == 2
    assert charge.state.totals.direct_net_g == 720
    assert charge.state.totals.storage_pv_kwh == 0
    assert charge.state.totals.storage_gross_g == 0
    assert charge.state.totals.storage_pv_burden_g == 0
    assert charge.state.totals.storage_burden_g == 0

    discharge = _step(
        charge.state, plan, _flows(discharge="2", household="2"), grid="500"
    )
    assert discharge.interval_processed
    totals = discharge.state.totals
    assert totals.direct_pv_kwh == 2
    assert totals.direct_net_g == 720
    assert totals.storage_pv_kwh == 2
    assert totals.storage_gross_g == 1000
    assert totals.storage_pv_burden_g == Fraction(800, 9)
    assert totals.storage_burden_g == 40
    assert totals.storage_net_g == Fraction(7840, 9)
    assert discharge.state.ledger is not None
    assert discharge.state.ledger.pv_lower.kwh == Fraction(7, 10)
    assert discharge.state.ledger.pv_burden.grams == Fraction(280, 9)
    assert all(count == 0 for _, count in discharge.state.diagnostics)


@pytest.mark.parametrize("topology", ["inverter", "smart_meter"])
@pytest.mark.parametrize("mode", ["aggregate_shares", "separate_meters"])
@pytest.mark.parametrize(
    ("household", "exported", "expected"),
    [("2", "0", Fraction(11, 10)), ("1", "1", Fraction(1, 10)), ("0", "2", Fraction())],
)
def test_mixed_inventory_uses_conservative_local_bound_and_no_export_credit(
    topology: str, mode: str, household: str, exported: str, expected: Fraction
) -> None:
    """ADR 9.4 spends the non-PV upper bound before guaranteeing local PV."""
    plan = EvaluationPlan.from_config(_storage_config(topology, mode))
    charged = _step(_empty(plan), plan, _flows(pv="3", imported="1", charge="4"))
    assert charged.state.ledger is not None
    assert charged.state.ledger.pv_lower.kwh == Fraction(27, 10)
    assert charged.state.ledger.non_pv_upper.kwh == Fraction(9, 10)
    result = _step(
        charged.state,
        plan,
        _flows(discharge="2", household=household, exported=exported),
    )
    assert result.interval_processed
    assert result.state.totals.direct_pv_kwh == 0
    assert result.state.totals.storage_pv_kwh == expected
    assert result.state.totals.storage_gross_g == expected * 400
    assert result.state.totals.storage_pv_burden_g == expected * Fraction(400, 9)
    assert result.state.totals.storage_burden_g == expected * 20
    assert result.state.ledger is not None
    assert result.state.ledger.pv_lower.kwh == Fraction(7, 10)
    assert result.state.ledger.pv_burden.grams == Fraction(280, 9)


@pytest.mark.parametrize("topology", ["inverter", "smart_meter"])
def test_grid_only_storage_charge_never_becomes_pv(topology: str) -> None:
    """Known grid inventory can be discharged without any PV benefit or burden."""
    plan = EvaluationPlan.from_config(_storage_config(topology))
    charged = _step(_empty(plan), plan, _flows(imported="3", charge="3"))
    assert charged.state.ledger is not None
    assert charged.state.ledger.pv_lower.kwh == 0
    discharged = _step(charged.state, plan, _flows(discharge="2", household="2"))
    assert discharged.interval_processed
    assert discharged.state.totals == CumulativeTotals()


@pytest.mark.parametrize("grid_problem", ["missing", "future", "stale", "wrong_source"])
def test_invalid_current_grid_consumes_provenance_without_later_revaluation(
    grid_problem: str,
) -> None:
    """Remove physical P/B and retain unvalued energy without using cached grid data."""
    plan = EvaluationPlan.from_config(_storage_config())
    charged = _charged(plan)
    vector = _next_vector(charged, _flows(pv="1", discharge="2", household="3"))
    period = vector[0].period_end
    sample = _sample(period)
    if grid_problem == "missing":
        sample = None
    elif grid_problem == "future":
        sample = _sample(period + timedelta(seconds=1))
    elif grid_problem == "stale":
        sample = _sample(period - timedelta(minutes=60, seconds=1))
    else:
        sample = replace(sample, source_registry_id="c" * 32)
    result = _poll(charged, plan, vector, period, sample)
    assert result.interval_processed
    assert result.grid_error is not None
    totals = result.state.totals
    assert totals.direct_pv_kwh == totals.unvalued_direct_kwh == 1
    assert totals.storage_pv_kwh == totals.unvalued_storage_kwh == 2
    assert totals.direct_gross_g == totals.direct_pv_burden_g == 0
    assert (
        totals.storage_gross_g
        == totals.storage_pv_burden_g
        == totals.storage_burden_g
        == 0
    )
    assert dict(result.state.diagnostics)["missing_grid_intensity"] == 1
    assert result.state.ledger is not None
    assert result.state.ledger.pv_lower.kwh == Fraction(7, 10)
    assert result.state.ledger.pv_burden.grams == Fraction(280, 9)

    repeated = _poll(result.state, plan, vector, period, _sample(period, "1000"))
    assert repeated.state is result.state
    assert not repeated.interval_processed
    later = _step(
        result.state, plan, _flows(discharge="0.7", household="0.7"), grid="500"
    )
    assert later.state.totals.unvalued_direct_kwh == 1
    assert later.state.totals.unvalued_storage_kwh == 2
    assert later.state.totals.storage_pv_kwh == Fraction(27, 10)
    assert later.state.totals.storage_gross_g == 350
    assert later.state.totals.storage_pv_burden_g == Fraction(280, 9)
    assert later.state.totals.storage_burden_g == 14
    assert later.state.ledger is not None
    assert later.state.ledger.pv_lower.kwh == later.state.ledger.pv_burden.grams == 0


def test_missing_grid_at_charge_retains_eligibility_and_uses_discharge_poll_only() -> (
    None
):
    """Charging retains the exact lifecycle burden without current grid intensity."""
    plan = EvaluationPlan.from_config(_storage_config())
    charged = _step(_empty(plan), plan, _flows(pv="3", charge="3"), grid=None)
    assert charged.state.ledger is not None
    assert charged.state.ledger.pv_lower.kwh == Fraction(27, 10)
    assert charged.state.ledger.pv_burden.grams == 120
    assert charged.state.totals == CumulativeTotals()
    discharged = _step(
        charged.state, plan, _flows(discharge="2", household="2"), grid="123"
    )
    assert discharged.state.totals.storage_gross_g == 246
    assert discharged.state.totals.storage_pv_burden_g == Fraction(800, 9)
    assert discharged.state.totals.unvalued_storage_kwh == 0
    assert dict(discharged.state.diagnostics)["missing_grid_intensity"] == 1


@pytest.mark.parametrize("topology", ["inverter", "smart_meter"])
@pytest.mark.parametrize("mode", ["aggregate_shares", "separate_meters"])
def test_direct_and_storage_share_one_atomic_revision_without_double_counting(
    topology: str, mode: str
) -> None:
    """Separate physical origins advance their system and consumer components once."""
    plan = EvaluationPlan.from_config(_storage_config(topology, mode))
    charged = _charged(plan)
    result = _step(
        charged, plan, _flows(pv="2", discharge="2", household="3", wallbox="1")
    )
    assert result.state.commit_revision == charged.commit_revision + 1
    assert result.state.measurement.revision == charged.measurement.revision + 1
    assert result.state.totals.direct_pv_kwh == result.state.totals.storage_pv_kwh == 2
    consumers = dict(result.state.consumer_totals)
    assert consumers[_HOUSE].direct_pv_kwh == consumers[_HOUSE].storage_pv_kwh == 1
    assert consumers[_WALLBOX].direct_pv_kwh == consumers[_WALLBOX].storage_pv_kwh == 0
    assert (
        result.state.unassigned_direct_kwh == result.state.unassigned_storage_kwh == 1
    )


@pytest.mark.parametrize("topology", ["inverter", "smart_meter"])
@pytest.mark.parametrize("rejection", ["overflow", "excess_discharge"])
def test_storage_rejection_discards_whole_interval_and_quarantines_once(
    topology: str, rejection: str
) -> None:
    """A valid direct-PV portion cannot escape a failed storage transition."""
    plan = EvaluationPlan.from_config(_storage_config(topology))
    charged = _charged(plan)
    increments = (
        _flows(pv="10", charge="9", household="1")
        if rejection == "overflow"
        else _flows(pv="1", discharge="3", household="4")
    )
    vector = _next_vector(charged, increments)
    period = vector[0].period_end
    result = _poll(charged, plan, vector, period, None)
    assert not result.interval_processed
    assert result.measurement_fault is None
    assert result.storage_error is (
        StorageRejectionReason.CAPACITY_OVERFLOW
        if rejection == "overflow"
        else StorageRejectionReason.DISCHARGE_EXCEEDS_UPPER_BOUND
    )
    assert result.state.totals == charged.totals
    assert result.state.consumer_totals == charged.consumer_totals
    assert result.state.unassigned_direct_kwh == charged.unassigned_direct_kwh
    assert result.state.unassigned_storage_kwh == charged.unassigned_storage_kwh
    assert result.state.measurement.phase is MeasurementPhase.AWAITING_REBASELINE
    assert result.state.measurement.baseline is charged.measurement.baseline
    assert charged.measurement.baseline is not None
    assert (
        result.state.measurement.recovery_after_period_end
        == charged.measurement.baseline.period_end
    )
    assert plan.storage is not None
    assert result.state.ledger == StorageLedger.quarantined(plan.storage.capacity)
    assert dict(result.state.diagnostics)["discarded_intervals"] == 1
    assert dict(result.state.diagnostics)["missing_grid_intensity"] == 0
    for _ in range(3):
        repeat = _poll(
            result.state,
            plan,
            charged.measurement.baseline.samples,
            period,
            _sample(period),
        )
        assert repeat.state is result.state
        assert dict(repeat.state.diagnostics)["discarded_intervals"] == 1
    # The failed endpoint is newer than the last accepted baseline. It is safe
    # as a recovery baseline, never as a retroactively processed energy interval.
    recovery = _poll(result.state, plan, vector, period, _sample(period))
    assert not recovery.interval_processed
    assert recovery.state.measurement.phase is MeasurementPhase.ACTIVE
    assert recovery.state.totals == charged.totals
    assert recovery.state.ledger == result.state.ledger
    assert dict(recovery.state.diagnostics)["discarded_intervals"] == 1
    next_interval = _step(
        recovery.state, plan, _flows(pv="1", discharge="1", household="2")
    )
    assert next_interval.interval_processed
    assert next_interval.state.totals.direct_pv_kwh == charged.totals.direct_pv_kwh + 1
    assert next_interval.state.totals.storage_pv_kwh == charged.totals.storage_pv_kwh


@pytest.mark.parametrize(
    "fault_kind", ["unavailable", "reset", "unit", "long_gap", "simultaneous"]
)
def test_physical_fault_discards_stored_provenance_and_recovery_has_no_credit(
    fault_kind: str,
) -> None:
    """Every physical interruption clears provenance once while preserving history."""
    plan = EvaluationPlan.from_config(_storage_config())
    charged = _charged(plan)
    assert charged.measurement.baseline is not None
    period = charged.measurement.baseline.period_end + timedelta(
        seconds=901 if fault_kind == "long_gap" else 60
    )
    vector: tuple[EnergyObservation, ...] = _next_vector(
        charged,
        _flows(pv="1", charge="1", discharge="1", household="1")
        if fault_kind == "simultaneous"
        else _flows(discharge="1", household="1"),
        period=period,
    )
    bad = _next_vector(charged, _flows(), period=period)[0]
    if fault_kind == "unavailable":
        vector = (
            InvalidEnergySample(
                bad.source, MeasurementRejectionReason.SOURCE_UNAVAILABLE
            ),
            *vector[1:],
        )
    elif fault_kind == "reset":
        vector = (replace(bad, cumulative=Energy.zero()), *vector[1:])
    elif fault_kind == "unit":
        vector = (replace(bad, source_unit=EnergyUnit.WATT_HOUR), *vector[1:])
    result = _poll(charged, plan, vector, period, _sample(period))
    assert result.measurement_fault is not None
    if fault_kind == "simultaneous":
        assert (
            result.measurement_fault.interval_reason
            is IntervalRejectionReason.SIMULTANEOUS_CHARGE_DISCHARGE
        )
    assert not result.interval_processed
    assert result.storage_error is None
    assert result.state.totals == charged.totals
    assert result.state.measurement.phase is MeasurementPhase.AWAITING_REBASELINE
    assert plan.storage is not None
    assert result.state.ledger == StorageLedger.quarantined(plan.storage.capacity)
    repeated = _poll(
        result.state,
        plan,
        charged.measurement.baseline.samples,
        period,
        _sample(period),
    )
    assert dict(repeated.state.diagnostics)["discarded_intervals"] == 1
    recovery_period = period + timedelta(minutes=1)
    recovery = _poll(
        repeated.state,
        plan,
        _next_vector(charged, _flows(), period=recovery_period),
        recovery_period,
        _sample(recovery_period),
    )
    assert not recovery.interval_processed
    assert recovery.state.measurement.phase is MeasurementPhase.ACTIVE
    discharge = _step(recovery.state, plan, _flows(discharge="2", household="2"))
    assert discharge.interval_processed
    assert discharge.state.totals == charged.totals
    assert dict(discharge.state.diagnostics)["discarded_intervals"] == 1


def test_restart_preserves_partial_lossy_cycles_and_benefit_never_exceeds_origin() -> (
    None
):
    """The stored snapshot restores both meter and ledger state without a gap delta."""
    plan = EvaluationPlan.from_config(_storage_config())
    charged = _charged(plan)
    first = _step(charged, plan, _flows(discharge="1", household="1"))
    codec = GenerationCodec(_STORE, "entry", _GENERATION)
    restored = codec.decode(codec.encode(first.state))
    second = _step(restored, plan, _flows(discharge="1.7", household="1.7"))
    assert second.state.totals.storage_pv_kwh == Fraction(27, 10)
    assert second.state.totals.storage_pv_burden_g == 120
    assert second.state.totals.storage_burden_g == 54
    assert second.state.ledger is not None
    assert second.state.ledger.stored_upper.kwh == second.state.ledger.pv_lower.kwh == 0
    assert second.state.ledger.pv_burden.grams == 0
    rejected = _step(second.state, plan, _flows(discharge="0.1", household="0.1"))
    assert (
        rejected.storage_error is StorageRejectionReason.DISCHARGE_EXCEEDS_UPPER_BOUND
    )
    assert rejected.state.totals == second.state.totals


def test_consumer_lifecycle_envelopes_are_independent_not_proportional() -> None:
    """A valid conservative restored hull may yield non-additive burden views."""
    plan = EvaluationPlan.from_config(_storage_config(mode="separate_meters"))
    state = _storage_baseline(plan)
    state = replace(
        state,
        ledger=StorageLedger(
            capacity=Energy.from_kwh("10"),
            stored_lower=Energy.from_kwh("4"),
            stored_upper=Energy.from_kwh("4"),
            pv_lower=Energy.from_kwh("4"),
            pv_burden=Emissions.from_grams("100"),
            pv_density_upper=EmissionDensity.from_g_per_kwh("100"),
        ),
    )
    result = _step(state, plan, _flows(discharge="2", household="1", wallbox="1"))
    consumers = dict(result.state.consumer_totals)
    assert result.state.totals.storage_pv_kwh == 2
    assert consumers[_HOUSE].storage_pv_kwh == consumers[_WALLBOX].storage_pv_kwh == 1
    assert result.state.unassigned_storage_kwh == 0
    assert result.state.totals.storage_pv_burden_g == 100
    assert consumers[_HOUSE].storage_pv_burden_g == 100
    assert consumers[_WALLBOX].storage_pv_burden_g == 100
    assert (
        sum(total.storage_pv_burden_g for _, total in result.state.consumer_totals)
        == 200
    )


def test_storage_net_can_be_negative_without_rounding_exact_factors() -> None:
    """Loss-adjusted charge burden and all battery factor digits remain visible."""
    config = _storage_config()
    config["factors"]["battery_factor"] = "20.0000000000000000001"
    plan = EvaluationPlan.from_config(config)
    result = _step(_charged(plan), plan, _flows(discharge="2", household="2"), grid="1")
    assert result.state.totals.storage_burden_g == Fraction("40.0000000000000000002")
    assert result.state.totals.storage_net_g == 2 - Fraction(800, 9) - Fraction(
        "40.0000000000000000002"
    )
    assert result.state.totals.storage_net_g < 0


@pytest.mark.parametrize("mismatch", ["missing_ledger", "capacity"])
def test_storage_plan_rejects_mismatched_persisted_ledger(mismatch: str) -> None:
    """Stale storage wiring cannot advance a different physical inventory."""
    plan = EvaluationPlan.from_config(_storage_config())
    state = replace(
        _storage_baseline(plan),
        ledger=None
        if mismatch == "missing_ledger"
        else StorageLedger.quarantined(Energy.from_kwh("9")),
    )
    with pytest.raises(ValueError, match="generation does not match"):
        _step(state, plan, _flows())


def test_smart_meter_optional_pv_source_checks_charge_discharge_balance() -> None:
    """The plausibility signal never replaces derived PV including storage flows."""
    config = _storage_config("smart_meter")
    config["sources"]["pv_plausibility"] = "1" * 32
    plan = EvaluationPlan.from_config(config)
    state = _empty(plan)
    increments = _flows(pv="3", charge="3")
    increments["pv_plausibility"] = Fraction(4)
    result = _step(state, plan, increments)
    assert result.measurement_fault is not None
    assert (
        result.measurement_fault.interval_reason
        is IntervalRejectionReason.PV_PLAUSIBILITY_MISMATCH
    )
    assert not result.interval_processed
    assert result.state.totals == state.totals
