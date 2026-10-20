from sentinel.workflow.emit_to_github import (
	ThemeSupersedePlan,
	plan_finding_issue_upsert,
	plan_theme_issue_upsert,
	plan_theme_supersede,
	sync_finding_issue,
	sync_theme_issue,
)
from sentinel.workflow.models import IssueClosurePlan
from sentinel.workflow.issue_renderer import build_finding_issue_title, render_finding_issue_body
from sentinel.workflow.label_taxonomy import build_finding_labels, build_theme_labels
from sentinel.workflow.models import (
	CrossLensReference,
	FindingIssueRecord,
	GitHubIssueSnapshot,
	ThemeIssueRecord,
	ThemeMember,
	ThemeReference,
)
from sentinel.workflow.theme_renderer import render_theme_issue_body


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
			)
		],
		state="owner-assessed",
	)


def test_plan_finding_issue_upsert_skips_when_existing_issue_matches() -> None:
	finding = _make_finding()
	existing_issue = GitHubIssueSnapshot(
		issue_number=101,
		title=build_finding_issue_title(finding),
		body=render_finding_issue_body(finding),
		labels=tuple(build_finding_labels(finding)),
		html_url="https://github.com/chemnteach/sentinel/issues/101",
	)

	result = plan_finding_issue_upsert(finding, existing_issue=existing_issue)

	assert result.upsert_plan.action == "skip"
	assert result.upsert_plan.issue_number == 101
	assert result.drift_report is None


def test_plan_theme_issue_upsert_updates_when_existing_issue_drifts() -> None:
	theme = _make_theme()
	existing_issue = GitHubIssueSnapshot(
		issue_number=42,
		title="Old theme title",
		body=render_theme_issue_body(theme),
		labels=("severity:p1", "state:triage", "struct-theme:T-001"),
		html_url="https://github.com/chemnteach/sentinel/issues/42",
	)

	plan = plan_theme_issue_upsert(theme, existing_issue=existing_issue)

	assert plan.action == "update"
	assert plan.issue_number == 42
	assert plan.title == "[Theme][T-001] Unchecked Optional returns from wafer-state queries"
	assert plan.labels == build_theme_labels(theme)


class _FakeGitHubClient:
	def __init__(self) -> None:
		self.finding_issue: GitHubIssueSnapshot | None = None
		self.theme_issue: GitHubIssueSnapshot | None = None
		self.created: list[tuple[str, str, list[str]]] = []
		self.updated: list[tuple[int, str, str, list[str]]] = []

	def find_finding_issue(self, finding_id: str) -> GitHubIssueSnapshot | None:
		assert finding_id == "F-123456789abc"
		return self.finding_issue

	def find_theme_issue(self, theme_id: str, lens: str) -> GitHubIssueSnapshot | None:
		assert theme_id == "T-001"
		assert lens == "struct"
		return self.theme_issue

	def create_issue(self, title: str, body: str, labels: list[str]) -> GitHubIssueSnapshot:
		self.created.append((title, body, labels))
		return GitHubIssueSnapshot(
			issue_number=201,
			title=title,
			body=body,
			labels=tuple(labels),
			html_url="https://github.com/chemnteach/sentinel/issues/201",
		)

	def update_issue(self, issue_number: int, title: str, body: str, labels: list[str]) -> GitHubIssueSnapshot:
		self.updated.append((issue_number, title, body, labels))
		return GitHubIssueSnapshot(
			issue_number=issue_number,
			title=title,
			body=body,
			labels=tuple(labels),
			html_url=f"https://github.com/chemnteach/sentinel/issues/{issue_number}",
		)


def test_sync_finding_issue_creates_when_missing() -> None:
	client = _FakeGitHubClient()

	snapshot = sync_finding_issue(client, _make_finding())

	assert snapshot.issue_number == 201
	assert len(client.created) == 1
	assert client.updated == []


def test_sync_theme_issue_updates_when_existing_issue_drifts() -> None:
	client = _FakeGitHubClient()
	client.theme_issue = GitHubIssueSnapshot(
		issue_number=42,
		title="Old theme title",
		body="outdated body",
		labels=("severity:p1",),
		html_url="https://github.com/chemnteach/sentinel/issues/42",
	)

	snapshot = sync_theme_issue(client, _make_theme())

	assert snapshot.issue_number == 42
	assert client.created == []
	assert len(client.updated) == 1



def test_plan_theme_supersede_emits_closure_plan_and_comment_with_markdown_link() -> None:
	plan = plan_theme_supersede(
		prior_theme_id='C-007',
		successor_theme_id='C-042',
		prior_issue_number=100,
		successor_issue_number=200,
		successor_issue_url='https://github.com/chemnteach/sentinel/issues/200',
	)

	assert isinstance(plan, ThemeSupersedePlan)
	assert plan.prior_theme_id == 'C-007'
	assert plan.successor_theme_id == 'C-042'
	assert plan.prior_issue_number == 100
	assert plan.closure_plan == IssueClosurePlan(
		issue_number=100,
		reason='superseded',
		successor_issue_number=200,
	)
	assert '[C-042 (#200)](https://github.com/chemnteach/sentinel/issues/200)' in plan.comment_body
	assert 'superseded' in plan.comment_body
	assert 'section 6.2' in plan.comment_body


def test_plan_theme_supersede_without_url_falls_back_to_shorthand_link() -> None:
	plan = plan_theme_supersede(
		prior_theme_id='C-100',
		successor_theme_id='C-200',
		prior_issue_number=1,
		successor_issue_number=2,
	)

	assert 'C-200 (#2)' in plan.comment_body
	assert '](' not in plan.comment_body
	assert plan.closure_plan.successor_issue_number == 2


def test_plan_theme_supersede_closure_plan_uses_superseded_reason() -> None:
	plan = plan_theme_supersede(
		prior_theme_id='C-1',
		successor_theme_id='C-2',
		prior_issue_number=10,
		successor_issue_number=20,
	)
	assert plan.closure_plan.reason == 'superseded'
	data = plan.to_dict()
	assert data['closure_plan']['reason'] == 'superseded'
	assert data['closure_plan']['successor_issue_number'] == 20
	assert data['prior_theme_id'] == 'C-1'
	assert data['successor_theme_id'] == 'C-2'


def test_plan_theme_supersede_result_is_frozen() -> None:
	import dataclasses
	import pytest

	plan = plan_theme_supersede(
		prior_theme_id='C-1',
		successor_theme_id='C-2',
		prior_issue_number=10,
		successor_issue_number=20,
	)
	with pytest.raises(dataclasses.FrozenInstanceError):
		plan.prior_issue_number = 99  # type: ignore[misc]


def test_plan_finding_issue_upsert_uses_config_override_label() -> None:
	from sentinel.workflow.config import (
		GitHubConfig,
		ProfileConfig,
		RemediationConfig,
		ThemesConfig,
		WorkflowConfig,
	)
	config = WorkflowConfig(
		github=GitHubConfig(owner="o", repo="r"),
		themes=ThemesConfig(),
		remediation=RemediationConfig(),
		profile=ProfileConfig(name="owner_gate", version="0.8.0", override_label="operator-locked"),
	)
	finding = _make_finding()
	existing_issue = GitHubIssueSnapshot(
		issue_number=701,
		title="anything",
		body="anything",
		labels=("operator-locked", "sentinel-auto"),
		html_url="https://example/issues/701",
	)
	result = plan_finding_issue_upsert(finding, existing_issue=existing_issue, config=config)
	assert result.upsert_plan.action == "skip"
	assert result.upsert_plan.issue_number == 701
	assert result.drift_report is not None
	assert result.drift_report.drift_kind == "skipped-upsert"
	assert result.drift_report.override_label_present is True
