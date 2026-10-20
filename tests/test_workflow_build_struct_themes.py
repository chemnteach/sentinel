import pytest

from sentinel.workflow.models import FindingIssueRecord, ThemeIssueRecord, ThemeReference
from sentinel.workflow.struct_theme_loader import SemanticGroupRecord, GroupProvenanceRecord
from sentinel.workflow.build_struct_themes import (
	synthesize_struct_theme_title,
	build_struct_theme,
	build_struct_themes,
)


def _make_group(group_id="g0001", grouping_key="security|sql-injection|db.py", members=("F1", "F2")):
	return SemanticGroupRecord(
		group_id=group_id,
		grouping_key=grouping_key,
		mode="registry",
		severity="P2",
		member_count=len(members),
		member_finding_ids=tuple(members),
		contributing_emitters=("layer2c",),
		canonical_emitter="layer2c",
		peer_emitters=("layer2c",),
		subject_key="db.query",
		finding_kind="sql-injection",
		file="db.py",
		function="query",
		category="security",
		provenance=GroupProvenanceRecord(
			mode="registry", grouping_key=grouping_key, subject_key="db.query",
			finding_kind="sql-injection", file="db.py", function="query", category="security",
		),
	)


def _make_finding(
	finding_id,
	summary,
	severity="P2",
	skill="layer2c",
	module="db.py",
	category="security",
	description=None,
	suggested_fix="fix it",
	concept_theme_id="C-001",
	effort_estimate="medium",
	# Producer reads risk_of_deferral via _RISK_RANK; the placeholder "high" is
	# kept for backward-compat with title/aggregate-severity tests where the
	# field is irrelevant. Tests that exercise _RISK_RANK ordering MUST override
	# with a canonical em-dash enum value (see test_build_struct_theme_aggregate_risk_of_deferral).
	risk_of_deferral="high",
	file="",
	line_range="",
):
	return FindingIssueRecord(
		triage_item_id=f"TD-{finding_id}",
		finding_id=finding_id,
		summary=summary,
		severity=severity,
		skill=skill,
		module=module,
		category=category,
		location=f"{module}:1",
		description=f"desc for {finding_id}" if description is None else description,
		suggested_fix=suggested_fix,
		status="triage",
		human_triage=None,
		effort_estimate=effort_estimate,
		risk_of_deferral=risk_of_deferral,
		primary_theme=ThemeReference(lens="struct", theme_id="T-001", title="t", member_count=1),
		struct_theme_id="T-001",
		concept_theme_id=concept_theme_id,
		file=file,
		line_range=line_range,
	)


def test_synthesize_struct_theme_title_with_findings():
	# Common prefix >= 16 chars names the shared pattern (section 6.4).
	group = _make_group(members=("F1", "F2"))
	findings = [
		_make_finding("F1", "Unchecked Optional return in db.query"),
		_make_finding("F2", "Unchecked Optional return in db.fetch"),
	]
	title = synthesize_struct_theme_title(group, findings)
	assert title == "Unchecked Optional return in db"


def test_synthesize_struct_theme_title_single_member():
	group = _make_group(members=("F1",))
	findings = [_make_finding("F1", "SQL injection in query builder")]
	assert synthesize_struct_theme_title(group, findings) == "SQL injection in query builder"


def test_synthesize_struct_theme_title_no_common_pattern():
	# Disparate summaries, short common prefix -> shortest summary representative.
	group = _make_group(members=("F1", "F2"))
	findings = [
		_make_finding("F1", "Race condition in cache"),
		_make_finding("F2", "Buffer overflow risk"),
	]
	assert synthesize_struct_theme_title(group, findings) == "Buffer overflow risk"


def test_synthesize_struct_theme_title_empty_members():
	group = _make_group(grouping_key="gk|abc", members=())
	assert synthesize_struct_theme_title(group, []) == "Structural group: gk|abc"


def test_build_struct_theme_field_mapping():
	group = _make_group(members=("F1", "F2"))
	findings = [
		_make_finding("F1", "Unchecked Optional A", severity="P2", skill="layer2c", module="db.py", category="security"),
		_make_finding("F2", "Unchecked Optional B", severity="P1", skill="layer3d", module="cache.py", category="correctness"),
	]
	theme = build_struct_theme(group, findings)
	assert isinstance(theme, ThemeIssueRecord)
	assert theme.lens == "struct"
	assert theme.theme_id == "g0001"
	assert theme.source_id == "security|sql-injection|db.py"
	assert theme.member_finding_ids == ["F1", "F2"]
	# aggregate_severity = max across members (P1 outranks P2).
	assert theme.aggregate_severity == "P1"
	# categories = union of member categories + group.category.
	assert theme.categories == {"security", "correctness"}
	# skills = union of member skills.
	assert theme.skills_involved == {"layer2c", "layer3d"}
	# modules = union of member modules (real module field, not file approximation).
	assert theme.modules_affected == {"db.py", "cache.py"}
	# Cycle-15 item 15: populated by build_struct_theme from member findings.
	# Both findings share suggested_fix="fix it" (default) -> modal remediation.
	assert theme.remediation_strategy == "fix it"
	# Descriptions "desc for F1" / "desc for F2" -> first-two joined with period.
	assert theme.root_cause_summary == "desc for F1. desc for F2."
	# Members list: one ThemeMember per finding, preserving input order.
	assert len(theme.members) == 2
	assert theme.members[0].finding_id == "F1"
	assert theme.members[1].finding_id == "F2"


def test_build_struct_theme_empty_members_fallback():
	group = _make_group(members=("F1",))
	theme = build_struct_theme(group, [])
	# Falls back to SemanticGroup-only values (cycle-9 adapter behavior).
	assert theme.title == "Structural group: security|sql-injection|db.py"
	assert theme.aggregate_severity == "P2"  # group.severity
	assert theme.categories == {"security"}  # {group.category}
	assert theme.skills_involved == {"layer2c"}  # contributing_emitters
	assert theme.modules_affected == {"db.py"}  # {group.file}


def test_build_struct_themes_happy_path():
	groups = [_make_group("g0001", members=("F1", "F2")), _make_group("g0002", grouping_key="gk2", members=("F3",))]
	findings = [
		_make_finding("F1", "pattern one alpha"),
		_make_finding("F2", "pattern one beta"),
		_make_finding("F3", "pattern two"),
	]
	themes = build_struct_themes(groups, findings)
	assert len(themes) == 2
	assert all(isinstance(t, ThemeIssueRecord) for t in themes)
	assert all(t.lens == "struct" for t in themes)
	assert {t.theme_id for t in themes} == {"g0001", "g0002"}


def test_build_struct_themes_missing_member():
	# F99 referenced by the group but absent from findings -> dropped, logged.
	group = _make_group(members=("F1", "F99"))
	findings = [_make_finding("F1", "the only present finding")]
	themes = build_struct_themes([group], findings)
	assert len(themes) == 1
	# Only F1 contributed to synthesis (single present member -> its summary).
	assert themes[0].title == "the only present finding"
	# Contract (P1 wave-3 fix): member_finding_ids emits only the resolved ids;
	# the unresolved F99 is dropped so the theme does not misreport membership.
	# The drop is surfaced via a WARNING (see test_workflow_p1_wave3).
	assert themes[0].member_finding_ids == ["F1"]


def test_build_struct_themes_empty_groups():
	assert build_struct_themes([], [_make_finding("F1", "x")]) == ()


def test_build_struct_themes_empty_findings():
	# Groups present, findings empty -> every group via empty-member fallback.
	groups = [_make_group("g0001", members=("F1",))]
	themes = build_struct_themes(groups, [])
	assert len(themes) == 1
	assert themes[0].title == "Structural group: security|sql-injection|db.py"
	assert themes[0].aggregate_severity == "P2"


# ----- Cycle-15 item 15: section-6.5 GitHub issue-body field synthesis -----


def test_build_struct_theme_root_cause_summary_multi_member():
	# Two distinct descriptions -> "first. second." (per-helper rstrip(".") + join).
	group = _make_group(members=("F1", "F2"))
	findings = [
		_make_finding("F1", "sA", description="cause one."),
		_make_finding("F2", "sB", description="cause two."),
	]
	theme = build_struct_theme(group, findings)
	assert theme.root_cause_summary == "cause one. cause two."


def test_build_struct_theme_root_cause_summary_single_member():
	# Single finding -> verbatim description.
	group = _make_group(members=("F1",))
	findings = [_make_finding("F1", "sA", description="only cause")]
	theme = build_struct_theme(group, findings)
	assert theme.root_cause_summary == "only cause"


def test_build_struct_theme_root_cause_summary_empty_descriptions():
	# All descriptions empty -> "".
	group = _make_group(members=("F1", "F2"))
	findings = [
		_make_finding("F1", "sA", description=""),
		_make_finding("F2", "sB", description=""),
	]
	theme = build_struct_theme(group, findings)
	assert theme.root_cause_summary == ""


def test_build_struct_theme_remediation_strategy_modal_fix():
	# "use parameterized query" appears twice; "use ORM" once -> modal wins.
	group = _make_group(members=("F1", "F2", "F3"))
	findings = [
		_make_finding("F1", "sA", suggested_fix="use parameterized query"),
		_make_finding("F2", "sB", suggested_fix="use parameterized query"),
		_make_finding("F3", "sC", suggested_fix="use ORM"),
	]
	theme = build_struct_theme(group, findings)
	assert theme.remediation_strategy == "use parameterized query"


def test_build_struct_theme_remediation_strategy_tiebreak():
	# Tiebreak key: max(counts.items(), key=lambda i: (i[1], -len(i[0]), i[0]))[0].
	# Fixture: three fixes each appearing once.
	#   "a"   -> (1, -1, "a")
	#   "bbb" -> (1, -3, "bbb")
	#   "cc"  -> (1, -2, "cc")
	# Max tuple is (1, -1, "a") -> the SHORTEST fix wins on length tiebreak.
	# This fixture is unambiguous: lengths 1/2/3 are all distinct, so the
	# alphabetical sub-tiebreak (item[0]) is never reached.
	group = _make_group(members=("F1", "F2", "F3"))
	findings = [
		_make_finding("F1", "sA", suggested_fix="a"),
		_make_finding("F2", "sB", suggested_fix="bbb"),
		_make_finding("F3", "sC", suggested_fix="cc"),
	]
	theme = build_struct_theme(group, findings)
	assert theme.remediation_strategy == "a"


def test_build_struct_theme_members_field_mapping():
	# Verify all seven ThemeMember fields map correctly, including:
	#   - file_path uses finding.file when present (not module fallback)
	#   - other_lens_theme_id == finding.concept_theme_id
	group = _make_group(members=("F1",))
	findings = [
		_make_finding(
			"F1", "row summary text",
			severity="P1", skill="layer3d", module="cache.py",
			concept_theme_id="C-042", file="cache/lru.py", line_range="100-120",
		),
	]
	theme = build_struct_theme(group, findings)
	assert len(theme.members) == 1
	m = theme.members[0]
	assert m.finding_id == "F1"
	assert m.severity == "P1"
	assert m.file_path == "cache/lru.py"  # file takes precedence over module
	assert m.line_range == "100-120"
	assert m.skill == "layer3d"
	assert m.other_lens_theme_id == "C-042"
	assert m.summary == "row summary text"
	# When finding.file is empty, file_path falls back to module.
	group2 = _make_group(members=("F2",))
	findings2 = [_make_finding("F2", "x", module="cache.py", file="")]
	theme2 = build_struct_theme(group2, findings2)
	assert theme2.members[0].file_path == "cache.py"


def test_build_struct_theme_members_preserve_finding_order():
	# Members list must mirror member_findings input order (no implicit sort).
	group = _make_group(members=("F3", "F1", "F2"))
	findings = [
		_make_finding("F3", "third"),
		_make_finding("F1", "first"),
		_make_finding("F2", "second"),
	]
	theme = build_struct_theme(group, findings)
	assert [m.finding_id for m in theme.members] == ["F3", "F1", "F2"]


def test_build_struct_theme_aggregate_effort():
	# Mix of "low", "medium", "high" -> "high" wins (lowest _EFFORT_RANK).
	group = _make_group(members=("F1", "F2", "F3"))
	findings = [
		_make_finding("F1", "sA", effort_estimate="low"),
		_make_finding("F2", "sB", effort_estimate="medium"),
		_make_finding("F3", "sC", effort_estimate="high"),
	]
	theme = build_struct_theme(group, findings)
	assert theme.aggregate_effort == "high"


def test_build_struct_theme_aggregate_risk_of_deferral():
	# Must use canonical _RISK_RANK enum strings (em-dash separator).
	# Strings used (verbatim from build_struct_themes._RISK_RANK):
	#   "medium — technical debt accumulates"            (rank 2)
	#   "high — likely to cause production issues"       (rank 1)
	#   "low — cosmetic or nice-to-have"                 (rank 3)
	# Lowest rank wins -> "high — likely to cause production issues".
	group = _make_group(members=("F1", "F2", "F3"))
	findings = [
		_make_finding("F1", "sA", risk_of_deferral="medium — technical debt accumulates"),
		_make_finding("F2", "sB", risk_of_deferral="high — likely to cause production issues"),
		_make_finding("F3", "sC", risk_of_deferral="low — cosmetic or nice-to-have"),
	]
	theme = build_struct_theme(group, findings)
	assert theme.aggregate_risk_of_deferral == "high — likely to cause production issues"


def test_build_struct_theme_empty_members_synthesis_fallback():
	# Empty member_findings -> all five new fields revert to dataclass defaults.
	group = _make_group(members=("F1",))
	theme = build_struct_theme(group, [])
	assert theme.root_cause_summary == ""
	assert theme.remediation_strategy == ""
	assert theme.members == []
	assert theme.aggregate_effort == "low"
	assert theme.aggregate_risk_of_deferral == ""
