"""Workflow-track loader for structural themes derived from the assess
track's SemanticGroup output.

Item 10 (cycle 9 burndown): the structural lens of the dashboard's theme
summary (spec section 9.4-2) and lens-divergence metrics (section 9.5) need
structural themes, which spec section 10.1-6 names as struct_themes.json.
The workflow track has no StructTheme persistence of its own; the structural
grouping that the structural lens represents is produced by
sentinel.assess.grouper as SemanticGroup records, persisted via
grouper.write_groups to ``<state_dir>/semantic_groups.json`` (cycle 9 Q1
decision 10-B: consume the existing grouper output rather than minting a new
struct_themes.json artifact with no writer).

Cross-track-assess flavor of the Q1-(b) parallel-loader pattern: this module
reads the JSON shape grouper emits, directly by path, with no import of the
assess track. The grouper side owns the format; this loader owns a read
contract against the on-disk JSON. SemanticGroupRecord duplicates
grouper.SemanticGroup's field shape rather than importing it, the accepted
duplication cost of preserving the workflow/assess decoupling.

This commit lands the deserializer + loader. The SemanticGroupRecord ->
ThemeIssueRecord(lens="struct") adapter follows separately.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from typing import Sequence

from sentinel.workflow.models import FindingIssueRecord, ThemeIssueRecord

log = logging.getLogger(__name__)

# Reads the JSON shape emitted by sentinel.assess.grouper._serialize_group /
# write_groups (file <state_dir>/semantic_groups.json); update if those shapes
# change. Format compatibility verified by
# tests/test_workflow_struct_theme_loader.py::test_loads_grouper_shape.
_GROUPS_FILENAME = "semantic_groups.json"
# Mirror of grouper._NO_CANONICAL_EMITTER; duplicated to preserve decoupling.
_NO_CANONICAL_EMITTER = ""

__all__ = [
	"GroupProvenanceRecord",
	"SemanticGroupRecord",
	"load_semantic_groups",
	"load_struct_themes",
]


# Mirror of sentinel.assess.grouper.Provenance. Field names/types match the
# producer exactly; update if grouper.Provenance changes.
@dataclass(frozen=True, slots=True)
class GroupProvenanceRecord:
	mode: str
	grouping_key: str
	subject_key: str | None
	finding_kind: str | None
	file: str | None
	function: str | None
	category: str | None


# Mirror of sentinel.assess.grouper.SemanticGroup. Field names/types match the
# producer exactly; update if grouper.SemanticGroup changes.
@dataclass(frozen=True, slots=True)
class SemanticGroupRecord:
	group_id: str
	grouping_key: str
	mode: str
	severity: str
	member_count: int
	member_finding_ids: tuple[str, ...]
	contributing_emitters: tuple[str, ...]
	canonical_emitter: str
	peer_emitters: tuple[str, ...]
	subject_key: str | None
	finding_kind: str | None
	file: str | None
	function: str | None
	category: str | None
	provenance: GroupProvenanceRecord


def _deserialize_semantic_group(payload: dict[str, Any]) -> SemanticGroupRecord:
	"""Reconstruct a SemanticGroupRecord from one grouper JSON entry.

	Duplicates the format-handling of grouper._deserialize_group (tolerant
	getattr-style reads with the same fallbacks) for the workflow side.
	"""
	prov_raw = payload.get("provenance") or {}
	provenance = GroupProvenanceRecord(
		mode=prov_raw.get("mode", payload.get("mode", "legacy")),
		grouping_key=prov_raw.get("grouping_key", payload.get("grouping_key", "")),
		subject_key=prov_raw.get("subject_key"),
		finding_kind=prov_raw.get("finding_kind"),
		file=prov_raw.get("file"),
		function=prov_raw.get("function"),
		category=prov_raw.get("category"),
	)
	return SemanticGroupRecord(
		group_id=payload["group_id"],
		grouping_key=payload["grouping_key"],
		mode=payload["mode"],
		severity=payload["severity"],
		member_count=payload["member_count"],
		member_finding_ids=tuple(payload.get("member_finding_ids") or ()),
		contributing_emitters=tuple(payload.get("contributing_emitters") or ()),
		canonical_emitter=payload.get("canonical_emitter", _NO_CANONICAL_EMITTER),
		peer_emitters=tuple(payload.get("peer_emitters") or ()),
		subject_key=payload.get("subject_key"),
		finding_kind=payload.get("finding_kind"),
		file=payload.get("file"),
		function=payload.get("function"),
		category=payload.get("category"),
		provenance=provenance,
	)


def load_semantic_groups(state_dir: str | Path) -> tuple[SemanticGroupRecord, ...]:
	"""Load SemanticGroup records from ``<state_dir>/semantic_groups.json``.

	Reads the artifact grouper.write_groups produces. Returns the records as
	a tuple. All failure modes return an empty tuple, but they are no longer
	indistinguishable to an operator reading logs:
	  * Absent file -> silent (expected before any assessment has run).
	  * OSError on read -> ERROR (the file exists but could not be read).
	  * Invalid JSON -> ERROR (on-disk corruption of a written artifact).
	  * Non-list top level -> WARNING (schema/shape mismatch).
	"""
	path = Path(state_dir) / _GROUPS_FILENAME
	if not path.exists():
		return ()
	try:
		text = path.read_text(encoding="utf-8")
	except OSError as exc:
		log.error("Failed to read semantic groups from %s: %s", path, exc)
		return ()
	try:
		payload = json.loads(text)
	except json.JSONDecodeError as exc:
		log.error("Corrupt semantic groups JSON at %s: %s", path, exc)
		return ()
	if not isinstance(payload, list):
		log.warning(
			"Semantic groups at %s is not a JSON list (got %s); ignoring",
			path,
			type(payload).__name__,
		)
		return ()
	return tuple(_deserialize_semantic_group(entry) for entry in payload)


def load_struct_themes(
	state_dir: str | Path,
	findings: Sequence[FindingIssueRecord] = (),
) -> tuple[ThemeIssueRecord, ...]:
	"""Load structural themes as ThemeIssueRecord(lens="struct") tuples.

	Reads the grouper SemanticGroup output (load_semantic_groups) and passes it
	to build_struct_themes (sentinel.workflow.build_struct_themes) along with the
	per-finding FindingIssueRecord data to produce section-6.4-faithful titles
	and dashboard-relevant rollup fields (item 14 producer). Returns an empty
	tuple when the groups artifact is absent or malformed (silent-default
	convention, inherited from load_semantic_groups). The output integrates
	directly into the dashboard's build_theme_summary struct_themes parameter
	(wired in cycle 8 Commit 3).

	``findings`` is optional and defaults to empty: callers without per-finding
	data still get themes, via the producer's empty-member fallback path (the
	cycle-9 adapter's SemanticGroup-only behavior is preserved as that fallback).
	Supplying findings produces the richer, section-6.4-faithful themes.
	"""
	# Local import avoids a circular import: build_struct_themes imports
	# SemanticGroupRecord from this module.
	from sentinel.workflow.build_struct_themes import build_struct_themes
	groups = load_semantic_groups(state_dir)
	return build_struct_themes(groups, findings)
