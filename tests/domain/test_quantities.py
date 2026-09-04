# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Tests for exact CO2 Saver domain quantities."""

from __future__ import annotations

import operator
from dataclasses import FrozenInstanceError
from decimal import Decimal
from fractions import Fraction
from typing import TYPE_CHECKING, cast

import pytest

from custom_components.co2saver.domain.errors import DomainValidationError
from custom_components.co2saver.domain.quantities import (
    EmissionDensity,
    EmissionFactor,
    Emissions,
    Energy,
    ExactInput,
    Ratio,
    exact_fraction,
)

if TYPE_CHECKING:
    from collections.abc import Callable


class _IntegerSubclass(int):
    """Unsupported subclass of an otherwise supported exact type."""


class _StringSubclass(str):
    """Unsupported subclass of an otherwise supported exact type."""

    __slots__ = ()


class _FractionSubclass(Fraction):
    """Unsupported subclass of an otherwise supported exact type."""


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("12.50", Fraction(25, 2), id="decimal-string"),
        pytest.param("-1.25e-2", Fraction(-1, 80), id="exponent-string"),
        pytest.param(7, Fraction(7), id="integer"),
        pytest.param(Decimal("0.125"), Fraction(1, 8), id="decimal"),
        pytest.param(Fraction(2, 3), Fraction(2, 3), id="fraction"),
    ],
)
def test_exact_fraction_accepts_supported_types(
    value: ExactInput,
    expected: Fraction,
) -> None:
    """Supported inputs retain their exact mathematical value."""
    assert exact_fraction(value) == expected


def test_exact_fraction_preserves_fraction_instance() -> None:
    """An already canonical fraction is returned without replacement."""
    value = Fraction(17, 19)

    assert exact_fraction(value) is value


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(True, id="bool"),
        pytest.param(False, id="false"),
        pytest.param(1.25, id="float"),
        pytest.param(float("nan"), id="float-nan"),
        pytest.param(float("inf"), id="float-infinity"),
        pytest.param(None, id="none"),
        pytest.param(object(), id="object"),
        pytest.param(_IntegerSubclass(1), id="integer-subclass"),
        pytest.param(_StringSubclass("1"), id="string-subclass"),
        pytest.param(_FractionSubclass(1, 2), id="fraction-subclass"),
    ],
)
def test_exact_fraction_rejects_unsupported_types(value: object) -> None:
    """Unsupported and inexact runtime types fail closed."""
    with pytest.raises(DomainValidationError):
        exact_fraction(cast("ExactInput", value))


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("", id="empty"),
        pytest.param("not-a-number", id="malformed"),
        pytest.param("1,25", id="decimal-comma"),
        pytest.param("NaN", id="string-nan"),
        pytest.param("sNaN", id="string-signaling-nan"),
        pytest.param("Infinity", id="string-infinity"),
        pytest.param("-Infinity", id="string-negative-infinity"),
        pytest.param(Decimal("NaN"), id="decimal-nan"),
        pytest.param(Decimal("sNaN"), id="decimal-signaling-nan"),
        pytest.param(Decimal("Infinity"), id="decimal-infinity"),
        pytest.param(Decimal("-Infinity"), id="decimal-negative-infinity"),
    ],
)
def test_exact_fraction_rejects_invalid_or_non_finite_values(
    value: str | Decimal,
) -> None:
    """Malformed, localized, and non-finite values are invalid."""
    with pytest.raises(DomainValidationError):
        exact_fraction(value)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("1", id="string"),
        pytest.param(1, id="integer"),
        pytest.param(Decimal(1), id="decimal"),
        pytest.param(1.0, id="float"),
        pytest.param(True, id="bool"),
        pytest.param(_FractionSubclass(1), id="fraction-subclass"),
    ],
)
def test_direct_constructors_require_canonical_fraction(value: object) -> None:
    """Every quantity constructor rejects a non-canonical representation."""
    fraction = cast("Fraction", value)

    with pytest.raises(DomainValidationError):
        Energy(fraction)
    with pytest.raises(DomainValidationError):
        Ratio(fraction)
    with pytest.raises(DomainValidationError):
        EmissionDensity(fraction)
    with pytest.raises(DomainValidationError):
        EmissionFactor(fraction)
    with pytest.raises(DomainValidationError):
        Emissions(fraction)


def test_direct_constructors_accept_fraction() -> None:
    """Canonical fractions can construct every quantity directly."""
    value = Fraction(1, 2)

    assert Energy(value).kwh == value
    assert Ratio(value).value == value
    assert EmissionDensity(value).grams_per_kwh == value
    assert EmissionFactor(value).grams_per_kwh == value
    assert Emissions(value).grams == value


@pytest.mark.parametrize(
    ("factory", "value", "expected_kwh"),
    [
        pytest.param(Energy.from_kwh, "1.25", Fraction(5, 4), id="kwh"),
        pytest.param(Energy.from_wh, "1250", Fraction(5, 4), id="wh"),
        pytest.param(Energy.from_mwh, "0.00125", Fraction(5, 4), id="mwh"),
    ],
)
def test_energy_unit_conversions_are_exact(
    factory: Callable[[ExactInput], Energy],
    value: ExactInput,
    expected_kwh: Fraction,
) -> None:
    """All energy constructors normalize exactly to kilowatt-hours."""
    assert factory(value) == Energy(expected_kwh)


@pytest.mark.parametrize(
    "factory",
    [
        pytest.param(Energy.from_kwh, id="kwh"),
        pytest.param(Energy.from_wh, id="wh"),
        pytest.param(Energy.from_mwh, id="mwh"),
    ],
)
def test_energy_rejects_negative_values(
    factory: Callable[[ExactInput], Energy],
) -> None:
    """Energy cannot be negative in any accepted source unit."""
    with pytest.raises(DomainValidationError):
        factory("-0.001")


def test_energy_zero_and_arithmetic() -> None:
    """Energy supports exact addition and non-negative subtraction."""
    first = Energy.from_kwh("1.25")
    second = Energy.from_wh(750)

    assert Energy.zero() == Energy(Fraction())
    assert first + second == Energy.from_kwh(2)
    assert first - second == Energy.from_kwh("0.5")
    assert first - first == Energy.zero()


def test_energy_subtraction_rejects_negative_result() -> None:
    """Subtracting more energy than available fails closed."""
    with pytest.raises(DomainValidationError):
        Energy.from_kwh(1) - Energy.from_kwh(2)


def test_energy_arithmetic_rejects_other_dimensions() -> None:
    """Energy arithmetic cannot mix incompatible physical quantities."""
    energy = Energy.from_kwh(1)
    emissions = Emissions.from_grams(1)

    with pytest.raises(DomainValidationError):
        operator.add(energy, emissions)
    with pytest.raises(DomainValidationError):
        operator.sub(energy, emissions)


@pytest.mark.parametrize(
    ("ratio", "expected"),
    [
        pytest.param(Ratio.from_value(0), Fraction(), id="value-zero"),
        pytest.param(Ratio.from_value(1), Fraction(1), id="value-one"),
        pytest.param(Ratio.from_value("0.125"), Fraction(1, 8), id="value"),
        pytest.param(Ratio.from_percent(0), Fraction(), id="percent-zero"),
        pytest.param(Ratio.from_percent(100), Fraction(1), id="percent-one"),
        pytest.param(Ratio.from_percent("12.5"), Fraction(1, 8), id="percent"),
    ],
)
def test_ratio_conversions_and_boundaries(ratio: Ratio, expected: Fraction) -> None:
    """Ratios accept both normalized and percentage representations."""
    assert ratio.value == expected


@pytest.mark.parametrize(
    ("factory", "value"),
    [
        pytest.param(Ratio.from_value, "-0.001", id="value-below"),
        pytest.param(Ratio.from_value, "1.001", id="value-above"),
        pytest.param(Ratio.from_percent, "-0.1", id="percent-below"),
        pytest.param(Ratio.from_percent, "100.1", id="percent-above"),
    ],
)
def test_ratio_rejects_values_outside_closed_interval(
    factory: Callable[[ExactInput], Ratio],
    value: ExactInput,
) -> None:
    """Ratios outside the inclusive zero-to-one interval are invalid."""
    with pytest.raises(DomainValidationError):
        factory(value)


def test_emission_factor_conversions_and_boundaries() -> None:
    """Emission factors normalize to grams per kilowatt-hour exactly."""
    assert EmissionFactor.from_g_per_kwh(0).grams_per_kwh == Fraction()
    assert EmissionFactor.from_g_per_kwh(5_000).grams_per_kwh == Fraction(5_000)
    assert EmissionFactor.from_kg_per_kwh("0.125").grams_per_kwh == Fraction(125)
    assert EmissionFactor.from_kg_per_kwh(5).grams_per_kwh == Fraction(5_000)


def test_emission_density_is_non_negative_without_upper_bound() -> None:
    """Internal burden densities allow exact values above the config limit."""
    assert EmissionDensity.from_g_per_kwh(0).grams_per_kwh == Fraction()
    assert EmissionDensity.from_g_per_kwh("5000.01").grams_per_kwh == Fraction(
        500_001,
        100,
    )
    assert EmissionDensity.from_g_per_kwh(50_000).grams_per_kwh == Fraction(50_000)


def test_emission_density_rejects_negative_value() -> None:
    """An internal burden density cannot be negative."""
    with pytest.raises(DomainValidationError):
        EmissionDensity.from_g_per_kwh("-0.001")


def test_emission_density_applies_exactly_to_energy() -> None:
    """An internal density produces exact emissions without clamping."""
    density = EmissionDensity.from_g_per_kwh("7500.5")

    assert density.apply(Energy.from_kwh("0.2")) == Emissions.from_grams("1500.1")


def test_emission_density_rejects_non_energy_operand() -> None:
    """Applying an emission density to another dimension fails validation."""
    density = EmissionDensity.from_g_per_kwh(100)

    with pytest.raises(DomainValidationError):
        density.apply(cast("Energy", Emissions.zero()))


@pytest.mark.parametrize(
    ("factory", "value"),
    [
        pytest.param(EmissionFactor.from_g_per_kwh, "-0.1", id="grams-below"),
        pytest.param(EmissionFactor.from_g_per_kwh, "5000.1", id="grams-above"),
        pytest.param(EmissionFactor.from_kg_per_kwh, "-0.001", id="kg-below"),
        pytest.param(EmissionFactor.from_kg_per_kwh, "5.001", id="kg-above"),
    ],
)
def test_emission_factor_rejects_values_outside_supported_range(
    factory: Callable[[ExactInput], EmissionFactor],
    value: ExactInput,
) -> None:
    """Emission intensities outside zero through 5000 g/kWh are invalid."""
    with pytest.raises(DomainValidationError):
        factory(value)


def test_emission_factor_applies_exactly_to_energy() -> None:
    """Multiplying factor and energy produces exact signed-capable emissions."""
    factor = EmissionFactor.from_g_per_kwh("123.45")
    energy = Energy.from_kwh("1.2")

    assert factor.apply(energy) == Emissions.from_grams("148.14")


def test_emission_factor_rejects_non_energy_operand() -> None:
    """Applying an emission factor to another dimension fails validation."""
    factor = EmissionFactor.from_g_per_kwh(100)

    with pytest.raises(DomainValidationError):
        factor.apply(cast("Energy", Emissions.zero()))


def test_emissions_conversions_zero_and_signed_arithmetic() -> None:
    """Emissions preserve signs across exact conversion and arithmetic."""
    positive = Emissions.from_kilograms("1.25")
    negative = Emissions.from_grams("-250.5")

    assert positive == Emissions.from_grams(1_250)
    assert Emissions.zero() == Emissions(Fraction())
    assert positive + negative == Emissions.from_grams("999.5")
    assert negative - positive == Emissions.from_grams("-1500.5")


def test_emissions_arithmetic_rejects_other_dimensions() -> None:
    """Emissions arithmetic cannot mix incompatible physical quantities."""
    emissions = Emissions.from_grams(1)
    energy = Energy.from_kwh(1)

    with pytest.raises(DomainValidationError):
        operator.add(emissions, energy)
    with pytest.raises(DomainValidationError):
        operator.sub(emissions, energy)


@pytest.mark.parametrize(
    ("quantity", "attribute"),
    [
        pytest.param(Energy.zero(), "kwh", id="energy"),
        pytest.param(Ratio.from_value(0), "value", id="ratio"),
        pytest.param(
            EmissionDensity.from_g_per_kwh(0),
            "grams_per_kwh",
            id="emission-density",
        ),
        pytest.param(
            EmissionFactor.from_g_per_kwh(0),
            "grams_per_kwh",
            id="emission-factor",
        ),
        pytest.param(Emissions.zero(), "grams", id="emissions"),
    ],
)
def test_quantities_are_immutable(quantity: object, attribute: str) -> None:
    """Canonical quantity fields cannot be reassigned after construction."""
    with pytest.raises(FrozenInstanceError):
        setattr(quantity, attribute, Fraction(1))
