"""FIX-REQUIRED-009: presence-based payload selection + partial-result retry."""
from unittest.mock import MagicMock

import sentinel.assess.reviewer as rv
from sentinel.assess.reviewer import _extract_findings, ReviewOrchestrator


class _Falsy:
    def __bool__(self) -> bool:
        return False


def _f(desc):
    return {"description": desc, "severity": "P2", "location": "m.py:1"}


def _passthrough(monkeypatch):
    monkeypatch.setattr(rv, "materialize_emitter_finding", lambda finding, **k: finding)


# ── Part A: presence-based field selection ───────────────────────────────

def test_findings_key_used_even_when_empty_container():
    assert _extract_findings({"findings": [], "issues": [_f("x")]}, "m", "s") == []


def test_falsey_custom_findings_object_used_over_fallback():
    assert _extract_findings({"findings": _Falsy(), "issues": [_f("x")]}, "m", "s") == []


def test_uses_issues_when_findings_absent(monkeypatch):
    _passthrough(monkeypatch)
    assert len(_extract_findings({"issues": [_f("x")]}, "m", "s")) == 1


def test_uses_results_when_findings_and_issues_absent(monkeypatch):
    _passthrough(monkeypatch)
    assert len(_extract_findings({"results": [_f("x")]}, "m", "s")) == 1


def test_selection_by_presence_not_truthiness():
    assert _extract_findings({"findings": [], "issues": [_f("y")]}, "m", "s") == []


def test_normalizes_single_non_list_payload(monkeypatch):
    _passthrough(monkeypatch)
    assert len(_extract_findings({"findings": _f("single")}, "m", "s")) == 1


# ── Part B: partial-result retry contract ────────────────────────────────

def _orch():
    o = ReviewOrchestrator.__new__(ReviewOrchestrator)
    o._repo_root_canonical = ""
    o._invalid_kind_counter = [0]
    o._build_payload_retry_context = MagicMock(return_value="")
    return o


def _sub(i):
    s = MagicMock()
    s.content = f"c{i}"
    s.content_with_header = f"c{i}"
    s.chunk_index = i
    return s


def _one(*_a, **_k):
    return [{"description": "rec"}]


def _runner_seq(*outcomes):
    """outcomes per call: 'ok' returns a finding-result, 'fail' raises (non-413)."""
    state = {"n": 0}

    def run(runner, context, module_name, skill, phase):
        outcome = outcomes[state["n"]]
        state["n"] += 1
        if outcome == "ok":
            return {"findings": [{"description": "rec"}]}
        raise RuntimeError("boom")

    return run


def _call_payload(o):
    return o._retry_chunk_for_payload(
        module={"path": "m.py"}, module_name="m", skill="s",
        chunk=MagicMock(content="big", chunk_index=0),
        prior_findings=None, runner=lambda c: None,
    )


def test_retry_payload_returns_partial_when_later_subchunk_fails(monkeypatch):
    monkeypatch.setattr(rv, "_extract_findings", _one)
    o = _orch()
    o._payload_retry_chunker = MagicMock()
    o._payload_retry_chunker.chunk_string.return_value = [_sub(0), _sub(1)]
    o._run_with_rate_limit_retry = _runner_seq("ok", "fail")
    res = _call_payload(o)
    assert res.status == "partial"
    assert len(res.findings) >= 1


def test_retry_payload_chars_returns_partial_when_later_segment_fails(monkeypatch):
    monkeypatch.setattr(rv, "_extract_findings", _one)
    monkeypatch.setattr(rv, "_split_by_chars", lambda *a, **k: ["s0", "s1"])
    o = _orch()
    o._run_with_rate_limit_retry = _runner_seq("ok", "fail")
    res = o._retry_chunk_for_payload_chars(
        module={"path": "m.py"}, module_name="m", skill="s",
        source_code="big", runner=lambda c: None,
    )
    assert res.status == "partial"
    assert len(res.findings) >= 1


def test_retry_distinguishes_no_recovery_from_partial(monkeypatch):
    monkeypatch.setattr(rv, "_extract_findings", _one)
    o = _orch()
    o._payload_retry_chunker = MagicMock()
    o._payload_retry_chunker.chunk_string.return_value = [_sub(0), _sub(1)]
    o._run_with_rate_limit_retry = _runner_seq("fail", "fail")
    none = _call_payload(o)
    o._run_with_rate_limit_retry = _runner_seq("ok", "fail")
    partial = _call_payload(o)
    assert none.status == "failed"
    assert partial.status == "partial"
    assert none.status != partial.status


def test_retry_success_unchanged_when_all_subchunks_recover(monkeypatch):
    monkeypatch.setattr(rv, "_extract_findings", _one)
    o = _orch()
    o._payload_retry_chunker = MagicMock()
    o._payload_retry_chunker.chunk_string.return_value = [_sub(0), _sub(1)]
    o._run_with_rate_limit_retry = _runner_seq("ok", "ok")
    res = _call_payload(o)
    assert res.status == "complete"
    assert len(res.findings) == 2
