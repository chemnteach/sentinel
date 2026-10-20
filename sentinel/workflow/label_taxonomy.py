"""GitHub label taxonomy definitions for workflow state and theme grouping."""

from __future__ import annotations

from sentinel.workflow.models import FindingIssueRecord, ThemeIssueRecord


SAVED_FILTERS = {
	"code_owner_triage_queue": "state:triage sort:severity",
	"code_owner_pr_queue": "is:pr label:sentinel-auto state:open",
	"owner_structural_scope": "is-struct-theme state:owner-assessed sort:severity",
	"owner_conceptual_scope": "is-concept-theme state:owner-assessed sort:severity",
	"sentinel_remediation_queue": "state:owner-approved sort:severity",
	"audit_lane_conflicts": "needs-human-review",
}


def build_finding_labels(record: FindingIssueRecord) -> list[str]:
	labels = [
		f"severity:{record.severity.lower()}",
		f"state:{record.status}",
		f"skill:{record.skill}",
		f"category:{record.category}",
		f"struct-theme:{record.struct_theme_id}",
		f"concept-theme:{record.concept_theme_id}",
		f"module:{record.module}",
	]
	if record.needs_human_review:
		labels.append("needs-human-review")
	labels.append("sentinel-auto")
	return labels


def build_theme_labels(record: ThemeIssueRecord) -> list[str]:
	theme_axis = "struct-theme" if record.lens == "struct" else "concept-theme"
	rollup_marker = "is-struct-theme" if record.lens == "struct" else "is-concept-theme"
	return [
		f"severity:{record.aggregate_severity.lower()}",
		f"state:{record.state}",
		f"{theme_axis}:{record.theme_id}",
		rollup_marker,
		"sentinel-auto",
	]


__all__ = ["SAVED_FILTERS", "build_finding_labels", "build_theme_labels"]
