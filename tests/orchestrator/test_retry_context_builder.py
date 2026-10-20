"""Tests for the Phase 2 retry-context builder (Option A).

``build_retry_context`` produces a structured markdown block summarising why a
gate rejected the previous attempt and instructing the Coder not to repeat it,
and persists it to ``retry/{task_id}/attempt-{N}/context.md`` under a base dir.
Fixture-driven — writes only under ``tmp_path``.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from sentinel.orchestrator.retry_context_builder import (
    build_retry_context,
    write_batch_summary,
)


def test_returns_markdown_with_gate_and_attempt(tmp_path):
    md = build_retry_context(
        task_id="task-1",
        gate_name="Architect diff review",
        gate_feedback="Missing null check on the config path.",
        test_failures=None,
        attempt_number=2,
        base_dir=tmp_path,
    )
    assert isinstance(md, str)
    assert "Attempt 2" in md
    assert "Architect diff review" in md


def test_includes_gate_feedback_text(tmp_path):
    md = build_retry_context(
        task_id="task-1",
        gate_name="QA",
        gate_feedback="Regression: test_widget_roundtrip now fails.",
        test_failures=None,
        attempt_number=3,
        base_dir=tmp_path,
    )
    assert "Regression: test_widget_roundtrip now fails." in md


def test_includes_do_not_repeat_instruction(tmp_path):
    md = build_retry_context(
        task_id="task-1",
        gate_name="QA",
        gate_feedback="fail",
        test_failures=None,
        attempt_number=2,
        base_dir=tmp_path,
    )
    # The whole point of the retry packet: forbid repeating the failed approach.
    assert "not repeat" in md.lower()


def test_writes_context_md_to_disk(tmp_path):
    build_retry_context(
        task_id="task-abc",
        gate_name="Architect diff review",
        gate_feedback="fail",
        test_failures=None,
        attempt_number=2,
        base_dir=tmp_path,
    )
    written = tmp_path / "retry" / "task-abc" / "attempt-2" / "context.md"
    assert written.exists()
    assert "Attempt 2" in written.read_text(encoding="utf-8")


def test_creates_nested_directory_when_absent(tmp_path):
    target = tmp_path / "retry" / "task-xyz" / "attempt-1"
    assert not target.exists()
    build_retry_context(
        task_id="task-xyz",
        gate_name="QA",
        gate_feedback="fail",
        test_failures=None,
        attempt_number=1,
        base_dir=tmp_path,
    )
    assert (target / "context.md").exists()


def test_renders_test_failures_when_provided(tmp_path):
    md = build_retry_context(
        task_id="task-1",
        gate_name="QA",
        gate_feedback="Two tests failed.",
        test_failures=[
            {"name": "test_alpha", "detail": "expected 3, got 4"},
            {"name": "test_beta", "detail": "raised ValueError"},
        ],
        attempt_number=2,
        base_dir=tmp_path,
    )
    assert "test_alpha" in md
    assert "expected 3, got 4" in md
    assert "test_beta" in md


def test_reports_none_when_no_test_failures(tmp_path):
    md = build_retry_context(
        task_id="task-1",
        gate_name="Architect diff review",
        gate_feedback="scope violation",
        test_failures=None,
        attempt_number=2,
        base_dir=tmp_path,
    )
    # No test-failure section content, but the packet still renders cleanly.
    assert "None reported" in md


# --- FIX-BLOCKING-012: batch_summary.json concurrent read-modify-write ---


def test_write_batch_summary_preserves_concurrent_updates(tmp_path):
    """Concurrent updates for different tasks must both survive — the full
    read-modify-write is serialized, not just the final atomic replace."""
    # Seed the file so every writer reads a real base.
    write_batch_summary(tmp_path, "seed", "IN_PROGRESS", 0)

    task_ids = [f"task-{i:03d}" for i in range(24)]
    barrier = threading.Barrier(len(task_ids))

    def _worker(tid: str) -> None:
        barrier.wait()  # maximize overlap of the read-modify-write windows
        write_batch_summary(tmp_path, tid, "COMPLETE", 1)

    threads = [threading.Thread(target=_worker, args=(t,)) for t in task_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    data = json.loads((tmp_path / "batch_summary.json").read_text(encoding="utf-8"))
    # Every concurrent task update is preserved (no lost updates), plus seed.
    for tid in task_ids:
        assert tid in data["tasks"], f"lost update for {tid}"
    assert data["tasks"]["seed"]["status"] == "IN_PROGRESS"


def test_write_batch_summary_updates_existing_task_in_place(tmp_path):
    write_batch_summary(tmp_path, "task-1", "IN_PROGRESS", 1)
    write_batch_summary(tmp_path, "task-1", "COMPLETE", 2)
    data = json.loads((tmp_path / "batch_summary.json").read_text(encoding="utf-8"))
    assert data["tasks"]["task-1"]["status"] == "COMPLETE"
    assert data["tasks"]["task-1"]["attempt_count"] == 2

