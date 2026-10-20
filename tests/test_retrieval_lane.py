from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from sentinel.config import Config
from sentinel.orchestrator.main import HumanGateRequired, Orchestrator
from sentinel.assess.retrieval import RetrievalPackager


def _cfg(tmp_path: Path, **extra_env: str) -> Config:
    env = {
        "SENTINEL_REPO_PATH": str(tmp_path / "repo"),
        "SENTINEL_LOCAL_ONLY_MODE": "true",
        "SENTINEL_ENABLE_RETRIEVAL_REASONING_LANE": "true",
    }
    env.update(extra_env)
    cfg = Config(env=env, load_dotenv_file=False)
    return cfg


def test_retrieval_manifest_contains_required_provenance_fields(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    packager = RetrievalPackager(cfg)

    module = {"name": "mod", "path": "src/mod.py"}
    source = "def f():\n    return 1\n"
    manifest = packager.build_module_manifest(module, source)

    skill_entry = manifest["skills"][0]
    snippet = skill_entry["snippets"][0]

    assert "module_path" in snippet
    assert "start_line" in snippet
    assert "end_line" in snippet
    assert "snippet_hash" in snippet
    assert "char_count" in snippet


def test_retrieval_packaging_respects_size_guardrails(tmp_path: Path) -> None:
    cfg = _cfg(
        tmp_path,
        SENTINEL_RETRIEVAL_MAX_SNIPPETS_PER_SKILL="2",
        SENTINEL_RETRIEVAL_MAX_CHARS_PER_SNIPPET="120",
    )
    packager = RetrievalPackager(cfg)

    module = {"name": "mod", "path": "src/mod.py"}
    source = "\n".join([f"line {i} with eval(user_input)" for i in range(200)])
    manifest = packager.build_module_manifest(module, source)

    for skill_entry in manifest["skills"]:
        assert len(skill_entry["snippets"]) <= 2
        for snippet in skill_entry["snippets"]:
            assert snippet["char_count"] <= 120


def test_retrieval_relevance_prefers_security_snippets(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    packager = RetrievalPackager(cfg)

    module = {"name": "mod", "path": "src/mod.py"}
    source = "\n".join(
        [
            "def safe_math(x):",
            "    return x + 1",
            "def dangerous(user_input):",
            "    return eval(user_input)",
        ]
    )
    manifest = packager.build_module_manifest(module, source)

    security = next(s for s in manifest["skills"] if s["skill"] == "layer2c-security-specialist")
    combined = "\n".join(snip["content"] for snip in security["snippets"])
    assert "eval(user_input)" in combined


def test_phase1_writes_retrieval_and_reasoning_artifacts_when_enabled(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")

    env = {
        "SENTINEL_REPO_PATH": str(repo),
        "SENTINEL_LOCAL_ONLY_MODE": "true",
        "SENTINEL_ENABLE_RETRIEVAL_REASONING_LANE": "true",
    }
    cfg = Config(env=env, load_dotenv_file=False)

    fake_reasoning = [
        {
            "module": "src",
            "skill": "layer2a-quick-review",
            "severity": "P3",
            "category": "maintainability",
            "location": "src/app.py:1",
            "description": "sample reasoning finding",
            "suggested_fix": "sample fix",
            "status": "open",
        }
    ]
    status = {
        "module": "src",
        "module_path": "src",
        "chunks_total": 1,
        "chunks_capped": 0,
        "quick_review_failed_chunks": 0,
        "specialist_failed_chunks": 0,
        "findings_count": 1,
        "skills_run": ["layer2a-quick-review"],
        "specialist_attempted_chunks": {},
        "specialist_failed_chunks_by_skill": {},
        "review_complete": True,
    }

    with Orchestrator(cfg) as orch:
        with patch(
            "sentinel.assess.reviewer.ReviewOrchestrator.review_module_with_telemetry",
            return_value=(fake_reasoning, status, []),
        ):
            try:
                orch.run_phase1([repo])
                assert False, "Expected Phase 1 triage gate"
            except HumanGateRequired as gate:
                assert gate.gate_type == "tech_debt_triage"

    retrieval_manifest = cfg.state_dir / "retrieval_manifest.json"
    reasoning_findings = cfg.state_dir / "reasoning_findings.json"
    assert retrieval_manifest.exists()
    assert reasoning_findings.exists()

    retrieval_payload = json.loads(retrieval_manifest.read_text(encoding="utf-8"))
    reasoning_payload = json.loads(reasoning_findings.read_text(encoding="utf-8"))
    assert len(retrieval_payload) >= 1
    assert len(reasoning_payload) >= 1


def test_retrieval_lane_preserves_security_evidence_for_reviewer(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "app.py").write_text(
        "def run(user_input):\n    return eval(user_input)\n",
        encoding="utf-8",
    )

    env = {
        "SENTINEL_REPO_PATH": str(repo),
        "SENTINEL_LOCAL_ONLY_MODE": "true",
        "SENTINEL_ENABLE_RETRIEVAL_REASONING_LANE": "true",
    }
    cfg = Config(env=env, load_dotenv_file=False)

    def reviewer_side_effect(module, source, manifest):
        assert "eval(user_input)" in source
        return (
            [
                {
                    "module": "src",
                    "skill": "layer2c-security-specialist",
                    "severity": "P2",
                    "category": "security",
                    "location": "src/app.py:2",
                    "description": "security evidence preserved",
                    "suggested_fix": "avoid eval",
                    "status": "open",
                }
            ],
            {
                "module": "src",
                "module_path": "src",
                "chunks_total": 1,
                "chunks_capped": 0,
                "quick_review_failed_chunks": 0,
                "specialist_failed_chunks": 0,
                "findings_count": 1,
                "skills_run": ["layer2c-security-specialist"],
                "specialist_attempted_chunks": {},
                "specialist_failed_chunks_by_skill": {},
                "review_complete": True,
            },
            [],
        )

    with Orchestrator(cfg) as orch:
        with patch(
            "sentinel.assess.reviewer.ReviewOrchestrator.review_module_with_telemetry",
            side_effect=reviewer_side_effect,
        ):
            try:
                orch.run_phase1([repo])
                assert False, "Expected Phase 1 triage gate"
            except HumanGateRequired as gate:
                assert gate.gate_type == "tech_debt_triage"


def test_phase1_skips_retrieval_artifacts_when_disabled(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")

    env = {
        "SENTINEL_REPO_PATH": str(repo),
        "SENTINEL_LOCAL_ONLY_MODE": "true",
    }
    cfg = Config(env=env, load_dotenv_file=False)

    with Orchestrator(cfg) as orch:
        try:
            orch.run_phase1([repo])
            assert False, "Expected Phase 1 triage gate"
        except HumanGateRequired as gate:
            assert gate.gate_type == "tech_debt_triage"

    assert not (cfg.state_dir / "retrieval_manifest.json").exists()
    assert not (cfg.state_dir / "reasoning_findings.json").exists()
