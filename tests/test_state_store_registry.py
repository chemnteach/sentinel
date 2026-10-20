"""Unit tests for StateStore registry entries.

Cycle 17 sub-step 1B promotes ``semantic_groups`` to a first-class
StateStore citizen. These tests pin the registry contract for that
store: the filename map entry, the list-default convention, and the
write/read round-trip.

Cycle 20 sub-step 2 extends ``StateStore.write`` with a keyword-only
``sort_keys`` parameter (default ``False``) opted into by the
``semantic_groups`` writer migrated from ``grouper.write_groups``.
The tail of this file pins the new kwarg's contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sentinel.orchestrator.state_store import StateStore


def test_semantic_groups_registered_in_files_map(tmp_path: Path) -> None:
    """``semantic_groups`` resolves through the registry to the
    canonical ``semantic_groups.json`` filename."""
    store = StateStore(state_dir=tmp_path)
    resolved = store._path("semantic_groups")
    assert resolved.name == "semantic_groups.json"


def test_semantic_groups_defaults_to_empty_list_when_absent(
    tmp_path: Path,
) -> None:
    """Absent ``semantic_groups`` reads as ``[]`` (not ``{}``) per the
    list-store default-value convention."""
    store = StateStore(state_dir=tmp_path)
    assert store.read("semantic_groups") == []


def test_semantic_groups_round_trips_write_and_read(tmp_path: Path) -> None:
    """A two-element list of group dicts written via ``StateStore.write``
    round-trips through ``StateStore.read`` unchanged."""
    store = StateStore(state_dir=tmp_path)
    payload = [
        {
            "group_id": "g-001",
            "finding_kind": "kind_a",
            "member_finding_ids": ["f-1", "f-2"],
        },
        {
            "group_id": "g-002",
            "finding_kind": "kind_b",
            "member_finding_ids": ["f-3"],
        },
    ]
    store.write("semantic_groups", payload)
    assert store.read("semantic_groups") == payload


# ---------------------------------------------------------------------------
# Cycle 27 sub-step 8-B: concept_themes registry citizen
# ---------------------------------------------------------------------------


def test_concept_themes_registered_in_files_map(tmp_path: Path) -> None:
    """``concept_themes`` resolves through the registry to the canonical
    ``concept_themes.json`` filename."""
    store = StateStore(state_dir=tmp_path)
    assert store._path("concept_themes").name == "concept_themes.json"


def test_concept_themes_defaults_to_empty_list_when_absent(
    tmp_path: Path,
) -> None:
    """Absent ``concept_themes`` reads as ``[]`` per the list-store
    default-value convention."""
    store = StateStore(state_dir=tmp_path)
    assert store.read("concept_themes") == []


def test_concept_themes_round_trips_write_and_read(tmp_path: Path) -> None:
    """A two-element list of concept-theme dicts round-trips through
    ``StateStore.write``/``StateStore.read`` unchanged."""
    store = StateStore(state_dir=tmp_path)
    payload = [
        {
            "concept_theme_id": "C-001",
            "title": "Unchecked Optional dereferences",
            "member_finding_ids": ["F-1", "F-2"],
        },
        {
            "concept_theme_id": "C-002",
            "title": "Missing error handling on I/O",
            "member_finding_ids": ["F-3"],
        },
    ]
    store.write("concept_themes", payload)
    assert store.read("concept_themes") == payload


# ---------------------------------------------------------------------------
# Cycle 20 sub-step 2: StateStore.write sort_keys kwarg
# ---------------------------------------------------------------------------


def test_write_default_sort_keys_false_preserves_insertion_order(
    tmp_path: Path,
) -> None:
    """Default ``sort_keys=False`` preserves Python dict insertion order
    in the on-disk JSON bytes (the 18-store byte-stability contract)."""
    store = StateStore(state_dir=tmp_path)
    payload = {"z": 1, "a": 2}
    store.write("semantic_groups", payload)
    text = (tmp_path / "semantic_groups.json").read_text(encoding="utf-8")
    assert text.index('"z"') < text.index('"a"')


def test_write_sort_keys_true_emits_alphabetic_key_order(
    tmp_path: Path,
) -> None:
    """Opting into ``sort_keys=True`` emits keys alphabetically at the
    JSON-bytes level."""
    store = StateStore(state_dir=tmp_path)
    payload = {"z": 1, "a": 2}
    store.write("semantic_groups", payload, sort_keys=True)
    text = (tmp_path / "semantic_groups.json").read_text(encoding="utf-8")
    assert text.index('"a"') < text.index('"z"')


def test_write_sort_keys_true_byte_identical_across_runs(
    tmp_path: Path,
) -> None:
    """``sort_keys=True`` writes are byte-identical across repeated runs
    given deterministic input — the determinism contract the migrated
    grouper writer relies on."""
    store_a = StateStore(state_dir=tmp_path / "run1")
    store_b = StateStore(state_dir=tmp_path / "run2")
    store_c = StateStore(state_dir=tmp_path / "run3")
    payload = {"z": 1, "a": 2, "m": 3}
    store_a.write("semantic_groups", payload, sort_keys=True)
    store_b.write("semantic_groups", payload, sort_keys=True)
    store_c.write("semantic_groups", payload, sort_keys=True)
    b1 = (tmp_path / "run1" / "semantic_groups.json").read_bytes()
    b2 = (tmp_path / "run2" / "semantic_groups.json").read_bytes()
    b3 = (tmp_path / "run3" / "semantic_groups.json").read_bytes()
    assert b1 == b2 == b3


def test_write_sort_keys_is_keyword_only(tmp_path: Path) -> None:
    """``sort_keys`` must be passed as a keyword argument; positional
    use raises ``TypeError`` (pins the keyword-only API decision)."""
    store = StateStore(state_dir=tmp_path)
    with pytest.raises(TypeError):
        # Positional 5th arg attempts to bind sort_keys positionally.
        store.write("semantic_groups", [], False, None, True)  # type: ignore[misc]
