"""Workflow-track sync-state persistence.

Item 12 (cycle 9 burndown): the dashboard's audit/freshness summary (spec
section 9.4-9, "workflow-store sync status when enabled") and the section 9.6
freshness/audit requirement ("whether workflow state was synchronized before
report generation, when workflow state is enabled") need a persisted record
of the last workflow sync. reconcile_issues computes plans and drift purely
in memory and persists nothing; no last-sync timestamp is recorded anywhere
the dashboard can read.

This module is the workflow-INTERNAL flavor of the cycle 9 Q1-(b) pattern:
unlike the quality-scorecard loader (cross-track-orchestrator) and the
struct-theme loader (cross-track-assess), both the writer (record_sync_state)
and the reader (load_sync_state) live in the workflow track, so there is no
cross-track format authority and no cross-track pinning comment -- the
pinning reference is the sibling writer in this same module.

Unwired-primitive status: record_sync_state has no production caller as of
cycle 9. This mirrors cycle 7's detect_drift, which landed unwired and was
consumed in cycle 8. A future Phase 4/5 sync runner that composes the
reconcile_issues planners into a real sync pass will invoke record_sync_state
at completion; the dashboard's audit-freshness consumer reads via
load_sync_state.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# The three documented last_sync_outcome values (spec section 9.6 three-way
# distinction). An out-of-set value on read is coerced to the pre-sync default
# so the dashboard never renders an undocumented outcome.
_VALID_OUTCOMES = {"never", "success", "failure"}

# sync_state.json lives at <state_dir>/sync_state.json, following the
# <state_dir>/<file>.json convention used by grouper.write_groups
# (semantic_groups.json) and the concept-theme persistence functions.
# record_sync_state writes it; load_sync_state reads it; both in this module.
_SYNC_STATE_FILENAME = "sync_state.json"

__all__ = [
	"WorkflowSyncState",
	"record_sync_state",
	"load_sync_state",
]


@dataclass(frozen=True, slots=True)
class WorkflowSyncState:
	"""Last-sync record for the workflow store (spec sections 9.4-9, 9.6).

	The three ``last_sync_outcome`` values map to section 9.6's three-way
	distinction:
	  * "never"   -- the pre-sync state; no sync has run. last_sync_timestamp
	                 is "" and last_sync_error_summary is "".
	  * "success" -- a sync completed; last_sync_timestamp holds the ISO 8601
	                 completion time; last_sync_error_summary is "".
	  * "failure" -- a sync ran but errored; last_sync_timestamp holds the ISO
	                 attempt time and last_sync_error_summary describes the error.

	The field type is ``str`` rather than a Literal; the enumeration is the
	docstring contract, pinned by tests rather than the type system.
	"""

	last_sync_timestamp: str = ""
	last_sync_outcome: str = "never"
	last_sync_error_summary: str = ""

	def to_dict(self) -> dict[str, Any]:
		return {
			"last_sync_timestamp": self.last_sync_timestamp,
			"last_sync_outcome": self.last_sync_outcome,
			"last_sync_error_summary": self.last_sync_error_summary,
		}

	@classmethod
	def from_dict(cls, data: dict[str, Any]) -> "WorkflowSyncState":
		# Coerce each field to str: on-disk JSON may carry non-string values
		# (e.g. null, a number) that would otherwise flow through untyped and
		# violate the dataclass's str contract. A missing/None outcome, or one
		# outside the documented enumeration, is normalized back to "never".
		timestamp = data.get("last_sync_timestamp", "")
		timestamp = "" if timestamp is None else str(timestamp)
		error_summary = data.get("last_sync_error_summary", "")
		error_summary = "" if error_summary is None else str(error_summary)
		outcome = data.get("last_sync_outcome", "never")
		outcome = "never" if outcome is None else str(outcome)
		if outcome not in _VALID_OUTCOMES:
			log.warning(
				"Unknown last_sync_outcome %r; coercing to 'never'", outcome
			)
			outcome = "never"
		return cls(
			last_sync_timestamp=timestamp,
			last_sync_outcome=outcome,
			last_sync_error_summary=error_summary,
		)


def record_sync_state(state_dir: str | Path, state: WorkflowSyncState) -> None:
	"""Write the workflow sync state to ``<state_dir>/sync_state.json``.

	Invoked by a future sync runner at sync-pass completion (unwired as of
	cycle 9). The timestamp is caller-supplied on the WorkflowSyncState (no
	wall-clock read here), consistent with how the dashboard takes
	generation_timestamp as a parameter.
	"""
	path = Path(state_dir) / _SYNC_STATE_FILENAME
	path.parent.mkdir(parents=True, exist_ok=True)
	# Atomic write: serialize to a temp file then replace, so a crash mid-write
	# cannot leave a truncated/corrupt sync_state.json. (FIX-DESIRED-023)
	tmp = path.with_suffix(path.suffix + ".tmp")
	tmp.write_text(
		json.dumps(state.to_dict(), indent=2) + "\n",
		encoding="utf-8",
	)
	tmp.replace(path)


def load_sync_state(state_dir: str | Path) -> WorkflowSyncState:
	"""Read the workflow sync state from ``<state_dir>/sync_state.json``.

	Returns a default WorkflowSyncState (outcome "never") in every failure
	mode. An absent file is the expected pre-sync state and returns silently;
	a file that exists but cannot be parsed (unreadable, invalid JSON, or not a
	JSON object) is corruption and is logged at WARNING before falling back, so
	the never-synced and corrupt cases are distinguishable in the logs.
	"""
	path = Path(state_dir) / _SYNC_STATE_FILENAME
	if not path.exists():
		return WorkflowSyncState()
	try:
		data = json.loads(path.read_text(encoding="utf-8"))
	except (json.JSONDecodeError, OSError) as exc:
		log.warning("Failed to load sync state from %s: %s", path, exc)
		return WorkflowSyncState()
	if not isinstance(data, dict):
		log.warning(
			"Sync state at %s is not a JSON object (got %s); using default",
			path,
			type(data).__name__,
		)
		return WorkflowSyncState()
	return WorkflowSyncState.from_dict(data)
