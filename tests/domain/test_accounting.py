# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Contract tests for conservative interval accounting."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from itertools import product
from typing import TYPE_CHECKING

import pytest

from custom_components.co2saver.domain.accounting import (
    calculate_direct_emissions,
    decompose_flows,
    normalize_interval,
    transport_lower_bound,
)
from custom_components.co2saver.domain.errors import (
    DomainInvariantError,
    DomainValidationError,
    IntervalRejectionReason,
)
from custom_components.co2saver.domain.models import (
    ConsumerFlow,
    ConsumerLoad,
    ConsumerLoads,
    ConsumerShare,
    ConsumptionMode,
    DirectEmissionFactors,
    EmissionBreakdown,
    EnergySink,
    EnergySource,
    FlowDecomposition,
    IntervalWindow,
    InverterIntervalInput,
    NormalizedInterval,
    RejectedInterval,
    SmartMeterIntervalInput,
    loads_from_meters,
    loads_from_shares,
)
from custom_components.co2saver.domain.quantities import (
    EmissionFactor,
    Emissions,
    Energy,
    Ratio,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


def _energy(value: int | Fraction = 0) -> Energy:
    """Construct exact test energy in kilowatt-hours."""
    return Energy(Fraction(value))


def _window(seconds: int = 300) -> IntervalWindow:
    """Construct one deterministic UTC measurement window."""
    start = datetime(2026, 9, 4, 12, tzinfo=UTC)
    return IntervalWindow(start=start, end=start + timedelta(seconds=seconds))


def _house_only(load: int | Fraction) -> ConsumerLoads:
    """Construct a separately metered house without additional consumers."""
    return loads_from_meters(ConsumerLoad("house", _energy(load)), ())


def _accepted(
    value: InverterIntervalInput | SmartMeterIntervalInput,
) -> NormalizedInterval:
    """Narrow a normalization result expected to be accepted."""
    result = normalize_interval(value)
    assert isinstance(result, NormalizedInterval)
    return result


def _rejected(
    value: InverterIntervalInput | SmartMeterIntervalInput,
) -> RejectedInterval:
    """Narrow a normalization result expected to be rejected."""
    result = normalize_interval(value)
    assert isinstance(result, RejectedInterval)
    return result


def _factors() -> DirectEmissionFactors:
    """Return the factors used by ADR examples 9.1 and 9.2."""
    return DirectEmissionFactors(
        grid_intensity=EmissionFactor(Fraction(400)),
        pv_lifecycle=EmissionFactor(Fraction(40)),
    )


def _flow_by_consumer(
    decomposition: FlowDecomposition,
    consumer_id: str,
) -> ConsumerFlow:
    """Return one consumer flow by its stable identifier."""
    return next(
        flow for flow in decomposition.consumers if flow.consumer_id == consumer_id
    )


def test_interval_window_accepts_utc_900_second_boundary() -> None:
    """The ADR interval ceiling is inclusive."""
    assert _window(900).end - _window(900).start == timedelta(seconds=900)


@pytest.mark.parametrize(
    ("start", "end"),
    [
        pytest.param(
            datetime(2026, 9, 4, 12, tzinfo=UTC).replace(tzinfo=None),
            datetime(2026, 9, 4, 12, 1, tzinfo=UTC).replace(tzinfo=None),
            id="naive",
        ),
        pytest.param(
            datetime(2026, 9, 4, 12, tzinfo=UTC),
            datetime(2026, 9, 4, 12, tzinfo=UTC),
            id="zero-duration",
        ),
        pytest.param(
            datetime(2026, 9, 4, 12, tzinfo=UTC),
            datetime(2026, 9, 4, 12, tzinfo=UTC) + timedelta(seconds=901),
            id="over-duration",
        ),
    ],
)
def test_interval_window_rejects_invalid_bounds(
    start: datetime,
    end: datetime,
) -> None:
    """Naive, empty, and overlong physical intervals fail closed."""
    with pytest.raises(DomainValidationError):
        IntervalWindow(start=start, end=end)


def test_aggregate_shares_partition_load_exactly() -> None:
    """Aggregate shares leave the exact complementary house remainder."""
    loads = loads_from_shares(
        _energy(4),
        "house",
        (ConsumerShare("wallbox", Ratio(Fraction(1, 4))),),
    )

    assert loads.total == _energy(4)
    assert loads.loads == (
        ConsumerLoad("house", _energy(3)),
        ConsumerLoad("wallbox", _energy(1)),
    )


def test_separate_meters_sum_loads_exactly() -> None:
    """Separate non-overlapping meters form one exact local-load margin."""
    loads = loads_from_meters(
        ConsumerLoad("house", _energy(2)),
        (ConsumerLoad("wallbox", _energy(1)),),
    )

    assert loads.total == _energy(3)


def test_consumer_models_reject_empty_identity_and_partition() -> None:
    """Every load has a stable owner and every partition has a house bucket."""
    with pytest.raises(DomainValidationError):
        ConsumerLoad(" ", _energy())
    with pytest.raises(DomainValidationError):
        ConsumerLoads(ConsumptionMode.SEPARATE_METERS, ())


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(
            lambda: loads_from_shares(
                _energy(1),
                "house",
                (ConsumerShare("house", Ratio(Fraction(1, 2))),),
            ),
            id="aggregate-duplicate-id",
        ),
        pytest.param(
            lambda: loads_from_shares(
                _energy(1),
                "house",
                (
                    ConsumerShare("first", Ratio(Fraction(3, 5))),
                    ConsumerShare("second", Ratio(Fraction(3, 5))),
                ),
            ),
            id="aggregate-share-overflow",
        ),
        pytest.param(
            lambda: loads_from_meters(
                ConsumerLoad("house", _energy()),
                (ConsumerLoad("house", _energy()),),
            ),
            id="meter-duplicate-id",
        ),
    ],
)
def test_consumer_partitions_reject_ambiguous_ownership(
    build: Callable[[], ConsumerLoads],
) -> None:
    """Duplicate owners and aggregate overflow cannot enter the domain."""
    with pytest.raises(DomainValidationError):
        build()


def test_adr_9_1_inverter_direct_pv_and_emissions() -> None:
    """ADR 9.1 credits four kWh and keeps the exported kWh uncredited."""
    consumers = loads_from_shares(
        _energy(4),
        "house",
        (ConsumerShare("wallbox", Ratio(Fraction(1, 4))),),
    )
    interval = _accepted(
        InverterIntervalInput(
            window=_window(),
            consumers=consumers,
            pv_generation=_energy(5),
            grid_import=_energy(),
            grid_export=_energy(1),
            battery_charge=_energy(),
            battery_discharge=_energy(),
        )
    )
    flows = decompose_flows(interval)
    emissions = calculate_direct_emissions(flows, _factors())

    assert len(flows.guaranteed_flows) == 16
    assert flows.direct_pv == _energy(4)
    assert flows.pv_to_export == _energy(1)
    assert flows.direct_pv_unassigned == _energy()
    assert _flow_by_consumer(flows, "house").direct_pv == _energy(3)
    assert _flow_by_consumer(flows, "wallbox").direct_pv == _energy(1)
    assert emissions.direct.gross_avoided == Emissions(Fraction(1_600))
    assert emissions.direct.pv_lifecycle == Emissions(Fraction(160))
    assert emissions.direct.battery_lifecycle == Emissions.zero()
    assert emissions.direct.net_saving == Emissions(Fraction(1_440))
    assert emissions.direct_unassigned.credited_energy == _energy()
    assert emissions.direct_unassigned.net_saving == Emissions.zero()
    assert tuple(view.direct.net_saving.grams for view in emissions.consumers) == (
        Fraction(1_080),
        Fraction(360),
    )


@pytest.mark.parametrize("aggregate_mode", [False, True])
def test_adr_9_2_smart_meter_uses_individual_consumer_bounds(
    *,
    aggregate_mode: bool,
) -> None:
    """ADR 9.2 never proportionally invents PV ownership for the wallbox."""
    consumers = (
        loads_from_shares(
            _energy(3),
            "house",
            (ConsumerShare("wallbox", Ratio(Fraction(1, 3))),),
        )
        if aggregate_mode
        else loads_from_meters(
            ConsumerLoad("house", _energy(2)),
            (ConsumerLoad("wallbox", _energy(1)),),
        )
    )
    interval = _accepted(
        SmartMeterIntervalInput(
            window=_window(),
            consumers=consumers,
            grid_import=_energy(1),
            grid_export=_energy(2),
            battery_charge=_energy(),
            battery_discharge=_energy(),
        )
    )
    flows = decompose_flows(interval)
    emissions = calculate_direct_emissions(flows, _factors())

    assert interval.pv == _energy(4)
    assert interval.total == _energy(5)
    assert flows.direct_pv == _energy(2)
    assert flows.pv_to_export == _energy(1)
    assert _flow_by_consumer(flows, "house").direct_pv == _energy(1)
    assert _flow_by_consumer(flows, "wallbox").direct_pv == _energy()
    assert flows.direct_pv_unassigned == _energy(1)
    assert emissions.direct.net_saving == Emissions(Fraction(720))
    assert emissions.direct_unassigned.credited_energy == _energy(1)
    assert emissions.direct_unassigned.gross_avoided == Emissions(Fraction(400))
    assert emissions.direct_unassigned.pv_lifecycle == Emissions(Fraction(40))
    assert emissions.direct_unassigned.net_saving == Emissions(Fraction(360))
    assert tuple(view.direct.net_saving.grams for view in emissions.consumers) == (
        Fraction(360),
        Fraction(),
    )


def test_adr_9_5_ambiguous_smart_meter_charge_has_no_guaranteed_pv_path() -> None:
    """ADR 9.5 derives PV but credits neither load nor battery charge."""
    interval = _accepted(
        SmartMeterIntervalInput(
            window=_window(),
            consumers=_house_only(1),
            grid_import=_energy(1),
            grid_export=_energy(),
            battery_charge=_energy(1),
            battery_discharge=_energy(),
        )
    )
    flows = decompose_flows(interval)

    assert interval.pv == _energy(1)
    assert interval.total == _energy(2)
    assert flows.direct_pv == _energy()
    assert flows.pv_to_charge == _energy()
    assert flows.grid_to_charge == _energy()


def test_positive_smart_meter_charge_is_guaranteed_when_it_is_the_only_sink() -> None:
    """The ADR positive charge vector derives and assigns two PV kWh."""
    interval = _accepted(
        SmartMeterIntervalInput(
            window=_window(),
            consumers=_house_only(0),
            grid_import=_energy(),
            grid_export=_energy(),
            battery_charge=_energy(2),
            battery_discharge=_energy(),
        )
    )

    assert interval.pv == _energy(2)
    assert decompose_flows(interval).pv_to_charge == _energy(2)


def test_tolerated_balance_rest_is_augmented_and_reduces_direct_credit() -> None:
    """ADR tolerated imbalance becomes unknown source, never invented PV."""
    interval = _accepted(
        InverterIntervalInput(
            window=_window(),
            consumers=_house_only(1),
            pv_generation=_energy(1),
            grid_import=_energy(),
            grid_export=_energy(Fraction(1, 200)),
            battery_charge=_energy(),
            battery_discharge=_energy(),
        )
    )
    flows = decompose_flows(interval)

    assert interval.unknown_source == _energy(Fraction(1, 200))
    assert interval.unknown_sink == _energy()
    assert interval.total == _energy(Fraction(201, 200))
    assert flows.direct_pv == _energy(Fraction(199, 200))


def test_balance_rest_above_tolerance_rejects_the_interval() -> None:
    """An imbalance just beyond two percent fails closed."""
    rejected = _rejected(
        InverterIntervalInput(
            window=_window(),
            consumers=_house_only(1),
            pv_generation=_energy(1),
            grid_import=_energy(),
            grid_export=_energy(Fraction(21, 1_000)),
            battery_charge=_energy(),
            battery_discharge=_energy(),
        )
    )

    assert rejected.reason is IntervalRejectionReason.SITE_IMBALANCE


def test_tolerated_negative_smart_meter_pv_clamps_to_unknown_sink() -> None:
    """A small negative raw PV value cannot create or erase credited energy."""
    interval = _accepted(
        SmartMeterIntervalInput(
            window=_window(),
            consumers=_house_only(1),
            grid_import=_energy(Fraction(201, 200)),
            grid_export=_energy(),
            battery_charge=_energy(),
            battery_discharge=_energy(),
        )
    )

    assert interval.pv == _energy()
    assert interval.unknown_source == _energy()
    assert interval.unknown_sink == _energy(Fraction(1, 200))
    assert decompose_flows(interval).direct_pv == _energy()


@pytest.mark.parametrize(
    ("grid_export", "battery_charge", "expected_export", "expected_charge"),
    [
        pytest.param(1, 0, 1, 0, id="export"),
        pytest.param(0, 1, 0, 1, id="charge"),
    ],
)
def test_export_and_battery_charge_never_count_as_direct_pv(
    grid_export: int,
    battery_charge: int,
    expected_export: int,
    expected_charge: int,
) -> None:
    """Only the local-load sink can produce direct-PV emissions."""
    interval = _accepted(
        InverterIntervalInput(
            window=_window(),
            consumers=_house_only(0),
            pv_generation=_energy(1),
            grid_import=_energy(),
            grid_export=_energy(grid_export),
            battery_charge=_energy(battery_charge),
            battery_discharge=_energy(),
        )
    )
    flows = decompose_flows(interval)

    assert flows.direct_pv == _energy()
    assert flows.pv_to_export == _energy(expected_export)
    assert flows.pv_to_charge == _energy(expected_charge)
    assert calculate_direct_emissions(flows, _factors()).direct.gross_avoided == (
        Emissions.zero()
    )


def test_positive_simultaneous_charge_and_discharge_rejects_before_attribution() -> (
    None
):
    """No tolerance may conceal ambiguous simultaneous storage directions."""
    rejected = _rejected(
        InverterIntervalInput(
            window=_window(),
            consumers=_house_only(1),
            pv_generation=_energy(1),
            grid_import=_energy(),
            grid_export=_energy(),
            battery_charge=_energy(Fraction(1, 1_000)),
            battery_discharge=_energy(Fraction(1, 1_000)),
        )
    )

    assert rejected.reason is IntervalRejectionReason.SIMULTANEOUS_CHARGE_DISCHARGE


def test_smart_meter_rejects_material_negative_pv_and_plausibility_mismatch() -> None:
    """Derived PV and its optional check obey their separate tolerances."""
    negative = _rejected(
        SmartMeterIntervalInput(
            window=_window(),
            consumers=_house_only(1),
            grid_import=_energy(2),
            grid_export=_energy(),
            battery_charge=_energy(),
            battery_discharge=_energy(),
        )
    )
    mismatch = _rejected(
        SmartMeterIntervalInput(
            window=_window(),
            consumers=_house_only(1),
            grid_import=_energy(),
            grid_export=_energy(),
            battery_charge=_energy(),
            battery_discharge=_energy(),
            pv_plausibility=_energy(2),
        )
    )

    assert negative.reason is IntervalRejectionReason.SMART_METER_NEGATIVE_PV
    assert mismatch.reason is IntervalRejectionReason.PV_PLAUSIBILITY_MISMATCH


def test_matching_smart_meter_plausibility_value_remains_non_authoritative() -> None:
    """An accepted plausibility meter checks but does not replace derived PV."""
    interval = _accepted(
        SmartMeterIntervalInput(
            window=_window(),
            consumers=_house_only(1),
            grid_import=_energy(),
            grid_export=_energy(),
            battery_charge=_energy(),
            battery_discharge=_energy(),
            pv_plausibility=_energy(Fraction(101, 100)),
        )
    )

    assert interval.pv == _energy(1)


def test_transport_lower_bound_rejects_margins_above_total() -> None:
    """The public transport primitive fails on structurally invalid margins."""
    with pytest.raises(DomainValidationError):
        transport_lower_bound(_energy(2), _energy(), _energy(1))
    with pytest.raises(DomainValidationError):
        transport_lower_bound(_energy(), _energy(2), _energy(1))


def test_normalized_interval_rejects_inconsistent_manual_construction() -> None:
    """Public normalized models cannot bypass conservation invariants."""
    interval = _accepted(
        InverterIntervalInput(
            window=_window(),
            consumers=_house_only(1),
            pv_generation=_energy(1),
            grid_import=_energy(),
            grid_export=_energy(),
            battery_charge=_energy(),
            battery_discharge=_energy(),
        )
    )

    with pytest.raises(DomainInvariantError):
        replace(interval, local_load=_energy(2))
    with pytest.raises(DomainInvariantError):
        replace(interval, total=_energy(2))
    with pytest.raises(DomainInvariantError):
        replace(
            interval,
            unknown_source=_energy(Fraction(1, 10)),
            unknown_sink=_energy(Fraction(1, 10)),
            total=_energy(Fraction(11, 10)),
        )
    with pytest.raises(DomainInvariantError):
        replace(
            interval,
            battery_charge=_energy(Fraction(1, 10)),
            battery_discharge=_energy(Fraction(1, 10)),
            total=_energy(Fraction(11, 10)),
        )
    with pytest.raises(DomainInvariantError):
        replace(
            interval,
            pv=_energy(100),
            unknown_sink=_energy(99),
            total=_energy(100),
        )


def _two_consumer_decomposition() -> FlowDecomposition:
    """Return ADR 9.2 as a non-trivial valid decomposition fixture."""
    consumers = loads_from_meters(
        ConsumerLoad("house", _energy(2)),
        (ConsumerLoad("wallbox", _energy(1)),),
    )
    interval = _accepted(
        SmartMeterIntervalInput(
            window=_window(),
            consumers=consumers,
            grid_import=_energy(1),
            grid_export=_energy(2),
            battery_charge=_energy(),
            battery_discharge=_energy(),
        )
    )
    return decompose_flows(interval)


def test_missing_matrix_cell_is_an_internal_invariant_failure() -> None:
    """A malformed decomposition cannot silently return zero for a missing cell."""
    interval = _accepted(
        InverterIntervalInput(
            window=_window(),
            consumers=_house_only(1),
            pv_generation=_energy(1),
            grid_import=_energy(),
            grid_export=_energy(),
            battery_charge=_energy(),
            battery_discharge=_energy(),
        )
    )
    with pytest.raises(DomainInvariantError):
        replace(decompose_flows(interval), guaranteed_flows=())


def test_flow_decomposition_rejects_duplicate_or_inexact_matrix_cells() -> None:
    """A 16-item tuple is valid only when every unique cell carries its exact LB."""
    flows = _two_consumer_decomposition()
    duplicate_cells = (
        flows.guaranteed_flows[1],
        *flows.guaranteed_flows[1:],
    )
    first_cell = flows.guaranteed_flows[0]
    inexact_cells = (
        replace(first_cell, energy=Energy(first_cell.energy.kwh + 1)),
        *flows.guaranteed_flows[1:],
    )

    with pytest.raises(DomainInvariantError):
        replace(flows, guaranteed_flows=duplicate_cells)
    with pytest.raises(DomainInvariantError):
        replace(flows, guaranteed_flows=inexact_cells)


def test_flow_decomposition_rejects_inexact_remainders_and_ambiguity() -> None:
    """Source, sink, and total ambiguity remainders are constructive proofs."""
    flows = _two_consumer_decomposition()
    first_source = flows.source_remainders[0]
    first_sink = flows.sink_remainders[0]
    wrong_sources = (
        replace(first_source, energy=Energy(first_source.energy.kwh + 1)),
        *flows.source_remainders[1:],
    )
    wrong_sinks = (
        replace(first_sink, energy=Energy(first_sink.energy.kwh + 1)),
        *flows.sink_remainders[1:],
    )
    duplicate_sources = (
        replace(first_source, role=flows.source_remainders[1].role),
        *flows.source_remainders[1:],
    )
    duplicate_sinks = (
        replace(first_sink, role=flows.sink_remainders[1].role),
        *flows.sink_remainders[1:],
    )

    with pytest.raises(DomainInvariantError):
        replace(flows, source_remainders=())
    with pytest.raises(DomainInvariantError):
        replace(flows, source_remainders=wrong_sources)
    with pytest.raises(DomainInvariantError):
        replace(flows, source_remainders=duplicate_sources)
    with pytest.raises(DomainInvariantError):
        replace(flows, sink_remainders=())
    with pytest.raises(DomainInvariantError):
        replace(flows, sink_remainders=wrong_sinks)
    with pytest.raises(DomainInvariantError):
        replace(flows, sink_remainders=duplicate_sinks)
    with pytest.raises(DomainInvariantError):
        replace(flows, ambiguity=Energy(flows.ambiguity.kwh + 1))


def test_flow_decomposition_rejects_forged_consumer_proofs() -> None:
    """Consumer identity, load, and both lower bounds must match the interval."""
    flows = _two_consumer_decomposition()
    first, second = flows.consumers
    forged_first_values = (
        replace(first, consumer_id="forged"),
        replace(first, consumer_id=second.consumer_id),
        replace(first, load=_energy(3)),
        replace(first, direct_pv=_energy(2)),
        replace(first, battery_to_local=_energy(1)),
    )

    for forged_first in forged_first_values:
        with pytest.raises(DomainInvariantError):
            replace(flows, consumers=(forged_first, second))


def test_flow_decomposition_rejects_forged_unassigned_remainders() -> None:
    """Unassigned energies must close the consumer proofs to each system flow."""
    flows = _two_consumer_decomposition()

    with pytest.raises(DomainInvariantError):
        replace(flows, direct_pv_unassigned=_energy())
    with pytest.raises(DomainInvariantError):
        replace(flows, battery_local_unassigned=_energy(1))


@pytest.mark.parametrize("negative_component", ["gross", "pv", "battery"])
def test_emission_breakdown_derives_signed_net_and_rejects_negative_components(
    negative_component: str,
) -> None:
    """Net savings cannot drift from their authoritative components."""
    breakdown = EmissionBreakdown(
        credited_energy=_energy(1),
        gross_avoided=Emissions(Fraction(10)),
        pv_lifecycle=Emissions(Fraction(12)),
        battery_lifecycle=Emissions(Fraction(3)),
    )

    assert breakdown.net_saving == Emissions(Fraction(-5))
    gross = Fraction(-1) if negative_component == "gross" else Fraction()
    pv = Fraction(-1) if negative_component == "pv" else Fraction()
    battery = Fraction(-1) if negative_component == "battery" else Fraction()
    with pytest.raises(DomainValidationError):
        EmissionBreakdown(
            credited_energy=_energy(1),
            gross_avoided=Emissions(gross),
            pv_lifecycle=Emissions(pv),
            battery_lifecycle=Emissions(battery),
        )


def test_interval_emissions_reject_nonclosing_energy_or_components() -> None:
    """Consumer and unassigned direct buckets must exactly close to the system."""
    result = calculate_direct_emissions(_two_consumer_decomposition(), _factors())
    missing_energy = replace(
        result.direct_unassigned,
        credited_energy=_energy(),
    )
    wrong_component = replace(
        result.direct_unassigned,
        pv_lifecycle=Emissions(
            result.direct_unassigned.pv_lifecycle.grams + Fraction(1)
        ),
    )

    with pytest.raises(DomainInvariantError):
        replace(result, direct_unassigned=missing_energy)
    with pytest.raises(DomainInvariantError):
        replace(result, direct_unassigned=wrong_component)


def test_interval_emissions_reject_nonuniform_factors_even_when_sums_close() -> None:
    """Offsetting component forgeries cannot disguise different bucket factors."""
    result = calculate_direct_emissions(_two_consumer_decomposition(), _factors())
    house, wallbox = result.consumers
    forged_house = replace(
        house,
        direct=replace(
            house.direct,
            gross_avoided=Emissions(house.direct.gross_avoided.grams + 1),
        ),
    )
    forged_unassigned = replace(
        result.direct_unassigned,
        gross_avoided=Emissions(result.direct_unassigned.gross_avoided.grams - 1),
    )

    with pytest.raises(DomainInvariantError):
        replace(
            result,
            consumers=(forged_house, wallbox),
            direct_unassigned=forged_unassigned,
        )


def test_interval_emissions_reject_duplicate_consumer_ids() -> None:
    """Emission views retain unambiguous consumer ownership."""
    result = calculate_direct_emissions(_two_consumer_decomposition(), _factors())
    house, wallbox = result.consumers
    duplicate = replace(wallbox, consumer_id=house.consumer_id)

    with pytest.raises(DomainInvariantError):
        replace(result, consumers=(house, duplicate))


def test_interval_emissions_reject_components_without_direct_energy() -> None:
    """A zero-energy direct result cannot carry any emissions component."""
    interval = _accepted(
        InverterIntervalInput(
            window=_window(),
            consumers=_house_only(0),
            pv_generation=_energy(1),
            grid_import=_energy(),
            grid_export=_energy(1),
            battery_charge=_energy(),
            battery_discharge=_energy(),
        )
    )
    result = calculate_direct_emissions(decompose_flows(interval), _factors())
    forged_system = replace(result.direct, gross_avoided=Emissions(Fraction(1)))
    forged_unassigned = replace(
        result.direct_unassigned,
        gross_avoided=Emissions(Fraction(1)),
    )

    with pytest.raises(DomainInvariantError):
        replace(
            result,
            direct=forged_system,
            direct_unassigned=forged_unassigned,
        )


def _balanced_grid_vectors() -> Iterator[tuple[int, int, int, int, int, int]]:
    """Yield a small exact grid of balanced inverter vectors."""
    for pv, imported, discharged, load, charge in product(range(3), repeat=5):
        if charge > 0 and discharged > 0:
            continue
        exported = pv + imported + discharged - load - charge
        if exported >= 0:
            yield pv, imported, discharged, load, charge, exported


def test_exhaustive_balanced_grid_preserves_matrix_and_consumer_invariants() -> None:
    """All exact small vectors conserve matrix and consumer energy."""
    for pv, imported, discharged, load, charge, exported in _balanced_grid_vectors():
        first_load = Fraction(load, 2)
        consumers = loads_from_meters(
            ConsumerLoad("house", _energy(first_load)),
            (ConsumerLoad("other", _energy(load - first_load)),),
        )
        interval = _accepted(
            InverterIntervalInput(
                window=_window(),
                consumers=consumers,
                pv_generation=_energy(pv),
                grid_import=_energy(imported),
                grid_export=_energy(exported),
                battery_charge=_energy(charge),
                battery_discharge=_energy(discharged),
            )
        )
        flows = decompose_flows(interval)
        source_margins = {
            EnergySource.PV: interval.pv,
            EnergySource.GRID: interval.grid_import,
            EnergySource.BATTERY: interval.battery_discharge,
            EnergySource.UNKNOWN: interval.unknown_source,
        }
        sink_margins = {
            EnergySink.LOCAL_LOAD: interval.local_load,
            EnergySink.BATTERY_CHARGE: interval.battery_charge,
            EnergySink.GRID_EXPORT: interval.grid_export,
            EnergySink.UNKNOWN: interval.unknown_sink,
        }

        assert len(flows.guaranteed_flows) == 16
        assert all(flow.energy.kwh >= 0 for flow in flows.guaranteed_flows)
        for source, margin in source_margins.items():
            row = sum(
                (
                    flow.energy.kwh
                    for flow in flows.guaranteed_flows
                    if flow.source is source
                ),
                Fraction(),
            )
            remainder = next(
                item.energy.kwh
                for item in flows.source_remainders
                if item.role is source
            )
            assert row + remainder == margin.kwh
            assert remainder >= 0
        for sink, margin in sink_margins.items():
            column = sum(
                (
                    flow.energy.kwh
                    for flow in flows.guaranteed_flows
                    if flow.sink is sink
                ),
                Fraction(),
            )
            remainder = next(
                item.energy.kwh for item in flows.sink_remainders if item.role is sink
            )
            assert column + remainder == margin.kwh
            assert remainder >= 0

        assert (
            sum((item.energy.kwh for item in flows.source_remainders), Fraction())
            == flows.ambiguity.kwh
        )
        assert (
            sum((item.energy.kwh for item in flows.sink_remainders), Fraction())
            == flows.ambiguity.kwh
        )
        assert (
            sum((flow.direct_pv.kwh for flow in flows.consumers), Fraction())
            + flows.direct_pv_unassigned.kwh
            == flows.direct_pv.kwh
        )
        assert (
            sum(
                (flow.battery_to_local.kwh for flow in flows.consumers),
                Fraction(),
            )
            + flows.battery_local_unassigned.kwh
            == flows.battery_to_local.kwh
        )
        assert calculate_direct_emissions(flows, _factors()).direct.credited_energy == (
            flows.direct_pv
        )
