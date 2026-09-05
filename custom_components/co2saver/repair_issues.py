# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Entry-scoped repair notifications without exposing source or measurement data."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from homeassistant.core import callback
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant


def storage_issue_id(entry_id: str) -> str:
    """Identify the one storage-integrity notification for this config entry."""
    return f"storage_integrity:{entry_id}"


@callback
def async_report_storage_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Offer verified reload or an explicitly confirmed new accounting generation."""
    issue = ir.async_get(hass).async_get_issue(DOMAIN, storage_issue_id(entry.entry_id))
    token = issue.data.get("repair_token") if issue is not None and issue.data else None
    if not isinstance(token, str):
        token = uuid4().hex
    ir.async_create_issue(
        hass,
        DOMAIN,
        storage_issue_id(entry.entry_id),
        is_fixable=True,
        is_persistent=True,
        severity=ir.IssueSeverity.ERROR,
        translation_key="storage_integrity",
        translation_placeholders={"name": entry.title},
        data={"entry_id": entry.entry_id, "repair_token": token},
    )


@callback
def async_report_source_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Require reconfiguration of a removed or disabled source without a reset."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"sources_changed:{entry.entry_id}",
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key="sources_changed",
        translation_placeholders={"name": entry.title},
    )


@callback
def async_report_configuration_issue(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Preserve incompatible configuration until compatible software or data returns."""
    ir.async_create_issue(
        hass,
        DOMAIN,
        f"configuration_invalid:{entry.entry_id}",
        is_fixable=False,
        severity=ir.IssueSeverity.ERROR,
        translation_key="configuration_invalid",
        translation_placeholders={"name": entry.title},
    )


@callback
def async_clear_setup_issues(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clear resolved source/configuration issues after successful verified setup."""
    for prefix in ("sources_changed", "configuration_invalid"):
        ir.async_delete_issue(hass, DOMAIN, f"{prefix}:{entry.entry_id}")
