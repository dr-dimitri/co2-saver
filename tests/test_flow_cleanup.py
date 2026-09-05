# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Finalization failures after CREATE_ENTRY cannot strand or bypass reservations."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType

from custom_components.co2saver.const import DOMAIN
from custom_components.co2saver.flow_commit import reservations

from .test_factor_config_flow import (
    _assert_no_reservations,
    _configure,
    _factors,
    _to_factors,
    setup_boundary,
    sites,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_registry import RegistryEntry


__all__ = ("setup_boundary", "sites")

pytestmark = pytest.mark.usefixtures("enable_custom_integrations", "setup_boundary")


async def test_actual_creation_failure_releases_reservation_when_flow_is_removed(
    hass: HomeAssistant, sites: list[dict[str, RegistryEntry]]
) -> None:
    """An error inserting the returned result cannot permanently reserve a plant."""
    result = await _to_factors(hass, sites[0])
    with (
        patch.object(
            hass.config_entries,
            "async_add",
            AsyncMock(side_effect=OSError("actual entry insertion failed")),
        ),
        pytest.raises(OSError, match="actual entry insertion failed"),
    ):
        await _configure(hass, result, _factors(sites[0]))
    assert hass.config_entries.async_entries(DOMAIN) == []
    assert reservations(hass).targets
    assert reservations(hass).creates

    hass.config_entries.flow.async_abort(result["flow_id"])
    await hass.async_block_till_done()
    _assert_no_reservations(hass)

    retry = await _to_factors(hass, sites[0])
    created = await _configure(hass, retry, _factors(sites[0]))
    assert created["type"] is FlowResultType.CREATE_ENTRY
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    _assert_no_reservations(hass)


async def test_failed_finalization_task_releases_without_explicit_flow_abort(
    hass: HomeAssistant, sites: list[dict[str, RegistryEntry]]
) -> None:
    """Task completion catches errors beyond the integration's final form method."""
    result = await _to_factors(hass, sites[0])
    with patch.object(
        hass.config_entries,
        "async_add",
        AsyncMock(side_effect=OSError("actual entry insertion failed")),
    ):
        owner = asyncio.create_task(_configure(hass, result, _factors(sites[0])))
        with pytest.raises(OSError, match="actual entry insertion failed"):
            await owner
    await hass.async_block_till_done()
    assert hass.config_entries.async_entries(DOMAIN) == []
    _assert_no_reservations(hass)

    # Retry the same still-visible form after its failed request task has ended.
    created = await _configure(hass, result, _factors(sites[0]))
    assert created["type"] is FlowResultType.CREATE_ENTRY
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    _assert_no_reservations(hass)


@pytest.mark.parametrize("termination", ["flow_abort", "task_cancel"])
async def test_stalled_entry_insertion_cannot_resume_after_reservation_is_released(
    hass: HomeAssistant,
    sites: list[dict[str, RegistryEntry]],
    termination: str,
) -> None:
    """External cancellation retains exclusivity until the old inserter has stopped."""
    result = await _to_factors(hass, sites[0])
    started = asyncio.Event()
    cancellation_seen = asyncio.Event()
    finish_cancellation = asyncio.Event()
    resume_insertion = asyncio.Event()
    original_add = hass.config_entries.async_add

    async def delayed_add(entry: ConfigEntry) -> None:
        started.set()
        try:
            await resume_insertion.wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            await finish_cancellation.wait()
            raise
        await original_add(entry)

    with patch.object(hass.config_entries, "async_add", side_effect=delayed_add):
        owner = asyncio.create_task(_configure(hass, result, _factors(sites[0])))
        await started.wait()
        if termination == "flow_abort":
            hass.config_entries.flow.async_abort(result["flow_id"])
        else:
            owner.cancel()
        await cancellation_seen.wait()
        assert not owner.done()
        assert hass.config_entries.async_entries(DOMAIN) == []
        assert reservations(hass).targets
        assert reservations(hass).creates

        competitor = await _to_factors(hass, sites[0])
        blocked = await _configure(hass, competitor, _factors(sites[0]))
        assert blocked["type"] is FlowResultType.ABORT
        assert blocked["reason"] == "already_in_progress"

        finish_cancellation.set()
        with pytest.raises(asyncio.CancelledError):
            await owner
    await hass.async_block_till_done()
    _assert_no_reservations(hass)
    assert hass.config_entries.async_entries(DOMAIN) == []
    if termination == "task_cancel":
        hass.config_entries.flow.async_abort(result["flow_id"])
    resume_insertion.set()
    retry = await _to_factors(hass, sites[0])
    created = await _configure(hass, retry, _factors(sites[0]))
    assert created["type"] is FlowResultType.CREATE_ENTRY
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    _assert_no_reservations(hass)
