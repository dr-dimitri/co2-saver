# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Home Assistant sensors presenting only the verified accounting generation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import DOMAIN

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from fractions import Fraction

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .persistence import CumulativeTotals, GenerationState
    from .runtime import Co2SaverConfigEntry

_KILOGRAMS_CO2_EQUIVALENT = "kgCO₂e"
_CONSUMER_METRICS = frozenset({"net_savings", "direct_pv_energy", "storage_pv_energy"})


@dataclass(frozen=True, kw_only=True)
class Co2SaverSensorDescription(SensorEntityDescription):
    """Describe an exact projection into the sensor's native presentation unit."""

    value_fn: Callable[[GenerationState, CumulativeTotals], Fraction]


SENSOR_DESCRIPTIONS: tuple[Co2SaverSensorDescription, ...] = (
    Co2SaverSensorDescription(
        key="net_savings",
        native_unit_of_measurement=_KILOGRAMS_CO2_EQUIVALENT,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda _state, totals: (
            (totals.direct_net_g + totals.storage_net_g) / 1000
        ),
    ),
    Co2SaverSensorDescription(
        key="direct_net_savings",
        native_unit_of_measurement=_KILOGRAMS_CO2_EQUIVALENT,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda _state, totals: totals.direct_net_g / 1000,
    ),
    Co2SaverSensorDescription(
        key="storage_net_savings",
        native_unit_of_measurement=_KILOGRAMS_CO2_EQUIVALENT,
        state_class=SensorStateClass.TOTAL,
        value_fn=lambda _state, totals: totals.storage_net_g / 1000,
    ),
    Co2SaverSensorDescription(
        key="gross_avoided",
        native_unit_of_measurement=_KILOGRAMS_CO2_EQUIVALENT,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda _state, totals: (
            (totals.direct_gross_g + totals.storage_gross_g) / 1000
        ),
    ),
    Co2SaverSensorDescription(
        key="pv_lifecycle",
        native_unit_of_measurement=_KILOGRAMS_CO2_EQUIVALENT,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda _state, totals: (
            (totals.direct_pv_burden_g + totals.storage_pv_burden_g) / 1000
        ),
    ),
    Co2SaverSensorDescription(
        key="battery_lifecycle",
        native_unit_of_measurement=_KILOGRAMS_CO2_EQUIVALENT,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda _state, totals: totals.storage_burden_g / 1000,
    ),
    Co2SaverSensorDescription(
        key="direct_pv_energy",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda _state, totals: totals.direct_pv_kwh,
    ),
    Co2SaverSensorDescription(
        key="storage_pv_energy",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda _state, totals: totals.storage_pv_kwh,
    ),
    Co2SaverSensorDescription(
        key="unassigned_direct_energy",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda state, _totals: state.unassigned_direct_kwh,
    ),
    Co2SaverSensorDescription(
        key="unassigned_storage_energy",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda state, _totals: state.unassigned_storage_kwh,
    ),
    Co2SaverSensorDescription(
        key="unvalued_direct_energy",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda _state, totals: totals.unvalued_direct_kwh,
    ),
    Co2SaverSensorDescription(
        key="unvalued_storage_energy",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda _state, totals: totals.unvalued_storage_kwh,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001 - entities receive hass during platform setup
    entry: Co2SaverConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create stable system and current-consumer series from a verified runtime."""
    entities = [
        Co2SaverSensor(entry, description) for description in SENSOR_DESCRIPTIONS
    ]
    consumption = cast("Mapping[str, object]", entry.data["consumption"])
    household_id = cast("str", consumption["household_id"])
    rows = cast("list[dict[str, str]]", consumption["consumers"])
    for consumer_id, consumer_name in (
        (household_id, None),
        *((row["consumer_id"], row["name"]) for row in rows),
    ):
        entities.extend(
            Co2SaverSensor(
                entry,
                description,
                consumer_id=consumer_id,
                consumer_name=consumer_name,
            )
            for description in SENSOR_DESCRIPTIONS
            if description.key in _CONSUMER_METRICS
        )
    async_add_entities(entities)


class Co2SaverSensor(SensorEntity):
    """Read-only projection without an independent restore state or polling loop."""

    entity_description: Co2SaverSensorDescription
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_suggested_display_precision = 3

    def __init__(
        self,
        entry: Co2SaverConfigEntry,
        description: Co2SaverSensorDescription,
        *,
        consumer_id: str | None = None,
        consumer_name: str | None = None,
    ) -> None:
        """Bind stable entry/consumer identities independently of display names."""
        self.entity_description = description
        self._runtime = entry.runtime_data
        self._consumer_id = consumer_id
        identity = entry.entry_id
        translation_prefix = ""
        if consumer_id is not None:
            identity = f"{identity}:consumer:{consumer_id}"
            translation_prefix = "household_" if consumer_name is None else "consumer_"
        self._attr_unique_id = f"{identity}:{description.key}"
        self._attr_translation_key = f"{translation_prefix}{description.key}"
        if consumer_name is not None:
            self._attr_translation_placeholders = {"consumer_name": consumer_name}
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title,
            entry_type=DeviceEntryType.SERVICE,
        )
        self._read_verified_state()

    @callback
    def _read_verified_state(self) -> None:
        """Cache one immutable generation, converting to float only for HA display."""
        state = self._runtime.state
        totals = (
            state.totals
            if self._consumer_id is None
            else dict(state.consumer_totals)[self._consumer_id]
        )
        self._attr_native_value = float(self.entity_description.value_fn(state, totals))
        self._attr_available = self._runtime.available
        self._attr_extra_state_attributes = {"accounting_status": self._runtime.status}
        self._attr_last_reset = (
            state.repair_reset_at
            if self.entity_description.state_class is SensorStateClass.TOTAL
            else None
        )

    @callback
    def _async_runtime_updated(self) -> None:
        """Publish only after the runtime has verified a complete commit or status."""
        self._read_verified_state()
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Follow committed runtime changes and release the listener on removal."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                self._runtime.update_signal,
                self._async_runtime_updated,
            )
        )
        self._read_verified_state()
