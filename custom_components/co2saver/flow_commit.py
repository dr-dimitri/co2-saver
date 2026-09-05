# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Transient final-flow reservations shared with entry setup."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.util.hass_dict import HassKey

from .bootstrap import manifest_lock
from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant


@dataclass(slots=True)
class CommitReservations:
    """Track target plants and edited owners until their commits are visible."""

    targets: dict[str, str] = field(default_factory=dict)
    entries: dict[str, str] = field(default_factory=dict)
    creates: dict[str, str] = field(default_factory=dict)


_RESERVATIONS = HassKey[CommitReservations]("co2saver_commit_reservations")


def reservations(hass: HomeAssistant) -> CommitReservations:
    """Return one integration-wide reservation table (under manifest lock)."""
    return hass.data.setdefault(_RESERVATIONS, CommitReservations())


def reserve_commit(
    hass: HomeAssistant,
    target: str,
    token: str,
    entry: ConfigEntry | None,
) -> None:
    """Check and reserve a final commit while holding the manifest lock."""
    pending = reservations(hass)
    if target in pending.targets or (
        entry is not None and entry.entry_id in pending.entries
    ):
        reason = "already_in_progress"
        raise AbortFlow(reason)
    if any(
        candidate is not entry and candidate.data.get("plant_key") == target
        for candidate in hass.config_entries.async_entries(DOMAIN)
    ):
        reason = "already_configured"
        raise AbortFlow(reason)
    pending.targets[target] = token
    if entry is not None:
        pending.entries[entry.entry_id] = token


def release_commit(hass: HomeAssistant, token: str) -> None:
    """Release only reservations belonging to this operation under the lock."""
    pending = reservations(hass)
    for table in (pending.targets, pending.entries, pending.creates):
        for key in [key for key, owner in table.items() if owner == token]:
            del table[key]


async def async_release_visible_create(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Let normal entry duplicate checks take over at the first setup."""
    async with manifest_lock(hass):
        locator = entry.data.get("storage_id")
        if isinstance(locator, str) and (
            token := reservations(hass).creates.get(locator)
        ):
            release_commit(hass, token)


async def _async_release_commit(hass: HomeAssistant, token: str) -> None:
    """Release a completed or aborted finalization under the shared lock."""
    async with manifest_lock(hass):
        release_commit(hass, token)


class CreateFinalization:
    """Keep a reservation until HA inserts the entry or its owning task stops."""

    def __init__(self, hass: HomeAssistant, token: str) -> None:
        """Track the task encompassing both the flow step and HA finalization."""
        owner = asyncio.current_task()
        if owner is None:  # pragma: no cover - created from an active flow task
            message = "create finalization requires a running task"
            raise RuntimeError(message)
        self._hass = hass
        self._token = token
        self._owner = owner
        self._finished = False
        self.storage_id: str | None = None
        owner.add_done_callback(self._owner_finished)

    @callback
    def discard(self) -> None:
        """Detach tracking when the flow step already released its reservation."""
        self._finished = True
        self._owner.remove_done_callback(self._owner_finished)

    @callback
    def flow_removed(self) -> None:
        """Cancel an unseen create before permitting another flow to claim it."""
        if self._finished:
            return
        visible = self.storage_id is not None and any(
            entry.data.get("storage_id") == self.storage_id
            for entry in self._hass.config_entries.async_entries(DOMAIN)
        )
        if (
            not visible
            and not self._owner.done()
            and self._owner is not asyncio.current_task()
        ):
            # Removing a flow does not itself stop HA's awaited finalization.
            # Retain the reservation until cancellation has actually completed;
            # otherwise that task could still insert a second entry later.
            self._owner.cancel()
            return
        self._schedule_release()

    @callback
    def _owner_finished(self, _task: asyncio.Task[object]) -> None:
        """Recover reservations from failure or cancellation in HA's finish step."""
        self._schedule_release()

    @callback
    def _schedule_release(self) -> None:
        """Schedule at most one lock-protected cleanup after insertion stops."""
        if self._finished:
            return
        self.discard()
        self._hass.async_create_task(
            _async_release_commit(self._hass, self._token),
            "co2saver release completed create reservation",
        )
