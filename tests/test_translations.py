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
