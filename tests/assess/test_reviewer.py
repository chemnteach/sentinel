from types import SimpleNamespace

from sentinel.assess.reviewer import ReviewOrchestrator
from sentinel.skills.loader import Skills


class _ArchitectStub:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def specialist_review(self, skill, context):
        self.calls.append((skill, context))
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class _StoreStub:
    def append_finding(self, finding):
        pass


class _ConfigStub:
    strict_quality_mode = False
    chunk_max_lines = 200
    chunk_overlap_lines = 20
    max_chunks_per_module = 10


class _ScanContextStub:
    repo_root_canonical = ""


class _ChunkStub:
    def __init__(self):
        self.content = "print('x')\n"
        self.content_with_header = self.content
        self.chunk_index = 0
        self.total_chunks = 1
        self.start_line = 1
        self.end_line = 1


def _make_orchestrator(architect):
    return ReviewOrchestrator(
        architect=architect,
        store=_StoreStub(),
        config=_ConfigStub(),
        scan_context=_ScanContextStub(),
    )


def _make_chunk():
    return _ChunkStub()


def test_run_specialist_with_telemetry_records_retry_exception_as_terminal_failure_when_compact_retry_fails():
    initial_exc = RuntimeError("HTTP 413 payload too large")
    retry_exc = RuntimeError("retry path failed")
    architect = _ArchitectStub([initial_exc])
    orchestrator = _make_orchestrator(architect)
    orchestrator._retry_chunk_for_payload = lambda **kwargs: (_ for _ in ()).throw(retry_exc)

    findings, attempted, failed, failures = orchestrator._run_specialist_with_telemetry(
        skill=Skills.CORRECTNESS,
        module={"name": "mod", "path": "mod.py"},
        chunks=[_make_chunk()],
        prior_findings=[],
    )

    assert findings == []
    assert attempted == 1
    assert failed == 1
    assert len(failures) == 1
    failure = failures[0]
    assert failure["error_type"] == "RuntimeError"
    assert failure["error"] == "retry path failed"
    assert failure["failure_metadata"]["initial_attempt_failed"] is True
    assert failure["failure_metadata"]["retry_attempted"] is True
    assert failure["failure_metadata"]["retry_failed"] is True
    assert failure["failure_metadata"]["initial_error"] == "HTTP 413 payload too large"
    assert failure["failure_metadata"]["retry_error"] == "retry path failed"
    assert failure["failure_metadata"]["terminal_outcome"] == "retry_failure"
    assert failure["failure_metadata"]["terminal_error"] == "retry path failed"


def test_run_specialist_with_telemetry_does_not_leave_terminal_failure_when_compact_retry_succeeds():
    initial_exc = RuntimeError("HTTP 413 payload too large")
    architect = _ArchitectStub([initial_exc])
    orchestrator = _make_orchestrator(architect)
    orchestrator._retry_chunk_for_payload = lambda **kwargs: SimpleNamespace(
        status="complete",
        findings=[{"id": "recovered"}],
    )

    findings, attempted, failed, failures = orchestrator._run_specialist_with_telemetry(
        skill=Skills.CORRECTNESS,
        module={"name": "mod", "path": "mod.py"},
        chunks=[_make_chunk()],
        prior_findings=[],
    )

    assert findings == [{"id": "recovered"}]
    assert attempted == 1
    assert failed == 0
    assert failures == []


def test_run_specialist_with_telemetry_includes_metadata_distinguishing_initial_and_retry_failures():
    initial_exc = RuntimeError("HTTP 413 payload too large")
    retry_exc = RuntimeError("retry path failed")
    architect = _ArchitectStub([initial_exc])
    orchestrator = _make_orchestrator(architect)
    orchestrator._retry_chunk_for_payload = lambda **kwargs: (_ for _ in ()).throw(retry_exc)

    _, _, _, failures = orchestrator._run_specialist_with_telemetry(
        skill=Skills.CORRECTNESS,
        module={"name": "mod", "path": "mod.py"},
        chunks=[_make_chunk()],
        prior_findings=[],
    )

    metadata = failures[0]["failure_metadata"]
    assert metadata["initial_attempt_failed"] is True
    assert metadata["retry_attempted"] is True
    assert metadata["retry_failed"] is True
    assert metadata["initial_error_type"] == "RuntimeError"
    assert metadata["initial_error"] == "HTTP 413 payload too large"
    assert metadata["retry_error_type"] == "RuntimeError"
    assert metadata["retry_error"] == "retry path failed"
    assert metadata["terminal_outcome"] == "retry_failure"
    assert metadata["terminal_error_type"] == "RuntimeError"
    assert metadata["terminal_error"] == "retry path failed"


def test_run_specialist_with_telemetry_covers_initial_failure_then_compact_retry_failure_branch():
    initial_exc = RuntimeError("HTTP 413 payload too large")
    retry_exc = RuntimeError("retry path failed")
    architect = _ArchitectStub([initial_exc])
    orchestrator = _make_orchestrator(architect)
    retry_called = {"value": False}

    def _retry(**kwargs):
        retry_called["value"] = True
        raise retry_exc

    orchestrator._retry_chunk_for_payload = _retry

    findings, attempted, failed, failures = orchestrator._run_specialist_with_telemetry(
        skill=Skills.CORRECTNESS,
        module={"name": "mod", "path": "mod.py"},
        chunks=[_make_chunk()],
        prior_findings=[],
    )

    assert retry_called["value"] is True
    assert findings == []
    assert attempted == 1
    assert failed == 1
    assert failures[0]["error"] == "retry path failed"
    assert failures[0]["failure_metadata"]["terminal_outcome"] == "retry_failure"


def test_payload_retry_result_rejects_invalid_status():
    """_PayloadRetryResult enforces its closed status set at construction
    (FIX-REQUIRED-327)."""
    import pytest

    from sentinel.assess.reviewer import _PayloadRetryResult

    # Valid statuses construct fine.
    for status in ("complete", "partial", "failed"):
        assert _PayloadRetryResult(status, []).status == status

    # Any other value fails fast before it can enter retry accounting.
    with pytest.raises(ValueError):
        _PayloadRetryResult("done", [])
    with pytest.raises(ValueError):
        _PayloadRetryResult("", [])


def test_run_specialist_with_telemetry_covers_initial_failure_then_compact_retry_success_branch():
    initial_exc = RuntimeError("HTTP 413 payload too large")
    architect = _ArchitectStub([initial_exc])
    orchestrator = _make_orchestrator(architect)
    retry_called = {"value": False}

    def _retry(**kwargs):
        retry_called["value"] = True
        return SimpleNamespace(status="complete", findings=[{"id": "ok"}])

    orchestrator._retry_chunk_for_payload = _retry

    findings, attempted, failed, failures = orchestrator._run_specialist_with_telemetry(
        skill=Skills.CORRECTNESS,
        module={"name": "mod", "path": "mod.py"},
        chunks=[_make_chunk()],
        prior_findings=[],
    )

    assert retry_called["value"] is True
    assert findings == [{"id": "ok"}]
    assert attempted == 1
    assert failed == 0
    assert failures == []
