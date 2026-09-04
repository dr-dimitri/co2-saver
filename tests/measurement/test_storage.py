# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Tests for the measurement codec and generic verified HA persistence."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from typing import TYPE_CHECKING, cast

import pytest
from homeassistant.core import CoreState
from homeassistant.helpers.storage import Store

from custom_components.co2saver.domain import (
    ConsumerLoad,
    Energy,
    InverterIntervalInput,
    NormalizedInterval,
    RejectedInterval,
    loads_from_meters,
    normalize_interval,
)
from custom_components.co2saver.measurement.models import (
    CandidateBuffer,
    CounterSnapshot,
    EnergyCounterSample,
    EnergySourceIdentity,
    EnergyUnit,
    MeasurementPhase,
    MeasurementPipelineState,
    RawEnergyDeltaBatch,
)
from custom_components.co2saver.measurement.pipeline import advance_measurements
from custom_components.co2saver.measurement.storage import (
    MeasurementStateCodecError,
    VerifiedAtomicStore,
    VerifiedAtomicStoreConflictError,
    VerifiedAtomicStoreError,
    VerifiedAtomicStoreVerificationError,
    VerifiedAtomicStoreVersionError,
    decode_measurement_state,
    encode_measurement_state,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant

_START = datetime(2026, 9, 4, 12, tzinfo=UTC)
_SOURCES = (
    EnergySourceIdentity(role="pv", registry_id="registry-pv"),
    EnergySourceIdentity(role="grid_import", registry_id="registry-grid-import"),
)


class _MeasurementCodec:
    """Adapt the public measurement codec functions to ``StateCodec``."""

    @staticmethod
    def encode(state: MeasurementPipelineState) -> dict[str, object]:
        """Encode one measurement state."""
        return encode_measurement_state(state)

    @staticmethod
    def decode(payload: object) -> MeasurementPipelineState:
        """Decode one measurement state."""
        return decode_measurement_state(payload)


class _MeasurementRevisionPolicy:
    """Test the generic adapter with the Issue #4 measurement lifecycle."""

    @staticmethod
    def revision(state: MeasurementPipelineState) -> int:
        """Return the measurement revision."""
        return state.revision

    @staticmethod
    def validate_initial(state: MeasurementPipelineState) -> None:
        """Require the explicit empty measurement bootstrap state."""
        if (
            state.revision != 0
            or state.phase is not MeasurementPhase.AWAITING_SEGMENT_BASELINE
            or state.baseline is not None
            or state.candidate is not None
            or state.recovery_after_period_end is not None
        ):
            message = "measurement state is not an initial bootstrap"
            raise ValueError(message)

    @staticmethod
    def validate_transition(
        before: MeasurementPipelineState,
        after: MeasurementPipelineState,
    ) -> None:
        """Keep configured source identity fixed within this test timeline."""
        if after.sources != before.sources:
            message = "measurement sources cannot change in-place"
            raise ValueError(message)


@dataclass(frozen=True, slots=True)
class _GenerationState:
    """Test-local full generation with independent subsystem state."""

    revision: int
    measurement: MeasurementPipelineState
    accounting_marker: str
    accounting_sum: Fraction = Fraction()


def _decode_test_accounting(value: object) -> tuple[str, Fraction]:
    """Decode the closed test-local accounting token and exact sum."""
    if type(value) is not dict:
        message = "accounting payload must be an object"
        raise ValueError(message)
    accounting = cast("dict[object, object]", value)
    if accounting.keys() != {"token", "sum"}:
        message = "accounting payload has unexpected keys"
        raise ValueError(message)
    marker = accounting["token"]
    if type(marker) is not str or not marker or marker != marker.strip():
        message = "accounting token must be canonical"
        raise ValueError(message)
    sum_value = accounting["sum"]
    if type(sum_value) is not dict:
        message = "accounting sum must be an exact fraction"
        raise ValueError(message)
    sum_payload = cast("dict[object, object]", sum_value)
    if sum_payload.keys() != {"numerator", "denominator"}:
        message = "accounting sum has unexpected keys"
        raise ValueError(message)
    numerator = sum_payload["numerator"]
    denominator = sum_payload["denominator"]
    if type(numerator) is not int or type(denominator) is not int or denominator <= 0:
        message = "accounting sum must be an exact fraction"
        raise ValueError(message)
    result = Fraction(numerator, denominator)
    if result < 0 or result.numerator != numerator or result.denominator != denominator:
        message = "accounting sum must be canonical and non-negative"
        raise ValueError(message)
    return marker, result


class _GenerationCodec:
    """Strict test-local codec for a complete future generation payload."""

    @staticmethod
    def encode(state: _GenerationState) -> dict[str, object]:
        """Nest measurement and unrelated accounting state in one payload."""
        if type(state) is not _GenerationState:
            message = "state must be a generation"
            raise TypeError(message)
        if type(state.revision) is not int or state.revision < 0:
            message = "generation revision must be a non-negative integer"
            raise ValueError(message)
        if (
            type(state.accounting_marker) is not str
            or not state.accounting_marker
            or state.accounting_marker != state.accounting_marker.strip()
        ):
            message = "accounting token must be canonical"
            raise ValueError(message)
        if not isinstance(state.accounting_sum, Fraction) or state.accounting_sum < 0:
            message = "accounting sum must be an exact non-negative fraction"
            raise ValueError(message)
        return {
            "revision": state.revision,
            "measurement": encode_measurement_state(state.measurement),
            "accounting": {
                "token": state.accounting_marker,
                "sum": {
                    "numerator": state.accounting_sum.numerator,
                    "denominator": state.accounting_sum.denominator,
                },
            },
        }

    @staticmethod
    def decode(payload: object) -> _GenerationState:
        """Decode only the complete, closed test-generation schema."""
        if type(payload) is not dict:
            message = "generation payload must be an object"
            raise ValueError(message)
        data = cast("dict[object, object]", payload)
        if data.keys() != {"revision", "measurement", "accounting"}:
            message = "generation payload has unexpected keys"
            raise ValueError(message)
        revision = data["revision"]
        if type(revision) is not int or revision < 0:
            message = "generation revision must be a non-negative integer"
            raise ValueError(message)
        marker, accounting_sum = _decode_test_accounting(data["accounting"])
        return _GenerationState(
            revision=revision,
            measurement=decode_measurement_state(data["measurement"]),
            accounting_marker=marker,
            accounting_sum=accounting_sum,
        )


class _GenerationRevisionPolicy:
    """Test-local policy for the complete generation revision."""

    @staticmethod
    def revision(state: _GenerationState) -> int:
        """Return the full-generation revision."""
        return state.revision

    @staticmethod
    def validate_initial(state: _GenerationState) -> None:
        """Start a new generation at revision zero."""
        if state.revision != 0:
            message = "generation must start at revision zero"
            raise ValueError(message)

    @staticmethod
    def validate_transition(
        before: _GenerationState,
        after: _GenerationState,
    ) -> None:
        """Require policy-level adjacency independently of the adapter guard."""
        if after.revision != before.revision + 1:
            message = "generation transition must advance exactly once"
            raise ValueError(message)


class _DriftingGenerationCodec(_GenerationCodec):
    """Return a different state only for noninitial preflight decoding."""

    @staticmethod
    def decode(payload: object) -> _GenerationState:
        """Simulate a codec whose decode result is not equal to its input state."""
        state = _GenerationCodec.decode(payload)
        if state.revision == 0:
            return state
        return replace(state, accounting_marker=f"{state.accounting_marker}-drift")


class _MutableIdentityCodec:
    """Pass mutable dictionaries through without defensive copying."""

    @staticmethod
    def encode(state: dict[str, object]) -> dict[str, object]:
        """Return the mutable state itself."""
        return state

    @staticmethod
    def decode(payload: object) -> dict[str, object]:
        """Return the mutable payload itself."""
        if type(payload) is not dict:
            message = "mutable state must be an object"
            raise ValueError(message)
        return cast("dict[str, object]", payload)


class _MutableRevisionPolicy:
    """Read a revision from the mutable test payload."""

    @staticmethod
    def revision(state: dict[str, object]) -> int:
        """Return one exact dictionary revision."""
        revision = state["revision"]
        if type(revision) is not int:
            message = "mutable revision must be an integer"
            raise ValueError(message)
        return revision

    @staticmethod
    def validate_initial(state: dict[str, object]) -> None:
        """Require a zero initial revision."""
        if state["revision"] != 0:
            message = "mutable state must start at zero"
            raise ValueError(message)

    @staticmethod
    def validate_transition(
        before: dict[str, object],
        after: dict[str, object],
    ) -> None:
        """Accept any adapter-sequential test transition."""
        del before, after


def _measurement_store(
    hass: HomeAssistant,
    key: str,
) -> VerifiedAtomicStore[MeasurementPipelineState]:
    """Build a generic Store around the measurement-only test state."""
    return VerifiedAtomicStore(
        hass,
        key,
        codec=_MeasurementCodec(),
        revision_policy=_MeasurementRevisionPolicy(),
    )


def _generation_store(
    hass: HomeAssistant,
    key: str,
) -> VerifiedAtomicStore[_GenerationState]:
    """Build a generic Store around the complete test generation."""
    return VerifiedAtomicStore(
        hass,
        key,
        codec=_GenerationCodec(),
        revision_policy=_GenerationRevisionPolicy(),
    )


def _sample(
    source: EnergySourceIdentity,
    cumulative: Fraction,
    period_end: datetime,
    *,
    source_unit: EnergyUnit = EnergyUnit.KILOWATT_HOUR,
    report_delay: int = 10,
) -> EnergyCounterSample:
    """Construct one exact persisted counter sample."""
    return EnergyCounterSample(
        source=source,
        cumulative=Energy(cumulative),
        source_unit=source_unit,
        period_end=period_end,
        last_reported=period_end + timedelta(seconds=report_delay),
    )


def _baseline() -> CounterSnapshot:
    """Construct a complete accepted baseline with non-decimal fractions."""
    period_end = _START + timedelta(minutes=5)
    return CounterSnapshot(
        samples=(
            _sample(_SOURCES[0], Fraction(10, 3), period_end),
            _sample(
                _SOURCES[1],
                Fraction(20, 7),
                period_end,
                source_unit=EnergyUnit.WATT_HOUR,
                report_delay=20,
            ),
        )
    )


def _initial_state() -> MeasurementPipelineState:
    """Construct a state which may explicitly initialize a new Store."""
    return MeasurementPipelineState.initial(_SOURCES, _START)


def _initial_generation() -> _GenerationState:
    """Construct a complete test generation at its initial revision."""
    return _GenerationState(
        revision=0,
        measurement=_initial_state(),
        accounting_marker="ledger-0",
    )


def _active_state(
    *,
    revision: int = 4,
    candidate: CandidateBuffer | None = None,
) -> MeasurementPipelineState:
    """Construct a healthy state with an optional partial candidate."""
    return MeasurementPipelineState(
        revision=revision,
        phase=MeasurementPhase.ACTIVE,
        sources=_SOURCES,
        segment_transition_at=_START,
        baseline=_baseline(),
        candidate=candidate,
    )


def _partial_candidate(
    source: EnergySourceIdentity = _SOURCES[0],
    *,
    minutes: int = 10,
    source_unit: EnergyUnit = EnergyUnit.KILOWATT_HOUR,
) -> CandidateBuffer:
    """Construct one valid, incomplete candidate vector."""
    period_end = _START + timedelta(minutes=minutes)
    return CandidateBuffer(
        period_end=period_end,
        samples=(
            _sample(
                source,
                Fraction(11, 3),
                period_end,
                source_unit=source_unit,
            ),
        ),
    )


def _next_complete_poll() -> tuple[EnergyCounterSample, EnergyCounterSample]:
    """Construct one exact complete poll after the active test baseline."""
    period_end = _START + timedelta(minutes=10)
    return (
        _sample(_SOURCES[0], Fraction(11, 3), period_end),
        _sample(
            _SOURCES[1],
            Fraction(22, 7),
            period_end,
            source_unit=EnergyUnit.WATT_HOUR,
            report_delay=20,
        ),
    )


def _assemble_store_interval(
    batch: RawEnergyDeltaBatch,
) -> NormalizedInterval | RejectedInterval:
    """Normalize real inverter deltas for the restart/accounting test."""
    pv = batch.energy_for(_SOURCES[0].role)
    grid_import = batch.energy_for(_SOURCES[1].role)
    local_load = Energy(pv.kwh + grid_import.kwh)
    return normalize_interval(
        InverterIntervalInput(
            window=batch.window,
            consumers=loads_from_meters(ConsumerLoad("house", local_load), ()),
            pv_generation=pv,
            grid_import=grid_import,
            grid_export=Energy.zero(),
            battery_charge=Energy.zero(),
            battery_discharge=Energy.zero(),
        )
    )


def _apply_accounted_poll(
    state: _GenerationState,
    poll: tuple[EnergyCounterSample, EnergyCounterSample],
    observed_at: datetime,
) -> _GenerationState:
    """Reduce one poll and add PV energy only for an emitted interval."""
    transition = advance_measurements(
        state.measurement,
        poll,
        observed_at,
        assemble_interval=_assemble_store_interval,
    )
    if transition.interval is None:
        if transition.state == state.measurement:
            return state
        return replace(
            state,
            revision=state.revision + 1,
            measurement=transition.state,
        )
    return replace(
        state,
        revision=state.revision + 1,
        measurement=transition.state,
        accounting_sum=state.accounting_sum + transition.interval.pv.kwh,
    )


def _publish_generation_once(
    state: _GenerationState,
    published_revisions: set[int],
    published_sums: list[Fraction],
) -> None:
    """Record only the first successful return for a generation revision."""
    if state.revision in published_revisions:
        return
    published_revisions.add(state.revision)
    published_sums.append(state.accounting_sum)


def _waiting_state_with_candidate() -> MeasurementPipelineState:
    """Construct an initial-segment wait with a persisted partial vector."""
    return MeasurementPipelineState(
        revision=1,
        phase=MeasurementPhase.AWAITING_SEGMENT_BASELINE,
        sources=_SOURCES,
        segment_transition_at=_START,
        candidate=_partial_candidate(minutes=1),
    )


def _recovery_state(
    *,
    candidate: CandidateBuffer | None = None,
) -> MeasurementPipelineState:
    """Construct fail-closed recovery state retaining its old baseline."""
    baseline = _baseline()
    return MeasurementPipelineState(
        revision=5,
        phase=MeasurementPhase.AWAITING_REBASELINE,
        sources=_SOURCES,
        segment_transition_at=_START,
        baseline=baseline,
        candidate=candidate,
        recovery_after_period_end=baseline.period_end,
    )


def _active_payload() -> dict[str, object]:
    """Return a mutable valid payload for corruption tests."""
    return encode_measurement_state(_active_state(candidate=_partial_candidate()))


def _object(value: object) -> dict[str, object]:
    """Narrow a known test payload object."""
    assert type(value) is dict
    return cast("dict[str, object]", value)


def _array(value: object) -> list[object]:
    """Narrow a known test payload array."""
    assert type(value) is list
    return cast("list[object]", value)


def _baseline_sample(payload: dict[str, object], index: int = 0) -> dict[str, object]:
    """Return one mutable baseline-sample payload."""
    baseline = _object(payload["baseline"])
    return _object(_array(baseline["samples"])[index])


def _raw_store_envelope(hass_storage: dict[str, object], key: str) -> dict[str, object]:
    """Read one mocked HA Store envelope without version migration."""
    return _object(hass_storage[key])


@pytest.mark.parametrize(
    "state",
    [
        pytest.param(_initial_state(), id="initial"),
        pytest.param(
            _waiting_state_with_candidate(),
            id="initial-partial-candidate",
        ),
        pytest.param(_active_state(), id="active"),
        pytest.param(
            _active_state(candidate=_partial_candidate()),
            id="partial-candidate",
        ),
        pytest.param(_recovery_state(), id="recovery"),
        pytest.param(
            _recovery_state(
                candidate=_partial_candidate(
                    source_unit=EnergyUnit.MEGAWATT_HOUR,
                )
            ),
            id="recovery-partial-candidate-with-new-unit",
        ),
    ],
)
def test_codec_round_trip_preserves_every_measurement_phase(
    state: MeasurementPipelineState,
) -> None:
    """Every restart-relevant state round-trips without representation loss."""
    assert decode_measurement_state(encode_measurement_state(state)) == state


def test_codec_uses_exact_fraction_utc_z_unit_and_source_fields() -> None:
    """The JSON contract retains exact values and all sample identity fields."""
    payload = encode_measurement_state(_active_state())
    sample = _baseline_sample(payload)

    assert sample == {
        "source": {"role": "pv", "registry_id": "registry-pv"},
        "cumulative_kwh": {"numerator": 10, "denominator": 3},
        "source_unit": "kWh",
        "period_end": "2026-09-04T12:05:00Z",
        "last_reported": "2026-09-04T12:05:10Z",
    }
    assert payload["segment_transition_at"] == "2026-09-04T12:00:00Z"


def test_codec_rejects_non_object_payload_and_non_string_keys() -> None:
    """Only an exact JSON object with string field names enters decoding."""
    with pytest.raises(MeasurementStateCodecError, match="must be an object"):
        decode_measurement_state([])
    with pytest.raises(MeasurementStateCodecError, match="keys must be strings"):
        decode_measurement_state({1: None})


def test_encoder_rejects_wrong_state_type_and_non_utc_tampering() -> None:
    """Encoding also fails closed if callers bypass the immutable model API."""
    with pytest.raises(MeasurementStateCodecError, match="MeasurementPipelineState"):
        encode_measurement_state(cast("MeasurementPipelineState", object()))

    state = _initial_state()
    object.__setattr__(state, "segment_transition_at", _START.replace(tzinfo=None))
    with pytest.raises(MeasurementStateCodecError, match="timezone-aware UTC"):
        encode_measurement_state(state)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(True, id="boolean"),
        pytest.param(-1, id="negative"),
        pytest.param("1", id="string"),
    ],
)
def test_codec_rejects_invalid_revisions(value: object) -> None:
    """Revision is a non-negative exact integer, never a JSON boolean."""
    payload = _active_payload()
    payload["revision"] = value

    with pytest.raises(MeasurementStateCodecError, match=r"state\.revision"):
        decode_measurement_state(payload)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("unknown", id="unknown-string"),
        pytest.param(1, id="non-string"),
    ],
)
def test_codec_rejects_unknown_or_mistyped_phases(value: object) -> None:
    """Only the persisted MeasurementPhase values are accepted."""
    payload = _active_payload()
    payload["phase"] = value

    with pytest.raises(MeasurementStateCodecError, match=r"state\.phase"):
        decode_measurement_state(payload)


def test_codec_rejects_missing_and_extension_fields() -> None:
    """The state schema is closed to omissions and unknown extensions."""
    missing = _active_payload()
    del missing["candidate"]
    extra = _active_payload()
    extra["unexpected"] = None

    with pytest.raises(MeasurementStateCodecError, match="unexpected keys"):
        decode_measurement_state(missing)
    with pytest.raises(MeasurementStateCodecError, match="unexpected keys"):
        decode_measurement_state(extra)


@pytest.mark.parametrize(
    "sources",
    [
        pytest.param([], id="empty"),
        pytest.param("not-an-array", id="wrong-container"),
        pytest.param(
            [
                {"role": "pv", "registry_id": "registry-pv"},
                {"role": "pv", "registry_id": "other"},
            ],
            id="duplicate-role",
        ),
        pytest.param(
            [
                {"role": "pv", "registry_id": "same"},
                {"role": "grid_import", "registry_id": "same"},
            ],
            id="duplicate-registry-id",
        ),
        pytest.param(
            [{"role": " pv", "registry_id": "registry-pv"}],
            id="edge-whitespace",
        ),
        pytest.param(
            [{"role": 1, "registry_id": "registry-pv"}],
            id="non-string-role",
        ),
    ],
)
def test_codec_rejects_invalid_source_sets(sources: object) -> None:
    """Configured roles and registry identities form a strict one-to-one set."""
    payload = _active_payload()
    payload["sources"] = sources

    with pytest.raises(MeasurementStateCodecError, match=r"state\.sources"):
        decode_measurement_state(payload)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("2026-09-04T12:00:00+00:00", id="offset-not-z"),
        pytest.param("2026-09-04 12:00:00Z", id="noncanonical-separator"),
        pytest.param("2026-09-04T12:00:00.000000Z", id="redundant-microseconds"),
        pytest.param("not-a-timeZ", id="malformed"),
        pytest.param(0, id="non-string"),
    ],
)
def test_codec_rejects_noncanonical_utc_z_timestamps(value: object) -> None:
    """Persisted timestamps use one canonical UTC-Z representation."""
    payload = _active_payload()
    payload["segment_transition_at"] = value

    with pytest.raises(
        MeasurementStateCodecError, match=r"state\.segment_transition_at"
    ):
        decode_measurement_state(payload)


@pytest.mark.parametrize(
    "fraction",
    [
        pytest.param({"numerator": True, "denominator": 1}, id="boolean"),
        pytest.param({"numerator": 1, "denominator": 0}, id="zero-denominator"),
        pytest.param({"numerator": 2, "denominator": 4}, id="not-reduced"),
        pytest.param({"numerator": -1, "denominator": 3}, id="negative-energy"),
        pytest.param({"numerator": 1, "denominator": 2, "extra": 0}, id="extra"),
    ],
)
def test_codec_rejects_invalid_exact_energy(fraction: object) -> None:
    """Counter values are canonical non-negative exact fractions."""
    payload = _active_payload()
    _baseline_sample(payload)["cumulative_kwh"] = fraction

    with pytest.raises(MeasurementStateCodecError, match="cumulative_kwh"):
        decode_measurement_state(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("source_unit", "J", id="unknown-unit"),
        pytest.param("source_unit", 1, id="non-string-unit"),
        pytest.param("period_end", "2026-09-04T12:05:11Z", id="period-after-report"),
        pytest.param("last_reported", "2026-09-04T12:06:01Z", id="late-report"),
    ],
)
def test_codec_rejects_invalid_sample_semantics(field: str, value: object) -> None:
    """Units and accepted-sample publication ordering remain fail-closed."""
    payload = _active_payload()
    _baseline_sample(payload)[field] = value

    with pytest.raises(MeasurementStateCodecError):
        decode_measurement_state(payload)


def test_codec_rejects_unconfigured_sample_identity() -> None:
    """A baseline or candidate cannot smuggle in an unconfigured source."""
    payload = _active_payload()
    _baseline_sample(payload)["source"] = {
        "role": "other",
        "registry_id": "registry-other",
    }

    with pytest.raises(MeasurementStateCodecError, match="configured sources"):
        decode_measurement_state(payload)


def test_codec_rejects_invalid_phase_baseline_and_recovery_combinations() -> None:
    """Phase, baseline and recovery barrier must describe one possible restart."""
    active_without_baseline = _active_payload()
    active_without_baseline["baseline"] = None
    recovery_without_barrier = _active_payload()
    recovery_without_barrier["phase"] = "awaiting_rebaseline"
    recovery_without_barrier["recovery_after_period_end"] = None
    before_segment = _active_payload()
    before_segment["segment_transition_at"] = "2026-09-04T12:06:00Z"

    for payload in (
        active_without_baseline,
        recovery_without_barrier,
        before_segment,
    ):
        with pytest.raises(MeasurementStateCodecError):
            decode_measurement_state(payload)


def test_codec_rejects_invalid_baseline_and_candidate_shapes() -> None:
    """Nested vectors retain unique sources and their declared physical period."""
    duplicate_baseline_source = _active_payload()
    first_source = _baseline_sample(duplicate_baseline_source)["source"]
    _baseline_sample(duplicate_baseline_source, 1)["source"] = first_source

    mismatched_candidate_period = _active_payload()
    candidate = _object(mismatched_candidate_period["candidate"])
    candidate["period_end"] = "2026-09-04T12:11:00Z"

    for payload in (duplicate_baseline_source, mismatched_candidate_period):
        with pytest.raises(MeasurementStateCodecError):
            decode_measurement_state(payload)


def test_codec_rejects_active_candidate_unit_change() -> None:
    """An active partial candidate cannot cross a declared-unit boundary."""
    payload = _active_payload()
    candidate = _object(payload["candidate"])
    sample = _object(_array(candidate["samples"])[0])
    sample["source_unit"] = EnergyUnit.MEGAWATT_HOUR.value

    with pytest.raises(MeasurementStateCodecError, match="units must match"):
        decode_measurement_state(payload)


def test_codec_rejects_complete_persisted_candidate() -> None:
    """A candidate containing every source must have been accepted atomically."""
    payload = _active_payload()
    candidate = _object(payload["candidate"])
    samples = _array(candidate["samples"])
    period_end = _START + timedelta(minutes=10)
    samples.append(
        encode_measurement_state(
            _active_state(
                candidate=_partial_candidate(
                    _SOURCES[1],
                    source_unit=EnergyUnit.WATT_HOUR,
                ),
            )
        )["candidate"]
    )
    nested_candidate = _object(samples[-1])
    samples[-1] = _array(nested_candidate["samples"])[0]
    candidate["period_end"] = period_end.isoformat().removesuffix("+00:00") + "Z"

    with pytest.raises(MeasurementStateCodecError, match="complete candidate"):
        decode_measurement_state(payload)


async def test_missing_store_load_is_side_effect_free(
    hass: HomeAssistant,
    hass_storage: dict[str, object],
) -> None:
    """A missing injected key returns None and is never initialized implicitly."""
    key = "co2saver.measurement.missing"
    adapter = _generation_store(hass, key)

    assert await adapter.async_load() is None
    assert key not in hass_storage


async def test_null_envelope_load_is_not_physical_absence_or_auto_init(
    hass: HomeAssistant,
    hass_storage: dict[str, object],
) -> None:
    """A data-null envelope yields None but remains untouched for Issue #8."""
    key = "co2saver.generation.null-envelope"
    envelope: dict[str, object] = {
        "version": 1,
        "minor_version": 1,
        "key": key,
        "data": None,
    }
    hass_storage[key] = deepcopy(envelope)

    assert await _generation_store(hass, key).async_load() is None
    assert hass_storage[key] == envelope


async def test_explicit_initialize_writes_store_v1_1_and_restarts_exactly(
    hass: HomeAssistant,
    hass_storage: dict[str, object],
) -> None:
    """Initialization is explicit, atomic and readable by a fresh adapter."""
    key = "co2saver.measurement.initialize"
    initial = _initial_generation()
    adapter = _generation_store(hass, key)

    assert await adapter.async_initialize_confirmed_absent(initial) == initial
    assert await _generation_store(hass, key).async_load() == initial

    envelope = _raw_store_envelope(hass_storage, key)
    assert envelope["version"] == 1
    assert envelope["minor_version"] == 1
    assert envelope["key"] == key
    assert envelope["data"] == _GenerationCodec.encode(initial)


def test_test_generation_codec_nests_measurement_and_accounting_state() -> None:
    """The generic contract represents a complete, nested generation payload."""
    state = _initial_generation()
    payload = _GenerationCodec.encode(state)

    assert payload == {
        "revision": 0,
        "measurement": encode_measurement_state(state.measurement),
        "accounting": {
            "token": "ledger-0",
            "sum": {"numerator": 0, "denominator": 1},
        },
    }
    assert _GenerationCodec.decode(payload) == state


async def test_every_write_uses_fresh_atomic_store_instances(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each operation uses separate loader, writer and read-back instances."""
    created: list[Store[dict[str, object]]] = []
    adapter = _generation_store(hass, "co2saver.generation.atomic")
    original_new_store = adapter._new_store  # noqa: SLF001

    def tracked_new_store() -> Store[dict[str, object]]:
        store = original_new_store()
        created.append(store)
        return store

    monkeypatch.setattr(adapter, "_new_store", tracked_new_store)

    await adapter.async_initialize_confirmed_absent(_initial_generation())

    assert len(created) == 3
    assert len({id(store) for store in created}) == 3
    assert all(vars(store)["_atomic_writes"] is True for store in created)
    assert all(vars(store)["_max_readable_version"] == 1 for store in created)

    created.clear()

    def advance(state: _GenerationState) -> _GenerationState:
        return replace(state, revision=1, accounting_marker="ledger-1")

    await adapter.async_transact(advance)

    assert len(created) == 3
    assert len({id(store) for store in created}) == 3
    assert all(vars(store)["_atomic_writes"] is True for store in created)
    assert all(vars(store)["_max_readable_version"] == 1 for store in created)


async def test_initialize_rejects_existing_store_without_overwriting(
    hass: HomeAssistant,
) -> None:
    """Explicit creation has create-if-absent rather than reset semantics."""
    key = "co2saver.generation.initialize-conflict"
    adapter = _generation_store(hass, key)
    initial = _initial_generation()
    await adapter.async_initialize_confirmed_absent(initial)

    with pytest.raises(VerifiedAtomicStoreConflictError, match="already exists"):
        await adapter.async_initialize_confirmed_absent(initial)

    assert await adapter.async_load() == initial


async def test_initialize_delegates_initial_semantics_to_policy(
    hass: HomeAssistant,
) -> None:
    """The injected policy, not the generic adapter, defines valid bootstrap."""
    adapter = _generation_store(hass, "co2saver.generation.bad-initialize")

    with pytest.raises(ValueError, match="revision zero"):
        await adapter.async_initialize_confirmed_absent(
            replace(_initial_generation(), revision=1)
        )
    assert await adapter.async_load() is None


async def test_transact_requires_existing_store_and_exact_next_revision(
    hass: HomeAssistant,
) -> None:
    """A changed state must advance the complete generation exactly once."""
    key = "co2saver.generation.revisions"
    adapter = _generation_store(hass, key)

    with pytest.raises(
        VerifiedAtomicStoreConflictError,
        match="initialize it explicitly",
    ):
        await adapter.async_transact(
            lambda state: replace(state, revision=state.revision + 1)
        )

    initial = _initial_generation()
    await adapter.async_initialize_confirmed_absent(initial)

    def changed_without_revision(state: _GenerationState) -> _GenerationState:
        return replace(state, accounting_marker="changed")

    def skipped_revision(state: _GenerationState) -> _GenerationState:
        return replace(state, revision=2, accounting_marker="changed")

    for invalid_transform in (changed_without_revision, skipped_revision):
        with pytest.raises(VerifiedAtomicStoreConflictError, match="must be 1"):
            await adapter.async_transact(invalid_transform)

    assert await adapter.async_load() == initial


async def test_noop_transaction_returns_loaded_state_without_save(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An idempotent duplicate is decided under lock and writes nothing."""
    adapter = _generation_store(hass, "co2saver.generation.noop")
    initial = _initial_generation()
    await adapter.async_initialize_confirmed_absent(initial)
    save_calls = 0
    original_save = Store.async_save

    async def count_save(
        store: Store[dict[str, object]],
        data: dict[str, object],
    ) -> None:
        nonlocal save_calls
        save_calls += 1
        await original_save(store, data)

    monkeypatch.setattr(Store, "async_save", count_save)

    loaded = await adapter.async_transact(lambda _state: initial)

    assert loaded is not initial
    assert loaded == initial
    assert save_calls == 0


async def test_changed_transaction_saves_complete_payload_exactly_once(
    hass: HomeAssistant,
    hass_storage: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One accepted mutation saves measurement and accounting as one unit."""
    key = "co2saver.generation.one-save"
    adapter = _generation_store(hass, key)
    initial = _initial_generation()
    await adapter.async_initialize_confirmed_absent(initial)
    save_calls = 0
    original_save = Store.async_save

    async def count_save(
        store: Store[dict[str, object]],
        data: dict[str, object],
    ) -> None:
        nonlocal save_calls
        save_calls += 1
        await original_save(store, data)

    monkeypatch.setattr(Store, "async_save", count_save)

    def advance(state: _GenerationState) -> _GenerationState:
        measurement = replace(
            state.measurement,
            revision=state.measurement.revision + 1,
            candidate=_partial_candidate(),
        )
        return replace(
            state,
            revision=state.revision + 1,
            measurement=measurement,
            accounting_marker="ledger-1",
        )

    committed = await adapter.async_transact(advance)

    assert save_calls == 1
    assert committed.revision == 1
    assert committed.measurement.candidate == _partial_candidate()
    assert committed.accounting_marker == "ledger-1"
    assert _raw_store_envelope(hass_storage, key)["data"] == (
        _GenerationCodec.encode(committed)
    )


async def test_key_shared_lock_serializes_concurrent_transformations(
    hass: HomeAssistant,
) -> None:
    """Two adapters derive successive revisions without a lost full update."""
    key = "co2saver.generation.concurrent"
    first = _generation_store(hass, key)
    second = _generation_store(hass, key)
    initial = _initial_generation()
    await first.async_initialize_confirmed_absent(initial)

    def append_a(state: _GenerationState) -> _GenerationState:
        return replace(
            state,
            revision=state.revision + 1,
            accounting_marker=f"{state.accounting_marker}A",
        )

    def append_b(state: _GenerationState) -> _GenerationState:
        return replace(
            state,
            revision=state.revision + 1,
            accounting_marker=f"{state.accounting_marker}B",
        )

    results = await asyncio.gather(
        first.async_transact(append_a),
        second.async_transact(append_b),
    )

    assert sorted(result.revision for result in results) == [1, 2]
    final = await first.async_load()
    assert final is not None
    assert final.revision == 2
    assert final.accounting_marker in {"ledger-0AB", "ledger-0BA"}


async def test_transform_error_keeps_old_complete_payload(
    hass: HomeAssistant,
    hass_storage: dict[str, object],
) -> None:
    """A crashing pure transform cannot publish a partial subsystem update."""
    key = "co2saver.generation.transform-error"
    adapter = _generation_store(hass, key)
    await adapter.async_initialize_confirmed_absent(_initial_generation())
    before = deepcopy(_raw_store_envelope(hass_storage, key)["data"])
    published: list[_GenerationState] = []

    def crash(_state: _GenerationState) -> _GenerationState:
        message = "simulated pre-save crash"
        raise RuntimeError(message)

    with pytest.raises(RuntimeError, match="pre-save crash"):
        published.append(await adapter.async_transact(crash))

    assert published == []
    assert _raw_store_envelope(hass_storage, key)["data"] == before


async def test_policy_error_keeps_old_complete_payload(
    hass: HomeAssistant,
    hass_storage: dict[str, object],
) -> None:
    """A semantic policy rejection happens before any physical save."""
    key = "co2saver.measurement.policy-error"
    adapter = _measurement_store(hass, key)
    initial = _initial_state()
    await adapter.async_initialize_confirmed_absent(initial)
    before = deepcopy(_raw_store_envelope(hass_storage, key)["data"])
    changed_sources = (
        EnergySourceIdentity(role="pv", registry_id="other-pv"),
        _SOURCES[1],
    )

    with pytest.raises(ValueError, match="sources cannot change"):
        await adapter.async_transact(
            lambda state: replace(
                state,
                revision=state.revision + 1,
                sources=changed_sources,
            )
        )

    assert _raw_store_envelope(hass_storage, key)["data"] == before


async def test_policy_revision_must_be_a_nonnegative_exact_integer(
    hass: HomeAssistant,
) -> None:
    """A buggy policy cannot return a boolean masquerading as a revision."""

    class BooleanRevisionPolicy(_GenerationRevisionPolicy):
        """Expose one invalid policy result for the adapter guard."""

        @staticmethod
        def revision(state: _GenerationState) -> int:
            """Return a runtime boolean despite the static interface."""
            del state
            invalid: object = True
            return cast("int", invalid)

    adapter = VerifiedAtomicStore(
        hass,
        "co2saver.generation.boolean-revision",
        codec=_GenerationCodec(),
        revision_policy=BooleanRevisionPolicy(),
    )

    with pytest.raises(
        VerifiedAtomicStoreVerificationError,
        match="non-negative integer",
    ):
        await adapter.async_initialize_confirmed_absent(_initial_generation())


async def test_codec_error_keeps_old_complete_payload(
    hass: HomeAssistant,
    hass_storage: dict[str, object],
) -> None:
    """A proposed state the codec cannot encode never reaches HA Store."""
    key = "co2saver.generation.codec-error"
    adapter = _generation_store(hass, key)
    initial = _initial_generation()
    await adapter.async_initialize_confirmed_absent(initial)
    before = deepcopy(_raw_store_envelope(hass_storage, key)["data"])

    with pytest.raises(ValueError, match="accounting token"):
        await adapter.async_transact(
            lambda state: replace(
                state,
                revision=state.revision + 1,
                accounting_marker=" bad-token",
            )
        )

    assert _raw_store_envelope(hass_storage, key)["data"] == before


async def test_preflight_rejects_a_nonobject_codec_result(
    hass: HomeAssistant,
) -> None:
    """The physical Store contract always owns one complete JSON object."""

    class NonObjectCodec(_GenerationCodec):
        """Violate the static codec contract at runtime."""

        @staticmethod
        def encode(state: _GenerationState) -> dict[str, object]:
            """Return a list disguised by a cast for the runtime guard."""
            del state
            return cast("dict[str, object]", [])

    adapter = VerifiedAtomicStore(
        hass,
        "co2saver.generation.nonobject",
        codec=NonObjectCodec(),
        revision_policy=_GenerationRevisionPolicy(),
    )

    with pytest.raises(
        VerifiedAtomicStoreVerificationError,
        match="JSON object",
    ):
        await adapter.async_initialize_confirmed_absent(_initial_generation())


async def test_preflight_inequality_keeps_old_complete_payload(
    hass: HomeAssistant,
    hass_storage: dict[str, object],
) -> None:
    """Encode-decode inequality is rejected before Store.async_save."""
    key = "co2saver.generation.preflight"
    initial_adapter = _generation_store(hass, key)
    initial = _initial_generation()
    await initial_adapter.async_initialize_confirmed_absent(initial)
    before = deepcopy(_raw_store_envelope(hass_storage, key)["data"])
    adapter = VerifiedAtomicStore(
        hass,
        key,
        codec=_DriftingGenerationCodec(),
        revision_policy=_GenerationRevisionPolicy(),
    )

    with pytest.raises(
        VerifiedAtomicStoreVerificationError,
        match="does not round-trip",
    ):
        await adapter.async_transact(
            lambda state: replace(
                state,
                revision=state.revision + 1,
                accounting_marker="ledger-1",
            )
        )

    assert _raw_store_envelope(hass_storage, key)["data"] == before


async def test_load_rejects_a_noncanonical_complete_payload(
    hass: HomeAssistant,
) -> None:
    """A lenient decoder cannot make extension fields silently canonical."""

    class LenientGenerationCodec(_GenerationCodec):
        """Decode one extension field which canonical encoding omits."""

        @staticmethod
        def decode(payload: object) -> _GenerationState:
            """Remove the test extension before strict domain decoding."""
            if type(payload) is not dict:
                return _GenerationCodec.decode(payload)
            canonical = deepcopy(cast("dict[str, object]", payload))
            canonical.pop("extension", None)
            return _GenerationCodec.decode(canonical)

    key = "co2saver.generation.noncanonical"
    payload = _GenerationCodec.encode(_initial_generation())
    payload["extension"] = True
    await Store[dict[str, object]](
        hass,
        1,
        key,
        atomic_writes=True,
        max_readable_version=1,
        minor_version=1,
    ).async_save(payload)
    adapter = VerifiedAtomicStore(
        hass,
        key,
        codec=LenientGenerationCodec(),
        revision_policy=_GenerationRevisionPolicy(),
    )

    with pytest.raises(
        VerifiedAtomicStoreVerificationError,
        match="not canonical",
    ):
        await adapter.async_load()


async def test_async_transform_is_rejected_and_closed(hass: HomeAssistant) -> None:
    """Only a synchronous transform may execute inside the shared key lock."""
    adapter = _generation_store(hass, "co2saver.generation.async-transform")
    initial = _initial_generation()
    await adapter.async_initialize_confirmed_absent(initial)

    async def async_transform(state: _GenerationState) -> _GenerationState:
        return replace(state, revision=state.revision + 1)

    transform = cast(
        "Callable[[_GenerationState], _GenerationState]",
        async_transform,
    )
    with pytest.raises(TypeError, match="must be synchronous"):
        await adapter.async_transact(transform)

    assert await adapter.async_load() == initial


async def test_mutating_transform_input_is_rejected(hass: HomeAssistant) -> None:
    """A transform may only return a new value, never mutate its input copy."""
    adapter = _generation_store(hass, "co2saver.generation.mutating-transform")
    initial = _initial_generation()
    await adapter.async_initialize_confirmed_absent(initial)

    def mutate(state: _GenerationState) -> _GenerationState:
        object.__setattr__(state, "accounting_marker", "mutated")
        return replace(state, revision=state.revision + 1)

    with pytest.raises(
        VerifiedAtomicStoreConflictError,
        match="must not mutate",
    ):
        await adapter.async_transact(mutate)

    assert await adapter.async_load() == initial


async def test_identity_codec_cannot_alias_and_publish_a_mutated_payload(
    hass: HomeAssistant,
    hass_storage: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Independent decode copies contain a mutating pass-through codec."""
    key = "co2saver.generation.identity-alias"
    adapter = VerifiedAtomicStore(
        hass,
        key,
        codec=_MutableIdentityCodec(),
        revision_policy=_MutableRevisionPolicy(),
    )
    initial: dict[str, object] = {
        "revision": 0,
        "nested": {"status": "old"},
    }
    await adapter.async_initialize_confirmed_absent(initial)
    before = deepcopy(_raw_store_envelope(hass_storage, key)["data"])
    save_calls = 0
    original_save = Store.async_save

    async def count_save(
        store: Store[dict[str, object]],
        data: dict[str, object],
    ) -> None:
        nonlocal save_calls
        save_calls += 1
        await original_save(store, data)

    def mutate(state: dict[str, object]) -> dict[str, object]:
        state["revision"] = 1
        nested = _object(state["nested"])
        nested["status"] = "mutated"
        return state

    monkeypatch.setattr(Store, "async_save", count_save)
    published: list[dict[str, object]] = []

    with pytest.raises(
        VerifiedAtomicStoreConflictError,
        match="must not mutate",
    ):
        published.append(await adapter.async_transact(mutate))

    assert published == []
    assert save_calls == 0
    assert _raw_store_envelope(hass_storage, key)["data"] == before
    assert (
        await VerifiedAtomicStore(
            hass,
            key,
            codec=_MutableIdentityCodec(),
            revision_policy=_MutableRevisionPolicy(),
        ).async_load()
        == initial
    )


async def test_save_error_keeps_old_complete_payload(
    hass: HomeAssistant,
    hass_storage: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Store exception exposes no state and leaves the old payload intact."""
    key = "co2saver.generation.save-error"
    adapter = _generation_store(hass, key)
    await adapter.async_initialize_confirmed_absent(_initial_generation())
    before = deepcopy(_raw_store_envelope(hass_storage, key)["data"])

    async def fail_save(
        store: Store[dict[str, object]],
        data: dict[str, object],
    ) -> None:
        del store, data
        message = "simulated save error"
        raise OSError(message)

    monkeypatch.setattr(Store, "async_save", fail_save)

    with pytest.raises(OSError, match="save error"):
        await adapter.async_transact(
            lambda state: replace(
                state,
                revision=state.revision + 1,
                accounting_marker="ledger-1",
            )
        )

    assert _raw_store_envelope(hass_storage, key)["data"] == before


async def test_readback_detects_swallowed_save_and_keeps_old_payload(
    hass: HomeAssistant,
    hass_storage: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unchanged fresh full read-back cannot masquerade as a commit."""
    key = "co2saver.generation.swallowed-save"
    adapter = _generation_store(hass, key)
    await adapter.async_initialize_confirmed_absent(_initial_generation())
    before = deepcopy(_raw_store_envelope(hass_storage, key)["data"])

    async def discard_save(
        store: Store[dict[str, object]],
        data: dict[str, object],
    ) -> None:
        del store, data

    monkeypatch.setattr(Store, "async_save", discard_save)

    with pytest.raises(VerifiedAtomicStoreVerificationError, match="differs"):
        await adapter.async_transact(
            lambda state: replace(
                state,
                revision=state.revision + 1,
                accounting_marker="ledger-1",
            )
        )

    assert _raw_store_envelope(hass_storage, key)["data"] == before


async def test_post_save_readback_error_releases_no_result_and_restores_atomically(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed verifier publishes nothing; restart sees one complete state."""
    key = "co2saver.generation.readback-error"
    adapter = _generation_store(hass, key)
    initial = _initial_generation()
    await adapter.async_initialize_confirmed_absent(initial)
    original_load = adapter._load_payload_unlocked  # noqa: SLF001
    load_calls = 0

    async def fail_verification_load() -> object | None:
        nonlocal load_calls
        load_calls += 1
        if load_calls == 2:
            message = "simulated read-back error"
            raise OSError(message)
        return await original_load()

    monkeypatch.setattr(adapter, "_load_payload_unlocked", fail_verification_load)
    published: list[_GenerationState] = []

    with pytest.raises(OSError, match="read-back error"):
        published.append(
            await adapter.async_transact(
                lambda state: replace(
                    state,
                    revision=state.revision + 1,
                    accounting_marker="ledger-1",
                )
            )
        )

    assert published == []
    restored = await _generation_store(hass, key).async_load()
    assert restored in (
        initial,
        replace(initial, revision=1, accounting_marker="ledger-1"),
    )


async def test_real_reducer_retry_accounts_complete_poll_exactly_once(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry one uncertain full-state commit without double accounting."""
    key = "co2saver.generation.reducer-retry"
    initial = _GenerationState(
        revision=0,
        measurement=_active_state(),
        accounting_marker="ledger-0",
    )
    poll = _next_complete_poll()
    observed_at = poll[-1].last_reported

    def apply_poll(state: _GenerationState) -> _GenerationState:
        return _apply_accounted_poll(state, poll, observed_at)

    expected = apply_poll(initial)
    assert expected.accounting_sum == Fraction(1, 3)
    assert expected.measurement.baseline is not None
    assert expected.measurement.baseline.period_end == poll[0].period_end

    first = _generation_store(hass, key)
    await first.async_initialize_confirmed_absent(initial)
    original_load = first._load_payload_unlocked  # noqa: SLF001
    load_calls = 0

    async def fail_post_save_readback() -> object | None:
        nonlocal load_calls
        load_calls += 1
        if load_calls == 2:
            message = "simulated post-save read-back error"
            raise OSError(message)
        return await original_load()

    monkeypatch.setattr(first, "_load_payload_unlocked", fail_post_save_readback)
    published_revisions: set[int] = set()
    published_sums: list[Fraction] = []

    with pytest.raises(OSError, match="post-save read-back"):
        _publish_generation_once(
            await first.async_transact(apply_poll),
            published_revisions,
            published_sums,
        )

    assert published_sums == []
    restarted = _generation_store(hass, key)
    recovered = await restarted.async_load()
    assert recovered in (initial, expected)

    save_calls = 0
    original_save = Store.async_save

    async def count_save(
        store: Store[dict[str, object]],
        data: dict[str, object],
    ) -> None:
        nonlocal save_calls
        save_calls += 1
        await original_save(store, data)

    monkeypatch.setattr(Store, "async_save", count_save)
    final = await restarted.async_transact(apply_poll)
    _publish_generation_once(final, published_revisions, published_sums)
    saves_after_retry = save_calls

    assert final == expected
    assert final.accounting_sum == Fraction(1, 3)
    assert final.measurement.baseline is not None
    assert final.measurement.baseline.period_end == poll[0].period_end
    assert published_sums == [Fraction(1, 3)]

    replayed = await restarted.async_transact(apply_poll)
    _publish_generation_once(replayed, published_revisions, published_sums)

    assert replayed == final
    assert save_calls == saves_after_retry
    assert published_sums == [Fraction(1, 3)]
    assert await _generation_store(hass, key).async_load() == final


async def test_crash_before_save_and_verified_save_restore_atomically(
    hass: HomeAssistant,
) -> None:
    """Fresh adapters see either the old state or the fully verified new state."""
    key = "co2saver.generation.crash-matrix"
    adapter = _generation_store(hass, key)
    initial = _initial_generation()
    await adapter.async_initialize_confirmed_absent(initial)

    def crash(_state: _GenerationState) -> _GenerationState:
        message = "crash before save"
        raise RuntimeError(message)

    with pytest.raises(RuntimeError, match="before save"):
        await adapter.async_transact(crash)
    assert await _generation_store(hass, key).async_load() == initial

    committed = await adapter.async_transact(
        lambda state: replace(
            state,
            revision=state.revision + 1,
            accounting_marker="ledger-1",
        )
    )
    assert await _generation_store(hass, key).async_load() == committed


async def test_measurement_codec_cannot_overwrite_a_full_generation(
    hass: HomeAssistant,
    hass_storage: dict[str, object],
) -> None:
    """A subsystem codec fails before touching a foreign complete payload."""
    key = "co2saver.generation.foreign-to-measurement"
    generation = _generation_store(hass, key)
    await generation.async_initialize_confirmed_absent(_initial_generation())
    before = deepcopy(_raw_store_envelope(hass_storage, key)["data"])
    measurement = _measurement_store(hass, key)

    with pytest.raises(MeasurementStateCodecError, match="unexpected keys"):
        await measurement.async_transact(
            lambda state: replace(state, revision=state.revision + 1)
        )

    assert _raw_store_envelope(hass_storage, key)["data"] == before


@pytest.mark.parametrize(
    ("major", "minor"),
    [
        pytest.param(0, 1, id="older-major"),
        pytest.param(1, 0, id="older-minor"),
        pytest.param(1, 2, id="future-minor"),
        pytest.param(2, 1, id="future-major"),
    ],
)
async def test_store_rejects_every_version_mismatch_without_migration(
    hass: HomeAssistant,
    hass_storage: dict[str, object],
    major: int,
    minor: int,
) -> None:
    """Issue #4 accepts exactly Store 1.1 and never rewrites another version."""
    key = f"co2saver.generation.version-{major}-{minor}"
    payload = _GenerationCodec.encode(_initial_generation())
    foreign = Store[dict[str, object]](
        hass,
        major,
        key,
        atomic_writes=True,
        minor_version=minor,
    )
    await foreign.async_save(payload)

    with pytest.raises(VerifiedAtomicStoreVersionError):
        await _generation_store(hass, key).async_load()

    envelope = _raw_store_envelope(hass_storage, key)
    assert envelope["version"] == major
    assert envelope["minor_version"] == minor
    assert envelope["data"] == payload


async def test_store_rejects_malformed_current_payload(hass: HomeAssistant) -> None:
    """Correct envelope version never makes malformed full state acceptable."""
    key = "co2saver.generation.malformed"
    malformed: dict[str, object] = {"revision": True}
    await Store[dict[str, object]](
        hass,
        1,
        key,
        atomic_writes=True,
        max_readable_version=1,
        minor_version=1,
    ).async_save(malformed)

    with pytest.raises(ValueError, match="unexpected keys"):
        await _generation_store(hass, key).async_load()


async def test_initialize_readback_detects_a_swallowed_write(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Initialization also publishes nothing until a fresh read-back exists."""

    async def discard_save(
        store: Store[dict[str, object]],
        data: dict[str, object],
    ) -> None:
        del store, data

    monkeypatch.setattr(Store, "async_save", discard_save)
    adapter = _generation_store(hass, "co2saver.generation.discarded-new")

    with pytest.raises(VerifiedAtomicStoreVerificationError, match="absent"):
        await adapter.async_initialize_confirmed_absent(_initial_generation())


async def test_transact_does_not_schedule_an_unverified_stopping_write(
    hass: HomeAssistant,
    hass_storage: dict[str, object],
) -> None:
    """HA's deferred STOPPING write path is rejected before Store.async_save."""
    key = "co2saver.generation.stopping"
    adapter = _generation_store(hass, key)
    initial = _initial_generation()
    await adapter.async_initialize_confirmed_absent(initial)
    before = deepcopy(_raw_store_envelope(hass_storage, key))

    hass.set_state(CoreState.stopping)
    try:
        with pytest.raises(VerifiedAtomicStoreError, match="while Home Assistant"):
            await adapter.async_transact(
                lambda state: replace(
                    state,
                    revision=state.revision + 1,
                    accounting_marker="ledger-1",
                )
            )
    finally:
        hass.set_state(CoreState.running)

    assert _raw_store_envelope(hass_storage, key) == before


def test_store_key_must_be_injected_as_a_canonical_nonempty_string() -> None:
    """The adapter never derives a fallback key from another identity."""
    hass = cast("HomeAssistant", object())
    for key in ("", " key", "key ", None, 1):
        with pytest.raises(ValueError, match="store_key"):
            VerifiedAtomicStore(
                hass,
                cast("str", key),
                codec=_GenerationCodec(),
                revision_policy=_GenerationRevisionPolicy(),
            )
