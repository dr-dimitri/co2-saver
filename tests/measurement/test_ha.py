# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Tests for the Home Assistant cumulative-energy boundary."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from typing import TYPE_CHECKING, cast

import pytest
from homeassistant.components.sensor import (
    ATTR_STATE_CLASS,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.helpers import entity_registry as er

from custom_components.co2saver.const import ATTR_CO2SAVER_PERIOD_END
from custom_components.co2saver.measurement import ha as ha_boundary
from custom_components.co2saver.measurement.ha import (
    HomeAssistantEnergyReader,
    UtcMinuteRunner,
)
from custom_components.co2saver.measurement.models import (
    EnergyCounterSample,
    EnergyObservation,
    EnergySourceIdentity,
    MeasurementRejectionReason,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_registry import RegistryEntry

_PERIOD_END = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)
_LAST_REPORTED = _PERIOD_END + timedelta(seconds=30)


def _register_source(
    hass: HomeAssistant,
    *,
    role: str = "pv",
    domain: str = "sensor",
    unique_id: str = "pv_energy",
) -> tuple[EnergySourceIdentity, RegistryEntry]:
    """Create one registry-backed test source."""
    entry = er.async_get(hass).async_get_or_create(
        domain,
        "test",
        unique_id,
        suggested_object_id=unique_id,
    )
    return EnergySourceIdentity(role=role, registry_id=entry.id), entry


def _set_energy_state(  # noqa: PLR0913
    hass: HomeAssistant,
    entity_id: str,
    *,
    value: object = "1.25",
    unit: object = "kWh",
    device_class: object = SensorDeviceClass.ENERGY,
    state_class: object = SensorStateClass.TOTAL_INCREASING,
    period_end: object = _PERIOD_END.isoformat(),
    reported_at: datetime = _LAST_REPORTED,
) -> None:
    """Publish one cumulative-energy state with explicit source semantics."""
    hass.states.async_set(
        entity_id,
        str(value),
        {
            ATTR_DEVICE_CLASS: device_class,
            ATTR_STATE_CLASS: state_class,
            ATTR_UNIT_OF_MEASUREMENT: unit,
            ATTR_CO2SAVER_PERIOD_END: period_end,
        },
        timestamp=reported_at.timestamp(),
    )


def _only_sample(
    observations: tuple[EnergyObservation, ...],
) -> EnergyCounterSample:
    """Return and type-narrow the sole valid observation."""
    assert len(observations) == 1
    sample = observations[0]
    assert isinstance(sample, EnergyCounterSample)
    return sample


def _only_reason(
    observations: tuple[EnergyObservation, ...],
) -> MeasurementRejectionReason:
    """Return the rejection reason of the sole invalid observation."""
    assert len(observations) == 1
    observation = observations[0]
    assert not isinstance(observation, EnergyCounterSample)
    return observation.reason


def test_reader_resolves_registry_uuid_and_normalizes_exact_units(
    hass: HomeAssistant,
) -> None:
    """Resolve every current entity ID and normalize Wh, kWh, and MWh exactly."""
    configured: list[EnergySourceIdentity] = []
    cases = (
        ("wh", "100", "Wh", Fraction(1, 10)),
        ("kwh", "0.1", "kWh", Fraction(1, 10)),
        ("mwh", "0.001", "MWh", Fraction(1)),
    )
    for role, value, unit, _expected in cases:
        source, entry = _register_source(hass, role=role, unique_id=role)
        configured.append(source)
        state_class = (
            SensorStateClass.TOTAL
            if role == "wh"
            else SensorStateClass.TOTAL_INCREASING
        )
        _set_energy_state(
            hass,
            entry.entity_id,
            value=value,
            unit=unit,
            state_class=state_class,
        )

    observations = HomeAssistantEnergyReader(hass, tuple(configured)).read()

    assert [
        cast("EnergyCounterSample", observation).cumulative.kwh
        for observation in observations
    ] == [expected for _role, _value, _unit, expected in cases]
    assert [
        cast("EnergyCounterSample", observation).source.role
        for observation in observations
    ] == ["wh", "kwh", "mwh"]


def test_reader_reresolves_entity_id_and_copies_mutable_state_scalars(
    hass: HomeAssistant,
) -> None:
    """Follow registry renames while retaining immutable prior observations."""
    source, entry = _register_source(hass)
    _set_energy_state(hass, entry.entity_id)
    reader = HomeAssistantEnergyReader(hass, (source,))
    first = _only_sample(reader.read())

    old_entity_id = entry.entity_id
    renamed = er.async_get(hass).async_update_entity(
        old_entity_id,
        new_entity_id="sensor.renamed_pv_energy",
    )
    hass.states.async_remove(old_entity_id)
    _set_energy_state(
        hass,
        renamed.entity_id,
        value="2.5",
        period_end=(_PERIOD_END + timedelta(minutes=1)).isoformat(),
        reported_at=_LAST_REPORTED + timedelta(minutes=1),
    )

    second = _only_sample(reader.read())

    assert first.cumulative.kwh == Fraction(5, 4)
    assert first.period_end == _PERIOD_END
    assert first.last_reported == _LAST_REPORTED
    assert second.source == source
    assert second.cumulative.kwh == Fraction(5, 2)
    assert second.period_end == _PERIOD_END + timedelta(minutes=1)


@pytest.mark.parametrize("state_value", [STATE_UNKNOWN, STATE_UNAVAILABLE])
def test_reader_rejects_unavailable_states(
    hass: HomeAssistant,
    state_value: str,
) -> None:
    """Map both Home Assistant absence sentinels to one explicit rejection."""
    source, entry = _register_source(hass)
    _set_energy_state(hass, entry.entity_id, value=state_value)

    assert _only_reason(HomeAssistantEnergyReader(hass, (source,)).read()) is (
        MeasurementRejectionReason.SOURCE_UNAVAILABLE
    )


@pytest.mark.parametrize("state_value", ["not-a-number", "nan", "inf", "-0.1"])
def test_reader_rejects_invalid_values(
    hass: HomeAssistant,
    state_value: str,
) -> None:
    """Reject nonnumeric, nonfinite, and negative cumulative readings."""
    source, entry = _register_source(hass)
    _set_energy_state(hass, entry.entity_id, value=state_value)

    assert _only_reason(HomeAssistantEnergyReader(hass, (source,)).read()) is (
        MeasurementRejectionReason.INVALID_VALUE
    )


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        (
            {"device_class": "power"},
            MeasurementRejectionReason.INVALID_DEVICE_CLASS,
        ),
        (
            {"state_class": "measurement"},
            MeasurementRejectionReason.INVALID_STATE_CLASS,
        ),
        ({"unit": "J"}, MeasurementRejectionReason.INVALID_UNIT),
        ({"period_end": "not-a-date"}, MeasurementRejectionReason.INVALID_PERIOD_END),
        (
            {"period_end": "2026-09-04T10:00:00"},
            MeasurementRejectionReason.INVALID_PERIOD_END,
        ),
        ({"period_end": None}, MeasurementRejectionReason.INVALID_PERIOD_END),
    ],
)
def test_reader_rejects_invalid_source_semantics(
    hass: HomeAssistant,
    overrides: dict[str, object],
    reason: MeasurementRejectionReason,
) -> None:
    """Reject states that do not satisfy the cumulative-energy contract."""
    source, entry = _register_source(hass)
    _set_energy_state(hass, entry.entity_id, **overrides)

    assert _only_reason(HomeAssistantEnergyReader(hass, (source,)).read()) is reason


def test_reader_requires_registry_binding_and_current_state(
    hass: HomeAssistant,
) -> None:
    """Distinguish an invalid registry binding from a missing current state."""
    source, entry = _register_source(hass)
    reader = HomeAssistantEnergyReader(hass, (source,))
    assert _only_reason(reader.read()) is MeasurementRejectionReason.SOURCE_MISSING

    entity_id_source = EnergySourceIdentity(
        role="pv",
        registry_id=entry.entity_id,
    )
    _set_energy_state(hass, entry.entity_id)
    assert (
        _only_reason(HomeAssistantEnergyReader(hass, (entity_id_source,)).read())
        is MeasurementRejectionReason.SOURCE_BINDING_MISMATCH
    )

    missing_registry_source = EnergySourceIdentity(
        role="import",
        registry_id="00000000000000000000000000000000",
    )
    assert (
        _only_reason(HomeAssistantEnergyReader(hass, (missing_registry_source,)).read())
        is MeasurementRejectionReason.SOURCE_BINDING_MISMATCH
    )


@pytest.mark.parametrize(
    ("sources", "message"),
    [
        (
            (
                EnergySourceIdentity(role="pv", registry_id="registry-a"),
                EnergySourceIdentity(role="pv", registry_id="registry-b"),
            ),
            "roles must be unique",
        ),
        (
            (
                EnergySourceIdentity(role="pv", registry_id="registry-a"),
                EnergySourceIdentity(role="import", registry_id="registry-a"),
            ),
            "registry ids must be unique",
        ),
    ],
)
def test_reader_requires_one_to_one_source_bindings(
    hass: HomeAssistant,
    sources: tuple[EnergySourceIdentity, ...],
    message: str,
) -> None:
    """Reject duplicate roles and registry UUIDs at the injected boundary."""
    with pytest.raises(ValueError, match=message):
        HomeAssistantEnergyReader(hass, sources)


def test_reader_rejects_non_sensor_registry_entry(hass: HomeAssistant) -> None:
    """Reject a registry UUID whose current entity is not a sensor."""
    source, entry = _register_source(hass, domain="number")
    _set_energy_state(hass, entry.entity_id)

    assert _only_reason(HomeAssistantEnergyReader(hass, (source,)).read()) is (
        MeasurementRejectionReason.SOURCE_BINDING_MISMATCH
    )


def test_reader_validates_last_reported_and_does_not_classify_age(
    hass: HomeAssistant,
) -> None:
    """Require an aware timestamp but leave freshness classification downstream."""
    source, entry = _register_source(hass)
    old_period = datetime(2020, 1, 1, tzinfo=UTC)
    _set_energy_state(
        hass,
        entry.entity_id,
        period_end=old_period.isoformat(),
        reported_at=old_period + timedelta(seconds=30),
    )
    old_sample = _only_sample(HomeAssistantEnergyReader(hass, (source,)).read())
    assert old_sample.period_end == old_period

    state = hass.states.get(entry.entity_id)
    assert state is not None
    state.last_reported = old_period.replace(tzinfo=None)
    assert _only_reason(HomeAssistantEnergyReader(hass, (source,)).read()) is (
        MeasurementRejectionReason.INVALID_LAST_REPORTED
    )


class _CountingReader:
    """Deterministic reader used to verify runner lifecycle ordering."""

    def __init__(self, observations: tuple[EnergyObservation, ...]) -> None:
        self.observations = observations
        self.calls = 0

    def read(self) -> tuple[EnergyObservation, ...]:
        """Count and return one immutable vector."""
        self.calls += 1
        return self.observations


def _patch_utc_timer(
    monkeypatch: pytest.MonkeyPatch,
    callbacks: list[Callable[[datetime], Awaitable[None]]],
    cancellations: list[None],
    cancelled: asyncio.Event,
) -> None:
    """Replace only the supported UTC time tracker."""

    def _track(
        _hass: HomeAssistant,
        action: Callable[[datetime], Awaitable[None]],
        *,
        second: int,
    ) -> Callable[[], None]:
        assert second == 0
        callbacks.append(action)

        def _cancel() -> None:
            cancellations.append(None)
            cancelled.set()

        return _cancel

    monkeypatch.setattr(ha_boundary, "async_track_utc_time_change", _track)


async def test_runner_reads_only_when_utc_minute_timer_fires(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Register one second-zero timer with no immediate, event, or catch-up read."""
    callbacks: list[Callable[[datetime], Awaitable[None]]] = []
    cancellations: list[None] = []
    cancelled = asyncio.Event()
    _patch_utc_timer(monkeypatch, callbacks, cancellations, cancelled)
    reader = _CountingReader(())
    consumed: list[tuple[tuple[EnergyObservation, ...], datetime]] = []

    async def _consume(
        observations: tuple[EnergyObservation, ...],
        observed_at: datetime,
    ) -> None:
        consumed.append((observations, observed_at))

    runner = UtcMinuteRunner(hass, reader, _consume)
    runner.start()

    assert len(callbacks) == 1
    assert reader.calls == 0
    hass.states.async_set("sensor.unrelated", "1")
    await hass.async_block_till_done()
    assert reader.calls == 0

    tick = datetime(2026, 9, 4, 10, 1, 0, 321000, tzinfo=UTC)
    await callbacks[0](tick)

    assert reader.calls == 1
    assert consumed == [((), tick)]
    await runner.async_stop()
    assert len(cancellations) == 1


async def test_runner_stops_timer_then_drains_commit_and_blocks_late_reads(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Let an active commit finish while suppressing queued and later callbacks."""
    callbacks: list[Callable[[datetime], Awaitable[None]]] = []
    cancellations: list[None] = []
    cancelled = asyncio.Event()
    _patch_utc_timer(monkeypatch, callbacks, cancellations, cancelled)
    reader = _CountingReader(())
    commit_started = asyncio.Event()
    allow_commit = asyncio.Event()
    commits = 0

    async def _consume(
        _observations: tuple[EnergyObservation, ...],
        _observed_at: datetime,
    ) -> None:
        nonlocal commits
        commits += 1
        commit_started.set()
        await allow_commit.wait()

    runner = UtcMinuteRunner(hass, reader, _consume)
    runner.start()
    tick = datetime(2026, 9, 4, 10, 1, tzinfo=UTC)
    active = asyncio.create_task(callbacks[0](tick))
    await commit_started.wait()

    stopped = asyncio.Event()

    async def _stop() -> None:
        await runner.async_stop()
        stopped.set()

    stop_task = asyncio.create_task(_stop())
    await cancelled.wait()
    assert not stopped.is_set()

    queued = asyncio.create_task(callbacks[0](tick + timedelta(minutes=1)))
    allow_commit.set()
    await asyncio.gather(active, stop_task, queued)
    await callbacks[0](tick + timedelta(minutes=2))

    assert reader.calls == 1
    assert commits == 1
    assert stopped.is_set()


async def test_runner_skips_tick_while_previous_commit_is_running(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drop a missed tick instead of reading current state after its boundary."""
    callbacks: list[Callable[[datetime], Awaitable[None]]] = []
    cancellations: list[None] = []
    _patch_utc_timer(
        monkeypatch,
        callbacks,
        cancellations,
        asyncio.Event(),
    )
    reader = _CountingReader(())
    commit_started = asyncio.Event()
    allow_commit = asyncio.Event()
    consumed_at: list[datetime] = []

    async def _consume(
        _observations: tuple[EnergyObservation, ...],
        observed_at: datetime,
    ) -> None:
        consumed_at.append(observed_at)
        commit_started.set()
        await allow_commit.wait()

    runner = UtcMinuteRunner(hass, reader, _consume)
    runner.start()
    first_tick = datetime(2026, 9, 4, 10, 1, tzinfo=UTC)
    active = asyncio.create_task(callbacks[0](first_tick))
    await commit_started.wait()

    await callbacks[0](first_tick + timedelta(minutes=1))
    assert reader.calls == 1
    assert consumed_at == [first_tick]

    allow_commit.set()
    await active
    await runner.async_stop()


def test_runner_cannot_register_duplicate_timer(
    hass: HomeAssistant,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reject repeated starts so setup cannot install overlapping timers."""
    callbacks: list[Callable[[datetime], Awaitable[None]]] = []
    cancellations: list[None] = []
    _patch_utc_timer(
        monkeypatch,
        callbacks,
        cancellations,
        asyncio.Event(),
    )

    async def _consume(
        _observations: tuple[EnergyObservation, ...],
        _observed_at: datetime,
    ) -> None:
        return

    runner = UtcMinuteRunner(hass, _CountingReader(()), _consume)
    runner.start()

    with pytest.raises(RuntimeError, match="cannot be started more than once"):
        runner.start()
    assert len(callbacks) == 1
