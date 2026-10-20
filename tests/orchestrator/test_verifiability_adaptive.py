"""Adaptive chunking for the verifiability gate.

The gate classified all approved findings in one LLM call; on large sets the
capped output truncated the JSON and failed the entire gate. `_classify_findings_
adaptive` splits the findings list in half on a JSON parse failure and retries
each half recursively, failing open to VERIFIABLE only when a chunk at the
minimum size still fails.

Only `self._architect.classify_findings` is mocked; `build_verifiability_context`
runs for real (it degrades to a "source not found" note for absent files, so the
fake finding paths here never raise).
"""

from __future__ import annotations

import logging

from unittest.mock import patch

from sentinel.orchestrator.main import Orchestrator

from tests.test_integration_e2e import _build_config, _setup_repo

F1, F2, F3, F4 = "F2-0001", "F2-0002", "F2-0003", "F2-0004"


def _finding(fid):
    return {"id": fid, "file": "sentinel/orchestrator/main.py",
            "finding_kind": "logic_bug", "subject_key": fid, "description": "x"}


def _present(context, verdicts):
    """The finding ids whose section appears in this chunk's context string."""
    return frozenset(fid for fid in verdicts if f"## Finding {fid}" in context)


def _classify(verdicts, fail_when):
    """Mock classify_findings: raises for a chunk whose id-set satisfies
    ``fail_when``; otherwise returns per-finding classifications for that chunk."""
    def _fn(context):
        present = _present(context, verdicts)
        if fail_when(present):
            raise ValueError("truncated JSON output")
        return {
            "classifications": [
                {"finding_id": fid, "verdict": verdicts[fid]} for fid in present
            ]
        }
    return _fn


def _orch(tmp_path):
    repo_root, _ = _setup_repo(tmp_path)
    cfg = _build_config(repo_root, tmp_path / "state")
    return Orchestrator(cfg), str(repo_root)


def test_single_successful_call_returns_verdict_map(tmp_path):
    orch, repo = _orch(tmp_path)
    verdicts = {F1: "VERIFIABLE", F2: "ADVISORY", F3: "FIXTURE-COMPLEX"}
    findings = [_finding(F1), _finding(F2), _finding(F3)]

    with patch.object(orch._architect, "classify_findings",
                      side_effect=_classify(verdicts, lambda s: False)) as m:
        result = orch._classify_findings_adaptive(findings, repo, min_chunk=1)

    assert result == verdicts
    assert m.call_count == 1


def test_value_error_splits_in_half_and_merges(tmp_path):
    orch, repo = _orch(tmp_path)
    verdicts = {F1: "VERIFIABLE", F2: "ADVISORY", F3: "VERIFIABLE", F4: "FIXTURE-COMPLEX"}
    findings = [_finding(F1), _finding(F2), _finding(F3), _finding(F4)]

    full = frozenset(verdicts)
    with patch.object(orch._architect, "classify_findings",
                      side_effect=_classify(verdicts, lambda s: s == full)) as m:
        result = orch._classify_findings_adaptive(findings, repo, min_chunk=1)

    assert result == verdicts
    # full (fail) + left half + right half
    assert m.call_count == 3


def test_min_chunk_fails_open_to_verifiable_and_warns(tmp_path, caplog):
    orch, repo = _orch(tmp_path)
    verdicts = {F1: "ADVISORY", F2: "ADVISORY"}
    findings = [_finding(F1), _finding(F2)]

    with patch.object(orch._architect, "classify_findings",
                      side_effect=_classify(verdicts, lambda s: True)):
        with caplog.at_level(logging.WARNING):
            result = orch._classify_findings_adaptive(findings, repo, min_chunk=2)

    # Cannot split below min_chunk → every finding defaults to VERIFIABLE.
    assert result == {F1: "VERIFIABLE", F2: "VERIFIABLE"}
    assert any("VERIFIABLE" in r.message and "minimum" in r.message.lower()
               for r in caplog.records)


def test_three_way_split_merges_all(tmp_path):
    orch, repo = _orch(tmp_path)
    verdicts = {F1: "VERIFIABLE", F2: "ADVISORY", F3: "FIXTURE-COMPLEX", F4: "VERIFIABLE"}
    findings = [_finding(F1), _finding(F2), _finding(F3), _finding(F4)]

    full = frozenset(verdicts)
    right = frozenset({F3, F4})
    # Full set fails; left half {F1,F2} succeeds; right half {F3,F4} fails and
    # splits again into singletons that succeed.
    with patch.object(orch._architect, "classify_findings",
                      side_effect=_classify(verdicts, lambda s: s in (full, right))) as m:
        result = orch._classify_findings_adaptive(findings, repo, min_chunk=1)

    assert result == verdicts
    # full + left + right(fail) + F3 + F4
    assert m.call_count == 5


def test_empty_findings_returns_empty_dict(tmp_path):
    orch, repo = _orch(tmp_path)
    with patch.object(orch._architect, "classify_findings") as m:
        result = orch._classify_findings_adaptive([], repo)
    assert result == {}
    m.assert_not_called()


def test_sleeps_between_chunk_calls_on_split(tmp_path):
    """A split throttles between recursive chunk calls with the configured delay."""
    orch, repo = _orch(tmp_path)
    orch._config.verifiability_gate_chunk_delay = 0.5
    verdicts = {F1: "VERIFIABLE", F2: "ADVISORY", F3: "VERIFIABLE", F4: "FIXTURE-COMPLEX"}
    findings = [_finding(F1), _finding(F2), _finding(F3), _finding(F4)]
    full = frozenset(verdicts)

    with patch.object(orch._architect, "classify_findings",
                      side_effect=_classify(verdicts, lambda s: s == full)):
        with patch("sentinel.orchestrator.main.time.sleep") as sleep_mock:
            result = orch._classify_findings_adaptive(findings, repo, min_chunk=1)

    assert result == verdicts
    # The top-level split sleeps once between the left and right halves.
    sleep_mock.assert_called()
    assert sleep_mock.call_args_list[0].args[0] == 0.5


def test_no_sleep_when_no_split(tmp_path):
    """A single successful classify call must not sleep."""
    orch, repo = _orch(tmp_path)
    verdicts = {F1: "VERIFIABLE", F2: "ADVISORY"}
    findings = [_finding(F1), _finding(F2)]
    with patch.object(orch._architect, "classify_findings",
                      side_effect=_classify(verdicts, lambda s: False)):
        with patch("sentinel.orchestrator.main.time.sleep") as sleep_mock:
            orch._classify_findings_adaptive(findings, repo, min_chunk=1)
    sleep_mock.assert_not_called()


# ── Context-window failures are also recoverable by splitting ─────────────
# (A provider with a smaller context window than LLM — e.g. a smaller hosted model — returns a
# 400 that base.py wraps as AgentError, not AgentResponseError. The gate must
# still split rather than failing the whole assessment.)

def _classify_ctx_window(verdicts, fail_when):
    """Like _classify but raises an AgentError carrying a context-window 400."""
    from sentinel.agents.base import AgentError

    def _fn(context):
        present = _present(context, verdicts)
        if fail_when(present):
            raise AgentError(
                "Agent 'architect' failed on skill 'layer5a-verifiability-gate': "
                "Error code: 400 - {'detail': 'Your input exceeds the context "
                "window of this model. Please adjust your input and try again.'}"
            )
        return {
            "classifications": [
                {"finding_id": fid, "verdict": verdicts[fid]} for fid in present
            ]
        }
    return _fn


def test_context_window_error_splits_in_half_and_merges(tmp_path):
    orch, repo = _orch(tmp_path)
    verdicts = {F1: "VERIFIABLE", F2: "ADVISORY", F3: "VERIFIABLE", F4: "FIXTURE-COMPLEX"}
    findings = [_finding(F1), _finding(F2), _finding(F3), _finding(F4)]
    full = frozenset(verdicts)

    with patch.object(orch._architect, "classify_findings",
                      side_effect=_classify_ctx_window(verdicts, lambda s: s == full)) as m:
        result = orch._classify_findings_adaptive(findings, repo, min_chunk=1)

    assert result == verdicts
    # full (context-window fail) + left half + right half
    assert m.call_count == 3


def test_context_window_error_fails_open_at_min_chunk(tmp_path, caplog):
    orch, repo = _orch(tmp_path)
    verdicts = {F1: "ADVISORY", F2: "ADVISORY"}
    findings = [_finding(F1), _finding(F2)]

    with patch.object(orch._architect, "classify_findings",
                      side_effect=_classify_ctx_window(verdicts, lambda s: True)):
        with caplog.at_level(logging.WARNING):
            result = orch._classify_findings_adaptive(findings, repo, min_chunk=2)

    assert result == {F1: "VERIFIABLE", F2: "VERIFIABLE"}


def test_non_context_window_agent_error_propagates(tmp_path):
    """A genuine (non-oversized) AgentError must NOT be swallowed by the adaptive
    split — it propagates so real failures aren't masked as VERIFIABLE."""
    import pytest

    from sentinel.agents.base import AgentError

    orch, repo = _orch(tmp_path)
    findings = [_finding(F1), _finding(F2)]

    def _fn(context):
        raise AgentError(
            "Agent 'architect' failed on skill 'layer5a-verifiability-gate': "
            "Error code: 401 - Invalid token"
        )

    with patch.object(orch._architect, "classify_findings", side_effect=_fn):
        with pytest.raises(AgentError):
            orch._classify_findings_adaptive(findings, repo, min_chunk=1)


# ── Proactive initial chunk cap (SENTINEL_VERIFIABILITY_MAX_FINDINGS_PER_CHUNK) ──
# Caps the batch size BEFORE any API call so chunks start small enough to fit a
# smaller/slower provider's context window and gateway timeout (e.g. a smaller hosted model),
# instead of sending one oversized call and splitting reactively.

def _many_findings(n):
    fids = [f"F2-{i:04d}" for i in range(1, n + 1)]
    return fids, {fid: "VERIFIABLE" for fid in fids}, [_finding(fid) for fid in fids]


def test_proactive_cap_splits_before_any_call(tmp_path):
    orch, repo = _orch(tmp_path)
    orch._config.verifiability_max_chunk = 2
    orch._config.verifiability_gate_chunk_delay = 0
    _fids, verdicts, findings = _many_findings(5)

    with patch.object(orch._architect, "classify_findings",
                      side_effect=_classify(verdicts, lambda s: False)) as m:
        result = orch._classify_findings_adaptive(findings, repo, min_chunk=1)

    assert result == verdicts
    assert m.call_count == 3  # ceil(5 / 2)


def test_proactive_cap_zero_is_unbounded_single_call(tmp_path):
    orch, repo = _orch(tmp_path)
    orch._config.verifiability_max_chunk = 0  # default: unbounded, one call
    _fids, verdicts, findings = _many_findings(5)

    with patch.object(orch._architect, "classify_findings",
                      side_effect=_classify(verdicts, lambda s: False)) as m:
        result = orch._classify_findings_adaptive(findings, repo, min_chunk=1)

    assert result == verdicts
    assert m.call_count == 1


def test_proactive_cap_695_findings_max25_makes_28_calls(tmp_path):
    import math

    orch, repo = _orch(tmp_path)
    orch._config.verifiability_max_chunk = 25
    orch._config.verifiability_gate_chunk_delay = 0
    _fids, verdicts, findings = _many_findings(695)

    with patch.object(orch._architect, "classify_findings",
                      side_effect=_classify(verdicts, lambda s: False)) as m:
        result = orch._classify_findings_adaptive(findings, repo, min_chunk=5)

    assert result == verdicts
    assert m.call_count == math.ceil(695 / 25)  # 28 — no oversized first call
