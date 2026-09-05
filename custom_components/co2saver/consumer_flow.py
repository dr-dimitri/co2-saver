# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Shared consumer drafts with a final factor and persistence step."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import voluptuous as vol
from homeassistant.core import valid_entity_id
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    BooleanSelector,
    EntitySelector,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .config_consumers import (
    ConsumerCandidate,
    SeparateConsumerCandidate,
    validate_consumer_input,
    validate_consumption_selection,
)
from .config_sources import energy_entity_selector, validate_energy_sources
from .factor_flow import FactorFlowSteps

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigFlowResult

_CONSUMPTION_MODES = ("aggregate_shares", "separate_meters")
_CONSUMER_ACTIONS = ("add", "edit", "remove", "finish")


def _choice_selector(options: tuple[str, ...], translation_key: str) -> SelectSelector:
    """Create an explicit translated selection without a hidden default."""
    return SelectSelector(
        SelectSelectorConfig(
            options=list(options),
            translation_key=translation_key,
            mode=SelectSelectorMode.LIST,
        )
    )


def _consumer_suggestions(
    existing: dict[str, Any] | None, user_input: dict[str, Any] | None
) -> dict[str, Any]:
    """Preserve submitted text or display exact prior assignments."""
    if user_input is not None:
        return dict(user_input)
    if existing is None:
        return {}
    suggestions: dict[str, Any] = {"name": existing["name"]}
    if "source" in existing:
        suggestions["source"] = existing["source"]
    if "share" in existing:
        suggestions["share_percent"] = format(Decimal(existing["share"] + "e2"), "f")
    return suggestions


class ConsumerFlowSteps(FactorFlowSteps):
    """Edit consumers in a detached draft; never commit incomplete choices."""

    def __init__(self) -> None:
        """Initialize flow-local state shared by ConfigFlow and OptionsFlow."""
        super().__init__()
        self._draft: dict[str, Any] = {}
        self._consumer_plan: dict[str, Any] | None = None
        self._editing_consumer_id: str | None = None
        self._adding_consumer_id: str | None = None

    @property
    def configuration_draft(self) -> dict[str, Any]:
        """Return a detached JSON-compatible snapshot of accepted flow steps."""
        return deepcopy(self._draft)

    def _energy_selector_with_suggestions(
        self, roles: tuple[str, ...], suggestions: dict[str, Any]
    ) -> EntitySelector:
        """Keep corrected sources retryable, resolving UUIDs to current UI IDs."""
        registry = er.async_get(self.hass)
        for role in roles:
            value = suggestions.get(role)
            if isinstance(value, str) and (entry := registry.async_get(value)):
                suggestions[role] = entry.entity_id
        selector = energy_entity_selector(self.hass)
        selected_entities = {
            value
            for role in roles
            if isinstance(value := suggestions.get(role), str)
            and valid_entity_id(value)
        }
        selector.config["include_entities"] = sorted(
            set(selector.config.get("include_entities", ())) | selected_entities
        )
        return selector

    def _consumer_working_plan(self) -> dict[str, Any]:
        """Create one editable copy while preserving existing consumer identities."""
        if self._consumer_plan is None:
            previous = self._draft.get("consumption")
            self._consumer_plan = (
                deepcopy(previous)
                if previous is not None
                else {"household_id": uuid4().hex, "consumers": []}
            )
        return self._consumer_plan

    def _upstream_energy_sources(self) -> dict[str, str]:
        """Compose the accepted PV/grid and optional battery source roles."""
        sources = dict(self._draft["sources"])
        if battery := self._draft.get("battery"):
            sources.update(
                battery_charge=battery["charge_source"],
                battery_discharge=battery["discharge_source"],
            )
        return sources

    async def async_step_consumers(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose between an aggregate partition and non-overlapping meters."""
        plan = self._consumer_working_plan()
        errors: dict[str, str] = {}
        if user_input is not None:
            mode = user_input.get("mode")
            if mode not in _CONSUMPTION_MODES:
                errors["mode"] = "invalid_consumption_mode"
            else:
                if plan.get("mode") != mode:
                    # Identities and names survive, but a percentage cannot be
                    # reinterpreted as a separate meter (or vice versa).
                    plan.pop("household_source", None)
                    plan["consumers"] = [
                        {"consumer_id": row["consumer_id"], "name": row["name"]}
                        for row in plan["consumers"]
                    ]
                plan["mode"] = mode
                return await self._async_load_meter()
        return self.async_show_form(
            step_id="consumers",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required("mode"): _choice_selector(
                            _CONSUMPTION_MODES, "consumption_mode"
                        )
                    }
                ),
                plan,
            ),
            errors=errors,
            last_step=False,
        )

    async def async_step_aggregate_load(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select the aggregate meter, including all configured local consumers."""
        return await self._async_load_meter(user_input)

    async def async_step_separate_load(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select the household-only meter, excluding additional consumers."""
        return await self._async_load_meter(user_input)

    async def _async_load_meter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate the load boundary without discarding incomplete consumer edits."""
        plan = self._consumer_working_plan()
        step_id = (
            "aggregate_load" if plan["mode"] == "aggregate_shares" else "separate_load"
        )
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get("load_measurement_confirmed") is not True:
                errors["load_measurement_confirmed"] = "load_confirmation_required"
            sources: dict[str, object] = dict(self._upstream_energy_sources())
            sources["local_load" if step_id == "aggregate_load" else "household"] = (
                user_input.get("household_source")
            )
            resolved, source_errors = validate_energy_sources(self.hass, sources)
            role = "local_load" if step_id == "aggregate_load" else "household"
            if role in source_errors:
                errors["household_source"] = source_errors[role]
            if any(field != role for field in source_errors):
                errors["base"] = "invalid_source_vector"
            if resolved is not None and not errors:
                plan["household_source"] = resolved[role]
                return await self.async_step_consumer_menu()
        suggestions = (
            dict(user_input)
            if user_input is not None
            else {"household_source": plan["household_source"]}
            if "household_source" in plan
            else {}
        )
        selector = self._energy_selector_with_suggestions(
            ("household_source",), suggestions
        )
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required("household_source"): selector,
                        vol.Required(
                            "load_measurement_confirmed", default=False
                        ): BooleanSelector(),
                    }
                ),
                suggestions,
            ),
            errors=errors,
            last_step=False,
        )

    async def async_step_consumer_menu(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage additional consumers or validate the complete configuration."""
        plan = self._consumer_working_plan()
        errors: dict[str, str] = {}
        if user_input is not None:
            action = user_input.get("action")
            if action == "add":
                self._adding_consumer_id = uuid4().hex
                return await self.async_step_consumer_add()
            if action == "edit" and plan["consumers"]:
                return await self.async_step_consumer_edit()
            if action == "remove" and plan["consumers"]:
                return await self.async_step_consumer_remove()
            if action == "finish":
                validated, validation_errors = validate_consumption_selection(
                    self.hass, self._upstream_energy_sources(), plan
                )
                if validated is not None:
                    self._draft["consumption"] = validated
                    return await self.async_step_factors()
                errors["base"] = (
                    "shares_exceed_total"
                    if "shares_exceed_total" in validation_errors.values()
                    else validation_errors.get("base")
                    or next(iter(validation_errors.values()), "invalid_consumer_plan")
                )
            else:
                errors["action"] = "invalid_consumer_action"
        actions = _CONSUMER_ACTIONS if plan["consumers"] else ("add", "finish")
        summary = self._consumer_summary()
        return self.async_show_form(
            step_id="consumer_menu",
            data_schema=vol.Schema(
                {vol.Required("action"): _choice_selector(actions, "consumer_action")}
            ),
            errors=errors,
            description_placeholders={"consumer_summary": summary or "—"},
            last_step=False,
        )

    def _consumer_summary(self) -> str:
        """Show names and assignments so incomplete mode changes are visible."""
        registry = er.async_get(self.hass)
        rows = []
        for index, consumer in enumerate(self._consumer_working_plan()["consumers"]):
            assignment = "—"
            if "share" in consumer:
                assignment = format(Decimal(consumer["share"] + "e2"), "f") + " %"
            elif "source" in consumer:
                entry = registry.async_get(consumer["source"])
                assignment = entry.entity_id if entry else consumer["source"]
            rows.append(f"{index + 1}. {consumer['name']} — {assignment}")
        return "\n".join(rows)

    async def async_step_consumer_add(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add one consumer with a fresh identity, stable throughout form retries."""
        if self._adding_consumer_id is None:
            self._adding_consumer_id = uuid4().hex
        return await self._async_consumer_details("consumer_add", user_input)

    def _consumer_id_selector(self) -> SelectSelector:
        """Identify consumers by UUID while showing disambiguated display names."""
        options: list[SelectOptionDict] = [
            {"value": row["consumer_id"], "label": f"{index + 1}. {row['name']}"}
            for index, row in enumerate(self._consumer_working_plan()["consumers"])
        ]
        return SelectSelector(SelectSelectorConfig(options=options))

    async def async_step_consumer_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Select an existing consumer, retaining its stable identity."""
        errors: dict[str, str] = {}
        if user_input is not None:
            consumer_id = user_input.get("consumer_id")
            if self._find_consumer(consumer_id) is None:
                errors["consumer_id"] = "consumer_not_found"
            else:
                self._editing_consumer_id = consumer_id
                return await self.async_step_consumer_edit_details()
        return self.async_show_form(
            step_id="consumer_edit",
            data_schema=vol.Schema(
                {vol.Required("consumer_id"): self._consumer_id_selector()}
            ),
            errors=errors,
            last_step=False,
        )

    def _find_consumer(self, consumer_id: object) -> dict[str, Any] | None:
        """Resolve only an identity present in this isolated editor."""
        rows: list[dict[str, Any]] = self._consumer_working_plan()["consumers"]
        for row in rows:
            if row["consumer_id"] == consumer_id:
                return row
        return None

    async def async_step_consumer_edit_details(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit a consumer's label and mode-specific assignment in place."""
        return await self._async_consumer_details("consumer_edit_details", user_input)

    def _validate_consumer_editor_input(
        self, mode: str, values: dict[str, Any]
    ) -> tuple[ConsumerCandidate | None, dict[str, str]]:
        """Pin a selected source to its registry identity before leaving the row."""
        parsed, errors = validate_consumer_input(mode, values)
        if parsed is None or mode != "separate_meters":
            return parsed, errors
        resolved, errors = validate_energy_sources(
            self.hass, {"source": values["source"]}
        )
        if resolved is None:
            return None, errors
        return SeparateConsumerCandidate(
            name=parsed["name"], source=resolved["source"]
        ), {}

    async def _async_consumer_details(
        self, step_id: str, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        """Share exact per-consumer input checks between add and edit."""
        plan = self._consumer_working_plan()
        separate = plan["mode"] == "separate_meters"
        existing = (
            self._find_consumer(self._editing_consumer_id)
            if step_id == "consumer_edit_details"
            else None
        )
        if step_id == "consumer_edit_details" and existing is None:
            return await self.async_step_consumer_edit()
        errors: dict[str, str] = {}
        if user_input is not None:
            values = dict(user_input)
            confirmation = values.pop("consumer_measurement_confirmed", None)
            if separate and confirmation is not True:
                errors["consumer_measurement_confirmed"] = (
                    "consumer_confirmation_required"
                )
            if not separate and confirmation is not None:
                errors["base"] = "unexpected_field"
            parsed, input_errors = self._validate_consumer_editor_input(
                plan["mode"], values
            )
            errors.update(input_errors)
            if parsed is not None and not errors:
                if existing is not None:
                    existing.clear()
                    existing.update(consumer_id=self._editing_consumer_id, **parsed)
                else:
                    plan["consumers"].append(
                        {"consumer_id": self._adding_consumer_id, **parsed}
                    )
                    self._adding_consumer_id = None
                return await self.async_step_consumer_menu()
        suggestions = _consumer_suggestions(existing, user_input)
        fields: dict[vol.Marker, object] = {vol.Required("name"): TextSelector()}
        if separate:
            fields[vol.Required("source")] = self._energy_selector_with_suggestions(
                ("source",), suggestions
            )
            fields[vol.Required("consumer_measurement_confirmed", default=False)] = (
                BooleanSelector()
            )
        else:
            fields[vol.Required("share_percent")] = TextSelector(
                TextSelectorConfig(type=TextSelectorType.TEXT, suffix="%")
            )
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(fields), suggestions
            ),
            errors=errors,
            last_step=False,
        )

    async def async_step_consumer_remove(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Explicitly remove a consumer only from the current draft."""
        plan = self._consumer_working_plan()
        errors: dict[str, str] = {}
        if user_input is not None:
            row = self._find_consumer(user_input.get("consumer_id"))
            if row is None:
                errors["consumer_id"] = "consumer_not_found"
            if user_input.get("confirm_removal") is not True:
                errors["confirm_removal"] = "removal_confirmation_required"
            if row is not None and not errors:
                plan["consumers"].remove(row)
                return await self.async_step_consumer_menu()
        return self.async_show_form(
            step_id="consumer_remove",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required("consumer_id"): self._consumer_id_selector(),
                        vol.Required(
                            "confirm_removal", default=False
                        ): BooleanSelector(),
                    }
                ),
                user_input,
            ),
            errors=errors,
            last_step=False,
        )
