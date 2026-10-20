"""Workflow-track loader for the orchestrator-track quality scorecard.

Item 11 (cycle 9 burndown): the run-confidence dashboard section (spec
section 9.4-8, wired in cycle 8 Commit 4 via Option-3 dict injection) needs
the quality scorecard, but the scorecard is an orchestrator-track artifact
(quality_scorecard.json, written by the orchestrator's StateStore and
produced by sentinel.orchestrator.quality_artifacts). The workflow track has
no dependency on the orchestrator track and the engagement preserves that
decoupling.

Per the cycle 9 Q1 decision (option b: parallel loaders), this module reads
the JSON artifact directly by path and shapes it into a plain dict, with no
import of the orchestrator track. The on-disk JSON file is the integration
boundary; the orchestrator side owns the writing, this loader owns a read
contract against that JSON shape.

Cross-track flavor: read-direction is orchestrator -> workflow; the format
authority is the orchestrator side.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = ["load_quality_scorecard", "empty_scorecard_default"]


# Mirror of sentinel.orchestrator.quality_artifacts.empty_quality_scorecard.
# Duplicated rather than imported to preserve the workflow/orchestrator track
# decoupling (cycle 9 Q1 option b); update if that producer shape changes.
# Format compatibility verified by
# tests/test_workflow_quality_scorecard_loader.py::test_loads_orchestrator_shape
# and ::test_default_matches_orchestrator_empty_shape.
def empty_scorecard_default() -> dict[str, Any]:
	"""Return the empty quality-scorecard shape (incomplete run / no data)."""
	return {
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


# Reads the JSON shape emitted by
# sentinel.orchestrator.quality_artifacts.build_quality_scorecard and
# empty_quality_scorecard; update if those shapes change.
# Format compatibility verified by
# tests/test_workflow_quality_scorecard_loader.py::test_loads_orchestrator_shape.
def load_quality_scorecard(path: str | Path) -> dict[str, Any]:
	"""Load the quality scorecard JSON written by the orchestrator track.

	Returns the parsed dict on success. When the file is absent or its
	contents are not valid JSON, returns the empty-scorecard default
	(``confidence_label="incomplete"``) -- malformed JSON is treated as
	equivalent to an absent file, consistent with how the run-confidence
	dashboard section already handles the empty case. The caller (dashboard
	run-confidence builder) need not distinguish absent from corrupt.
	"""
	file_path = Path(path)
	if not file_path.exists():
		return empty_scorecard_default()
	try:
		text = file_path.read_text(encoding="utf-8")
		data = json.loads(text)
	except (json.JSONDecodeError, OSError):
		return empty_scorecard_default()
	if not isinstance(data, dict):
		return empty_scorecard_default()
	return data
