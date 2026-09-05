# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Upgrade real pre-migration Store bytes without losing or replaying accounting."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Any

import attr
import pytest
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry, mock_registry

from custom_components.co2saver.persistence import GenerationCodec

from .test_runtime import (
    _HOUSE,
    _WALLBOX,
    _energy,
    _grid,
    _tick,
    reads,
    runtime_environment,
    timers,
)
from .test_storage_runtime import _StorageSite

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from custom_components.co2saver.persistence import GenerationState

    from .test_runtime import _Reads, _Timer

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")
__all__ = ("reads", "runtime_environment", "timers")


@pytest.fixture
def hass_storage() -> dict[str, object]:
    """Exercise physical Store serialization, migration, and fresh readback."""
    return {}


@pytest.fixture
def hass_config_dir(tmp_path: Path) -> str:
    """Isolate all real Home Assistant files before its first registry load."""
    return str(tmp_path)


@pytest.fixture
def historical_upgrade() -> dict[str, Any]:
    """Load the committed capture without Git, an old checkout, or the network."""
    path = Path(__file__).with_name("fixtures") / "upgrade_filled_storage.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _restore_sources(
    hass: HomeAssistant, fixture: dict[str, Any]
) -> dict[str, er.RegistryEntry]:
    """Restore the original synthetic source identities, preserving every role."""
    registry = er.async_get(hass)
    sources = {}
    for role, stored in fixture["sources"].items():
        source = registry.async_get_or_create(
            "sensor",
            stored["platform"],
            stored["unique_id"],
            suggested_object_id=stored["entity_id"].split(".", 1)[1],
        )
        assert source.entity_id == stored["entity_id"]
        sources[role] = attr.evolve(source, id=stored["id"])
    mock_registry(hass, {source.entity_id: source for source in sources.values()})
    return sources


def _restore_files(directory: Path, stores: dict[str, str]) -> None:
    """Install exactly the captured bytes, without a current-code re-encoding."""
    directory.mkdir(exist_ok=True)
    for key, raw in stores.items():
        (directory / key).write_bytes(raw.encode("utf-8"))


def _assert_next_discharge(previous: GenerationState, current: GenerationState) -> None:
    """Credit just the next kWh, preserving exact prior direct and storage totals."""
    assert current.commit_revision == previous.commit_revision + 1
    assert current.segment_fingerprint == previous.segment_fingerprint
    assert current.diagnostics == previous.diagnostics
    assert current.totals.direct_pv_kwh == previous.totals.direct_pv_kwh == 2
    assert current.totals.direct_gross_g == previous.totals.direct_gross_g == 800
    assert current.totals.direct_pv_burden_g == previous.totals.direct_pv_burden_g == 80
    assert current.totals.direct_net_g == previous.totals.direct_net_g == 720
    assert current.totals.storage_pv_kwh == 2
    assert current.totals.storage_gross_g == 1000
    assert current.totals.storage_pv_burden_g == Fraction(800, 9)
    assert current.totals.storage_burden_g == 40
    assert current.totals.storage_net_g == Fraction(7840, 9)
    assert (
        current.totals.unvalued_direct_kwh == current.totals.unvalued_storage_kwh == 0
    )
    assert current.ledger is not None
    assert (
        current.ledger.stored_lower.kwh
        == current.ledger.stored_upper.kwh
        == current.ledger.pv_lower.kwh
        == Fraction(7, 10)
    )
    assert current.ledger.pv_burden.grams == Fraction(280, 9)
    assert dict(current.consumer_totals)[_HOUSE].storage_pv_kwh == Fraction(3, 2)
    assert dict(current.consumer_totals)[_WALLBOX].storage_pv_kwh == Fraction(1, 2)
    assert current.unassigned_storage_kwh == 0


async def test_upgrade_from_issue_11_retains_filled_storage_and_books_once(
    hass: HomeAssistant,
    historical_upgrade: dict[str, Any],
    timers: list[_Timer],
    reads: _Reads,
) -> None:
    """Manifest migration leaves all prior accounting intact through real setup."""
    fixture = historical_upgrade
    assert fixture["source_commit"] == "e77c740201b6565c3cd43ff2894e742b2a2706e9"
    stores = fixture["stores"]
    directory = Path(hass.config.config_dir) / ".storage"
    await hass.async_add_executor_job(_restore_files, directory, stores)
    sources = _restore_sources(hass, fixture)
    entry = MockConfigEntry(**fixture["config_entry"])
    entry.add_to_hass(hass)
    assert entry.version == entry.minor_version == 1
    manifest_key = f"co2saver.{entry.data['storage_id']}.manifest"
    old_manifest = json.loads(stores[manifest_key])
    old_pointer = old_manifest["data"]
    generation_key = (
        f"co2saver.{entry.data['storage_id']}.{old_pointer['active_generation']}"
    )
    old_generation_bytes = stores[generation_key].encode("utf-8")
    old_generation = json.loads(old_generation_bytes)["data"]
    previous = GenerationCodec(
        entry.data["storage_id"], entry.entry_id, old_pointer["active_generation"]
    ).decode(old_generation)
    assert old_pointer["schema_version"] == old_pointer["minor_version"] == 1
    assert previous.totals.direct_net_g == 720
    assert previous.totals.storage_net_g == Fraction(3920, 9)
    assert previous.ledger is not None
    assert previous.ledger.pv_lower.kwh == Fraction(17, 10)
    assert previous.ledger.pv_burden.grams == Fraction(680, 9)

    assert await hass.config_entries.async_setup(entry.entry_id)
    assert entry.runtime_data.state == previous
    assert reads.energy == reads.grid == 0
    assert (
        await hass.async_add_executor_job((directory / generation_key).read_bytes)
        == old_generation_bytes
    )
    migrated_bytes = await hass.async_add_executor_job(
        (directory / manifest_key).read_bytes
    )
    assert json.loads(migrated_bytes) == {
        **old_manifest,
        "data": {
            **old_pointer,
            "minor_version": 2,
            "commit_revision": old_pointer["commit_revision"] + 1,
            "repair_reset_at": None,
            "manifest_lost": False,
            "repair_pending": False,
            "repair_issue_token": None,
        },
    }

    # A second setup neither migrates again nor starts a new accounting segment.
    assert await hass.config_entries.async_reload(entry.entry_id)
    assert entry.runtime_data.state == previous
    assert reads.energy == reads.grid == 0
    assert (
        await hass.async_add_executor_job((directory / manifest_key).read_bytes)
        == migrated_bytes
    )
    assert (
        await hass.async_add_executor_job((directory / generation_key).read_bytes)
        == old_generation_bytes
    )

    site = _StorageSite(
        hass=hass,
        entry=entry,
        sources=sources,
        timers=timers,
        mode=entry.data["consumption"]["mode"],
        counters={role: Decimal(value) for role, value in fixture["counters"].items()},
        period=datetime.fromisoformat(fixture["period_end"]),
    )
    for role, counter in site.counters.items():
        _energy(hass, sources[role], str(counter), site.period)
    _grid(hass, sources, site.period, value="500")
    await _tick(hass, timers, site.period)
    assert entry.runtime_data.available
    assert entry.runtime_data.state == previous
    assert (
        await hass.async_add_executor_job((directory / generation_key).read_bytes)
        == old_generation_bytes
    )

    current = await site.step({"discharge": 1, "load": 1}, grid="500")
    _assert_next_discharge(previous, current)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert timers[-1].cancelled.is_set()
