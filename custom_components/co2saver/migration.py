# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Explicit version gates and lossless migration of the known manifest payload."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from .config_plan import canonical_plan
from .measurement.storage import PayloadMigration, _as_object
from .persistence import Manifest, ManifestCodec, storage_identifier

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .runtime import Co2SaverConfigEntry

CONFIG_VERSION = 1
CONFIG_MINOR_VERSION = 1
_LEGACY_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "minor_version",
        "storage_id",
        "manifest_epoch",
        "owner_entry_id",
        "active_generation",
        "previous_generations",
        "initialized",
        "commit_revision",
    }
)


class ManifestPayloadMigrator:
    """Validate manifest 1.1 completely before proposing its single 1.2 revision."""

    def __init__(self, storage_id: str, owner_entry_id: str | None = None) -> None:
        """Bind the locator and allowed owner before any migration can save."""
        self._codec = ManifestCodec(storage_id)
        self._owner_entry_id = owner_entry_id

    def __call__(self, value: object) -> PayloadMigration[Manifest] | None:
        """Preserve every known old field; unknown versions stay strictly read-only."""
        if (
            type(value) is not dict
            or type(value.get("schema_version")) is not int
            or value.get("schema_version") != 1
            or type(value.get("minor_version")) is not int
            or value.get("minor_version") != 1
        ):
            return None
        legacy = _as_object(value, path="manifest 1.1", keys=_LEGACY_MANIFEST_KEYS)
        previous = self._codec.decode(
            {
                **legacy,
                "minor_version": 2,
                "repair_reset_at": None,
                "manifest_lost": False,
                "repair_pending": False,
                "repair_issue_token": None,
            }
        )
        if previous.owner_entry_id not in (None, self._owner_entry_id):
            message = "manifest migration refuses a foreign owner"
            raise ValueError(message)
        return PayloadMigration(
            previous_revision=previous.commit_revision,
            state=replace(previous, commit_revision=previous.commit_revision + 1),
        )


async def async_migrate_entry(hass: HomeAssistant, entry: Co2SaverConfigEntry) -> bool:
    """Accept only the real 1.1 configuration; no artificial rewrite or downgrade."""
    from .repair_issues import (  # noqa: PLC0415 - keep version constants standalone
        async_report_configuration_issue,
    )

    if (
        type(entry.version) is not int
        or type(entry.minor_version) is not int
        or (entry.version, entry.minor_version)
        != (CONFIG_VERSION, CONFIG_MINOR_VERSION)
    ):
        async_report_configuration_issue(hass, entry)
        return False
    try:
        canonical_plan(entry.data)
        storage_identifier(entry.data.get("storage_id"))
    except KeyError, TypeError, ValueError:
        async_report_configuration_issue(hass, entry)
        return False
    return True
