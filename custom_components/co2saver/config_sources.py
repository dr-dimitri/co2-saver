# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Validate the generation and grid sources selected by the config flow."""

from __future__ import annotations

from typing import TYPE_CHECKING, Never, TypedDict

from homeassistant.components.sensor import (
    ATTR_STATE_CLASS,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.const import UnitOfEnergy
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    EntityWithDeviceFilterSelectorConfig,
)
from homeassistant.util import dt as dt_util

from custom_components.co2saver.measurement.ha import HomeAssistantEnergyReader
from custom_components.co2saver.measurement.models import (
    EnergyCounterSample,
    EnergyObservation,
    EnergySourceIdentity,
    InvalidEnergySample,
    MeasurementFault,
    MeasurementPhase,
    MeasurementPipelineState,
    MeasurementRejectionReason,
    RawEnergyDeltaBatch,
)
from custom_components.co2saver.measurement.pipeline import advance_measurements

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_registry import RegistryEntry


_CONFIRMATION_FIELD = "synchronous_sources_confirmed"
_INVERTER = "inverter"
_SMART_METER = "smart_meter"
_PV_PLAUSIBILITY = "pv_plausibility"
_SOURCE_FIELDS: dict[str, tuple[str, ...]] = {
    _INVERTER: ("pv_generation", "grid_import", "grid_export"),
    _SMART_METER: ("grid_import", "grid_export", _PV_PLAUSIBILITY),
}
_SUPPORTED_UNITS = (
    UnitOfEnergy.WATT_HOUR.value,
    UnitOfEnergy.KILO_WATT_HOUR.value,
    UnitOfEnergy.MEGA_WATT_HOUR.value,
)
_SUPPORTED_STATE_CLASSES = frozenset(
    (SensorStateClass.TOTAL, SensorStateClass.TOTAL_INCREASING)
)
_SOURCE_FAULT_ERRORS: dict[MeasurementRejectionReason, str] = {
    MeasurementRejectionReason.SOURCE_MISSING: "source_missing",
    MeasurementRejectionReason.SOURCE_UNAVAILABLE: "source_unavailable",
    MeasurementRejectionReason.INVALID_VALUE: "invalid_value",
    MeasurementRejectionReason.INVALID_UNIT: "invalid_unit",
    MeasurementRejectionReason.INVALID_DEVICE_CLASS: "invalid_device_class",
    MeasurementRejectionReason.INVALID_STATE_CLASS: "invalid_state_class",
    MeasurementRejectionReason.INVALID_PERIOD_END: "invalid_period_end",
    MeasurementRejectionReason.INVALID_LAST_REPORTED: "invalid_last_reported",
    MeasurementRejectionReason.SOURCE_BINDING_MISMATCH: "source_not_registered",
    MeasurementRejectionReason.FUTURE_PERIOD_END: "future_period_end",
    MeasurementRejectionReason.FUTURE_LAST_REPORTED: "future_last_reported",
    MeasurementRejectionReason.PERIOD_AFTER_PUBLICATION: ("period_after_publication"),
    MeasurementRejectionReason.PUBLICATION_DELAY: "publication_delay",
    MeasurementRejectionReason.NEW_SAMPLE_STALE: "source_stale",
    MeasurementRejectionReason.CANDIDATE_STALE: "source_stale",
}
_VECTOR_FAULT_ERRORS: dict[MeasurementRejectionReason, str] = {
    MeasurementRejectionReason.CANDIDATE_PERIOD_MISMATCH: ("sources_not_synchronized"),
    MeasurementRejectionReason.PUBLICATION_SKEW: "publication_skew",
}


class SourceDraft(TypedDict):
    """Serializable source-selection result passed to the next flow step."""

    topology: str
    sources: dict[str, str]
    plant_key: str
    synchronous_sources_confirmed: bool


def source_fields(topology: str) -> tuple[str, ...]:
    """Return the ordered source fields for one supported topology."""
    if type(topology) is not str or topology not in _SOURCE_FIELDS:
        message = f"unsupported source topology: {topology!r}"
        raise ValueError(message)
    return _SOURCE_FIELDS[topology]


def _unexpected_interval_assembly(_batch: RawEnergyDeltaBatch) -> Never:
    """Guard that source validation can never account an energy interval."""
    message = "source validation must not assemble or account an interval"
    raise RuntimeError(message)


def _fault_errors(
    fault: MeasurementFault,
    roles: tuple[str, ...],
) -> dict[str, str]:
    """Map one #4 reader or timeline fault to concrete flow fields."""
    if fault.source is not None:
        return {
            fault.source.role: _SOURCE_FAULT_ERRORS.get(
                fault.reason,
                "invalid_source",
            )
        }
    error = _VECTOR_FAULT_ERRORS.get(fault.reason, "invalid_source_vector")
    return dict.fromkeys(roles, error)


def _validate_current_vector(
    sources: tuple[EnergySourceIdentity, ...],
    observations: tuple[EnergyObservation, ...],
    observed_at: datetime,
) -> dict[str, str]:
    """Apply the #4 first-baseline time contract without retaining state."""
    samples = tuple(
        observation
        for observation in observations
        if isinstance(observation, EnergyCounterSample)
    )
    if len(samples) != len(sources):
        for observation in observations:
            if isinstance(observation, InvalidEnergySample):
                return _fault_errors(
                    MeasurementFault(observation.reason, observation.source),
                    tuple(source.role for source in sources),
                )
        return dict.fromkeys(
            (source.role for source in sources),
            "invalid_source_vector",
        )

    initial = MeasurementPipelineState.initial(
        sources,
        min(sample.period_end for sample in samples),
    )
    transition = advance_measurements(
        initial,
        observations,
        observed_at,
        assemble_interval=_unexpected_interval_assembly,
    )
    if transition.fault is not None:
        return _fault_errors(
            transition.fault,
            tuple(source.role for source in sources),
        )
    if transition.state.phase is not MeasurementPhase.ACTIVE:
        return dict.fromkeys(
            (source.role for source in sources),
            "invalid_source_vector",
        )
    return {}


def _is_currently_eligible(
    hass: HomeAssistant,
    entry: RegistryEntry,
) -> bool:
    """Supplement the selector's missing state-class filter."""
    if entry.domain != SENSOR_DOMAIN or entry.disabled:
        return False
    state = hass.states.get(entry.entity_id)
    return (
        state is not None
        and isinstance(state_class := state.attributes.get(ATTR_STATE_CLASS), str)
        and state_class in _SUPPORTED_STATE_CLASSES
    )


def energy_entity_selector(hass: HomeAssistant) -> EntitySelector:
    """Build a state-class-aware energy selector from current eligible sources."""
    registry = er.async_get(hass)
    include_entities = sorted(
        entry.entity_id
        for entry in registry.entities.values()
        if _is_currently_eligible(hass, entry)
    )
    return EntitySelector(
        EntitySelectorConfig(
            filter=EntityWithDeviceFilterSelectorConfig(
                domain=SENSOR_DOMAIN,
                device_class=SensorDeviceClass.ENERGY.value,
                unit_of_measurement=list(_SUPPORTED_UNITS),
            ),
            include_entities=include_entities,
        )
    )


def _shape_errors(
    topology: str,
    user_input: Mapping[str, object],
) -> dict[str, str]:
    """Reject missing, malformed, and unexpected flow input fields."""
    fields = source_fields(topology)
    allowed = {*fields, _CONFIRMATION_FIELD}
    errors: dict[str, str] = {}
    for field in user_input:
        if field not in allowed:
            errors[field] = "unexpected_field"

    for field in fields:
        value = user_input.get(field)
        if field == _PV_PLAUSIBILITY and (value is None or value == ""):
            continue
        if value is None or value == "":
            errors[field] = "required"
        elif type(value) is not str or value != value.strip():
            errors[field] = "invalid_selection"

    if user_input.get(_CONFIRMATION_FIELD) is not True:
        errors[_CONFIRMATION_FIELD] = "confirmation_required"
    return errors


def validate_energy_sources(
    hass: HomeAssistant,
    selections: Mapping[str, object],
) -> tuple[dict[str, str] | None, dict[str, str]]:
    """Validate one complete energy vector and return registry identities."""
    if not selections:
        return None, {"base": "invalid_source_vector"}

    registry = er.async_get(hass)
    resolved: dict[str, str] = {}
    errors: dict[str, str] = {}
    for role, value in selections.items():
        registry_id, error = _resolve_registry_source(registry, value)
        if error is not None:
            errors[role] = error
        elif registry_id is not None:
            resolved[role] = registry_id

    errors.update(_duplicate_source_errors(resolved))
    if errors:
        return None, errors

    sources = tuple(
        EnergySourceIdentity(role=role, registry_id=registry_id)
        for role, registry_id in resolved.items()
    )
    observations = HomeAssistantEnergyReader(hass, sources).read()
    errors = _validate_current_vector(sources, observations, dt_util.utcnow())
    if errors:
        return None, errors
    return resolved, {}


def _resolve_registry_source(
    registry: er.EntityRegistry,
    value: object,
) -> tuple[str | None, str | None]:
    """Resolve one entity ID or UUID and return its field error when invalid."""
    if value is None or value == "":
        return None, "required"
    if type(value) is not str or value != value.strip():
        return None, "invalid_selection"
    entry = registry.async_get(value)
    if entry is None:
        return None, "source_not_registered"
    if entry.domain != SENSOR_DOMAIN:
        return None, "invalid_domain"
    if entry.disabled:
        return None, "source_disabled"
    return entry.id, None


def _duplicate_source_errors(resolved: Mapping[str, str]) -> dict[str, str]:
    """Mark every role sharing ownership of one physical registry entity."""
    roles_by_registry_id: dict[str, list[str]] = {}
    for role, registry_id in resolved.items():
        roles_by_registry_id.setdefault(registry_id, []).append(role)
    return {
        role: "duplicate_source"
        for duplicate_roles in roles_by_registry_id.values()
        if len(duplicate_roles) > 1
        for role in duplicate_roles
    }


def validate_source_selection(
    hass: HomeAssistant,
    topology: str,
    user_input: Mapping[str, object],
) -> tuple[SourceDraft | None, dict[str, str]]:
    """Validate and canonicalize one side-effect-free source flow submission."""
    if type(topology) is not str or topology not in _SOURCE_FIELDS:
        return None, {"base": "invalid_topology"}

    errors = _shape_errors(topology, user_input)
    if errors:
        return None, errors

    selected = {
        role: user_input[role]
        for role in source_fields(topology)
        if role != _PV_PLAUSIBILITY or user_input.get(role) not in (None, "")
    }
    resolved, errors = validate_energy_sources(hass, selected)
    if errors:
        return None, errors
    if resolved is None:  # pragma: no cover - success contract of shared validator
        return None, {"base": "invalid_source_vector"}

    grid_ids = sorted((resolved["grid_import"], resolved["grid_export"]))
    return (
        SourceDraft(
            topology=topology,
            sources=dict(resolved),
            plant_key=f"grid:{grid_ids[0]}:{grid_ids[1]}",
            synchronous_sources_confirmed=True,
        ),
        {},
    )


__all__ = (
    "SourceDraft",
    "energy_entity_selector",
    "source_fields",
    "validate_energy_sources",
    "validate_source_selection",
)
