# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Complete factor forms and commit fully validated configuration drafts."""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import voluptuous as vol
from homeassistant.config_entries import ConfigEntryBaseFlow, ConfigFlowResult
from homeassistant.core import callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
)

from .bootstrap import async_reserve_bootstrap, manifest_lock
from .config_factors import validate_factor_selection
from .config_plan import canonical_plan, validate_current_plan
from .flow_commit import (
    CreateFinalization,
    release_commit,
    reservations,
    reserve_commit,
)
from .measurement.storage import VerifiedAtomicStoreError

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry


class FactorFlowSteps(ConfigEntryBaseFlow):
    """Share the final form between initial setup, reconfigure, and options."""

    _draft: dict[str, Any]
    _original_entry_data: dict[str, Any] | None = None
    _create_finalization: CreateFinalization | None = None

    @callback
    def async_remove(self) -> None:
        """Stop an unfinished create before releasing its plant reservation."""
        super().async_remove()
        if self._create_finalization is not None:
            self._create_finalization.flow_removed()

    def _discard_create_tracking(self) -> None:
        """Drop task callbacks after an incomplete step releases its reservation."""
        if self._create_finalization is not None:
            self._create_finalization.discard()
            self._create_finalization = None

    def _configuration_entry(self) -> ConfigEntry | None:
        """Return an existing owner for an edit, or None for a new plant."""
        return None

    def _finish_configuration(
        self, data: dict[str, Any], entry: ConfigEntry | None
    ) -> ConfigFlowResult:
        """Use the concrete ConfigFlow or OptionsFlow completion API."""
        raise NotImplementedError

    async def _async_commit_configuration(self) -> ConfigFlowResult:
        """Revalidate and serialize the final authoritative configuration write."""
        entry = self._configuration_entry()
        token = uuid4().hex
        async with manifest_lock(self.hass):
            errors = validate_current_plan(self.hass, self._draft)
            if errors:
                return self._factor_form(None, errors)
            data = canonical_plan(self._draft)
            if entry is not None and dict(entry.data) != self._original_entry_data:
                reason = "configuration_changed"
                raise AbortFlow(reason)
            reserve_commit(self.hass, str(data["plant_key"]), token, entry)
            keep_create = False
            try:
                if entry is None:
                    finalization = CreateFinalization(self.hass, token)
                    self._create_finalization = finalization
                    storage_id = await async_reserve_bootstrap(self.hass)
                    finalization.storage_id = storage_id
                    if errors := validate_current_plan(self.hass, self._draft):
                        return self._factor_form(None, errors)
                else:
                    storage_id = entry.data["storage_id"]
                data["storage_id"] = storage_id
                result = self._finish_configuration(data, entry)
                if entry is None:
                    reservations(self.hass).creates[storage_id] = token
                    keep_create = True
            except OSError, ValueError, VerifiedAtomicStoreError:
                return self._factor_form(None, {"base": "storage_failed"})
            else:
                return result
            finally:
                if not keep_create:
                    release_commit(self.hass, token)
                    self._discard_create_tracking()

    async def async_step_factors(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate current intensity and explicitly entered lifecycle factors."""
        errors: dict[str, str] = {}
        if user_input is not None:
            factors, errors = validate_factor_selection(
                self.hass, self._draft.get("battery") is not None, user_input
            )
            if factors is not None:
                self._draft["factors"] = factors
                return await self._async_commit_configuration()
        return self._factor_form(user_input, errors)

    def _factor_form(
        self, user_input: dict[str, Any] | None, errors: dict[str, str]
    ) -> ConfigFlowResult:
        """Show units and exact text without implicit manufacturing defaults."""
        suggestions = deepcopy(
            user_input
            if user_input is not None
            else self._draft.get("factors", {"grid_max_age_minutes": 60})
        )
        registry = er.async_get(self.hass)
        source = suggestions.get("grid_intensity_source")
        if isinstance(source, str) and (registered := registry.async_get(source)):
            suggestions["grid_intensity_source"] = registered.entity_id
        fields: dict[vol.Marker, object] = {
            vol.Required("grid_intensity_source"): EntitySelector(
                EntitySelectorConfig(domain="sensor")
            ),
            vol.Required("grid_max_age_minutes"): NumberSelector(
                NumberSelectorConfig(
                    min=1,
                    max=1440,
                    step=1,
                    mode=NumberSelectorMode.BOX,
                    unit_of_measurement="min",
                )
            ),
            vol.Required("pv_factor"): TextSelector(
                TextSelectorConfig(suffix="g CO₂e/kWh")
            ),
        }
        if self._draft.get("battery") is not None:
            fields[vol.Required("battery_factor")] = TextSelector(
                TextSelectorConfig(suffix="g CO₂e/kWh")
            )
        return self.async_show_form(
            step_id="factors",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(fields), suggestions
            ),
            errors=errors,
            last_step=True,
        )
