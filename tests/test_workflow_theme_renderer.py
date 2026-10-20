from sentinel.workflow.models import CrossLensReference, ThemeIssueRecord, ThemeMember
from sentinel.workflow.theme_renderer import render_theme_issue_body


def _make_theme() -> ThemeIssueRecord:
	return ThemeIssueRecord(
		lens="struct",
		theme_id="T-001",
		source_id="RC-OPTIONAL-001",
		title="Unchecked Optional returns from wafer-state queries",
		member_finding_ids=["F-123456789abc", "F-fedcba987654"],
		skills_involved={"layer2a-quick-review", "layer3d-error-handling-specialist"},
		categories={"error_handling", "correctness"},
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
			),
			ThemeMember(
				finding_id="F-fedcba987654",
				severity="P2",
				file_path="queries/wafer_repo.py",
				line_range="88-90",
				skill="layer3d-error-handling-specialist",
				other_lens_theme_id="C-007",
				summary="Repository lookup omits null-safe branch",
			),
		],
		state="owner-assessed",
	)


def test_render_theme_issue_body_includes_cross_lens_context_and_members() -> None:
	body = render_theme_issue_body(_make_theme())

	assert "## Structural Theme T-001: Unchecked Optional returns from wafer-state queries" in body
	assert "**Lens:** Structural (root_cause_id)" in body
	assert "**Source ID:** RC-OPTIONAL-001" in body
	assert "**Members:** 2 findings" in body
	assert "**Skills involved:** layer2a-quick-review, layer3d-error-handling-specialist" in body
	assert "**Categories:** correctness, error_handling" in body
	assert "**Modules / files affected:** handlers/wafer_state.py, queries/wafer_repo.py" in body
	assert "## Cross-lens view" in body
	assert "https://github.com/chemnteach/sentinel/issues/8" in body
	assert "## Root cause summary" in body
	assert "## Unified remediation strategy" in body
	assert "## Member findings" in body
	assert "| P1 | handlers/wafer_state.py:147 | layer2a-quick-review | C-007 | Unchecked Optional return from get_wafer_state |" in body
	assert "| P2 | queries/wafer_repo.py:88-90 | layer3d-error-handling-specialist | C-007 | Repository lookup omits null-safe branch |" in body
