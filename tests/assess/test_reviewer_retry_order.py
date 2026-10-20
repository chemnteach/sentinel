"""FIX-REQUIRED-011: payload retry preserves findings from ALL successful
subchunks (order-invariant) instead of returning early on the first failure."""
from unittest.mock import MagicMock

import sentinel.assess.reviewer as rv
from sentinel.assess.reviewer import ReviewOrchestrator


def _sub(i):
    s = MagicMock()
    s.content = f"c{i}"
    s.content_with_header = f"c{i}"
    s.chunk_index = i
    return s


def _runner_seq(*outcomes):
    state = {"n": 0}

    def run(runner, context, module_name, skill, phase):
        outcome = outcomes[state["n"]]
        state["n"] += 1
        if outcome == "ok":
            return {"findings": [{"description": "X"}]}
        raise RuntimeError("boom")

    return run


def _run_payload(outcomes):
    """Run _retry_chunk_for_payload over len(outcomes) subchunks; each entry is
    'ok' (yields a finding) or 'fail' (raises a non-413 error)."""
    orch = ReviewOrchestrator.__new__(ReviewOrchestrator)
    orch._repo_root_canonical = ""
    orch._invalid_kind_counter = [0]
    orch._build_payload_retry_context = MagicMock(return_value="")
    orch._payload_retry_chunker = MagicMock()
    orch._payload_retry_chunker.chunk_string.return_value = [
        _sub(i) for i in range(len(outcomes))
    ]
    orch._run_with_rate_limit_retry = _runner_seq(*outcomes)
    return orch._retry_chunk_for_payload(
        module={"path": "m.py"}, module_name="m", skill="s",
        chunk=MagicMock(content="big", chunk_index=0),
        prior_findings=None, runner=lambda c: None,
    )


def test_returns_accumulated_findings_when_later_subchunk_fails(monkeypatch):
    monkeypatch.setattr(rv, "_extract_findings", lambda *a, **k: [{"description": "X"}])
    res = _run_payload(["ok", "fail"])
    assert res.findings  # earlier success preserved, not empty
    assert res.status != "complete"


def test_mixed_success_marked_partial_not_complete(monkeypatch):
    monkeypatch.setattr(rv, "_extract_findings", lambda *a, **k: [{"description": "X"}])
    res = _run_payload(["ok", "fail"])
    # status signals degraded recovery independent of findings presence
    assert res.status == "partial"
    assert len(res.findings) >= 1


def test_recovered_findings_are_order_invariant(monkeypatch):
    monkeypatch.setattr(rv, "_extract_findings", lambda *a, **k: [{"description": "X"}])
    ok_then_fail = _run_payload(["ok", "fail"])
    fail_then_ok = _run_payload(["fail", "ok"])
    # Same success/failure set, different order → identical preserved findings.
    assert ok_then_fail.findings == fail_then_ok.findings
    assert ok_then_fail.status == fail_then_ok.status == "partial"


def test_all_subchunks_fail_returns_failed_and_no_findings(monkeypatch):
    monkeypatch.setattr(rv, "_extract_findings", lambda *a, **k: [{"description": "X"}])
    res = _run_payload(["fail", "fail"])
    assert res.status == "failed"
    assert res.findings == []


def test_all_subchunks_succeed_returns_complete(monkeypatch):
    monkeypatch.setattr(rv, "_extract_findings", lambda *a, **k: [{"description": "X"}])
    res = _run_payload(["ok", "ok"])
    assert res.status == "complete"
    assert len(res.findings) == 2
