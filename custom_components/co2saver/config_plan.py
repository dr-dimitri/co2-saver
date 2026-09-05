# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Canonical, complete measurement plans and prospective segment identities."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from decimal import Decimal
from fractions import Fraction
from typing import TYPE_CHECKING, Never, cast

from .config_factors import (
    canonical_decimal,
    parse_exact_decimal,
    validate_factor_selection,
)
from .config_sources import validate_energy_sources
from .measurement.models import EnergySourceIdentity

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


_MAX_AGE_MINUTES = 1440
_UUID = re.compile(r"[0-9a-f]{32}\Z")
_PLAN_FIELDS = frozenset(
    (
        "topology",
        "sources",
        "plant_key",
        "synchronous_sources_confirmed",
        "battery",
        "consumption",
        "factors",
    )
)
_BATTERY_FIELDS = frozenset(
    (
        "battery_id",
        "charge_source",
        "discharge_source",
        "usable_capacity_kwh",
        "round_trip_efficiency",
    )
)
_CONSUMPTION_FIELDS = frozenset(
    ("mode", "household_id", "household_source", "consumers")
)


def _invalid(field: str) -> Never:
    """Make malformed plan failures explicit without exposing source values."""
    message = f"invalid measurement plan field: {field}"
    raise ValueError(message)


def _mapping(value: object, field: str) -> Mapping[str, object]:
    """Require a string-keyed mapping before inspecting persisted configuration."""
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        _invalid(field)
    return cast("Mapping[str, object]", value)


def _shape(value: Mapping[str, object], fields: frozenset[str], field: str) -> None:
    """Reject missing and surplus keys instead of silently dropping semantics."""
    if set(value) != fields:
        _invalid(field)


def _uuid(value: object, field: str) -> str:
    """Require canonical identities, never mutable entity IDs or display names."""
    if type(value) is not str or _UUID.fullmatch(value) is None:
        _invalid(field)
    return value


def _decimal(
    value: object, field: str, minimum: str, maximum: str, *, exclude_min: bool = False
) -> str:
    """Canonicalize a bounded exact decimal without changing its value."""
    number, _error = parse_exact_decimal(value)
    if number is None or not Decimal(minimum) <= number <= Decimal(maximum):
        _invalid(field)
    if exclude_min and number == Decimal(minimum):
        _invalid(field)
    return canonical_decimal(number)


def _canonical_sources(topology: object, raw: object) -> dict[str, str]:
    """Require the complete accepted topology and role-preserving registry IDs."""
    sources = _mapping(raw, "sources")
    if topology == "inverter":
        _shape(
            sources,
            frozenset(("pv_generation", "grid_import", "grid_export")),
            "sources",
        )
    elif topology == "smart_meter":
        required = {"grid_import", "grid_export"}
        if not required <= set(sources) <= required | {"pv_plausibility"}:
            _invalid("sources")
    else:
        _invalid("topology")
    return {role: _uuid(value, role) for role, value in sorted(sources.items())}


def _canonical_battery(raw: object) -> dict[str, str] | None:
    """Keep explicit battery identity, source directions, capacity, and efficiency."""
    if raw is None:
        return None
    battery = _mapping(raw, "battery")
    _shape(battery, _BATTERY_FIELDS, "battery")
    return {
        "battery_id": _uuid(battery["battery_id"], "battery_id"),
        "charge_source": _uuid(battery["charge_source"], "charge_source"),
        "discharge_source": _uuid(battery["discharge_source"], "discharge_source"),
        "usable_capacity_kwh": _decimal(
            battery["usable_capacity_kwh"], "usable_capacity_kwh", "0.1", "1000"
        ),
        "round_trip_efficiency": _decimal(
            battery["round_trip_efficiency"],
            "round_trip_efficiency",
            "0",
            "1",
            exclude_min=True,
        ),
    }


def _canonical_consumer(raw: object, mode: str) -> dict[str, str]:
    """Preserve a named consumer's stable identity and exact semantic assignment."""
    consumer = _mapping(raw, "consumer")
    semantic = "share" if mode == "aggregate_shares" else "source"
    _shape(consumer, frozenset(("consumer_id", "name", semantic)), "consumer")
    name = consumer["name"]
    if type(name) is not str or not name.strip():
        _invalid("name")
    return {
        "consumer_id": _uuid(consumer["consumer_id"], "consumer_id"),
        "name": name.strip(),
        semantic: (
            _decimal(consumer[semantic], "share", "0", "1")
            if semantic == "share"
            else _uuid(consumer[semantic], "source")
        ),
    }


def _canonical_consumption(raw: object) -> dict[str, object]:
    """Validate mutually exclusive allocation modes and non-overlapping IDs."""
    consumption = _mapping(raw, "consumption")
    _shape(consumption, _CONSUMPTION_FIELDS, "consumption")
    mode = consumption["mode"]
    if type(mode) is not str or mode not in ("aggregate_shares", "separate_meters"):
        _invalid("mode")
    household = _uuid(consumption["household_id"], "household_id")
    raw_consumers = consumption["consumers"]
    if type(raw_consumers) is not list:
        _invalid("consumers")
    consumers = sorted(
        (_canonical_consumer(consumer, mode) for consumer in raw_consumers),
        key=lambda consumer: consumer["consumer_id"],
    )
    ids = [household, *(consumer["consumer_id"] for consumer in consumers)]
    if len(ids) != len(set(ids)):
        _invalid("consumer_id")
    if (
        mode == "aggregate_shares"
        and sum(
            (Fraction(consumer["share"]) for consumer in consumers), start=Fraction()
        )
        > 1
    ):
        _invalid("share")
    return {
        "mode": mode,
        "household_id": household,
        "household_source": _uuid(consumption["household_source"], "household_source"),
        "consumers": consumers,
    }


def _canonical_factors(raw: object, *, with_battery: bool) -> dict[str, object]:
    """Validate persisted factor shape and exact technical boundaries offline."""
    factors = _mapping(raw, "factors")
    fields = {"grid_intensity_source", "grid_max_age_minutes", "pv_factor"}
    if with_battery:
        fields.add("battery_factor")
    _shape(factors, frozenset(fields), "factors")
    age = factors["grid_max_age_minutes"]
    if type(age) is not int or not 1 <= age <= _MAX_AGE_MINUTES:
        _invalid("grid_max_age_minutes")
    result: dict[str, object] = {
        "grid_intensity_source": _uuid(
            factors["grid_intensity_source"], "grid_intensity_source"
        ),
        "grid_max_age_minutes": age,
        "pv_factor": _decimal(factors["pv_factor"], "pv_factor", "0", "5000"),
    }
    if with_battery:
        result["battery_factor"] = _decimal(
            factors["battery_factor"], "battery_factor", "0", "5000"
        )
    return result


def _energy_bindings(plan: Mapping[str, object]) -> tuple[EnergySourceIdentity, ...]:
    """Flatten a validated plan into unambiguous role-owned energy sources."""
    sources = dict(cast("dict[str, str]", plan["sources"]))
    battery = cast("dict[str, str] | None", plan["battery"])
    if battery is not None:
        sources["battery_charge"] = battery["charge_source"]
        sources["battery_discharge"] = battery["discharge_source"]
    consumption = cast("dict[str, object]", plan["consumption"])
    aggregate = consumption["mode"] == "aggregate_shares"
    sources["local_load" if aggregate else "household"] = cast(
        "str", consumption["household_source"]
    )
    if not aggregate:
        for consumer in cast("list[dict[str, str]]", consumption["consumers"]):
            sources[f"consumer:{consumer['consumer_id']}"] = consumer["source"]
    return tuple(
        EnergySourceIdentity(role, identity)
        for role, identity in sorted(sources.items())
    )


def canonical_plan(draft: Mapping[str, object]) -> dict[str, object]:
    """Validate the complete plan and return a detached serializable canonical copy."""
    raw = _mapping(draft, "plan")
    fields = set(raw)
    if fields - {"storage_id"} != _PLAN_FIELDS:
        _invalid("plan")
    if "storage_id" in raw:
        _uuid(raw["storage_id"], "storage_id")
    if raw["synchronous_sources_confirmed"] is not True:
        _invalid("synchronous_sources_confirmed")
    sources = _canonical_sources(raw["topology"], raw["sources"])
    grid_pair = sorted((sources["grid_import"], sources["grid_export"]))
    plant_key = f"grid:{grid_pair[0]}:{grid_pair[1]}"
    if raw["plant_key"] != plant_key:
        _invalid("plant_key")
    battery = _canonical_battery(raw["battery"])
    factors = _canonical_factors(raw["factors"], with_battery=battery is not None)
    result: dict[str, object] = {
        "topology": raw["topology"],
        "sources": sources,
        "plant_key": plant_key,
        "synchronous_sources_confirmed": True,
        "battery": battery,
        "consumption": _canonical_consumption(raw["consumption"]),
        "factors": factors,
    }
    identities = [source.registry_id for source in _energy_bindings(result)]
    identities.append(cast("str", factors["grid_intensity_source"]))
    if len(identities) != len(set(identities)):
        _invalid("duplicate_source")
    return result


def source_bindings(draft: Mapping[str, object]) -> tuple[EnergySourceIdentity, ...]:
    """Return every energy role sorted deterministically by role name."""
    return _energy_bindings(canonical_plan(draft))


def all_source_registry_ids(draft: Mapping[str, object]) -> tuple[str, ...]:
    """Return all stable source identities, including the grid CO₂ source."""
    plan = canonical_plan(draft)
    factors = cast("dict[str, object]", plan["factors"])
    return tuple(
        sorted(
            (
                *(source.registry_id for source in _energy_bindings(plan)),
                cast("str", factors["grid_intensity_source"]),
            )
        )
    )


def consumer_ids(draft: Mapping[str, object]) -> tuple[str, ...]:
    """Return the stable IDs of the household and all configured consumers."""
    plan = canonical_plan(draft)
    consumption = cast("dict[str, object]", plan["consumption"])
    consumers = cast("list[dict[str, str]]", consumption["consumers"])
    return tuple(
        sorted(
            (
                cast("str", consumption["household_id"]),
                *(consumer["consumer_id"] for consumer in consumers),
            )
        )
    )


def segment_fingerprint(draft: Mapping[str, object]) -> str:
    """Hash only canonical accounting semantics, excluding labels and locators."""
    plan = canonical_plan(draft)
    plan.pop("plant_key")
    plan.pop("synchronous_sources_confirmed")
    consumption = cast("dict[str, object]", plan["consumption"])
    for consumer in cast("list[dict[str, str]]", consumption["consumers"]):
        consumer.pop("name")
    plan.update(fingerprint_version=1, adr_version="2.2", accounting_version=1)
    canonical = json.dumps(
        plan, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_current_plan(
    hass: HomeAssistant, draft: Mapping[str, object]
) -> dict[str, str]:
    """Revalidate every live input immediately before reserving a final commit."""
    try:
        plan = canonical_plan(draft)
    except ValueError:
        return {"base": "invalid_measurement_plan"}
    selections = {source.role: source.registry_id for source in _energy_bindings(plan)}
    _resolved, source_errors = validate_energy_sources(hass, selections)
    factors = cast("dict[str, object]", plan["factors"])
    _parameters, errors = validate_factor_selection(
        hass, plan["battery"] is not None, factors
    )
    if source_errors:
        errors["base"] = "invalid_source_vector"
    return errors


__all__ = (
    "all_source_registry_ids",
    "canonical_plan",
    "consumer_ids",
    "segment_fingerprint",
    "source_bindings",
    "validate_current_plan",
)
