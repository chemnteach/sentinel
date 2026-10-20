import json

import pytest

from sentinel.workflow.sync_state import (
	WorkflowSyncState,
	record_sync_state,
	load_sync_state,
	_SYNC_STATE_FILENAME,
)


def test_workflow_sync_state_defaults():
	state = WorkflowSyncState()
	assert state.last_sync_timestamp == ""
	assert state.last_sync_outcome == "never"
	assert state.last_sync_error_summary == ""


def test_workflow_sync_state_to_dict():
	state = WorkflowSyncState(
		last_sync_timestamp="2026-05-31T00:00:00Z",
		last_sync_outcome="success",
		last_sync_error_summary="",
	)
	assert state.to_dict() == {
		"last_sync_timestamp": "2026-05-31T00:00:00Z",
		"last_sync_outcome": "success",
		"last_sync_error_summary": "",
	}


def test_workflow_sync_state_from_dict_tolerates_missing_keys():
	# Partial dict -> defaults fill the gaps (ConceptTheme.from_dict pattern).
	state = WorkflowSyncState.from_dict({"last_sync_outcome": "failure"})
	assert state.last_sync_outcome == "failure"
	assert state.last_sync_timestamp == ""
	assert state.last_sync_error_summary == ""


def test_record_sync_state_writes_json(tmp_path):
	state = WorkflowSyncState(
		last_sync_timestamp="2026-05-31T12:00:00Z",
		last_sync_outcome="failure",
		last_sync_error_summary="github 503",
	)
	record_sync_state(tmp_path, state)
	path = tmp_path / _SYNC_STATE_FILENAME
	assert path.exists()
	data = json.loads(path.read_text(encoding="utf-8"))
	assert data == state.to_dict()


def test_record_sync_state_leaves_no_temp_artifact(tmp_path):
	# The atomic temp-then-replace write must not leave a ``.tmp`` file behind
	# and must produce a readable final file. (FIX-DESIRED-023)
	state = WorkflowSyncState(last_sync_outcome="success")
	record_sync_state(tmp_path, state)
	assert list(tmp_path.glob("*.tmp")) == []
	assert load_sync_state(tmp_path).last_sync_outcome == "success"


def test_load_sync_state_roundtrip(tmp_path):
	state = WorkflowSyncState(
		last_sync_timestamp="2026-05-31T09:30:00Z",
		last_sync_outcome="success",
	)
	record_sync_state(tmp_path, state)
	assert load_sync_state(tmp_path) == state


def test_load_sync_state_returns_default_on_missing_file(tmp_path):
	loaded = load_sync_state(tmp_path)
	assert loaded == WorkflowSyncState()
	assert loaded.last_sync_outcome == "never"


def test_load_sync_state_returns_default_on_malformed_json(tmp_path):
	(tmp_path / _SYNC_STATE_FILENAME).write_text("{ not valid json", encoding="utf-8")
	assert load_sync_state(tmp_path) == WorkflowSyncState()


def test_load_sync_state_returns_default_on_unexpected_shape(tmp_path):
	(tmp_path / _SYNC_STATE_FILENAME).write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
	assert load_sync_state(tmp_path) == WorkflowSyncState()


def test_workflow_sync_state_enum_values(tmp_path):
	# Pin the three documented outcome values through a record/load roundtrip.
	for outcome in ("never", "success", "failure"):
		state = WorkflowSyncState(last_sync_outcome=outcome)
		record_sync_state(tmp_path, state)
		assert load_sync_state(tmp_path).last_sync_outcome == outcome


def test_path_accepts_str_and_path(tmp_path):
	state = WorkflowSyncState(last_sync_outcome="success", last_sync_timestamp="2026-05-31T00:00:00Z")
	record_sync_state(str(tmp_path), state)
	assert load_sync_state(tmp_path) == state
	assert load_sync_state(str(tmp_path)) == state
