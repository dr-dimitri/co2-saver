# Copyright (C) 2026 CO2 Saver contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Adversarial codec and revision contracts for full persisted state."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from typing import TYPE_CHECKING, cast

import pytest

from custom_components.co2saver.domain import (
    EmissionDensity,
    Emissions,
    Energy,
    StorageLedger,
)
from custom_components.co2saver.measurement.models import (
    CounterSnapshot,
    EnergyCounterSample,
    EnergySourceIdentity,
    EnergyUnit,
    MeasurementPhase,
    MeasurementPipelineState,
)
from custom_components.co2saver.measurement.storage import (
    VerifiedAtomicStoreConflictError,
)
from custom_components.co2saver.persistence import (
    CumulativeTotals,
    GenerationCodec,
    GenerationRevisionPolicy,
    GenerationState,
    Manifest,
    ManifestCodec,
    ManifestRevisionPolicy,
    storage_identifier,
)

if TYPE_CHECKING:
    from collections.abc import Callable

_STORAGE = "1" * 32
_GENERATION = "2" * 32
_EPOCH = "3" * 32
_HOUSE = "4" * 32
_OTHER = "5" * 32
_FINGERPRINT = "6" * 64
_BOUNDARY = datetime(2026, 9, 5, 12, tzinfo=UTC)
_SOURCES = (EnergySourceIdentity("pv_generation", "source"),)


def _manifest() -> Manifest:
    """Create one unbound bootstrap payload."""
    return Manifest(_STORAGE, _EPOCH, None, _GENERATION)


def _state() -> GenerationState:
    """Create a complete conservative generation with a household timeline."""
    return GenerationState(
        storage_id=_STORAGE,
        owner_entry_id="owner",
        generation=_GENERATION,
        commit_revision=1,
        segment_fingerprint=_FINGERPRINT,
        measurement=MeasurementPipelineState.initial(_SOURCES, _BOUNDARY),
        ledger=StorageLedger.quarantined(Energy(Fraction(10))),
        totals=CumulativeTotals(),
        consumer_totals=((_HOUSE, CumulativeTotals()),),
    )


def _codec() -> GenerationCodec:
    """Bind a generation codec to the physical locator and authoritative owner."""
    return GenerationCodec(_STORAGE, "owner", _GENERATION)


def _set(payload: dict[str, object], path: str, value: object) -> dict[str, object]:
    """Mutate a nested payload field as corruption arriving from disk."""
    names = path.split(".")
    target = payload
    for key in names[:-1]:
        target = cast("dict[str, object]", target[key])
    target[names[-1]] = value
    return payload


@pytest.mark.parametrize(
    "value",
    [None, True, 1, "", "../escape", "a" * 31, "A" * 32, "a" * 32 + ".manifest"],
)
def test_storage_identifiers_reject_noncanonical_paths(value: object) -> None:
    """No persisted identifier can reach file APIs as an unsafe path."""
    with pytest.raises(ValueError, match="storage identifier"):
        storage_identifier(value)


def test_manifest_round_trip_preserves_the_authoritative_pointer() -> None:
    """Bootstrap and initialized state are fully represented in one payload."""
    codec = ManifestCodec(_STORAGE)
    for state in (
        _manifest(),
        replace(
            _manifest(),
            owner_entry_id="owner",
            initialized=True,
            previous_generations=(_OTHER,),
            commit_revision=3,
            repair_reset_at=_BOUNDARY,
            manifest_lost=True,
            repair_pending=True,
            repair_issue_token=_OTHER,
        ),
    ):
        assert codec.decode(codec.encode(state)) == state


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema_version", True, "schema"),
        ("minor_version", 3, "schema"),
        ("storage_id", _OTHER, "foreign"),
        ("manifest_epoch", "not-hex", "storage identifier"),
        ("owner_entry_id", "", "non-empty"),
        ("active_generation", _OTHER + ".corrupt", "storage identifier"),
        ("previous_generations", [_GENERATION], "unique"),
        ("previous_generations", [_OTHER, _OTHER], "unique"),
        ("initialized", True, "requires an owner"),
        ("initialized", 1, "boolean"),
        ("commit_revision", 0, "positive"),
        ("commit_revision", True, "integer"),
        ("repair_reset_at", "2026-09-05T12:00:00Z", "repair requires an owner"),
        ("repair_reset_at", "invalid", "must end in Z"),
        ("manifest_lost", 1, "must be boolean"),
        ("manifest_lost", True, "repair requires an owner"),
        ("repair_pending", 1, "must be boolean"),
        ("repair_pending", True, "requires a reset timestamp"),
        ("repair_issue_token", "invalid", "storage identifier"),
        ("repair_issue_token", _OTHER, "requires a reset"),
        ("extension", "value", "unexpected keys"),
    ],
)
def test_manifest_rejects_malformed_and_foreign_fields(
    field: str, value: object, message: str
) -> None:
    """A correct Store envelope cannot launder invalid manifest state."""
    payload = ManifestCodec.encode(_manifest())
    payload[field] = value
    with pytest.raises(ValueError, match=message):
        ManifestCodec(_STORAGE).decode(payload)


@pytest.mark.parametrize(
    "change",
    [
        {"commit_revision": 2},
        {"owner_entry_id": "owner"},
        {"initialized": True},
        {"previous_generations": (_OTHER,)},
        {"repair_reset_at": _BOUNDARY},
        {"manifest_lost": True},
        {"repair_pending": True},
        {"repair_issue_token": _OTHER},
    ],
)
def test_initial_manifest_requires_pristine_reservation(
    change: dict[str, object],
) -> None:
    """Owner and generation initialization only happen after entry creation."""
    with pytest.raises(ValueError, match="unbound bootstrap"):
        ManifestRevisionPolicy.validate_initial(replace(_manifest(), **change))


def test_manifest_policy_accepts_binding_then_initialization() -> None:
    """Only the two required setup mutations advance the existing pointer."""
    before = _manifest()
    ManifestRevisionPolicy.validate_initial(before)
    bound = replace(before, owner_entry_id="owner", commit_revision=2)
    initialized = replace(bound, initialized=True, commit_revision=3)
    ManifestRevisionPolicy.validate_transition(before, bound)
    ManifestRevisionPolicy.validate_transition(bound, initialized)
    assert ManifestRevisionPolicy.revision(initialized) == 3


@pytest.mark.parametrize(
    "change",
    [
        {"storage_id": _OTHER},
        {"manifest_epoch": _OTHER},
        {"active_generation": _OTHER},
        {"previous_generations": (_OTHER,)},
        {"initialized": False},
        {"owner_entry_id": None},
        {"owner_entry_id": "foreign"},
        {"repair_reset_at": _BOUNDARY},
        {"manifest_lost": True},
        {"repair_pending": True},
        {"repair_issue_token": _OTHER},
    ],
)
def test_manifest_policy_rejects_identity_replacement(
    change: dict[str, object],
) -> None:
    """Unrequested repair or reownership is never a normal state transition."""
    before = replace(
        _manifest(), owner_entry_id="owner", initialized=True, commit_revision=3
    )
    with pytest.raises(
        VerifiedAtomicStoreConflictError, match="authoritative identity"
    ):
        ManifestRevisionPolicy.validate_transition(
            before, replace(before, commit_revision=4, **change)
        )


def test_generation_round_trip_keeps_exact_ledger_components_and_negative_nets() -> (
    None
):
    """Finite Store JSON preserves nonterminating rationals and burden history."""
    totals = CumulativeTotals(
        direct_pv_kwh=Fraction(1, 3),
        storage_pv_kwh=Fraction(1, 7),
        direct_gross_g=Fraction(10),
        direct_pv_burden_g=Fraction(20),
        storage_gross_g=Fraction(3),
        storage_pv_burden_g=Fraction(8),
        storage_burden_g=Fraction(1),
        unvalued_direct_kwh=Fraction(1, 6),
        unvalued_storage_kwh=Fraction(1, 14),
    )
    state = replace(
        _state(),
        totals=totals,
        consumer_totals=((_HOUSE, totals),),
        repair_reset_at=_BOUNDARY,
    )
    decoded = _codec().decode(_codec().encode(state))
    assert decoded == state
    assert decoded.totals.direct_net_g == -10
    assert decoded.totals.storage_net_g == -6
    assert decoded.repair_reset_at == _BOUNDARY


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema_version", 2, "schema"),
        ("minor_version", 2, "schema"),
        ("owner_entry_id", "foreign", "foreign"),
        ("generation", _OTHER, "foreign"),
        ("segment_fingerprint", "bad", "fingerprint"),
        ("segment_fingerprint", None, "fingerprint"),
        ("ledger", [], "object"),
        ("ledger.non_pv_upper_kwh", {"numerator": 0, "denominator": 1}, "non-PV"),
        (
            "ledger.stored_lower_kwh",
            {"numerator": 11, "denominator": 1},
            "stored bounds",
        ),
        ("ledger.pv_burden_g", {"numerator": 1, "denominator": 1}, "density envelope"),
        ("totals.direct_pv_kwh", {"numerator": 2, "denominator": 2}, "reduced"),
        ("totals.direct_pv_kwh", {"numerator": -1, "denominator": 1}, "non-negative"),
        ("totals.direct_pv_kwh", {"numerator": 1, "denominator": 1}, "must equal"),
        ("totals.unvalued_storage_kwh", {"numerator": 1, "denominator": 1}, "unvalued"),
        ("unassigned_direct_kwh", {"numerator": -1, "denominator": 1}, "must equal"),
        ("unassigned_storage_kwh", {"numerator": 1, "denominator": 1}, "must equal"),
        ("consumer_totals", [], "consumer IDs"),
        ("consumer_totals", [{"consumer_id": _HOUSE, "totals": {}}], "unexpected keys"),
        ("diagnostics", [], "diagnostics must be"),
        ("diagnostics", {"": 0}, "non-empty"),
        ("diagnostics", {"failure": -1}, "must not be negative"),
        ("repair_reset_at", "2026-09-05", "must end in Z"),
        ("extension", 1, "unexpected keys"),
    ],
)
def test_generation_rejects_semantic_corruption(
    field: str, value: object, message: str
) -> None:
    """Malformed persisted state cannot become valid-looking result state."""
    payload = _set(_codec().encode(_state()), field, value)
    with pytest.raises(ValueError, match=message):
        _codec().decode(payload)


@pytest.mark.parametrize("ids", [(_HOUSE, _HOUSE), (_OTHER, _HOUSE)])
def test_generation_rejects_duplicate_or_unsorted_historical_consumers(
    ids: tuple[str, ...],
) -> None:
    """Canonical historical identities cannot alias or depend on row ordering."""
    state = replace(
        _state(),
        consumer_totals=tuple((consumer_id, CumulativeTotals()) for consumer_id in ids),
    )
    with pytest.raises(ValueError, match="consumer IDs"):
        _codec().decode(_codec().encode(state))


@pytest.mark.parametrize("value", [Fraction(-1), 0, 0.0, True])
def test_cumulative_totals_require_nonnegative_exact_values(value: object) -> None:
    """Binary floats and booleans never enter the exact persistent ledger."""
    with pytest.raises(ValueError, match="non-negative Fractions"):
        CumulativeTotals(direct_gross_g=cast("Fraction", value))


def test_unvalued_direct_energy_is_bounded_by_delivered_energy() -> None:
    """A missing intensity is only counted for otherwise proven PV delivery."""
    with pytest.raises(ValueError, match="unvalued energy"):
        CumulativeTotals(unvalued_direct_kwh=Fraction(1))


def _active_measurement() -> MeasurementPipelineState:
    """Create a valid post-boundary baseline for transition rejection tests."""
    snapshot = CounterSnapshot(
        (
            EnergyCounterSample(
                _SOURCES[0],
                Energy(Fraction(1)),
                EnergyUnit.KILOWATT_HOUR,
                _BOUNDARY,
                _BOUNDARY,
            ),
        )
    )
    return replace(
        _state().measurement,
        phase=MeasurementPhase.ACTIVE,
        baseline=snapshot,
        revision=1,
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda state: replace(state, commit_revision=2),
        lambda state: replace(state, measurement=_active_measurement()),
        lambda state: replace(
            state, totals=CumulativeTotals(direct_gross_g=Fraction(1))
        ),
        lambda state: replace(
            state,
            consumer_totals=((_HOUSE, CumulativeTotals(direct_gross_g=Fraction(1))),),
        ),
        lambda state: replace(state, diagnostics=(("discarded_intervals", 1),)),
        lambda state: replace(state, unassigned_direct_kwh=Fraction(1)),
        lambda state: replace(state, unassigned_storage_kwh=Fraction(1)),
        lambda state: replace(
            state, ledger=replace(state.ledger, stored_lower=Energy(Fraction(1)))
        ),
    ],
)
def test_initial_generation_cannot_contain_prior_results_or_provenance(
    mutate: Callable[[GenerationState], GenerationState],
) -> None:
    """Fresh generation means zero cumulative history and unknown storage."""
    with pytest.raises(ValueError, match="empty and quarantined"):
        GenerationRevisionPolicy.validate_initial(mutate(_state()))


def test_generation_policy_accepts_pristine_no_storage_and_monotonic_state() -> None:
    """Valid initial and cumulative transitions require no accounting adapter."""
    before = replace(_state(), ledger=None)
    GenerationRevisionPolicy.validate_initial(before)
    after = replace(
        before, commit_revision=2, totals=CumulativeTotals(direct_gross_g=Fraction(1))
    )
    GenerationRevisionPolicy.validate_transition(before, after)
    assert GenerationRevisionPolicy.revision(after) == 2
    assert _codec().decode(_codec().encode(before)) == before


@pytest.mark.parametrize(
    "change",
    [
        {"storage_id": _OTHER},
        {"owner_entry_id": "foreign"},
        {"generation": _OTHER},
        {"repair_reset_at": _BOUNDARY},
    ],
)
def test_generation_transition_cannot_change_durable_identity(
    change: dict[str, object],
) -> None:
    """A repair needs a new generation rather than overwriting current history."""
    before = _state()
    with pytest.raises(ValueError, match="durable identity"):
        GenerationRevisionPolicy.validate_transition(
            before, replace(before, commit_revision=2, **change)
        )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda state: replace(state, totals=CumulativeTotals()), "monotonic"),
        (
            lambda state: replace(
                state, consumer_totals=((_OTHER, CumulativeTotals()),)
            ),
            "retained",
        ),
        (
            lambda state: replace(
                state, consumer_totals=((_HOUSE, CumulativeTotals()),)
            ),
            "monotonic",
        ),
        (lambda state: replace(state, diagnostics=()), "diagnostic"),
        (lambda state: replace(state, unassigned_direct_kwh=Fraction()), "unassigned"),
        (lambda state: replace(state, unassigned_storage_kwh=Fraction()), "unassigned"),
    ],
)
def test_generation_transition_cannot_erase_history(
    mutate: Callable[[GenerationState], GenerationState], message: str
) -> None:
    """Every positive component and removed consumer's prior history survives."""
    totals = CumulativeTotals(direct_gross_g=Fraction(1))
    before = replace(
        _state(),
        totals=totals,
        consumer_totals=((_HOUSE, totals),),
        diagnostics=(("discarded_intervals", 1),),
        unassigned_direct_kwh=Fraction(1),
        unassigned_storage_kwh=Fraction(1),
    )
    with pytest.raises(ValueError, match=message):
        GenerationRevisionPolicy.validate_transition(
            before, mutate(replace(before, commit_revision=2))
        )


def _next_segment() -> GenerationState:
    """Return a valid empty new segment at the next explicit boundary."""
    before = _state()
    return replace(
        before,
        commit_revision=2,
        segment_fingerprint="7" * 64,
        measurement=MeasurementPipelineState.initial(
            _SOURCES, _BOUNDARY + timedelta(minutes=1)
        ),
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda state: replace(state, measurement=_active_measurement()),
        lambda state: replace(
            state,
            measurement=MeasurementPipelineState.initial(
                _SOURCES, _BOUNDARY - timedelta(minutes=1)
            ),
        ),
        lambda state: replace(
            state, ledger=replace(state.ledger, stored_lower=Energy(Fraction(1)))
        ),
        lambda state: replace(
            state, totals=CumulativeTotals(direct_gross_g=Fraction(1))
        ),
        lambda state: replace(
            state,
            consumer_totals=((_HOUSE, CumulativeTotals(direct_gross_g=Fraction(1))),),
        ),
    ],
)
def test_segment_boundary_cannot_book_results_or_keep_old_provenance(
    mutate: Callable[[GenerationState], GenerationState],
) -> None:
    """A changed fingerprint requires only baseline reset and quarantine."""
    with pytest.raises(ValueError, match="preserve history and quarantine"):
        GenerationRevisionPolicy.validate_transition(_state(), mutate(_next_segment()))


def test_segment_boundary_accepts_new_sources_and_storage_removal() -> None:
    """New sources or battery topology affect only a clean future baseline."""
    before = _state()
    after = replace(
        _next_segment(),
        ledger=None,
        measurement=MeasurementPipelineState.initial(
            (EnergySourceIdentity("local_load", "replacement"),),
            _BOUNDARY + timedelta(minutes=1),
        ),
    )
    GenerationRevisionPolicy.validate_transition(before, after)
    assert _codec().decode(_codec().encode(after)) == after


def test_codec_round_trip_preserves_filled_provenance_envelope() -> None:
    """The burden upper envelope and every energy bound survive restart."""
    state = replace(
        _state(),
        ledger=StorageLedger(
            capacity=Energy(Fraction(10)),
            stored_lower=Energy(Fraction(4)),
            stored_upper=Energy(Fraction(6)),
            pv_lower=Energy(Fraction(2)),
            pv_burden=Emissions(Fraction(80)),
            pv_density_upper=EmissionDensity(Fraction(40)),
        ),
    )
    assert _codec().decode(_codec().encode(state)) == state
