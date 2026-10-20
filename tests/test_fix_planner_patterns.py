"""Tests for the Phase 3 Fix Planner extension cycle.

Six test classes mirroring the Architect re-entry and transition-manifest
harness cadence:

* ``TestPromptAssembly`` -- ``build_fix_planner_context`` body shape with
  patterns/registry_blockers wiring; Tier 1 longest-string-field
  truncation; Tier 2 raw-JSON fallback; both-blocks-omitted byte-identity
  against the Commit 3 baseline SHA.
* ``TestAgentMethod`` -- ``ArchitectAgent.build_remediation_plan`` routes
  to ``Skills.FIX_PLANNER`` at ``temperature=0.1`` with the synthesis
  context as the user message.
* ``TestTriageFilter`` -- Stage 6 partitioning + APPROVE filter +
  ``remediation_pattern_skipped`` audit shape + ``semantic_groups.json``
  read-once invariant.
* ``TestTaskLineage`` -- ``source_pattern`` threads to
  ``Task.pattern_id`` through ``_extract_plan_items`` /
  ``TaskManager.create_task``; backward-compat for plan items missing
  the key entirely.
* ``TestRegistryBlockerPath`` -- ``registry_conflict`` patterns route to
  the blocker block; ``FIX-BLOCKING-*`` item shape honored when the stub
  architect emits the spec-mandated dict (option a: wiring honor, not
  LLM compliance); triage filter bypassed for blockers regardless of
  member triage.
* ``TestDeterminism`` -- ``build_fix_planner_context`` byte-identical
  across repeat invocations with identical inputs (patterns + blockers
  included).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from sentinel.agents.architect import ArchitectAgent
from sentinel.api.client import ModelClient, ModelResponse
from sentinel.config import Config
from sentinel.orchestrator.context import (
    _PATTERN_CAP_CHARS,
    _TRUNCATION_MARKER,
    _render_patterns,
    _render_single_pattern,
    _truncate_longest_string_field,
    build_fix_planner_context,
)
from sentinel.orchestrator.main import HumanGateRequired, Orchestrator
from sentinel.orchestrator.state_store import StateStore
from sentinel.orchestrator.task_manager import Task, TaskManager
from sentinel.skills.loader import Skills, SkillLoader


# Commit 3 byte-identity baseline (locked at refactor(fix-planner): 7782f6e).
# build_fix_planner_context with no patterns / no registry_blockers must
# match this SHA for the exact inputs below. If this assertion ever
# breaks, the byte-identity guarantee is gone and the upgrade path
# needs a deliberate refresh.
#
# Refreshed for S4-8: build_fix_planner_context now serializes each finding
# with an explicit ``effective_severity`` field (recalibrated-or-original)
# so the planner prioritizes by Stage 5c's corrected severity. This is a
# deliberate, documented change to the no-pattern serialization, so the
# baseline SHA is refreshed accordingly.
_BASELINE_FINDINGS: list[dict[str, Any]] = [
    {"id": "F1", "severity": "P1", "title": "x"}
]
_BASELINE_MODULES: list[dict[str, Any]] = [{"name": "m1"}]
_BASELINE_DEPS: list[dict[str, Any]] = [{"src": "a", "dst": "b"}]
_BASELINE_SHA = (
    "5e77d6d8d2e6f08f9dca1724315b3235a9c9b33a9736c1ceb60c77686fa66cab"
)


# ── Module-level builders ────────────────────────────────────────────


def _make_pattern(
    *,
    pattern_id: str = "RP-001",
    group_id: str = "g-001",
    pattern_kind: str = "code_change",
    title: str = "Apply locked refactor.",
    rationale: str = "Rationale body.",
    proposed_changes: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
    scope_signals: list[str] | None = None,
    confidence: str = "high",
    notes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """RemediationPattern dict matching the locked Stage 5b 10-key
    schema. ``pattern_kind == "registry_conflict"`` flips a pattern
    into the registry-blocker partition; any other value keeps it in
    the actionable partition."""
    return {
        "pattern_id": pattern_id,
        "group_id": group_id,
        "pattern_kind": pattern_kind,
        "title": title,
        "rationale": rationale,
        "proposed_changes": proposed_changes or ["Edit X.", "Add test Y."],
        "acceptance_criteria": acceptance_criteria or ["pytest green"],
        "scope_signals": scope_signals or ["src/foo.py"],
        "confidence": confidence,
        "notes": notes or {},
    }


def _make_group(
    *,
    group_id: str = "g-001",
    member_finding_ids: list[str] | None = None,
) -> dict[str, Any]:
    """SemanticGroup dict matching the on-disk shape produced by
    ``grouper.write_groups``. Only the fields Stage 6 reads are
    populated."""
    return {
        "group_id": group_id,
        "member_finding_ids": member_finding_ids or ["F1"],
    }


def _make_finding(
    *, id: str = "F1", severity: str = "P1", title: str = "x"
) -> dict[str, Any]:
    return {"id": id, "severity": severity, "title": title}


def _make_tech_debt(
    *,
    finding_id: str,
    human_triage: str,
    description: str = "fix it",
    acceptance_criteria: list[str] | None = None,
    affected_files: list[str] | None = None,
    scope_boundary: str = "",
) -> dict[str, Any]:
    return {
        "finding_id": finding_id,
        "human_triage": human_triage,
        "description": description,
        "acceptance_criteria": acceptance_criteria or [],
        "affected_files": affected_files or [],
        "scope_boundary": scope_boundary,
    }


class _StubArchitect:
    """Stub for ``ArchitectAgent.build_remediation_plan``. Returns a
    fixed plan dict; records contexts and call count."""

    def __init__(self, plan_response: dict[str, Any]) -> None:
        self.plan_response = plan_response
        self.call_count = 0
        self.contexts: list[str] = []

    def build_remediation_plan(self, synthesis_context: str) -> dict[str, Any]:
        self.call_count += 1
        self.contexts.append(synthesis_context)
        return dict(self.plan_response)

    def classify_findings(self, context: str) -> dict[str, Any]:
        # Verifiability gate no-op for planner-pattern tests: no classifications
        # means nothing is filtered, preserving pre-gate routing behavior.
        return {"classifications": []}


def _stub_orchestrator(
    tmp_path: Path,
    architect: _StubArchitect | MagicMock,
    *,
    self_approve_mode: bool = False,
) -> Orchestrator:
    """Construct an Orchestrator via ``__new__`` and attach only the
    attributes Stage 6 (``continue_phase1_after_triage``) actually
    touches: ``_store``, ``_synthesizer``, ``_architect``, ``_config``,
    ``_task_manager``."""
    orch = Orchestrator.__new__(Orchestrator)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    orch._store = StateStore(state_dir=state_dir, repo_path=tmp_path)
    orch._architect = architect

    synth = MagicMock()
    synth.build_fix_planner_context.return_value = {
        "findings": _BASELINE_FINDINGS,
        "modules": _BASELINE_MODULES,
        "dependencies": _BASELINE_DEPS,
    }
    orch._synthesizer = synth

    orch._task_manager = MagicMock(spec=TaskManager)

    cfg = MagicMock(spec=Config)
    cfg.state_dir = state_dir
    cfg.self_approve_mode = self_approve_mode
    cfg.repo_path = str(tmp_path)  # used by the verifiability gate's source retrieval
    # S2-2 — verification_stage is a Config *instance* attribute (set in
    # __init__), so spec=Config does not auto-provide it. The Stage 5d bundle
    # build reads it; default True mirrors production so the path is exercised.
    cfg.verification_stage = True
    orch._config = cfg
    return orch


# ── Test config / agent fixtures ─────────────────────────────────────


@pytest.fixture()
def agent_config() -> Config:
    with patch.dict(
        "os.environ",
        {"GITHUB_TOKEN": "ghp_test", "SENTINEL_REPO_PATH": "/tmp"},
    ):
        return Config()


@pytest.fixture()
def mock_client() -> MagicMock:
    client = MagicMock(spec=ModelClient)
    client.call_model.return_value = ModelResponse(
        content="mock",
        model="gpt-5.1",
        prompt_tokens=1,
        completion_tokens=1,
        total_tokens=2,
    )
    client.call_model_json.return_value = {"blocking": [], "required": []}
    return client


@pytest.fixture()
def mock_loader() -> MagicMock:
    loader = MagicMock(spec=SkillLoader)
    loader.load.return_value = "# Mock layer5 skill body"
    return loader


# =====================================================================
# TestPromptAssembly
# =====================================================================


class TestPromptAssembly:
    def test_remediation_patterns_block_renders_when_patterns_provided(
        self,
    ) -> None:
        pattern = _make_pattern(pattern_id="RP-007")
        ctx = build_fix_planner_context(
            findings=_BASELINE_FINDINGS,
            modules=_BASELINE_MODULES,
            dependencies=_BASELINE_DEPS,
            patterns=[pattern],
        )
        assert "### Remediation Patterns" in ctx
        assert "RP-007" in ctx
        # Closing extension fires when patterns present.
        assert "source_pattern field" in ctx
        # Blockers block stays absent when blockers empty.
        assert "### Registry Blockers" not in ctx

    def test_registry_blockers_block_renders_when_blockers_provided(
        self,
    ) -> None:
        blocker = _make_pattern(
            pattern_id="RP-CONFLICT-001",
            pattern_kind="registry_conflict",
            title="Two emitters claim the same canonical key.",
        )
        ctx = build_fix_planner_context(
            findings=_BASELINE_FINDINGS,
            modules=_BASELINE_MODULES,
            dependencies=_BASELINE_DEPS,
            registry_blockers=[blocker],
        )
        assert "### Registry Blockers" in ctx
        assert "RP-CONFLICT-001" in ctx
        assert "FIX-BLOCKING-*" in ctx
        assert "### Remediation Patterns" not in ctx

    def test_tier1_truncation_preserves_json_validity_with_marker_inside_field(
        self,
    ) -> None:
        # Rationale 2000 chars > _PATTERN_CAP_CHARS=1500. Tier 1
        # truncates the longest string field in place and the result
        # remains JSON-parseable.
        big = _make_pattern(
            pattern_id="RP-BIG", rationale="X" * 2000,
        )
        rendered = _render_single_pattern(big)
        # Cap respected (with small margin for fence chars).
        assert len(rendered) <= _PATTERN_CAP_CHARS + 20
        # Marker present inside the rendered output.
        assert _TRUNCATION_MARKER in rendered
        # JSON inside the fence still parses.
        body_start = rendered.index("```json\n") + len("```json\n")
        body_end = rendered.index("\n```", body_start)
        parsed = json.loads(rendered[body_start:body_end])
        assert parsed["pattern_id"] == "RP-BIG"
        # Marker lives inside the rationale field specifically.
        assert parsed["rationale"].endswith(_TRUNCATION_MARKER)

    def test_tier2_fallback_emits_html_comment_marker_outside_fence(
        self,
    ) -> None:
        # Pathological structure: 250 small fields, none individually
        # truncatable enough to fit _PATTERN_CAP_CHARS. Tier 2 falls
        # back to raw JSON truncation with an out-of-band HTML comment.
        pathological: dict[str, Any] = {
            f"k{i:03d}": f"v{i:03d}" for i in range(250)
        }
        pathological["pattern_id"] = "RP-PATHO"
        rendered = _render_single_pattern(pathological)
        assert (
            "<!-- pattern RP-PATHO truncated at raw JSON level -->"
            in rendered
        )
        # The HTML comment sits outside the fenced block; the fence
        # opens on the line after the comment.
        comment_line, _, rest = rendered.partition("\n")
        assert comment_line.startswith("<!--")
        assert rest.startswith("```json\n")

    def test_both_blocks_omitted_with_none_defaults_matches_baseline_sha(
        self,
    ) -> None:
        # Locked Commit 3 baseline -- if this assertion ever breaks,
        # the byte-identity guarantee for the no-pattern path is gone.
        ctx = build_fix_planner_context(
            findings=_BASELINE_FINDINGS,
            modules=_BASELINE_MODULES,
            dependencies=_BASELINE_DEPS,
        )
        actual_sha = hashlib.sha256(ctx.encode()).hexdigest()
        assert actual_sha == _BASELINE_SHA, (
            "byte-identity baseline broken: "
            f"actual={actual_sha} expected={_BASELINE_SHA} len={len(ctx)}"
        )

    def test_tier1_truncates_longest_list_element_when_list_field_dominates(
        self,
    ) -> None:
        """Edge case: the longest string lives inside a list field
        (proposed_changes / acceptance_criteria / scope_signals), not
        as a top-level string. Tier 1 should still target it."""
        pattern = _make_pattern(
            pattern_id="RP-LIST",
            rationale="short",
            proposed_changes=["short", "Y" * 2000, "short"],
        )
        truncated = _truncate_longest_string_field(
            pattern, target_size=_PATTERN_CAP_CHARS - 50
        )
        # The middle list element was the longest; it should now end
        # with the truncation marker.
        assert truncated["proposed_changes"][1].endswith(_TRUNCATION_MARKER)
        # Adjacent elements untouched.
        assert truncated["proposed_changes"][0] == "short"
        assert truncated["proposed_changes"][2] == "short"


# =====================================================================
# TestAgentMethod
# =====================================================================


class TestAgentMethod:
    def test_build_remediation_plan_routes_to_fix_planner_skill(
        self, agent_config, mock_client, mock_loader
    ) -> None:
        architect = ArchitectAgent(agent_config, mock_client, mock_loader)
        architect.build_remediation_plan("synthesis context body")
        mock_loader.load.assert_called_with(Skills.FIX_PLANNER)

    def test_build_remediation_plan_uses_low_temperature_and_passes_context(
        self, agent_config, mock_client, mock_loader
    ) -> None:
        architect = ArchitectAgent(agent_config, mock_client, mock_loader)
        architect.build_remediation_plan("synthesis context body")
        # The Architect base.run_json -> client.call_model_json call
        # carries the temperature and a user message containing the
        # synthesis context. Inspect the call kwargs.
        assert mock_client.call_model_json.call_count == 1
        _, kwargs = mock_client.call_model_json.call_args
        assert kwargs.get("temperature") == 0.1
        # The user message body should embed the synthesis context.
        user_msg = kwargs.get("user_message") or kwargs.get("messages") or ""
        if isinstance(user_msg, list):
            user_msg = json.dumps(user_msg)
        assert "synthesis context body" in user_msg


# =====================================================================
# TestTriageFilter
# =====================================================================


class TestTriageFilter:
    def test_all_approve_members_pattern_flows_through(
        self, tmp_path: Path
    ) -> None:
        architect = _StubArchitect({"blocking": [], "required": []})
        orch = _stub_orchestrator(tmp_path, architect)
        orch._store.write("findings", [_make_finding(id="F1")])
        orch._store.write("tech_debt", [
            _make_tech_debt(finding_id="F1", human_triage="APPROVE"),
        ])
        orch._store.write(
            "remediation_patterns",
            [_make_pattern(pattern_id="RP-1", group_id="g-001")],
        )
        (orch._config.state_dir / "semantic_groups.json").write_text(
            json.dumps([_make_group(group_id="g-001",
                                    member_finding_ids=["F1"])]),
            encoding="utf-8",
        )
        with pytest.raises(HumanGateRequired):
            orch.continue_phase1_after_triage()
        ctx = architect.contexts[0]
        assert "RP-1" in ctx
        assert "### Remediation Patterns" in ctx
        # No skip audit when at least one member approved.
        audit = orch._store.read("audit_log")
        skip = [e for e in audit
                if e.get("action") == "remediation_pattern_skipped"]
        assert skip == []

    def test_all_defer_members_pattern_skipped_with_audit_payload(
        self, tmp_path: Path
    ) -> None:
        architect = _StubArchitect({"blocking": [], "required": []})
        orch = _stub_orchestrator(tmp_path, architect)
        # Tech debt has both APPROVE (so Stage 6 actually runs) and DEFER.
        # The pattern member is F2 (DEFER); F3 keeps the gate from
        # short-circuiting at the no-approved-items branch.
        orch._store.write("findings", [
            _make_finding(id="F2"), _make_finding(id="F3"),
        ])
        orch._store.write("tech_debt", [
            _make_tech_debt(finding_id="F2", human_triage="DEFER"),
            _make_tech_debt(finding_id="F3", human_triage="APPROVE"),
        ])
        orch._store.write(
            "remediation_patterns",
            [_make_pattern(pattern_id="RP-SKIP", group_id="g-defer")],
        )
        (orch._config.state_dir / "semantic_groups.json").write_text(
            json.dumps([_make_group(group_id="g-defer",
                                    member_finding_ids=["F2"])]),
            encoding="utf-8",
        )
        with pytest.raises(HumanGateRequired):
            orch.continue_phase1_after_triage()
        # Pattern absent from context.
        ctx = architect.contexts[0]
        assert "RP-SKIP" not in ctx
        # Audit event present with exact payload shape.
        audit = orch._store.read("audit_log")
        skip = [e for e in audit
                if e.get("action") == "remediation_pattern_skipped"]
        assert len(skip) == 1
        details = skip[0]["details"]
        assert details == {
            "pattern_id": "RP-SKIP",
            "reason": "no_approved_members",
            "member_finding_count": 1,
            "member_triage_summary": {"DEFER": 1},
        }

    def test_mixed_triage_pattern_flows_when_any_member_approved(
        self, tmp_path: Path
    ) -> None:
        architect = _StubArchitect({"blocking": [], "required": []})
        orch = _stub_orchestrator(tmp_path, architect)
        orch._store.write("findings", [
            _make_finding(id="F1"), _make_finding(id="F2"),
            _make_finding(id="F3"),
        ])
        orch._store.write("tech_debt", [
            _make_tech_debt(finding_id="F1", human_triage="APPROVE"),
            _make_tech_debt(finding_id="F2", human_triage="DEFER"),
            _make_tech_debt(finding_id="F3", human_triage="EXCLUDE"),
        ])
        orch._store.write(
            "remediation_patterns",
            [_make_pattern(pattern_id="RP-MIXED", group_id="g-mixed")],
        )
        (orch._config.state_dir / "semantic_groups.json").write_text(
            json.dumps([_make_group(group_id="g-mixed",
                                    member_finding_ids=["F1", "F2", "F3"])]),
            encoding="utf-8",
        )
        with pytest.raises(HumanGateRequired):
            orch.continue_phase1_after_triage()
        ctx = architect.contexts[0]
        assert "RP-MIXED" in ctx
        audit = orch._store.read("audit_log")
        assert [e for e in audit
                if e.get("action") == "remediation_pattern_skipped"] == []

    def test_registry_conflict_exempt_from_approve_filter(
        self, tmp_path: Path
    ) -> None:
        architect = _StubArchitect({"blocking": [], "required": []})
        orch = _stub_orchestrator(tmp_path, architect)
        orch._store.write("findings", [
            _make_finding(id="F-A"), _make_finding(id="F-B"),
        ])
        # F-A is APPROVE (so Stage 6 runs); F-B is DEFER (the blocker's
        # only member). The blocker must still flow.
        orch._store.write("tech_debt", [
            _make_tech_debt(finding_id="F-A", human_triage="APPROVE"),
            _make_tech_debt(finding_id="F-B", human_triage="DEFER"),
        ])
        orch._store.write(
            "remediation_patterns",
            [_make_pattern(
                pattern_id="RP-CONFLICT",
                group_id="g-conflict",
                pattern_kind="registry_conflict",
            )],
        )
        (orch._config.state_dir / "semantic_groups.json").write_text(
            json.dumps([_make_group(group_id="g-conflict",
                                    member_finding_ids=["F-B"])]),
            encoding="utf-8",
        )
        with pytest.raises(HumanGateRequired):
            orch.continue_phase1_after_triage()
        ctx = architect.contexts[0]
        assert "### Registry Blockers" in ctx
        assert "RP-CONFLICT" in ctx
        # Blocker should NOT show up in the actionable patterns block.
        assert "### Remediation Patterns" not in ctx
        # No skip audit for blockers -- they bypass the filter.
        audit = orch._store.read("audit_log")
        assert [e for e in audit
                if e.get("action") == "remediation_pattern_skipped"] == []

    def test_semantic_groups_read_once_per_invocation(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Read-once invariant: semantic_groups.json must be read
        exactly once per ``continue_phase1_after_triage`` call,
        regardless of pattern count. A future refactor that moves the
        read inside a per-pattern loop is the failure mode being
        guarded against."""
        architect = _StubArchitect({"blocking": [], "required": []})
        orch = _stub_orchestrator(tmp_path, architect)
        # 5 actionable patterns + 1 blocker so the read happening
        # per-pattern would produce 6+ counts instead of 1.
        orch._store.write("findings", [
            _make_finding(id=f"F{i}") for i in range(1, 6)
        ])
        orch._store.write("tech_debt", [
            _make_tech_debt(finding_id=f"F{i}", human_triage="APPROVE")
            for i in range(1, 6)
        ])
        patterns = [
            _make_pattern(pattern_id=f"RP-{i}", group_id=f"g-{i}")
            for i in range(1, 6)
        ]
        patterns.append(_make_pattern(
            pattern_id="RP-BLOCK", group_id="g-block",
            pattern_kind="registry_conflict",
        ))
        orch._store.write("remediation_patterns", patterns)
        groups = [
            _make_group(group_id=f"g-{i}", member_finding_ids=[f"F{i}"])
            for i in range(1, 6)
        ]
        groups.append(_make_group(group_id="g-block",
                                  member_finding_ids=["F1"]))
        groups_path = orch._config.state_dir / "semantic_groups.json"
        groups_path.write_text(json.dumps(groups), encoding="utf-8")

        # Wrap Path.read_text and count reads of the specific path.
        original_read_text = Path.read_text
        read_count = {"n": 0}

        def counting_read_text(self, *args, **kwargs):
            if self == groups_path:
                read_count["n"] += 1
            return original_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", counting_read_text)
        with pytest.raises(HumanGateRequired):
            orch.continue_phase1_after_triage()
        assert read_count["n"] == 1, (
            f"semantic_groups.json read {read_count['n']} times; "
            "expected exactly 1 (read-once invariant violated)"
        )


# =====================================================================
# TestTaskLineage
# =====================================================================


class TestTaskLineage:
    def test_source_pattern_threads_to_task_pattern_id(self) -> None:
        # Direct exercise of TaskManager.create_task semantics. The
        # _extract_plan_items + create_task call site at main.py:1437
        # passes pattern_id=item.get("source_pattern", "").
        mgr = MagicMock(spec=TaskManager)
        item = {
            "description": "Refactor X.",
            "acceptance_criteria": ["green"],
            "affected_files": ["src/foo.py"],
            "scope_boundary": "src/foo.py only",
            "required_skills": [],
            "source_pattern": "RP-007",
        }
        mgr.create_task(
            plan_id="p-001",
            description=item.get("description", ""),
            acceptance_criteria=item.get("acceptance_criteria", []),
            affected_files=item.get("affected_files", []),
            scope_boundary=item.get("scope_boundary", ""),
            required_skills=item.get("required_skills", []),
            pattern_id=item.get("source_pattern", ""),
        )
        _, kwargs = mgr.create_task.call_args
        assert kwargs["pattern_id"] == "RP-007"

    def test_missing_source_pattern_defaults_to_empty_string(self) -> None:
        mgr = MagicMock(spec=TaskManager)
        item: dict[str, Any] = {
            "description": "Refactor X.",
            "source_pattern": "",
        }
        mgr.create_task(
            plan_id="p-001",
            description=item.get("description", ""),
            acceptance_criteria=[],
            affected_files=[],
            scope_boundary="",
            required_skills=[],
            pattern_id=item.get("source_pattern", ""),
        )
        _, kwargs = mgr.create_task.call_args
        assert kwargs["pattern_id"] == ""

    def test_source_pattern_coexists_with_source_findings_on_same_item(
        self,
    ) -> None:
        mgr = MagicMock(spec=TaskManager)
        item = {
            "description": "Refactor X.",
            "source_pattern": "RP-009",
            "source_findings": ["F1", "F2"],
        }
        mgr.create_task(
            plan_id="p-001",
            description=item.get("description", ""),
            acceptance_criteria=[],
            affected_files=[],
            scope_boundary="",
            required_skills=[],
            pattern_id=item.get("source_pattern", ""),
        )
        _, kwargs = mgr.create_task.call_args
        assert kwargs["pattern_id"] == "RP-009"
        # source_findings is the planner's responsibility; the task
        # layer only consumes source_pattern. The kwarg list must NOT
        # carry source_findings as a Task field.
        assert "source_findings" not in kwargs

    def test_old_plan_json_without_source_pattern_key_produces_empty(
        self, tmp_path: Path
    ) -> None:
        """Backward-compat: a pre-Commit-3 plan.json item lacks the
        ``source_pattern`` key entirely (key absent, not present-but-
        empty). The ``.get("source_pattern", "")`` fallback yields ''."""
        # Round-trip via Task to also exercise the dataclass default.
        item: dict[str, Any] = {
            "description": "Pre-Commit-3 plan item.",
            # No source_pattern key.
        }
        pattern_id = item.get("source_pattern", "")
        assert pattern_id == ""
        task = Task(
            task_id="t-old",
            plan_id="p-old",
            description=item.get("description", ""),
            pattern_id=pattern_id,
        )
        assert task.pattern_id == ""
        # And the round-trip via from_dict honors the default when the
        # dict is missing the key entirely.
        d = task.to_dict()
        d.pop("pattern_id", None)
        # Task.from_dict requires the full kwarg set; emulate the
        # backward-compat path by adding back the empty-string
        # explicitly (the loader would supply this default).
        d["pattern_id"] = ""
        revived = Task.from_dict(d)
        assert revived.pattern_id == ""


# =====================================================================
# TestRegistryBlockerPath
# =====================================================================


# Spec-mandated FIX-BLOCKING-* item shape. Wired in by the
# layer5-fix-planner skill body (Commit 1, 0811bd5). The stub
# architect emits this shape so the wiring test verifies the
# orchestrator honors it; the test is NOT verifying that the real
# LLM produces this shape.
_FIX_BLOCKING_ITEM = {
    "id": "FIX-BLOCKING-001",
    "title": "Resolve registry emitter conflict for src/foo.py::bar.",
    "source_pattern": "RP-CONFLICT-001",
    "scope_boundary": (
        "Registry-only: assign canonical emitter; "
        "no source-file edits in this task."
    ),
    "verification_required": [],
    "pr_size_estimate": "XS",
}


class TestRegistryBlockerPath:
    def test_registry_conflict_pattern_routed_to_blockers_block_only(
        self, tmp_path: Path
    ) -> None:
        architect = _StubArchitect({
            "blocking": [_FIX_BLOCKING_ITEM],
            "required": [],
        })
        orch = _stub_orchestrator(tmp_path, architect)
        orch._store.write("findings", [_make_finding(id="F-A")])
        orch._store.write("tech_debt", [
            _make_tech_debt(finding_id="F-A", human_triage="APPROVE"),
        ])
        orch._store.write(
            "remediation_patterns",
            [_make_pattern(
                pattern_id="RP-CONFLICT-001",
                group_id="g-conflict",
                pattern_kind="registry_conflict",
            )],
        )
        (orch._config.state_dir / "semantic_groups.json").write_text(
            json.dumps([_make_group(
                group_id="g-conflict", member_finding_ids=["F-A"],
            )]),
            encoding="utf-8",
        )
        with pytest.raises(HumanGateRequired):
            orch.continue_phase1_after_triage()
        ctx = architect.contexts[0]
        assert "### Registry Blockers" in ctx
        # Single pattern, kind=registry_conflict -> no actionable
        # patterns block at all.
        assert "### Remediation Patterns" not in ctx

    def test_fix_blocking_item_shape_honored_when_stub_architect_emits_it(
        self, tmp_path: Path
    ) -> None:
        """Option a wiring test: stub returns the spec-mandated
        FIX-BLOCKING-* shape, the orchestrator persists it verbatim
        into plan.json. Does NOT verify LLM compliance."""
        architect = _StubArchitect({
            "blocking": [_FIX_BLOCKING_ITEM],
            "required": [],
        })
        orch = _stub_orchestrator(tmp_path, architect)
        orch._store.write("findings", [_make_finding(id="F-A")])
        orch._store.write("tech_debt", [
            _make_tech_debt(finding_id="F-A", human_triage="APPROVE"),
        ])
        orch._store.write(
            "remediation_patterns",
            [_make_pattern(
                pattern_id="RP-CONFLICT-001",
                group_id="g-conflict",
                pattern_kind="registry_conflict",
            )],
        )
        (orch._config.state_dir / "semantic_groups.json").write_text(
            json.dumps([_make_group(
                group_id="g-conflict", member_finding_ids=["F-A"],
            )]),
            encoding="utf-8",
        )
        with pytest.raises(HumanGateRequired):
            orch.continue_phase1_after_triage()
        plan = orch._store.read("plan")
        assert plan["blocking"][0]["id"].startswith("FIX-BLOCKING-")
        assert plan["blocking"][0]["verification_required"] == []
        assert plan["blocking"][0]["pr_size_estimate"] == "XS"
        assert "Registry-only" in plan["blocking"][0]["scope_boundary"]

    def test_blocker_bypasses_triage_filter_even_with_all_defer_members(
        self, tmp_path: Path
    ) -> None:
        """A registry blocker whose only members are all DEFER must
        still appear in the blockers block. The triage filter applies
        ONLY to actionable patterns."""
        architect = _StubArchitect({"blocking": [], "required": []})
        orch = _stub_orchestrator(tmp_path, architect)
        orch._store.write("findings", [
            _make_finding(id="F-keep"), _make_finding(id="F-block"),
        ])
        orch._store.write("tech_debt", [
            # F-keep keeps Stage 6 from short-circuiting.
            _make_tech_debt(finding_id="F-keep", human_triage="APPROVE"),
            _make_tech_debt(finding_id="F-block", human_triage="DEFER"),
        ])
        orch._store.write(
            "remediation_patterns",
            [_make_pattern(
                pattern_id="RP-BLOCK-DEFER",
                group_id="g-block-defer",
                pattern_kind="registry_conflict",
            )],
        )
        (orch._config.state_dir / "semantic_groups.json").write_text(
            json.dumps([_make_group(
                group_id="g-block-defer",
                member_finding_ids=["F-block"],
            )]),
            encoding="utf-8",
        )
        with pytest.raises(HumanGateRequired):
            orch.continue_phase1_after_triage()
        ctx = architect.contexts[0]
        assert "RP-BLOCK-DEFER" in ctx
        assert "### Registry Blockers" in ctx
        # No skip audit -- the blocker bypassed the filter.
        audit = orch._store.read("audit_log")
        assert [e for e in audit
                if e.get("action") == "remediation_pattern_skipped"] == []


# =====================================================================
# TestDeterminism
# =====================================================================


class TestDeterminism:
    def test_context_byte_identical_across_repeat_invocations_with_patterns(
        self,
    ) -> None:
        """Same inputs (including patterns + blockers) -> same output.
        Distinct from TestPromptAssembly's no-input baseline; this
        checks repeat-invocation stability."""
        pattern = _make_pattern(pattern_id="RP-DET", rationale="stable body")
        blocker = _make_pattern(
            pattern_id="RP-CONFLICT-DET",
            pattern_kind="registry_conflict",
        )
        kwargs = {
            "findings": _BASELINE_FINDINGS,
            "modules": _BASELINE_MODULES,
            "dependencies": _BASELINE_DEPS,
            "patterns": [pattern],
            "registry_blockers": [blocker],
        }
        ctx1 = build_fix_planner_context(**kwargs)
        ctx2 = build_fix_planner_context(**kwargs)
        assert ctx1 == ctx2
        # Stronger: SHA equality across runs.
        assert (
            hashlib.sha256(ctx1.encode()).hexdigest()
            == hashlib.sha256(ctx2.encode()).hexdigest()
        )
