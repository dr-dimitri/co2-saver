# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Pure, atomic direct-PV evaluation of one current observation vector."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Self, cast

from .domain import (
    ConsumerLoad,
    ConsumerLoads,
    ConsumerShare,
    ConsumptionMode,
    DirectEmissionFactors,
    DomainValidationError,
    EmissionBreakdown,
    EmissionFactor,
    Energy,
    InputTopology,
    InverterIntervalInput,
    Ratio,
    SmartMeterIntervalInput,
    calculate_direct_emissions,
    decompose_flows,
    loads_from_meters,
    loads_from_shares,
    normalize_interval,
)
from .measurement.pipeline import advance_measurements

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .config_factors import GridIntensitySample
    from .domain import NormalizedInterval, RejectedInterval
    from .measurement.models import (
        EnergyObservation,
        EnergySourceIdentity,
        MeasurementFault,
        RawEnergyDeltaBatch,
    )
    from .persistence import CumulativeTotals, GenerationState


@dataclass(frozen=True, slots=True)
class DirectEvaluationPlan:
    """Immutable typed settings for a complete, explicitly battery-free segment."""

    topology: InputTopology
    sources: tuple[EnergySourceIdentity, ...]
    consumption_mode: ConsumptionMode
    household_id: str
    shares: tuple[ConsumerShare, ...]
    metered_consumers: tuple[str, ...]
    pv_lifecycle: EmissionFactor
    grid_source_registry_id: str
    grid_max_age_minutes: int
    segment_fingerprint: str

    @classmethod
    def from_config(cls, data: Mapping[str, object]) -> Self:
        """Validate configuration once without retaining mutable config mappings."""
        from .config_plan import (  # noqa: PLC0415 - keep evaluator import HA-free
            canonical_plan,
            segment_fingerprint,
            source_bindings,
        )

        plan = canonical_plan(data)
        if plan["battery"] is not None:
            message = "direct evaluation requires a configuration without storage"
            raise ValueError(message)
        consumers = cast("dict[str, object]", plan["consumption"])
        factors = cast("dict[str, object]", plan["factors"])
        mode = ConsumptionMode(cast("str", consumers["mode"]))
        rows = cast("list[dict[str, str]]", consumers["consumers"])
        return cls(
            topology=InputTopology(cast("str", plan["topology"])),
            sources=source_bindings(plan),
            consumption_mode=mode,
            household_id=cast("str", consumers["household_id"]),
            shares=tuple(
                ConsumerShare(row["consumer_id"], Ratio.from_value(row["share"]))
                for row in rows
            )
            if mode is ConsumptionMode.AGGREGATE_SHARES
            else (),
            metered_consumers=tuple(row["consumer_id"] for row in rows)
            if mode is ConsumptionMode.SEPARATE_METERS
            else (),
            pv_lifecycle=EmissionFactor.from_g_per_kwh(
                cast("str", factors["pv_factor"])
            ),
            grid_source_registry_id=cast("str", factors["grid_intensity_source"]),
            grid_max_age_minutes=cast("int", factors["grid_max_age_minutes"]),
            segment_fingerprint=segment_fingerprint(plan),
        )

    @property
    def consumer_ids(self) -> tuple[str, ...]:
        """Return current consumer identities without losing the household."""
        return tuple(
            sorted(
                (
                    self.household_id,
                    *(share.consumer_id for share in self.shares),
                    *self.metered_consumers,
                )
            )
        )

    def _consumer_loads(self, batch: RawEnergyDeltaBatch) -> ConsumerLoads:
        """Construct exact local loads from one explicit configured input mode."""
        if self.consumption_mode is ConsumptionMode.AGGREGATE_SHARES:
            return loads_from_shares(
                batch.energy_for("local_load"), self.household_id, self.shares
            )
        return loads_from_meters(
            ConsumerLoad(self.household_id, batch.energy_for("household")),
            tuple(
                ConsumerLoad(consumer_id, batch.energy_for(f"consumer:{consumer_id}"))
                for consumer_id in self.metered_consumers
            ),
        )

    def assemble_interval(
        self, batch: RawEnergyDeltaBatch
    ) -> NormalizedInterval | RejectedInterval:
        """Normalize exactly the configured role/registry-owned energy deltas."""
        if {delta.source for delta in batch.deltas} != set(self.sources):
            message = "raw interval sources do not match the evaluation plan"
            raise ValueError(message)
        consumers = self._consumer_loads(batch)
        if self.topology is InputTopology.INVERTER:
            return normalize_interval(
                InverterIntervalInput(
                    window=batch.window,
                    consumers=consumers,
                    pv_generation=batch.energy_for("pv_generation"),
                    grid_import=batch.energy_for("grid_import"),
                    grid_export=batch.energy_for("grid_export"),
                    battery_charge=Energy.zero(),
                    battery_discharge=Energy.zero(),
                )
            )
        roles = {source.role for source in self.sources}
        return normalize_interval(
            SmartMeterIntervalInput(
                window=batch.window,
                consumers=consumers,
                grid_import=batch.energy_for("grid_import"),
                grid_export=batch.energy_for("grid_export"),
                battery_charge=Energy.zero(),
                battery_discharge=Energy.zero(),
                pv_plausibility=batch.energy_for("pv_plausibility")
                if "pv_plausibility" in roles
                else None,
            )
        )


@dataclass(frozen=True, slots=True)
class EvaluationOutcome:
    """Complete proposed generation and current poll quality, before persistence."""

    state: GenerationState
    measurement_fault: MeasurementFault | None = None
    grid_error: str | None = None
    interval_processed: bool = False


def _grid_error(
    sample: GridIntensitySample | None,
    effective_at: datetime,
    plan: DirectEvaluationPlan,
) -> str | None:
    """Validate only this poll's sample, with no historical fallback or caching."""
    if sample is None:
        return "source_unavailable"
    if sample.source_registry_id != plan.grid_source_registry_id:
        return "grid_source_mismatch"
    try:
        EmissionFactor(sample.value_g_co2e_per_kwh)
    except DomainValidationError:
        return "invalid_grid_value"
    if (
        not isinstance(sample.observed_at, datetime)
        or sample.observed_at.tzinfo is not UTC
    ):
        return "invalid_last_reported"
    if sample.observed_at > effective_at:
        return "future_last_reported"
    return (
        "grid_source_stale"
        if effective_at - sample.observed_at
        > timedelta(minutes=plan.grid_max_age_minutes)
        else None
    )


def _validate_state_plan(state: GenerationState, plan: DirectEvaluationPlan) -> None:
    """Reject stale plan wiring before any measurement or emissions advancement."""
    if (
        state.segment_fingerprint != plan.segment_fingerprint
        or state.measurement.sources != plan.sources
        or state.ledger is not None
        or not set(plan.consumer_ids) <= dict(state.consumer_totals).keys()
    ):
        message = "generation does not match its battery-free evaluation plan"
        raise ValueError(message)


def _add_direct_totals(
    previous: CumulativeTotals, energy: Energy, emissions: EmissionBreakdown | None
) -> CumulativeTotals:
    """Advance physical energy and either its emissions or permanent unvalued total."""
    if emissions is None:
        return replace(
            previous,
            direct_pv_kwh=previous.direct_pv_kwh + energy.kwh,
            unvalued_direct_kwh=previous.unvalued_direct_kwh + energy.kwh,
        )
    return replace(
        previous,
        direct_pv_kwh=previous.direct_pv_kwh + energy.kwh,
        direct_gross_g=previous.direct_gross_g + emissions.gross_avoided.grams,
        direct_pv_burden_g=previous.direct_pv_burden_g + emissions.pv_lifecycle.grams,
    )


def _apply_interval(
    state: GenerationState,
    interval: NormalizedInterval,
    plan: DirectEvaluationPlan,
    sample: GridIntensitySample | None,
    *,
    grid_error: str | None,
) -> GenerationState:
    """Apply one domain-proven system result and its independent consumer bounds."""
    flows = decompose_flows(interval)
    emissions = (
        calculate_direct_emissions(
            flows,
            DirectEmissionFactors(
                grid_intensity=EmissionFactor(sample.value_g_co2e_per_kwh),
                pv_lifecycle=plan.pv_lifecycle,
            ),
        )
        if sample is not None and grid_error is None
        else None
    )
    consumer_emissions = (
        {consumer.consumer_id: consumer.direct for consumer in emissions.consumers}
        if emissions is not None
        else {}
    )
    consumers = dict(state.consumer_totals)
    for consumer in flows.consumers:
        consumers[consumer.consumer_id] = _add_direct_totals(
            consumers[consumer.consumer_id],
            consumer.direct_pv,
            consumer_emissions.get(consumer.consumer_id),
        )
    diagnostics = dict(state.diagnostics)
    if grid_error is not None:
        diagnostics["missing_grid_intensity"] = (
            diagnostics.get("missing_grid_intensity", 0) + 1
        )
    return replace(
        state,
        totals=_add_direct_totals(
            state.totals,
            flows.direct_pv,
            emissions.direct if emissions is not None else None,
        ),
        consumer_totals=tuple(sorted(consumers.items())),
        unassigned_direct_kwh=state.unassigned_direct_kwh
        + flows.direct_pv_unassigned.kwh,
        diagnostics=tuple(sorted(diagnostics.items())),
    )


def evaluate_observations(
    state: GenerationState,
    observations: tuple[EnergyObservation, ...],
    observed_at: datetime,
    *,
    plan: DirectEvaluationPlan,
    current_grid_sample: GridIntensitySample | None,
) -> EvaluationOutcome:
    """Propose at most one complete generation revision from this poll's copies."""
    _validate_state_plan(state, plan)
    transition = advance_measurements(
        state.measurement,
        observations,
        observed_at,
        assemble_interval=plan.assemble_interval,
    )
    effective_at = (
        transition.interval.window.end
        if transition.interval is not None
        else observed_at
    )
    grid_error = _grid_error(current_grid_sample, effective_at, plan)
    updated = replace(state, measurement=transition.state)
    if transition.interruption_started:
        diagnostics = dict(updated.diagnostics)
        diagnostics["discarded_intervals"] = (
            diagnostics.get("discarded_intervals", 0) + 1
        )
        updated = replace(updated, diagnostics=tuple(sorted(diagnostics.items())))
    if transition.interval is not None:
        updated = _apply_interval(
            updated,
            transition.interval,
            plan,
            current_grid_sample,
            grid_error=grid_error,
        )
    updated = (
        state
        if updated == state
        else replace(updated, commit_revision=state.commit_revision + 1)
    )
    return EvaluationOutcome(
        state=updated,
        measurement_fault=transition.fault,
        grid_error=grid_error,
        interval_processed=transition.interval is not None,
    )


__all__ = ("DirectEvaluationPlan", "EvaluationOutcome", "evaluate_observations")
