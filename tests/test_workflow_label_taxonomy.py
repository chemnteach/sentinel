from sentinel.workflow.label_taxonomy import SAVED_FILTERS, build_finding_labels, build_theme_labels
from sentinel.workflow.models import (
	CrossLensReference,
	FindingIssueRecord,
	ThemeIssueRecord,
	ThemeMember,
	ThemeReference,
)


def _make_finding(*, needs_human_review: bool = False) -> FindingIssueRecord:
	return FindingIssueRecord(
		triage_item_id="TD-0001",
		finding_id="F-123456789abc",
		summary="Unchecked Optional return from get_wafer_state",
		severity="P1",
		skill="layer2a-quick-review",
		module="handlers/wafer_state.py",
		category="error_handling",
		location="handlers/wafer_state.py:147",
		description="Unchecked Optional return from get_wafer_state can raise at runtime.",
		suggested_fix="Check for None before dereferencing the result.",
		status="triage",
		human_triage=None,
		effort_estimate="medium",
		risk_of_deferral="high — likely to cause production issues",
		primary_theme=ThemeReference(
			lens="struct",
			theme_id="T-001",
			title="Unchecked Optional returns from wafer-state queries",
			member_count=2,
			issue_url="https://github.com/chemnteach/sentinel/issues/42",
		),
		struct_theme_id="T-001",
		concept_theme_id="C-007",
		root_cause_id="RC-OPTIONAL-001",
		file="handlers/wafer_state.py",
		function="get_wafer_state",
		line_range="147",
		layers=("layer2a-quick-review", "layer3d-error-handling-specialist"),
		consolidated_from=2,
		lane_provenance="deterministic,retrieval,reconciled",
		needs_human_review=needs_human_review,
		first_seen="scan-2026-05-14",
	)


def _make_theme() -> ThemeIssueRecord:
	return ThemeIssueRecord(
		lens="struct",
		theme_id="T-001",
		source_id="RC-OPTIONAL-001",
		title="Unchecked Optional returns from wafer-state queries",
		member_finding_ids=["F-123456789abc", "F-fedcba987654"],
		skills_involved={"layer2a-quick-review", "layer3d-error-handling-specialist"},
		categories={"error_handling"},
		modules_affected={"handlers/wafer_state.py", "queries/wafer_repo.py"},
		aggregate_severity="P1",
		aggregate_effort="medium",
		aggregate_risk_of_deferral="high — likely to cause production issues",
		cross_lens_themes=[
			CrossLensReference(
				theme_id="C-007",
				title="Defensive null handling omissions",
				issue_url="https://github.com/chemnteach/sentinel/issues/8",
			)
		],
		root_cause_summary="Multiple call sites dereference Optional query results without checking for empty responses.",
		remediation_strategy="Add explicit Optional guards and a shared helper for null-safe wafer-state access.",
		members=[
			ThemeMember(
				finding_id="F-123456789abc",
				severity="P1",
				file_path="handlers/wafer_state.py",
				line_range="147",
				skill="layer2a-quick-review",
				other_lens_theme_id="C-007",
				summary="Unchecked Optional return from get_wafer_state",
			)
		],
		state="owner-assessed",
	)


def test_build_finding_labels_includes_all_axes_and_flags() -> None:
	labels = build_finding_labels(_make_finding(needs_human_review=True))

	assert labels == [
		"severity:p1",
		"state:triage",
		"skill:layer2a-quick-review",
		"category:error_handling",
		"struct-theme:T-001",
		"concept-theme:C-007",
		"module:handlers/wafer_state.py",
		"needs-human-review",
		"sentinel-auto",
	]


def test_build_theme_labels_marks_structural_rollups() -> None:
	labels = build_theme_labels(_make_theme())

	assert labels == [
		"severity:p1",
		"state:owner-assessed",
		"struct-theme:T-001",
		"is-struct-theme",
		"sentinel-auto",
	]


def test_saved_filters_match_workflow_spec() -> None:
	assert SAVED_FILTERS["code_owner_triage_queue"] == "state:triage sort:severity"
	assert SAVED_FILTERS["owner_structural_scope"] == "is-struct-theme state:owner-assessed sort:severity"
	assert SAVED_FILTERS["audit_lane_conflicts"] == "needs-human-review"
