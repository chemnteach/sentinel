"""Tests for the Phase-3 agentic task packet generator."""
from __future__ import annotations

from sentinel.orchestrator.task_manager import Task
from sentinel.orchestrator.task_packet import generate_packet


def _task(**kw):
    base = dict(
        task_id="t1",
        plan_id="FIX-BLOCKING-001",
        description="A hardcoded credential default in config.py.",
        affected_files=["sentinel/config.py", "sentinel/other.py"],
        acceptance_criteria=["No hardcoded default", "Validation fails closed"],
        scope_boundary="",
    )
    base.update(kw)
    return Task(**base)


def test_packet_includes_plan_id():
    assert "## Task: FIX-BLOCKING-001" in generate_packet(_task())


def test_packet_includes_all_affected_files():
    packet = generate_packet(_task())
    assert "- sentinel/config.py" in packet
    assert "- sentinel/other.py" in packet


def test_packet_includes_all_acceptance_criteria():
    packet = generate_packet(_task())
    assert "1. No hardcoded default" in packet
    assert "2. Validation fails closed" in packet


def test_packet_includes_hard_gates_section():
    packet = generate_packet(_task())
    assert "**Hard gates (all required):**" in packet
    assert "FAILS on the current buggy code" in packet
    assert "pytest tests/ -q --no-cov" in packet
    assert "Do not one-shot." in packet
    assert "Report: SUCCESS/FAIL" in packet


def test_packet_preserves_explicit_empty_scope_boundary():
    # An explicit "" means "no extra scope" and must NOT be overwritten by the
    # default scope text. (FIX-DESIRED-003)
    from sentinel.orchestrator.task_packet import _DEFAULT_SCOPE

    packet = generate_packet(_task(scope_boundary=""))
    assert _DEFAULT_SCOPE not in packet


def test_packet_falls_back_to_default_scope_when_unset():
    # A genuinely unset scope_boundary (None) falls back to the default text.
    from types import SimpleNamespace

    from sentinel.orchestrator.task_packet import _DEFAULT_SCOPE

    task = SimpleNamespace(
        plan_id="FIX-X-001",
        description="d",
        affected_files=[],
        acceptance_criteria=[],
        scope_boundary=None,
    )
    assert _DEFAULT_SCOPE in generate_packet(task)


def test_packet_handles_empty_acceptance_criteria():
    packet = generate_packet(_task(acceptance_criteria=[]))
    assert "**Acceptance criteria:**" in packet   # section present, no crash
    assert "(none specified)" in packet
    assert "## Task: FIX-BLOCKING-001" in packet
