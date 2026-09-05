# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""CO2 Saver integration setup."""

from __future__ import annotations

from typing import TYPE_CHECKING

from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.helper_integration import async_handle_source_entity_changes

from .bootstrap import PersistedRuntime, async_setup_storage
from .config_plan import all_source_registry_ids, canonical_plan
from .flow_commit import async_release_visible_create
from .measurement.storage import VerifiedAtomicStoreError

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    type Co2SaverConfigEntry = ConfigEntry[PersistedRuntime]


def _validated_sources(
    hass: HomeAssistant, entry: Co2SaverConfigEntry
) -> tuple[str, ...]:
    """Require a complete plan and live registry bindings before activation."""
    canonical_plan(entry.data)
    sources = all_source_registry_ids(entry.data)
    registry = er.async_get(hass)
    if any(
        (registered := registry.async_get(source)) is None
        or registered.disabled_by is not None
        for source in sources
    ):
        message = "A configured source was removed or disabled; reconfigure the plant"
        raise ConfigEntryError(message)
    return sources


@callback
def _keep_registry_identity(_entity_id: str) -> None:
    """Registry UUIDs are authoritative; entity-ID renames change no settings."""


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Co2SaverConfigEntry,
) -> bool:
    """Bind and verify storage before registering source lifecycle callbacks."""
    await async_release_visible_create(hass, entry)
    try:
        _validated_sources(hass, entry)
        runtime = await async_setup_storage(hass, entry)
        sources = _validated_sources(hass, entry)
    except (KeyError, OSError, ValueError, VerifiedAtomicStoreError) as err:
        message = "CO2 Saver configuration or stored state is invalid"
        raise ConfigEntryError(message) from err
    entry.runtime_data = runtime

    async def source_removed() -> None:
        """Stop this entry and require source reconfiguration on the next setup."""
        await hass.config_entries.async_reload(entry.entry_id)

    for source in sources:
        entry.async_on_unload(
            async_handle_source_entity_changes(
                hass,
                helper_config_entry_id=entry.entry_id,
                set_source_entity_id_or_uuid=_keep_registry_identity,
                source_device_id=None,
                source_entity_id_or_uuid=source,
                source_entity_removed=source_removed,
            )
        )
    return True


async def async_unload_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: Co2SaverConfigEntry,  # noqa: ARG001
) -> bool:
    """Unload a CO2 Saver config entry."""
    return True
