# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Verify that Home Assistant can load every implemented flow label and error."""

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
        "setup_incomplete",
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
        "factors": (),
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
        "setup_incomplete",
    ):
        assert translated[f"{prefix}.error.{error}"]
    selectors = await async_get_translations(hass, language, "selector", {DOMAIN})
    for selector, choices in {
        "consumption_mode": ("aggregate_shares", "separate_meters"),
        "consumer_action": ("add", "edit", "remove", "finish"),
    }.items():
        for choice in choices:
            assert selectors[f"component.{DOMAIN}.selector.{selector}.options.{choice}"]
