# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Stable error semantics for the CO2 Saver domain model."""

from enum import StrEnum


class DomainValidationError(ValueError):
    """Reject structurally invalid domain input."""


class DomainInvariantError(RuntimeError):
    """Signal an internally inconsistent domain result."""


class IntervalRejectionReason(StrEnum):
    """Reasons why measured interval energy cannot be evaluated."""

    SIMULTANEOUS_CHARGE_DISCHARGE = "simultaneous_charge_discharge"
    SMART_METER_NEGATIVE_PV = "smart_meter_negative_pv"
    PV_PLAUSIBILITY_MISMATCH = "pv_plausibility_mismatch"
    SITE_IMBALANCE = "site_imbalance"


class StorageRejectionReason(StrEnum):
    """Reasons why a measured interval contradicts storage bounds."""

    CAPACITY_OVERFLOW = "capacity_overflow"
    DISCHARGE_EXCEEDS_UPPER_BOUND = "discharge_exceeds_upper_bound"
