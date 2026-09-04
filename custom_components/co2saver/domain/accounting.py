# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Pure conservative energy-flow and direct-emissions accounting."""

from __future__ import annotations

from fractions import Fraction

from .errors import DomainInvariantError, DomainValidationError, IntervalRejectionReason
from .models import (
    ConsumerEmissionView,
    ConsumerFlow,
    DirectEmissionFactors,
    EmissionBreakdown,
    EnergySink,
    EnergySource,
    FlowDecomposition,
    GuaranteedFlow,
    InputTopology,
    IntervalEmissionResult,
    InverterIntervalInput,
    MarginRemainder,
    NormalizedInterval,
    RejectedInterval,
    SmartMeterIntervalInput,
)
from .quantities import Emissions, Energy

type IntervalInput = InverterIntervalInput | SmartMeterIntervalInput

_ABSOLUTE_TOLERANCE_KWH = Fraction(1, 100)
_RELATIVE_TOLERANCE = Fraction(1, 50)


def _tolerance(*values: Fraction) -> Fraction:
    """Return the exact ADR absolute-or-two-percent tolerance."""
    return max(_ABSOLUTE_TOLERANCE_KWH, _RELATIVE_TOLERANCE * max(values))


def normalize_interval(value: IntervalInput) -> NormalizedInterval | RejectedInterval:
    """Resolve topology-specific PV and conservatively balance one interval."""
    local_load = value.consumers.total
    charge = value.battery_charge
    discharge = value.battery_discharge
    if charge.kwh > 0 and discharge.kwh > 0:
        return RejectedInterval(IntervalRejectionReason.SIMULTANEOUS_CHARGE_DISCHARGE)

    if isinstance(value, InverterIntervalInput):
        topology = InputTopology.INVERTER
        pv = value.pv_generation
    else:
        topology = InputTopology.SMART_METER
        raw_pv = (
            local_load.kwh
            + charge.kwh
            + value.grid_export.kwh
            - value.grid_import.kwh
            - discharge.kwh
        )
        raw_tolerance = _tolerance(
            local_load.kwh,
            charge.kwh,
            value.grid_export.kwh,
            value.grid_import.kwh,
            discharge.kwh,
        )
        if raw_pv < -raw_tolerance:
            return RejectedInterval(IntervalRejectionReason.SMART_METER_NEGATIVE_PV)
        pv = Energy(max(raw_pv, Fraction()))
        if value.pv_plausibility is not None:
            plausibility_tolerance = _tolerance(pv.kwh, value.pv_plausibility.kwh)
            if abs(pv.kwh - value.pv_plausibility.kwh) > plausibility_tolerance:
                return RejectedInterval(
                    IntervalRejectionReason.PV_PLAUSIBILITY_MISMATCH
                )

    source_total = pv.kwh + value.grid_import.kwh + discharge.kwh
    sink_total = local_load.kwh + charge.kwh + value.grid_export.kwh
    balance_tolerance = _tolerance(
        pv.kwh,
        value.grid_import.kwh,
        discharge.kwh,
        local_load.kwh,
        charge.kwh,
        value.grid_export.kwh,
    )
    if abs(source_total - sink_total) > balance_tolerance:
        return RejectedInterval(IntervalRejectionReason.SITE_IMBALANCE)

    unknown_source = Energy(max(sink_total - source_total, Fraction()))
    unknown_sink = Energy(max(source_total - sink_total, Fraction()))
    total = Energy(max(source_total, sink_total))
    return NormalizedInterval(
        topology=topology,
        window=value.window,
        consumers=value.consumers,
        pv=pv,
        grid_import=value.grid_import,
        battery_discharge=discharge,
        local_load=local_load,
        battery_charge=charge,
        grid_export=value.grid_export,
        unknown_source=unknown_source,
        unknown_sink=unknown_sink,
        total=total,
    )


def transport_lower_bound(source: Energy, sink: Energy, total: Energy) -> Energy:
    """Return the Fréchet lower bound for one transport cell."""
    if source.kwh > total.kwh or sink.kwh > total.kwh:
        msg = "transport margins must not exceed total energy"
        raise DomainValidationError(msg)
    return Energy(max(Fraction(), source.kwh + sink.kwh - total.kwh))


def _source_margins(interval: NormalizedInterval) -> dict[EnergySource, Energy]:
    """Return all augmented source margins in stable enum order."""
    return {
        EnergySource.PV: interval.pv,
        EnergySource.GRID: interval.grid_import,
        EnergySource.BATTERY: interval.battery_discharge,
        EnergySource.UNKNOWN: interval.unknown_source,
    }


def _sink_margins(interval: NormalizedInterval) -> dict[EnergySink, Energy]:
    """Return all augmented sink margins in stable enum order."""
    return {
        EnergySink.LOCAL_LOAD: interval.local_load,
        EnergySink.BATTERY_CHARGE: interval.battery_charge,
        EnergySink.GRID_EXPORT: interval.grid_export,
        EnergySink.UNKNOWN: interval.unknown_sink,
    }


def decompose_flows(interval: NormalizedInterval) -> FlowDecomposition:
    """Build the complete guaranteed matrix and conservative consumer views."""
    sources = _source_margins(interval)
    sinks = _sink_margins(interval)
    guaranteed = tuple(
        GuaranteedFlow(
            source,
            sink,
            transport_lower_bound(source_energy, sink_energy, interval.total),
        )
        for source, source_energy in sources.items()
        for sink, sink_energy in sinks.items()
    )

    def cell(source: EnergySource, sink: EnergySink) -> Fraction:
        return next(
            flow.energy.kwh
            for flow in guaranteed
            if flow.source is source and flow.sink is sink
        )

    source_remainders = tuple(
        MarginRemainder(
            source,
            Energy(
                source_energy.kwh
                - sum((cell(source, sink) for sink in EnergySink), Fraction())
            ),
        )
        for source, source_energy in sources.items()
    )
    sink_remainders = tuple(
        MarginRemainder(
            sink,
            Energy(
                sink_energy.kwh
                - sum((cell(source, sink) for source in EnergySource), Fraction())
            ),
        )
        for sink, sink_energy in sinks.items()
    )
    guaranteed_total = sum((flow.energy.kwh for flow in guaranteed), Fraction())
    ambiguity = Energy(interval.total.kwh - guaranteed_total)

    consumer_flows = tuple(
        ConsumerFlow(
            consumer_id=load.consumer_id,
            load=load.energy,
            direct_pv=transport_lower_bound(interval.pv, load.energy, interval.total),
            battery_to_local=transport_lower_bound(
                interval.battery_discharge, load.energy, interval.total
            ),
        )
        for load in interval.consumers.loads
    )
    direct_pv = cell(EnergySource.PV, EnergySink.LOCAL_LOAD)
    battery_local = cell(EnergySource.BATTERY, EnergySink.LOCAL_LOAD)
    allocated_direct = sum((flow.direct_pv.kwh for flow in consumer_flows), Fraction())
    allocated_battery = sum(
        (flow.battery_to_local.kwh for flow in consumer_flows), Fraction()
    )
    if allocated_direct > direct_pv or allocated_battery > battery_local:
        msg = "consumer lower bounds must not exceed their aggregate lower bounds"
        raise DomainInvariantError(msg)

    return FlowDecomposition(
        interval=interval,
        guaranteed_flows=guaranteed,
        source_remainders=source_remainders,
        sink_remainders=sink_remainders,
        ambiguity=ambiguity,
        consumers=consumer_flows,
        direct_pv_unassigned=Energy(direct_pv - allocated_direct),
        battery_local_unassigned=Energy(battery_local - allocated_battery),
    )


def _direct_breakdown(
    energy: Energy,
    factors: DirectEmissionFactors,
) -> EmissionBreakdown:
    """Calculate direct-PV emissions on their declared factor bases."""
    gross = factors.grid_intensity.apply(energy)
    pv_lifecycle = factors.pv_lifecycle.apply(energy)
    battery_lifecycle = Emissions(Fraction())
    return EmissionBreakdown(
        credited_energy=energy,
        gross_avoided=gross,
        pv_lifecycle=pv_lifecycle,
        battery_lifecycle=battery_lifecycle,
    )


def calculate_direct_emissions(
    flows: FlowDecomposition,
    factors: DirectEmissionFactors,
) -> IntervalEmissionResult:
    """Calculate only direct-PV results; storage remains a separate transition."""
    return IntervalEmissionResult(
        direct=_direct_breakdown(flows.direct_pv, factors),
        consumers=tuple(
            ConsumerEmissionView(
                consumer_id=consumer.consumer_id,
                direct=_direct_breakdown(consumer.direct_pv, factors),
            )
            for consumer in flows.consumers
        ),
        direct_unassigned=_direct_breakdown(flows.direct_pv_unassigned, factors),
    )
