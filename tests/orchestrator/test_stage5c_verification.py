"""Tests for Stage 5c — automated finding verification.

Covers ``Orchestrator._run_finding_verification`` and
``ArchitectAgent.verify_finding``. The architect's ``verify_finding`` is
mocked in every test to return a controlled ``{"verifications": [...]}``
payload so no real model call is made.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sentinel.config import Config
import sentinel.config as config_module
from sentinel.orchestrator.main import Orchestrator


@pytest.fixture(autouse=True)
def _reset_global_config_singleton():
    config_module._config = None
    yield
    config_module._config = None


def _make_orch(tmp_path: Path, verification_stage: bool = True):
    repo = tmp_path / "repo"
    (repo / "sentinel").mkdir(parents=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    cfg = Config(
        env={
            "SENTINEL_REPO_PATH": str(repo),
            "SENTINEL_LOCAL_ONLY_MODE": "true",
            "SENTINEL_STATE_DIR": str(state_dir),
            "SENTINEL_VERIFICATION_STAGE": "true" if verification_stage else "false",
        },
        load_dotenv_file=False,
    )
    orch = Orchestrator(cfg)
    return orch, state_dir, repo


def _write_source(repo: Path, rel: str, lines: list[str]) -> None:
    path = repo / "sentinel" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _seed(state_dir: Path, findings, tech_debt=None) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "findings.json").write_text(
        json.dumps(findings, indent=2), encoding="utf-8"
    )
    if tech_debt is not None:
        (state_dir / "tech_debt.json").write_text(
            json.dumps(tech_debt, indent=2), encoding="utf-8"
        )


def _read(state_dir: Path, name: str):
    path = state_dir / name
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1. verification_stage=false → returns immediately, ZERO architect calls
# ---------------------------------------------------------------------------

def test_stage_disabled_returns_immediately(tmp_path, monkeypatch):
    orch, state_dir, repo = _make_orch(tmp_path, verification_stage=False)
    _write_source(repo, "foo.py", ["def a():", "    return 1"])
    _seed(state_dir, [{"id": "F1", "file": "foo.py", "title": "x",
                       "description": "y", "severity": "P1"}])

    calls = {"n": 0}

    def _verify(_ctx):
        calls["n"] += 1
        return {"verifications": []}

    monkeypatch.setattr(orch._architect, "verify_finding", _verify)

    with orch:
        orch._run_finding_verification()

    assert calls["n"] == 0
    findings = _read(state_dir, "findings.json")
    assert len(findings) == 1
    assert "verified" not in findings[0]


# ---------------------------------------------------------------------------
# 2. verified finding gets updated line number written to findings.json
# ---------------------------------------------------------------------------

def test_verified_finding_updates_line(tmp_path, monkeypatch):
    orch, state_dir, repo = _make_orch(tmp_path)
    _write_source(repo, "foo.py", ["def a():", "    x = 1", "    return x"])
    _seed(state_dir, [{"id": "F1", "file": "foo.py", "title": "bug",
                       "finding_kind": "correctness",
                       "description": "off by one", "line_range": "2",
                       "severity": "P1"}])

    def _verify(_ctx):
        return {"verifications": [{
            "index": 1, "verdict": "VERIFIED", "confidence": "HIGH",
            "actual_line_start": 3, "actual_line_end": 3,
            "evidence": "return x", "severity_assessment": "CORRECT",
            "recommended_severity": "P1", "notes": "",
        }]}

    monkeypatch.setattr(orch._architect, "verify_finding", _verify)

    with orch:
        orch._run_finding_verification()

    findings = _read(state_dir, "findings.json")
    assert len(findings) == 1
    f = findings[0]
    assert f["verified"] is True
    assert f["verification_verdict"] == "VERIFIED"
    assert f["verified_location"] == {"start": 3, "end": 3}
    assert f["verification_evidence"] == "return x"


# ---------------------------------------------------------------------------
# 3. unverified finding moves to unverified_findings.json, removed from findings
# ---------------------------------------------------------------------------

def test_unverified_finding_moved(tmp_path, monkeypatch):
    orch, state_dir, repo = _make_orch(tmp_path)
    _write_source(repo, "foo.py", ["def a():", "    return 1"])
    _seed(state_dir, [{"id": "F1", "file": "foo.py", "title": "ghost",
                       "description": "not real", "severity": "P2"}])

    def _verify(_ctx):
        return {"verifications": [{
            "index": 1, "verdict": "UNVERIFIED", "confidence": "HIGH",
            "actual_line_start": None, "actual_line_end": None,
            "evidence": "n/a", "severity_assessment": "CORRECT",
            "recommended_severity": "P2", "notes": "not present",
        }]}

    monkeypatch.setattr(orch._architect, "verify_finding", _verify)

    with orch:
        orch._run_finding_verification()

    findings = _read(state_dir, "findings.json")
    assert findings == []
    unverified = _read(state_dir, "unverified_findings.json")
    assert len(unverified) == 1
    assert unverified[0]["id"] == "F1"
    assert unverified[0]["verified"] is False


# ---------------------------------------------------------------------------
# 4. 3 findings in same file = exactly 1 architect verify_finding call
# ---------------------------------------------------------------------------

def test_batching_one_call_per_file(tmp_path, monkeypatch):
    orch, state_dir, repo = _make_orch(tmp_path)
    _write_source(repo, "foo.py", ["a", "b", "c", "d", "e"])
    _seed(state_dir, [
        {"id": "F1", "file": "foo.py", "title": "t1", "description": "d1",
         "severity": "P1"},
        {"id": "F2", "file": "foo.py", "title": "t2", "description": "d2",
         "severity": "P2"},
        {"id": "F3", "file": "foo.py", "title": "t3", "description": "d3",
         "severity": "P3"},
    ])

    calls = {"n": 0}

    def _verify(_ctx):
        calls["n"] += 1
        return {"verifications": [
            {"index": 1, "verdict": "VERIFIED", "confidence": "HIGH",
             "actual_line_start": 1, "actual_line_end": 1, "evidence": "a",
             "severity_assessment": "CORRECT", "recommended_severity": "P1",
             "notes": ""},
            {"index": 2, "verdict": "VERIFIED", "confidence": "HIGH",
             "actual_line_start": 2, "actual_line_end": 2, "evidence": "b",
             "severity_assessment": "CORRECT", "recommended_severity": "P2",
             "notes": ""},
            {"index": 3, "verdict": "VERIFIED", "confidence": "HIGH",
             "actual_line_start": 3, "actual_line_end": 3, "evidence": "c",
             "severity_assessment": "CORRECT", "recommended_severity": "P3",
             "notes": ""},
        ]}

    monkeypatch.setattr(orch._architect, "verify_finding", _verify)

    with orch:
        orch._run_finding_verification()

    assert calls["n"] == 1
    findings = _read(state_dir, "findings.json")
    assert len(findings) == 3


# ---------------------------------------------------------------------------
# 5. RELOCATED finding gets corrected location and is kept (verified True)
# ---------------------------------------------------------------------------

def test_relocated_finding_corrected(tmp_path, monkeypatch):
    orch, state_dir, repo = _make_orch(tmp_path)
    _write_source(repo, "foo.py", ["a", "b", "c", "d"])
    _seed(state_dir, [{"id": "F1", "file": "foo.py", "title": "drift",
                       "description": "moved", "line_range": "2",
                       "severity": "P1"}])

    def _verify(_ctx):
        return {"verifications": [{
            "index": 1, "verdict": "RELOCATED", "confidence": "MEDIUM",
            "actual_line_start": 4, "actual_line_end": 4,
            "evidence": "d", "severity_assessment": "CORRECT",
            "recommended_severity": "P1", "notes": "line drift",
        }]}

    monkeypatch.setattr(orch._architect, "verify_finding", _verify)

    with orch:
        orch._run_finding_verification()

    findings = _read(state_dir, "findings.json")
    assert len(findings) == 1
    f = findings[0]
    assert f["verified"] is True
    assert f["verification_verdict"] == "RELOCATED"
    assert f["verified_location"] == {"start": 4, "end": 4}


# ---------------------------------------------------------------------------
# 6. severity OVERSTATED → recommended_severity stored as recalibrated_severity
# ---------------------------------------------------------------------------

def test_severity_overstated_recalibrated(tmp_path, monkeypatch):
    orch, state_dir, repo = _make_orch(tmp_path)
    _write_source(repo, "foo.py", ["a", "b"])
    _seed(state_dir, [{"id": "F1", "file": "foo.py", "title": "loud",
                       "description": "minor", "line_range": "1",
                       "severity": "Critical"}])

    def _verify(_ctx):
        return {"verifications": [{
            "index": 1, "verdict": "VERIFIED", "confidence": "HIGH",
            "actual_line_start": 1, "actual_line_end": 1,
            "evidence": "a", "severity_assessment": "OVERSTATED",
            "recommended_severity": "P2", "notes": "",
        }]}

    monkeypatch.setattr(orch._architect, "verify_finding", _verify)

    with orch:
        orch._run_finding_verification()

    findings = _read(state_dir, "findings.json")
    assert findings[0]["recalibrated_severity"] == "P2"


def test_severity_correct_no_recalibration(tmp_path, monkeypatch):
    orch, state_dir, repo = _make_orch(tmp_path)
    _write_source(repo, "foo.py", ["a", "b"])
    _seed(state_dir, [{"id": "F1", "file": "foo.py", "title": "ok",
                       "description": "fine", "line_range": "1",
                       "severity": "P1"}])

    def _verify(_ctx):
        return {"verifications": [{
            "index": 1, "verdict": "VERIFIED", "confidence": "HIGH",
            "actual_line_start": 1, "actual_line_end": 1,
            "evidence": "a", "severity_assessment": "CORRECT",
            "recommended_severity": "P1", "notes": "",
        }]}

    monkeypatch.setattr(orch._architect, "verify_finding", _verify)

    with orch:
        orch._run_finding_verification()

    findings = _read(state_dir, "findings.json")
    assert "recalibrated_severity" not in findings[0]


# ---------------------------------------------------------------------------
# 7. tech_debt items updated to exclude unverified findings
# ---------------------------------------------------------------------------

def test_tech_debt_excludes_unverified(tmp_path, monkeypatch):
    orch, state_dir, repo = _make_orch(tmp_path)
    _write_source(repo, "foo.py", ["a", "b"])
    _seed(
        state_dir,
        [
            {"id": "F1", "file": "foo.py", "title": "real",
             "description": "keep", "severity": "P1"},
            {"id": "F2", "file": "foo.py", "title": "fake",
             "description": "drop", "severity": "P2"},
        ],
        tech_debt=[
            {"id": "TD-0001", "finding_id": "F1", "severity": "P1"},
            {"id": "TD-0002", "finding_id": "F2", "severity": "P2"},
        ],
    )

    def _verify(_ctx):
        return {"verifications": [
            {"index": 1, "verdict": "VERIFIED", "confidence": "HIGH",
             "actual_line_start": 1, "actual_line_end": 1, "evidence": "a",
             "severity_assessment": "CORRECT", "recommended_severity": "P1",
             "notes": ""},
            {"index": 2, "verdict": "UNVERIFIED", "confidence": "HIGH",
             "actual_line_start": None, "actual_line_end": None,
             "evidence": "n/a", "severity_assessment": "CORRECT",
             "recommended_severity": "P2", "notes": ""},
        ]}

    monkeypatch.setattr(orch._architect, "verify_finding", _verify)

    with orch:
        orch._run_finding_verification()

    tech_debt = _read(state_dir, "tech_debt.json")
    ids = {item["finding_id"] for item in tech_debt}
    assert ids == {"F1"}


# ---------------------------------------------------------------------------
# 8. language detection from extension
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ext,expected", [
    ("py", "Python"),
    ("cs", "C#"),
    ("cpp", "C++"),
    ("cc", "C++"),
    ("h", "C++"),
    ("hpp", "C++"),
    ("js", "JavaScript"),
    ("ts", "TypeScript"),
    ("tsx", "TypeScript"),
    ("unknown", "generic"),
])
def test_language_detection(tmp_path, ext, expected):
    orch, _state_dir, _repo = _make_orch(tmp_path)
    with orch:
        assert orch._detect_language(f"foo.{ext}") == expected


# ---------------------------------------------------------------------------
# 9. batch context passed to verify_finding includes language + all findings
# ---------------------------------------------------------------------------

def test_batch_context_contents(tmp_path, monkeypatch):
    orch, state_dir, repo = _make_orch(tmp_path)
    _write_source(repo, "foo.py", ["def a():", "    return 1"])
    _seed(state_dir, [
        {"id": "F1", "file": "foo.py", "title": "title-one",
         "finding_kind": "correctness", "description": "desc-one",
         "line_range": "1", "severity": "P1"},
        {"id": "F2", "file": "foo.py", "title": "title-two",
         "finding_kind": "security", "description": "desc-two",
         "line_range": "2", "severity": "P2"},
    ])

    seen = {"ctx": None}

    def _verify(ctx):
        seen["ctx"] = ctx
        return {"verifications": [
            {"index": 1, "verdict": "VERIFIED", "confidence": "HIGH",
             "actual_line_start": 1, "actual_line_end": 1, "evidence": "a",
             "severity_assessment": "CORRECT", "recommended_severity": "P1",
             "notes": ""},
            {"index": 2, "verdict": "VERIFIED", "confidence": "HIGH",
             "actual_line_start": 2, "actual_line_end": 2, "evidence": "b",
             "severity_assessment": "CORRECT", "recommended_severity": "P2",
             "notes": ""},
        ]}

    monkeypatch.setattr(orch._architect, "verify_finding", _verify)

    with orch:
        orch._run_finding_verification()

    ctx = seen["ctx"]
    assert "Python" in ctx
    assert "Finding 1" in ctx
    assert "Finding 2" in ctx
    assert "title-one" in ctx
    assert "title-two" in ctx


# ---------------------------------------------------------------------------
# 10. degraded failure: verify_finding raises → findings kept (verified True)
# ---------------------------------------------------------------------------

def test_verify_failure_keeps_findings(tmp_path, monkeypatch):
    orch, state_dir, repo = _make_orch(tmp_path)
    _write_source(repo, "foo.py", ["a", "b"])
    _seed(state_dir, [{"id": "F1", "file": "foo.py", "title": "t",
                       "description": "d", "severity": "P1"}])

    def _verify(_ctx):
        raise RuntimeError("model exploded")

    monkeypatch.setattr(orch._architect, "verify_finding", _verify)

    with orch:
        orch._run_finding_verification()

    findings = _read(state_dir, "findings.json")
    assert len(findings) == 1
    assert findings[0]["id"] == "F1"
    assert findings[0].get("verified") is True


# ---------------------------------------------------------------------------
# 11. architect.verify_finding validates required keys
# ---------------------------------------------------------------------------

def test_architect_verify_finding_missing_list(tmp_path, monkeypatch):
    from sentinel.agents.base import AgentResponseError

    orch, _state_dir, _repo = _make_orch(tmp_path)
    monkeypatch.setattr(orch._architect, "run_json", lambda **kw: {"foo": 1})
    with orch:
        with pytest.raises(AgentResponseError):
            orch._architect.verify_finding("ctx")


def test_architect_verify_finding_empty_list(tmp_path, monkeypatch):
    from sentinel.agents.base import AgentResponseError

    orch, _state_dir, _repo = _make_orch(tmp_path)
    monkeypatch.setattr(
        orch._architect, "run_json", lambda **kw: {"verifications": []}
    )
    with orch:
        with pytest.raises(AgentResponseError):
            orch._architect.verify_finding("ctx")


def test_architect_verify_finding_missing_element_key(tmp_path, monkeypatch):
    from sentinel.agents.base import AgentResponseError

    orch, _state_dir, _repo = _make_orch(tmp_path)
    monkeypatch.setattr(
        orch._architect, "run_json",
        lambda **kw: {"verifications": [{"verdict": "VERIFIED"}]},
    )
    with orch:
        with pytest.raises(AgentResponseError):
            orch._architect.verify_finding("ctx")


def test_architect_verify_finding_valid(tmp_path, monkeypatch):
    orch, _state_dir, _repo = _make_orch(tmp_path)
    payload = {"verifications": [{
        "index": 1, "verdict": "VERIFIED", "confidence": "HIGH",
        "evidence": "code", "actual_line_start": 1,
        "actual_line_end": 1, "severity_assessment": "CORRECT",
        "recommended_severity": "P1", "notes": "",
    }]}
    monkeypatch.setattr(orch._architect, "run_json", lambda **kw: payload)
    with orch:
        result = orch._architect.verify_finding("ctx")
    assert result == payload


# ---------------------------------------------------------------------------
# 12. REGRESSION: repo passed as CLI arg (config.repo_path != scanned repo).
#     Stage 5c must resolve source via repositories.json, not config.repo_path,
#     otherwise every finding is quarantined "# Source not found". This is the
#     bug the gpt-5.6 smoke test caught: 0 verified / 17 unverified.
# ---------------------------------------------------------------------------

def test_source_resolves_from_repositories_not_config_repo_path(
    tmp_path, monkeypatch
):
    # Two distinct dirs: the scanned target repo, and the (wrong) CWD default
    # that config.repo_path would carry when the repo is a CLI positional arg.
    target = tmp_path / "target_repo"
    target.mkdir(parents=True)
    # Source lives at the repo ROOT (not under a sentinel/ subtree).
    (target / "payments.py").write_text(
        "def transfer(account, amount):\n"
        "    balance = account.get_balance()\n"
        "    return balance - amount\n",
        encoding="utf-8",
    )

    cwd_default = tmp_path / "cwd_default"  # e.g. D:/Git/Sentinel — the wrong root
    (cwd_default / "sentinel").mkdir(parents=True)

    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    cfg = Config(
        env={
            "SENTINEL_REPO_PATH": str(cwd_default),  # NOT the scanned repo
            "SENTINEL_LOCAL_ONLY_MODE": "true",
            "SENTINEL_STATE_DIR": str(state_dir),
            "SENTINEL_VERIFICATION_STAGE": "true",
        },
        load_dotenv_file=False,
    )
    orch = Orchestrator(cfg)

    # The discovered repository — the authoritative source root.
    (state_dir / "repositories.json").write_text(
        json.dumps([{"name": "target_repo", "path": str(target)}]),
        encoding="utf-8",
    )
    _seed(state_dir, [{
        "id": "F1",
        "file": "payments.py",
        "module": "target_repo:root:payments.py",
        "title": "no validation",
        "finding_kind": "correctness",
        "description": "transfer does not validate amount",
        "line_range": "1-3",
        "function": "transfer",
        "severity": "Critical",
    }])

    seen = {"ctx": None}

    def _verify(ctx):
        seen["ctx"] = ctx
        return {"verifications": [{
            "index": 1, "verdict": "VERIFIED", "confidence": "HIGH",
            "actual_line_start": 1, "actual_line_end": 3, "evidence": "transfer",
            "severity_assessment": "CORRECT", "recommended_severity": "Critical",
            "notes": "",
        }]}

    monkeypatch.setattr(orch._architect, "verify_finding", _verify)

    with orch:
        orch._run_finding_verification()

    # The verifier must have SEEN the real source (not "# Source not found").
    assert seen["ctx"] is not None
    assert "# Source not found" not in seen["ctx"]
    assert "def transfer" in seen["ctx"]

    # And the finding survives as verified, not quarantined.
    findings = _read(state_dir, "findings.json")
    assert len(findings) == 1
    assert findings[0]["id"] == "F1"
    assert findings[0]["verified"] is True
    unverified = _read(state_dir, "unverified_findings.json")
    assert unverified == []


# ---------------------------------------------------------------------------
# 13. Large per-file finding sets are chunked into capped sub-batches so a
#     single oversized call cannot blank out a reasoning model and sink an
#     entire file's findings into keep-by-default (the run-003 failure: files
#     with 77-95 findings returned empty/error and were all kept unverified).
# ---------------------------------------------------------------------------

def test_large_file_chunked_by_cap(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "sentinel").mkdir(parents=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    cfg = Config(
        env={
            "SENTINEL_REPO_PATH": str(repo),
            "SENTINEL_LOCAL_ONLY_MODE": "true",
            "SENTINEL_STATE_DIR": str(state_dir),
            "SENTINEL_VERIFICATION_STAGE": "true",
            "SENTINEL_VERIFIABILITY_MAX_FINDINGS_PER_CHUNK": "10",
        },
        load_dotenv_file=False,
    )
    orch = Orchestrator(cfg)
    _write_source(repo, "big.py", [f"line{i}" for i in range(60)])
    # 25 findings in ONE file, cap 10 -> three sub-batches (10 + 10 + 5).
    _seed(state_dir, [
        {"id": f"F{i}", "file": "big.py", "title": f"t{i}",
         "description": f"d{i}", "line_range": str((i % 50) + 1),
         "severity": "P2"}
        for i in range(25)
    ])

    calls = {"sizes": []}

    def _verify(ctx):
        n = ctx.count("Finding ")  # markers written by _build_verification_batch
        calls["sizes"].append(n)
        return {"verifications": [
            {"index": i, "verdict": "VERIFIED", "confidence": "HIGH",
             "actual_line_start": 1, "actual_line_end": 1, "evidence": "x",
             "severity_assessment": "CORRECT", "recommended_severity": "P2",
             "notes": ""}
            for i in range(1, n + 1)
        ]}

    monkeypatch.setattr(orch._architect, "verify_finding", _verify)

    with orch:
        orch._run_finding_verification()

    assert len(calls["sizes"]) == 3          # not one giant call
    assert sorted(calls["sizes"]) == [5, 10, 10]
    findings = _read(state_dir, "findings.json")
    assert len(findings) == 25
    assert all(f["verified"] for f in findings)
    assert all(f["verification_verdict"] == "VERIFIED" for f in findings)


# ---------------------------------------------------------------------------
# 14. A transient verifier failure retries and recovers, so the finding gets a
#     real verdict instead of falling back to keep-by-default VERIFICATION_ERROR.
# ---------------------------------------------------------------------------

def test_verify_retry_recovers_before_keep(tmp_path, monkeypatch):
    orch, state_dir, repo = _make_orch(tmp_path)
    _write_source(repo, "foo.py", ["def a():", "    return 1"])
    _seed(state_dir, [{"id": "F1", "file": "foo.py", "title": "t",
                       "description": "d", "line_range": "1", "severity": "P1"}])

    calls = {"n": 0}

    def _verify(_ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("gateway returned {'detail': 'transient'}")
        return {"verifications": [{
            "index": 1, "verdict": "VERIFIED", "confidence": "HIGH",
            "actual_line_start": 1, "actual_line_end": 1, "evidence": "a",
            "severity_assessment": "CORRECT", "recommended_severity": "P1",
            "notes": "",
        }]}

    monkeypatch.setattr(orch._architect, "verify_finding", _verify)

    with orch:
        orch._run_finding_verification()

    assert calls["n"] == 2  # retried once, then succeeded
    findings = _read(state_dir, "findings.json")
    assert findings[0]["verified"] is True
    # Real verdict, NOT the keep-by-default fallback.
    assert findings[0]["verification_verdict"] == "VERIFIED"


# ---------------------------------------------------------------------------
# 15. ADAPTIVE BISECTION: a dense batch that empties out (returns non-JSON /
#     empty completion — the run-003 mode-2 residual) is split in half and
#     recursively re-verified, so findings still get real verdicts instead of
#     all falling to keep-by-default. Simulates a model that fails on any batch
#     larger than N findings but succeeds at or below it.
# ---------------------------------------------------------------------------

def test_adaptive_bisection_recovers_dense_batch(tmp_path, monkeypatch):
    from sentinel.agents.base import AgentResponseError

    orch, state_dir, repo = _make_orch(tmp_path)
    _write_source(repo, "foo.py", [f"line{i}" for i in range(40)])
    # 8 findings in one file, default cap (25) -> single batch of 8. The model
    # only succeeds when a batch is <= 2 findings, forcing 8 -> 4 -> 2.
    _seed(state_dir, [
        {"id": f"F{i}", "file": "foo.py", "title": f"t{i}",
         "description": f"d{i}", "line_range": str(i + 1), "severity": "P2"}
        for i in range(8)
    ])

    SUCCEED_AT_OR_BELOW = 2

    def _verify(ctx):
        n = ctx.count("Finding ")
        if n > SUCCEED_AT_OR_BELOW:
            # Emulate the empty-completion failure the verifier raises on.
            raise AgentResponseError("Model response is not valid JSON: ")
        return {"verifications": [
            {"index": i, "verdict": "VERIFIED", "confidence": "HIGH",
             "actual_line_start": 1, "actual_line_end": 1, "evidence": "x",
             "severity_assessment": "CORRECT", "recommended_severity": "P2",
             "notes": ""}
            for i in range(1, n + 1)
        ]}

    monkeypatch.setattr(orch._architect, "verify_finding", _verify)

    with orch:
        orch._run_finding_verification()

    findings = _read(state_dir, "findings.json")
    # All 8 recovered via bisection — real verdicts, none kept-by-default.
    assert len(findings) == 8
    assert all(f["verification_verdict"] == "VERIFIED" for f in findings)
    assert not any(
        f["verification_verdict"] == "VERIFICATION_ERROR" for f in findings
    )


# ---------------------------------------------------------------------------
# 16. BISECTION BASE CASE: a single finding that always fails (even alone) is
#     kept-by-default VERIFICATION_ERROR — never dropped, never infinite-loops.
# ---------------------------------------------------------------------------

def test_bisection_single_finding_failure_kept(tmp_path, monkeypatch):
    from sentinel.agents.base import AgentResponseError

    orch, state_dir, repo = _make_orch(tmp_path)
    _write_source(repo, "foo.py", ["def a():", "    return 1"])
    _seed(state_dir, [
        {"id": "F1", "file": "foo.py", "title": "keep-me",
         "description": "always fails", "line_range": "1", "severity": "P1"},
    ])

    calls = {"n": 0}

    def _verify(_ctx):
        calls["n"] += 1
        raise AgentResponseError("Model response is not valid JSON: ")

    monkeypatch.setattr(orch._architect, "verify_finding", _verify)

    with orch:
        orch._run_finding_verification()

    # Exactly the retry budget (no infinite recursion below size 1).
    assert calls["n"] == 3
    findings = _read(state_dir, "findings.json")
    assert len(findings) == 1
    assert findings[0]["id"] == "F1"           # kept, not dropped
    assert findings[0]["verified"] is True
    assert findings[0]["verification_verdict"] == "VERIFICATION_ERROR"
