# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Explicitly confirmed repair, completing only after a verified loaded generation."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.selector import BooleanSelector
from homeassistant.util.hass_dict import HassKey

from .const import DOMAIN
from .measurement.storage import VerifiedAtomicStoreError
from .repair_issues import storage_issue_id
from .repair_storage import async_complete_repair, async_prepare_repair
from .runtime import EntryRuntime

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .persistence import Manifest

_REPAIR_LOCKS: HassKey[dict[str, asyncio.Lock]] = HassKey("co2saver_repair_locks")


class StorageRepairFlow(RepairsFlow):
    """Keep failed preparation/reload resumable without resetting a second time."""

    def __init__(self, entry_id: str | None) -> None:
        """Bind one issue's existing owner, never a user-supplied storage path."""
        super().__init__()
        self._entry_id = entry_id
        self._prepared: Manifest | None = None

    def _entry(self) -> ConfigEntry | None:
        """Refuse missing or foreign config entries before any action."""
        if self._entry_id is None:
            return None
        entry = self.hass.config_entries.async_get_entry(self._entry_id)
        return entry if entry is not None and entry.domain == DOMAIN else None

    def _confirmation(self, error: str | None = None) -> RepairsFlowResult:
        """Require an affirmative checkbox and explain irreversible reset effects."""
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema(
                {vol.Required("confirm_reset", default=False): BooleanSelector()}
            ),
            errors={"base": error} if error else None,
        )

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,  # noqa: ARG002
    ) -> RepairsFlowResult:
        """Offer a non-destructive retry before an optional confirmed new balance."""
        if self._entry_id is None:
            return self.async_abort(reason="unknown_issue")
        if (entry := self._entry()) is None:
            return self.async_abort(reason="entry_missing")
        return self.async_show_menu(
            step_id="init",
            menu_options=["retry", "confirm"],
            description_placeholders={"name": entry.title},
        )

    async def _reload(self, entry: ConfigEntry) -> bool:
        """Await public reload and its final loaded state before reporting success."""
        return (
            await self.hass.config_entries.async_reload(entry.entry_id)
            and entry.state is ConfigEntryState.LOADED
        )

    async def _stop_failed_runtime(self, entry: ConfigEntry) -> None:
        """Leave no active writer behind after an incomplete repair."""
        runtime = getattr(entry, "runtime_data", None)
        if not isinstance(runtime, EntryRuntime):
            return
        runtime.failed = True
        runtime.available = False
        runtime.status = "storage_error"
        if runtime.runner is not None:
            await runtime.runner.async_stop()
        # A successful commit already in flight may have published while draining.
        runtime.failed = True
        runtime.available = False
        runtime.status = "storage_error"
        async_dispatcher_send(self.hass, runtime.update_signal)

    async def _run(self, *, reset: bool) -> RepairsFlowResult:  # noqa: PLR0911
        """Serialize all side effects and recheck issue lifetime inside the lock."""
        if (entry := self._entry()) is None:
            return self.async_abort(reason="entry_missing")
        locks = self.hass.data.setdefault(_REPAIR_LOCKS, {})
        async with locks.setdefault(entry.entry_id, asyncio.Lock()):
            if self._entry() is not entry:
                return self.async_abort(reason="entry_missing")
            issue_id = storage_issue_id(entry.entry_id)
            issue = ir.async_get(self.hass).async_get_issue(DOMAIN, issue_id)
            if issue is None:
                return self.async_abort(reason="already_repaired")
            token = issue.data.get("repair_token") if issue.data else None
            try:
                if reset:
                    if not await self.hass.config_entries.async_unload(entry.entry_id):
                        return self._confirmation("unload_failed")
                    self._prepared = await async_prepare_repair(
                        self.hass,
                        entry,
                        prepared=self._prepared,
                        issue_token=token if isinstance(token, str) else None,
                    )
                if not await self._reload(entry):
                    await self._stop_failed_runtime(entry)
                    return self._confirmation("reload_failed")
                await async_complete_repair(self.hass, entry, prepared=self._prepared)
                if entry.runtime_data.failed:
                    return self._confirmation("repair_failed")
            except OSError, ValueError, VerifiedAtomicStoreError, HomeAssistantError:
                await self._stop_failed_runtime(entry)
                return self._confirmation("repair_failed" if reset else "reload_failed")
            ir.async_delete_issue(self.hass, DOMAIN, issue_id)
            return self.async_create_entry(data={})

    async def async_step_retry(
        self,
        user_input: dict[str, Any] | None = None,  # noqa: ARG002
    ) -> RepairsFlowResult:
        """Revalidate the authoritative generation without replacing its pointer."""
        return await self._run(reset=False)

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> RepairsFlowResult:
        """Begin a new generation only after the user explicitly accepts data loss."""
        if user_input is None:
            return self._confirmation()
        if user_input.get("confirm_reset") is not True:
            return self._confirmation("confirmation_required")
        return await self._run(reset=True)


async def async_create_fix_flow(
    hass: HomeAssistant,  # noqa: ARG001 - the Repairs manager supplies hass to the flow
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Resolve only the storage repair belonging to its declared existing entry."""
    entry_id = data.get("entry_id") if data is not None else None
    return StorageRepairFlow(
        entry_id
        if isinstance(entry_id, str) and issue_id == storage_issue_id(entry_id)
        else None
    )
