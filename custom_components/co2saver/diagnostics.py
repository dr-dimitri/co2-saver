# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Allowlisted diagnostics without source reads, storage I/O, or personal data."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from homeassistant.components.diagnostics import REDACTED

from .domain import StorageLedger, StorageRejectionReason
from .measurement.models import MeasurementPhase, MeasurementRejectionReason
from .persistence import (
    GENERATION_MINOR_VERSION,
    GENERATION_SCHEMA_VERSION,
    MANIFEST_MINOR_VERSION,
    MANIFEST_SCHEMA_VERSION,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .persistence import GenerationState
    from .runtime import EntryRuntime

_ENERGY_ROLES = frozenset(
    {
        "pv_generation",
        "pv_plausibility",
        "grid_import",
        "grid_export",
        "battery_charge",
        "battery_discharge",
        "local_load",
        "household",
    }
)
_DIAGNOSTIC_COUNTERS = (
    "discarded_intervals",
    "manifest_losses",
    "missing_grid_intensity",
    "segment_transitions",
)
_STATUSES = frozenset(
    {
        "ok",
        "awaiting_observation",
        "storage_error",
        "grid_source_mismatch",
        "invalid_grid_value",
        "invalid_grid_unit",
        "grid_source_stale",
        "source_disabled",
        "source_not_registered",
        *(reason.value for reason in MeasurementRejectionReason),
        *(reason.value for reason in StorageRejectionReason),
        *(phase.value for phase in MeasurementPhase),
    }
)


def _choice(value: object, choices: tuple[str, ...]) -> str | None:
    """Copy only a known configuration enumeration, never arbitrary user text."""
    return value if isinstance(value, str) and value in choices else None


def _mapping(value: object) -> Mapping[str, object]:
    """Accept incomplete configuration without serializing any unknown content."""
    return value if isinstance(value, Mapping) else {}


def _configured_roles(data: Mapping[str, object]) -> list[str]:
    """Recover only non-identifying roles when setup never produced runtime state."""
    roles = [
        role
        for role in ("pv_generation", "pv_plausibility", "grid_import", "grid_export")
        if role in _mapping(data.get("sources"))
    ]
    if isinstance(data.get("battery"), Mapping):
        roles.extend(("battery_charge", "battery_discharge"))
    consumption = _mapping(data.get("consumption"))
    if consumption.get("mode") == "aggregate_shares":
        roles.append("local_load")
    elif consumption.get("mode") == "separate_meters":
        roles.append("household")
        consumers = consumption.get("consumers")
        if isinstance(consumers, list):
            roles.extend("consumer" for row in consumers if isinstance(row, Mapping))
    return roles


def _sources(
    data: Mapping[str, object], state: GenerationState | None
) -> list[dict[str, str | None]]:
    """Expose cached units while redacting every source and consumer identity."""
    if state is None:
        sources = [
            {"role": role, "identity": REDACTED, "unit": None}
            for role in _configured_roles(data)
        ]
    else:
        measurement = state.measurement
        snapshot = measurement.baseline or measurement.candidate
        units = (
            {sample.source: sample.source_unit.value for sample in snapshot.samples}
            if snapshot is not None
            else {}
        )
        sources = []
        for source in measurement.sources:
            role = source.role
            if role.startswith("consumer:"):
                role = "consumer"
            elif role not in _ENERGY_ROLES:
                role = REDACTED
            sources.append(
                {"role": role, "identity": REDACTED, "unit": units.get(source)}
            )
    if "grid_intensity_source" in _mapping(data.get("factors")):
        # Current CO₂ samples are deliberately never cached; their source unit
        # cannot be obtained here without an extra measurement read.
        sources.append({"role": "grid_intensity", "identity": REDACTED, "unit": None})
    return sources


def _runtime_diagnostics(runtime: EntryRuntime | None) -> dict[str, object] | None:
    """Copy only bounded quality information from the already verified generation."""
    if runtime is None:
        return None
    state = runtime.state
    measurement = state.measurement
    ledger = state.ledger
    counters = dict(state.diagnostics)
    return {
        "status": runtime.status if runtime.status in _STATUSES else REDACTED,
        "available": runtime.available,
        "failed": runtime.failed,
        "phase": measurement.phase.value,
        "last_accepted_period_end": measurement.baseline.period_end.isoformat()
        if measurement.baseline is not None
        else None,
        "diagnostic_counters": {
            key: counters[key] for key in _DIAGNOSTIC_COUNTERS if key in counters
        },
        "ledger_present": ledger is not None,
        "ledger_quarantined": ledger == StorageLedger.quarantined(ledger.capacity)
        if ledger is not None
        else None,
    }


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,  # noqa: ARG001 - diagnostics must never fetch live sources
    entry: ConfigEntry[EntryRuntime],
) -> dict[str, object]:
    """Return minimal redacted context even when setup failed before runtime exists."""
    runtime: EntryRuntime | None = getattr(entry, "runtime_data", None)
    data = entry.data
    consumption = _mapping(data.get("consumption"))
    return {
        "configuration": {
            "topology": _choice(data.get("topology"), ("inverter", "smart_meter")),
            "consumption_mode": _choice(
                consumption.get("mode"), ("aggregate_shares", "separate_meters")
            ),
            "battery_configured": data["battery"] is not None
            if "battery" in data
            and (data["battery"] is None or isinstance(data["battery"], Mapping))
            else None,
        },
        "versions": {
            "config_entry": {"major": entry.version, "minor": entry.minor_version},
            "supported_manifest": {
                "major": MANIFEST_SCHEMA_VERSION,
                "minor": MANIFEST_MINOR_VERSION,
            },
            "supported_generation": {
                "major": GENERATION_SCHEMA_VERSION,
                "minor": GENERATION_MINOR_VERSION,
            },
        },
        "entry_state": entry.state.value,
        "sources": _sources(data, runtime.state if runtime is not None else None),
        "runtime": _runtime_diagnostics(runtime),
    }
