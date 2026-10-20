from sentinel.workflow.issue_renderer import build_finding_issue_title, render_finding_issue_body
from sentinel.workflow.models import FindingIssueRecord, ThemeReference


def _make_finding() -> FindingIssueRecord:
	return FindingIssueRecord(
		triage_item_id="TD-0001",
		finding_id="F-123456789abc",
		summary="Unchecked Optional return from get_wafer_state",
		severity="P1",
		skill="layer2a-quick-review",
		module="handlers/wafer_state.py",
		category="error_handling",
		location="handlers/wafer_state.py:147",
		description="Unchecked Optional return from get_wafer_state can raise at runtime and mask wafer lookup failures.",
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
		needs_human_review=True,
		first_seen="scan-2026-05-14",
	)


def test_build_finding_issue_title_matches_spec() -> None:
	title = build_finding_issue_title(_make_finding())

	assert title == (
		"[P1] layer2a-quick-review: Unchecked Optional return from get_wafer_state "
		"(handlers/wafer_state.py:147)"
	)


def test_render_finding_issue_body_includes_detection_and_triage_sections() -> None:
	body = render_finding_issue_body(_make_finding())

	assert "## Theme" in body
	assert "T-001 — Unchecked Optional returns from wafer-state queries (2 findings in theme)" in body
	assert "https://github.com/chemnteach/sentinel/issues/42" in body
	assert "## Description" in body
	assert "Unchecked Optional return from get_wafer_state can raise at runtime" in body
	assert "## Suggested fix" in body
	assert "Check for None before dereferencing the result." in body
	assert "- **Severity:** P1" in body
	assert "- **Skill:** layer2a-quick-review" in body
	assert "- **Root cause ID:** RC-OPTIONAL-001" in body
	assert "- **Layers (dedup):** layer2a-quick-review, layer3d-error-handling-specialist" in body
	assert "- **Consolidated from:** 2" in body
	assert "- **Lane provenance:** deterministic,retrieval,reconciled" in body
	assert "- **First seen:** scan-2026-05-14" in body
	assert "## Triage state" in body
	assert "- **Needs human review (lane conflict):** true" in body


def test_build_finding_issue_title_falls_back_when_summary_and_description_empty() -> None:
	# When neither summary nor description yields a usable fragment, the title
	# must still carry a non-empty, human-usable placeholder. (FIX-DESIRED-007)
	from dataclasses import replace

	record = replace(_make_finding(), summary="   ", description="   ")
	title = build_finding_issue_title(record)

	assert "(no summary available)" in title
