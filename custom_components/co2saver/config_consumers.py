# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Validate consumer allocation drafts without performing accounting or I/O."""

from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal
from fractions import Fraction
from typing import TYPE_CHECKING, TypedDict, cast

from custom_components.co2saver.config_sources import validate_energy_sources

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


_AGGREGATE_SHARES = "aggregate_shares"
_SEPARATE_METERS = "separate_meters"
_MODES = (_AGGREGATE_SHARES, _SEPARATE_METERS)
_PLAN_FIELDS = frozenset(("mode", "household_id", "household_source", "consumers"))
_AGGREGATE_INPUT_FIELDS = frozenset(("name", "share_percent"))
_SEPARATE_INPUT_FIELDS = frozenset(("name", "source"))
_AGGREGATE_DRAFT_FIELDS = frozenset(("consumer_id", "name", "share"))
_SEPARATE_DRAFT_FIELDS = frozenset(("consumer_id", "name", "source"))
_EXISTING_SOURCE_ROLES = frozenset(
    (
        "pv_generation",
        "pv_plausibility",
        "grid_import",
        "grid_export",
        "battery_charge",
        "battery_discharge",
    )
)
_PLAIN_DECIMAL = re.compile(r"-?[0-9]+(?:\.[0-9]+)?\Z")
_CANONICAL_UUID_HEX = re.compile(r"[0-9a-f]{32}\Z")
_ZERO = Decimal(0)
_ONE = Decimal(1)
_ONE_HUNDRED = Decimal(100)


class _NamedConsumer(TypedDict):
    """Common canonical fields of an additional consumer."""

    name: str


class AggregateConsumerCandidate(_NamedConsumer):
    """Validated aggregate-mode editor result before ID assignment."""

    share: str


class SeparateConsumerCandidate(_NamedConsumer):
    """Validated separate-meter editor result before source resolution."""

    source: str


type ConsumerCandidate = AggregateConsumerCandidate | SeparateConsumerCandidate


class _IdentifiedConsumer(_NamedConsumer):
    """Common persisted-draft identity fields."""

    consumer_id: str


class AggregateConsumerDraft(_IdentifiedConsumer):
    """One exact share of an aggregate local-load meter."""

    share: str


class SeparateConsumerDraft(_IdentifiedConsumer):
    """One separately measured, non-overlapping local load."""

    source: str


type ConsumerDraft = AggregateConsumerDraft | SeparateConsumerDraft


class ConsumptionDraft(TypedDict):
    """Serializable canonical local-consumption configuration."""

    mode: str
    household_id: str
    household_source: str
    consumers: list[ConsumerDraft]


def _parse_decimal(value: object) -> tuple[Decimal | None, str | None]:
    """Parse exact plain decimal input without binary floating-point coercion."""
    if type(value) is int:
        return Decimal(value), None
    if type(value) is not str:
        return None, "invalid_number"
    if "," in value:
        return None, "invalid_decimal_separator"
    if _PLAIN_DECIMAL.fullmatch(value) is None:
        return None, "invalid_number"
    return Decimal(value), None


def _canonical_decimal(value: Decimal) -> str:
    """Format a finite plain Decimal exactly without exponent or rounding."""
    if value.is_zero():
        return "0"
    canonical = format(value, "f")
    if "." in canonical:
        canonical = canonical.rstrip("0").rstrip(".")
    return canonical


def _ratio_from_percent(value: Decimal) -> str:
    """Shift an exact percentage by two decimal places without context rounding."""
    sign, digits, exponent = value.as_tuple()
    if not isinstance(exponent, int):  # pragma: no cover - parser guarantees finite
        message = "consumer percentage must be finite"
        raise TypeError(message)
    return _canonical_decimal(Decimal((sign, digits, exponent - 2)))


def _canonical_name(value: object) -> tuple[str | None, str | None]:
    """Trim a meaningful display label without assigning it semantic identity."""
    if value is None or value == "":
        return None, "required"
    if type(value) is not str or not (name := value.strip()):
        return None, "invalid_name"
    return name, None


def _selection_error(value: object) -> str | None:
    """Validate the shape shared by entity IDs and entity-registry UUIDs."""
    if value is None or value == "":
        return "required"
    if type(value) is not str or value != value.strip():
        return "invalid_selection"
    return None


def _unexpected_errors(
    user_input: Mapping[str, object],
    allowed: frozenset[str],
) -> dict[str, str]:
    """Return concrete errors for every field outside one exact input shape."""
    return {
        str(field): "unexpected_field" for field in user_input if field not in allowed
    }


def validate_consumer_input(
    mode: str,
    user_input: Mapping[str, object],
) -> tuple[ConsumerCandidate | None, dict[str, str]]:
    """Validate one add/edit form before the caller assigns a stable UUID."""
    if type(mode) is not str or mode not in _MODES:
        return None, {"base": "invalid_consumption_mode"}
    if mode == _AGGREGATE_SHARES:
        return _validate_aggregate_input(user_input)
    return _validate_separate_input(user_input)


def _validate_aggregate_input(
    user_input: Mapping[str, object],
) -> tuple[AggregateConsumerCandidate | None, dict[str, str]]:
    """Validate one aggregate-share add/edit form."""
    errors = _unexpected_errors(user_input, _AGGREGATE_INPUT_FIELDS)
    name, name_error = _canonical_name(user_input.get("name"))
    if name_error is not None:
        errors["name"] = name_error
    share, share_error = _parse_percentage(user_input.get("share_percent"))
    if share_error is not None:
        errors["share_percent"] = share_error
    if errors:
        return None, errors
    if name is None or share is None:  # pragma: no cover - helper contract
        return None, {"base": "invalid_consumer_plan"}
    return AggregateConsumerCandidate(name=name, share=share), {}


def _validate_separate_input(
    user_input: Mapping[str, object],
) -> tuple[SeparateConsumerCandidate | None, dict[str, str]]:
    """Validate one separate-meter add/edit form."""
    errors = _unexpected_errors(user_input, _SEPARATE_INPUT_FIELDS)
    name, name_error = _canonical_name(user_input.get("name"))
    if name_error is not None:
        errors["name"] = name_error
    source = user_input.get("source")
    if (source_error := _selection_error(source)) is not None:
        errors["source"] = source_error
    if errors:
        return None, errors
    if name is None or type(source) is not str:  # pragma: no cover - helper contract
        return None, {"base": "invalid_consumer_plan"}
    return SeparateConsumerCandidate(name=name, source=source), {}


def _parse_percentage(value: object) -> tuple[str | None, str | None]:
    """Canonicalize one inclusive 0..100 editor percentage as a ratio."""
    if value is None or value == "":
        return None, "required"
    percentage, error = _parse_decimal(value)
    if error is not None:
        return None, error
    if percentage is None:  # pragma: no cover - parser contract
        return None, "invalid_number"
    if not _ZERO <= percentage <= _ONE_HUNDRED:
        return None, "share_out_of_range"
    return _ratio_from_percent(percentage), None


def _is_canonical_uuid(value: object) -> bool:
    """Return whether a value is the stable lowercase UUID-hex representation."""
    return type(value) is str and _CANONICAL_UUID_HEX.fullmatch(value) is not None


def _consumer_prefix(index: int, consumer_id: object) -> str:
    """Prefer stable identity in nested errors and fall back to list position."""
    if _is_canonical_uuid(consumer_id):
        return f"consumer:{consumer_id}"
    return f"consumer_index:{index}"


def _common_consumer_fields(
    row: Mapping[str, object],
    index: int,
    allowed: frozenset[str],
) -> tuple[str | None, str | None, str, dict[str, str]]:
    """Validate one row's stable identity, label, and mutually exclusive shape."""
    raw_id = row.get("consumer_id")
    prefix = _consumer_prefix(index, raw_id)
    errors = {
        f"{prefix}:{field}": error
        for field, error in _unexpected_errors(row, allowed).items()
    }

    consumer_id: str | None = None
    if raw_id is None or raw_id == "":
        errors[f"{prefix}:consumer_id"] = "required"
    elif not _is_canonical_uuid(raw_id):
        errors[f"{prefix}:consumer_id"] = "invalid_consumer_id"
    else:
        consumer_id = cast("str", raw_id)

    name, name_error = _canonical_name(row.get("name"))
    if name_error is not None:
        errors[f"{prefix}:name"] = name_error
    return consumer_id, name, prefix, errors


def _parse_aggregate_consumer(
    row: Mapping[str, object],
    index: int,
) -> tuple[AggregateConsumerDraft | None, Fraction | None, dict[str, str]]:
    """Validate one identified aggregate-share row."""
    consumer_id, name, prefix, errors = _common_consumer_fields(
        row,
        index,
        _AGGREGATE_DRAFT_FIELDS,
    )
    raw_share = row.get("share")
    if raw_share is None or raw_share == "":
        errors[f"{prefix}:share"] = "required"
        share = None
    else:
        decimal_share, error = _parse_decimal(raw_share)
        if error is not None:
            errors[f"{prefix}:share"] = error
            share = None
        elif decimal_share is None:  # pragma: no cover - parser contract
            errors[f"{prefix}:share"] = "invalid_number"
            share = None
        elif not _ZERO <= decimal_share <= _ONE:
            errors[f"{prefix}:share"] = "share_out_of_range"
            share = None
        else:
            share = decimal_share

    if errors:
        return None, None, errors
    if consumer_id is None or name is None or share is None:  # pragma: no cover
        return None, None, {"base": "invalid_consumer_plan"}
    return (
        AggregateConsumerDraft(
            consumer_id=consumer_id,
            name=name,
            share=_canonical_decimal(share),
        ),
        Fraction(share),
        {},
    )


def _parse_separate_consumer(
    row: Mapping[str, object],
    index: int,
) -> tuple[SeparateConsumerDraft | None, dict[str, str]]:
    """Validate one identified separate-meter row before registry resolution."""
    consumer_id, name, prefix, errors = _common_consumer_fields(
        row,
        index,
        _SEPARATE_DRAFT_FIELDS,
    )
    source = row.get("source")
    if (source_error := _selection_error(source)) is not None:
        errors[f"{prefix}:source"] = source_error
    if errors:
        return None, errors
    if (
        consumer_id is None or name is None or type(source) is not str
    ):  # pragma: no cover - helper contract
        return None, {"base": "invalid_consumer_plan"}
    return SeparateConsumerDraft(
        consumer_id=consumer_id,
        name=name,
        source=source,
    ), {}


def _parse_consumers(
    mode: str,
    raw_consumers: list[object],
    household_id: str,
) -> tuple[list[ConsumerDraft], dict[str, str]]:
    """Validate every row without dropping malformed or zero-share consumers."""
    consumers: list[ConsumerDraft] = []
    shares: list[Fraction] = []
    errors: dict[str, str] = {}
    for index, value in enumerate(raw_consumers):
        if not isinstance(value, Mapping):
            errors[f"consumer_index:{index}"] = "invalid_consumer_plan"
            continue
        row = cast("Mapping[str, object]", value)
        consumer: ConsumerDraft | None
        if mode == _AGGREGATE_SHARES:
            aggregate, share, row_errors = _parse_aggregate_consumer(row, index)
            consumer = aggregate
            if share is not None:
                shares.append(share)
        else:
            separate, row_errors = _parse_separate_consumer(row, index)
            consumer = separate
        errors.update(row_errors)
        if consumer is not None:
            consumers.append(consumer)

    ids = [household_id, *(consumer["consumer_id"] for consumer in consumers)]
    if len(ids) != len(set(ids)):
        errors["consumers"] = "duplicate_consumer_id"
    if sum(shares, start=Fraction()) > 1:
        errors["consumers"] = "shares_exceed_total"
    return consumers, errors


def _plan_shape_errors(draft: Mapping[str, object]) -> dict[str, str]:
    """Validate the exact top-level plan shape before inspecting its rows."""
    errors = _unexpected_errors(draft, _PLAN_FIELDS)
    mode = draft.get("mode")
    if mode is None or mode == "":
        errors["mode"] = "required"
    elif type(mode) is not str or mode not in _MODES:
        errors["mode"] = "invalid_consumption_mode"

    household_id = draft.get("household_id")
    if household_id is None or household_id == "":
        errors["household_id"] = "required"
    elif not _is_canonical_uuid(household_id):
        errors["household_id"] = "invalid_consumer_id"

    if (source_error := _selection_error(draft.get("household_source"))) is not None:
        errors["household_source"] = source_error

    consumers = draft.get("consumers")
    if consumers is None:
        errors["consumers"] = "required"
    elif type(consumers) is not list:
        errors["consumers"] = "invalid_consumer_plan"
    return errors


def _existing_sources_are_complete(existing_sources: Mapping[str, str]) -> bool:
    """Validate the already staged PV/grid and optional battery role shape."""
    roles = set(existing_sources)
    if (
        not roles
        or any(type(role) is not str for role in roles)
        or not roles <= _EXISTING_SOURCE_ROLES
        or not {"grid_import", "grid_export"} <= roles
    ):
        return False
    if "pv_generation" in roles and "pv_plausibility" in roles:
        return False
    battery_roles = {"battery_charge", "battery_discharge"}
    return not (roles & battery_roles) or battery_roles <= roles


def _map_source_errors(
    source_errors: Mapping[str, str],
    load_role: str,
    consumer_ids: frozenset[str],
) -> dict[str, str]:
    """Map physical-vector errors back to stable consumer editor fields."""
    errors: dict[str, str] = {}
    for role, error in source_errors.items():
        if role == load_role:
            errors["household_source"] = error
        elif (
            role.startswith("consumer:")
            and role.removeprefix("consumer:") in consumer_ids
        ):
            errors[f"{role}:source"] = error
        else:
            errors["base"] = "invalid_source_vector"
    return errors


def _validate_and_resolve_sources(  # noqa: PLR0913
    hass: HomeAssistant,
    existing_sources: Mapping[str, str],
    *,
    mode: str,
    household_id: str,
    household_source: str,
    consumers: list[ConsumerDraft],
) -> tuple[ConsumptionDraft | None, dict[str, str]]:
    """Validate the complete physical vector and canonicalize its identities."""
    load_role = "local_load" if mode == _AGGREGATE_SHARES else "household"
    consumer_ids = frozenset(consumer["consumer_id"] for consumer in consumers)

    selections: dict[str, object] = dict(existing_sources)
    selections[load_role] = household_source
    if mode == _SEPARATE_METERS:
        for consumer in cast("list[SeparateConsumerDraft]", consumers):
            selections[f"consumer:{consumer['consumer_id']}"] = consumer["source"]

    resolved, source_errors = validate_energy_sources(hass, selections)
    if source_errors:
        return None, _map_source_errors(source_errors, load_role, consumer_ids)
    if resolved is None:  # pragma: no cover - shared validator success contract
        return None, {"base": "invalid_source_vector"}

    canonical_consumers: list[ConsumerDraft]
    if mode == _AGGREGATE_SHARES:
        canonical_consumers = consumers
    else:
        canonical_consumers = [
            SeparateConsumerDraft(
                consumer_id=consumer["consumer_id"],
                name=consumer["name"],
                source=resolved[f"consumer:{consumer['consumer_id']}"],
            )
            for consumer in cast("list[SeparateConsumerDraft]", consumers)
        ]
    return (
        ConsumptionDraft(
            mode=mode,
            household_id=household_id,
            household_source=resolved[load_role],
            consumers=canonical_consumers,
        ),
        {},
    )


def validate_consumption_selection(
    hass: HomeAssistant,
    existing_sources: Mapping[str, str],
    draft: Mapping[str, object],
) -> tuple[ConsumptionDraft | None, dict[str, str]]:
    """Validate and canonicalize a complete side-effect-free consumption plan."""
    errors = _plan_shape_errors(draft)
    if errors:
        return None, errors

    mode = draft.get("mode")
    household_id = draft.get("household_id")
    household_source = draft.get("household_source")
    raw_consumers = draft.get("consumers")
    if (
        type(mode) is not str
        or type(household_id) is not str
        or type(household_source) is not str
        or type(raw_consumers) is not list
    ):  # pragma: no cover - shape validation contract
        return None, {"base": "invalid_consumer_plan"}

    consumers, errors = _parse_consumers(mode, raw_consumers, household_id)
    if errors:
        return None, errors
    if not _existing_sources_are_complete(existing_sources):
        return None, {"base": "invalid_source_vector"}
    return _validate_and_resolve_sources(
        hass,
        existing_sources,
        mode=mode,
        household_id=household_id,
        household_source=household_source,
        consumers=consumers,
    )


__all__ = (
    "AggregateConsumerCandidate",
    "AggregateConsumerDraft",
    "ConsumerCandidate",
    "ConsumerDraft",
    "ConsumptionDraft",
    "SeparateConsumerCandidate",
    "SeparateConsumerDraft",
    "validate_consumer_input",
    "validate_consumption_selection",
)
