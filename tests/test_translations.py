# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Verify that Home Assistant can load implemented flow and sensor translations."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from homeassistant.helpers.translation import async_get_translations

from custom_components.co2saver.const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


@pytest.mark.parametrize("language", ["en", "de"])
async def test_flow_translations(
    hass: HomeAssistant, enable_custom_integrations: None, language: str
) -> None:
    """Expose readable setup/reconfigure steps and all validator error keys."""
    _ = enable_custom_integrations
    translated = await async_get_translations(hass, language, "config", {DOMAIN})
    prefix = f"component.{DOMAIN}.config"
    for step in (
        "user",
        "reconfigure",
        "sources",
        "storage",
        "storage_sources",
        "consumers",
    ):
        assert translated[f"{prefix}.step.{step}.title"]
        assert translated[f"{prefix}.step.{step}.description"]
    for role in (
        "pv_generation",
        "grid_import",
        "grid_export",
        "pv_plausibility",
        "synchronous_sources_confirmed",
    ):
        assert translated[f"{prefix}.step.sources.data.{role}"]
    assert translated[f"{prefix}.step.storage.data.battery_present"]
    for field in (
        "battery_charge",
        "battery_discharge",
        "usable_capacity_kwh",
        "round_trip_efficiency_percent",
        "battery_sources_confirmed",
        "battery_identity",
    ):
        assert translated[f"{prefix}.step.storage_sources.data.{field}"]
    for error in (
        "invalid_topology",
        "required",
        "invalid_selection",
        "source_not_registered",
        "invalid_domain",
        "source_disabled",
        "duplicate_source",
        "source_missing",
        "source_unavailable",
        "invalid_value",
        "invalid_device_class",
        "invalid_state_class",
        "invalid_unit",
        "invalid_period_end",
        "invalid_last_reported",
        "future_period_end",
        "future_last_reported",
        "period_after_publication",
        "publication_delay",
        "source_stale",
        "confirmation_required",
        "unexpected_field",
        "sources_not_synchronized",
        "publication_skew",
        "invalid_source",
        "invalid_source_vector",
        "invalid_battery_choice",
        "battery_confirmation_required",
        "invalid_battery_identity",
        "invalid_number",
        "invalid_decimal_separator",
        "capacity_out_of_range",
        "efficiency_out_of_range",
    ):
        assert translated[f"{prefix}.error.{error}"]
    assert translated[f"{prefix}.abort.already_configured"]
    selectors = await async_get_translations(hass, language, "selector", {DOMAIN})
    for topology in ("inverter", "smart_meter"):
        assert selectors[f"component.{DOMAIN}.selector.topology.options.{topology}"]
    for identity in ("same_physical_battery", "physical_battery_replaced"):
        assert selectors[
            f"component.{DOMAIN}.selector.battery_identity.options.{identity}"
        ]
    for choice in ("without_battery", "with_battery"):
        assert selectors[
            f"component.{DOMAIN}.selector.battery_present.options.{choice}"
        ]


@pytest.mark.parametrize("language", ["en", "de"])
@pytest.mark.parametrize("category", ["config", "options"])
async def test_consumer_translations(
    hass: HomeAssistant,
    enable_custom_integrations: None,
    language: str,
    category: str,
) -> None:
    """Load the shared editor's labels and errors through HA's public API."""
    _ = enable_custom_integrations
    translated = await async_get_translations(hass, language, category, {DOMAIN})
    prefix = f"component.{DOMAIN}.{category}"
    assert not any("[%key:" in value for value in translated.values())
    step_fields = {
        "consumers": ("mode",),
        "aggregate_load": ("household_source", "load_measurement_confirmed"),
        "separate_load": ("household_source", "load_measurement_confirmed"),
        "consumer_menu": ("action",),
        "consumer_add": (
            "name",
            "share_percent",
            "source",
            "consumer_measurement_confirmed",
        ),
        "consumer_edit": ("consumer_id",),
        "consumer_edit_details": (
            "name",
            "share_percent",
            "source",
            "consumer_measurement_confirmed",
        ),
        "consumer_remove": ("consumer_id", "confirm_removal"),
        "factors": (
            "grid_intensity_source",
            "grid_max_age_minutes",
            "pv_factor",
            "battery_factor",
        ),
    }
    for step, fields in step_fields.items():
        assert translated[f"{prefix}.step.{step}.title"]
        assert translated[f"{prefix}.step.{step}.description"]
        for field in fields:
            assert translated[f"{prefix}.step.{step}.data.{field}"]
    for error in (
        "invalid_consumption_mode",
        "load_confirmation_required",
        "consumer_confirmation_required",
        "invalid_consumer_action",
        "consumer_not_found",
        "removal_confirmation_required",
        "invalid_name",
        "share_out_of_range",
        "shares_exceed_total",
        "invalid_consumer_plan",
        "invalid_consumer_id",
        "duplicate_consumer_id",
        "invalid_number",
        "invalid_decimal_separator",
        "duplicate_source",
        "invalid_unit",
        "invalid_source_vector",
        "source_unavailable",
        "required",
        "factor_out_of_range",
        "invalid_grid_unit",
        "invalid_grid_value",
        "grid_source_stale",
        "grid_age_out_of_range",
        "invalid_measurement_plan",
        "storage_failed",
    ):
        assert translated[f"{prefix}.error.{error}"]
    for reason in (
        "already_configured",
        "already_in_progress",
        "configuration_changed",
        "reconfigure_successful",
        "options_saved",
    ):
        assert translated[f"{prefix}.abort.{reason}"]
    selectors = await async_get_translations(hass, language, "selector", {DOMAIN})
    for selector, choices in {
        "consumption_mode": ("aggregate_shares", "separate_meters"),
        "consumer_action": ("add", "edit", "remove", "finish"),
    }.items():
        for choice in choices:
            assert selectors[f"component.{DOMAIN}.selector.{selector}.options.{choice}"]


@pytest.mark.parametrize("language", ["en", "de"])
async def test_sensor_translations(
    hass: HomeAssistant, enable_custom_integrations: None, language: str
) -> None:
    """Localize every system and household value and each named consumer template."""
    _ = enable_custom_integrations
    translated = await async_get_translations(hass, language, "entity", {DOMAIN})
    prefix = f"component.{DOMAIN}.entity.sensor"
    for key in (
        "net_savings",
        "direct_net_savings",
        "storage_net_savings",
        "gross_avoided",
        "pv_lifecycle",
        "battery_lifecycle",
        "direct_pv_energy",
        "storage_pv_energy",
        "unassigned_direct_energy",
        "unassigned_storage_energy",
        "unvalued_direct_energy",
        "unvalued_storage_energy",
    ):
        assert translated[f"{prefix}.{key}.name"]
    household = "Haushalt" if language == "de" else "Household"
    for metric in ("net_savings", "direct_pv_energy", "storage_pv_energy"):
        household_label = translated[f"{prefix}.household_{metric}.name"]
        assert household in household_label
        assert "{" not in household_label
        template = translated[f"{prefix}.consumer_{metric}.name"]
        assert "{consumer_name}" in template
        assert "Wallbox" in template.format(consumer_name="Wallbox")
    assert not any("[%key:" in value for value in translated.values())


@pytest.mark.parametrize("language", ["en", "de"])
async def test_repairs_translations(
    hass: HomeAssistant, enable_custom_integrations: None, language: str
) -> None:
    """Expose repair consequences, explicit confirmation, retry and failure messages."""
    _ = enable_custom_integrations
    translated = await async_get_translations(hass, language, "issues", {DOMAIN})
    prefix = f"component.{DOMAIN}.issues"
    for issue in ("storage_integrity", "sources_changed", "configuration_invalid"):
        assert translated[f"{prefix}.{issue}.title"]
        description = translated[f"{prefix}.{issue}.description"]
        assert "{name}" in description
        assert "Solar" in description.format(name="Solar")
    flow = f"{prefix}.storage_integrity.fix_flow"
    for step in ("init", "confirm"):
        assert translated[f"{flow}.step.{step}.title"]
        assert translated[f"{flow}.step.{step}.description"]
    for option in ("retry", "confirm"):
        assert translated[f"{flow}.step.init.menu_options.{option}"]
    assert translated[f"{flow}.step.confirm.data.confirm_reset"]
    for error in (
        "confirmation_required",
        "reload_failed",
        "unload_failed",
        "repair_failed",
    ):
        assert translated[f"{flow}.error.{error}"]
    for reason in ("unknown_issue", "entry_missing", "already_repaired"):
        assert translated[f"{flow}.abort.{reason}"]
    assert not any("[%key:" in value for value in translated.values())
