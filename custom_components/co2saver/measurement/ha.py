# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Home Assistant boundary for cumulative energy observations."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from homeassistant.components.sensor import (
    ATTR_STATE_CLASS,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_utc_time_change
from homeassistant.util import dt as dt_util

from custom_components.co2saver.const import ATTR_CO2SAVER_PERIOD_END
from custom_components.co2saver.domain import DomainValidationError, Energy
from custom_components.co2saver.measurement.models import (
    EnergyCounterSample,
    EnergyObservation,
    EnergySourceIdentity,
    EnergyUnit,
    InvalidEnergySample,
    MeasurementRejectionReason,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


type AtomicObservationConsumer = Callable[
    [tuple[EnergyObservation, ...], datetime], Awaitable[None]
]

_VALID_STATE_CLASSES = frozenset(
    (SensorStateClass.TOTAL, SensorStateClass.TOTAL_INCREASING)
)


class EnergyObservationReader(Protocol):
    """Synchronous source of one immutable energy-observation vector."""

    def read(self) -> tuple[EnergyObservation, ...]:
        """Read all configured sources exactly once."""
        ...


def _invalid(
    source: EnergySourceIdentity,
    reason: MeasurementRejectionReason,
) -> InvalidEnergySample:
    """Create one typed, fail-closed adapter observation."""
    return InvalidEnergySample(source=source, reason=reason)


def _as_utc_datetime(value: object) -> datetime | None:
    """Parse one timezone-aware timestamp and copy it into canonical UTC."""
    parsed: datetime | None
    if isinstance(value, datetime):
        parsed = value
    elif type(value) is str:
        parsed = dt_util.parse_datetime(value)
    else:
        return None
    if parsed is None or parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    try:
        return parsed.astimezone(UTC)
    except OverflowError, ValueError:
        return None


def _normalized_energy(value: str, unit: EnergyUnit) -> Energy | None:
    """Convert one HA state string to exact non-negative kWh."""
    try:
        if unit is EnergyUnit.WATT_HOUR:
            return Energy.from_wh(value)
        if unit is EnergyUnit.KILOWATT_HOUR:
            return Energy.from_kwh(value)
        return Energy.from_mwh(value)
    except DomainValidationError:
        return None


class HomeAssistantEnergyReader:
    """Synchronously copy cumulative counters selected by registry UUID."""

    def __init__(
        self,
        hass: HomeAssistant,
        sources: tuple[EnergySourceIdentity, ...],
    ) -> None:
        """Initialize a reader for an immutable configured source set."""
        configured_sources = tuple(sources)
        if not configured_sources:
            message = "at least one energy source is required"
            raise ValueError(message)
        roles = [source.role for source in configured_sources]
        if len(roles) != len(set(roles)):
            message = "energy source roles must be unique"
            raise ValueError(message)
        registry_ids = [source.registry_id for source in configured_sources]
        if len(registry_ids) != len(set(registry_ids)):
            message = "energy source registry ids must be unique"
            raise ValueError(message)
        self._hass = hass
        self._sources = configured_sources

    @property
    def sources(self) -> tuple[EnergySourceIdentity, ...]:
        """Return the immutable configured source order."""
        return self._sources

    def read(self) -> tuple[EnergyObservation, ...]:
        """Resolve and copy every source without crossing an await boundary."""
        registry = er.async_get(self._hass)
        return tuple(self._read_source(registry, source) for source in self._sources)

    def _read_source(  # noqa: C901, PLR0911
        self,
        registry: er.EntityRegistry,
        source: EnergySourceIdentity,
    ) -> EnergyObservation:
        """Copy and validate one current cumulative-counter state."""
        registry_entry = registry.async_get(source.registry_id)
        if (
            registry_entry is None
            or registry_entry.id != source.registry_id
            or registry_entry.domain != SENSOR_DOMAIN
        ):
            return _invalid(
                source,
                MeasurementRejectionReason.SOURCE_BINDING_MISMATCH,
            )

        state = self._hass.states.get(registry_entry.entity_id)
        if state is None:
            return _invalid(source, MeasurementRejectionReason.SOURCE_MISSING)

        # Copy every mutable-State scalar before doing further work. In particular,
        # a repeated HA report may mutate State.last_reported in place.
        state_value = state.state
        device_class = state.attributes.get(ATTR_DEVICE_CLASS)
        state_class = state.attributes.get(ATTR_STATE_CLASS)
        unit_value = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        period_end_value = state.attributes.get(ATTR_CO2SAVER_PERIOD_END)
        last_reported_value = state.last_reported

        if state_value in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return _invalid(source, MeasurementRejectionReason.SOURCE_UNAVAILABLE)
        if device_class != SensorDeviceClass.ENERGY:
            return _invalid(source, MeasurementRejectionReason.INVALID_DEVICE_CLASS)
        if not isinstance(state_class, str) or state_class not in _VALID_STATE_CLASSES:
            return _invalid(source, MeasurementRejectionReason.INVALID_STATE_CLASS)
        if not isinstance(unit_value, str):
            return _invalid(source, MeasurementRejectionReason.INVALID_UNIT)
        try:
            source_unit = EnergyUnit(unit_value)
        except TypeError, ValueError:
            return _invalid(source, MeasurementRejectionReason.INVALID_UNIT)

        cumulative = _normalized_energy(state_value, source_unit)
        if cumulative is None:
            return _invalid(source, MeasurementRejectionReason.INVALID_VALUE)

        period_end = _as_utc_datetime(period_end_value)
        if period_end is None:
            return _invalid(source, MeasurementRejectionReason.INVALID_PERIOD_END)
        last_reported = _as_utc_datetime(last_reported_value)
        if last_reported is None:
            return _invalid(source, MeasurementRejectionReason.INVALID_LAST_REPORTED)

        return EnergyCounterSample(
            source=source,
            cumulative=cumulative,
            source_unit=source_unit,
            period_end=period_end,
            last_reported=last_reported,
        )


class UtcMinuteRunner:
    """Serialize UTC-minute reads with an injected atomic consumer."""

    def __init__(
        self,
        hass: HomeAssistant,
        reader: EnergyObservationReader,
        consumer: AtomicObservationConsumer,
    ) -> None:
        """Initialize a stopped runner without reading any source."""
        self._hass = hass
        self._reader = reader
        self._consumer = consumer
        self._lock = asyncio.Lock()
        self._cancel_timer: Callable[[], None] | None = None
        self._stopped = False

    def start(self) -> None:
        """Register exactly one UTC-minute timer without an immediate read."""
        if self._cancel_timer is not None or self._stopped:
            message = "UTC minute runner cannot be started more than once"
            raise RuntimeError(message)
        self._cancel_timer = async_track_utc_time_change(
            self._hass,
            self._async_handle_tick,
            second=0,
        )

    def request_stop(self) -> None:
        """Prevent further reads immediately, including from inside a consumer."""
        cancel_timer = self._cancel_timer
        if cancel_timer is not None:
            cancel_timer()
            self._cancel_timer = None
        self._stopped = True

    async def async_stop(self) -> None:
        """Remove the timer first, then await any in-flight atomic commit."""
        self.request_stop()
        async with self._lock:
            return

    async def _async_handle_tick(self, observed_at: datetime) -> None:
        """Read and commit one current vector unless shutdown has begun."""
        if self._stopped or self._lock.locked():
            return
        async with self._lock:
            if self._stopped:
                return
            observations = self._reader.read()
            if self._stopped:
                return
            await self._consumer(observations, observed_at.astimezone(UTC))


__all__ = (
    "AtomicObservationConsumer",
    "EnergyObservationReader",
    "HomeAssistantEnergyReader",
    "UtcMinuteRunner",
)
