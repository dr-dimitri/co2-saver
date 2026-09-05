# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Complete plan boundaries and canonical prospective segment fingerprints."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from homeassistant.const import ATTR_UNIT_OF_MEASUREMENT
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.co2saver.config_plan import (
    all_source_registry_ids,
    canonical_plan,
    consumer_ids,
    segment_fingerprint,
    source_bindings,
    validate_current_plan,
)
from custom_components.co2saver.const import ATTR_CO2SAVER_PERIOD_END

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


def _id(index: int) -> str:
    """Make deterministic canonical identities distinct across all physical roles."""
    return f"{index:032x}"


def _draft() -> dict[str, Any]:
    """Build a full canonical inverter plan with battery and aggregate wallbox."""
    return {
        "topology": "inverter",
        "sources": {
            "pv_generation": _id(1),
            "grid_import": _id(2),
            "grid_export": _id(3),
        },
        "plant_key": f"grid:{_id(2)}:{_id(3)}",
        "synchronous_sources_confirmed": True,
        "battery": {
            "battery_id": _id(10),
            "charge_source": _id(4),
            "discharge_source": _id(5),
            "usable_capacity_kwh": "13.5",
            "round_trip_efficiency": "0.9",
        },
        "consumption": {
            "mode": "aggregate_shares",
            "household_id": _id(11),
            "household_source": _id(6),
            "consumers": [
                {"consumer_id": _id(13), "name": "Wallbox", "share": "0.2"},
                {"consumer_id": _id(12), "name": "Wärmepumpe", "share": "0.1"},
            ],
        },
        "factors": {
            "grid_intensity_source": _id(7),
            "grid_max_age_minutes": 60,
            "pv_factor": "40",
            "battery_factor": "12",
        },
    }


def test_full_plan_is_detached_serializable_and_complete() -> None:
    """All required physical roles and consumers retain stable identities."""
    draft = _draft()
    original = deepcopy(draft)
    draft["storage_id"] = _id(20)
    plan = canonical_plan(draft)
    assert "storage_id" not in plan
    assert json.loads(json.dumps(plan)) == plan
    assert segment_fingerprint(plan) == segment_fingerprint(draft)
    assert consumer_ids(plan) == (_id(11), _id(12), _id(13))
    assert all_source_registry_ids(plan) == tuple(_id(index) for index in range(1, 8))
    assert {source.role: source.registry_id for source in source_bindings(plan)} == {
        "pv_generation": _id(1),
        "grid_import": _id(2),
        "grid_export": _id(3),
        "battery_charge": _id(4),
        "battery_discharge": _id(5),
        "local_load": _id(6),
    }
    draft.pop("storage_id")
    assert draft == original


def test_nonsemantic_edits_and_exact_decimal_spelling_preserve_segment() -> None:
    """Names, ordering, leading/trailing zeros, and locators never reset a segment."""
    original = _draft()
    draft = deepcopy(original)
    draft["consumption"]["consumers"].reverse()
    draft["consumption"]["consumers"][0]["name"] = "  Heat Pump  "
    draft["consumption"]["consumers"][0]["share"] = "000.100000"
    draft["factors"]["pv_factor"] = "00040.00000"
    draft["battery"]["round_trip_efficiency"] = "000.90000"
    draft["storage_id"] = _id(90)
    assert segment_fingerprint(draft) == segment_fingerprint(original)
    assert canonical_plan(draft)["consumption"]["consumers"][0]["name"] == "Heat Pump"
    original["factors"]["pv_factor"] = "-0.000"
    draft["factors"]["pv_factor"] = "0"
    assert segment_fingerprint(draft) == segment_fingerprint(original)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("battery", "battery_id"), _id(30)),
        (("battery", "charge_source"), _id(30)),
        (("battery", "discharge_source"), _id(30)),
        (("battery", "usable_capacity_kwh"), "14"),
        (("battery", "round_trip_efficiency"), "0.900000000000000000000000000000001"),
        (("factors", "pv_factor"), "40.000000000000000000000000000000000001"),
        (("factors", "battery_factor"), "12.01"),
        (("factors", "grid_intensity_source"), _id(30)),
        (("factors", "grid_max_age_minutes"), 61),
        (("consumption", "household_id"), _id(30)),
        (("consumption", "household_source"), _id(30)),
        (("consumption", "consumers", 0, "consumer_id"), _id(30)),
        (("consumption", "consumers", 0, "share"), "0.3"),
        (("sources", "pv_generation"), _id(30)),
    ],
)
def test_every_accounting_semantic_changes_the_fingerprint(
    path: tuple[str | int, ...], value: object
) -> None:
    """Prospective changes include provenance, factors, ownership and exact shares."""
    original = _draft()
    edited = deepcopy(original)
    target = edited
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    assert segment_fingerprint(edited) != segment_fingerprint(original)


def test_swapped_grid_roles_preserve_plant_identity_but_change_segment() -> None:
    """Duplicate protection sees one grid pair while accounting retains directions."""
    original = _draft()
    edited = deepcopy(original)
    edited["sources"]["grid_import"], edited["sources"]["grid_export"] = (
        edited["sources"]["grid_export"],
        edited["sources"]["grid_import"],
    )
    assert canonical_plan(edited)["plant_key"] == original["plant_key"]
    assert segment_fingerprint(edited) != segment_fingerprint(original)


@pytest.mark.parametrize("with_pv_check", [False, True])
def test_smartmeter_separate_household_and_wallbox_without_storage(
    *,
    with_pv_check: bool,
) -> None:
    """The alternate topology contains exactly the nonoverlapping configured loads."""
    draft = _draft()
    draft["topology"] = "smart_meter"
    draft["sources"].pop("pv_generation")
    if with_pv_check:
        draft["sources"]["pv_plausibility"] = _id(1)
    draft["battery"] = None
    draft["factors"].pop("battery_factor")
    draft["consumption"]["mode"] = "separate_meters"
    draft["consumption"]["consumers"] = [
        {"consumer_id": _id(12), "name": "Wallbox", "source": _id(8)}
    ]
    roles = {source.role for source in source_bindings(draft)}
    assert roles == {
        "grid_import",
        "grid_export",
        "household",
        f"consumer:{_id(12)}",
    } | ({"pv_plausibility"} if with_pv_check else set())
    assert consumer_ids(draft) == (_id(11), _id(12))
    draft["consumption"]["consumers"] = []
    assert consumer_ids(draft) == (_id(11),)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("topology",), "invented"),
        (("plant_key",), "grid:incorrect"),
        (("synchronous_sources_confirmed",), False),
        (("sources",), []),
        (("sources",), {3: _id(1)}),
        (("sources",), {"grid_import": _id(2), "grid_export": _id(3)}),
        (("sources", "pv_plausibility"), _id(4)),
        (("sources", "grid_import"), "sensor.import"),
        (("sources", "grid_export"), _id(2)),
        (("battery",), {}),
        (("battery", "usable_capacity_kwh"), "0.09"),
        (("battery", "usable_capacity_kwh"), "1000.00001"),
        (("battery", "usable_capacity_kwh"), "NaN"),
        (("battery", "round_trip_efficiency"), "0"),
        (("battery", "round_trip_efficiency"), 0.9),
        (("battery", "round_trip_efficiency"), "1.01"),
        (("battery", "charge_source"), _id(2)),
        (("consumption", "mode"), "mixed"),
        (("consumption", "household_id"), _id(12)),
        (("consumption", "household_source"), _id(2)),
        (("consumption", "consumers"), None),
        (("consumption", "consumers", 0, "name"), "   "),
        (
            ("consumption", "consumers", 0, "share"),
            "0.900000000000000000000000000000001",
        ),
        (("consumption", "consumers", 0, "source"), _id(8)),
        (("factors", "grid_max_age_minutes"), True),
        (("factors", "grid_max_age_minutes"), 60.0),
        (("factors", "grid_max_age_minutes"), 0),
        (("factors", "grid_max_age_minutes"), 1441),
        (("factors", "grid_intensity_source"), _id(2)),
        (("factors", "pv_factor"), "-1"),
        (("factors", "battery_factor"), "5000.0000000000000000001"),
        (("storage_id",), "not-a-stable-id"),
        (("unexpected",), "discard-me"),
    ],
)
def test_malformed_or_inconsistent_plans_fail_closed(
    path: tuple[str | int, ...], value: object
) -> None:
    """A final commit cannot silently sanitize unsupported or duplicate semantics."""
    edited = _draft()
    target = edited
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    with pytest.raises(ValueError, match="invalid measurement plan"):
        canonical_plan(edited)


def test_incomplete_smartmeter_and_extra_battery_factor_are_rejected() -> None:
    """Optional PV checks cannot replace either required grid direction."""
    draft = _draft()
    draft["topology"] = "smart_meter"
    with pytest.raises(ValueError, match="sources"):
        canonical_plan(draft)
    draft["sources"].pop("pv_generation")
    draft["sources"].pop("grid_import")
    with pytest.raises(ValueError, match="sources"):
        canonical_plan(draft)
    draft = _draft()
    draft["battery"] = None
    with pytest.raises(ValueError, match="factors"):
        canonical_plan(draft)


def test_final_plan_revalidates_all_current_sources(
    hass: HomeAssistant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Earlier flow selections can become invalid before the final manifest commit."""
    now = datetime(2026, 9, 5, 12, tzinfo=UTC)
    monkeypatch.setattr(dt_util, "utcnow", lambda: now)
    draft = _draft()
    registry = er.async_get(hass)
    id_map = {}
    for source_id in all_source_registry_ids(draft):
        entry = registry.async_get_or_create("sensor", "plan_test", source_id)
        id_map[source_id] = entry.id
        attributes = (
            {ATTR_UNIT_OF_MEASUREMENT: "gCO2eq/kWh"}
            if source_id == _id(7)
            else {
                ATTR_UNIT_OF_MEASUREMENT: "kWh",
                "device_class": "energy",
                "state_class": "total_increasing",
                ATTR_CO2SAVER_PERIOD_END: now - timedelta(seconds=30),
            }
        )
        hass.states.async_set(
            entry.entity_id, "100", attributes, timestamp=now.timestamp()
        )
    draft = json.loads(json.dumps(draft).replace(_id(1), id_map[_id(1)]))
    for source_id, registry_id in id_map.items():
        draft = json.loads(json.dumps(draft).replace(source_id, registry_id))
    grid_pair = sorted(
        (draft["sources"]["grid_import"], draft["sources"]["grid_export"])
    )
    draft["plant_key"] = f"grid:{grid_pair[0]}:{grid_pair[1]}"
    assert validate_current_plan(hass, draft) == {}
    energy_entry = registry.async_get(draft["sources"]["pv_generation"])
    hass.states.async_remove(energy_entry.entity_id)
    assert validate_current_plan(hass, draft) == {"base": "invalid_source_vector"}
    grid_entry = registry.async_get(draft["factors"]["grid_intensity_source"])
    hass.states.async_set(
        grid_entry.entity_id,
        "unavailable",
        {ATTR_UNIT_OF_MEASUREMENT: "gCO2eq/kWh"},
        timestamp=now.timestamp(),
    )
    assert validate_current_plan(hass, draft) == {
        "base": "invalid_source_vector",
        "grid_intensity_source": "source_unavailable",
    }
    draft.pop("consumption")
    assert validate_current_plan(hass, draft) == {"base": "invalid_measurement_plan"}
