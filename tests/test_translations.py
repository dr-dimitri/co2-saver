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
    for step in ("user", "reconfigure", "sources", "storage"):
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
    ):
        assert translated[f"{prefix}.error.{error}"]
    assert translated[f"{prefix}.abort.already_configured"]
    selectors = await async_get_translations(hass, language, "selector", {DOMAIN})
    for topology in ("inverter", "smart_meter"):
        assert selectors[f"component.{DOMAIN}.selector.topology.options.{topology}"]
