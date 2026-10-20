"""Tests for the Architect re-entry layer (Stage 5b).

Seven test classes mirroring the grouper harness cadence:

* ``TestPromptAssembly`` — ``build_remediation_pattern_context`` output
  shape; persona excerpt extraction (incl. 2000-char cap); peer
  one-liners; silent-peer block; missing-skill-file degradation.
* ``TestAgentMethod`` — ``ArchitectAgent.propose_remediation_pattern``
  key-presence check; ``AgentResponseError`` on missing keys;
  defensive setdefault on ``notes``; single ``run_json`` call per group.
* ``TestRegistryConflictPath`` — ``build_synthetic_conflict_pattern``
  10-key shape; reserved ``pattern_kind == "registry_conflict"``;
  ``confidence`` within categorical contract; Stage 4b conflict catch
  short-circuits Stage 5b.
* ``TestPersistence`` — ``remediation_patterns.json`` round-trip;
  ``remediation_patterns ↔ semantic_groups`` join on ``group_id``
  resolves ``canonical_emitter`` (validates the Commit 3 design choice
  to drop ``canonical_emitter`` from pattern output).
* ``TestSilentPeerLogic`` — ``silent_peers = peer_emitters -
  contributing_emitters``; block rendered/omitted appropriately;
  ``finding_kind`` label / fallback.
* ``TestStageWiring`` — light-weight option (b): direct method
  invocation on stubbed Orchestrator. One pattern per group; order
  preserved; member findings non-empty in prompt (validates Commit 4
  fix); per-group ``AgentResponseError`` is skip-and-continue;
  persistence via ``_store.write(commit=True)``.
* ``TestDeterminism`` — ``pattern_id`` stable; context bit-identical
  across runs.

Imports from ``sentinel.orchestrator.context`` include three
leading-underscore names (``_compute_pattern_id``,
``_extract_canonical_persona_block``, ``_serialise_group``). Tests
need surgical access to these helpers' behaviors; renaming them in
production code requires updating this harness.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from sentinel.agents.architect import ArchitectAgent
from sentinel.agents.base import AgentResponseError
from sentinel.api.client import ModelClient
from sentinel.assess.grouper import CanonicalEmitterConflictError
from sentinel.config import Config
from sentinel.orchestrator.context import (
    build_remediation_pattern_context,
    build_synthetic_conflict_pattern,
    _compute_pattern_id,
    _extract_canonical_persona_block,
    _serialise_group,
)
from sentinel.orchestrator.main import Orchestrator
from sentinel.orchestrator.state_store import StateStore
from sentinel.skills.loader import Skills, SkillLoader


# ── Module-level builders ────────────────────────────────────────────


def _make_semantic_group(
    *,
    group_id: str = "g-001",
    canonical_emitter: str = "layer2c-security-specialist",
    contributing_emitters: tuple[str, ...] = ("layer2c-security-specialist",),
    peer_emitters: tuple[str, ...] = ("layer2c-security-specialist",),
    member_finding_ids: tuple[str, ...] = ("f2-aaa", "f2-bbb"),
    finding_kind: str | None = "race-condition",
    subject_key: str | None = "src/db.py::commit",
    file: str | None = "src/db.py",
    function: str | None = "commit",
    category: str | None = "concurrency",
    mode: str = "registry",
    grouping_key: str = "registry::race-condition::src/db.py::commit",
    severity: str | None = "P1",
    member_count: int | None = None,
) -> dict[str, Any]:
    """Returns dict shape matching ``grouper._serialize_group`` output
    (the JSON shape on disk in ``semantic_groups.json``), NOT the
    ``SemanticGroup`` dataclass. Stage 5b consumers read serialized
    dicts; this harness exercises that contract.
    """
    if member_count is None:
        member_count = len(member_finding_ids)
    return {
        "group_id": group_id,
        "grouping_key": grouping_key,
        "mode": mode,
        "severity": severity,
        "member_count": member_count,
        "member_finding_ids": list(member_finding_ids),
        "contributing_emitters": list(contributing_emitters),
        "canonical_emitter": canonical_emitter,
        "peer_emitters": list(peer_emitters),
        "subject_key": subject_key,
        "finding_kind": finding_kind,
        "file": file,
        "function": function,
        "category": category,
        "provenance": {
            "mode": mode,
            "grouping_key": grouping_key,
            "subject_key": subject_key,
            "finding_kind": finding_kind,
            "file": file,
            "function": function,
            "category": category,
        },
    }


def _make_member_finding(
    *,
    id: str = "f2-aaa",
    file: str = "src/db.py",
    function: str = "commit",
    finding_kind: str = "race-condition",
    subject_key: str = "src/db.py::commit",
    category: str = "concurrency",
    severity: str = "P1",
    skill: str = "layer2c-security-specialist",
    description: str = "Race condition on commit path.",
) -> dict[str, Any]:
    """Finding dict shape matching dedup output. Keyed on ``id``
    (production contract; see Commit 4)."""
    return {
        "id": id,
        "file": file,
        "function": function,
        "finding_kind": finding_kind,
        "subject_key": subject_key,
        "category": category,
        "severity": severity,
        "skill": skill,
        "description": description,
        "layers": [skill],
    }


def _make_fake_skill_loader(
    tmp_path: Path, skills: dict[str, str]
) -> tuple[SkillLoader, Path]:
    """Write each ``skills[name] = body`` to
    ``<tmp_path>/skill_files/<name>.md`` and return
    ``(loader, skill_files_dir)``."""
    skill_files_dir = tmp_path / "skill_files"
    skill_files_dir.mkdir(parents=True, exist_ok=True)
    for name, body in skills.items():
        (skill_files_dir / f"{name}.md").write_text(body, encoding="utf-8")
    return SkillLoader(skill_files_dir), skill_files_dir


def _valid_pattern(group_id: str = "g-001") -> dict[str, Any]:
    """Drop-in valid 10-key RemediationPattern dict."""
    return {
        "pattern_id": "abc123def456",
        "group_id": group_id,
        "pattern_kind": "add-explicit-transaction-boundary",
        "title": "Wrap commit() in explicit transaction",
        "rationale": "The race surface is the implicit transaction boundary.",
        "proposed_changes": ["Wrap commit() in BEGIN/COMMIT."],
        "acceptance_criteria": ["Concurrent commits serialize correctly."],
        "scope_signals": {
            "files": ["src/db.py"],
            "functions": ["commit"],
            "modules": [],
        },
        "confidence": "high",
        "notes": {
            "silent_peer_notes": "",
            "canonical_emitter_caveats": "",
        },
    }


class _StubArchitect:
    """Stub for ``ArchitectAgent``. ``responses`` maps ``group_id`` to
    the pattern dict to return. Parses ``group_id`` out of
    ``group_context`` by locating the ``### Group Record`` JSON fence
    (regex finds the fence; ``json.loads`` parses the content)."""

    JSON_FENCE_RE = re.compile(
        r"### Group Record\s*\nEcho [^\n]*\n+```json\s*\n(\{.*?\})\s*\n```",
        re.DOTALL,
    )

    def __init__(self, responses: dict[str, dict[str, Any]]) -> None:
        self.responses = responses
        self.call_count = 0
        self.contexts: list[str] = []
        self.raise_for: dict[str, Exception] = {}

    def propose_remediation_pattern(
        self, group_context: str
    ) -> dict[str, Any]:
        self.call_count += 1
        self.contexts.append(group_context)
        match = self.JSON_FENCE_RE.search(group_context)
        if not match:
            raise AssertionError(
                "stub could not locate Group Record JSON block"
            )
        group_record = json.loads(match.group(1))
        group_id = group_record["group_id"]
        if group_id in self.raise_for:
            raise self.raise_for[group_id]
        if group_id not in self.responses:
            raise KeyError(f"unmapped group_id: {group_id}")
        # Echo the pattern_id from input verbatim, matching skill contract.
        pattern = dict(self.responses[group_id])
        pattern["pattern_id"] = group_record["pattern_id"]
        pattern["group_id"] = group_id
        return pattern


def _stub_orchestrator(
    tmp_path: Path,
    architect: _StubArchitect,
    loader: SkillLoader,
    skill_files_dir: Path,
) -> Orchestrator:
    """Construct an Orchestrator via ``__new__`` and attach only the
    attributes Stage 4b/5b methods actually touch. Avoids the
    environment-coupled work in ``__init__``."""
    orch = Orchestrator.__new__(Orchestrator)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    orch._store = StateStore(state_dir=state_dir, repo_path=tmp_path)
    orch._architect = architect
    orch._loader = loader
    orch._client = MagicMock(spec=ModelClient)
    cfg = MagicMock(spec=Config)
    cfg.state_dir = state_dir
    cfg.skill_files_dir = skill_files_dir
    cfg.verifiability_gate_chunk_delay = 0  # no real sleep in the Stage-5b throttle
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
def mock_loader() -> MagicMock:
    loader = MagicMock(spec=SkillLoader)
    loader.load.return_value = "# Mock skill body"
    return loader


# =====================================================================
# TestPromptAssembly
# =====================================================================


class TestPromptAssembly:
    def test_context_contains_required_sections(self, tmp_path: Path) -> None:
        loader, skill_dir = _make_fake_skill_loader(
            tmp_path,
            {
                "layer2c-security-specialist": (
                    "---\nname: layer2c-security-specialist\n"
                    "description: Security specialist.\n---\n"
                    "## Persona\nYou hunt vulnerabilities.\n"
                ),
            },
        )
        group = _make_semantic_group()
        members = [_make_member_finding(id="f2-aaa"), _make_member_finding(id="f2-bbb")]
        ctx = build_remediation_pattern_context(
            group, members, "pat-001", loader, skill_dir,
        )
        for heading in (
            "## Remediation Pattern Proposal Request",
            "### Group Record",
            "### Member Findings",
            "### Canonical Emitter Persona",
            "### Peer Emitters Present",
            "### Silent Peers",
        ):
            assert heading in ctx, f"missing section: {heading}"
        assert "Follow the Remediation Pattern Proposer skill instructions exactly." in ctx

    def test_context_echoes_pattern_id_and_group_id_in_serialised_block(
        self, tmp_path: Path,
    ) -> None:
        loader, skill_dir = _make_fake_skill_loader(
            tmp_path,
            {"layer2c-security-specialist": "## Persona\nx"},
        )
        group = _make_semantic_group(group_id="g-xyz-789")
        ctx = build_remediation_pattern_context(
            group, [], "pat-xyz-789", loader, skill_dir,
        )
        assert '"pattern_id": "pat-xyz-789"' in ctx
        assert '"group_id": "g-xyz-789"' in ctx

    def test_persona_block_extracted_from_real_layer2c_skill(self) -> None:
        loader = SkillLoader(Path("sentinel/skills/skill_files"))
        block = _extract_canonical_persona_block(
            "layer2c-security-specialist", loader,
        )
        assert block.strip()  # non-empty
        # The Persona section content should not include the
        # heading of the next section.
        assert not block.lstrip().startswith("## ")

    def test_persona_block_capped_at_2000_chars_on_oversized_skill(
        self, tmp_path: Path,
    ) -> None:
        # Build a 3000-char Persona section with newlines every 80 chars.
        lines = ["x" * 80 for _ in range(40)]  # 3240 chars
        body = "## Persona\n" + "\n".join(lines) + "\n\n## Next Section\nfoo\n"
        loader, _ = _make_fake_skill_loader(
            tmp_path, {"oversized": body},
        )
        block = _extract_canonical_persona_block("oversized", loader)
        assert len(block) <= 2100  # cap + truncation marker
        assert "persona truncated at ~2000 chars" in block

    def test_missing_skill_file_falls_back_to_placeholder_without_raising(
        self, tmp_path: Path,
    ) -> None:
        loader, _ = _make_fake_skill_loader(tmp_path, {})
        block = _extract_canonical_persona_block(
            "nonexistent-emitter", loader,
        )
        assert "nonexistent-emitter" in block
        assert "not found" in block

    def test_peer_oneliners_block_reads_descriptions_from_frontmatter(
        self, tmp_path: Path,
    ) -> None:
        loader, skill_dir = _make_fake_skill_loader(
            tmp_path,
            {
                "canonical-x": (
                    "---\nname: canonical-x\ndescription: Canonical guy.\n---\n"
                    "## Persona\nC.\n"
                ),
                "peer-alpha": (
                    "---\nname: peer-alpha\ndescription: Alpha peer doc.\n---\n"
                    "Body.\n"
                ),
                "peer-beta": (
                    "---\nname: peer-beta\ndescription: Beta peer doc.\n---\n"
                    "Body.\n"
                ),
            },
        )
        group = _make_semantic_group(
            canonical_emitter="canonical-x",
            contributing_emitters=("canonical-x", "peer-alpha", "peer-beta"),
            peer_emitters=("canonical-x", "peer-alpha", "peer-beta"),
        )
        ctx = build_remediation_pattern_context(
            group, [], "p-001", loader, skill_dir,
        )
        assert "Alpha peer doc." in ctx
        assert "Beta peer doc." in ctx
        # Canonical itself must not appear in peer block.
        peer_section = ctx.split("### Peer Emitters Present")[1].split("### Silent Peers")[0]
        assert "canonical-x" not in peer_section


# =====================================================================
# TestAgentMethod
# =====================================================================


class TestAgentMethod:
    def test_returns_parsed_dict_when_response_has_all_required_keys(
        self, agent_config, mock_loader,
    ) -> None:
        client = MagicMock(spec=ModelClient)
        client.call_model_json.return_value = _valid_pattern()
        agent = ArchitectAgent(agent_config, client, mock_loader)
        result = agent.propose_remediation_pattern("group context")
        assert result["pattern_id"] == "abc123def456"
        assert result["confidence"] == "high"

    def test_raises_agent_response_error_when_required_key_missing(
        self, agent_config, mock_loader,
    ) -> None:
        client = MagicMock(spec=ModelClient)
        bad = _valid_pattern()
        del bad["confidence"]
        client.call_model_json.return_value = bad
        agent = ArchitectAgent(agent_config, client, mock_loader)
        with pytest.raises(AgentResponseError, match="missing required"):
            agent.propose_remediation_pattern("group context")

    def test_sets_default_empty_strings_on_notes_sub_fields(
        self, agent_config, mock_loader,
    ) -> None:
        client = MagicMock(spec=ModelClient)
        response = _valid_pattern()
        response["notes"] = {}  # Empty — defaults should fill.
        client.call_model_json.return_value = response
        agent = ArchitectAgent(agent_config, client, mock_loader)
        result = agent.propose_remediation_pattern("group context")
        assert result["notes"]["silent_peer_notes"] == ""
        assert result["notes"]["canonical_emitter_caveats"] == ""

    def test_makes_exactly_one_call_to_run_json(
        self, agent_config, mock_loader,
    ) -> None:
        client = MagicMock(spec=ModelClient)
        client.call_model_json.return_value = _valid_pattern()
        agent = ArchitectAgent(agent_config, client, mock_loader)
        agent.propose_remediation_pattern("group context")
        assert client.call_model_json.call_count == 1
        call_kwargs = client.call_model_json.call_args.kwargs
        assert call_kwargs.get("temperature") == 0.1
        # Loader was called with the L5b skill name.
        mock_loader.load.assert_called_with(Skills.REMEDIATION_PATTERN_PROPOSER)


# =====================================================================
# TestRegistryConflictPath
# =====================================================================


def _make_conflict_exc() -> CanonicalEmitterConflictError:
    return CanonicalEmitterConflictError(
        finding_kind="race-condition",
        contributing_emitters=(
            "layer2c-security-specialist",
            "layer3e-concurrency-specialist",
        ),
        conflicting_canonicals=(
            "layer2c-security-specialist",
            "layer3e-concurrency-specialist",
        ),
    )


class TestRegistryConflictPath:
    def test_synthetic_pattern_has_all_10_required_top_level_keys(self) -> None:
        exc = _make_conflict_exc()
        stub_group = {
            "group_id": "conflict::race-condition",
            "finding_kind": "race-condition",
            "canonical_emitter": "",
        }
        syn = build_synthetic_conflict_pattern(stub_group, exc)
        required = {
            "pattern_id", "group_id", "pattern_kind", "title",
            "rationale", "proposed_changes", "acceptance_criteria",
            "scope_signals", "confidence", "notes",
        }
        assert required <= set(syn.keys())

    def test_synthetic_pattern_kind_is_reserved_literal_registry_conflict(
        self,
    ) -> None:
        syn = build_synthetic_conflict_pattern(
            {"group_id": "g", "finding_kind": "k", "canonical_emitter": ""},
            _make_conflict_exc(),
        )
        assert syn["pattern_kind"] == "registry_conflict"

    def test_synthetic_pattern_confidence_is_valid_categorical_value(
        self,
    ) -> None:
        syn = build_synthetic_conflict_pattern(
            {"group_id": "g", "finding_kind": "k", "canonical_emitter": ""},
            _make_conflict_exc(),
        )
        assert syn["confidence"] in {"low", "medium", "high"}

    def test_stage5b_conflict_group_emits_synthetic_pattern_skips_architect(
        self, tmp_path: Path,
    ) -> None:
        """Per-group quarantine: a SemanticGroup with a populated
        ``canonical_emitter_conflict`` payload routes to the synthetic
        ``registry_conflict`` pattern in Stage 5b and never reaches the
        Architect. Grouper no longer raises; the conflict surfaces as
        a field on the persisted group dict.
        """
        loader, skill_dir = _make_fake_skill_loader(tmp_path, {})
        architect = _StubArchitect(responses={})
        orch = _stub_orchestrator(tmp_path, architect, loader, skill_dir)
        # Seed findings.json (Stage 5b reads it to build the id->finding
        # lookup; the conflict branch does not consume members but the
        # read still happens).
        orch._store.write("findings", [], commit=False)
        # Seed semantic_groups.json with a single conflict-flagged
        # group. Stage 5b is the single point of entry for this test.
        conflict_group = _make_semantic_group(
            group_id="g-conflict-001",
            canonical_emitter="",
            peer_emitters=(),
        )
        conflict_group["canonical_emitter_conflict"] = {
            "finding_kind": "race-condition",
            "contributing_emitters": [
                "layer2c-security-specialist",
                "layer3e-concurrency-specialist",
            ],
            "conflicting_canonicals": [
                "layer2c-security-specialist",
                "layer3e-concurrency-specialist",
            ],
        }
        (orch._config.state_dir / "semantic_groups.json").write_text(
            json.dumps([conflict_group]), encoding="utf-8",
        )

        orch._run_remediation_pattern_proposals()

        patterns = orch._store.read("remediation_patterns")
        assert len(patterns) == 1
        assert patterns[0]["pattern_kind"] == "registry_conflict"
        # Architect was never invoked for the conflict group.
        assert architect.call_count == 0

    def test_stage5b_mixed_conflict_and_healthy_groups_isolates_conflict(
        self, tmp_path: Path,
    ) -> None:
        """Fail-isolated direct validator: when a batch contains one
        conflict-flagged group and one clean group, the conflict group
        emits a synthetic ``registry_conflict`` pattern and is skipped
        by the Architect, while the healthy group proceeds through the
        normal Architect propose path. Both patterns land in
        ``remediation_patterns.json`` in group-iteration order.
        """
        loader, skill_dir = _make_fake_skill_loader(
            tmp_path,
            {"layer2c-security-specialist": "## Persona\nSecurity.\n"},
        )
        architect = _StubArchitect(
            responses={"g-healthy-002": _valid_pattern("g-healthy-002")},
        )
        orch = _stub_orchestrator(tmp_path, architect, loader, skill_dir)
        orch._store.write(
            "findings",
            [_make_member_finding(id="f2-aaa"), _make_member_finding(id="f2-bbb")],
            commit=False,
        )

        conflict_group = _make_semantic_group(
            group_id="g-conflict-001",
            canonical_emitter="",
            peer_emitters=(),
            member_finding_ids=(),
        )
        conflict_group["canonical_emitter_conflict"] = {
            "finding_kind": "race-condition",
            "contributing_emitters": [
                "layer2c-security-specialist",
                "layer3e-concurrency-specialist",
            ],
            "conflicting_canonicals": [
                "layer2c-security-specialist",
                "layer3e-concurrency-specialist",
            ],
        }
        healthy_group = _make_semantic_group(group_id="g-healthy-002")
        (orch._config.state_dir / "semantic_groups.json").write_text(
            json.dumps([conflict_group, healthy_group]), encoding="utf-8",
        )

        orch._run_remediation_pattern_proposals()

        patterns = orch._store.read("remediation_patterns")
        assert len(patterns) == 2
        # One pattern is the synthetic registry_conflict, exactly one.
        conflict_patterns = [
            p for p in patterns if p["pattern_kind"] == "registry_conflict"
        ]
        healthy_patterns = [
            p for p in patterns if p["pattern_kind"] != "registry_conflict"
        ]
        assert len(conflict_patterns) == 1
        assert len(healthy_patterns) == 1
        # Architect was invoked exactly once \u2014 for the healthy group only.
        assert architect.call_count == 1


# =====================================================================
# TestPersistence
# =====================================================================


class TestPersistence:
    def test_remediation_patterns_written_as_json_list(
        self, tmp_path: Path,
    ) -> None:
        loader, skill_dir = _make_fake_skill_loader(
            tmp_path,
            {"layer2c-security-specialist": "## Persona\nP.\n"},
        )
        architect = _StubArchitect(responses={"g-001": _valid_pattern("g-001")})
        orch = _stub_orchestrator(tmp_path, architect, loader, skill_dir)
        orch._store.write("findings", [_make_member_finding()], commit=False)
        group = _make_semantic_group(group_id="g-001")
        (orch._config.state_dir / "semantic_groups.json").write_text(
            json.dumps([group]), encoding="utf-8",
        )
        orch._run_remediation_pattern_proposals()
        raw = (orch._config.state_dir / "remediation_patterns.json").read_text()
        patterns = json.loads(raw)
        assert isinstance(patterns, list)
        assert len(patterns) == 1
        assert patterns[0]["group_id"] == "g-001"

    def test_patterns_to_groups_join_on_group_id_resolves_canonical_emitter(
        self, tmp_path: Path,
    ) -> None:
        """Validates the Commit 3 design choice to drop
        canonical_emitter from pattern output: consumers join
        remediation_patterns.json against semantic_groups.json on
        group_id to recover it."""
        loader, skill_dir = _make_fake_skill_loader(
            tmp_path,
            {
                "layer2c-security-specialist": "## Persona\nA.\n",
                "layer3e-concurrency-specialist": "## Persona\nB.\n",
                "layer2d-data-integrity-specialist": "## Persona\nC.\n",
            },
        )
        architect = _StubArchitect(responses={
            "g-001": _valid_pattern("g-001"),
            "g-002": _valid_pattern("g-002"),
            "g-003": _valid_pattern("g-003"),
        })
        orch = _stub_orchestrator(tmp_path, architect, loader, skill_dir)
        orch._store.write("findings", [_make_member_finding()], commit=False)
        groups = [
            _make_semantic_group(
                group_id="g-001",
                canonical_emitter="layer2c-security-specialist",
                contributing_emitters=("layer2c-security-specialist",),
                peer_emitters=("layer2c-security-specialist",),
            ),
            _make_semantic_group(
                group_id="g-002",
                canonical_emitter="layer3e-concurrency-specialist",
                contributing_emitters=("layer3e-concurrency-specialist",),
                peer_emitters=("layer3e-concurrency-specialist",),
            ),
            _make_semantic_group(
                group_id="g-003",
                canonical_emitter="layer2d-data-integrity-specialist",
                contributing_emitters=("layer2d-data-integrity-specialist",),
                peer_emitters=("layer2d-data-integrity-specialist",),
            ),
        ]
        (orch._config.state_dir / "semantic_groups.json").write_text(
            json.dumps(groups), encoding="utf-8",
        )
        orch._run_remediation_pattern_proposals()

        patterns = json.loads(
            (orch._config.state_dir / "remediation_patterns.json").read_text()
        )
        groups_read = json.loads(
            (orch._config.state_dir / "semantic_groups.json").read_text()
        )
        by_group_id = {g["group_id"]: g for g in groups_read}
        expected = {
            "g-001": "layer2c-security-specialist",
            "g-002": "layer3e-concurrency-specialist",
            "g-003": "layer2d-data-integrity-specialist",
        }
        for pattern in patterns:
            joined_group = by_group_id[pattern["group_id"]]
            assert joined_group["canonical_emitter"] == expected[pattern["group_id"]]

    def test_remediation_patterns_persisted_via_state_store_write_with_commit(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        loader, skill_dir = _make_fake_skill_loader(
            tmp_path, {"layer2c-security-specialist": "## Persona\nP.\n"},
        )
        architect = _StubArchitect(responses={"g-001": _valid_pattern("g-001")})
        orch = _stub_orchestrator(tmp_path, architect, loader, skill_dir)
        orch._store.write("findings", [_make_member_finding()], commit=False)
        (orch._config.state_dir / "semantic_groups.json").write_text(
            json.dumps([_make_semantic_group(group_id="g-001")]),
            encoding="utf-8",
        )
        write_calls: list[tuple[str, bool, str | None]] = []
        original_write = orch._store.write

        def _spy(key, data, commit=False, commit_message=None):
            write_calls.append((key, commit, commit_message))
            return original_write(key, data, commit=commit, commit_message=commit_message)

        monkeypatch.setattr(orch._store, "write", _spy)
        orch._run_remediation_pattern_proposals()
        rp_writes = [c for c in write_calls if c[0] == "remediation_patterns"]
        assert len(rp_writes) == 1
        assert rp_writes[0][1] is True  # commit=True
        assert rp_writes[0][2].startswith("sentinel: Stage 5b")

    def test_empty_groups_file_writes_empty_patterns_list(
        self, tmp_path: Path,
    ) -> None:
        loader, skill_dir = _make_fake_skill_loader(tmp_path, {})
        architect = _StubArchitect(responses={})
        orch = _stub_orchestrator(tmp_path, architect, loader, skill_dir)
        orch._store.write("findings", [], commit=False)
        # No semantic_groups.json file at all.
        orch._run_remediation_pattern_proposals()
        patterns = json.loads(
            (orch._config.state_dir / "remediation_patterns.json").read_text()
        )
        assert patterns == []
        assert architect.call_count == 0


# =====================================================================
# TestSilentPeerLogic
# =====================================================================


class TestSilentPeerLogic:
    def test_silent_peers_are_peers_minus_contributing(
        self, tmp_path: Path,
    ) -> None:
        loader, skill_dir = _make_fake_skill_loader(
            tmp_path,
            {
                "A": "---\ndescription: A.\n---\n## Persona\nx",
                "B": "---\ndescription: B.\n---\nbody",
                "C": "---\ndescription: C.\n---\nbody",
            },
        )
        group = _make_semantic_group(
            canonical_emitter="A",
            contributing_emitters=("A", "B"),
            peer_emitters=("A", "B", "C"),
        )
        ctx = build_remediation_pattern_context(
            group, [], "p-1", loader, skill_dir,
        )
        silent_section = ctx.split("### Silent Peers")[1]
        assert "- **C**" in silent_section
        assert "- **A**" not in silent_section
        assert "- **B**" not in silent_section

    def test_silent_peer_block_omitted_when_no_silent_peers(
        self, tmp_path: Path,
    ) -> None:
        loader, skill_dir = _make_fake_skill_loader(
            tmp_path, {"A": "---\ndescription: A.\n---\n## Persona\nx"},
        )
        group = _make_semantic_group(
            canonical_emitter="A",
            contributing_emitters=("A",),
            peer_emitters=("A",),
        )
        ctx = build_remediation_pattern_context(
            group, [], "p-1", loader, skill_dir,
        )
        silent_section = ctx.split("### Silent Peers")[1]
        assert "no silent peers on this finding_kind" in silent_section

    def test_silent_peer_block_includes_finding_kind_label(
        self, tmp_path: Path,
    ) -> None:
        loader, skill_dir = _make_fake_skill_loader(
            tmp_path,
            {
                "A": "---\ndescription: A.\n---\n## Persona\nx",
                "C": "---\ndescription: C.\n---\nbody",
            },
        )
        group = _make_semantic_group(
            canonical_emitter="A",
            contributing_emitters=("A",),
            peer_emitters=("A", "C"),
            finding_kind="sql-injection",
        )
        ctx = build_remediation_pattern_context(
            group, [], "p-1", loader, skill_dir,
        )
        silent_section = ctx.split("### Silent Peers")[1]
        assert "sql-injection" in silent_section

    def test_silent_peer_block_uses_unknown_finding_kind_fallback(
        self, tmp_path: Path,
    ) -> None:
        loader, skill_dir = _make_fake_skill_loader(
            tmp_path,
            {
                "A": "---\ndescription: A.\n---\n## Persona\nx",
                "C": "---\ndescription: C.\n---\nbody",
            },
        )
        group = _make_semantic_group(
            canonical_emitter="A",
            contributing_emitters=("A",),
            peer_emitters=("A", "C"),
            finding_kind=None,
        )
        ctx = build_remediation_pattern_context(
            group, [], "p-1", loader, skill_dir,
        )
        silent_section = ctx.split("### Silent Peers")[1]
        assert "unknown finding_kind" in silent_section


# =====================================================================
# TestStageWiring
# =====================================================================


class TestStageWiring:
    def _seed(
        self, tmp_path: Path, groups: list[dict[str, Any]],
        findings: list[dict[str, Any]],
        responses: dict[str, dict[str, Any]],
    ) -> tuple[Orchestrator, _StubArchitect]:
        loader, skill_dir = _make_fake_skill_loader(
            tmp_path,
            {"layer2c-security-specialist": "## Persona\nP.\n"},
        )
        architect = _StubArchitect(responses=responses)
        orch = _stub_orchestrator(tmp_path, architect, loader, skill_dir)
        orch._store.write("findings", findings, commit=False)
        (orch._config.state_dir / "semantic_groups.json").write_text(
            json.dumps(groups), encoding="utf-8",
        )
        return orch, architect

    def test_one_pattern_emitted_per_input_group(self, tmp_path: Path) -> None:
        groups = [
            _make_semantic_group(group_id="g-001"),
            _make_semantic_group(group_id="g-002"),
            _make_semantic_group(group_id="g-003"),
        ]
        findings = [_make_member_finding()]
        responses = {f"g-00{i}": _valid_pattern(f"g-00{i}") for i in (1, 2, 3)}
        orch, _ = self._seed(tmp_path, groups, findings, responses)
        orch._run_remediation_pattern_proposals()
        patterns = orch._store.read("remediation_patterns")
        assert len(patterns) == 3

    def test_group_order_preserved_in_pattern_list(self, tmp_path: Path) -> None:
        groups = [
            _make_semantic_group(group_id="g-z"),
            _make_semantic_group(group_id="g-a"),
            _make_semantic_group(group_id="g-m"),
        ]
        responses = {"g-z": _valid_pattern("g-z"),
                     "g-a": _valid_pattern("g-a"),
                     "g-m": _valid_pattern("g-m")}
        orch, _ = self._seed(
            tmp_path, groups, [_make_member_finding()], responses,
        )
        orch._run_remediation_pattern_proposals()
        patterns = orch._store.read("remediation_patterns")
        assert [p["group_id"] for p in patterns] == ["g-z", "g-a", "g-m"]

    def test_member_findings_in_rendered_prompt_are_non_empty(
        self, tmp_path: Path,
    ) -> None:
        """Validates Commit 4 fix: member findings keyed on 'id'
        appear in the rendered prompt, not as []."""
        groups = [_make_semantic_group(
            group_id="g-001",
            member_finding_ids=("f2-real-a", "f2-real-b"),
        )]
        findings = [
            _make_member_finding(id="f2-real-a"),
            _make_member_finding(id="f2-real-b"),
        ]
        responses = {"g-001": _valid_pattern("g-001")}
        orch, architect = self._seed(tmp_path, groups, findings, responses)
        orch._run_remediation_pattern_proposals()
        assert len(architect.contexts) == 1
        ctx = architect.contexts[0]
        assert '"id": "f2-real-a"' in ctx
        assert '"id": "f2-real-b"' in ctx

    def test_per_group_agent_failure_is_skip_and_continue(
        self, tmp_path: Path,
    ) -> None:
        groups = [
            _make_semantic_group(group_id="g-001"),
            _make_semantic_group(group_id="g-002"),
            _make_semantic_group(group_id="g-003"),
        ]
        responses = {"g-001": _valid_pattern("g-001"),
                     "g-003": _valid_pattern("g-003")}
        orch, architect = self._seed(
            tmp_path, groups, [_make_member_finding()], responses,
        )
        architect.raise_for["g-002"] = AgentResponseError("synthetic failure")
        orch._run_remediation_pattern_proposals()
        patterns = orch._store.read("remediation_patterns")
        assert [p["group_id"] for p in patterns] == ["g-001", "g-003"]
        audit_log = orch._store.read("audit_log")
        failure_events = [
            e for e in audit_log
            if e.get("action") == "remediation_pattern_failed"
        ]
        assert len(failure_events) == 1
        assert failure_events[0]["details"]["group_id"] == "g-002"

    def test_persistence_uses_store_write_with_commit_true(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        groups = [_make_semantic_group(group_id="g-001")]
        responses = {"g-001": _valid_pattern("g-001")}
        orch, _ = self._seed(
            tmp_path, groups, [_make_member_finding()], responses,
        )
        captured: dict[str, Any] = {}
        original = orch._store.write

        def _spy(key, data, commit=False, commit_message=None):
            if key == "remediation_patterns":
                captured["commit"] = commit
                captured["commit_message"] = commit_message
            return original(key, data, commit=commit, commit_message=commit_message)

        monkeypatch.setattr(orch._store, "write", _spy)
        orch._run_remediation_pattern_proposals()
        assert captured["commit"] is True
        assert "Stage 5b" in captured["commit_message"]


# =====================================================================
# TestDeterminism
# =====================================================================


class TestDeterminism:
    def test_pattern_id_stable_across_repeated_computation(self) -> None:
        g = _make_semantic_group(group_id="abc", canonical_emitter="layer2c")
        assert _compute_pattern_id(g) == _compute_pattern_id(g)
        g_other = _make_semantic_group(group_id="abc", canonical_emitter="layer3e")
        assert _compute_pattern_id(g) != _compute_pattern_id(g_other)

    def test_pattern_id_differs_between_synthetic_and_llm_paths(self) -> None:
        g = _make_semantic_group()
        llm = _compute_pattern_id(g)
        synthetic = _compute_pattern_id(g, suffix="registry_conflict")
        assert llm != synthetic

    def test_context_byte_identical_across_runs(self, tmp_path: Path) -> None:
        loader, skill_dir = _make_fake_skill_loader(
            tmp_path,
            {"layer2c-security-specialist": "## Persona\nP.\n"},
        )
        group = _make_semantic_group()
        members = [_make_member_finding(id="f2-a"), _make_member_finding(id="f2-b")]
        a = build_remediation_pattern_context(
            group, members, "pat-001", loader, skill_dir,
        )
        b = build_remediation_pattern_context(
            group, members, "pat-001", loader, skill_dir,
        )
        assert a == b
