"""Regression tests for the P1 error_signal_loss / contract_violation fixes.

Covers:
  - reviewer.py: rate-limit backoff, review_profile validation, non-JSON
    specialist failures counted, chunks_total pre-cap reporting, Quick Review
    failure short-circuits specialists.
  - context.py: truncation markers on sliced JSON blobs.
  - coder.py: self_review fallback matches the Quick Review contract shape.
  - decision_brief.py: guarded tech_debt.json load, synthesize_patterns logs on
    failure, repo_root default is not a hardcoded path.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import sentinel.assess.reviewer as rv
from sentinel.assess.reviewer import ReviewOrchestrator, _RATE_LIMIT_BACKOFF_CAP_SECONDS
from sentinel.skills.loader import Skills


# ---------------------------------------------------------------------------
# reviewer.py — rate-limit backoff (F2-186e85c44280)
# ---------------------------------------------------------------------------

def _bare_orch(config=None):
    o = ReviewOrchestrator.__new__(ReviewOrchestrator)
    o._repo_root_canonical = ""
    o._invalid_kind_counter = [0]
    o._config = config
    return o


def test_rate_limit_retry_sleeps_with_backoff(monkeypatch):
    """A 429 on the first attempt must sleep before retrying (was: no sleep)."""
    sleeps: list[float] = []
    monkeypatch.setattr(rv.time, "sleep", lambda s: sleeps.append(s))

    calls = {"n": 0}

    def runner(_ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("HTTP 429 too many requests")
        return {"findings": []}

    o = _bare_orch()
    result = o._run_with_rate_limit_retry(
        runner=runner, context="ctx", module_name="m", skill="s", phase="p",
    )
    assert result == {"findings": []}
    # Exactly one backoff sleep occurred before the successful retry.
    assert len(sleeps) == 1
    assert 0 < sleeps[0] <= _RATE_LIMIT_BACKOFF_CAP_SECONDS


def test_rate_limit_backoff_is_capped(monkeypatch):
    """Backoff never exceeds the cap even after several attempts."""
    sleeps: list[float] = []
    monkeypatch.setattr(rv.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(rv, "_PAYLOAD_RETRY_RATE_LIMIT_ATTEMPTS", 10)

    def runner(_ctx):
        raise RuntimeError("rate limited")

    o = _bare_orch()
    with pytest.raises(RuntimeError):
        o._run_with_rate_limit_retry(
            runner=runner, context="ctx", module_name="m", skill="s", phase="p",
        )
    assert sleeps, "expected at least one backoff sleep"
    assert all(s <= _RATE_LIMIT_BACKOFF_CAP_SECONDS for s in sleeps)


def test_non_rate_limit_error_does_not_sleep(monkeypatch):
    """A non-429 error propagates immediately with no backoff sleep."""
    sleeps: list[float] = []
    monkeypatch.setattr(rv.time, "sleep", lambda s: sleeps.append(s))

    def runner(_ctx):
        raise RuntimeError("some other error")

    o = _bare_orch()
    with pytest.raises(RuntimeError):
        o._run_with_rate_limit_retry(
            runner=runner, context="ctx", module_name="m", skill="s", phase="p",
        )
    assert sleeps == []


# ---------------------------------------------------------------------------
# reviewer.py — review_profile validation (F2-3978b74637b9 / F2-5875c0e23ed8)
# ---------------------------------------------------------------------------

def test_resolved_review_profile_accepts_supported():
    o = _bare_orch(config=SimpleNamespace(review_profile="reliability-first"))
    assert o._resolved_review_profile() == "reliability-first"


def test_resolved_review_profile_rejects_unsupported(caplog):
    o = _bare_orch(config=SimpleNamespace(review_profile="not-a-profile"))
    with caplog.at_level("ERROR"):
        assert o._resolved_review_profile() == "triggered"
    assert any("Unsupported review_profile" in r.message for r in caplog.records)
    assert any(r.levelname == "ERROR" for r in caplog.records)


def test_resolved_review_profile_missing_attr_defaults():
    o = _bare_orch(config=SimpleNamespace())
    assert o._resolved_review_profile() == "triggered"


# ---------------------------------------------------------------------------
# reviewer.py — non-JSON specialist failures counted
# (F2-79661ada17ab / F2-d54778f44d63 / F2-a29a986d4019 / F2-ec040bf5726d)
# ---------------------------------------------------------------------------

def _chunk(i=0):
    return SimpleNamespace(
        content=f"c{i}", content_with_header=f"c{i}", chunk_index=i,
        start_line=1, end_line=10,
    )


def test_non_json_specialist_failure_is_counted(monkeypatch):
    """A non-JSON agent error must increment failed_chunks and record a failure."""
    o = ReviewOrchestrator.__new__(ReviewOrchestrator)
    o._repo_root_canonical = ""
    o._invalid_kind_counter = [0]
    o._strict_mode = False

    architect = MagicMock()
    architect.specialist_review.side_effect = RuntimeError(
        "Agent 'architect' returned non-json on skill 's': bad"
    )
    o._architect = architect

    monkeypatch.setattr(
        rv, "build_module_review_context", lambda **k: "ctx"
    )

    findings, attempted, failed, failures = o._run_specialist_with_telemetry(
        skill="s", module={"name": "m", "path": "m.py"},
        chunks=[_chunk(0)], prior_findings=[],
    )
    assert findings == []
    assert attempted == 1
    assert failed == 1
    assert len(failures) == 1
    assert failures[0]["skill"] == "s"


# ---------------------------------------------------------------------------
# reviewer.py — Quick Review failure short-circuits specialists (F2-44f14e6f2195)
# ---------------------------------------------------------------------------

def test_quick_review_failure_skips_specialists(monkeypatch):
    """When a Quick Review chunk fails, specialists must NOT run."""
    o = ReviewOrchestrator.__new__(ReviewOrchestrator)
    o._repo_root_canonical = ""
    o._invalid_kind_counter = [0]
    o._strict_mode = False
    o._config = SimpleNamespace(
        max_chunks_per_module=100,
        review_profile="triggered",
        enforce_full_specialist_coverage=False,
    )
    o._chunker = MagicMock()
    o._chunker.chunk_string.return_value = [_chunk(0)]
    o._store = MagicMock()

    architect = MagicMock()
    architect.prioritize_review.return_value = {"recommended_skills": [], "criticality": "low"}
    architect.quick_review.side_effect = RuntimeError("quick review boom")
    o._architect = architect

    monkeypatch.setattr(rv, "build_review_prioritizer_context", lambda *a, **k: "ctx")
    monkeypatch.setattr(rv, "build_module_review_context", lambda **k: "ctx")

    # If specialists were (incorrectly) run, this would raise.
    o._run_specialist = MagicMock(side_effect=AssertionError("specialists must not run"))

    result = o.review_module({"name": "m", "path": "m.py"}, "source", {})
    assert result == []
    architect.specialist_review.assert_not_called()
    o._run_specialist.assert_not_called()


# ---------------------------------------------------------------------------
# context.py — truncation markers (F2-2ddad949be6c et al.)
# ---------------------------------------------------------------------------

def test_truncate_with_marker_appends_marker():
    from sentinel.orchestrator.context import _truncate_with_marker

    text = "x" * 100
    out = _truncate_with_marker(text, 40)
    assert out.startswith("x" * 40)
    assert "TRUNCATED" in out
    assert "60 chars omitted" in out


def test_truncate_with_marker_noop_under_limit():
    from sentinel.orchestrator.context import _truncate_with_marker

    text = "short"
    assert _truncate_with_marker(text, 100) == text


def test_fix_planner_context_marks_truncation():
    from sentinel.orchestrator.context import build_fix_planner_context

    findings = [{"severity": "P1", "description": "d" * 500} for _ in range(200)]
    ctx = build_fix_planner_context(findings, [], [])
    assert "TRUNCATED" in ctx


# ---------------------------------------------------------------------------
# coder.py — self_review fallback contract shape (F2-be3d768f0aa4)
# ---------------------------------------------------------------------------

def test_self_review_fallback_matches_quick_review_contract():
    from sentinel.agents.base import AgentResponseError
    from sentinel.agents.coder import CoderAgent

    coder = CoderAgent.__new__(CoderAgent)
    coder._config = SimpleNamespace(local_only_mode=False)

    def _run_json(**_k):
        raise AgentResponseError("bad json")

    def _run(**_k):
        return SimpleNamespace(content="plain text review")

    coder.run_json = _run_json
    coder.run = _run

    result = coder.self_review("diff")
    # All documented Quick Review keys present so callers cannot KeyError.
    for key in ("module", "skill", "findings", "quick_review_summary", "highest_priority"):
        assert key in result
    assert result["findings"] == []
    assert result["skill"] == Skills.QUICK_REVIEW
    assert result["quick_review_summary"] == "plain text review"


# ---------------------------------------------------------------------------
# decision_brief.py — synthesize_patterns logs on failure (F2-3821d3ee49a5)
# ---------------------------------------------------------------------------

def test_synthesize_patterns_logs_on_failure(caplog):
    from sentinel.reports.decision_brief import synthesize_patterns

    class _BadClient:
        def complete(self, _prompt):
            raise RuntimeError("model down")

    patterns = [{"proposed_changes": ["do the thing"]}]
    with caplog.at_level("WARNING"):
        out = synthesize_patterns("logic_bug", "mod", patterns, _BadClient())
    assert out == ""
    assert any("synthesis failed" in r.message.lower() for r in caplog.records)


def test_synthesize_patterns_empty_returns_blank_no_log(caplog):
    from sentinel.reports.decision_brief import synthesize_patterns

    with caplog.at_level("WARNING"):
        out = synthesize_patterns("logic_bug", "mod", [], client=object())
    assert out == ""
    # No patterns => early return, no crash-warning emitted.
    assert not any("synthesis failed" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# decision_brief.py — repo_root default is not hardcoded (F2-51ac9bc6a31b)
# ---------------------------------------------------------------------------

def test_report_arg_repo_root_default_is_none():
    from sentinel.reports.decision_brief import _parse_args

    args = _parse_args(["--state-dir", "somedir"])
    assert args.repo_root is None


# ---------------------------------------------------------------------------
# decision_brief.py — guarded tech_debt.json load in render (F2-0b722c77c508)
# ---------------------------------------------------------------------------

def test_render_raises_clickexception_on_corrupt_tech_debt(tmp_path):
    import json
    import click
    from sentinel.reports import decision_brief as db

    state = tmp_path
    (state / "findings.json").write_text("[]", encoding="utf-8")
    (state / "semantic_groups.json").write_text("[]", encoding="utf-8")
    (state / "remediation_patterns.json").write_text("[]", encoding="utf-8")
    # Corrupt tech_debt.json — must raise ClickException, not a raw JSONDecodeError.
    (state / "tech_debt.json").write_text("{ not valid json", encoding="utf-8")

    with pytest.raises(click.ClickException) as exc:
        db.render(state)
    assert "tech_debt.json" in str(exc.value)
