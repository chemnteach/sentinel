"""P1 wave-3 fixes for the assess cluster.

Covers:
- finding_schema: per-item blob isolation, None-location coercion,
  structured-location error formatting, repaired subject_key placeholder.
- grouper: malformed-group deserialization guard, encoder-failure isolation,
  enum-safe malformed_subject_key policy serialization.
- dedup: symmetric severity merge, decorated line-range parsing.
- finding_status: malformed-record load guard, non-list bootstrap validation,
  bulk_update snapshot/rollback on save failure.
- deterministic: quick-review skill name, explicit-empty adapters honored.
- grouper_view: stale triage-lock recovery.
- scope_recommender: root-level source files in the include set.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from sentinel.assess.finding_schema import (
    _format_validation_context,
    flatten_blob,
    repair_invalid_finding,
)
from sentinel.assess.grouper import (
    FileAuditLog,
    MalformedSubjectKeyAuditEntry,
    read_groups,
    two_stage_group,
)
from sentinel.assess.dedup import _parse_line_range, deduplicate
from sentinel.assess.deterministic import DeterministicAnalyzer
from sentinel.assess.finding_status import StatusManager, StatusValue
from sentinel.assess import grouper_view
from sentinel.assess.grouper_view import _acquire_triage_lock, _release_triage_lock
from sentinel.assess.scope_recommender import recommend_scope


# ── finding_schema.flatten_blob ──────────────────────────────────────────


def test_flatten_blob_isolates_malformed_item_and_keeps_the_rest():
    """One malformed blob item must not drop the remaining valid findings."""
    blob = json.dumps(
        {
            "p1": [
                # Malformed: unregistered finding_kind -> materialize raises.
                {
                    "description": "bad one",
                    "finding_kind": "definitely_not_a_registered_kind",
                    "location": "a.py:1",
                },
                # Valid: registered optional-subject kind.
                {
                    "description": "good one",
                    "finding_kind": "logic_bug",
                    "location": "b.py:2",
                },
            ]
        }
    )
    out = flatten_blob(blob, "mod", "layer2a-quick-review")
    descriptions = [f["description"] for f in out]
    assert "good one" in descriptions
    assert "bad one" not in descriptions
    assert len(out) == 1


def test_flatten_blob_none_location_coerces_to_empty_string():
    """A ``None`` location/file must not become the literal string ``"None"``."""
    blob = json.dumps(
        {
            "p2": [
                {
                    "description": "no location",
                    "finding_kind": "logic_bug",
                    "location": None,
                    "file": None,
                }
            ]
        }
    )
    out = flatten_blob(blob, "mod", "layer2a-quick-review")
    assert len(out) == 1
    # The fix under test: location coerces None -> "" (never the literal "None").
    assert out[0]["location"] == ""
    assert out[0]["location"] != "None"
    # And nothing derives the literal string "None" as a file path either.
    assert out[0]["file"] != "None"


# ── finding_schema._format_validation_context ────────────────────────────


def test_format_validation_context_handles_structured_location_object():
    """A structured (dict) location renders as file:line, not a dict repr."""
    ctx = _format_validation_context(
        {"location": {"file": "x.py", "line": 42}},
        emitter_name="unit-test-emitter",
    )
    assert "location=x.py:42" in ctx
    assert "{" not in ctx  # no raw dict repr leaked


def test_format_validation_context_handles_string_location():
    ctx = _format_validation_context(
        {"location": "y.py:7"}, emitter_name="unit-test-emitter"
    )
    assert "location=y.py:7" in ctx


# ── finding_schema.repair_invalid_finding ────────────────────────────────


def test_repair_invalid_finding_derives_placeholder_subject_key():
    """A missing required subject_key gets a kind:file placeholder, not
    the flat ``unknown:unknown``."""
    raw = {
        "finding_kind": "contract_violation",  # subject_required = required
        "description": "missing subject",
        "file": "pkg/sub/service.py",
        "location": "pkg/sub/service.py:10",
        "module": "m",
        "skill": "layer2a-quick-review",
        "category": "correctness",
    }
    repaired = repair_invalid_finding(raw, emitter_name="layer2a-quick-review")
    assert repaired["subject_key"] == "contract_violation:service.py"
    assert repaired["subject_key"] != "unknown:unknown"


# ── grouper.read_groups deserialization guard ────────────────────────────


def _valid_group_payload() -> dict:
    return {
        "group_id": "g-ok",
        "grouping_key": "legacy|f|fn|c",
        "mode": "legacy",
        "severity": "P1",
        "member_count": 1,
        "member_finding_ids": ["F2-a"],
        "contributing_emitters": [],
        "canonical_emitter": "",
        "peer_emitters": [],
        "subject_key": None,
        "finding_kind": None,
        "file": "f.py",
        "function": "fn",
        "category": "c",
        "provenance": {
            "mode": "legacy",
            "grouping_key": "legacy|f|fn|c",
            "subject_key": None,
            "finding_kind": None,
            "file": "f.py",
            "function": "fn",
            "category": "c",
        },
        "canonical_emitter_conflict": None,
    }


def test_read_groups_skips_malformed_group_and_keeps_valid(tmp_path: Path):
    """A malformed group entry (missing required key) is skipped, not fatal."""
    payload = [
        {"provenance": {}},  # malformed: missing group_id etc. -> KeyError
        _valid_group_payload(),
    ]
    (tmp_path / "semantic_groups.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    groups = read_groups(tmp_path)
    assert [g.group_id for g in groups] == ["g-ok"]


# ── grouper.two_stage_group encoder-failure isolation ────────────────────


class _BoomEncoder:
    def encode(self, *args, **kwargs):
        raise RuntimeError("encoder exploded")


def test_two_stage_group_isolates_encoder_failure_per_bucket():
    """An encoder blow-up on one file bucket must not abort grouping; the
    bucket's findings pass through as singleton clusters."""
    findings = [
        {"id": "F1", "file": "a.py", "description": "one", "finding_kind": "logic_bug"},
        {"id": "F2", "file": "a.py", "description": "two", "finding_kind": "logic_bug"},
        {"id": "F3", "file": "b.py", "description": "three", "finding_kind": "logic_bug"},
    ]
    clusters, near = two_stage_group(findings, _BoomEncoder())
    all_ids = {
        str(f.get("id"))
        for cl in clusters
        for f in cl["findings"]
    }
    assert all_ids == {"F1", "F2", "F3"}
    # Every cluster is a singleton (no merges happened under failure).
    assert all(len(cl["findings"]) == 1 for cl in clusters)


# ── grouper.FileAuditLog enum-safe policy serialization ──────────────────


class _EnumLikePolicy:
    value = "route_to_legacy"


def test_file_audit_log_serializes_enum_policy(tmp_path: Path):
    """record_malformed_subject_key must JSON-serialize an Enum-like policy."""
    audit = FileAuditLog(tmp_path)
    entry = MalformedSubjectKeyAuditEntry(
        timestamp="2026-01-01T00:00:00Z",
        finding_id="F2-x",
        subject_key="bad",
        policy=_EnumLikePolicy(),  # type: ignore[arg-type]
        grouping_key=None,
    )
    audit.record_malformed_subject_key(entry)
    line = audit.path.read_text(encoding="utf-8").strip()
    record = json.loads(line)  # would raise if policy stayed an object
    assert record["policy"] == "route_to_legacy"


# ── dedup._parse_line_range ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,expected",
    [
        ("42", (42, 42)),
        ("42-50", (42, 50)),
        ("line 42", (42, 42)),
        ("lines 42-50", (42, 50)),
        ("42:some_function", (42, 42)),
        ("Line 7", (7, 7)),
        ("not a line", None),
        ("", None),
    ],
)
def test_parse_line_range_handles_decorated_forms(text, expected):
    assert _parse_line_range(text) == expected


# ── dedup._merge_into symmetric severity ─────────────────────────────────


def test_exact_dedup_keeps_higher_severity_when_primary_is_duplicate():
    """When the later (duplicate) finding is selected primary (longer desc),
    the earlier finding's higher severity must still win."""
    findings = [
        # existing: higher severity, shorter description.
        {
            "finding_kind": "logic_bug",
            "category": "correctness",
            "file": "app.py",
            "function": "f",
            "description": "short",
            "severity": "Critical",
            "skill": "layer2a-quick-review",
        },
        # duplicate: lower severity, longer description -> becomes primary.
        {
            "finding_kind": "logic_bug",
            "category": "correctness",
            "file": "app.py",
            "function": "f",
            "description": "a much longer description that wins primary selection",
            "severity": "P3",
            "skill": "layer2a-quick-review",
        },
    ]
    out = deduplicate(findings)
    assert len(out) == 1
    assert out[0]["severity"] == "Critical"


# ── deterministic.DeterministicAnalyzer ──────────────────────────────────


def test_deterministic_findings_use_quick_review_skill_name():
    analyzer = DeterministicAnalyzer()
    module = {"name": "m.py", "path": "m.py"}
    out = analyzer.review_module(module, "x = 1  # TODO clean up\n")
    assert out, "expected a TODO finding"
    assert all(f["skill"] == "layer2a-quick-review" for f in out)


def test_deterministic_explicit_empty_adapters_runs_nothing():
    analyzer = DeterministicAnalyzer(adapters=[])
    module = {"name": "m.py", "path": "m.py"}
    out = analyzer.review_module(module, "x = 1  # TODO FIXME\n")
    assert out == []


# ── finding_status ───────────────────────────────────────────────────────


def _write_status_state(tmp_path: Path, tech_debt, groups) -> Path:
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    (state / "tech_debt.json").write_text(json.dumps(tech_debt), encoding="utf-8")
    (state / "semantic_groups.json").write_text(json.dumps(groups), encoding="utf-8")
    return state


def _minimal_status_record(gid: str) -> dict:
    return {
        "group_id": gid,
        "canonical_finding_id": "F2-a",
        "member_finding_ids": ["F2-a"],
        "severity": "P1",
        "status": StatusValue.UNTRIAGED,
        "approval_type": None,
        "conditions": "",
        "task_id": None,
        "batch_id": None,
        "status_updated_at": "2026-01-01T00:00:00Z",
        "status_updated_by": "sentinel",
        "notes": "",
    }


def test_status_load_skips_malformed_record(tmp_path: Path):
    state = _write_status_state(tmp_path, [], [])
    # Pre-seed finding_status.json with one malformed + one valid record.
    payload = [{"not": "a valid record"}, _minimal_status_record("g-ok")]
    (state / "finding_status.json").write_text(json.dumps(payload), encoding="utf-8")

    mgr = StatusManager(
        state, state / "semantic_groups.json", state / "tech_debt.json"
    )
    assert mgr.get("g-ok") is not None
    # The malformed record (which had no group_id) is not indexed.
    assert mgr.get_summary()["total"] == 1


def test_bootstrap_from_triage_rejects_non_list(tmp_path: Path):
    state = _write_status_state(tmp_path, [], [])
    (state / "finding_status.json").write_text(
        json.dumps([_minimal_status_record("g-ok")]), encoding="utf-8"
    )
    mgr = StatusManager(
        state, state / "semantic_groups.json", state / "tech_debt.json"
    )
    bad = state / "bad_tech_debt.json"
    bad.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
    with pytest.raises(ValueError):
        mgr.bootstrap_from_triage(bad)


def test_bulk_update_rolls_back_in_memory_on_save_failure(tmp_path: Path, monkeypatch):
    state = _write_status_state(tmp_path, [], [])
    (state / "finding_status.json").write_text(
        json.dumps(
            [_minimal_status_record("g1"), _minimal_status_record("g2")]
        ),
        encoding="utf-8",
    )
    mgr = StatusManager(
        state, state / "semantic_groups.json", state / "tech_debt.json"
    )

    def _boom(self):
        raise OSError("disk full")

    monkeypatch.setattr(StatusManager, "_save", _boom, raising=True)

    with pytest.raises(OSError):
        mgr.bulk_update(["g1", "g2"], StatusValue.APPROVED)

    # In-memory statuses were restored to their pre-update value.
    assert mgr.get("g1").status == StatusValue.UNTRIAGED
    assert mgr.get("g2").status == StatusValue.UNTRIAGED


# ── grouper_view stale-lock recovery ─────────────────────────────────────


def test_acquire_triage_lock_reclaims_stale_lock(tmp_path: Path, monkeypatch):
    lock_path = tmp_path / "tech_debt.json.triage.lock"
    # Simulate a crashed writer's abandoned lock file.
    lock_path.write_text("stale", encoding="utf-8")

    # Make the lock look old, and shorten the acquire timeout so the test is fast.
    old_mtime = time.time() - 999
    os.utime(lock_path, (old_mtime, old_mtime))
    monkeypatch.setattr(
        grouper_view, "_TECH_DEBT_TRIAGE_LOCK_TIMEOUT_SECONDS", 0.05
    )

    fd = _acquire_triage_lock(lock_path)
    try:
        assert lock_path.exists()  # reclaimed and re-created
    finally:
        _release_triage_lock(lock_path, fd)


def test_acquire_triage_lock_times_out_on_fresh_lock(tmp_path: Path, monkeypatch):
    lock_path = tmp_path / "tech_debt.json.triage.lock"
    lock_path.write_text("fresh", encoding="utf-8")  # just created -> not stale
    monkeypatch.setattr(
        grouper_view, "_TECH_DEBT_TRIAGE_LOCK_TIMEOUT_SECONDS", 0.05
    )
    with pytest.raises(TimeoutError):
        _acquire_triage_lock(lock_path)
    # Fresh lock left intact.
    assert lock_path.exists()


# ── scope_recommender root-level source files ────────────────────────────


def test_recommended_includes_root_level_source_files(tmp_path: Path):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    # Root-level source files that must appear in includes.
    (root / "setup.py").write_text("print('setup')\n", encoding="utf-8")
    (root / "main.py").write_text("print('main')\n", encoding="utf-8")
    # A root-level non-source file that must NOT appear.
    (root / "README.md").write_text("# readme\n", encoding="utf-8")

    rec = recommend_scope(str(root))
    includes = set(rec.recommended_includes)
    assert "pkg/" in includes
    assert "setup.py" in includes
    assert "main.py" in includes
    assert "README.md" not in includes
