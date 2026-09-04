# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Staged UI configuration for CO2 Saver's cumulative energy sources."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    BooleanSelector,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .config_sources import (
    source_fields,
    validate_source_selection,
)
from .config_storage import validate_storage_selection
from .const import DOMAIN
from .consumer_flow import ConsumerFlowSteps

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.config_entries import ConfigEntry

_TOPOLOGIES = ("inverter", "smart_meter")
_BATTERY_ROLES = ("battery_charge", "battery_discharge")
_BATTERY_IDENTITIES = ("same_physical_battery", "physical_battery_replaced")
_BATTERY_CHOICES = ("without_battery", "with_battery")


class Co2SaverConfigFlow(ConsumerFlowSteps, ConfigFlow, domain=DOMAIN):
    """Collect an isolated draft without committing intermediate configuration."""

    VERSION = 1

    def __init__(self) -> None:
        """Keep all incomplete configuration private to this flow."""
        super().__init__()
        self._original_battery: dict[str, Any] | None = None
        self._new_battery_id: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Expose the same staged consumer editor without committing options."""
        _ = config_entry
        return Co2SaverOptionsFlow()

    @property
    def battery_change_pending(self) -> bool:
        """Report a staged difference, never an authoritative persisted flag."""
        return (
            "battery" in self._draft
            and self._draft["battery"] != self._original_battery
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose the authoritative PV measurement topology."""
        return await self._async_topology_step("user", user_input)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Stage source changes while retaining the original entry and locator."""
        if not self._draft:
            self._draft = deepcopy(dict(self._get_reconfigure_entry().data))
            self._original_battery = deepcopy(self._draft.get("battery"))
        return await self._async_topology_step("reconfigure", user_input)

    async def _async_topology_step(
        self, step_id: str, user_input: Mapping[str, object] | None
    ) -> ConfigFlowResult:
        """Share topology validation between setup and reconfiguration."""
        errors: dict[str, str] = {}
        if user_input is not None:
            topology = user_input.get("topology")
            if topology not in _TOPOLOGIES:
                errors["topology"] = "invalid_topology"
            else:
                self._draft["topology"] = topology
                return await self.async_step_sources()
        schema = vol.Schema(
            {
                vol.Required("topology"): SelectSelector(
                    SelectSelectorConfig(
                        options=list(_TOPOLOGIES),
                        mode=SelectSelectorMode.LIST,
                        translation_key="topology",
                    )
                )
            }
        )
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(schema, self._draft),
            errors=errors,
            last_step=False,
        )

    async def async_step_sources(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Resolve and validate the PV/grid roles, then stage their identities."""
        topology = self._draft["topology"]
        errors: dict[str, str] = {}
        if user_input is not None:
            draft, errors = validate_source_selection(self.hass, topology, user_input)
            if draft is not None:
                # The final commit repeats this check under the reservations lock
                # in issue #8. Intermediate drafts reserve or mutate nothing.
                self._async_abort_entries_match({"plant_key": draft["plant_key"]})
                self._draft.update(draft)
                return await self.async_step_storage()
        suggestions = dict(
            user_input if user_input is not None else self._source_suggestions(topology)
        )
        selector = self._energy_selector_with_suggestions(
            source_fields(topology), suggestions
        )
        fields: dict[vol.Marker, object] = {
            (
                vol.Optional(role) if role == "pv_plausibility" else vol.Required(role)
            ): selector
            for role in source_fields(topology)
        }
        fields[vol.Required("synchronous_sources_confirmed", default=False)] = (
            BooleanSelector()
        )
        return self.async_show_form(
            step_id="sources",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(fields), suggestions
            ),
            errors=errors,
            last_step=False,
        )

    def _source_suggestions(self, topology: str) -> dict[str, object]:
        """Resolve old registry identities to current UI names, never persist them."""
        registry = er.async_get(self.hass)
        sources = self._draft.get("sources", {})
        suggestions: dict[str, object] = {}
        for role in source_fields(topology):
            registry_id = sources.get(role)
            if isinstance(registry_id, str) and (
                entry := registry.async_get(registry_id)
            ):
                suggestions[role] = entry.entity_id
        # Every source edit requires the physical measurement contract to be
        # confirmed anew, even when the previously stored selection was valid.
        return suggestions

    async def async_step_storage(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose whether this plant has a battery, without silently assuming one."""
        errors: dict[str, str] = {}
        if user_input is not None:
            present = user_input.get("battery_present")
            if present not in _BATTERY_CHOICES:
                errors["battery_present"] = "invalid_battery_choice"
            elif present == "with_battery":
                return await self.async_step_storage_sources()
            else:
                self._draft["battery"] = None
                return await self.async_step_consumers()
        suggestions = (
            {
                "battery_present": (
                    "with_battery"
                    if self._draft["battery"] is not None
                    else "without_battery"
                )
            }
            if "battery" in self._draft
            else {}
        )
        return self.async_show_form(
            step_id="storage",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required("battery_present"): SelectSelector(
                            SelectSelectorConfig(
                                options=list(_BATTERY_CHOICES),
                                translation_key="battery_present",
                                mode=SelectSelectorMode.LIST,
                            )
                        )
                    }
                ),
                suggestions,
            ),
            errors=errors,
            last_step=False,
        )

    async def async_step_storage_sources(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate battery roles and exact parameters against the complete vector."""
        errors: dict[str, str] = {}
        if user_input is not None:
            values = dict(user_input)
            if values.pop("battery_sources_confirmed", None) is not True:
                errors["battery_sources_confirmed"] = "battery_confirmation_required"
            identity = values.pop("battery_identity", None)
            if (
                self._original_battery is not None
                and identity not in _BATTERY_IDENTITIES
            ):
                errors["battery_identity"] = "invalid_battery_identity"
            if self._original_battery is None and identity is not None:
                errors["base"] = "invalid_battery_identity"
            parameters, source_errors = validate_storage_selection(
                self.hass, self._draft["sources"], values
            )
            errors.update(source_errors)
            if parameters is not None and not errors:
                if (
                    self._original_battery is not None
                    and identity == "same_physical_battery"
                ):
                    battery_id = self._original_battery["battery_id"]
                else:
                    if self._new_battery_id is None:
                        self._new_battery_id = uuid4().hex
                    battery_id = self._new_battery_id
                self._draft["battery"] = {"battery_id": battery_id, **parameters}
                return await self.async_step_consumers()

        suggestions = (
            dict(user_input) if user_input is not None else self._storage_suggestions()
        )
        selector = self._energy_selector_with_suggestions(_BATTERY_ROLES, suggestions)
        fields: dict[vol.Marker, object] = {
            vol.Required(role): selector for role in _BATTERY_ROLES
        }
        fields.update(
            {
                vol.Required("usable_capacity_kwh"): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT, suffix="kWh")
                ),
                vol.Required("round_trip_efficiency_percent"): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT, suffix="%")
                ),
                vol.Required(
                    "battery_sources_confirmed", default=False
                ): BooleanSelector(),
            }
        )
        if self._original_battery is not None:
            fields[vol.Required("battery_identity")] = SelectSelector(
                SelectSelectorConfig(
                    options=list(_BATTERY_IDENTITIES),
                    translation_key="battery_identity",
                    mode=SelectSelectorMode.LIST,
                )
            )
        return self.async_show_form(
            step_id="storage_sources",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(fields), suggestions
            ),
            errors=errors,
            last_step=False,
        )

    def _storage_suggestions(self) -> dict[str, Any]:
        """Suggest exact prior values but require fresh direction/identity consent."""
        battery = self._draft.get("battery")
        if battery is None:
            return {"round_trip_efficiency_percent": "90"}
        # Appending an exponent shifts an exact decimal string without applying
        # the Decimal context's precision or binary floating-point rounding.
        percent = format(Decimal(battery["round_trip_efficiency"] + "e2"), "f")
        return {
            "battery_charge": battery["charge_source"],
            "battery_discharge": battery["discharge_source"],
            "usable_capacity_kwh": battery["usable_capacity_kwh"],
            "round_trip_efficiency_percent": percent,
        }


class Co2SaverOptionsFlow(ConsumerFlowSteps, OptionsFlow):
    """Prepare consumer/factor edits while preserving original data and options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Read the authoritative data only; never overlay opaque options."""
        if not self._draft:
            self._draft = deepcopy(dict(self.config_entry.data))
        return await self.async_step_consumers(user_input)
