# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Minimal diagnostics expose cached quality without identifiers or measurement I/O."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.components.diagnostics import REDACTED
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)

from custom_components.co2saver.config_factors import HomeAssistantGridIntensityReader
from custom_components.co2saver.const import DOMAIN
from custom_components.co2saver.diagnostics import async_get_config_entry_diagnostics
from custom_components.co2saver.measurement.ha import HomeAssistantEnergyReader
from custom_components.co2saver.persistence import (
    GENERATION_MINOR_VERSION,
    GENERATION_SCHEMA_VERSION,
    MANIFEST_MINOR_VERSION,
    MANIFEST_SCHEMA_VERSION,
)

from .test_runtime import (
    _BASELINE,
    _HOUSE,
    _START,
    _WALLBOX,
    _baseline,
    _energy,
    _grid,
    _plan,
    _setup,
    _tick,
    _vector,
    runtime_environment,
    timers,
)
from .test_storage_runtime import _pv_charge, _site

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

    from .test_runtime import _Timer

__all__ = ("runtime_environment", "timers")
pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.mark.parametrize("battery", [False, True])
@pytest.mark.parametrize("mode", ["aggregate_shares", "separate_meters"])
async def test_public_diagnostics_download_contains_only_allowed_quality_context(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    timers: list[_Timer],
    mode: str,
    *,
    battery: bool,
) -> None:
    """The actual diagnostics endpoint reveals units and state, never personal data."""
    plan, sources = _plan(hass, mode=mode, battery=battery)
    plan["consumption"]["consumers"][0]["name"] = "Maria Hauptstraße 42"
    before_setup = await async_get_config_entry_diagnostics(
        hass, MockConfigEntry(domain=DOMAIN, data=plan)
    )
    assert before_setup["runtime"] is None
    unknown_units = before_setup["sources"]
    assert isinstance(unknown_units, list)
    assert all(row["unit"] is None for row in unknown_units)
    entry = await _setup(hass, plan)
    await _baseline(hass, sources, timers, mode=mode)
    state = entry.runtime_data.state
    result = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    assert result["configuration"] == {
        "topology": "inverter",
        "consumption_mode": mode,
        "battery_configured": battery,
    }
    assert result["versions"] == {
        "config_entry": {"major": entry.version, "minor": entry.minor_version},
        "supported_manifest": {
            "major": MANIFEST_SCHEMA_VERSION,
            "minor": MANIFEST_MINOR_VERSION,
        },
        "supported_generation": {
            "major": GENERATION_SCHEMA_VERSION,
            "minor": GENERATION_MINOR_VERSION,
        },
    }
    assert result["runtime"] == {
        "status": "ok",
        "available": True,
        "failed": False,
        "phase": "active",
        "last_accepted_period_end": _BASELINE.isoformat(),
        "diagnostic_counters": dict(state.diagnostics),
        "ledger_present": battery,
        "ledger_quarantined": True if battery else None,
    }
    assert result["entry_state"] == "loaded"
    assert set(result) == {
        "configuration",
        "versions",
        "runtime",
        "entry_state",
        "sources",
    }
    source_rows = result["sources"]
    assert isinstance(source_rows, list)
    assert all(row["identity"] == REDACTED for row in source_rows)
    assert all(
        row["unit"] == (None if row["role"] == "grid_intensity" else "kWh")
        for row in source_rows
    )
    assert ("consumer" in {row["role"] for row in source_rows}) == (
        mode == "separate_meters"
    )
    serialized = json.dumps(result)
    private_values = (
        entry.entry_id,
        entry.data["storage_id"],
        state.generation,
        state.segment_fingerprint,
        _HOUSE,
        _WALLBOX,
        "Maria Hauptstraße 42",
        hass.config.config_dir,
        *(source.id for source in sources.values()),
        *(source.entity_id for source in sources.values()),
    )
    assert all(value not in serialized for value in private_values)
    for forbidden in (
        "capacity",
        "cumulative",
        "totals",
        "consumer_totals",
        "pv_lower",
        "stored_upper",
        "last_reported",
        "repair_reset_at",
        "candidate",
        "latitude",
        "longitude",
    ):
        assert forbidden not in serialized


async def test_diagnostics_never_read_states_adapters_or_store(
    hass: HomeAssistant, timers: list[_Timer], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A diagnostics request remains observational even while measurement is active."""
    plan, sources = _plan(hass)
    entry = await _setup(hass, plan)
    await _baseline(hass, sources, timers)
    before = entry.runtime_data.state
    forbidden = Mock(side_effect=AssertionError("diagnostics must not read sources"))
    forbidden_io = AsyncMock(
        side_effect=AssertionError("diagnostics must not access storage")
    )
    with monkeypatch.context() as patch:
        patch.setattr(type(hass.states), "get", forbidden)
        patch.setattr(HomeAssistantEnergyReader, "read", forbidden)
        patch.setattr(HomeAssistantGridIntensityReader, "read", forbidden)
        patch.setattr(Store, "async_load", forbidden_io)
        patch.setattr(Store, "async_save", forbidden_io)
        result = await async_get_config_entry_diagnostics(hass, entry)
    assert result["runtime"] is not None
    assert entry.runtime_data.state is before
    forbidden.assert_not_called()
    forbidden_io.assert_not_called()


async def test_setup_error_diagnostics_need_no_runtime_or_source_state(
    hass: HomeAssistant,
) -> None:
    """Diagnose incomplete or invalid configuration without leaking private data."""
    private_text = "Private Name /private/location/manifest.json"
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=private_text,
        data={
            "topology": private_text,
            "battery": private_text,
            "sources": {"grid_import": private_text, private_text: private_text},
            "consumption": {"mode": private_text, "consumers": private_text},
            "factors": {"grid_intensity_source": private_text},
            "storage_id": private_text,
        },
    )
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert result["runtime"] is None
    assert result["entry_state"] == "setup_error"
    assert result["configuration"] == {
        "topology": None,
        "consumption_mode": None,
        "battery_configured": None,
    }
    assert result["sources"] == [
        {"role": "grid_import", "identity": REDACTED, "unit": None},
        {"role": "grid_intensity", "identity": REDACTED, "unit": None},
    ]
    assert private_text not in json.dumps(result)


async def test_diagnostics_redact_unknown_status_and_counter_names(
    hass: HomeAssistant, timers: list[_Timer]
) -> None:
    """New arbitrary status or diagnostic strings cannot widen the export contract."""
    plan, sources = _plan(hass)
    entry = await _setup(hass, plan)
    await _baseline(hass, sources, timers)
    runtime = entry.runtime_data
    runtime.status = "/private/user/home"
    baseline = runtime.state.measurement.baseline
    assert baseline is not None
    original_source = baseline.samples[0].source
    private_source = replace(original_source, role="Private Source Role")
    runtime.state = replace(
        runtime.state,
        diagnostics=tuple(
            sorted(
                (
                    *runtime.state.diagnostics,
                    ("Private User", 100),
                    ("manifest_losses", 1),
                )
            )
        ),
        measurement=replace(
            runtime.state.measurement,
            sources=tuple(
                private_source if source == original_source else source
                for source in runtime.state.measurement.sources
            ),
            baseline=replace(
                baseline,
                samples=(
                    replace(baseline.samples[0], source=private_source),
                    *baseline.samples[1:],
                ),
            ),
        ),
    )
    result = await async_get_config_entry_diagnostics(hass, entry)
    details = result["runtime"]
    assert isinstance(details, dict)
    assert details["status"] == REDACTED
    assert details["diagnostic_counters"]["manifest_losses"] == 1
    assert "Private User" not in json.dumps(result)
    assert "Private Source Role" not in json.dumps(result)
    assert "/private/user/home" not in json.dumps(result)


@pytest.mark.parametrize("consumption", [None, {"mode": "separate_meters"}])
async def test_incomplete_configuration_does_not_guess_missing_values(
    hass: HomeAssistant, consumption: dict[str, str] | None
) -> None:
    """Missing fields leave explicit unknowns without copying or reading more data."""
    entry = MockConfigEntry(domain=DOMAIN, data={"consumption": consumption})
    result = await async_get_config_entry_diagnostics(hass, entry)
    assert result["runtime"] is None
    assert result["configuration"] == {
        "topology": None,
        "consumption_mode": "separate_meters" if consumption else None,
        "battery_configured": None,
    }
    assert result["sources"] == (
        [{"role": "household", "identity": REDACTED, "unit": None}]
        if consumption
        else []
    )


async def test_cached_candidate_units_do_not_export_candidate_time_or_values(
    hass: HomeAssistant, timers: list[_Timer]
) -> None:
    """A waiting initial candidate supplies safe units without exposing its history."""
    plan, sources = _plan(hass)
    entry = await _setup(hass, plan)
    initial = await async_get_config_entry_diagnostics(hass, entry)
    initial_sources = initial["sources"]
    assert isinstance(initial_sources, list)
    assert all(row["unit"] is None for row in initial_sources)
    _vector(hass, sources, _BASELINE, cycles=0)
    _energy(hass, sources["grid_export"], "100", _START - timedelta(seconds=1))
    _grid(hass, sources, _BASELINE)
    await _tick(hass, timers, _BASELINE)
    assert entry.runtime_data.state.measurement.candidate is not None
    assert entry.runtime_data.state.measurement.baseline is None
    result = await async_get_config_entry_diagnostics(hass, entry)
    source_rows = result["sources"]
    assert isinstance(source_rows, list)
    assert all(
        row["unit"]
        == (None if row["role"] in {"grid_export", "grid_intensity"} else "kWh")
        for row in source_rows
    )
    details = result["runtime"]
    assert isinstance(details, dict)
    assert details["last_accepted_period_end"] is None
    assert _BASELINE.isoformat() not in json.dumps(result)


async def test_filled_provenance_exports_only_its_quarantine_indicator(
    hass: HomeAssistant, timers: list[_Timer]
) -> None:
    """A real PV charge is useful context without publishing inventory or burdens."""
    site = await _site(hass, timers)
    await _pv_charge(site)
    result = await async_get_config_entry_diagnostics(hass, site.entry)
    details = result["runtime"]
    assert isinstance(details, dict)
    assert details["ledger_present"] is True
    assert details["ledger_quarantined"] is False
    assert "2.7" not in json.dumps(result)
    assert "120" not in json.dumps(result)
