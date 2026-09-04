# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Conservative storage-provenance ledger for the CO2 Saver domain."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import NoReturn

from .errors import DomainInvariantError, DomainValidationError, StorageRejectionReason
from .models import ConsumerFlow, FlowDecomposition
from .quantities import EmissionDensity, EmissionFactor, Emissions, Energy, Ratio

_ZERO = Fraction(0)
_MIN_CAPACITY_KWH = Fraction(1, 10)
_MAX_CAPACITY_KWH = Fraction(1000)


def _invalid(message: str) -> NoReturn:
    raise DomainValidationError(message)


def _invariant(message: str) -> NoReturn:
    raise DomainInvariantError(message)


def _require_type(value: object, expected: type[object], name: str) -> None:
    if not isinstance(value, expected):
        _invalid(f"{name} must be {expected.__name__}")


def _require_result_type(value: object, expected: type[object], name: str) -> None:
    if not isinstance(value, expected):
        _invariant(f"{name} must be {expected.__name__}")


@dataclass(frozen=True, slots=True)
class StorageLedger:
    """Bounds on stored energy and guaranteed eligible PV provenance."""

    capacity: Energy
    stored_lower: Energy
    stored_upper: Energy
    pv_lower: Energy
    pv_burden: Emissions
    pv_density_upper: EmissionDensity

    def __post_init__(self) -> None:
        """Validate every ADR-0001 storage-ledger invariant."""
        _require_type(self.capacity, Energy, "capacity")
        _require_type(self.stored_lower, Energy, "stored_lower")
        _require_type(self.stored_upper, Energy, "stored_upper")
        _require_type(self.pv_lower, Energy, "pv_lower")
        _require_type(self.pv_burden, Emissions, "pv_burden")
        _require_type(
            self.pv_density_upper,
            EmissionDensity,
            "pv_density_upper",
        )

        capacity = self.capacity.kwh
        stored_lower = self.stored_lower.kwh
        stored_upper = self.stored_upper.kwh
        pv_lower = self.pv_lower.kwh
        burden = self.pv_burden.grams
        density = self.pv_density_upper.grams_per_kwh

        if not _MIN_CAPACITY_KWH <= capacity <= _MAX_CAPACITY_KWH:
            _invalid("capacity must be between 0.1 and 1000 kWh")
        if not _ZERO <= stored_lower <= stored_upper <= capacity:
            _invalid("stored bounds must satisfy 0 <= lower <= upper <= capacity")
        if not _ZERO <= pv_lower <= stored_lower:
            _invalid("pv_lower must satisfy 0 <= pv_lower <= stored_lower")
        if burden < _ZERO:
            _invalid("pv_burden must be non-negative")
        if burden > density * pv_lower:
            _invalid("pv_burden exceeds the density envelope")
        if pv_lower == _ZERO and (burden != _ZERO or density != _ZERO):
            _invalid("an empty PV guarantee requires zero burden and density")

    @property
    def non_pv_upper(self) -> Energy:
        """Return the upper bound on grid-sourced or unknown stored energy."""
        return Energy(self.stored_upper.kwh - self.pv_lower.kwh)

    @classmethod
    def quarantined(cls, capacity: Energy) -> StorageLedger:
        """Create the conservative state for completely unknown storage content."""
        return cls(
            capacity=capacity,
            stored_lower=Energy.zero(),
            stored_upper=capacity,
            pv_lower=Energy.zero(),
            pv_burden=Emissions.zero(),
            pv_density_upper=EmissionDensity(_ZERO),
        )


@dataclass(frozen=True, slots=True)
class ConsumerStorageCredit:
    """Guaranteed local PV-storage energy and its independent burden view."""

    consumer_id: str
    energy: Energy
    pv_burden_view: Emissions

    def __post_init__(self) -> None:
        """Require a well-formed independently conservative consumer view."""
        if not isinstance(self.consumer_id, str) or not self.consumer_id.strip():
            _invariant("consumer_id must not be empty")
        _require_result_type(self.energy, Energy, "energy")
        _require_result_type(self.pv_burden_view, Emissions, "pv_burden_view")
        if self.pv_burden_view.grams < _ZERO:
            _invariant("pv_burden_view must be non-negative")
        if self.energy.kwh == _ZERO and self.pv_burden_view.grams != _ZERO:
            _invariant("zero consumer energy requires zero burden")


@dataclass(frozen=True, slots=True)
class StorageEffects:
    """Observable energy and burden effects of one accepted storage transition."""

    stored_charge: Energy
    pv_stored_charge: Energy
    pv_discharged: Energy
    pv_used_locally: Energy
    pv_burden_used: Emissions
    pv_burden_discarded: Emissions
    consumers: tuple[ConsumerStorageCredit, ...]
    unassigned_local_pv: Energy

    def __post_init__(self) -> None:
        """Require internally closed immutable storage effects."""
        _validate_storage_effects(self)


@dataclass(frozen=True, slots=True)
class StorageTransition:
    """Accepted transition between two valid immutable ledger states."""

    before: StorageLedger
    after: StorageLedger
    effects: StorageEffects

    def __post_init__(self) -> None:
        """Reconstruct and validate the exact accepted ledger transition."""
        _require_result_type(self.before, StorageLedger, "before")
        _require_result_type(self.after, StorageLedger, "after")
        _require_result_type(self.effects, StorageEffects, "effects")
        _validate_storage_transition(self)


@dataclass(frozen=True, slots=True)
class StorageRejected:
    """Rejected physical transition and its mandatory quarantine state."""

    quarantined_ledger: StorageLedger
    reason: StorageRejectionReason

    def __post_init__(self) -> None:
        """Require a declared rejection carrying only quarantined state."""
        _require_result_type(
            self.quarantined_ledger,
            StorageLedger,
            "quarantined_ledger",
        )
        _require_result_type(self.reason, StorageRejectionReason, "reason")
        expected = StorageLedger.quarantined(self.quarantined_ledger.capacity)
        if self.quarantined_ledger != expected:
            _invariant("a storage rejection must carry the exact quarantine ledger")


def _validated_effect_consumers(
    effects: StorageEffects,
) -> tuple[ConsumerStorageCredit, ...]:
    try:
        consumers = tuple(effects.consumers)
    except TypeError as err:
        msg = "consumers must be an iterable of ConsumerStorageCredit"
        raise DomainInvariantError(msg) from err
    object.__setattr__(effects, "consumers", consumers)
    if not consumers:
        _invariant("storage effects require at least the house consumer")
    for consumer in consumers:
        _require_result_type(consumer, ConsumerStorageCredit, "consumer")
        if (
            not isinstance(consumer.consumer_id, str)
            or not consumer.consumer_id.strip()
        ):
            _invariant("consumer storage credit ids must not be empty")
    consumer_ids = [consumer.consumer_id for consumer in consumers]
    if len(consumer_ids) != len(set(consumer_ids)):
        _invariant("consumer storage credit ids must be unique")
    return consumers


def _validate_effect_quantities(effects: StorageEffects) -> None:
    energy_fields = (
        (effects.stored_charge, "stored_charge"),
        (effects.pv_stored_charge, "pv_stored_charge"),
        (effects.pv_discharged, "pv_discharged"),
        (effects.pv_used_locally, "pv_used_locally"),
        (effects.unassigned_local_pv, "unassigned_local_pv"),
    )
    for value, name in energy_fields:
        _require_result_type(value, Energy, name)
    _require_result_type(effects.pv_burden_used, Emissions, "pv_burden_used")
    _require_result_type(
        effects.pv_burden_discarded,
        Emissions,
        "pv_burden_discarded",
    )


def _validate_storage_effects(effects: StorageEffects) -> None:
    _validate_effect_quantities(effects)
    consumers = _validated_effect_consumers(effects)
    if effects.pv_stored_charge.kwh > effects.stored_charge.kwh:
        _invariant("PV stored charge cannot exceed total stored charge")
    if effects.pv_used_locally.kwh > effects.pv_discharged.kwh:
        _invariant("local PV use cannot exceed guaranteed PV discharge")
    assigned = sum((consumer.energy.kwh for consumer in consumers), _ZERO)
    if assigned + effects.unassigned_local_pv.kwh != effects.pv_used_locally.kwh:
        _invariant("consumer energy and unassigned energy must close locally")
    if effects.pv_burden_used.grams < _ZERO:
        _invariant("pv_burden_used must be non-negative")
    if effects.pv_used_locally.kwh == _ZERO and effects.pv_burden_used.grams != _ZERO:
        _invariant("zero local PV use requires zero used burden")
    if effects.pv_burden_discarded.grams < _ZERO:
        _invariant("pv_burden_discarded must be non-negative")
    if effects.stored_charge.kwh > _ZERO and not _charge_effects_are_exclusive(effects):
        _invariant("charging cannot carry discharge or local-credit effects")


def _charge_effects_are_exclusive(effects: StorageEffects) -> bool:
    return (
        effects.pv_discharged.kwh == _ZERO
        and effects.pv_used_locally.kwh == _ZERO
        and effects.pv_burden_used.grams == _ZERO
        and effects.pv_burden_discarded.grams == _ZERO
        and effects.unassigned_local_pv.kwh == _ZERO
        and all(
            consumer.energy.kwh == _ZERO and consumer.pv_burden_view.grams == _ZERO
            for consumer in effects.consumers
        )
    )


def _burden_envelope(ledger: StorageLedger, energy: Energy) -> Emissions:
    return Emissions(
        min(
            ledger.pv_burden.grams,
            ledger.pv_density_upper.grams_per_kwh * energy.kwh,
        )
    )


def _require_charge_transition(transition: StorageTransition) -> None:
    before = transition.before
    after = transition.after
    effects = transition.effects
    stored_charge = effects.stored_charge.kwh
    pv_stored_charge = effects.pv_stored_charge.kwh
    expected_upper = (
        min(
            before.stored_upper.kwh,
            before.capacity.kwh - stored_charge,
        )
        + stored_charge
    )
    if (
        after.stored_lower.kwh != before.stored_lower.kwh + stored_charge
        or after.stored_upper.kwh != expected_upper
        or after.pv_lower.kwh != before.pv_lower.kwh + pv_stored_charge
    ):
        _invariant("charge transition does not match the ADR ledger update")

    burden_delta = after.pv_burden.grams - before.pv_burden.grams
    if burden_delta < _ZERO:
        _invariant("charge transition cannot reduce PV burden")
    if pv_stored_charge == _ZERO:
        if burden_delta != _ZERO or after.pv_density_upper != before.pv_density_upper:
            _invariant("a charge without guaranteed PV cannot alter its burden")
        return
    expected_density = max(
        before.pv_density_upper.grams_per_kwh,
        burden_delta / pv_stored_charge,
    )
    if after.pv_density_upper.grams_per_kwh != expected_density:
        _invariant("charge transition has an inconsistent PV burden density")


def _effects_are_zero(effects: StorageEffects) -> bool:
    return (
        effects.stored_charge.kwh == _ZERO
        and effects.pv_stored_charge.kwh == _ZERO
        and effects.pv_discharged.kwh == _ZERO
        and effects.pv_used_locally.kwh == _ZERO
        and effects.pv_burden_used.grams == _ZERO
        and effects.pv_burden_discarded.grams == _ZERO
        and effects.unassigned_local_pv.kwh == _ZERO
        and all(
            consumer.energy.kwh == _ZERO and consumer.pv_burden_view.grams == _ZERO
            for consumer in effects.consumers
        )
    )


def _require_discharge_transition(
    transition: StorageTransition,
    discharge: Energy,
) -> None:
    before = transition.before
    after = transition.after
    effects = transition.effects
    expected_lower = max(_ZERO, before.stored_lower.kwh - discharge.kwh)
    expected_pv_lower = max(_ZERO, before.pv_lower.kwh - discharge.kwh)
    expected_pv_discharged = max(
        _ZERO,
        discharge.kwh - before.non_pv_upper.kwh,
    )
    if (
        after.stored_lower.kwh != expected_lower
        or after.pv_lower.kwh != expected_pv_lower
        or effects.pv_discharged.kwh != expected_pv_discharged
    ):
        _invariant("discharge transition does not match the ADR energy update")

    expected_burden_used = _burden_envelope(before, effects.pv_used_locally)
    if effects.pv_burden_used != expected_burden_used:
        _invariant("discharge transition has an inconsistent used PV burden")
    expected_burden = min(
        before.pv_burden.grams - expected_burden_used.grams,
        before.pv_density_upper.grams_per_kwh * expected_pv_lower,
    )
    if after.pv_burden.grams != expected_burden:
        _invariant("discharge transition has an inconsistent retained PV burden")
    expected_discarded = (
        before.pv_burden.grams - expected_burden_used.grams - expected_burden
    )
    if effects.pv_burden_discarded.grams != expected_discarded:
        _invariant("discharge transition has an inconsistent discarded PV burden")
    expected_density = (
        _ZERO if expected_burden == _ZERO else before.pv_density_upper.grams_per_kwh
    )
    if after.pv_density_upper.grams_per_kwh != expected_density:
        _invariant("discharge transition has an inconsistent PV burden density")
    for consumer in effects.consumers:
        if consumer.pv_burden_view != _burden_envelope(before, consumer.energy):
            _invariant("consumer storage burden does not match the ADR envelope")
    positive_consumer_sources = sum(
        (
            consumer.energy.kwh + before.non_pv_upper.kwh
            for consumer in effects.consumers
            if consumer.energy.kwh > _ZERO
        ),
        _ZERO,
    )
    system_source = effects.pv_used_locally.kwh + before.non_pv_upper.kwh
    if positive_consumer_sources > system_source:
        _invariant("consumer storage credits have no feasible source allocation")


def _validate_storage_transition(transition: StorageTransition) -> None:
    before = transition.before
    after = transition.after
    effects = transition.effects
    if before.capacity != after.capacity:
        _invariant("storage transition cannot change capacity")
    if effects.stored_charge.kwh > _ZERO:
        _require_charge_transition(transition)
        return
    if after.stored_upper.kwh > before.stored_upper.kwh:
        _invariant("a non-charge transition cannot increase stored energy")
    discharge = Energy(before.stored_upper.kwh - after.stored_upper.kwh)
    if discharge.kwh == _ZERO:
        if before != after or not _effects_are_zero(effects):
            _invariant("an idle transition must preserve state and have zero effects")
        return
    _require_discharge_transition(transition, discharge)


def _zero_consumer_credits(
    consumers: tuple[ConsumerFlow, ...],
) -> tuple[ConsumerStorageCredit, ...]:
    return tuple(
        ConsumerStorageCredit(
            consumer_id=consumer.consumer_id,
            energy=Energy.zero(),
            pv_burden_view=Emissions.zero(),
        )
        for consumer in consumers
    )


def _effects_without_discharge(
    flows: FlowDecomposition,
    *,
    stored_charge: Energy,
    pv_stored_charge: Energy,
) -> StorageEffects:
    return StorageEffects(
        stored_charge=stored_charge,
        pv_stored_charge=pv_stored_charge,
        pv_discharged=Energy.zero(),
        pv_used_locally=Energy.zero(),
        pv_burden_used=Emissions.zero(),
        pv_burden_discarded=Emissions.zero(),
        consumers=_zero_consumer_credits(flows.consumers),
        unassigned_local_pv=Energy.zero(),
    )


def _validate_transition_inputs(
    flows: FlowDecomposition,
    efficiency: Ratio,
    pv_factor_at_charge: EmissionFactor,
) -> None:
    _require_type(flows, FlowDecomposition, "flows")
    _require_type(efficiency, Ratio, "efficiency")
    _require_type(pv_factor_at_charge, EmissionFactor, "pv_factor_at_charge")
    if efficiency.value == _ZERO:
        _invalid("efficiency must satisfy 0 < efficiency <= 1")

    charge = flows.interval.battery_charge.kwh
    discharge = flows.interval.battery_discharge.kwh
    if flows.pv_to_charge.kwh > charge:
        _invalid("guaranteed PV charge cannot exceed total battery charge")
    if flows.battery_to_local.kwh > discharge:
        _invalid("guaranteed local battery flow cannot exceed battery discharge")
    if charge > _ZERO and discharge > _ZERO:
        _invalid(
            "simultaneous positive charging and discharging must be rejected upstream"
        )
    for consumer in flows.consumers:
        _require_type(consumer, ConsumerFlow, "consumer")
        if consumer.battery_to_local.kwh > discharge:
            _invalid("consumer battery flow cannot exceed battery discharge")


def _apply_charge(
    ledger: StorageLedger,
    flows: FlowDecomposition,
    efficiency: Ratio,
    pv_factor_at_charge: EmissionFactor,
) -> StorageTransition | StorageRejected:
    stored_charge = Energy(flows.interval.battery_charge.kwh * efficiency.value)
    pv_stored_charge = Energy(flows.pv_to_charge.kwh * efficiency.value)

    if ledger.stored_lower.kwh + stored_charge.kwh > ledger.capacity.kwh:
        return StorageRejected(
            quarantined_ledger=StorageLedger.quarantined(ledger.capacity),
            reason=StorageRejectionReason.CAPACITY_OVERFLOW,
        )

    stored_upper_before = min(
        ledger.stored_upper.kwh,
        ledger.capacity.kwh - stored_charge.kwh,
    )
    burden_added = pv_factor_at_charge.apply(flows.pv_to_charge)
    density = ledger.pv_density_upper
    if pv_stored_charge.kwh > _ZERO:
        density = EmissionDensity(
            max(
                density.grams_per_kwh,
                pv_factor_at_charge.grams_per_kwh / efficiency.value,
            )
        )

    after = StorageLedger(
        capacity=ledger.capacity,
        stored_lower=Energy(ledger.stored_lower.kwh + stored_charge.kwh),
        stored_upper=Energy(stored_upper_before + stored_charge.kwh),
        pv_lower=Energy(ledger.pv_lower.kwh + pv_stored_charge.kwh),
        pv_burden=Emissions(ledger.pv_burden.grams + burden_added.grams),
        pv_density_upper=density,
    )
    return StorageTransition(
        before=ledger,
        after=after,
        effects=_effects_without_discharge(
            flows,
            stored_charge=stored_charge,
            pv_stored_charge=pv_stored_charge,
        ),
    )


def _consumer_discharge_credits(
    ledger: StorageLedger,
    consumers: tuple[ConsumerFlow, ...],
) -> tuple[ConsumerStorageCredit, ...]:
    non_pv_upper = ledger.non_pv_upper.kwh
    return tuple(
        ConsumerStorageCredit(
            consumer_id=consumer.consumer_id,
            energy=(
                energy := Energy(
                    max(_ZERO, consumer.battery_to_local.kwh - non_pv_upper)
                )
            ),
            pv_burden_view=_burden_envelope(ledger, energy),
        )
        for consumer in consumers
    )


def _apply_discharge(
    ledger: StorageLedger,
    flows: FlowDecomposition,
) -> StorageTransition | StorageRejected:
    discharge = flows.interval.battery_discharge
    if discharge.kwh > ledger.stored_upper.kwh:
        return StorageRejected(
            quarantined_ledger=StorageLedger.quarantined(ledger.capacity),
            reason=StorageRejectionReason.DISCHARGE_EXCEEDS_UPPER_BOUND,
        )

    non_pv_upper = ledger.non_pv_upper.kwh
    pv_discharged = Energy(max(_ZERO, discharge.kwh - non_pv_upper))
    pv_used_locally = Energy(max(_ZERO, flows.battery_to_local.kwh - non_pv_upper))
    burden_used = _burden_envelope(ledger, pv_used_locally)

    stored_lower = Energy(max(_ZERO, ledger.stored_lower.kwh - discharge.kwh))
    stored_upper = Energy(ledger.stored_upper.kwh - discharge.kwh)
    pv_lower = Energy(max(_ZERO, ledger.pv_lower.kwh - discharge.kwh))
    burden = Emissions(
        min(
            ledger.pv_burden.grams - burden_used.grams,
            ledger.pv_density_upper.grams_per_kwh * pv_lower.kwh,
        )
    )
    density = (
        EmissionDensity(_ZERO) if burden.grams == _ZERO else ledger.pv_density_upper
    )
    after = StorageLedger(
        capacity=ledger.capacity,
        stored_lower=stored_lower,
        stored_upper=stored_upper,
        pv_lower=pv_lower,
        pv_burden=burden,
        pv_density_upper=density,
    )

    consumer_credits = _consumer_discharge_credits(ledger, flows.consumers)
    assigned = sum((credit.energy.kwh for credit in consumer_credits), start=_ZERO)
    if assigned > pv_used_locally.kwh:
        _invalid("consumer PV-storage guarantees exceed the system guarantee")
    burden_discarded = Emissions(
        ledger.pv_burden.grams - burden_used.grams - burden.grams
    )
    effects = StorageEffects(
        stored_charge=Energy.zero(),
        pv_stored_charge=Energy.zero(),
        pv_discharged=pv_discharged,
        pv_used_locally=pv_used_locally,
        pv_burden_used=burden_used,
        pv_burden_discarded=burden_discarded,
        consumers=consumer_credits,
        unassigned_local_pv=Energy(pv_used_locally.kwh - assigned),
    )
    return StorageTransition(before=ledger, after=after, effects=effects)


def advance_storage(
    ledger: StorageLedger,
    flows: FlowDecomposition,
    efficiency: Ratio,
    pv_factor_at_charge: EmissionFactor,
) -> StorageTransition | StorageRejected:
    """Advance the conservative ledger for one accepted energy interval."""
    if not isinstance(ledger, StorageLedger):
        _invalid("ledger must be StorageLedger")
    _validate_transition_inputs(flows, efficiency, pv_factor_at_charge)

    if flows.interval.battery_charge.kwh > _ZERO:
        return _apply_charge(ledger, flows, efficiency, pv_factor_at_charge)
    if flows.interval.battery_discharge.kwh > _ZERO:
        return _apply_discharge(ledger, flows)
    return StorageTransition(
        before=ledger,
        after=ledger,
        effects=_effects_without_discharge(
            flows,
            stored_charge=Energy.zero(),
            pv_stored_charge=Energy.zero(),
        ),
    )
