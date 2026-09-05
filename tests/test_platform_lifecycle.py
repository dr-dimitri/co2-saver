# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Sensor-platform failure boundaries preserve the authoritative minute runner."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState

from custom_components.co2saver import async_unload_entry

from .test_runtime import (
    _BASELINE,
    _baseline,
    _plan,
    _setup,
    _tick,
    runtime_environment,
    timers,
)

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .test_runtime import _Timer

__all__ = ("runtime_environment", "timers")
pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.mark.parametrize("failed", [False, True])
async def test_refused_platform_unload_resumes_only_a_healthy_runner(
    hass: HomeAssistant, timers: list[_Timer], *, failed: bool
) -> None:
    """A refused unload preserves loaded entities without reviving a fatal store."""
    plan, sources = _plan(hass)
    entry = await _setup(hass, plan)
    await _baseline(hass, sources, timers)
    runtime = entry.runtime_data
    runtime.failed = failed
    previous = runtime.state
    registered = len(timers)
    with patch.object(
        hass.config_entries,
        "async_unload_platforms",
        new=AsyncMock(return_value=False),
    ):
        assert not await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.FAILED_UNLOAD
    assert timers[registered - 1].cancelled.is_set()
    assert len(timers) == registered + (not failed)
    await _tick(hass, timers, _BASELINE)
    assert runtime.state == previous
    # HA marks FAILED_UNLOAD unrecoverable; release this test's surviving platform.
    assert await async_unload_entry(hass, entry)
