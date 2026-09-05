# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""CO2 Saver lifecycle, keeping the accounting import graph HA-independent."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .runtime import Co2SaverConfigEntry


async def async_setup_entry(hass: HomeAssistant, entry: Co2SaverConfigEntry) -> bool:
    """Load Home Assistant wiring only when setting up an entry."""
    from .runtime import async_setup_entry as setup  # noqa: PLC0415

    return await setup(hass, entry)


async def async_unload_entry(hass: HomeAssistant, entry: Co2SaverConfigEntry) -> bool:
    """Unload Home Assistant wiring without coupling the pure domain to HA."""
    from .runtime import async_unload_entry as unload  # noqa: PLC0415

    return await unload(hass, entry)
