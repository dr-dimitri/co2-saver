# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Staged UI configuration for CO2 Saver's cumulative energy sources."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.core import valid_entity_id
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    BooleanSelector,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .config_sources import (
    energy_entity_selector,
    source_fields,
    validate_source_selection,
)
from .const import DOMAIN

if TYPE_CHECKING:
    from collections.abc import Mapping

_TOPOLOGIES = ("inverter", "smart_meter")


class Co2SaverConfigFlow(ConfigFlow, domain=DOMAIN):
    """Collect an isolated draft without committing intermediate configuration."""

    VERSION = 1

    def __init__(self) -> None:
        """Keep all incomplete configuration private to this flow."""
        self._draft: dict[str, Any] = {}

    @property
    def configuration_draft(self) -> dict[str, Any]:
        """Return a detached, serializable snapshot of the staged configuration."""
        return deepcopy(self._draft)

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
        registry = er.async_get(self.hass)
        for role in source_fields(topology):
            value = suggestions.get(role)
            if isinstance(value, str) and (entry := registry.async_get(value)):
                suggestions[role] = entry.entity_id
        selector = energy_entity_selector(self.hass)
        # Keep the submitted selection retryable if its semantics changed after
        # the first form. The backend still validates every role on every submit.
        selected_entities = {
            value
            for role in source_fields(topology)
            if isinstance(value := suggestions.get(role), str)
            and valid_entity_id(value)
        }
        selector.config["include_entities"] = sorted(
            set(selector.config.get("include_entities", ())) | selected_entities
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
        """Expose the next flow boundary; issue #6 supplies its configuration."""
        return self.async_show_form(
            step_id="storage",
            data_schema=vol.Schema({}),
            errors={"base": "setup_incomplete"} if user_input is not None else {},
            last_step=False,
        )
