# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Exact physical quantities used by the CO2 Saver domain model."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Self

from .errors import DomainValidationError

__all__ = (
    "EmissionDensity",
    "EmissionFactor",
    "Emissions",
    "Energy",
    "ExactInput",
    "Ratio",
    "exact_fraction",
)

type ExactInput = str | int | Decimal | Fraction

_ZERO = Fraction()
_ONE = Fraction(1)
_UNIT_SCALE = Fraction(1_000)
_PERCENT_SCALE = Fraction(100)
_MAX_EMISSION_FACTOR = Fraction(5_000)


def _require_exact_input_type(value: object, expected_type: type[object]) -> None:
    """Reject subclasses and other non-canonical runtime input types."""
    if type(value) is not expected_type:
        message = "value must use an exact supported input type"
        raise DomainValidationError(message)


def _fraction_from_decimal(value: Decimal) -> Fraction:
    """Convert a finite decimal to its exact rational representation."""
    if not value.is_finite():
        message = "value must be finite"
        raise DomainValidationError(message)
    return Fraction(value)


def _fraction_from_string(value: str) -> Fraction:
    """Parse a dot-decimal string without admitting localized commas."""
    if "," in value:
        message = "decimal input must use a dot, not a comma"
        raise DomainValidationError(message)
    try:
        decimal_value = Decimal(value)
    except InvalidOperation as err:
        message = "value must be a valid decimal number"
        raise DomainValidationError(message) from err
    return _fraction_from_decimal(decimal_value)


def exact_fraction(value: ExactInput) -> Fraction:
    """Return an exact finite fraction without accepting binary floats."""
    if isinstance(value, Fraction):
        _require_exact_input_type(value, Fraction)
        return value

    if isinstance(value, Decimal):
        _require_exact_input_type(value, Decimal)
        return _fraction_from_decimal(value)

    if isinstance(value, bool):
        message = "bool is not an exact numeric input"
        raise DomainValidationError(message)

    if isinstance(value, int):
        _require_exact_input_type(value, int)
        return Fraction(value)

    if isinstance(value, str):
        _require_exact_input_type(value, str)
        return _fraction_from_string(value)

    message = "value must be str, int, Decimal, or Fraction"
    raise DomainValidationError(message)


def _require_fraction(value: object, *, field_name: str) -> None:
    """Require the canonical representation at direct construction sites."""
    if type(value) is not Fraction:
        message = f"{field_name} must be a Fraction"
        raise DomainValidationError(message)


def _require_instance(value: object, expected_type: type[object]) -> None:
    """Reject operands from a different physical dimension."""
    if not isinstance(value, expected_type):
        message = f"operand must be {expected_type.__name__}"
        raise DomainValidationError(message)


@dataclass(frozen=True, slots=True)
class Energy:
    """A non-negative exact energy quantity in kilowatt-hours."""

    kwh: Fraction

    def __post_init__(self) -> None:
        """Validate the canonical unit and non-negative range."""
        _require_fraction(self.kwh, field_name="kwh")
        if self.kwh < _ZERO:
            message = "energy must not be negative"
            raise DomainValidationError(message)

    @classmethod
    def zero(cls) -> Self:
        """Return zero energy."""
        return cls(_ZERO)

    @classmethod
    def from_kwh(cls, value: ExactInput) -> Self:
        """Create energy from kilowatt-hours."""
        return cls(exact_fraction(value))

    @classmethod
    def from_wh(cls, value: ExactInput) -> Self:
        """Create energy from watt-hours."""
        return cls(exact_fraction(value) / _UNIT_SCALE)

    @classmethod
    def from_mwh(cls, value: ExactInput) -> Self:
        """Create energy from megawatt-hours."""
        return cls(exact_fraction(value) * _UNIT_SCALE)

    def __add__(self, other: Energy) -> Self:
        """Add energy quantities."""
        _require_instance(other, Energy)
        return type(self)(self.kwh + other.kwh)

    def __sub__(self, other: Energy) -> Self:
        """Subtract energy while preserving non-negativity."""
        _require_instance(other, Energy)
        return type(self)(self.kwh - other.kwh)


@dataclass(frozen=True, slots=True)
class Ratio:
    """An exact dimensionless ratio in the inclusive range from zero to one."""

    value: Fraction

    def __post_init__(self) -> None:
        """Validate the canonical type and inclusive range."""
        _require_fraction(self.value, field_name="value")
        if not _ZERO <= self.value <= _ONE:
            message = "ratio must be between zero and one"
            raise DomainValidationError(message)

    @classmethod
    def from_value(cls, value: ExactInput) -> Self:
        """Create a ratio from a value between zero and one."""
        return cls(exact_fraction(value))

    @classmethod
    def from_percent(cls, value: ExactInput) -> Self:
        """Create a ratio from a percentage between zero and one hundred."""
        return cls(exact_fraction(value) / _PERCENT_SCALE)


@dataclass(frozen=True, slots=True)
class EmissionDensity:
    """A non-negative internal emissions burden in grams per kilowatt-hour."""

    grams_per_kwh: Fraction

    def __post_init__(self) -> None:
        """Validate the canonical type and non-negative range."""
        _require_fraction(self.grams_per_kwh, field_name="grams_per_kwh")
        if self.grams_per_kwh < _ZERO:
            message = "emission density must not be negative"
            raise DomainValidationError(message)

    @classmethod
    def from_g_per_kwh(cls, value: ExactInput) -> Self:
        """Create an emission density from grams per kilowatt-hour."""
        return cls(exact_fraction(value))

    def apply(self, energy: Energy) -> Emissions:
        """Apply this burden density to an energy quantity."""
        _require_instance(energy, Energy)
        return Emissions(self.grams_per_kwh * energy.kwh)


@dataclass(frozen=True, slots=True)
class EmissionFactor:
    """An exact emissions intensity in grams per kilowatt-hour."""

    grams_per_kwh: Fraction

    def __post_init__(self) -> None:
        """Validate the canonical type and supported technical range."""
        _require_fraction(self.grams_per_kwh, field_name="grams_per_kwh")
        if not _ZERO <= self.grams_per_kwh <= _MAX_EMISSION_FACTOR:
            message = "emission factor must be between 0 and 5000 g/kWh"
            raise DomainValidationError(message)

    @classmethod
    def from_g_per_kwh(cls, value: ExactInput) -> Self:
        """Create an emission factor from grams per kilowatt-hour."""
        return cls(exact_fraction(value))

    @classmethod
    def from_kg_per_kwh(cls, value: ExactInput) -> Self:
        """Create an emission factor from kilograms per kilowatt-hour."""
        return cls(exact_fraction(value) * _UNIT_SCALE)

    def apply(self, energy: Energy) -> Emissions:
        """Apply this intensity to an energy quantity."""
        _require_instance(energy, Energy)
        return Emissions(self.grams_per_kwh * energy.kwh)


@dataclass(frozen=True, slots=True)
class Emissions:
    """An exact signed emissions quantity in grams of CO2 equivalent."""

    grams: Fraction

    def __post_init__(self) -> None:
        """Validate the canonical unit representation."""
        _require_fraction(self.grams, field_name="grams")

    @classmethod
    def zero(cls) -> Self:
        """Return zero emissions."""
        return cls(_ZERO)

    @classmethod
    def from_grams(cls, value: ExactInput) -> Self:
        """Create emissions from grams."""
        return cls(exact_fraction(value))

    @classmethod
    def from_kilograms(cls, value: ExactInput) -> Self:
        """Create emissions from kilograms."""
        return cls(exact_fraction(value) * _UNIT_SCALE)

    def __add__(self, other: Emissions) -> Self:
        """Add emissions quantities."""
        _require_instance(other, Emissions)
        return type(self)(self.grams + other.grams)

    def __sub__(self, other: Emissions) -> Self:
        """Subtract emissions quantities."""
        _require_instance(other, Emissions)
        return type(self)(self.grams - other.grams)
