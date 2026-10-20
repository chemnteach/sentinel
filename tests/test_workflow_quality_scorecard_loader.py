import json

import pytest

from sentinel.workflow.quality_scorecard_loader import (
	load_quality_scorecard,
	empty_scorecard_default,
)


# Mirror of sentinel.orchestrator.quality_artifacts.build_quality_scorecard
# output shape (the populated case). Kept here as the format-compatibility
# fixture; update alongside the loader's default if the producer changes.
def _orchestrator_populated_shape():
	return {
		"confidence_label": "high",
		"metrics": {
			"module_completeness_pct": 100.0,
			"quick_review_success_pct": 100.0,
			"evidence_completeness_pct": 98.0,
			"lane_agreement_pct": 98.0,
			"unresolved_conflicts_by_severity": {},
			"unresolved_critical_conflicts": 0,
			"failed_chunk_entries": 0,
			"failure_telemetry_summary": {},
		},
		"gates": {
			"review_completeness": "pass",
			"lane_conflict_resolution": "pass",
			"confidence_floor": "pass",
		},
		"remediation_guidance": [],
	}


def test_loads_orchestrator_shape(tmp_path):
	scorecard = _orchestrator_populated_shape()
	path = tmp_path / "quality_scorecard.json"
	path.write_text(json.dumps(scorecard, indent=2), encoding="utf-8")
	loaded = load_quality_scorecard(path)
	assert loaded == scorecard
	assert loaded["confidence_label"] == "high"
	assert loaded["metrics"]["lane_agreement_pct"] == 98.0
	assert loaded["gates"]["confidence_floor"] == "pass"


def test_default_matches_orchestrator_empty_shape():
	# The loader's default must match the orchestrator's empty_quality_scorecard
	# shape verbatim (the Q1-b format contract). This pins the duplication.
	default = empty_scorecard_default()
	assert default == {
		"confidence_label": "incomplete",
		"metrics": {
			"module_completeness_pct": 0.0,
			"evidence_completeness_pct": 0.0,
			"lane_agreement_pct": 0.0,
			"unresolved_critical_conflicts": 0,
		},
		"gates": {
			"review_completeness": "unknown",
			"lane_conflict_resolution": "unknown",
			"confidence_floor": "unknown",
		},
	}


def test_returns_empty_shape_on_missing_file(tmp_path):
	path = tmp_path / "does_not_exist.json"
	loaded = load_quality_scorecard(path)
	assert loaded == empty_scorecard_default()
	assert loaded["confidence_label"] == "incomplete"


def test_returns_empty_shape_on_malformed_json(tmp_path):
	path = tmp_path / "corrupt.json"
	path.write_text("{ this is not valid json", encoding="utf-8")
	loaded = load_quality_scorecard(path)
	# Malformed JSON treated as equivalent to absent (silent default).
	assert loaded == empty_scorecard_default()


def test_returns_empty_shape_on_non_dict_json(tmp_path):
	path = tmp_path / "list.json"
	path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
	loaded = load_quality_scorecard(path)
	assert loaded == empty_scorecard_default()


def test_path_resolution_accepts_str_and_path(tmp_path):
	scorecard = _orchestrator_populated_shape()
	path = tmp_path / "quality_scorecard.json"
	path.write_text(json.dumps(scorecard), encoding="utf-8")
	# Both a Path object and a string path resolve identically.
	assert load_quality_scorecard(path) == scorecard
	assert load_quality_scorecard(str(path)) == scorecard
