import pytest

from sentinel.workflow.config import (
	GitHubConfig,
	ProfileConfig,
	RemediationConfig,
	ThemesConfig,
	WorkflowConfig,
	WorkflowConfigError,
)
from sentinel.workflow.models import (
	DriftReport,
	FieldDrift,
	FindingIssueRecord,
	GitHubIssueSnapshot,
	ThemeReference,
)
from sentinel.workflow.reconcile_issues import (
	_derive_human_triage,
	detect_drift,
	extract_finding_id,
	parse_state_label,
	plan_finding_closures,
	reconcile_finding_projection,
)


def _make_finding(status: str = "triage") -> FindingIssueRecord:
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
		status=status,
		human_triage=None,
		effort_estimate="medium",
		risk_of_deferral="high â€” likely to cause production issues",
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
		first_seen="scan-2026-05-14",
	)


def test_reconcile_finding_projection_prefers_github_state_label() -> None:
	finding = _make_finding(status="triage")
	issue = GitHubIssueSnapshot(
		issue_number=101,
		title="irrelevant",
		body="irrelevant",
		labels=("severity:p1", "state:owner-assessed", "sentinel-auto"),
	)

	# Item 16 part A (Commit 3): pass explicit owner_gate config so the
	# no-config DeprecationWarning path is exercised separately by
	# test_derive_human_triage_no_config_warns. This converts the test
	# to the profile-explicit pattern Commits 5-8 standardize on.
	result = reconcile_finding_projection(
		finding, issue, config=WorkflowConfig.default()
	)

	assert result.finding.status == "owner-assessed"
	assert result.finding.human_triage == "accepted"
	assert result.drift_report is None


def test_parse_state_label_rejects_multiple_state_values() -> None:
	with pytest.raises(ValueError, match="Multiple workflow state labels"):
		parse_state_label(["state:triage", "state:wont-fix"])





def _finding_body(finding_id: str) -> str:
	return f"## Description\nsome detail\n\n- **Finding ID:** {finding_id}\n"


def test_extract_finding_id_returns_id_from_body() -> None:
	assert extract_finding_id(_finding_body('F-abc123')) == 'F-abc123'


def test_extract_finding_id_returns_none_when_absent() -> None:
	assert extract_finding_id('no marker here') is None
	assert extract_finding_id('') is None


def test_plan_finding_closures_emits_for_non_reproducing_finding() -> None:
	open_issues = [
		GitHubIssueSnapshot(
			issue_number=11,
			title='t',
			body=_finding_body('F-still-here'),
			labels=('severity:p1', 'state:triage', 'sentinel-auto'),
		),
		GitHubIssueSnapshot(
			issue_number=12,
			title='t',
			body=_finding_body('F-gone'),
			labels=('severity:p2', 'state:triage', 'sentinel-auto'),
		),
	]
	result = plan_finding_closures({'F-still-here'}, open_issues)
	assert len(result.closure_plans) == 1
	assert result.closure_plans[0].issue_number == 12
	assert result.closure_plans[0].reason == 'no-longer-reproduces'
	assert result.closure_plans[0].successor_issue_number is None
	assert result.drift_reports == ()


def test_plan_finding_closures_respects_human_override() -> None:
	open_issues = [
		GitHubIssueSnapshot(
			issue_number=21,
			title='t',
			body=_finding_body('F-gone-but-protected'),
			labels=('state:triage', 'sentinel-auto', 'human-override'),
		),
		GitHubIssueSnapshot(
			issue_number=22,
			title='t',
			body=_finding_body('F-gone-unprotected'),
			labels=('state:triage', 'sentinel-auto'),
		),
	]
	result = plan_finding_closures(set(), open_issues)
	assert [plan.issue_number for plan in result.closure_plans] == [22]
	assert len(result.drift_reports) == 1
	assert result.drift_reports[0].issue_number == 21
	assert result.drift_reports[0].finding_id == 'F-gone-but-protected'
	assert result.drift_reports[0].drift_kind == 'skipped-closure'
	assert result.drift_reports[0].override_label_present is True


def test_plan_finding_closures_skips_theme_rollup_issues() -> None:
	open_issues = [
		GitHubIssueSnapshot(
			issue_number=31,
			title='[Theme][T-001] rollup',
			body='Member findings: F-x, F-y',
			labels=('is-struct-theme', 'state:triage', 'sentinel-auto'),
		),
		GitHubIssueSnapshot(
			issue_number=32,
			title='[Theme][C-001] rollup',
			body='Member findings: F-z',
			labels=('is-concept-theme', 'state:triage'),
		),
	]
	result = plan_finding_closures(set(), open_issues)
	assert result.closure_plans == ()
	assert result.drift_reports == ()


def test_plan_finding_closures_skips_issues_without_finding_id_marker() -> None:
	open_issues = [
		GitHubIssueSnapshot(
			issue_number=41,
			title='random',
			body='no finding marker here',
			labels=('state:triage',),
		),
	]
	result = plan_finding_closures(set(), open_issues)
	assert result.closure_plans == ()
	assert result.drift_reports == ()


def test_plan_finding_closures_supports_custom_override_label() -> None:
	open_issues = [
		GitHubIssueSnapshot(
			issue_number=51,
			title='t',
			body=_finding_body('F-gone'),
			labels=('state:triage', 'operator-locked'),
		),
	]
	result = plan_finding_closures(set(), open_issues, override_label='operator-locked')
	assert result.closure_plans == ()
	assert len(result.drift_reports) == 1
	assert result.drift_reports[0].drift_kind == 'skipped-closure'


def test_drift_report_dataclass_is_frozen_with_slots() -> None:
	import dataclasses
	report = DriftReport(
		finding_id="F-abc",
		issue_number=7,
		drift_kind="skipped-closure",
		override_label_present=True,
	)
	with pytest.raises(dataclasses.FrozenInstanceError):
		report.issue_number = 8  # type: ignore[misc]
	assert report.drift_details == ()
	assert report.to_dict()["drift_kind"] == "skipped-closure"


def test_drift_report_serializes_field_details() -> None:
	report = DriftReport(
		finding_id="F-abc",
		issue_number=7,
		drift_kind="field-disagreement",
		drift_details=(
			FieldDrift(field_name="status", sentinel_value="triage", github_value="wont-fix"),
		),
	)
	dumped = report.to_dict()
	assert dumped["drift_details"] == [
		{"field_name": "status", "sentinel_value": "triage", "github_value": "wont-fix"},
	]
	assert dumped["override_label_present"] is False


def test_reconcile_finding_projection_respects_override_and_emits_drift() -> None:
	finding = _make_finding(status="triage")
	issue = GitHubIssueSnapshot(
		issue_number=901,
		title="t",
		body="b",
		labels=("state:wont-fix", "human-override"),
	)
	result = reconcile_finding_projection(finding, issue)
	assert result.finding is finding
	assert result.finding.status == "triage"
	assert result.drift_report is not None
	assert result.drift_report.drift_kind == "skipped-projection"
	assert result.drift_report.issue_number == 901
	assert result.drift_report.override_label_present is True


def test_reconcile_finding_projection_returns_unchanged_when_no_state_label() -> None:
	finding = _make_finding(status="triage")
	issue = GitHubIssueSnapshot(
		issue_number=902,
		title="t",
		body="b",
		labels=("severity:p1",),
	)
	result = reconcile_finding_projection(finding, issue)
	assert result.finding is finding
	assert result.drift_report is None


def test_reconcile_finding_projection_supports_custom_override_label() -> None:
	finding = _make_finding(status="triage")
	issue = GitHubIssueSnapshot(
		issue_number=903,
		title="t",
		body="b",
		labels=("state:owner-assessed", "operator-locked"),
	)
	result = reconcile_finding_projection(finding, issue, override_label="operator-locked")
	assert result.finding is finding
	assert result.drift_report is not None
	assert result.drift_report.drift_kind == "skipped-projection"


def test_detect_drift_returns_none_when_state_matches() -> None:
	finding = _make_finding(status="triage")
	issue = GitHubIssueSnapshot(
		issue_number=701,
		title="t",
		body="b",
		labels=("severity:p1", "state:triage"),
	)
	assert detect_drift(finding, issue) is None


def test_detect_drift_reports_status_disagreement() -> None:
	finding = _make_finding(status="triage")
	issue = GitHubIssueSnapshot(
		issue_number=702,
		title="t",
		body="b",
		labels=("severity:p1", "state:wont-fix"),
	)
	report = detect_drift(finding, issue)
	assert report is not None
	assert report.drift_kind == "field-disagreement"
	assert report.finding_id == "F-123456789abc"
	assert report.issue_number == 702
	names = [detail.field_name for detail in report.drift_details]
	assert names == ["status"]
	assert report.drift_details[0].sentinel_value == "triage"
	assert report.drift_details[0].github_value == "wont-fix"


def test_detect_drift_reports_severity_and_status_disagreement() -> None:
	finding = _make_finding(status="triage")
	issue = GitHubIssueSnapshot(
		issue_number=703,
		title="t",
		body="b",
		labels=("severity:p2", "state:owner-assessed"),
	)
	report = detect_drift(finding, issue)
	assert report is not None
	fields = {detail.field_name: (detail.sentinel_value, detail.github_value) for detail in report.drift_details}
	assert fields == {
		"severity": ("P1", "p2"),
		"status": ("triage", "owner-assessed"),
	}


def test_detect_drift_skips_fields_without_label() -> None:
	finding = _make_finding(status="triage")
	issue = GitHubIssueSnapshot(
		issue_number=704,
		title="t",
		body="b",
		labels=("sentinel-auto",),
	)
	assert detect_drift(finding, issue) is None


def test_plan_finding_closures_uses_config_override_label() -> None:
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
	open_issues = [
		GitHubIssueSnapshot(
			issue_number=601,
			title='t',
			body=_finding_body('F-gone'),
			labels=('state:triage', 'operator-locked'),
		),
		GitHubIssueSnapshot(
			issue_number=602,
			title='t',
			body=_finding_body('F-also-gone'),
			labels=('state:triage', 'human-override'),
		),
	]
	result = plan_finding_closures(set(), open_issues, config=config)
	# config.profile.override_label = "operator-locked" wins -- the
	# "human-override" label no longer skips, so issue 602 gets closed.
	assert [plan.issue_number for plan in result.closure_plans] == [602]
	assert len(result.drift_reports) == 1
	assert result.drift_reports[0].issue_number == 601


def test_reconcile_finding_projection_uses_config_override_label() -> None:
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
	finding = _make_finding(status="triage")
	issue = GitHubIssueSnapshot(
		issue_number=611,
		title='t',
		body='b',
		labels=('state:owner-assessed', 'operator-locked'),
	)
	result = reconcile_finding_projection(finding, issue, config=config)
	assert result.drift_report is not None
	assert result.drift_report.drift_kind == "skipped-projection"
	assert result.finding is finding


# ---------------------------------------------------------------------------
# Cycle 11 Commit 3: item 16 part A -- _derive_human_triage rewire to
# read accepted_states/rejected_states from config.profile.transitions
# via _resolve_transitions. Caller cascade is zero: only
# reconcile_finding_projection invokes _derive_human_triage, and it
# already accepts config as a keyword.
# ---------------------------------------------------------------------------


def _solo_config() -> WorkflowConfig:
	return WorkflowConfig.default(profile_name="solo_local_remediating")


@pytest.mark.parametrize(
	"profile_name, accepted_status, expected_triage",
	[
		# owner_gate: "owner-assessed" is an accepted-lane state; the
		# function defaults human_triage to "accepted" when current_value
		# is None.
		("owner_gate", "owner-assessed", "accepted"),
		# solo_local_remediating: "accepted" is itself an accepted-lane
		# state per spec §1.6.3; same control flow, same "accepted"
		# default literal.
		("solo_local_remediating", "accepted", "accepted"),
		# solo_local_remediating: "approved" (canonical synthesis per
		# Commit 2) is an accepted-lane state.
		("solo_local_remediating", "approved", "accepted"),
	],
)
def test_derive_human_triage_accepted_states_across_profiles(
	profile_name: str, accepted_status: str, expected_triage: str
) -> None:
	"""Profile-agnostic behavior: for any state in the active profile's
	accepted_states, the function returns current_value or 'accepted'."""
	config = WorkflowConfig.default(profile_name=profile_name)
	assert (
		_derive_human_triage(accepted_status, None, config=config)
		== expected_triage
	)


@pytest.mark.parametrize(
	"profile_name, rejected_status",
	[
		# owner_gate rejected-lane vocabulary.
		("owner_gate", "wont-fix"),
		("owner_gate", "deferred"),
		# solo_local_remediating rejected-lane vocabulary -- distinct
		# from owner_gate: "rejected" instead of "wont-fix".
		("solo_local_remediating", "rejected"),
		("solo_local_remediating", "deferred"),
	],
)
def test_derive_human_triage_rejected_states_across_profiles(
	profile_name: str, rejected_status: str
) -> None:
	"""Profile-agnostic behavior: for any state in the active profile's
	rejected_states, the function returns the status itself as the
	human_triage label."""
	config = WorkflowConfig.default(profile_name=profile_name)
	assert (
		_derive_human_triage(rejected_status, None, config=config)
		== rejected_status
	)


def test_derive_human_triage_owner_gate_config_matches_pre_rewire_behavior() -> None:
	"""Format-compatibility contract verification: cycle 11 Commit 1 pinned
	the contract that _OWNER_GATE_TRANSITIONS.accepted_states and
	.rejected_states match the pre-Commit-3 hardcoded sets exactly. This
	test verifies the Commit 3 wiring satisfies that contract: every
	previously hardcoded state produces identical behavior to the
	pre-rewire implementation."""
	config = WorkflowConfig.default()  # owner_gate
	# Pre-rewire accepted set (line 87 of pre-Commit-3 reconcile_issues.py).
	for status in [
		"owner-assessed",
		"owner-approved",
		"remediating",
		"pr-open",
		"closed",
	]:
		assert (
			_derive_human_triage(status, None, config=config) == "accepted"
		), f"accepted-lane status {status!r} regressed"
	# Pre-rewire rejected set (line 89 of pre-Commit-3 reconcile_issues.py).
	for status in ["wont-fix", "deferred"]:
		assert (
			_derive_human_triage(status, None, config=config) == status
		), f"rejected-lane status {status!r} regressed"
	# Out-of-vocabulary status returns current_value unchanged.
	assert _derive_human_triage("triage", None, config=config) is None
	assert (
		_derive_human_triage("triage", "existing", config=config) == "existing"
	)


def test_derive_human_triage_solo_local_remediating_uses_profile_vocabulary() -> None:
	"""Profile-specific verification: solo_local_remediating produces
	different results for the owner_gate-specific vocabulary because
	those labels are not in the solo profile's accepted/rejected
	state sets."""
	config = _solo_config()
	# "owner-assessed" is owner_gate-only -> not in solo accepted_states.
	# Falls through to "return current_value" branch.
	assert _derive_human_triage("owner-assessed", None, config=config) is None
	# "wont-fix" is owner_gate display label -> not in solo rejected_states.
	# Falls through to "return current_value" branch.
	assert _derive_human_triage("wont-fix", None, config=config) is None
	# "rejected" is solo-only -> returns "rejected" as human_triage.
	assert _derive_human_triage("rejected", None, config=config) == "rejected"
	# Current value is preserved on out-of-vocabulary status.
	assert (
		_derive_human_triage("owner-assessed", "preserved", config=config)
		== "preserved"
	)


def test_derive_human_triage_advisory_profile_raises() -> None:
	"""Advisory profile (transitions=None) cannot drive workflow
	transitions per spec §1.6.2; _resolve_transitions raises and the
	error propagates."""
	advisory = WorkflowConfig(
		github=GitHubConfig(owner="o", repo="r"),
		themes=ThemesConfig(),
		remediation=RemediationConfig(),
		profile=ProfileConfig(
			name="solo_local_advisory",
			version="0.8.0",
			transitions=None,
		),
	)
	with pytest.raises(
		WorkflowConfigError, match=r"declares no workflow transitions"
	):
		_derive_human_triage("triage", None, config=advisory)


def test_derive_human_triage_no_config_warns_and_falls_back_to_owner_gate() -> None:
	"""No-config-fallback discipline preserved (Commit 1 state 1):
	calling without config emits DeprecationWarning and uses
	_OWNER_GATE_TRANSITIONS, so existing call sites that have not yet
	been converted to explicit-config still work."""
	with pytest.warns(DeprecationWarning, match=r"WorkflowConfig not supplied"):
		result = _derive_human_triage("owner-assessed", None)
	assert result == "accepted"
	# Verify the no-config path matches owner_gate-explicit path
	# (cycle-8-precedent equality).
	with pytest.warns(DeprecationWarning):
		assert _derive_human_triage("wont-fix", None) == "wont-fix"


def test_derive_human_triage_preserves_existing_human_triage_on_accepted_lane() -> None:
	"""The 'return current_value or "accepted"' branch preserves any
	existing human_triage label rather than overwriting it. Profile
	parity verified across both profiles."""
	for config in [WorkflowConfig.default(), _solo_config()]:
		# Pick an accepted-lane status from this profile's vocabulary.
		accepted_status = next(iter(config.profile.transitions.accepted_states))
		assert (
			_derive_human_triage(
				accepted_status, "owner-set-label", config=config
			)
			== "owner-set-label"
		)


def test_reconcile_finding_projection_threads_config_to_derive_human_triage() -> None:
	"""End-to-end integration: reconcile_finding_projection passes its
	config through to _derive_human_triage, so a solo_local_remediating
	config produces solo-vocabulary human_triage on the projected
	finding."""
	finding = _make_finding(status="triage")
	issue = GitHubIssueSnapshot(
		issue_number=701,
		title="t",
		body="b",
		labels=("state:rejected",),
	)
	result = reconcile_finding_projection(finding, issue, config=_solo_config())
	assert result.finding.status == "rejected"
	# "rejected" is solo's rejected-lane label -> human_triage set to status.
	assert result.finding.human_triage == "rejected"
	assert result.drift_report is None