# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Public factor-flow completion, atomic bootstrap, and concurrent edit tests."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
import voluptuous as vol
from homeassistant.config_entries import SOURCE_RECONFIGURE, SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.co2saver.const import ATTR_CO2SAVER_PERIOD_END, DOMAIN
from custom_components.co2saver.flow_commit import (
    async_release_visible_create,
    reservations,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

    from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
    from homeassistant.core import HomeAssistant

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.fixture(autouse=True)
def setup_boundary() -> Iterator[None]:
    """Keep flow tests focused while releasing visible-create reservations."""

    async def setup(hass: HomeAssistant, entry: ConfigEntry) -> bool:
        await async_release_visible_create(hass, entry)
        return True

    with (
        patch("custom_components.co2saver.async_setup_entry", side_effect=setup),
        patch("custom_components.co2saver.async_unload_entry", return_value=True),
    ):
        yield


@pytest.fixture
def sites(hass: HomeAssistant) -> list[dict[str, er.RegistryEntry]]:
    """Publish three independent sites with synchronous energy and grid CO₂."""
    registry = er.async_get(hass)
    period = (dt_util.utcnow() - timedelta(seconds=1)).isoformat()
    result = []
    for number in range(3):
        sources = {}
        for role in (
            "pv_generation",
            "grid_import",
            "grid_export",
            "household_load",
            "battery_charge",
            "battery_discharge",
            "grid_intensity",
        ):
            entry = registry.async_get_or_create(
                "sensor",
                "factor_flow_test",
                f"{number}_{role}",
                suggested_object_id=f"site_{number}_{role}",
            )
            attributes = (
                {"unit_of_measurement": "gCO2eq/kWh"}
                if role == "grid_intensity"
                else {
                    "device_class": "energy",
                    "state_class": "total_increasing",
                    "unit_of_measurement": "kWh",
                    ATTR_CO2SAVER_PERIOD_END: period,
                }
            )
            hass.states.async_set(entry.entity_id, "100", attributes)
            sources[role] = entry
        result.append(sources)
    return result


async def _configure(
    hass: HomeAssistant, result: ConfigFlowResult, data: dict[str, Any]
) -> ConfigFlowResult:
    """Advance either public flow manager from its public handler field."""
    manager = (
        hass.config_entries.flow
        if result["handler"] == DOMAIN
        else hass.config_entries.options
    )
    return await manager.async_configure(result["flow_id"], data)


async def _consumers(
    hass: HomeAssistant,
    result: ConfigFlowResult,
    sources: dict[str, er.RegistryEntry],
    mode: str,
) -> ConfigFlowResult:
    """Configure a household-only load through the public consumer forms."""
    result = await _configure(hass, result, {"mode": mode})
    result = await _configure(
        hass,
        result,
        {
            "household_source": sources["household_load"].entity_id,
            "load_measurement_confirmed": True,
        },
    )
    result = await _configure(hass, result, {"action": "finish"})
    assert result["step_id"] == "factors"
    return result


async def _to_factors(  # noqa: PLR0913
    hass: HomeAssistant,
    sources: dict[str, er.RegistryEntry],
    topology: str = "inverter",
    *,
    battery: bool = False,
    mode: str = "aggregate_shares",
    entry: ConfigEntry | None = None,
) -> ConfigFlowResult:
    """Traverse all predecessor steps without assigning private flow state."""
    context = (
        {"source": SOURCE_USER}
        if entry is None
        else {"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id}
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context=context,
        data={"topology": topology},
    )
    roles = ["grid_import", "grid_export"]
    if topology == "inverter":
        roles.append("pv_generation")
    result = await _configure(
        hass,
        result,
        {
            **{role: sources[role].entity_id for role in roles},
            "synchronous_sources_confirmed": True,
        },
    )
    result = await _configure(
        hass,
        result,
        {
            "battery_present": "with_battery" if battery else "without_battery",
        },
    )
    if battery:
        data = {
            "battery_charge": sources["battery_charge"].entity_id,
            "battery_discharge": sources["battery_discharge"].entity_id,
            "usable_capacity_kwh": "10",
            "round_trip_efficiency_percent": "90",
            "battery_sources_confirmed": True,
        }
        if entry is not None and entry.data.get("battery") is not None:
            data["battery_identity"] = "same_physical_battery"
        result = await _configure(hass, result, data)
    return await _consumers(hass, result, sources, mode)


def _factors(
    sources: dict[str, er.RegistryEntry],
    *,
    battery: bool = False,
) -> dict[str, Any]:
    """Supply independent exact lifecycle factors and an explicit grid source."""
    result = {
        "grid_intensity_source": sources["grid_intensity"].entity_id,
        "grid_max_age_minutes": 60,
        "pv_factor": "40.000",
    }
    if battery:
        result["battery_factor"] = "20.500"
    return result


async def _create(
    hass: HomeAssistant,
    sources: dict[str, er.RegistryEntry],
) -> ConfigEntry:
    """Create a complete entry for an edit scenario via the real user flow."""
    result = await _to_factors(hass, sources)
    result = await _configure(hass, result, _factors(sources))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    return result["result"]


def _assert_no_reservations(hass: HomeAssistant) -> None:
    """Assert no future plant or entry is blocked by a completed operation."""
    pending = reservations(hass)
    assert pending.targets == {}
    assert pending.entries == {}
    assert pending.creates == {}


@pytest.mark.parametrize("topology", ["inverter", "smart_meter"])
@pytest.mark.parametrize("battery", [False, True])
@pytest.mark.parametrize("mode", ["aggregate_shares", "separate_meters"])
async def test_complete_configuration(
    hass: HomeAssistant,
    sites: list[dict[str, er.RegistryEntry]],
    topology: str,
    mode: str,
    *,
    battery: bool,
) -> None:
    """Every supported full path creates serializable data after bootstrap."""
    from custom_components.co2saver.factor_flow import (  # noqa: PLC0415
        async_reserve_bootstrap,
    )

    result = await _to_factors(hass, sites[0], topology, battery=battery, mode=mode)
    assert not hass.config_entries.async_entries(DOMAIN)
    fields = {
        str(key): (key, value) for key, value in result["data_schema"].schema.items()
    }
    assert result["last_step"] is True
    assert fields["pv_factor"][0].default is vol.UNDEFINED
    assert not (fields["pv_factor"][0].description or {}).get("suggested_value")
    assert fields["pv_factor"][1].config["suffix"] == "g CO₂e/kWh"
    assert ("battery_factor" in fields) is battery
    if battery:
        assert fields["battery_factor"][1].config["suffix"] == "g CO₂e/kWh"
    assert fields["grid_max_age_minutes"][0].description["suggested_value"] == 60
    verified = False

    async def verify_before_creation(bootstrap_hass: HomeAssistant) -> str:
        nonlocal verified
        locator = await async_reserve_bootstrap(bootstrap_hass)
        assert not hass.config_entries.async_entries(DOMAIN)
        verified = True
        return locator

    with patch(
        "custom_components.co2saver.factor_flow.async_reserve_bootstrap",
        side_effect=verify_before_creation,
    ):
        result = await _configure(hass, result, _factors(sites[0], battery=battery))
    assert verified
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.unique_id is None
    assert entry.options == {}
    data = dict(entry.data)
    assert json.loads(json.dumps(data)) == data
    assert data["topology"] == topology
    assert data["consumption"]["mode"] == mode
    assert data["factors"]["pv_factor"] == "40"
    assert data["factors"]["grid_intensity_source"] == sites[0]["grid_intensity"].id
    assert ("battery_factor" in data["factors"]) is battery
    if battery:
        assert data["factors"]["battery_factor"] == "20.5"
    assert len(data["storage_id"]) == 32
    assert not {"active_generation", "generation", "initialized"} & data.keys()
    await hass.async_block_till_done()
    _assert_no_reservations(hass)


@pytest.mark.parametrize(
    "missing", ["pv_factor", "battery_factor", "grid_intensity_source"]
)
async def test_required_factor_fields(
    hass: HomeAssistant,
    sites: list[dict[str, er.RegistryEntry]],
    missing: str,
) -> None:
    """Required factors and source never receive silent defaults or persistence."""
    result = await _to_factors(hass, sites[0], battery=True)
    data = _factors(sites[0], battery=True)
    del data[missing]
    with patch(
        "custom_components.co2saver.factor_flow.async_reserve_bootstrap"
    ) as bootstrap:
        with pytest.raises(InvalidData) as error:
            await _configure(hass, result, data)
        assert error.value.path == [missing]
        bootstrap.assert_not_called()
    assert not hass.config_entries.async_entries(DOMAIN)
    hass.config_entries.flow.async_abort(result["flow_id"])


@pytest.mark.parametrize(
    ("value", "error"),
    [
        ("-0.001", "factor_out_of_range"),
        ("5000.000000000001", "factor_out_of_range"),
        ("40,1", "invalid_decimal_separator"),
        ("NaN", "invalid_number"),
        ("Infinity", "invalid_number"),
        ("4e1", "invalid_number"),
        (" 40", "invalid_number"),
        ("", "required"),
    ],
)
async def test_invalid_factors_are_retryable(
    hass: HomeAssistant,
    sites: list[dict[str, er.RegistryEntry]],
    value: str,
    error: str,
) -> None:
    """Bad exact text stays on the factor form without reserving a store."""
    result = await _to_factors(hass, sites[0])
    data = _factors(sites[0]) | {"pv_factor": value}
    with patch(
        "custom_components.co2saver.factor_flow.async_reserve_bootstrap"
    ) as bootstrap:
        result = await _configure(hass, result, data)
        assert result["errors"] == {"pv_factor": error}
        marker = next(
            key for key in result["data_schema"].schema if str(key) == "pv_factor"
        )
        assert marker.description["suggested_value"] == value
        bootstrap.assert_not_called()
    result = await _configure(hass, result, _factors(sites[0]))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    _assert_no_reservations(hass)


@pytest.mark.parametrize("value", ["0", "-0.000", "5000"])
async def test_factor_boundaries(
    hass: HomeAssistant,
    sites: list[dict[str, er.RegistryEntry]],
    value: str,
) -> None:
    """Both independent lifecycle factors accept inclusive exact endpoints."""
    result = await _to_factors(hass, sites[0], battery=True)
    result = await _configure(
        hass,
        result,
        _factors(sites[0], battery=True)
        | {
            "pv_factor": value,
            "battery_factor": value,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    expected = "0" if value.startswith("-0") else value
    assert result["data"]["factors"]["pv_factor"] == expected
    assert result["data"]["factors"]["battery_factor"] == expected


async def test_options_update_data_without_overwriting_options(
    hass: HomeAssistant,
    sites: list[dict[str, er.RegistryEntry]],
) -> None:
    """Options commits use authoritative data and preserve the immutable locator."""
    entry = await _create(hass, sites[0])
    locator = entry.data["storage_id"]
    opaque = {"unrelated": {"preserve": True}}
    hass.config_entries.async_update_entry(entry, options=opaque)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await _consumers(hass, result, sites[0], "aggregate_shares")
    with patch.object(hass.config_entries, "async_schedule_reload") as reload_entry:
        result = await _configure(
            hass, result, _factors(sites[0]) | {"pv_factor": "55.5"}
        )
        reload_entry.assert_called_once_with(entry.entry_id)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "options_saved"
    assert entry.data["factors"]["pv_factor"] == "55.5"
    assert entry.data["storage_id"] == locator
    assert dict(entry.options) == opaque
    _assert_no_reservations(hass)


async def test_reconfigure_preserves_locator_and_changes_plant(
    hass: HomeAssistant,
    sites: list[dict[str, er.RegistryEntry]],
) -> None:
    """Replacing upstream meters changes plant identity without relocating state."""
    entry = await _create(hass, sites[0])
    before = deepcopy(dict(entry.data))
    result = await _to_factors(hass, sites[1], "smart_meter", entry=entry)
    with patch.object(hass.config_entries, "async_schedule_reload") as reload_entry:
        result = await _configure(hass, result, _factors(sites[1]))
        reload_entry.assert_called_once_with(entry.entry_id)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.unique_id is None
    assert entry.data["storage_id"] == before["storage_id"]
    assert entry.data["plant_key"] != before["plant_key"]
    assert entry.data["sources"]["grid_import"] == sites[1]["grid_import"].id
    _assert_no_reservations(hass)


async def test_options_reject_stale_configuration(
    hass: HomeAssistant,
    sites: list[dict[str, er.RegistryEntry]],
) -> None:
    """An old options draft cannot silently revert a committed sensor change."""
    entry = await _create(hass, sites[0])
    options = await hass.config_entries.options.async_init(entry.entry_id)
    options = await _consumers(hass, options, sites[0], "aggregate_shares")
    reconfigure = await _to_factors(hass, sites[1], entry=entry)
    with patch.object(hass.config_entries, "async_schedule_reload"):
        await _configure(hass, reconfigure, _factors(sites[1]))
        changed = deepcopy(dict(entry.data))
        result = await _configure(
            hass, options, _factors(sites[0]) | {"pv_factor": "55"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "configuration_changed"
    assert dict(entry.data) == changed
    _assert_no_reservations(hass)


@pytest.mark.parametrize("other_kind", ["user", "reconfigure"])
async def test_user_bootstrap_serializes_competing_target(
    hass: HomeAssistant,
    sites: list[dict[str, er.RegistryEntry]],
    other_kind: str,
) -> None:
    """A pending verified bootstrap excludes a user or reconfigure competitor."""
    from custom_components.co2saver.factor_flow import (  # noqa: PLC0415
        async_reserve_bootstrap,
    )

    existing = await _create(hass, sites[1]) if other_kind == "reconfigure" else None
    original = deepcopy(dict(existing.data)) if existing is not None else None
    first = await _to_factors(hass, sites[0])
    second = await _to_factors(hass, sites[0], entry=existing)
    entered, release, competing = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def paused_bootstrap(bootstrap_hass: HomeAssistant) -> str:
        locator = await async_reserve_bootstrap(bootstrap_hass)
        entered.set()
        await release.wait()
        return locator

    async def compete() -> ConfigFlowResult:
        competing.set()
        return await _configure(hass, second, _factors(sites[0]))

    with patch(
        "custom_components.co2saver.factor_flow.async_reserve_bootstrap",
        side_effect=paused_bootstrap,
    ):
        winner = asyncio.create_task(_configure(hass, first, _factors(sites[0])))
        await entered.wait()
        loser = asyncio.create_task(compete())
        await competing.wait()
        release.set()
        first_result, second_result = await asyncio.gather(winner, loser)
    assert first_result["type"] is FlowResultType.CREATE_ENTRY
    assert second_result["type"] is FlowResultType.ABORT
    assert second_result["reason"] in {"already_in_progress", "already_configured"}
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == (2 if existing is not None else 1)
    assert len({entry.data["plant_key"] for entry in entries}) == len(entries)
    if existing is not None:
        assert dict(existing.data) == original
    await hass.async_block_till_done()
    _assert_no_reservations(hass)


@pytest.mark.parametrize("same_entry", [False, True])
async def test_concurrent_reconfigure_commits(
    hass: HomeAssistant,
    sites: list[dict[str, er.RegistryEntry]],
    *,
    same_entry: bool,
) -> None:
    """Concurrent edits cannot share a plant or overwrite one owner's new data."""
    from custom_components.co2saver.bootstrap import manifest_lock  # noqa: PLC0415

    first_entry = await _create(hass, sites[0])
    second_entry = first_entry if same_entry else await _create(hass, sites[1])
    second_sources = sites[1] if same_entry else sites[2]
    first = await _to_factors(hass, sites[2], entry=first_entry)
    second = await _to_factors(hass, second_sources, entry=second_entry)
    with patch.object(hass.config_entries, "async_schedule_reload"):
        async with manifest_lock(hass):
            first_task = asyncio.create_task(
                _configure(hass, first, _factors(sites[2]))
            )
            second_task = asyncio.create_task(
                _configure(hass, second, _factors(second_sources))
            )
        first_result, second_result = await asyncio.gather(first_task, second_task)
    assert first_result["reason"] == "reconfigure_successful"
    assert second_result["type"] is FlowResultType.ABORT
    assert second_result["reason"] == (
        "configuration_changed" if same_entry else "already_configured"
    )
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len({entry.data["plant_key"] for entry in entries}) == len(entries)
    _assert_no_reservations(hass)


@pytest.mark.parametrize("failure", ["save", "readback"])
async def test_bootstrap_failure_releases_reservations(
    hass: HomeAssistant,
    sites: list[dict[str, er.RegistryEntry]],
    failure: str,
) -> None:
    """Failed saves and failed verification free the target for a new attempt."""
    from custom_components.co2saver.measurement.storage import (  # noqa: PLC0415
        VerifiedAtomicStoreVerificationError,
    )

    result = await _to_factors(hass, sites[0])
    failures = {
        "save": OSError("simulated write failure"),
        "readback": VerifiedAtomicStoreVerificationError("simulated mismatch"),
    }
    with patch(
        "custom_components.co2saver.factor_flow.async_reserve_bootstrap",
        side_effect=failures[failure],
    ):
        result = await _configure(hass, result, _factors(sites[0]))
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": "storage_failed"}
    assert not hass.config_entries.async_entries(DOMAIN)
    _assert_no_reservations(hass)
    hass.config_entries.flow.async_abort(result["flow_id"])
    retry = await _to_factors(hass, sites[0])
    created = await _configure(hass, retry, _factors(sites[0]))
    assert created["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    _assert_no_reservations(hass)


async def test_cancelled_pending_bootstrap_releases_reservations(
    hass: HomeAssistant,
    sites: list[dict[str, er.RegistryEntry]],
) -> None:
    """Cancelling an actively awaited bootstrap leaves no reservation or entry."""
    from custom_components.co2saver.factor_flow import (  # noqa: PLC0415
        async_reserve_bootstrap,
    )

    entered, wait_forever = asyncio.Event(), asyncio.Event()
    result = await _to_factors(hass, sites[0])

    async def paused(bootstrap_hass: HomeAssistant) -> str:
        locator = await async_reserve_bootstrap(bootstrap_hass)
        entered.set()
        await wait_forever.wait()
        return locator

    with patch(
        "custom_components.co2saver.factor_flow.async_reserve_bootstrap",
        side_effect=paused,
    ):
        task = asyncio.create_task(_configure(hass, result, _factors(sites[0])))
        await entered.wait()
        assert reservations(hass).targets
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not hass.config_entries.async_entries(DOMAIN)
    _assert_no_reservations(hass)
    hass.config_entries.flow.async_abort(result["flow_id"])
    await _create(hass, sites[0])
    _assert_no_reservations(hass)


async def test_reconfigure_wins_over_concurrent_user(
    hass: HomeAssistant,
    sites: list[dict[str, er.RegistryEntry]],
) -> None:
    """A user draft cannot create a second entry after reconfigure claims its plant."""
    from custom_components.co2saver.bootstrap import manifest_lock  # noqa: PLC0415

    entry = await _create(hass, sites[1])
    reconfigure = await _to_factors(hass, sites[0], entry=entry)
    user = await _to_factors(hass, sites[0])
    with (
        patch.object(hass.config_entries, "async_schedule_reload"),
        patch(
            "custom_components.co2saver.factor_flow.async_reserve_bootstrap"
        ) as bootstrap,
    ):
        async with manifest_lock(hass):
            edit_task = asyncio.create_task(
                _configure(hass, reconfigure, _factors(sites[0]))
            )
            user_task = asyncio.create_task(_configure(hass, user, _factors(sites[0])))
        edit_result, user_result = await asyncio.gather(edit_task, user_task)
        bootstrap.assert_not_called()
    assert edit_result["reason"] == "reconfigure_successful"
    assert user_result["reason"] == "already_configured"
    assert hass.config_entries.async_entries(DOMAIN) == [entry]
    _assert_no_reservations(hass)


@pytest.mark.parametrize("role", ["grid_import", "grid_intensity"])
async def test_source_removed_during_bootstrap_prevents_creation(
    hass: HomeAssistant,
    sites: list[dict[str, er.RegistryEntry]],
    role: str,
) -> None:
    """The final live-source check closes the persistence-await removal race."""
    from custom_components.co2saver.factor_flow import (  # noqa: PLC0415
        async_reserve_bootstrap,
    )

    result = await _to_factors(hass, sites[0])

    async def removed_during_save(bootstrap_hass: HomeAssistant) -> str:
        locator = await async_reserve_bootstrap(bootstrap_hass)
        er.async_get(hass).async_remove(sites[0][role].entity_id)
        return locator

    with patch(
        "custom_components.co2saver.factor_flow.async_reserve_bootstrap",
        side_effect=removed_during_save,
    ):
        result = await _configure(hass, result, _factors(sites[0]))
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == (
        {"base": "invalid_source_vector"}
        if role == "grid_import"
        else {"grid_intensity_source": "source_not_registered"}
    )
    assert not hass.config_entries.async_entries(DOMAIN)
    _assert_no_reservations(hass)
    hass.config_entries.flow.async_abort(result["flow_id"])


@pytest.mark.parametrize("problem", ["unavailable", "unit", "removed"])
async def test_changed_vector_is_rejected_before_bootstrap(
    hass: HomeAssistant,
    sites: list[dict[str, er.RegistryEntry]],
    problem: str,
) -> None:
    """A formerly accepted source draft is fully revalidated before reservation."""
    result = await _to_factors(hass, sites[0])
    source = sites[0]["grid_import"]
    state = hass.states.get(source.entity_id)
    assert state is not None
    if problem == "removed":
        er.async_get(hass).async_remove(source.entity_id)
    else:
        attributes = dict(state.attributes)
        if problem == "unit":
            attributes["unit_of_measurement"] = "W"
        hass.states.async_set(
            source.entity_id,
            "unavailable" if problem == "unavailable" else state.state,
            attributes,
        )
    with patch(
        "custom_components.co2saver.factor_flow.async_reserve_bootstrap"
    ) as bootstrap:
        result = await _configure(hass, result, _factors(sites[0]))
        bootstrap.assert_not_called()
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_source_vector"}
    assert not hass.config_entries.async_entries(DOMAIN)
    _assert_no_reservations(hass)
    hass.config_entries.flow.async_abort(result["flow_id"])


async def test_create_failure_releases_verified_bootstrap_reservation(
    hass: HomeAssistant,
    sites: list[dict[str, er.RegistryEntry]],
) -> None:
    """A failure between verified bootstrap and CREATE_ENTRY permits a clean retry."""
    from custom_components.co2saver.config_flow import (  # noqa: PLC0415
        Co2SaverConfigFlow,
    )

    result = await _to_factors(hass, sites[0])
    with patch.object(
        Co2SaverConfigFlow,
        "async_create_entry",
        side_effect=ValueError("create failed"),
    ):
        result = await _configure(hass, result, _factors(sites[0]))
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "storage_failed"}
    assert not hass.config_entries.async_entries(DOMAIN)
    _assert_no_reservations(hass)
    result = await _configure(hass, result, _factors(sites[0]))
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1
    await hass.async_block_till_done()
    _assert_no_reservations(hass)
