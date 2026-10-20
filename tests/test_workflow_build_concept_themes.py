from pathlib import Path
from uuid import uuid4

from sentinel.workflow.build_concept_themes import (
	build_concept_themes,
	build_concept_themes_from_descriptions,
	load_concept_themes,
	write_concept_themes,
)
from sentinel.workflow.models import ConceptTheme, FindingIssueRecord, ThemeReference


def _make_finding(
	finding_id: str,
	summary: str,
	description: str,
	suggested_fix: str,
	severity: str = "P2",
) -> FindingIssueRecord:
	return FindingIssueRecord(
		triage_item_id=f"TD-{finding_id[-4:]}",
		finding_id=finding_id,
		summary=summary,
		severity=severity,
		skill="layer2a-quick-review",
		module="handlers/wafer_state.py",
		category="error_handling",
		location="handlers/wafer_state.py:147",
		description=description,
		suggested_fix=suggested_fix,
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
		concept_theme_id="",
		root_cause_id="RC-OPTIONAL-001",
		file="handlers/wafer_state.py",
		function="get_wafer_state",
		line_range="147",
		first_seen="scan-2026-05-14",
	)


def test_build_concept_themes_reuses_prior_ids_and_mints_new_singletons() -> None:
	findings = [
		_make_finding(
			"F-000000000001",
			"Unchecked Optional return from get_wafer_state",
			"Unchecked Optional return from get_wafer_state can raise at runtime.",
			"Check for None before dereferencing the result.",
			severity="P1",
		),
		_make_finding(
			"F-000000000002",
			"Unchecked Optional return from read_cached_wafer_state",
			"Unchecked Optional return from read_cached_wafer_state can raise at runtime.",
			"Check for None before dereferencing the result.",
			severity="P2",
		),
		_make_finding(
			"F-000000000003",
			"Missing retry guard for wafer-state polling",
			"Polling retries continue indefinitely without a stop condition.",
			"Add a bounded retry counter and fail closed when retries are exhausted.",
			severity="P1",
		),
	]
	prior_themes = [
		ConceptTheme(
			concept_theme_id="C-007",
			title="Unchecked Optional returns from wafer-state queries",
			root_cause_summary="Optional query results are dereferenced without checking for empty responses.",
			remediation_strategy="Check for None before dereferencing the result.",
			member_finding_ids=["F-000000000001", "F-000000000002"],
			skills_involved={"layer2a-quick-review"},
			categories={"error_handling"},
			files_affected={"handlers/wafer_state.py"},
			aggregate_severity="P1",
			aggregate_effort="medium",
			aggregate_risk_of_deferral="high — likely to cause production issues",
		)
	]

	themes = build_concept_themes(
		findings,
		cluster_assignments={
			"F-000000000001": 0,
			"F-000000000002": 0,
			"F-000000000003": -1,
		},
		previous_themes=prior_themes,
	)

	assert [theme.concept_theme_id for theme in themes] == ["C-007", "C-008"]
	assert themes[0].member_finding_ids == ["F-000000000001", "F-000000000002"]
	assert themes[0].aggregate_severity == "P1"
	assert themes[0].remediation_strategy == "Check for None before dereferencing the result."
	assert themes[1].member_finding_ids == ["F-000000000003"]
	assert themes[1].title == "Missing retry guard for wafer-state polling"


def test_concept_theme_records_round_trip_through_json() -> None:
	theme = ConceptTheme(
		concept_theme_id="C-007",
		title="Unchecked Optional returns from wafer-state queries",
		root_cause_summary="Optional query results are dereferenced without checking for empty responses.",
		remediation_strategy="Check for None before dereferencing the result.",
		member_finding_ids=["F-000000000001", "F-000000000002"],
		skills_involved={"layer2a-quick-review"},
		categories={"error_handling"},
		files_affected={"handlers/wafer_state.py"},
		aggregate_severity="P1",
		aggregate_effort="medium",
		aggregate_risk_of_deferral="high — likely to cause production issues",
	)
	output_path = Path("tests/fixtures/workflow") / f"concept_themes_{uuid4().hex}.json"
	output_path.parent.mkdir(parents=True, exist_ok=True)
	try:
		write_concept_themes(output_path, [theme])
		loaded = load_concept_themes(output_path)

		assert output_path.exists()
		assert len(loaded) == 1
		assert loaded[0].concept_theme_id == "C-007"
		assert loaded[0].member_finding_ids == ["F-000000000001", "F-000000000002"]
	finally:
		if output_path.exists():
			output_path.unlink()


class _FakeEmbedder:
	def __init__(self) -> None:
		self.calls: list[list[str]] = []

	def encode(
		self,
		texts: list[str],
		*,
		convert_to_numpy: bool,
		normalize_embeddings: bool,
		show_progress_bar: bool,
	) -> list[list[float]]:
		self.calls.append(list(texts))
		assert convert_to_numpy is True
		assert normalize_embeddings is True
		assert show_progress_bar is False
		return [
			[0.90, 0.10],
			[0.89, 0.11],
			[0.10, 0.90],
		]


class _FakeClusterer:
	def __init__(self) -> None:
		self.calls: list[list[list[float]]] = []

	def fit_predict(self, embeddings: list[list[float]]) -> list[int]:
		self.calls.append(embeddings)
		return [0, 0, -1]


def test_build_concept_themes_from_descriptions_uses_embedder_and_clusterer() -> None:
	findings = [
		_make_finding(
			"F-000000000001",
			"Unchecked Optional return from get_wafer_state",
			"Unchecked Optional return from get_wafer_state can raise at runtime.",
			"Check for None before dereferencing the result.",
			severity="P1",
		),
		_make_finding(
			"F-000000000002",
			"Unchecked Optional return from read_cached_wafer_state",
			"Unchecked Optional return from read_cached_wafer_state can raise at runtime.",
			"Check for None before dereferencing the result.",
			severity="P2",
		),
		_make_finding(
			"F-000000000003",
			"Missing retry guard for wafer-state polling",
			"Polling retries continue indefinitely without a stop condition.",
			"Add a bounded retry counter and fail closed when retries are exhausted.",
			severity="P1",
		),
	]
	embedder = _FakeEmbedder()
	clusterer = _FakeClusterer()

	themes = build_concept_themes_from_descriptions(
		findings,
		embedder=embedder,
		clusterer=clusterer,
	)

	assert embedder.calls == [[
		"Unchecked Optional return from get_wafer_state can raise at runtime.",
		"Unchecked Optional return from read_cached_wafer_state can raise at runtime.",
		"Polling retries continue indefinitely without a stop condition.",
	]]
	assert clusterer.calls == [[
		[0.90, 0.10],
		[0.89, 0.11],
		[0.10, 0.90],
	]]
	assert [theme.member_finding_ids for theme in themes] == [
		["F-000000000001", "F-000000000002"],
		["F-000000000003"],
	]
