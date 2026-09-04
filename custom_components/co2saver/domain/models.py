# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Immutable, Home Assistant-independent accounting domain models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from fractions import Fraction

from .errors import DomainInvariantError, DomainValidationError, IntervalRejectionReason
from .quantities import EmissionFactor, Emissions, Energy, Ratio

_MAX_INTERVAL_DURATION = timedelta(seconds=900)
_ABSOLUTE_BALANCE_TOLERANCE_KWH = Fraction(1, 100)
_RELATIVE_BALANCE_TOLERANCE = Fraction(1, 50)


class InputTopology(StrEnum):
    """Supported ways to obtain photovoltaic generation."""

    INVERTER = "inverter"
    SMART_METER = "smart_meter"


class ConsumptionMode(StrEnum):
    """Supported ways to describe individual local loads."""

    AGGREGATE_SHARES = "aggregate_shares"
    SEPARATE_METERS = "separate_meters"


class EnergySource(StrEnum):
    """Source margins of the augmented site-energy transport."""

    PV = "pv"
    GRID = "grid"
    BATTERY = "battery"
    UNKNOWN = "unknown"


class EnergySink(StrEnum):
    """Sink margins of the augmented site-energy transport."""

    LOCAL_LOAD = "local_load"
    BATTERY_CHARGE = "battery_charge"
    GRID_EXPORT = "grid_export"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class IntervalWindow:
    """A common, already synchronized physical measurement interval in UTC."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        """Validate ordering, timezone, and the ADR duration ceiling."""
        if (
            self.start.tzinfo is None
            or self.end.tzinfo is None
            or self.start.utcoffset() != timedelta(0)
            or self.end.utcoffset() != timedelta(0)
        ):
            msg = "interval timestamps must be timezone-aware UTC values"
            raise DomainValidationError(msg)
        duration = self.end - self.start
        if duration <= timedelta(0) or duration > _MAX_INTERVAL_DURATION:
            msg = "interval duration must be greater than zero and at most 900 seconds"
            raise DomainValidationError(msg)


def _validate_consumer_id(consumer_id: str) -> None:
    """Reject identifiers that cannot provide stable consumer ownership."""
    if not consumer_id or not consumer_id.strip():
        msg = "consumer_id must not be empty"
        raise DomainValidationError(msg)


@dataclass(frozen=True, slots=True)
class ConsumerShare:
    """Configured share of an aggregate local-load meter."""

    consumer_id: str
    share: Ratio

    def __post_init__(self) -> None:
        """Validate the stable identifier and share range."""
        _validate_consumer_id(self.consumer_id)
        if not 0 <= self.share.value <= 1:
            msg = "consumer share must be between zero and one"
            raise DomainValidationError(msg)


@dataclass(frozen=True, slots=True)
class ConsumerLoad:
    """Normalized local energy owned by one consumer."""

    consumer_id: str
    energy: Energy

    def __post_init__(self) -> None:
        """Validate the stable identifier."""
        _validate_consumer_id(self.consumer_id)


@dataclass(frozen=True, slots=True)
class ConsumerLoads:
    """A complete, non-overlapping partition of local-load energy."""

    mode: ConsumptionMode
    loads: tuple[ConsumerLoad, ...]

    def __post_init__(self) -> None:
        """Make the collection immutable and require unique ownership."""
        object.__setattr__(self, "loads", tuple(self.loads))
        if not self.loads:
            msg = "at least the house consumer is required"
            raise DomainValidationError(msg)
        ids = [load.consumer_id for load in self.loads]
        if len(ids) != len(set(ids)):
            msg = "consumer ids must be unique"
            raise DomainValidationError(msg)

    @property
    def total(self) -> Energy:
        """Return the exact local-load sum in kWh."""
        return Energy(sum((load.energy.kwh for load in self.loads), Fraction()))


def loads_from_shares(
    total: Energy,
    house_id: str,
    shares: tuple[ConsumerShare, ...],
) -> ConsumerLoads:
    """Split one aggregate meter by exact configured shares."""
    _validate_consumer_id(house_id)
    normalized_shares = tuple(shares)
    ids = [house_id, *(share.consumer_id for share in normalized_shares)]
    if len(ids) != len(set(ids)):
        msg = "consumer ids must be unique"
        raise DomainValidationError(msg)
    share_sum = sum((share.share.value for share in normalized_shares), Fraction())
    if share_sum > 1:
        msg = "additional consumer shares must not exceed one"
        raise DomainValidationError(msg)
    loads = (
        ConsumerLoad(house_id, Energy(total.kwh * (1 - share_sum))),
        *(
            ConsumerLoad(share.consumer_id, Energy(total.kwh * share.share.value))
            for share in normalized_shares
        ),
    )
    result = ConsumerLoads(ConsumptionMode.AGGREGATE_SHARES, loads)
    if result.total != total:
        msg = "aggregate consumer allocation must conserve local energy"
        raise DomainInvariantError(msg)
    return result


def loads_from_meters(
    house: ConsumerLoad,
    additional: tuple[ConsumerLoad, ...],
) -> ConsumerLoads:
    """Combine non-overlapping, separately metered local loads."""
    return ConsumerLoads(ConsumptionMode.SEPARATE_METERS, (house, *tuple(additional)))


@dataclass(frozen=True, slots=True)
class InverterIntervalInput:
    """Interval deltas with authoritative inverter PV generation."""

    window: IntervalWindow
    consumers: ConsumerLoads
    pv_generation: Energy
    grid_import: Energy
    grid_export: Energy
    battery_charge: Energy
    battery_discharge: Energy


@dataclass(frozen=True, slots=True)
class SmartMeterIntervalInput:
    """Interval deltas from which PV generation is derived by conservation."""

    window: IntervalWindow
    consumers: ConsumerLoads
    grid_import: Energy
    grid_export: Energy
    battery_charge: Energy
    battery_discharge: Energy
    pv_plausibility: Energy | None = None


def _balance_tolerance(*values: Energy) -> Fraction:
    """Return the exact ADR absolute-or-two-percent balance tolerance."""
    return max(
        _ABSOLUTE_BALANCE_TOLERANCE_KWH,
        _RELATIVE_BALANCE_TOLERANCE * max(value.kwh for value in values),
    )


@dataclass(frozen=True, slots=True)
class NormalizedInterval:
    """An exactly balanced interval in the common kWh domain."""

    topology: InputTopology
    window: IntervalWindow
    consumers: ConsumerLoads
    pv: Energy
    grid_import: Energy
    battery_discharge: Energy
    local_load: Energy
    battery_charge: Energy
    grid_export: Energy
    unknown_source: Energy
    unknown_sink: Energy
    total: Energy

    def __post_init__(self) -> None:
        """Guard the constructive augmented-balance invariants."""
        if self.local_load != self.consumers.total:
            msg = "normalized local load must equal the consumer-load sum"
            raise DomainInvariantError(msg)
        if self.battery_charge.kwh > 0 and self.battery_discharge.kwh > 0:
            msg = "normalized interval must not charge and discharge simultaneously"
            raise DomainInvariantError(msg)
        raw_source_total = (
            self.pv.kwh + self.grid_import.kwh + self.battery_discharge.kwh
        )
        raw_sink_total = (
            self.local_load.kwh + self.battery_charge.kwh + self.grid_export.kwh
        )
        tolerance = _balance_tolerance(
            self.pv,
            self.grid_import,
            self.battery_discharge,
            self.local_load,
            self.battery_charge,
            self.grid_export,
        )
        if abs(raw_source_total - raw_sink_total) > tolerance:
            msg = "normalized raw balance difference must remain within tolerance"
            raise DomainInvariantError(msg)
        expected_unknown_source = max(raw_sink_total - raw_source_total, Fraction())
        expected_unknown_sink = max(raw_source_total - raw_sink_total, Fraction())
        expected_total = max(raw_source_total, raw_sink_total)
        if (
            self.unknown_source.kwh != expected_unknown_source
            or self.unknown_sink.kwh != expected_unknown_sink
        ):
            msg = "unknown margins must exactly augment the smaller raw balance side"
            raise DomainInvariantError(msg)
        if self.total.kwh != expected_total:
            msg = "normalized total must equal the larger raw balance side"
            raise DomainInvariantError(msg)


@dataclass(frozen=True, slots=True)
class GuaranteedFlow:
    """A guaranteed lower-bound transport cell with explicit provenance."""

    source: EnergySource
    sink: EnergySink
    energy: Energy


@dataclass(frozen=True, slots=True)
class MarginRemainder:
    """Unresolved energy remaining on one source or sink margin."""

    role: EnergySource | EnergySink
    energy: Energy


@dataclass(frozen=True, slots=True)
class ConsumerFlow:
    """Guaranteed transport lower bounds for one local consumer."""

    consumer_id: str
    load: Energy
    direct_pv: Energy
    battery_to_local: Energy


@dataclass(frozen=True, slots=True)
class FlowDecomposition:
    """Complete system and consumer transport lower-bound decomposition."""

    interval: NormalizedInterval
    guaranteed_flows: tuple[GuaranteedFlow, ...]
    source_remainders: tuple[MarginRemainder, ...]
    sink_remainders: tuple[MarginRemainder, ...]
    ambiguity: Energy
    consumers: tuple[ConsumerFlow, ...]
    direct_pv_unassigned: Energy
    battery_local_unassigned: Energy

    def __post_init__(self) -> None:
        """Validate the complete lower-bound proof carried by this result."""
        object.__setattr__(self, "guaranteed_flows", tuple(self.guaranteed_flows))
        object.__setattr__(self, "source_remainders", tuple(self.source_remainders))
        object.__setattr__(self, "sink_remainders", tuple(self.sink_remainders))
        object.__setattr__(self, "consumers", tuple(self.consumers))
        _validate_flow_decomposition(self)

    def flow(self, source: EnergySource, sink: EnergySink) -> Energy:
        """Return one cell from the complete deterministic 4-by-4 matrix."""
        for flow in self.guaranteed_flows:
            if flow.source is source and flow.sink is sink:
                return flow.energy
        msg = f"missing guaranteed-flow cell {source.value}->{sink.value}"
        raise DomainInvariantError(msg)

    @property
    def direct_pv(self) -> Energy:
        """Return guaranteed direct PV use by all local loads."""
        return self.flow(EnergySource.PV, EnergySink.LOCAL_LOAD)

    @property
    def pv_to_charge(self) -> Energy:
        """Return guaranteed PV energy entering battery charging."""
        return self.flow(EnergySource.PV, EnergySink.BATTERY_CHARGE)

    @property
    def pv_to_export(self) -> Energy:
        """Return guaranteed PV export without granting it a benefit."""
        return self.flow(EnergySource.PV, EnergySink.GRID_EXPORT)

    @property
    def grid_to_charge(self) -> Energy:
        """Return guaranteed grid energy entering battery charging."""
        return self.flow(EnergySource.GRID, EnergySink.BATTERY_CHARGE)

    @property
    def battery_to_local(self) -> Energy:
        """Return guaranteed battery discharge serving all local loads."""
        return self.flow(EnergySource.BATTERY, EnergySink.LOCAL_LOAD)


def _source_margins(interval: NormalizedInterval) -> dict[EnergySource, Energy]:
    """Return the four constructive source margins."""
    return {
        EnergySource.PV: interval.pv,
        EnergySource.GRID: interval.grid_import,
        EnergySource.BATTERY: interval.battery_discharge,
        EnergySource.UNKNOWN: interval.unknown_source,
    }


def _sink_margins(interval: NormalizedInterval) -> dict[EnergySink, Energy]:
    """Return the four constructive sink margins."""
    return {
        EnergySink.LOCAL_LOAD: interval.local_load,
        EnergySink.BATTERY_CHARGE: interval.battery_charge,
        EnergySink.GRID_EXPORT: interval.grid_export,
        EnergySink.UNKNOWN: interval.unknown_sink,
    }


def _lower_bound(source: Energy, sink: Energy, total: Energy) -> Energy:
    """Return the exact lower bound expected in a validated result."""
    return Energy(max(Fraction(), source.kwh + sink.kwh - total.kwh))


def _validated_flow_cells(
    decomposition: FlowDecomposition,
) -> dict[tuple[EnergySource, EnergySink], Energy]:
    """Validate and index the complete unique 4-by-4 matrix."""
    if len(decomposition.guaranteed_flows) != len(EnergySource) * len(EnergySink):
        msg = "flow decomposition must contain exactly 16 cells"
        raise DomainInvariantError(msg)
    cells: dict[tuple[EnergySource, EnergySink], Energy] = {}
    sources = _source_margins(decomposition.interval)
    sinks = _sink_margins(decomposition.interval)
    for flow in decomposition.guaranteed_flows:
        if (
            not isinstance(flow, GuaranteedFlow)
            or type(flow.source) is not EnergySource
            or type(flow.sink) is not EnergySink
        ):
            msg = "every flow cell must have one declared source and sink role"
            raise DomainInvariantError(msg)
        key = (flow.source, flow.sink)
        if key in cells:
            msg = "flow decomposition cells must be unique"
            raise DomainInvariantError(msg)
        expected = _lower_bound(
            sources[flow.source], sinks[flow.sink], decomposition.interval.total
        )
        if flow.energy != expected:
            msg = "every flow cell must equal its exact transport lower bound"
            raise DomainInvariantError(msg)
        cells[key] = flow.energy
    return cells


def _validate_source_remainders(
    decomposition: FlowDecomposition,
    cells: dict[tuple[EnergySource, EnergySink], Energy],
) -> None:
    """Require one exact unresolved remainder per source margin."""
    if len(decomposition.source_remainders) != len(EnergySource):
        msg = "flow decomposition must contain four source remainders"
        raise DomainInvariantError(msg)
    remainders: dict[EnergySource, Energy] = {}
    for remainder in decomposition.source_remainders:
        if (
            not isinstance(remainder, MarginRemainder)
            or type(remainder.role) is not EnergySource
            or remainder.role in remainders
        ):
            msg = "source remainder roles must be complete and unique"
            raise DomainInvariantError(msg)
        remainders[remainder.role] = remainder.energy
    for source, margin in _source_margins(decomposition.interval).items():
        expected = Energy(
            margin.kwh
            - sum((cells[source, sink].kwh for sink in EnergySink), Fraction())
        )
        if remainders[source] != expected:
            msg = "every source remainder must exactly close its margin"
            raise DomainInvariantError(msg)


def _validate_sink_remainders(
    decomposition: FlowDecomposition,
    cells: dict[tuple[EnergySource, EnergySink], Energy],
) -> None:
    """Require one exact unresolved remainder per sink margin."""
    if len(decomposition.sink_remainders) != len(EnergySink):
        msg = "flow decomposition must contain four sink remainders"
        raise DomainInvariantError(msg)
    remainders: dict[EnergySink, Energy] = {}
    for remainder in decomposition.sink_remainders:
        if (
            not isinstance(remainder, MarginRemainder)
            or type(remainder.role) is not EnergySink
            or remainder.role in remainders
        ):
            msg = "sink remainder roles must be complete and unique"
            raise DomainInvariantError(msg)
        remainders[remainder.role] = remainder.energy
    for sink, margin in _sink_margins(decomposition.interval).items():
        expected = Energy(
            margin.kwh
            - sum((cells[source, sink].kwh for source in EnergySource), Fraction())
        )
        if remainders[sink] != expected:
            msg = "every sink remainder must exactly close its margin"
            raise DomainInvariantError(msg)


def _validate_consumer_flows(
    decomposition: FlowDecomposition,
    cells: dict[tuple[EnergySource, EnergySink], Energy],
) -> None:
    """Require exact per-load bounds and exact unassigned system remainders."""
    consumers: dict[str, ConsumerFlow] = {}
    for consumer in decomposition.consumers:
        if not isinstance(consumer, ConsumerFlow) or consumer.consumer_id in consumers:
            msg = "consumer flows must have unique declared consumer ids"
            raise DomainInvariantError(msg)
        consumers[consumer.consumer_id] = consumer
    expected_loads = {
        load.consumer_id: load.energy for load in decomposition.interval.consumers.loads
    }
    if consumers.keys() != expected_loads.keys():
        msg = "consumer flow ids must exactly match normalized consumer ids"
        raise DomainInvariantError(msg)
    for consumer_id, load in expected_loads.items():
        consumer = consumers[consumer_id]
        if consumer.load != load:
            msg = "consumer flow load must equal its normalized load"
            raise DomainInvariantError(msg)
        direct = _lower_bound(
            decomposition.interval.pv, load, decomposition.interval.total
        )
        battery = _lower_bound(
            decomposition.interval.battery_discharge,
            load,
            decomposition.interval.total,
        )
        if consumer.direct_pv != direct or consumer.battery_to_local != battery:
            msg = "consumer flows must equal their exact transport lower bounds"
            raise DomainInvariantError(msg)
    allocated_direct = sum(
        (consumer.direct_pv.kwh for consumer in consumers.values()), Fraction()
    )
    allocated_battery = sum(
        (consumer.battery_to_local.kwh for consumer in consumers.values()), Fraction()
    )
    expected_direct_unassigned = Energy(
        cells[EnergySource.PV, EnergySink.LOCAL_LOAD].kwh - allocated_direct
    )
    expected_battery_unassigned = Energy(
        cells[EnergySource.BATTERY, EnergySink.LOCAL_LOAD].kwh - allocated_battery
    )
    if decomposition.direct_pv_unassigned != expected_direct_unassigned:
        msg = "direct-PV unassigned remainder must exactly close the system flow"
        raise DomainInvariantError(msg)
    if decomposition.battery_local_unassigned != expected_battery_unassigned:
        msg = "battery-local unassigned remainder must exactly close the system flow"
        raise DomainInvariantError(msg)


def _validate_flow_decomposition(decomposition: FlowDecomposition) -> None:
    """Validate all constructive matrix, margin, and consumer identities."""
    cells = _validated_flow_cells(decomposition)
    _validate_source_remainders(decomposition, cells)
    _validate_sink_remainders(decomposition, cells)
    guaranteed_total = sum((energy.kwh for energy in cells.values()), Fraction())
    expected_ambiguity = Energy(decomposition.interval.total.kwh - guaranteed_total)
    if decomposition.ambiguity != expected_ambiguity:
        msg = "ambiguity must equal total energy minus all guaranteed cells"
        raise DomainInvariantError(msg)
    _validate_consumer_flows(decomposition, cells)


@dataclass(frozen=True, slots=True)
class RejectedInterval:
    """A measured interval rejected before any accounting mutation."""

    reason: IntervalRejectionReason


@dataclass(frozen=True, slots=True)
class EmissionBreakdown:
    """Exact emissions for one credited-energy path."""

    credited_energy: Energy
    gross_avoided: Emissions
    pv_lifecycle: Emissions
    battery_lifecycle: Emissions

    def __post_init__(self) -> None:
        """Require non-negative physical component amounts."""
        if (
            self.gross_avoided.grams < 0
            or self.pv_lifecycle.grams < 0
            or self.battery_lifecycle.grams < 0
        ):
            msg = "emission components must not be negative"
            raise DomainValidationError(msg)

    @property
    def net_saving(self) -> Emissions:
        """Derive the signed net saving from its authoritative components."""
        return Emissions(
            self.gross_avoided.grams
            - self.pv_lifecycle.grams
            - self.battery_lifecycle.grams
        )


@dataclass(frozen=True, slots=True)
class ConsumerEmissionView:
    """Conservative direct-emission view for one consumer."""

    consumer_id: str
    direct: EmissionBreakdown

    def __post_init__(self) -> None:
        """Require stable consumer ownership for the view."""
        _validate_consumer_id(self.consumer_id)


@dataclass(frozen=True, slots=True)
class IntervalEmissionResult:
    """Authoritative direct system result and its consumer views."""

    direct: EmissionBreakdown
    consumers: tuple[ConsumerEmissionView, ...]
    direct_unassigned: EmissionBreakdown

    def __post_init__(self) -> None:
        """Require additive energy and uniform-factor component closure."""
        object.__setattr__(self, "consumers", tuple(self.consumers))
        consumer_ids = [consumer.consumer_id for consumer in self.consumers]
        if len(consumer_ids) != len(set(consumer_ids)):
            msg = "consumer emission ids must be unique"
            raise DomainInvariantError(msg)
        parts = (
            *(consumer.direct for consumer in self.consumers),
            self.direct_unassigned,
        )
        if sum((part.credited_energy.kwh for part in parts), Fraction()) != (
            self.direct.credited_energy.kwh
        ):
            msg = "consumer and unassigned direct energy must close to the system"
            raise DomainInvariantError(msg)
        _validate_emission_component_closure(self.direct, parts)


@dataclass(frozen=True, slots=True)
class DirectEmissionFactors:
    """Factors used exclusively for direct-PV emission accounting."""

    grid_intensity: EmissionFactor
    pv_lifecycle: EmissionFactor


def _validate_emission_component_closure(
    system: EmissionBreakdown,
    parts: tuple[EmissionBreakdown, ...],
) -> None:
    """Require exact additive components produced by one pair of factors."""
    component_names = ("gross_avoided", "pv_lifecycle", "battery_lifecycle")
    system_energy = system.credited_energy.kwh
    for component_name in component_names:
        system_component = getattr(system, component_name)
        part_components = tuple(getattr(part, component_name) for part in parts)
        if sum((component.grams for component in part_components), Fraction()) != (
            system_component.grams
        ):
            msg = "consumer and unassigned emission components must close to the system"
            raise DomainInvariantError(msg)
        if system_energy == 0:
            if system_component.grams != 0:
                msg = "zero direct energy must have zero emission components"
                raise DomainInvariantError(msg)
            continue
        if any(
            component.grams * system_energy
            != system_component.grams * part.credited_energy.kwh
            for component, part in zip(part_components, parts, strict=True)
        ):
            msg = "direct emission parts must use the same factors as the system"
            raise DomainInvariantError(msg)
