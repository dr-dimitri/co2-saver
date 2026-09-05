# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only
# ruff: noqa: INP001

"""Capture an upgrade fixture when copied into the documented historical archive."""

from __future__ import annotations

import json
import os
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from .test_runtime import runtime_environment, timers
from .test_storage_runtime import _pv_charge, _site

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .test_runtime import _Timer

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")
__all__ = ("runtime_environment", "timers")


@pytest.fixture
def hass_storage() -> dict[str, object]:
    """Use real Store I/O instead of the HA test plugin's default memory store."""
    return {}


@pytest.fixture
def hass_config_dir(tmp_path: Path) -> str:
    """Keep every real HA write inside this capture's temporary directory."""
    return str(tmp_path)


async def test_capture_historical_upgrade(
    hass: HomeAssistant, timers: list[_Timer]
) -> None:
    """Export real old-code Store bytes after observed charging and partial use."""
    site = await _site(hass, timers)
    await _pv_charge(site)
    state = await site.step({"discharge": 1, "load": 1}, grid="500")
    assert state.totals.direct_net_g == 720
    assert state.totals.storage_net_g == Fraction(3920, 9)
    assert state.ledger is not None
    assert state.ledger.pv_lower.kwh == Fraction(17, 10)
    assert state.ledger.pv_burden.grams == Fraction(680, 9)
    assert await hass.config_entries.async_unload(site.entry.entry_id)

    entry = site.entry
    storage_directory = Path(hass.config.config_dir) / ".storage"
    stores = await hass.async_add_executor_job(
        lambda: {
            path.name: path.read_text(encoding="utf-8")
            for path in sorted(storage_directory.glob("co2saver.*"))
        }
    )
    assert len(stores) == 2
    assert all(json.loads(raw)["data"]["minor_version"] == 1 for raw in stores.values())
    fixture = {
        "source_commit": "e77c740201b6565c3cd43ff2894e742b2a2706e9",
        "integration_tree": "193ff9f72ea89c1804eb0e57a1c7442981601833",
        "config_entry": {
            "entry_id": entry.entry_id,
            "domain": entry.domain,
            "title": entry.title,
            "version": entry.version,
            "minor_version": entry.minor_version,
            "source": entry.source,
            "data": dict(entry.data),
            "options": dict(entry.options),
        },
        "sources": {
            role: {
                "id": source.id,
                "entity_id": source.entity_id,
                "platform": source.platform,
                "unique_id": source.unique_id,
            }
            for role, source in site.sources.items()
        },
        "counters": {role: str(value) for role, value in site.counters.items()},
        "period_end": site.period.isoformat(),
        "stores": stores,
    }
    target = Path(os.environ["CO2SAVER_UPGRADE_FIXTURE"])
    await hass.async_add_executor_job(
        lambda: target.write_text(
            json.dumps(fixture, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    )
