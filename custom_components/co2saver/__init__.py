# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""CO2 Saver integration setup."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

type Co2SaverConfigEntry = ConfigEntry[None]


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: Co2SaverConfigEntry,  # noqa: ARG001
) -> bool:
    """Set up CO2 Saver from a config entry."""
    return True


async def async_unload_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: Co2SaverConfigEntry,  # noqa: ARG001
) -> bool:
    """Unload a CO2 Saver config entry."""
    return True
