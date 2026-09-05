# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""CO2 Saver integration setup."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.const import Platform
from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.helper_integration import async_handle_source_entity_changes

from .bootstrap import PersistedRuntime, async_setup_storage
from .config_factors import HomeAssistantGridIntensityReader
from .config_plan import all_source_registry_ids, canonical_plan
from .const import DOMAIN
from .evaluation import EvaluationOutcome, EvaluationPlan, evaluate_observations
from .flow_commit import async_release_visible_create
from .measurement.ha import HomeAssistantEnergyReader, UtcMinuteRunner
from .measurement.models import MeasurementPhase
from .measurement.storage import VerifiedAtomicStoreError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .measurement.models import EnergyObservation
    from .persistence import GenerationState

    type Co2SaverConfigEntry = ConfigEntry[EntryRuntime]

_LOGGER = logging.getLogger(__name__)
_PLATFORMS = (Platform.SENSOR,)


@dataclass(slots=True)
class EntryRuntime(PersistedRuntime):
    """One verified generation and its live observation status."""

    runner: UtcMinuteRunner | None = None
    available: bool = False
    status: str = "awaiting_observation"
    failed: bool = False
    update_signal: str = ""


def _start_runner(
    hass: HomeAssistant, runtime: EntryRuntime, data: Mapping[str, object]
) -> None:
    """Bind immutable configuration and one synchronous CO₂ read to each tick."""
    plan = EvaluationPlan.from_config(data)
    energy_reader = HomeAssistantEnergyReader(hass, runtime.state.measurement.sources)
    grid_reader = HomeAssistantGridIntensityReader(hass, plan.grid_source_registry_id)

    async def consume(
        observations: tuple[EnergyObservation, ...], observed_at: datetime
    ) -> None:
        """Publish only the fully verified result of the captured physical poll."""
        if runtime.failed:
            return
        previous = (runtime.state, runtime.available, runtime.status)
        sample, sample_error = grid_reader.read()
        outcome: EvaluationOutcome | None = None

        def transform(state: GenerationState) -> GenerationState:
            """Evaluate against durable state, never an unverified runtime cache."""
            nonlocal outcome
            outcome = evaluate_observations(
                state,
                observations,
                observed_at,
                plan=plan,
                current_grid_sample=sample,
            )
            return outcome.state

        try:
            committed = await runtime.store.async_transact(transform)
        except (OSError, ValueError, VerifiedAtomicStoreError) as err:
            runtime.failed = True
            runtime.available = False
            runtime.status = "storage_error"
            runner.request_stop()
            async_dispatcher_send(hass, runtime.update_signal)
            _LOGGER.exception(
                "CO2 Saver stopped after an unverifiable state commit (%s)",
                type(err).__name__,
            )
            return
        if outcome is None:  # pragma: no cover - synchronous Store contract
            raise RuntimeError
        runtime.state = committed
        grid_error = sample_error or outcome.grid_error
        if outcome.measurement_fault is not None:
            runtime.status = outcome.measurement_fault.reason.value
        elif outcome.storage_error is not None:
            runtime.status = outcome.storage_error.value
        elif grid_error is not None:
            runtime.status = grid_error
        else:
            runtime.status = (
                "ok"
                if committed.measurement.phase is MeasurementPhase.ACTIVE
                else committed.measurement.phase.value
            )
        runtime.available = runtime.status == "ok"
        if previous != (runtime.state, runtime.available, runtime.status):
            async_dispatcher_send(hass, runtime.update_signal)

    runner = UtcMinuteRunner(hass, energy_reader, consume)
    runtime.runner = runner
    runner.start()


def _validated_sources(
    hass: HomeAssistant, entry: Co2SaverConfigEntry
) -> tuple[str, ...]:
    """Require a complete plan and live registry bindings before activation."""
    canonical_plan(entry.data)
    sources = all_source_registry_ids(entry.data)
    registry = er.async_get(hass)
    if any(
        (registered := registry.async_get(source)) is None
        or registered.disabled_by is not None
        for source in sources
    ):
        message = "A configured source was removed or disabled; reconfigure the plant"
        raise ConfigEntryError(message)
    return sources


@callback
def _keep_registry_identity(_entity_id: str) -> None:
    """Registry UUIDs are authoritative; entity-ID renames change no settings."""


async def async_setup_entry(
    hass: HomeAssistant,
    entry: Co2SaverConfigEntry,
) -> bool:
    """Bind and verify storage before registering source lifecycle callbacks."""
    await async_release_visible_create(hass, entry)
    try:
        _validated_sources(hass, entry)
        persisted = await async_setup_storage(hass, entry)
        sources = _validated_sources(hass, entry)
    except (KeyError, OSError, ValueError, VerifiedAtomicStoreError) as err:
        message = "CO2 Saver configuration or stored state is invalid"
        raise ConfigEntryError(message) from err
    runtime = EntryRuntime(
        store=persisted.store,
        state=persisted.state,
        update_signal=f"{DOMAIN}_{entry.entry_id}_updated",
    )
    entry.runtime_data = runtime

    async def source_removed() -> None:
        """Stop this entry and require source reconfiguration on the next setup."""
        await hass.config_entries.async_reload(entry.entry_id)

    for source in sources:
        entry.async_on_unload(
            async_handle_source_entity_changes(
                hass,
                helper_config_entry_id=entry.entry_id,
                set_source_entity_id_or_uuid=_keep_registry_identity,
                source_device_id=None,
                source_entity_id_or_uuid=source,
                source_entity_removed=source_removed,
            )
        )
    await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)
    _start_runner(hass, runtime, entry.data)
    return True


async def async_unload_entry(
    hass: HomeAssistant,
    entry: Co2SaverConfigEntry,
) -> bool:
    """Unload a CO2 Saver config entry."""
    runtime = entry.runtime_data
    if runtime.runner is not None:
        await runtime.runner.async_stop()
    if await hass.config_entries.async_unload_platforms(entry, _PLATFORMS):
        runtime.available = False
        return True
    if runtime.runner is not None and not runtime.failed:
        _start_runner(hass, runtime, entry.data)
    return False
