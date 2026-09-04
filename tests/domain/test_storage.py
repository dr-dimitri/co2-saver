# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Contract tests for the conservative storage-provenance ledger."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from typing import TYPE_CHECKING, cast

import pytest

from custom_components.co2saver.domain.accounting import decompose_flows
from custom_components.co2saver.domain.errors import (
    DomainInvariantError,
    DomainValidationError,
    StorageRejectionReason,
)
from custom_components.co2saver.domain.models import (
    ConsumerFlow,
    ConsumerLoad,
    ConsumerLoads,
    ConsumptionMode,
    EnergySink,
    EnergySource,
    FlowDecomposition,
    InputTopology,
    IntervalWindow,
    NormalizedInterval,
)
from custom_components.co2saver.domain.quantities import (
    EmissionDensity,
    EmissionFactor,
    Emissions,
    Energy,
    ExactInput,
    Ratio,
)
from custom_components.co2saver.domain.storage import (
    ConsumerStorageCredit,
    StorageEffects,
    StorageLedger,
    StorageRejected,
    StorageTransition,
    advance_storage,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_ZERO = Fraction()


def _energy(value: ExactInput = 0) -> Energy:
    """Construct exact test energy in kilowatt-hours."""
    return Energy.from_kwh(value)


def _factor(value: ExactInput = 40) -> EmissionFactor:
    """Construct an exact PV lifecycle factor."""
    return EmissionFactor.from_g_per_kwh(value)


def _efficiency(value: ExactInput = "0.9") -> Ratio:
    """Construct an exact storage efficiency."""
    return Ratio.from_value(value)


def _ledger(  # noqa: PLR0913 - mirrors the six persisted ledger fields
    *,
    capacity: ExactInput,
    stored_lower: ExactInput,
    stored_upper: ExactInput,
    pv_lower: ExactInput,
    pv_burden: ExactInput,
    pv_density_upper: ExactInput,
) -> StorageLedger:
    """Construct one ledger from readable exact values."""
    return StorageLedger(
        capacity=_energy(capacity),
        stored_lower=_energy(stored_lower),
        stored_upper=_energy(stored_upper),
        pv_lower=_energy(pv_lower),
        pv_burden=Emissions.from_grams(pv_burden),
        pv_density_upper=EmissionDensity.from_g_per_kwh(pv_density_upper),
    )


def _empty_ledger(capacity: ExactInput = 10) -> StorageLedger:
    """Construct a storage ledger whose empty state is proven."""
    return _ledger(
        capacity=capacity,
        stored_lower=0,
        stored_upper=0,
        pv_lower=0,
        pv_burden=0,
        pv_density_upper=0,
    )


def _flows(
    *,
    charge: ExactInput = 0,
    pv_to_charge: ExactInput = 0,
    discharge: ExactInput = 0,
    battery_to_local: ExactInput = 0,
) -> FlowDecomposition:
    """Build a complete validated decomposition for one storage movement."""
    charge_energy = _energy(charge)
    pv_charge_energy = _energy(pv_to_charge)
    discharge_energy = _energy(discharge)
    battery_local_energy = _energy(battery_to_local)
    assert pv_charge_energy.kwh <= charge_energy.kwh
    assert battery_local_energy.kwh <= discharge_energy.kwh

    consumer_loads = (ConsumerLoad("house", battery_local_energy),)
    consumers = ConsumerLoads(ConsumptionMode.SEPARATE_METERS, consumer_loads)

    pv_energy = pv_charge_energy
    grid_energy = Energy(charge_energy.kwh - pv_charge_energy.kwh)
    export_energy = Energy(discharge_energy.kwh - battery_local_energy.kwh)
    total = Energy(charge_energy.kwh + discharge_energy.kwh)
    start = datetime(2026, 9, 4, 12, tzinfo=UTC)
    interval = NormalizedInterval(
        topology=InputTopology.INVERTER,
        window=IntervalWindow(start=start, end=start + timedelta(minutes=5)),
        consumers=consumers,
        pv=pv_energy,
        grid_import=grid_energy,
        battery_discharge=discharge_energy,
        local_load=battery_local_energy,
        battery_charge=charge_energy,
        grid_export=export_energy,
        unknown_source=Energy.zero(),
        unknown_sink=Energy.zero(),
        total=total,
    )
    result = decompose_flows(interval)
    assert result.pv_to_charge == pv_charge_energy
    assert result.battery_to_local == battery_local_energy
    assert len(result.guaranteed_flows) == 16
    assert len(result.source_remainders) == 4
    assert len(result.sink_remainders) == 4
    return result


def _consumer_remainder_flows() -> FlowDecomposition:
    """Build exact system and consumer bounds with a nonzero assignment rest."""
    start = datetime(2026, 9, 4, 12, tzinfo=UTC)
    consumers = ConsumerLoads(
        ConsumptionMode.SEPARATE_METERS,
        (
            ConsumerLoad("house", _energy(2)),
            ConsumerLoad("wallbox", _energy("0.5")),
        ),
    )
    interval = NormalizedInterval(
        topology=InputTopology.INVERTER,
        window=IntervalWindow(start=start, end=start + timedelta(minutes=5)),
        consumers=consumers,
        pv=Energy.zero(),
        grid_import=_energy("0.5"),
        battery_discharge=_energy(2),
        local_load=_energy("2.5"),
        battery_charge=Energy.zero(),
        grid_export=Energy.zero(),
        unknown_source=Energy.zero(),
        unknown_sink=Energy.zero(),
        total=_energy("2.5"),
    )
    result = decompose_flows(interval)
    assert result.battery_to_local == _energy(2)
    assert tuple(flow.battery_to_local for flow in result.consumers) == (
        _energy("1.5"),
        Energy.zero(),
    )
    assert result.battery_local_unassigned == _energy("0.5")
    return result


def _tamper_system_flow(
    flows: FlowDecomposition,
    source: EnergySource,
    sink: EnergySink,
    energy: ExactInput,
) -> FlowDecomposition:
    """Corrupt one cell only after a valid decomposition was constructed."""
    cell = next(
        flow
        for flow in flows.guaranteed_flows
        if flow.source is source and flow.sink is sink
    )
    object.__setattr__(cell, "energy", _energy(energy))
    return flows


def _tamper_consumers(
    flows: FlowDecomposition,
    consumers: tuple[ConsumerFlow, ...],
) -> FlowDecomposition:
    """Corrupt consumer bounds only after validating the base decomposition."""
    object.__setattr__(flows, "consumers", consumers)
    return flows


def _tamper_simultaneous_movement(flows: FlowDecomposition) -> FlowDecomposition:
    """Add discharge only after constructing a valid charge decomposition."""
    object.__setattr__(flows.interval, "battery_discharge", _energy(1))
    return flows


def _accepted(
    result: StorageTransition | StorageRejected,
) -> StorageTransition:
    """Narrow a storage result expected to be accepted."""
    assert isinstance(result, StorageTransition)
    return result


def _rejected(
    result: StorageTransition | StorageRejected,
) -> StorageRejected:
    """Narrow a storage result expected to be rejected."""
    assert isinstance(result, StorageRejected)
    return result


def _advance(
    ledger: StorageLedger,
    flows: FlowDecomposition,
    *,
    efficiency: Ratio | None = None,
    factor: EmissionFactor | None = None,
) -> StorageTransition | StorageRejected:
    """Advance with the ADR example defaults unless explicitly overridden."""
    return advance_storage(
        ledger,
        flows,
        efficiency or _efficiency(),
        factor or _factor(),
    )


def _assert_invariants(ledger: StorageLedger) -> None:
    """Assert the complete persistent invariant set from ADR section 7."""
    assert ledger.stored_lower.kwh >= _ZERO
    assert ledger.stored_lower.kwh <= ledger.stored_upper.kwh
    assert ledger.stored_upper.kwh <= ledger.capacity.kwh
    assert _ZERO <= ledger.pv_lower.kwh <= ledger.stored_lower.kwh
    assert ledger.non_pv_upper.kwh == (ledger.stored_upper.kwh - ledger.pv_lower.kwh)
    assert ledger.pv_burden.grams >= _ZERO
    assert ledger.pv_burden.grams <= (
        ledger.pv_density_upper.grams_per_kwh * ledger.pv_lower.kwh
    )
    if ledger.pv_lower.kwh == _ZERO:
        assert ledger.pv_burden == Emissions.zero()
        assert ledger.pv_density_upper == EmissionDensity.from_g_per_kwh(0)


def _mixed_charge_ledger() -> StorageLedger:
    """Return the exact mixed PV/grid ledger from ADR section 9.4."""
    transition = _accepted(
        _advance(
            _empty_ledger(4),
            _flows(charge=4, pv_to_charge=3),
        )
    )
    return transition.after


def test_quarantined_ledger_represents_completely_unknown_content() -> None:
    """Initialization proves no content while retaining the capacity bound."""
    ledger = StorageLedger.quarantined(_energy(10))

    assert ledger == _ledger(
        capacity=10,
        stored_lower=0,
        stored_upper=10,
        pv_lower=0,
        pv_burden=0,
        pv_density_upper=0,
    )
    assert ledger.non_pv_upper == _energy(10)
    _assert_invariants(ledger)


@pytest.mark.parametrize(
    "capacity",
    [pytest.param("0.1", id="minimum"), pytest.param(1000, id="maximum")],
)
def test_capacity_boundaries_are_inclusive(capacity: ExactInput) -> None:
    """Both documented usable-capacity boundaries construct valid ledgers."""
    ledger = StorageLedger.quarantined(_energy(capacity))

    assert ledger.capacity == _energy(capacity)
    _assert_invariants(ledger)


def test_storage_ledger_is_immutable() -> None:
    """A valid ledger cannot be mutated after construction."""
    ledger = _empty_ledger()

    with pytest.raises(FrozenInstanceError):
        ledger.stored_lower = _energy(1)  # type: ignore[misc]


@pytest.mark.parametrize(
    "values",
    [
        pytest.param(("0.09", 0, 0, 0, 0, 0), id="capacity-below-minimum"),
        pytest.param((1001, 0, 0, 0, 0, 0), id="capacity-above-maximum"),
        pytest.param((10, 2, 1, 0, 0, 0), id="lower-above-upper"),
        pytest.param((10, 0, 11, 0, 0, 0), id="upper-above-capacity"),
        pytest.param((10, 1, 1, 2, 0, 0), id="pv-above-lower"),
        pytest.param((10, 1, 1, 1, -1, 1), id="negative-burden"),
        pytest.param((10, 1, 1, 1, 2, 1), id="burden-above-envelope"),
        pytest.param((10, 0, 0, 0, 1, 1), id="zero-pv-with-burden"),
        pytest.param((10, 0, 0, 0, 0, 1), id="zero-pv-with-density"),
    ],
)
def test_constructor_rejects_every_invalid_ledger_invariant(
    values: tuple[ExactInput, ...],
) -> None:
    """Invalid capacity, bounds, and burden envelopes fail closed."""
    capacity, stored_lower, stored_upper, pv_lower, burden, density = values
    with pytest.raises(DomainValidationError):
        _ledger(
            capacity=capacity,
            stored_lower=stored_lower,
            stored_upper=stored_upper,
            pv_lower=pv_lower,
            pv_burden=burden,
            pv_density_upper=density,
        )


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(
            lambda: StorageLedger(
                capacity=cast("Energy", object()),
                stored_lower=_energy(),
                stored_upper=_energy(),
                pv_lower=_energy(),
                pv_burden=Emissions.zero(),
                pv_density_upper=EmissionDensity.from_g_per_kwh(0),
            ),
            id="capacity",
        ),
        pytest.param(
            lambda: replace(
                _empty_ledger(),
                pv_burden=cast("Emissions", object()),
            ),
            id="burden",
        ),
        pytest.param(
            lambda: replace(
                _empty_ledger(),
                pv_density_upper=cast("EmissionDensity", object()),
            ),
            id="density",
        ),
    ],
)
def test_constructor_rejects_wrong_quantity_dimensions(
    build: Callable[[], StorageLedger],
) -> None:
    """Runtime callers cannot smuggle another physical dimension into state."""
    with pytest.raises(DomainValidationError):
        build()


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(
            lambda: ConsumerStorageCredit("", Energy.zero(), Emissions.zero()),
            id="empty-id",
        ),
        pytest.param(
            lambda: ConsumerStorageCredit(
                "house",
                cast("Energy", object()),
                Emissions.zero(),
            ),
            id="energy-type",
        ),
        pytest.param(
            lambda: ConsumerStorageCredit(
                "house",
                Energy.zero(),
                cast("Emissions", object()),
            ),
            id="burden-type",
        ),
        pytest.param(
            lambda: ConsumerStorageCredit(
                "house",
                _energy(1),
                Emissions.from_grams(-1),
            ),
            id="negative-burden",
        ),
        pytest.param(
            lambda: ConsumerStorageCredit(
                "house",
                Energy.zero(),
                Emissions.from_grams(1),
            ),
            id="zero-energy-burden",
        ),
    ],
)
def test_consumer_storage_credit_rejects_inconsistent_results(
    build: Callable[[], ConsumerStorageCredit],
) -> None:
    """A consumer result always has an owner and a physical burden basis."""
    with pytest.raises(DomainInvariantError):
        build()


@pytest.mark.parametrize(
    "field",
    [
        "stored_charge",
        "pv_stored_charge",
        "pv_discharged",
        "pv_used_locally",
        "unassigned_local_pv",
        "pv_burden_used",
        "pv_burden_discarded",
    ],
)
def test_storage_effects_reject_wrong_quantity_types(field: str) -> None:
    """Every effect retains its declared physical dimension at runtime."""
    effects = _accepted(_advance(_empty_ledger(), _flows())).effects

    with pytest.raises(DomainInvariantError):
        replace(effects, **{field: object()})  # type: ignore[arg-type]


def test_storage_effects_freeze_and_validate_consumer_collection() -> None:
    """Consumer effects become an immutable, nonempty, unique tuple."""
    effects = _accepted(_advance(_empty_ledger(), _flows())).effects
    as_list = list(effects.consumers)
    normalized = replace(
        effects,
        consumers=cast("tuple[ConsumerStorageCredit, ...]", as_list),
    )
    duplicate = (effects.consumers[0], effects.consumers[0])

    assert isinstance(normalized.consumers, tuple)
    with pytest.raises(DomainInvariantError):
        replace(effects, consumers=())
    with pytest.raises(DomainInvariantError):
        replace(effects, consumers=duplicate)
    with pytest.raises(DomainInvariantError):
        replace(
            effects,
            consumers=cast("tuple[ConsumerStorageCredit, ...]", (object(),)),
        )


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(
            lambda effects: replace(effects, pv_stored_charge=_energy(2)),
            id="pv-charge-above-charge",
        ),
        pytest.param(
            lambda effects: replace(effects, pv_used_locally=_energy(1)),
            id="local-above-discharge",
        ),
        pytest.param(
            lambda effects: replace(
                effects,
                pv_discharged=_energy(1),
                pv_used_locally=_energy(1),
            ),
            id="consumer-energy-does-not-close",
        ),
        pytest.param(
            lambda effects: replace(
                effects,
                pv_burden_used=Emissions.from_grams(-1),
            ),
            id="negative-used-burden",
        ),
        pytest.param(
            lambda effects: replace(
                effects,
                pv_burden_discarded=Emissions.from_grams(-1),
            ),
            id="negative-discarded-burden",
        ),
    ],
)
def test_storage_effects_reject_nonconstructive_balances(
    build: Callable[[StorageEffects], StorageEffects],
) -> None:
    """Energy and burden effect summaries fail closed when inconsistent."""
    effects = _accepted(_advance(_empty_ledger(), _flows(charge=1))).effects

    with pytest.raises(DomainInvariantError):
        build(effects)


def test_storage_effects_reject_burden_without_local_pv_use() -> None:
    """A used burden requires a positive system-level local PV effect."""
    effects = _accepted(_advance(_empty_ledger(), _flows())).effects

    with pytest.raises(DomainInvariantError):
        replace(effects, pv_burden_used=Emissions.from_grams(1))


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(
            lambda effects: replace(effects, pv_discharged=_energy(1)),
            id="discharge",
        ),
        pytest.param(
            lambda effects: replace(
                effects,
                pv_burden_used=Emissions.from_grams(1),
            ),
            id="used-burden",
        ),
        pytest.param(
            lambda effects: replace(
                effects,
                pv_burden_discarded=Emissions.from_grams(1),
            ),
            id="discarded-burden",
        ),
        pytest.param(
            lambda effects: replace(
                effects,
                pv_discharged=_energy(1),
                pv_used_locally=_energy(1),
                consumers=(
                    ConsumerStorageCredit("house", _energy(1), Emissions.zero()),
                ),
            ),
            id="consumer-credit",
        ),
        pytest.param(
            lambda effects: replace(
                effects,
                pv_discharged=_energy(1),
                pv_used_locally=_energy(1),
                unassigned_local_pv=_energy(1),
            ),
            id="unassigned-credit",
        ),
    ],
)
def test_charge_effects_reject_every_discharge_side_effect(
    build: Callable[[StorageEffects], StorageEffects],
) -> None:
    """Charging cannot simultaneously report any discharge-side result."""
    effects = _accepted(
        _advance(_empty_ledger(), _flows(charge=1, pv_to_charge=1))
    ).effects

    with pytest.raises(DomainInvariantError):
        build(effects)


@pytest.mark.parametrize("field", ["before", "after", "effects"])
def test_storage_transition_rejects_wrong_result_types(field: str) -> None:
    """A transition cannot hide a non-ledger or non-effect result value."""
    transition = _accepted(_advance(_empty_ledger(), _flows()))

    with pytest.raises(DomainInvariantError):
        replace(transition, **{field: object()})  # type: ignore[arg-type]


def test_storage_transition_rejects_capacity_change() -> None:
    """An accepted transition cannot silently replace storage capacity."""
    transition = _accepted(_advance(_empty_ledger(4), _flows()))

    with pytest.raises(DomainInvariantError):
        replace(transition, after=_empty_ledger(5))


@pytest.mark.parametrize(
    "after",
    [
        pytest.param(
            _ledger(
                capacity=4,
                stored_lower="3.5",
                stored_upper="3.6",
                pv_lower="2.7",
                pv_burden=120,
                pv_density_upper=Fraction(400, 9),
            ),
            id="stored-lower",
        ),
        pytest.param(
            _ledger(
                capacity=4,
                stored_lower="3.6",
                stored_upper="3.7",
                pv_lower="2.7",
                pv_burden=120,
                pv_density_upper=Fraction(400, 9),
            ),
            id="stored-upper",
        ),
        pytest.param(
            _ledger(
                capacity=4,
                stored_lower="3.6",
                stored_upper="3.6",
                pv_lower="2.8",
                pv_burden=120,
                pv_density_upper=Fraction(400, 9),
            ),
            id="pv-lower",
        ),
    ],
)
def test_charge_transition_rejects_inexact_energy_state(after: StorageLedger) -> None:
    """Charge effects determine all three successor energy bounds exactly."""
    transition = _accepted(_advance(_empty_ledger(4), _flows(charge=4, pv_to_charge=3)))

    with pytest.raises(DomainInvariantError):
        replace(transition, after=after)


def test_charge_transition_rejects_decreasing_burden() -> None:
    """Stored guaranteed PV burden is monotone while charging."""
    before = _ledger(
        capacity=5,
        stored_lower=1,
        stored_upper=1,
        pv_lower=1,
        pv_burden=40,
        pv_density_upper=40,
    )
    transition = _accepted(
        _advance(
            before,
            _flows(charge=1),
            efficiency=_efficiency(1),
        )
    )
    forged_after = replace(
        transition.after,
        pv_burden=Emissions.from_grams(39),
    )

    with pytest.raises(DomainInvariantError):
        replace(transition, after=forged_after)


def test_charge_without_pv_rejects_changed_burden_or_density() -> None:
    """A grid-only charge cannot alter the tracked PV cohort envelope."""
    before = _ledger(
        capacity=5,
        stored_lower=1,
        stored_upper=1,
        pv_lower=1,
        pv_burden=40,
        pv_density_upper=40,
    )
    transition = _accepted(
        _advance(
            before,
            _flows(charge=1),
            efficiency=_efficiency(1),
        )
    )
    forged_after = replace(
        transition.after,
        pv_burden=Emissions.from_grams(41),
        pv_density_upper=EmissionDensity.from_g_per_kwh(41),
    )

    with pytest.raises(DomainInvariantError):
        replace(transition, after=forged_after)


def test_charge_transition_rejects_inconsistent_cohort_density() -> None:
    """Burden added per stored PV kWh determines the successor density."""
    transition = _accepted(_advance(_empty_ledger(4), _flows(charge=4, pv_to_charge=3)))
    forged_after = replace(
        transition.after,
        pv_density_upper=EmissionDensity.from_g_per_kwh(50),
    )

    with pytest.raises(DomainInvariantError):
        replace(transition, after=forged_after)


def test_charge_transition_rejects_decreasing_density() -> None:
    """A new PV cohort cannot weaken an existing conservative density bound."""
    before = _ledger(
        capacity=5,
        stored_lower=1,
        stored_upper=1,
        pv_lower=1,
        pv_burden=40,
        pv_density_upper=40,
    )
    transition = _accepted(
        _advance(
            before,
            _flows(charge=1, pv_to_charge=1),
            efficiency=_efficiency(1),
            factor=_factor(0),
        )
    )
    forged_after = replace(
        transition.after,
        pv_density_upper=EmissionDensity.from_g_per_kwh(30),
    )

    with pytest.raises(DomainInvariantError):
        replace(transition, after=forged_after)


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(
            lambda transition: replace(
                transition,
                after=replace(transition.after, stored_lower=_energy("1.5")),
            ),
            id="stored-lower",
        ),
        pytest.param(
            lambda transition: replace(
                transition,
                after=replace(transition.after, pv_lower=_energy("0.8")),
            ),
            id="pv-lower",
        ),
        pytest.param(
            lambda transition: replace(
                transition,
                effects=replace(
                    transition.effects,
                    pv_discharged=_energy("1.2"),
                    pv_used_locally=_energy("1.1"),
                ),
            ),
            id="pv-discharged",
        ),
        pytest.param(
            lambda transition: replace(
                transition,
                effects=replace(
                    transition.effects,
                    pv_burden_used=Emissions.from_grams(50),
                ),
            ),
            id="burden-used",
        ),
        pytest.param(
            lambda transition: replace(
                transition,
                after=replace(
                    transition.after,
                    pv_burden=Emissions.from_grams(30),
                ),
            ),
            id="burden-retained",
        ),
        pytest.param(
            lambda transition: replace(
                transition,
                effects=replace(
                    transition.effects,
                    pv_burden_discarded=Emissions.from_grams(41),
                ),
            ),
            id="burden-discarded",
        ),
        pytest.param(
            lambda transition: replace(
                transition,
                after=replace(
                    transition.after,
                    pv_density_upper=EmissionDensity.from_g_per_kwh(50),
                ),
            ),
            id="density",
        ),
        pytest.param(
            lambda transition: replace(
                transition,
                effects=replace(
                    transition.effects,
                    consumers=(
                        replace(
                            transition.effects.consumers[0],
                            pv_burden_view=Emissions.from_grams(50),
                        ),
                    ),
                ),
            ),
            id="consumer-burden-view",
        ),
    ],
)
def test_discharge_transition_rejects_inexact_successor_or_effects(
    build: Callable[[StorageTransition], StorageTransition],
) -> None:
    """Discharge state, burden envelope, and consumer views are reconstructible."""
    transition = _accepted(
        _advance(_mixed_charge_ledger(), _flows(discharge=2, battery_to_local=2))
    )

    with pytest.raises(DomainInvariantError):
        build(transition)


def test_discharge_transition_rejects_impossible_consumer_intersections() -> None:
    """Independent consumer bounds must fit one shared non-PV uncertainty mass."""
    transition = _accepted(
        _advance(_mixed_charge_ledger(), _flows(discharge=2, battery_to_local=2))
    )
    consumer_burden = Emissions.from_grams(Fraction(220, 9))
    forged_effects = replace(
        transition.effects,
        consumers=(
            ConsumerStorageCredit("house", _energy("0.55"), consumer_burden),
            ConsumerStorageCredit("wallbox", _energy("0.55"), consumer_burden),
        ),
        unassigned_local_pv=Energy.zero(),
    )

    with pytest.raises(DomainInvariantError):
        replace(transition, effects=forged_effects)


def test_idle_transition_rejects_state_or_effect_changes() -> None:
    """Zero discharge and charge require identity state and entirely zero effects."""
    transition = _accepted(_advance(_mixed_charge_ledger(), _flows()))
    forged_after = replace(transition.after, stored_lower=_energy("3.5"))
    forged_effects = replace(
        transition.effects,
        pv_burden_discarded=Emissions.from_grams(1),
    )

    with pytest.raises(DomainInvariantError):
        replace(transition, after=forged_after)
    with pytest.raises(DomainInvariantError):
        replace(transition, effects=forged_effects)


def test_storage_rejected_requires_reason_and_exact_quarantine() -> None:
    """A rejection cannot expose an informative or malformed successor state."""
    rejected = StorageRejected(
        quarantined_ledger=StorageLedger.quarantined(_energy(4)),
        reason=StorageRejectionReason.CAPACITY_OVERFLOW,
    )

    with pytest.raises(DomainInvariantError):
        replace(rejected, quarantined_ledger=_empty_ledger(4))
    with pytest.raises(DomainInvariantError):
        replace(
            rejected,
            quarantined_ledger=cast("StorageLedger", object()),
        )
    with pytest.raises(DomainInvariantError):
        replace(rejected, reason=cast("StorageRejectionReason", object()))


def test_adr_9_3_pv_storage_cycle_defers_and_then_uses_burden() -> None:
    """ADR 9.3 charges 2.7 kWh and credits only its later local discharge."""
    charge = _accepted(
        _advance(
            _empty_ledger(3),
            _flows(charge=3, pv_to_charge=3),
        )
    )

    assert charge.after.stored_lower == _energy("2.7")
    assert charge.after.stored_upper == _energy("2.7")
    assert charge.after.pv_lower == _energy("2.7")
    assert charge.after.non_pv_upper == Energy.zero()
    assert charge.after.pv_burden == Emissions.from_grams(120)
    assert charge.after.pv_density_upper == EmissionDensity.from_g_per_kwh(
        Fraction(400, 9)
    )
    assert charge.effects.stored_charge == _energy("2.7")
    assert charge.effects.pv_stored_charge == _energy("2.7")
    assert charge.effects.pv_used_locally == Energy.zero()
    assert charge.effects.pv_burden_used == Emissions.zero()

    discharge = _accepted(
        _advance(
            charge.after,
            _flows(discharge=2, battery_to_local=2),
        )
    )

    assert discharge.effects.pv_discharged == _energy(2)
    assert discharge.effects.pv_used_locally == _energy(2)
    assert discharge.effects.pv_burden_used == Emissions.from_grams(Fraction(800, 9))
    assert discharge.effects.pv_burden_discarded == Emissions.zero()
    assert discharge.after.stored_lower == _energy("0.7")
    assert discharge.after.stored_upper == _energy("0.7")
    assert discharge.after.pv_lower == _energy("0.7")
    assert discharge.after.pv_burden == Emissions.from_grams(Fraction(280, 9))
    _assert_invariants(discharge.after)


def test_adr_9_4_mixed_charge_tracks_only_guaranteed_pv() -> None:
    """The mixed PV/grid charge creates the exact ADR 9.4 bounds."""
    ledger = _mixed_charge_ledger()

    assert ledger.stored_lower == _energy("3.6")
    assert ledger.stored_upper == _energy("3.6")
    assert ledger.pv_lower == _energy("2.7")
    assert ledger.non_pv_upper == _energy("0.9")
    assert ledger.pv_burden == Emissions.from_grams(120)
    assert ledger.pv_density_upper == EmissionDensity.from_g_per_kwh(Fraction(400, 9))
    _assert_invariants(ledger)


def test_adr_9_4_full_local_discharge_credits_only_1_1_kwh() -> None:
    """Non-PV uncertainty reduces a fully local discharge to 1.1 PV kWh."""
    transition = _accepted(
        _advance(
            _mixed_charge_ledger(),
            _flows(discharge=2, battery_to_local=2),
        )
    )

    assert transition.effects.pv_discharged == _energy("1.1")
    assert transition.effects.pv_used_locally == _energy("1.1")
    assert transition.effects.pv_burden_used == Emissions.from_grams(Fraction(440, 9))
    assert transition.effects.pv_burden_discarded == Emissions.from_grams(40)
    assert transition.after.stored_lower == _energy("1.6")
    assert transition.after.stored_upper == _energy("1.6")
    assert transition.after.pv_lower == _energy("0.7")
    assert transition.after.non_pv_upper == _energy("0.9")
    assert transition.after.pv_burden == Emissions.from_grams(Fraction(280, 9))
    _assert_invariants(transition.after)


def test_adr_9_4_partial_local_discharge_uses_strict_intersection() -> None:
    """One local and one exported kWh guarantee only 0.1 local PV kWh."""
    transition = _accepted(
        _advance(
            _mixed_charge_ledger(),
            _flows(discharge=2, battery_to_local=1),
        )
    )

    assert transition.effects.pv_discharged == _energy("1.1")
    assert transition.effects.pv_used_locally == _energy("0.1")
    assert transition.effects.pv_burden_used == Emissions.from_grams(Fraction(40, 9))
    assert transition.effects.pv_burden_discarded == Emissions.from_grams(
        Fraction(760, 9)
    )
    assert transition.after.pv_lower == _energy("0.7")
    assert transition.after.pv_burden == Emissions.from_grams(Fraction(280, 9))
    _assert_invariants(transition.after)


def test_charge_tightens_unknown_start_without_inventing_free_capacity() -> None:
    """Observed charge shrinks the unknown pre-charge range before adding energy."""
    transition = _accepted(
        _advance(
            StorageLedger.quarantined(_energy(10)),
            _flows(charge=2, pv_to_charge=1),
        )
    )

    assert transition.after.stored_lower == _energy("1.8")
    assert transition.after.stored_upper == _energy(10)
    assert transition.after.pv_lower == _energy("0.9")
    assert transition.after.non_pv_upper == _energy("9.1")
    assert transition.after.pv_burden == Emissions.from_grams(40)
    _assert_invariants(transition.after)


def test_zero_pv_factor_keeps_positive_pv_energy_with_zero_burden() -> None:
    """The ADR permits the converse of the zero-PV implication to be false."""
    charge = _accepted(
        _advance(
            _empty_ledger(1),
            _flows(charge=1, pv_to_charge=1),
            efficiency=_efficiency("0.5"),
            factor=_factor(0),
        )
    )

    assert charge.after.pv_lower == _energy("0.5")
    assert charge.after.pv_burden == Emissions.zero()
    assert charge.after.pv_density_upper == EmissionDensity.from_g_per_kwh(0)

    discharge = _accepted(
        _advance(
            charge.after,
            _flows(discharge="0.5", battery_to_local="0.5"),
            factor=_factor(0),
        )
    )
    assert discharge.effects.pv_used_locally == _energy("0.5")
    assert discharge.effects.pv_burden_used == Emissions.zero()
    assert discharge.after == _empty_ledger(1)


def test_periodic_decimal_case_retains_exact_density_envelope() -> None:
    """F=1, eta=.03, and E=.01 remain exact despite repeating density."""
    transition = _accepted(
        _advance(
            _empty_ledger(1),
            _flows(charge="0.01", pv_to_charge="0.01"),
            efficiency=_efficiency("0.03"),
            factor=_factor(1),
        )
    )

    assert transition.after.pv_lower == _energy(Fraction(3, 10_000))
    assert transition.after.pv_burden == Emissions.from_grams(Fraction(1, 100))
    assert transition.after.pv_density_upper == EmissionDensity.from_g_per_kwh(
        Fraction(100, 3)
    )
    assert transition.after.pv_burden.grams == (
        transition.after.pv_density_upper.grams_per_kwh * transition.after.pv_lower.kwh
    )
    _assert_invariants(transition.after)


def test_internal_density_can_exceed_the_configured_factor_limit() -> None:
    """A low efficiency may lift output density above the factor input range."""
    transition = _accepted(
        _advance(
            _empty_ledger(1),
            _flows(charge="0.01", pv_to_charge="0.01"),
            efficiency=_efficiency("0.03"),
            factor=_factor(5000),
        )
    )

    assert transition.after.pv_density_upper == EmissionDensity.from_g_per_kwh(
        Fraction(500_000, 3)
    )
    assert transition.after.pv_burden == Emissions.from_grams(50)
    _assert_invariants(transition.after)


def test_capacity_overflow_rejects_and_quarantines() -> None:
    """A charge exceeding the guaranteed free capacity fails closed."""
    before = _ledger(
        capacity=1,
        stored_lower=1,
        stored_upper=1,
        pv_lower=0,
        pv_burden=0,
        pv_density_upper=0,
    )
    rejected = _rejected(
        _advance(
            before,
            _flows(charge="0.01"),
            efficiency=_efficiency(1),
        )
    )

    assert rejected.reason is StorageRejectionReason.CAPACITY_OVERFLOW
    assert rejected.quarantined_ledger == StorageLedger.quarantined(_energy(1))
    assert before.stored_lower == _energy(1)


def test_discharge_above_upper_bound_rejects_and_quarantines() -> None:
    """A discharge above even the possible stock fails closed."""
    before = _ledger(
        capacity=2,
        stored_lower=1,
        stored_upper=1,
        pv_lower=1,
        pv_burden=40,
        pv_density_upper=40,
    )
    rejected = _rejected(
        _advance(
            before,
            _flows(discharge="1.01", battery_to_local="1.01"),
        )
    )

    assert rejected.reason is StorageRejectionReason.DISCHARGE_EXCEEDS_UPPER_BOUND
    assert rejected.quarantined_ledger == StorageLedger.quarantined(_energy(2))
    assert before.pv_lower == _energy(1)


@pytest.mark.parametrize(
    "local",
    [pytest.param(1, id="partial"), pytest.param(2, id="full")],
)
def test_discharge_conserves_the_system_burden_envelope(local: int) -> None:
    """Used, retained, and discarded burden exactly partition prior burden."""
    before = _mixed_charge_ledger()
    transition = _accepted(
        _advance(
            before,
            _flows(discharge=2, battery_to_local=local),
        )
    )

    assert before.pv_burden == (
        transition.effects.pv_burden_used
        + transition.after.pv_burden
        + transition.effects.pv_burden_discarded
    )
    _assert_invariants(transition.after)


def test_consumer_bounds_leave_exact_unassigned_system_remainder() -> None:
    """Independent consumer intersections never replace the system envelope."""
    transition = _accepted(
        _advance(
            _mixed_charge_ledger(),
            _consumer_remainder_flows(),
        )
    )

    house, wallbox = transition.effects.consumers
    assert house.consumer_id == "house"
    assert house.energy == _energy("0.6")
    assert house.pv_burden_view == Emissions.from_grams(Fraction(80, 3))
    assert wallbox.consumer_id == "wallbox"
    assert wallbox.energy == Energy.zero()
    assert wallbox.pv_burden_view == Emissions.zero()
    assert transition.effects.pv_used_locally == _energy("1.1")
    assert transition.effects.unassigned_local_pv == _energy("0.5")
    assert (
        sum(
            (credit.energy.kwh for credit in transition.effects.consumers),
            start=transition.effects.unassigned_local_pv.kwh,
        )
        == transition.effects.pv_used_locally.kwh
    )
    assert (
        sum(
            (credit.pv_burden_view.grams for credit in transition.effects.consumers),
            start=_ZERO,
        )
        != transition.effects.pv_burden_used.grams
    )


def test_sequential_discharges_cannot_credit_pv_twice() -> None:
    """The conservative successor bound consumes eligibility at most once."""
    initial = _mixed_charge_ledger()
    first = _accepted(_advance(initial, _flows(discharge=2, battery_to_local=2)))
    second = _accepted(
        _advance(
            first.after,
            _flows(discharge="1.6", battery_to_local="1.6"),
        )
    )

    credited = first.effects.pv_used_locally.kwh + second.effects.pv_used_locally.kwh
    assert credited == Fraction(9, 5)
    assert credited + second.after.pv_lower.kwh <= initial.pv_lower.kwh
    assert second.after.pv_lower == Energy.zero()
    assert second.after.pv_burden == Emissions.zero()
    assert second.after.pv_density_upper == EmissionDensity.from_g_per_kwh(0)
    assert initial.pv_burden == (
        first.effects.pv_burden_used
        + first.effects.pv_burden_discarded
        + second.effects.pv_burden_used
        + second.effects.pv_burden_discarded
        + second.after.pv_burden
    )
    _assert_invariants(second.after)


def test_idle_interval_preserves_state_and_emits_zero_effects() -> None:
    """An interval without battery movement cannot alter eligibility."""
    before = _mixed_charge_ledger()
    transition = _accepted(_advance(before, _flows()))

    assert transition.before is before
    assert transition.after is before
    assert transition.effects.stored_charge == Energy.zero()
    assert transition.effects.pv_stored_charge == Energy.zero()
    assert transition.effects.pv_discharged == Energy.zero()
    assert transition.effects.pv_used_locally == Energy.zero()
    assert transition.effects.pv_burden_used == Emissions.zero()
    assert transition.effects.pv_burden_discarded == Emissions.zero()
    assert transition.effects.unassigned_local_pv == Energy.zero()
    assert transition.effects.consumers[0].energy == Energy.zero()


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(
            lambda: advance_storage(
                cast("StorageLedger", object()),
                _flows(),
                _efficiency(),
                _factor(),
            ),
            id="ledger-type",
        ),
        pytest.param(
            lambda: advance_storage(
                _empty_ledger(),
                cast("FlowDecomposition", object()),
                _efficiency(),
                _factor(),
            ),
            id="flows-type",
        ),
        pytest.param(
            lambda: advance_storage(
                _empty_ledger(),
                _flows(),
                cast("Ratio", object()),
                _factor(),
            ),
            id="efficiency-type",
        ),
        pytest.param(
            lambda: advance_storage(
                _empty_ledger(),
                _flows(),
                _efficiency(),
                cast("EmissionFactor", object()),
            ),
            id="factor-type",
        ),
        pytest.param(
            lambda: _advance(
                _empty_ledger(),
                _flows(),
                efficiency=_efficiency(0),
            ),
            id="zero-efficiency",
        ),
        pytest.param(
            lambda: _advance(
                _empty_ledger(),
                _tamper_simultaneous_movement(_flows(charge=1)),
            ),
            id="simultaneous-charge-discharge",
        ),
    ],
)
def test_transition_rejects_structurally_invalid_inputs(
    call: Callable[[], StorageTransition | StorageRejected],
) -> None:
    """Wrong dimensions and upstream-invalid movement cannot mutate a ledger."""
    with pytest.raises(DomainValidationError):
        call()


@pytest.mark.parametrize(
    "flows",
    [
        pytest.param(
            _tamper_system_flow(
                _flows(charge=1, pv_to_charge=1),
                EnergySource.PV,
                EnergySink.BATTERY_CHARGE,
                2,
            ),
            id="pv-charge-above-charge",
        ),
        pytest.param(
            _tamper_system_flow(
                _flows(discharge=1, battery_to_local=1),
                EnergySource.BATTERY,
                EnergySink.LOCAL_LOAD,
                2,
            ),
            id="local-battery-flow-above-discharge",
        ),
        pytest.param(
            _tamper_consumers(
                _flows(discharge=1, battery_to_local=1),
                (ConsumerFlow("house", _energy(2), Energy.zero(), _energy(2)),),
            ),
            id="consumer-battery-flow-above-discharge",
        ),
    ],
)
def test_transition_rejects_impossible_flow_lower_bounds(
    flows: FlowDecomposition,
) -> None:
    """Guaranteed flow cells cannot exceed their physical interval margins."""
    with pytest.raises(DomainValidationError):
        _advance(_empty_ledger(), flows)


def test_consumer_guarantees_cannot_exceed_system_guarantee() -> None:
    """An inconsistent decomposition fails instead of yielding a negative rest."""
    flows = _flows(discharge=2, battery_to_local=2)
    inconsistent = _tamper_consumers(
        flows,
        (
            ConsumerFlow("house", _energy(2), Energy.zero(), _energy(2)),
            ConsumerFlow("wallbox", _energy(2), Energy.zero(), _energy(2)),
        ),
    )

    with pytest.raises(DomainValidationError):
        _advance(_mixed_charge_ledger(), inconsistent)


@pytest.mark.parametrize(
    ("efficiency", "charge", "pv_charge", "discharge", "local"),
    [
        pytest.param(1, 1, 0, 1, 1, id="grid-only"),
        pytest.param("0.5", 2, 1, "0.5", "0.25", id="mixed-half"),
        pytest.param("0.03", "0.01", "0.01", "0.0003", "0.0003", id="small"),
        pytest.param("0.9", 4, 3, 2, 1, id="adr-mixed"),
    ],
)
def test_rational_transition_grid_preserves_all_ledger_invariants(
    efficiency: ExactInput,
    charge: ExactInput,
    pv_charge: ExactInput,
    discharge: ExactInput,
    local: ExactInput,
) -> None:
    """Representative exact rational paths preserve state and effect bounds."""
    charged = _accepted(
        _advance(
            _empty_ledger(),
            _flows(charge=charge, pv_to_charge=pv_charge),
            efficiency=_efficiency(efficiency),
            factor=_factor(1),
        )
    )
    discharged = _accepted(
        _advance(
            charged.after,
            _flows(discharge=discharge, battery_to_local=local),
            factor=_factor(1),
        )
    )

    _assert_invariants(charged.after)
    _assert_invariants(discharged.after)
    assert discharged.effects.pv_used_locally.kwh >= _ZERO
    assert discharged.effects.unassigned_local_pv.kwh >= _ZERO
    assert discharged.effects.pv_used_locally.kwh <= (
        discharged.effects.pv_discharged.kwh
    )
    assert discharged.effects.pv_used_locally.kwh + discharged.after.pv_lower.kwh <= (
        charged.after.pv_lower.kwh
    )
