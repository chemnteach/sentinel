"""Tests for sentinel.assess.grouper (Phase 2 semantic grouping layer).

Seven test classes organized by concern:

* ``TestModeClassification`` — registry / half_registry / legacy mode
  detection from finding shape.
* ``TestGroupingKey`` — subject_key parsing, registry key construction,
  legacy key construction.
* ``TestPairAwareResolution`` — canonical_emitter + peer_emitters
  resolution; per-group conflict raise; clean cases.
* ``TestSemanticGroupConstruction`` — end-to-end ``group()`` round-trip
  integrity over synthetic finding sets.
* ``TestPersistence`` — ``write_groups`` / ``read_groups`` round-trip;
  ``FileAuditLog`` JSONL append-only semantics.
* ``TestErrorPaths`` — ``MalformedSubjectKeyError`` and
  ``UnknownFindingKindError`` under policy variants.
* ``TestDeterminism`` — stability of group ordering, index shape, and
  ``write_groups`` byte output.

Conventions:

* Module-level builders construct finding dicts in the shape consumed
  by ``SemanticGrouper.group``. The half-registry builder enforces the
  "exactly one of finding_kind / subject_key" constraint at
  construction time.
* ``_make_fake_registry`` returns a ``types.SimpleNamespace`` exposing
  only the ``iter_table_2_relationships`` attribute — the sole surface
  the grouper actually uses.
* ``_stable_id`` derives a deterministic synthetic finding_id via
  sha256, marked with an ``f2-`` prefix to distinguish from real F2
  ids.
* ``_RecordingAuditLog`` is a Protocol-conforming in-memory sink used
  by tests that need to assert audit emission without filesystem
  involvement; the one ``FileAuditLog`` round-trip test in
  ``TestPersistence`` covers the JSONL shape.
"""

from __future__ import annotations

import hashlib
import json
import random
import types
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from sentinel.assess import subject_key_registry as real_registry
from sentinel.assess.grouper import (
    AUDIT_FILENAME,
    GROUPS_FILENAME,
    FileAuditLog,
    HalfRegistryAuditEntry,
    MalformedSubjectKeyAuditEntry,
    MalformedSubjectKeyError,
    Provenance,
    SemanticGroup,
    SemanticGrouper,
    UnknownFindingKindError,
    _build_kind_index_from,
    read_groups,
)
from sentinel.assess.subject_key_registry import Table2Relationship

from tests.conftest import write_semantic_groups


# ── Module-level helpers ─────────────────────────────────────────────


def _stable_id(
    *,
    finding_kind: str = "",
    subject_key: str = "",
    file: str = "",
    function: str = "",
    skill: str = "",
    category: str = "",
) -> str:
    """Synthetic finding_id with ``f2-`` prefix and 10-hex sha256.

    Keyword-only with a fixed field-order tuple so two builders that
    omit the same field produce the same hash component for that
    position. Independent of ``generate_finding_id_v2``.
    """
    parts = (finding_kind, subject_key, file, function, skill, category)
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return "f2-" + digest[:10]


def _make_fake_registry(
    relationships: Sequence[Table2Relationship],
) -> types.SimpleNamespace:
    """Fake registry module exposing only ``iter_table_2_relationships``.

    Returns a fresh iterator on each call so the helper survives
    repeated traversal if grouper internals change.
    """
    return types.SimpleNamespace(
        iter_table_2_relationships=lambda: iter(relationships),
    )


def _make_registry_finding(
    *,
    finding_kind: str = "missing_transaction_boundary",
    subject_key: str = "transaction_boundary:db.py:save",
    skill: str = "layer2d-data-integrity-specialist",
    severity: str = "P2",
    file: str = "src/db.py",
    function: str = "save",
    category: str = "data-integrity",
    layers: Sequence[str] = (),
    finding_id: str | None = None,
) -> dict[str, Any]:
    """Registry-mode finding (both subject_key and finding_kind set)."""
    if finding_id is None:
        finding_id = _stable_id(
            finding_kind=finding_kind,
            subject_key=subject_key,
            file=file,
            function=function,
            skill=skill,
            category=category,
        )
    return {
        "id": finding_id,
        "subject_key": subject_key,
        "finding_kind": finding_kind,
        "file": file,
        "function": function,
        "category": category,
        "severity": severity,
        "skill": skill,
        "layers": list(layers),
    }


def _make_legacy_finding(
    *,
    skill: str = "layer2a-quick-review",
    severity: str = "P3",
    file: str = "src/util.py",
    function: str = "helper",
    category: str = "maintainability",
    layers: Sequence[str] = (),
    finding_id: str | None = None,
) -> dict[str, Any]:
    """Legacy-mode finding (neither subject_key nor finding_kind)."""
    if finding_id is None:
        finding_id = _stable_id(
            file=file, function=function, skill=skill, category=category,
        )
    return {
        "id": finding_id,
        "subject_key": None,
        "finding_kind": None,
        "file": file,
        "function": function,
        "category": category,
        "severity": severity,
        "skill": skill,
        "layers": list(layers),
    }


def _make_half_registry_finding(
    *,
    finding_kind: str | None = None,
    subject_key: str | None = None,
    skill: str = "layer3d-error-handling-specialist",
    severity: str = "P2",
    file: str = "src/api.py",
    function: str = "fetch",
    category: str = "error-handling",
    layers: Sequence[str] = (),
    finding_id: str | None = None,
) -> dict[str, Any]:
    """Half-registry finding: exactly one of finding_kind / subject_key.

    Enforces the constraint at construction time so a test cannot
    accidentally build a registry-mode (both) or legacy-mode (neither)
    finding through this helper.
    """
    has_kind = finding_kind is not None
    has_subject = subject_key is not None
    if has_kind == has_subject:
        raise ValueError(
            "half-registry finding requires exactly one of "
            "finding_kind / subject_key (got both or neither)"
        )
    if finding_id is None:
        finding_id = _stable_id(
            finding_kind=finding_kind or "",
            subject_key=subject_key or "",
            file=file,
            function=function,
            skill=skill,
            category=category,
        )
    return {
        "id": finding_id,
        "subject_key": subject_key,
        "finding_kind": finding_kind,
        "file": file,
        "function": function,
        "category": category,
        "severity": severity,
        "skill": skill,
        "layers": list(layers),
    }


class _RecordingAuditLog:
    """In-memory ``AuditLog`` Protocol implementation for assertions."""

    def __init__(self) -> None:
        self.half_registry_entries: list[HalfRegistryAuditEntry] = []
        self.malformed_subject_key_entries: list[
            MalformedSubjectKeyAuditEntry
        ] = []

    def record_half_registry(self, entry: HalfRegistryAuditEntry) -> None:
        self.half_registry_entries.append(entry)

    def record_malformed_subject_key(
        self, entry: MalformedSubjectKeyAuditEntry
    ) -> None:
        self.malformed_subject_key_entries.append(entry)


def _canonicalize_kind_index(
    index: dict[str, dict[str, tuple[Any, ...]]],
) -> tuple[Any, ...]:
    """Reduce the kind index to a sorted-tuples canonical form so
    equality assertions are honest about "identical shape" regardless
    of dict iteration order."""
    return tuple(sorted(
        (
            kind,
            tuple(sorted(
                (
                    emitter,
                    tuple(sorted(
                        (
                            r.other_emitter,
                            r.canonical_emitter or "",
                            r.consolidation,
                            r.table_label,
                        )
                        for r in rules
                    )),
                )
                for emitter, rules in by_emitter.items()
            )),
        )
        for kind, by_emitter in index.items()
    ))


# ── Tests ────────────────────────────────────────────────────────────


class TestModeClassification:
    """Mode detection from finding shape (externally-visible
    ``SemanticGroup.mode`` field, also mirrored on ``provenance.mode``).
    """

    def test_finding_with_subject_and_kind_classes_as_registry(self) -> None:
        grouper = SemanticGrouper()
        finding = _make_registry_finding()
        groups = grouper.group([finding])
        assert len(groups) == 1
        assert groups[0].mode == "registry"
        assert groups[0].provenance.mode == "registry"

    def test_finding_with_kind_only_classes_as_half_registry(self) -> None:
        grouper = SemanticGrouper()
        finding = _make_half_registry_finding(finding_kind="error_signal_loss")
        groups = grouper.group([finding])
        assert len(groups) == 1
        assert groups[0].mode == "half_registry"

    def test_finding_with_subject_only_classes_as_half_registry(self) -> None:
        grouper = SemanticGrouper()
        finding = _make_half_registry_finding(
            subject_key="catch_site:src/api.py:fetch"
        )
        groups = grouper.group([finding])
        assert len(groups) == 1
        assert groups[0].mode == "half_registry"

    def test_finding_with_neither_classes_as_legacy(self) -> None:
        grouper = SemanticGrouper()
        finding = _make_legacy_finding()
        groups = grouper.group([finding])
        assert len(groups) == 1
        assert groups[0].mode == "legacy"

    def test_mixed_input_produces_groups_in_each_mode(self) -> None:
        grouper = SemanticGrouper()
        findings = [
            _make_registry_finding(),
            _make_half_registry_finding(finding_kind="error_signal_loss"),
            _make_legacy_finding(),
        ]
        groups = grouper.group(findings)
        modes = {g.mode for g in groups}
        assert modes == {"registry", "half_registry", "legacy"}

    def test_half_registry_builder_rejects_both_fields(self) -> None:
        with pytest.raises(ValueError):
            _make_half_registry_finding(
                finding_kind="error_signal_loss",
                subject_key="catch_site:x:y",
            )

    def test_half_registry_builder_rejects_neither_field(self) -> None:
        with pytest.raises(ValueError):
            _make_half_registry_finding()


class TestGroupingKey:
    """Grouping-key construction for each mode."""

    def test_registry_key_format(self) -> None:
        grouper = SemanticGrouper()
        # Input carries surface-variance form (src/ prefix); G7 normalization
        # strips it before key construction.
        finding = _make_registry_finding(
            finding_kind="missing_transaction_boundary",
            subject_key="transaction_boundary:src/db.py:save",
        )
        groups = grouper.group([finding])
        expected = (
            "registry|transaction_boundary:db.py:save"
            "|missing_transaction_boundary"
        )
        assert groups[0].grouping_key == expected
        assert groups[0].provenance.grouping_key == expected

    def test_half_registry_key_uses_kind_when_present(self) -> None:
        grouper = SemanticGrouper()
        finding = _make_half_registry_finding(
            finding_kind="error_signal_loss",
            file="src/api.py",
            function="fetch",
        )
        groups = grouper.group([finding])
        assert groups[0].grouping_key == (
            "half|src/api.py|fetch|error_signal_loss"
        )

    def test_half_registry_key_uses_subject_when_only_subject_present(
        self,
    ) -> None:
        grouper = SemanticGrouper()
        finding = _make_half_registry_finding(
            subject_key="catch_site:src/api.py:fetch",
            file="src/api.py",
            function="fetch",
        )
        groups = grouper.group([finding])
        # G7 normalization strips src/ from the subject_key body; file path
        # in the bucket key is unaffected (different field).
        assert groups[0].grouping_key == (
            "half|src/api.py|fetch|catch_site:api.py:fetch"
        )

    def test_legacy_key_uses_file_function_category(self) -> None:
        grouper = SemanticGrouper()
        finding = _make_legacy_finding(
            file="src/util.py",
            function="helper",
            category="maintainability",
        )
        groups = grouper.group([finding])
        assert groups[0].grouping_key == (
            "legacy|src/util.py|helper|maintainability"
        )

    def test_two_registry_findings_with_same_key_bucket_together(self) -> None:
        grouper = SemanticGrouper()
        a = _make_registry_finding(skill="layer2d-data-integrity-specialist")
        b = _make_registry_finding(
            skill="layer3d-error-handling-specialist",
            finding_id="f2-distinct",
        )
        groups = grouper.group([a, b])
        assert len(groups) == 1
        assert groups[0].member_count == 2

    def test_two_registry_findings_with_different_subjects_split(self) -> None:
        grouper = SemanticGrouper()
        a = _make_registry_finding(
            subject_key="transaction_boundary:src/db.py:save"
        )
        b = _make_registry_finding(
            subject_key="transaction_boundary:src/db.py:delete",
            finding_id="f2-distinct",
        )
        groups = grouper.group([a, b])
        assert len(groups) == 2


class TestPairAwareResolution:
    """Canonical + peer resolution via the pair-aware kind index."""

    def test_real_registry_missing_txn_pair_resolves_layer2d(self) -> None:
        grouper = SemanticGrouper()
        a = _make_registry_finding(
            finding_kind="missing_transaction_boundary",
            subject_key="transaction_boundary:src/db.py:save",
            skill="layer2d-data-integrity-specialist",
            layers=("layer2d-data-integrity-specialist",),
        )
        b = _make_registry_finding(
            finding_kind="missing_transaction_boundary",
            subject_key="transaction_boundary:src/db.py:save",
            skill="layer3d-error-handling-specialist",
            layers=("layer3d-error-handling-specialist",),
            finding_id="f2-second",
        )
        groups = grouper.group([a, b])
        assert len(groups) == 1
        assert groups[0].canonical_emitter == "layer2d-data-integrity-specialist"

    def test_real_registry_silent_data_loss_pair_resolves_layer2d(
        self,
    ) -> None:
        grouper = SemanticGrouper()
        a = _make_registry_finding(
            finding_kind="silent_data_loss",
            subject_key="external_call:dep:src/api.py:call",
            skill="layer2d-data-integrity-specialist",
            layers=("layer2d-data-integrity-specialist",),
        )
        b = _make_registry_finding(
            finding_kind="silent_data_loss",
            subject_key="external_call:dep:src/api.py:call",
            skill="layer3e-concurrency-specialist",
            layers=("layer3e-concurrency-specialist",),
            finding_id="f2-second",
        )
        groups = grouper.group([a, b])
        assert len(groups) == 1
        assert groups[0].canonical_emitter == "layer2d-data-integrity-specialist"

    def test_blocking_io_layer3a_layer3d_pair_no_shared_other(self) -> None:
        """Layer3a + layer3d are not paired with each other in Table 2;
        both only pair with layer3e. ``present_pair_rules`` is empty
        (no rule's ``other_emitter`` is in the contributing set), so
        canonical falls back to ``_NO_CANONICAL_EMITTER``. Peers
        come from ``relevant_rules`` (both rules' ``other`` is
        layer3e), so peer set is the singleton ``layer3e``."""
        grouper = SemanticGrouper()
        a = _make_registry_finding(
            finding_kind="blocking_io_in_async",
            subject_key="await_site:src/api.py:fetch",
            skill="layer3a-performance-specialist",
            layers=("layer3a-performance-specialist",),
        )
        b = _make_registry_finding(
            finding_kind="blocking_io_in_async",
            subject_key="await_site:src/api.py:fetch",
            skill="layer3d-error-handling-specialist",
            layers=("layer3d-error-handling-specialist",),
            finding_id="f2-second",
        )
        groups = grouper.group([a, b])
        assert len(groups) == 1
        assert groups[0].canonical_emitter == ""
        assert groups[0].peer_emitters == ("layer3e-concurrency-specialist",)

    def test_unknown_finding_kind_under_form_empty_group_yields_no_canonical(
        self,
    ) -> None:
        """A registered kind with no Table 2 rule resolves cleanly to
        ``_NO_CANONICAL_EMITTER`` with empty peers. Uses ``logic_bug``,
        which is registered but absent from Table 2."""
        grouper = SemanticGrouper()
        finding = _make_registry_finding(
            finding_kind="logic_bug",
            subject_key="method:src/x.py:Foo:bar",
            skill="layer2a-quick-review",
            layers=("layer2a-quick-review",),
        )
        groups = grouper.group([finding])
        assert len(groups) == 1
        assert groups[0].canonical_emitter == ""
        assert groups[0].peer_emitters == ()

    def test_synthetic_conflict_attaches_conflict_record(
        self,
    ) -> None:
        """Fake registry with two pair rules whose canonicals disagree
        on a shared kind. A group whose contributors include the
        shared central emitter (skillA) plus both peripheral emitters
        (skillB, skillC) attaches a populated
        ``canonical_emitter_conflict`` ConflictRecord to the group
        (per-group quarantine; no raise propagates)."""
        fake_relationships = [
            Table2Relationship(
                emitter_a="skillA",
                emitter_b="skillB",
                table_label="2-TEST-X",
                shared_kinds=("error_signal_loss",),
                shared_namespaces=("test:",),
                relationship="boundary_discipline",
                consolidation="collapse_on_shared_subject_key",
                canonical_emitter="skillA",
                note="synthetic for grouper conflict test",
            ),
            Table2Relationship(
                emitter_a="skillA",
                emitter_b="skillC",
                table_label="2-TEST-Y",
                shared_kinds=("error_signal_loss",),
                shared_namespaces=("test:",),
                relationship="boundary_discipline",
                consolidation="collapse_on_shared_subject_key",
                canonical_emitter="skillC",
                note="synthetic for grouper conflict test",
            ),
        ]
        fake = _make_fake_registry(fake_relationships)
        grouper = SemanticGrouper(registry_module=fake)
        findings = [
            _make_registry_finding(
                finding_kind="error_signal_loss",
                subject_key="test:foo:bar",
                skill="skillA",
                layers=("skillA",),
                finding_id="f2-a",
            ),
            _make_registry_finding(
                finding_kind="error_signal_loss",
                subject_key="test:foo:bar",
                skill="skillB",
                layers=("skillB",),
                finding_id="f2-b",
            ),
            _make_registry_finding(
                finding_kind="error_signal_loss",
                subject_key="test:foo:bar",
                skill="skillC",
                layers=("skillC",),
                finding_id="f2-c",
            ),
        ]
        groups = grouper.group(findings)
        assert len(groups) == 1
        group = groups[0]
        assert group.canonical_emitter_conflict is not None
        record = group.canonical_emitter_conflict
        assert record.finding_kind == "error_signal_loss"
        assert record.contributing_emitters == ("skillA", "skillB", "skillC")
        assert record.conflicting_canonicals == ("skillA", "skillC")
        # Canonical falls back to the no-canonical sentinel and peer
        # set is empty when the conflict is captured.
        assert group.canonical_emitter == ""
        assert group.peer_emitters == ()

    def test_synthetic_two_contributor_no_conflict_when_shared_central_absent(
        self,
    ) -> None:
        """Same fake registry; a group of just (skillB, skillC) without
        skillA has empty ``present_pair_rules`` because neither rule's
        ``other`` is in the contributing set, so no conflict raises."""
        fake_relationships = [
            Table2Relationship(
                emitter_a="skillA",
                emitter_b="skillB",
                table_label="2-TEST-X",
                shared_kinds=("error_signal_loss",),
                shared_namespaces=("test:",),
                relationship="boundary_discipline",
                consolidation="collapse_on_shared_subject_key",
                canonical_emitter="skillA",
                note="",
            ),
            Table2Relationship(
                emitter_a="skillA",
                emitter_b="skillC",
                table_label="2-TEST-Y",
                shared_kinds=("error_signal_loss",),
                shared_namespaces=("test:",),
                relationship="boundary_discipline",
                consolidation="collapse_on_shared_subject_key",
                canonical_emitter="skillC",
                note="",
            ),
        ]
        fake = _make_fake_registry(fake_relationships)
        grouper = SemanticGrouper(registry_module=fake)
        findings = [
            _make_registry_finding(
                finding_kind="error_signal_loss",
                subject_key="test:foo:bar",
                skill="skillB",
                layers=("skillB",),
                finding_id="f2-b",
            ),
            _make_registry_finding(
                finding_kind="error_signal_loss",
                subject_key="test:foo:bar",
                skill="skillC",
                layers=("skillC",),
                finding_id="f2-c",
            ),
        ]
        groups = grouper.group(findings)
        assert len(groups) == 1
        assert groups[0].canonical_emitter == ""
        # Peer set is skillA from both rules' other_emitter.
        assert groups[0].peer_emitters == ("skillA",)


class TestSemanticGroupConstruction:
    """End-to-end ``group()`` round-trip integrity over synthetic
    finding sets. Asserts the locked triple ``(group_id,
    member_finding_ids, mode)`` plus invariants. Resolution-derived
    fields get sanity checks; value-level in TestPairAwareResolution.
    """

    def test_registry_round_trip_triple(self) -> None:
        grouper = SemanticGrouper()
        a = _make_registry_finding(finding_id="f2-a")
        b = _make_registry_finding(
            skill="layer3d-error-handling-specialist",
            finding_id="f2-b",
            layers=("layer3d-error-handling-specialist",),
        )
        groups = grouper.group([a, b])
        assert len(groups) == 1
        g = groups[0]
        assert g.mode == "registry"
        assert set(g.member_finding_ids) == {"f2-a", "f2-b"}
        assert isinstance(g.group_id, str) and len(g.group_id) == 12

    def test_member_count_matches_member_finding_ids_length(self) -> None:
        grouper = SemanticGrouper()
        findings = [
            _make_registry_finding(finding_id=f"f2-{i}")
            for i in range(4)
        ]
        groups = grouper.group(findings)
        for g in groups:
            assert g.member_count == len(g.member_finding_ids)

    def test_member_finding_ids_sorted_at_construction_time(self) -> None:
        """Cycle-16 Item 1: member ids must be lexicographically sorted
        for deterministic group identity and downstream diff stability.
        """
        grouper = SemanticGrouper()
        findings = [
            _make_registry_finding(finding_id="f2-zzz"),
            _make_registry_finding(finding_id="f2-aaa"),
            _make_registry_finding(finding_id="f2-mmm"),
        ]
        groups = grouper.group(findings)
        assert len(groups) == 1
        assert groups[0].member_finding_ids == ("f2-aaa", "f2-mmm", "f2-zzz")

    def test_severity_aggregates_to_max_by_rank(self) -> None:
        grouper = SemanticGrouper()
        a = _make_registry_finding(severity="P3", finding_id="f2-a")
        b = _make_registry_finding(severity="Critical", finding_id="f2-b")
        c = _make_registry_finding(severity="P2", finding_id="f2-c")
        groups = grouper.group([a, b, c])
        assert len(groups) == 1
        assert groups[0].severity == "Critical"

    def test_severity_aggregates_when_all_p3(self) -> None:
        grouper = SemanticGrouper()
        findings = [
            _make_legacy_finding(severity="P3", finding_id=f"f2-{i}")
            for i in range(3)
        ]
        groups = grouper.group(findings)
        assert len(groups) == 1
        assert groups[0].severity == "P3"

    def test_legacy_round_trip_no_resolution_fields(self) -> None:
        grouper = SemanticGrouper()
        finding = _make_legacy_finding(finding_id="f2-legacy")
        groups = grouper.group([finding])
        assert len(groups) == 1
        g = groups[0]
        assert g.mode == "legacy"
        assert g.canonical_emitter == ""
        assert g.peer_emitters == ()
        assert g.subject_key is None
        assert g.finding_kind is None

    def test_resolution_sanity_for_registry_group(self) -> None:
        """Resolution invoked; returns tuples (not None). Value-level
        assertions in TestPairAwareResolution."""
        grouper = SemanticGrouper()
        finding = _make_registry_finding(
            layers=("layer2d-data-integrity-specialist",),
        )
        groups = grouper.group([finding])
        assert isinstance(groups[0].peer_emitters, tuple)
        assert isinstance(groups[0].contributing_emitters, tuple)


class TestPersistence:
    """``read_groups`` round-trip and ``FileAuditLog`` JSONL shape +
    cross-instance append semantics.
    """

    def test_write_then_read_round_trip(self, tmp_path: Path) -> None:
        grouper = SemanticGrouper()
        findings = [
            _make_registry_finding(finding_id="f2-r1"),
            _make_legacy_finding(finding_id="f2-l1"),
        ]
        groups = grouper.group(findings)
        write_semantic_groups(tmp_path, groups)
        assert (tmp_path / GROUPS_FILENAME).exists()

        read_back = read_groups(tmp_path)
        assert len(read_back) == len(groups)
        for original, restored in zip(groups, read_back):
            assert restored.group_id == original.group_id
            assert restored.grouping_key == original.grouping_key
            assert restored.mode == original.mode
            assert restored.severity == original.severity
            assert restored.member_count == original.member_count
            assert restored.member_finding_ids == original.member_finding_ids
            assert restored.canonical_emitter == original.canonical_emitter
            assert restored.peer_emitters == original.peer_emitters

    def test_read_groups_missing_file_returns_empty_list(
        self, tmp_path: Path
    ) -> None:
        assert read_groups(tmp_path) == []

    def test_file_audit_log_jsonl_round_trip_cross_instance_append(
        self, tmp_path: Path
    ) -> None:
        """Two separate FileAuditLog instances pointed at the same
        path must append (not truncate). The future audit-script
        consumer depends on this cross-instance append semantic."""
        entry1 = HalfRegistryAuditEntry(
            timestamp="2026-05-29T00:00:00Z",
            finding_id="f2-h1",
            subject_key=None,
            finding_kind="error_signal_loss",
            file="src/a.py",
            function="fn_a",
            grouping_key="half|src/a.py|fn_a|error_signal_loss",
        )
        entry2 = HalfRegistryAuditEntry(
            timestamp="2026-05-29T00:00:01Z",
            finding_id="f2-h2",
            subject_key="catch_site:src/b.py:fn_b",
            finding_kind=None,
            file="src/b.py",
            function="fn_b",
            grouping_key="half|src/b.py|fn_b|catch_site:src/b.py:fn_b",
        )

        audit_a = FileAuditLog(tmp_path)
        audit_a.record_half_registry(entry1)
        # Second instance, same path.
        audit_b = FileAuditLog(tmp_path)
        audit_b.record_half_registry(entry2)

        audit_path = tmp_path / AUDIT_FILENAME
        lines = audit_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2

        rec1 = json.loads(lines[0])
        rec2 = json.loads(lines[1])
        assert rec1["record_type"] == "half_registry"
        assert rec1["finding_id"] == "f2-h1"
        assert rec1["finding_kind"] == "error_signal_loss"
        assert rec1["subject_key"] is None
        assert rec2["record_type"] == "half_registry"
        assert rec2["finding_id"] == "f2-h2"
        assert rec2["finding_kind"] is None
        assert rec2["subject_key"] == "catch_site:src/b.py:fn_b"

    def test_file_audit_log_records_malformed_subject_key(
        self, tmp_path: Path
    ) -> None:
        entry = MalformedSubjectKeyAuditEntry(
            timestamp="2026-05-29T00:00:00Z",
            finding_id="f2-m",
            subject_key="no_colon_here",
            policy="route_to_legacy",
            grouping_key=None,
        )
        audit = FileAuditLog(tmp_path)
        audit.record_malformed_subject_key(entry)
        audit_path = tmp_path / AUDIT_FILENAME
        lines = audit_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["record_type"] == "malformed_subject_key"
        assert rec["subject_key"] == "no_colon_here"
        assert rec["policy"] == "route_to_legacy"


class TestErrorPaths:
    """Policy variants for malformed subject keys and unknown finding
    kinds."""

    def test_malformed_subject_key_under_fail_fast_raises(self) -> None:
        grouper = SemanticGrouper(
            malformed_subject_key_policy="fail_fast",
        )
        # No colon: malformed under fail_fast.
        finding = _make_registry_finding(
            subject_key="no_colon_here",
            finding_kind="missing_transaction_boundary",
        )
        with pytest.raises(MalformedSubjectKeyError) as exc_info:
            grouper.group([finding])
        assert exc_info.value.subject_key == "no_colon_here"

    def test_malformed_subject_key_under_route_to_legacy_audits(self) -> None:
        audit = _RecordingAuditLog()
        grouper = SemanticGrouper(
            audit_log=audit,
            malformed_subject_key_policy="route_to_legacy",
        )
        finding = _make_registry_finding(
            subject_key="no_colon_here",
            finding_kind="missing_transaction_boundary",
        )
        groups = grouper.group([finding])
        assert len(groups) == 1
        assert groups[0].mode == "legacy"
        assert len(audit.malformed_subject_key_entries) == 1
        assert (
            audit.malformed_subject_key_entries[0].subject_key
            == "no_colon_here"
        )

    def test_unknown_finding_kind_under_fail_fast_raises(self) -> None:
        grouper = SemanticGrouper(
            unknown_finding_kind_policy="fail_fast",
        )
        finding = _make_registry_finding(
            finding_kind="_not_a_real_kind_for_tests",
        )
        with pytest.raises(UnknownFindingKindError) as exc_info:
            grouper.group([finding])
        assert exc_info.value.finding_kind == "_not_a_real_kind_for_tests"

    def test_unknown_finding_kind_under_form_empty_group_routes_to_alt(
        self,
    ) -> None:
        """Under ``form_empty_group``, the finding_kind is stripped and
        the finding falls through to half_registry (subject_key still
        present) or legacy (neither). Here subject_key remains, so
        half_registry."""
        audit = _RecordingAuditLog()
        grouper = SemanticGrouper(
            audit_log=audit,
            unknown_finding_kind_policy="form_empty_group",
        )
        finding = _make_registry_finding(
            finding_kind="_not_a_real_kind_for_tests",
            subject_key="catch_site:src/x.py:Foo:bar",
        )
        groups = grouper.group([finding])
        assert len(groups) == 1
        assert groups[0].mode == "half_registry"
        assert groups[0].finding_kind is None
        assert len(audit.half_registry_entries) == 1

    def test_half_registry_emission_recorded_on_audit_log(self) -> None:
        audit = _RecordingAuditLog()
        grouper = SemanticGrouper(audit_log=audit)
        finding = _make_half_registry_finding(
            finding_kind="error_signal_loss",
            file="src/api.py",
            function="fetch",
        )
        grouper.group([finding])
        assert len(audit.half_registry_entries) == 1
        recorded = audit.half_registry_entries[0]
        assert recorded.finding_kind == "error_signal_loss"
        assert recorded.subject_key is None
        assert recorded.file == "src/api.py"


class TestDeterminism:
    """Stability of group ordering, index shape, and serialization
    bytes across repeated runs / permuted inputs."""

    def test_group_sort_order_stable_across_input_permutations(self) -> None:
        grouper = SemanticGrouper()
        base = [
            _make_registry_finding(
                subject_key=f"transaction_boundary:src/db.py:fn_{i}",
                finding_id=f"f2-{i}",
                severity=("P3", "P2", "P1", "Critical")[i % 4],
            )
            for i in range(8)
        ]
        baseline = grouper.group(list(base))
        baseline_ids = [g.group_id for g in baseline]
        rng = random.Random(1234)
        for _ in range(5):
            permuted = list(base)
            rng.shuffle(permuted)
            result = grouper.group(permuted)
            assert [g.group_id for g in result] == baseline_ids

    def test_index_build_stable_across_repeated_construction(self) -> None:
        idx1 = _build_kind_index_from(real_registry)
        idx2 = _build_kind_index_from(real_registry)
        idx3 = _build_kind_index_from(real_registry)
        c1 = _canonicalize_kind_index(idx1)
        c2 = _canonicalize_kind_index(idx2)
        c3 = _canonicalize_kind_index(idx3)
        assert c1 == c2 == c3
