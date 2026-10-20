import pytest
from sentinel.workflow.dashboard.contract import (
	DashboardBundle, SeverityRiskRow, LaneConflictRow, HumanOverrideRow, ThemeSummaryRow, WorkflowStateRow
)
from sentinel.workflow.dashboard.contract import ModuleHotspotRow, RunConfidence, AuditFreshnessSummary, LensDivergence
from sentinel.workflow.dashboard.build_dashboard import (
	build_dashboard_bundle, build_severity_risk_summary, build_lane_conflict_summary,
	build_human_override_summary, build_theme_summary, build_workflow_state_summary,
	build_module_hotspot_summary, build_run_confidence, build_audit_freshness_summary,
	build_lens_divergence, BundleInput
)
from sentinel.workflow.config import (
	_OWNER_GATE_STATE_LABELS, WorkflowConfig, ProfileConfig, GitHubConfig, ThemesConfig, RemediationConfig
)


# Stub mirroring sentinel/workflow/models.py GitHubIssueSnapshot. Field names match the model exactly. Update when the model changes.
class DummyIssue:
	def __init__(self, finding_id, issue_number=0, labels=(), title="", body=None, html_url=None):
		self.issue_number = issue_number
		self.title = title
		self.body = body or f"**Finding ID:** {finding_id}"
		self.labels = labels
		self.html_url = html_url


# Stub mirroring sentinel/workflow/models.py ThemeIssueRecord. Field names match the model exactly. Update when the model changes.
class DummyTheme:
	def __init__(self, theme_id, title="", member_finding_ids=(), aggregate_severity="P3",
			skills_involved=(), categories=(), modules_affected=(), state="triage"):
		self.theme_id = theme_id
		self.title = title
		self.member_finding_ids = list(member_finding_ids)
		self.aggregate_severity = aggregate_severity
		self.skills_involved = set(skills_involved)
		self.categories = set(categories)
		self.modules_affected = set(modules_affected)
		self.state = state


def _make_config(state_labels):
	# Minimal WorkflowConfig with a custom profile state vocabulary for OOV tests.
	return WorkflowConfig(
		github=GitHubConfig(owner="o", repo="r"),
		themes=ThemesConfig(),
		remediation=RemediationConfig(),
		profile=ProfileConfig(name="custom", version="1.0", state_labels=frozenset(state_labels)),
	)


@pytest.fixture
def synthetic_findings():
	# Stub mirroring sentinel/workflow/models.py FindingIssueRecord. Field names match the model exactly. Update when the model changes.
	class Rec:
		def __init__(self, finding_id, severity, status, needs_human_review=False, struct_theme_id="", concept_theme_id="", module=""):
			self.finding_id = finding_id
			self.severity = severity
			self.status = status
			self.needs_human_review = needs_human_review
			self.struct_theme_id = struct_theme_id
			self.concept_theme_id = concept_theme_id
			self.module = module
	return [
		Rec("F1", "P0", "Open", needs_human_review=True),
		Rec("F2", "P1", "Closed"),
		Rec("F3", "P2", "Open", needs_human_review=True),
		Rec("F4", "P1", "In Review"),
		Rec("F5", "P0", "Closed"),
	]


@pytest.fixture
def synthetic_state_findings():
	# Minimal stub: build_workflow_state_summary reads only FindingIssueRecord.status.
	class StateRec:
		def __init__(self, status):
			self.status = status
	return [
		StateRec("triage"),
		StateRec("triage"),
		StateRec("closed"),
		StateRec("remediating"),
		StateRec("in-review"),
	]


@pytest.fixture
def synthetic_module_findings():
	# Stub for module/hotspot ranking. build_module_hotspot_summary reads only
	# module + severity, but the same fixture is also fed to the full
	# build_dashboard_bundle, whose other builders read status (severity_risk,
	# workflow_state) and finding_id (human_override builds a finding_map). The
	# complete set of non-getattr finding attributes the bundle reads is
	# finding_id, module, severity, status; all four are carried here so the
	# stub is bundle-compatible. finding_id and status are irrelevant to module
	# ranking and present only for that compatibility.
	class ModRec:
		def __init__(self, finding_id, module, severity, status="Open"):
			self.finding_id = finding_id
			self.module = module
			self.severity = severity
			self.status = status
	# mod_a: 3 (2xP0, 1xP1); mod_b: 3 (1xP1, 2xP2); mod_c: 1 (P3).
	# mod_a and mod_b tie on count (3) -> alphabetical tiebreaker puts mod_a first.
	return [
		ModRec("M1", "mod_a", "P0"),
		ModRec("M2", "mod_a", "P0"),
		ModRec("M3", "mod_a", "P1"),
		ModRec("M4", "mod_b", "P1"),
		ModRec("M5", "mod_b", "P2"),
		ModRec("M6", "mod_b", "P2"),
		ModRec("M7", "mod_c", "P3"),
	]


@pytest.fixture
def synthetic_scorecard():
	# Dict mirroring the orchestrator-track quality_scorecard.json shape
	# (sentinel/orchestrator/quality_artifacts.py build_quality_scorecard).
	return {
		"confidence_label": "high",
		"metrics": {
			"module_completeness_pct": 100.0,
			"lane_agreement_pct": 98.0,
			"unresolved_critical_conflicts": 0,
		},
		"gates": {
			"review_completeness": "pass",
			"lane_conflict_resolution": "pass",
			"confidence_floor": "pass",
		},
		"remediation_guidance": ["should be excluded from supporting_evidence"],
	}


@pytest.fixture
def synthetic_issues():
	return [
		DummyIssue("F1", issue_number=101, labels=("human-override",)),
		DummyIssue("F2", issue_number=102),
		DummyIssue("F3", issue_number=103, labels=("human-override",)),
		DummyIssue("F4", issue_number=104),
		DummyIssue("F5", issue_number=105),
	]


@pytest.fixture
def synthetic_struct_themes():
	# Deliberately out of theme_id order to exercise the sort.
	return [
		DummyTheme("S-002", title="Struct two", member_finding_ids=["F3", "F4"], aggregate_severity="P1",
			skills_involved={"correctness"}, categories={"logic"}, modules_affected={"mod_b"}, state="triage"),
		DummyTheme("S-001", title="Struct one", member_finding_ids=["F1", "F2"], aggregate_severity="P0",
			skills_involved={"security"}, categories={"injection"}, modules_affected={"mod_a"}, state="owner-assessed"),
	]


@pytest.fixture
def synthetic_concept_themes():
	# Deliberately out of theme_id order to exercise the sort.
	return [
		DummyTheme("C-002", title="Concept two", member_finding_ids=["F5"], aggregate_severity="P2",
			skills_involved={"maintainability"}, categories={"smell"}, modules_affected={"mod_c"}, state="triage"),
		DummyTheme("C-001", title="Concept one", member_finding_ids=["F1", "F3", "F4"], aggregate_severity="P0",
			skills_involved={"security", "correctness"}, categories={"injection"}, modules_affected={"mod_a", "mod_b"}, state="remediating"),
	]


def test_severity_risk_summary(synthetic_findings):
	rows = build_severity_risk_summary(synthetic_findings)
	assert any(r.severity == "P0" and r.states["Open"] == 1 for r in rows)
	assert any(r.severity == "P1" and r.states["Closed"] == 1 for r in rows)
	assert all(isinstance(r, SeverityRiskRow) for r in rows)


def test_lane_conflict_summary(synthetic_findings):
	rows = build_lane_conflict_summary(synthetic_findings)
	assert len(rows) == 2
	assert all(isinstance(r, LaneConflictRow) for r in rows)
	assert all(r.needs_human_review is True for r in rows)
	assert {r.finding_id for r in rows} == {"F1", "F3"}


def test_human_override_summary(synthetic_findings, synthetic_issues):
	rows = build_human_override_summary(synthetic_findings, synthetic_issues)
	# Spec section 9.4-6 scopes the section to overridden records: only the
	# two override-labeled issues (F1, F3) surface, not all five findings.
	assert len(rows) == 2
	assert all(isinstance(r, HumanOverrideRow) for r in rows)
	assert {r.finding_id for r in rows} == {"F1", "F3"}
	assert {r.issue_number for r in rows} == {101, 103}
	assert all(r.override_label_present is True for r in rows)
	# Override without field drift: synthetic issues carry no severity:/state:
	# labels, so detect_drift finds no disagreement -> empty drift_details.
	assert all(r.drift_kind == "" for r in rows)
	assert all(r.drift_details == () for r in rows)


def test_theme_summary(synthetic_struct_themes, synthetic_concept_themes):
	rows = build_theme_summary(synthetic_struct_themes, synthetic_concept_themes)
	assert len(rows) == 4
	assert all(isinstance(r, ThemeSummaryRow) for r in rows)
	# Spec section 9.4-2: one lens-tagged section, struct first (by theme_id),
	# then concept (by theme_id).
	assert [r.theme_id for r in rows] == ["S-001", "S-002", "C-001", "C-002"]
	assert [r.lens for r in rows] == ["struct", "struct", "concept", "concept"]
	# Field mapping spot-check on the first struct row (S-001).
	s001 = rows[0]
	assert s001.title == "Struct one"
	assert s001.member_count == 2
	assert s001.aggregate_severity == "P0"
	assert s001.skills_involved == ["security"]
	assert s001.categories == ["injection"]
	assert s001.modules_affected == ["mod_a"]
	assert s001.state == "owner-assessed"
	# Multi-value fields are sorted (C-001 spans two skills/modules).
	c001 = next(r for r in rows if r.theme_id == "C-001")
	assert c001.member_count == 3
	assert c001.skills_involved == ["correctness", "security"]
	assert c001.modules_affected == ["mod_a", "mod_b"]


def test_workflow_state_summary_default(synthetic_state_findings):
	rows = build_workflow_state_summary(synthetic_state_findings)
	by_state = {r.state: r.count for r in rows}
	# Every configured owner_gate state is present (count zero when absent).
	for s in _OWNER_GATE_STATE_LABELS:
		assert s in by_state
	assert by_state["triage"] == 2
	assert by_state["closed"] == 1
	assert by_state["remediating"] == 1
	assert by_state["owner-assessed"] == 0
	# OOV state surfaces (in-review is not in the owner_gate vocabulary).
	assert by_state["in-review"] == 1
	# Configured group precedes the OOV group: the lone OOV state lands
	# immediately after all configured states.
	states = [r.state for r in rows]
	assert states.index("in-review") == len(_OWNER_GATE_STATE_LABELS)


def test_workflow_state_summary_with_config(synthetic_state_findings):
	config = _make_config({"triage", "closed"})
	rows = build_workflow_state_summary(synthetic_state_findings, config=config)
	by_state = {r.state: r.count for r in rows}
	# Only the two configured states are guaranteed present from the vocabulary.
	assert by_state["triage"] == 2
	assert by_state["closed"] == 1
	# owner_gate-only states are NOT configured here, so they do not appear
	# unless findings carry them.
	assert "owner-assessed" not in by_state
	assert "pr-open" not in by_state
	# remediating and in-review are OOV relative to {triage, closed} and
	# surface because findings carry them.
	assert by_state["remediating"] == 1
	assert by_state["in-review"] == 1


def test_workflow_state_summary_oov_sort(synthetic_state_findings):
	config = _make_config({"triage", "closed"})
	rows = build_workflow_state_summary(synthetic_state_findings, config=config)
	# Two-group sort: configured states alphabetical first (closed < triage),
	# then OOV states alphabetical (in-review < remediating). This is distinct
	# from a global alphabetical sort, which would place "in-review" before
	# "triage".
	assert [r.state for r in rows] == ["closed", "triage", "in-review", "remediating"]


def test_module_hotspot_summary(synthetic_module_findings):
	rows = build_module_hotspot_summary(synthetic_module_findings)
	assert all(isinstance(r, ModuleHotspotRow) for r in rows)
	assert len(rows) == 3
	# Sort: count descending, module name alphabetical tiebreaker. mod_a and
	# mod_b both have 3 findings -> mod_a first (alpha); mod_c (1) last.
	assert [r.module for r in rows] == ["mod_a", "mod_b", "mod_c"]
	assert [r.open_finding_count for r in rows] == [3, 3, 1]
	# severity_mix per module.
	by_module = {r.module: r.severity_mix for r in rows}
	assert by_module["mod_a"] == {"P0": 2, "P1": 1}
	assert by_module["mod_b"] == {"P1": 1, "P2": 2}
	assert by_module["mod_c"] == {"P3": 1}


def test_module_hotspot_summary_empty():
	assert build_module_hotspot_summary([]) == ()


def test_run_confidence_populated(synthetic_scorecard):
	rc = build_run_confidence(synthetic_scorecard)
	assert isinstance(rc, RunConfidence)
	assert rc.confidence_label == "high"
	# supporting_evidence carries metrics + gates only; remediation_guidance excluded.
	assert set(rc.supporting_evidence.keys()) == {"metrics", "gates"}
	assert rc.supporting_evidence["metrics"]["lane_agreement_pct"] == 98.0
	assert rc.supporting_evidence["gates"]["confidence_floor"] == "pass"
	assert "remediation_guidance" not in rc.supporting_evidence


def test_run_confidence_absent():
	rc = build_run_confidence(None)
	assert isinstance(rc, RunConfidence)
	assert rc.confidence_label == ""
	assert rc.supporting_evidence is None


def test_audit_freshness_summary_all_fields():
	summary = build_audit_freshness_summary(
		scan_id="scanX",
		generation_timestamp="2026-05-31T00:00:00Z",
		profile="owner_gate",
		profile_version="0.8.0",
		artifact_status="ok",
		freshness_evidence="synced",
		workflow_store_sync_status="synced",
	)
	assert summary == AuditFreshnessSummary(
		scan_id="scanX",
		generation_timestamp="2026-05-31T00:00:00Z",
		profile="owner_gate",
		profile_version="0.8.0",
		workflow_store_sync_status="synced",
		artifact_status="ok",
		freshness_evidence="synced",
	)


def test_audit_freshness_summary_defaults():
	summary = build_audit_freshness_summary()
	assert isinstance(summary, AuditFreshnessSummary)
	assert summary.scan_id == ""
	assert summary.generation_timestamp == ""
	assert summary.profile == ""
	assert summary.profile_version == ""
	assert summary.workflow_store_sync_status == ""
	assert summary.artifact_status == ""
	assert summary.freshness_evidence == ""


def test_audit_freshness_summary_partial():
	summary = build_audit_freshness_summary(scan_id="scanX", profile="owner_gate")
	assert summary.scan_id == "scanX"
	assert summary.profile == "owner_gate"
	# Unprovided fields remain empty.
	assert summary.generation_timestamp == ""
	assert summary.profile_version == ""
	assert summary.workflow_store_sync_status == ""
	assert summary.artifact_status == ""
	assert summary.freshness_evidence == ""


def test_build_dashboard_bundle_with_sync_status():
	bundle = build_dashboard_bundle(BundleInput(workflow_store_sync_status="synced"))
	assert bundle.audit_freshness_summary.workflow_store_sync_status == "synced"


def test_lens_divergence_basic():
	# Two struct themes (S-001 covers F1,F2; S-002 covers F3,F4); one concept
	# theme spanning all four findings -> spans both struct themes.
	struct = [
		DummyTheme("S-001", member_finding_ids=["F1", "F2"]),
		DummyTheme("S-002", member_finding_ids=["F3", "F4"]),
	]
	concept = [DummyTheme("C-001", member_finding_ids=["F1", "F2", "F3", "F4"])]
	ld = build_lens_divergence(struct, concept)
	assert isinstance(ld, LensDivergence)
	# 2 struct / 1 concept = 2.0
	assert ld.consolidation_ratio == 2.0
	# C-001 spans S-001 and S-002 = 2 distinct -> mean 2.0
	assert ld.mean_conceptual_span == 2.0


def test_lens_divergence_empty_concept():
	struct = [DummyTheme("S-001", member_finding_ids=["F1"])]
	ld = build_lens_divergence(struct, [])
	assert ld.consolidation_ratio == 0.0
	assert ld.mean_conceptual_span == 0.0


def test_lens_divergence_empty_struct():
	concept = [DummyTheme("C-001", member_finding_ids=["F1", "F2"])]
	ld = build_lens_divergence([], concept)
	# 0 struct / 1 concept = 0.0
	assert ld.consolidation_ratio == 0.0
	# No struct memberships -> C-001 spans 0 distinct -> mean 0.0
	assert ld.mean_conceptual_span == 0.0


def test_lens_divergence_empty_member_concept():
	struct = [
		DummyTheme("S-001", member_finding_ids=["F1"]),
		DummyTheme("S-002", member_finding_ids=["F2"]),
	]
	# C-001 spans both struct themes (2); C-002 has no members (contributes 0).
	concept = [
		DummyTheme("C-001", member_finding_ids=["F1", "F2"]),
		DummyTheme("C-002", member_finding_ids=[]),
	]
	ld = build_lens_divergence(struct, concept)
	# 2 struct / 2 concept = 1.0
	assert ld.consolidation_ratio == 1.0
	# mean of (2, 0) across two concept themes = 1.0 (empty-member contributes 0)
	assert ld.mean_conceptual_span == 1.0


def test_lens_divergence_member_not_in_struct():
	struct = [DummyTheme("S-001", member_finding_ids=["F1"])]
	# C-001 has F1 (in S-001) and F99 (in no struct theme -> skipped silently).
	concept = [DummyTheme("C-001", member_finding_ids=["F1", "F99"])]
	ld = build_lens_divergence(struct, concept)
	assert ld.consolidation_ratio == 1.0
	# Only F1 maps to a struct theme -> 1 distinct; F99 skipped.
	assert ld.mean_conceptual_span == 1.0


def test_build_dashboard_bundle_with_lens_divergence():
	struct = [
		DummyTheme("S-001", member_finding_ids=["F1", "F2"]),
		DummyTheme("S-002", member_finding_ids=["F3"]),
	]
	concept = [DummyTheme("C-001", member_finding_ids=["F1", "F2", "F3"])]
	bundle = build_dashboard_bundle(BundleInput(struct_themes=struct, concept_themes=concept))
	# 2 struct / 1 concept = 2.0; C-001 spans both struct themes = 2.0
	assert bundle.lens_divergence.consolidation_ratio == 2.0
	assert bundle.lens_divergence.mean_conceptual_span == 2.0


def test_lens_divergence_signal_bearing_default():
	# Convention: default is signal-bearing with empty suppression_reason.
	ld = LensDivergence()
	assert ld.signal_bearing is True
	assert ld.suppression_reason == ""


@pytest.mark.parametrize("reason", [
	"non-signal-bearing", "low-signal", "effectively-singleton", "concept-unstable",
])
def test_build_lens_divergence_suppression_reasons(reason):
	struct = [DummyTheme("S-001", member_finding_ids=["F1"])]
	concept = [DummyTheme("C-001", member_finding_ids=["F1"])]
	ld = build_lens_divergence(struct, concept, signal_bearing=False, suppression_reason=reason)
	assert ld.signal_bearing is False
	assert ld.suppression_reason == reason
	# Metrics are still computed and stored regardless of suppression.
	assert ld.consolidation_ratio == 1.0


def test_build_lens_divergence_default_signal_bearing():
	struct = [DummyTheme("S-001", member_finding_ids=["F1"])]
	concept = [DummyTheme("C-001", member_finding_ids=["F1"])]
	ld = build_lens_divergence(struct, concept)
	assert ld.signal_bearing is True
	assert ld.suppression_reason == ""


def test_build_dashboard_bundle_with_suppression():
	struct = [DummyTheme("S-001", member_finding_ids=["F1"])]
	concept = [DummyTheme("C-001", member_finding_ids=["F1"])]
	bundle = build_dashboard_bundle(
		BundleInput(
			struct_themes=struct,
			concept_themes=concept,
			lens_signal_bearing=False,
			lens_suppression_reason="low-signal",
		)
	)
	assert bundle.lens_divergence.signal_bearing is False
	assert bundle.lens_divergence.suppression_reason == "low-signal"


def test_build_dashboard_bundle(synthetic_findings, synthetic_issues):
	bundle = build_dashboard_bundle(BundleInput(findings=synthetic_findings, issues=synthetic_issues))
	assert isinstance(bundle, DashboardBundle)
	assert bundle.severity_risk_summary
	assert bundle.lane_conflict_summary
	assert bundle.human_override_summary


def test_build_dashboard_bundle_with_themes(synthetic_findings, synthetic_issues, synthetic_struct_themes, synthetic_concept_themes):
	bundle = build_dashboard_bundle(
		BundleInput(
			findings=synthetic_findings,
			issues=synthetic_issues,
			struct_themes=synthetic_struct_themes,
			concept_themes=synthetic_concept_themes,
		)
	)
	assert isinstance(bundle, DashboardBundle)
	assert len(bundle.theme_summary) == 4
	assert bundle.workflow_state_summary


def test_build_dashboard_bundle_with_scorecard(synthetic_module_findings, synthetic_scorecard):
	bundle = build_dashboard_bundle(BundleInput(findings=synthetic_module_findings, scorecard=synthetic_scorecard))
	assert isinstance(bundle, DashboardBundle)
	assert bundle.module_hotspot_summary
	assert len(bundle.module_hotspot_summary) == 3
	assert bundle.run_confidence.confidence_label == "high"
	assert set(bundle.run_confidence.supporting_evidence.keys()) == {"metrics", "gates"}


def test_bundle_input_defaults():
	b = BundleInput()
	assert b.scan_id == ""
	assert b.profile == ""
	assert b.profile_version == ""
	assert b.artifact_scope == ""
	assert b.artifact_status == ""
	assert b.review_policy == ""
	assert b.generation_timestamp == ""
	assert b.freshness_evidence == ""
	assert b.workflow_store_sync_status == ""
	assert b.findings == []
	assert b.issues == []
	assert b.struct_themes == ()
	assert b.concept_themes == ()
	assert b.scorecard is None
	assert b.lens_signal_bearing is True
	assert b.lens_suppression_reason == ""
	assert b.config is None


def test_bundle_input_is_frozen():
	import dataclasses
	b = BundleInput()
	with pytest.raises(dataclasses.FrozenInstanceError):
		b.scan_id = "x"


def test_bundle_input_has_slots():
	b = BundleInput()
	assert not hasattr(b, "__dict__")
	assert hasattr(BundleInput, "__slots__")
	# Setting a new attribute is rejected; the exact exception type depends on
	# the frozen/slots interaction (TypeError under the frozen __setattr__
	# guard on a slots class), so accept any of the rejection types.
	with pytest.raises((AttributeError, TypeError)):
		b.new_attr = "x"
