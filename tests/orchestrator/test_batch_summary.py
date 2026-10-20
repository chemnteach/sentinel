"""Tests for write_batch_summary (Phase 3, Deliverable 4).

Writes/merges per-task remediation status into state_dir/batch_summary.json,
which Screen 3 (/api/progress) reads. Atomic write (tmp + replace) with a
Windows lock-race guard. Fixture-driven — writes only under tmp_path.
"""

from __future__ import annotations

import json

from sentinel.orchestrator.retry_context_builder import write_batch_summary


def _read(state_dir):
    return json.loads((state_dir / "batch_summary.json").read_text(encoding="utf-8"))


def test_creates_file_with_run_label_and_task(tmp_path):
    state = tmp_path / "self-assessment-012"
    state.mkdir()
    write_batch_summary(state, "task-1", "IN_PROGRESS", 1)
    data = _read(state)
    assert data["run_label"] == "self-assessment-012"
    assert "last_updated" in data and data["last_updated"]
    assert data["tasks"]["task-1"]["status"] == "IN_PROGRESS"
    assert data["tasks"]["task-1"]["attempt_count"] == 1
    assert data["tasks"]["task-1"]["last_updated"]


def test_second_task_is_merged_not_overwritten(tmp_path):
    state = tmp_path / "run"
    state.mkdir()
    write_batch_summary(state, "task-1", "COMPLETE", 2)
    write_batch_summary(state, "task-2", "ESCALATED", 4)
    data = _read(state)
    assert set(data["tasks"]) == {"task-1", "task-2"}
    assert data["tasks"]["task-1"]["status"] == "COMPLETE"
    assert data["tasks"]["task-2"]["status"] == "ESCALATED"


def test_same_task_is_updated_in_place(tmp_path):
    state = tmp_path / "run"
    state.mkdir()
    write_batch_summary(state, "task-1", "IN_PROGRESS", 1)
    write_batch_summary(state, "task-1", "COMPLETE", 3)
    data = _read(state)
    assert len(data["tasks"]) == 1
    assert data["tasks"]["task-1"]["status"] == "COMPLETE"
    assert data["tasks"]["task-1"]["attempt_count"] == 3


def test_no_leftover_tmp_file(tmp_path):
    state = tmp_path / "run"
    state.mkdir()
    write_batch_summary(state, "task-1", "IN_PROGRESS", 0)
    assert not (state / "batch_summary.tmp").exists()
    assert (state / "batch_summary.json").exists()


def test_corrupt_existing_file_is_replaced_gracefully(tmp_path):
    state = tmp_path / "run"
    state.mkdir()
    (state / "batch_summary.json").write_text("{ not json", encoding="utf-8")
    # Must not raise; starts a fresh well-formed summary.
    write_batch_summary(state, "task-1", "IN_PROGRESS", 1)
    data = _read(state)
    assert data["tasks"]["task-1"]["status"] == "IN_PROGRESS"
