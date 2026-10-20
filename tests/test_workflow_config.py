"""Tests for repo-level workflow configuration loading."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from sentinel.workflow.config import (
	GitHubConfig,
	ProfileConfig,
	RemediationConfig,
	ThemesConfig,
	TransitionStateConfig,
	WorkflowConfig,
	WorkflowConfigError,
	_OWNER_GATE_TRANSITIONS,
	_SOLO_LOCAL_REMEDIATING_STATE_LABELS,
	_SOLO_LOCAL_REMEDIATING_TRANSITIONS,
	_resolve_transitions,
	load_workflow_config,
)
from sentinel.workflow.github_client import GitHubWorkflowClient


def _write_config(path: Path, body: str) -> Path:
	config_path = path / "config.toml"
	config_path.write_text(body, encoding="utf-8")
	return config_path


def test_load_workflow_config_happy_path(tmp_path: Path) -> None:
	config_path = _write_config(
		tmp_path,
		"""
[github]
owner = "chemnteach"
repo = "sentinel"
default_assignee = "octocat"
approver_team = "owner-approvers"

[themes]
primary_lens = "conceptual"

[remediation]
max_open_prs = 5
pr_max_files = 15
pr_max_loc = 500
pilot_gate_enabled = true
pilot_auto_relax_after_n_clean_merges = 3
require_tests_green = true

[profile]
name = "owner_gate"
version = "0.8.0"
""",
	)

	config = load_workflow_config(config_path)

	assert config.github.owner == "chemnteach"
	assert config.github.repo == "sentinel"
	assert config.github.default_assignee == "octocat"
	assert config.github.approver_team == "owner-approvers"
	assert config.themes.primary_lens == "conceptual"
	assert config.themes.conceptual_lens.stability_jaccard_threshold == 0.7
	assert config.remediation.max_open_prs == 5
	assert config.remediation.pr_max_files == 15
	assert config.remediation.pr_max_loc == 500
	assert config.remediation.pilot_gate_enabled is True
	assert config.remediation.pilot_auto_relax_after_n_clean_merges == 3
	assert config.remediation.require_tests_green is True
	assert config.profile.name == "owner_gate"
	assert config.profile.version == "0.8.0"
	assert config.profile.override_label == "human-override"
	assert "owner-approved" in config.profile.state_labels
	assert config.profile.auto_label == "sentinel-auto"


def test_load_workflow_config_missing_github_section_raises(tmp_path: Path) -> None:
	config_path = _write_config(
		tmp_path,
		"""
[profile]
name = "owner_gate"
""",
	)
	with pytest.raises(WorkflowConfigError, match=r"missing required section \[github\]"):
		load_workflow_config(config_path)


def test_load_workflow_config_missing_github_owner_raises(tmp_path: Path) -> None:
	config_path = _write_config(
		tmp_path,
		"""
[github]
repo = "sentinel"

[profile]
name = "owner_gate"
""",
	)
	with pytest.raises(WorkflowConfigError, match=r"missing required key 'owner' in section \[github\]"):
		load_workflow_config(config_path)


def test_load_workflow_config_unknown_profile_raises(tmp_path: Path) -> None:
	config_path = _write_config(
		tmp_path,
		"""
[github]
owner = "o"
repo = "r"

[profile]
name = "not_a_real_profile"
""",
	)
	with pytest.raises(WorkflowConfigError, match=r"unknown profile 'not_a_real_profile'"):
		load_workflow_config(config_path)


def test_load_workflow_config_ignores_skills_section(tmp_path: Path) -> None:
	config_path = _write_config(
		tmp_path,
		"""
[github]
owner = "o"
repo = "r"

[skills]
enabled = ["layer2a-quick-review"]
disabled = ["maintainability-smell"]

[profile]
name = "owner_gate"
""",
	)
	config = load_workflow_config(config_path)
	assert not hasattr(config, "skills")
	assert config.to_dict().keys() == {"github", "themes", "remediation", "dashboard", "profile"}


def test_load_workflow_config_supports_custom_override_label(tmp_path: Path) -> None:
	config_path = _write_config(
		tmp_path,
		"""
[github]
owner = "o"
repo = "r"

[profile]
name = "owner_gate"
override_label = "operator-locked"
""",
	)
	config = load_workflow_config(config_path)
	assert config.profile.override_label == "operator-locked"


def test_workflow_config_default_returns_owner_gate() -> None:
	config = WorkflowConfig.default()
	assert config.profile.name == "owner_gate"
	assert config.profile.version == "0.8.0"
	assert config.profile.override_label == "human-override"
	assert "triage" in config.profile.state_labels
	assert "owner-assessed" in config.profile.state_labels
	assert "owner-approved" in config.profile.state_labels
	assert "closed" in config.profile.state_labels


def test_workflow_config_default_rejects_unknown_profile() -> None:
	with pytest.raises(WorkflowConfigError, match=r"unknown profile 'nope'"):
		WorkflowConfig.default(profile_name="nope")


def test_profile_config_dataclass_is_frozen_with_slots() -> None:
	profile = ProfileConfig(name="owner_gate", version="0.8.0")
	with pytest.raises(dataclasses.FrozenInstanceError):
		profile.override_label = "x"  # type: ignore[misc]
	assert profile.to_dict()["name"] == "owner_gate"
	assert profile.to_dict()["override_label"] == "human-override"


def test_workflow_config_to_dict_roundtrip_keys() -> None:
	config = WorkflowConfig(
		github=GitHubConfig(owner="o", repo="r"),
		themes=ThemesConfig(),
		remediation=RemediationConfig(),
		profile=ProfileConfig(name="owner_gate", version="0.8.0"),
	)
	dumped = config.to_dict()
	assert set(dumped.keys()) == {"github", "themes", "remediation", "dashboard", "profile"}
	assert dumped["github"]["owner"] == "o"
	assert dumped["profile"]["state_labels"] == []


def test_github_workflow_client_from_config_uses_github_section() -> None:
	config = WorkflowConfig(
		github=GitHubConfig(owner="chemnteach", repo="sentinel"),
		themes=ThemesConfig(),
		remediation=RemediationConfig(),
		profile=ProfileConfig(name="owner_gate", version="0.8.0"),
	)
	client = GitHubWorkflowClient.from_config(config, token="t0k3n")
	assert isinstance(client, GitHubWorkflowClient)
	assert client._owner == "chemnteach"  # type: ignore[attr-defined]
	assert client._repo == "sentinel"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Cycle 11 Commit 1: TransitionStateConfig + _resolve_transitions
# ---------------------------------------------------------------------------


def test_transition_state_config_dataclass_shape() -> None:
	"""Verify the 9 fields (7 foundation + 2 disposition discriminators),
	types, frozen+slots, and constructor."""
	transitions = TransitionStateConfig(
		triage_state="t",
		accepted_states=frozenset({"a", "b"}),
		rejected_states=frozenset({"r", "d"}),
		rejection_label="r",
		deferral_label="d",
		approval_state="approve",
		remediation_state="remed",
		review_state="rev",
		closed_state="done",
	)
	# Field presence and types.
	assert transitions.triage_state == "t"
	assert transitions.accepted_states == frozenset({"a", "b"})
	assert transitions.rejected_states == frozenset({"r", "d"})
	assert transitions.rejection_label == "r"
	assert transitions.deferral_label == "d"
	assert transitions.approval_state == "approve"
	assert transitions.remediation_state == "remed"
	assert transitions.review_state == "rev"
	assert transitions.closed_state == "done"
	# Frozen.
	with pytest.raises(dataclasses.FrozenInstanceError):
		transitions.triage_state = "x"  # type: ignore[misc]
	# Slots (no __dict__).
	assert not hasattr(transitions, "__dict__")
	# to_dict sorted-list view of the frozensets.
	dumped = transitions.to_dict()
	assert dumped["accepted_states"] == ["a", "b"]
	assert dumped["rejected_states"] == ["d", "r"]
	assert dumped["rejection_label"] == "r"
	assert dumped["deferral_label"] == "d"


def test_owner_gate_transitions_default() -> None:
	"""Format-compatibility pin: cycle 11 Commits 3-4 wire reconcile_issues
	and pr_feedback_handler to read from _OWNER_GATE_TRANSITIONS. Their
	current hardcoded values must match these field values exactly; this
	test is the contract anchor."""
	t = _OWNER_GATE_TRANSITIONS
	assert t.triage_state == "triage"
	# reconcile_issues._derive_human_triage accepted-states set (line ~87).
	assert t.accepted_states == frozenset({
		"owner-assessed",
		"owner-approved",
		"remediating",
		"pr-open",
		"closed",
	})
	# reconcile_issues._derive_human_triage rejected-states set (line ~89).
	assert t.rejected_states == frozenset({"wont-fix", "deferred"})
	# pr_feedback_handler.handle_pr_closed_without_merge target.
	assert t.approval_state == "owner-approved"
	# pr_feedback_handler.handle_per_commit_rejection target.
	assert t.remediation_state == "remediating"
	# pr_feedback_handler source state for all three PR-feedback flows.
	assert t.review_state == "pr-open"
	# pr_feedback_handler.handle_pr_merged target.
	assert t.closed_state == "closed"


def test_profile_config_transitions_field_defaults_to_none() -> None:
	"""Additive field; existing ProfileConfig constructions remain valid."""
	profile = ProfileConfig(name="solo_local_advisory", version="0.8.0")
	assert profile.transitions is None
	# to_dict surfaces None when absent.
	assert profile.to_dict()["transitions"] is None


def test_profile_config_accepts_transition_state_config() -> None:
	profile = ProfileConfig(
		name="owner_gate",
		version="0.8.0",
		state_labels=frozenset({"triage"}),
		transitions=_OWNER_GATE_TRANSITIONS,
	)
	assert profile.transitions is _OWNER_GATE_TRANSITIONS
	dumped = profile.to_dict()
	assert dumped["transitions"]["triage_state"] == "triage"
	assert dumped["transitions"]["closed_state"] == "closed"


def test_workflow_config_default_owner_gate_populates_transitions() -> None:
	"""Loaded and defaulted owner_gate configs must carry transitions
	(loader fix: ProfileConfig in load_workflow_config forwards
	default_profile.transitions). Without this, every loaded owner_gate
	config would trip the advisory-profile refusal in
	_resolve_transitions."""
	config = WorkflowConfig.default()
	assert config.profile.transitions is _OWNER_GATE_TRANSITIONS


def test_load_workflow_config_populates_transitions(tmp_path: Path) -> None:
	config_path = _write_config(
		tmp_path,
		"""
[github]
owner = "o"
repo = "r"

[profile]
name = "owner_gate"
version = "0.8.0"
""",
	)
	config = load_workflow_config(config_path)
	assert config.profile.transitions is _OWNER_GATE_TRANSITIONS


def test_resolve_transitions_no_config_warns_and_falls_back() -> None:
	with pytest.warns(DeprecationWarning, match=r"WorkflowConfig not supplied"):
		result = _resolve_transitions(None)
	assert result is _OWNER_GATE_TRANSITIONS


def test_resolve_transitions_advisory_profile_raises() -> None:
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
		WorkflowConfigError,
		match=r"declares no workflow transitions",
	):
		_resolve_transitions(advisory)


def test_resolve_transitions_configured_returns_profile_transitions() -> None:
	custom = TransitionStateConfig(
		triage_state="new",
		accepted_states=frozenset({"open"}),
		rejected_states=frozenset({"closed", "deferred"}),
		rejection_label="closed",
		deferral_label="deferred",
		approval_state="open",
		remediation_state="open",
		review_state="open",
		closed_state="closed",
	)
	config = WorkflowConfig(
		github=GitHubConfig(owner="o", repo="r"),
		themes=ThemesConfig(),
		remediation=RemediationConfig(),
		profile=ProfileConfig(
			name="owner_gate",
			version="0.8.0",
			transitions=custom,
		),
	)
	assert _resolve_transitions(config) is custom


# ---------------------------------------------------------------------------
# Cycle 11 Commit 2: solo_local_remediating as second profile (Q2 lock)
# ---------------------------------------------------------------------------


def test_solo_local_remediating_transitions_default() -> None:
	"""Format-compatibility pin per spec §1.6.3. Parallel to
	test_owner_gate_transitions_default. Commits 5-7 (Phase 4 handlers) and
	Commit 8 (multi-profile demonstration) parametrize over both profiles;
	this test is the contract anchor for the solo_local_remediating side."""
	t = _SOLO_LOCAL_REMEDIATING_TRANSITIONS
	assert t.triage_state == "triage"
	# §1.6.3 accepted-lane progression: triage -> accepted -> approved
	# (owner-authorized) -> remediating -> review -> closed. The
	# accepted_states set excludes the triage entry state (consistent
	# with _OWNER_GATE_TRANSITIONS's accepted_states convention).
	assert t.accepted_states == frozenset({
		"accepted",
		"approved",
		"remediating",
		"review",
		"closed",
	})
	# §1.6.3 rejected lane: "rejected" and "deferred" (the GitHub
	# display variant "wont-fix" is a presentation label, not core).
	assert t.rejected_states == frozenset({"rejected", "deferred"})
	# Canonical-approved synthesis decision per cycle 11 Q2 lock
	# (documented in _SOLO_LOCAL_REMEDIATING_TRANSITIONS docstring).
	assert t.approval_state == "approved"
	assert t.remediation_state == "remediating"
	assert t.review_state == "review"
	assert t.closed_state == "closed"


def test_solo_local_remediating_state_labels_match_spec() -> None:
	"""Per §1.6.3: the eight core state labels (alternate display
	variants like ``wont-fix`` and ``authorized`` are not in the
	core set)."""
	assert _SOLO_LOCAL_REMEDIATING_STATE_LABELS == frozenset({
		"triage",
		"accepted",
		"rejected",
		"deferred",
		"approved",
		"remediating",
		"review",
		"closed",
	})


def test_workflow_config_default_solo_local_remediating_populates_profile() -> None:
	config = WorkflowConfig.default(profile_name="solo_local_remediating")
	assert config.profile.name == "solo_local_remediating"
	assert config.profile.version == "0.8.0"
	assert config.profile.state_labels is _SOLO_LOCAL_REMEDIATING_STATE_LABELS
	assert config.profile.transitions is _SOLO_LOCAL_REMEDIATING_TRANSITIONS
	# Foundation markers per §1.6.3 Label vocabulary row.
	assert config.profile.override_label == "human-override"
	assert config.profile.auto_label == "sentinel-auto"


def test_load_workflow_config_solo_local_remediating(tmp_path: Path) -> None:
	"""Loader integration: TOML round-trip populates transitions for
	the second profile, parallel to the Commit 1 owner_gate loader fix."""
	config_path = _write_config(
		tmp_path,
		"""
[github]
owner = "solo-owner"
repo = "my-repo"

[profile]
name = "solo_local_remediating"
version = "0.8.0"
""",
	)
	config = load_workflow_config(config_path)
	assert config.profile.name == "solo_local_remediating"
	assert config.profile.transitions is _SOLO_LOCAL_REMEDIATING_TRANSITIONS
	assert config.profile.state_labels == _SOLO_LOCAL_REMEDIATING_STATE_LABELS


def test_solo_local_remediating_distinct_from_owner_gate() -> None:
	"""5-dimension distinctness pin per Q2 design pass: protects against
	future refactors that might inadvertently converge the two profiles.
	If a refactor needs to converge a dimension, this test must be
	updated explicitly with the design-pass rationale."""
	acme = _OWNER_GATE_TRANSITIONS
	solo = _SOLO_LOCAL_REMEDIATING_TRANSITIONS

	# Dimension 1: single human role -> "accepted" (solo owner triage
	# acceptance) vs the owner_gate two-role split that runs through
	# "owner-assessed" first.
	assert "accepted" in solo.accepted_states
	assert "accepted" not in acme.accepted_states
	assert "owner-assessed" in acme.accepted_states
	assert "owner-assessed" not in solo.accepted_states

	# Dimension 2: approval state -- "approved" (canonical synthesis
	# from §1.6.3 "approved or authorized") vs owner_gate's
	# "owner-approved" (organization-scoped).
	assert solo.approval_state == "approved"
	assert acme.approval_state == "owner-approved"

	# Dimension 3: review state -- generic "review" vs owner_gate's
	# GitHub-PR-specific "pr-open".
	assert solo.review_state == "review"
	assert acme.review_state == "pr-open"

	# Dimension 4: rejected lane vocabulary -- generic "rejected" vs
	# owner_gate's "wont-fix" display label.
	assert "rejected" in solo.rejected_states
	assert "rejected" not in acme.rejected_states
	assert "wont-fix" in acme.rejected_states
	assert "wont-fix" not in solo.rejected_states

	# Dimension 5: shared rejected-lane label -- both carry "deferred"
	# as the foundation deferral concept (this is the one accepted
	# overlap; documenting it explicitly prevents future "all rejected
	# labels are profile-specific" assumptions).
	assert "deferred" in solo.rejected_states
	assert "deferred" in acme.rejected_states


def test_resolve_transitions_solo_local_remediating_returns_profile_transitions() -> None:
	config = WorkflowConfig.default(profile_name="solo_local_remediating")
	assert _resolve_transitions(config) is _SOLO_LOCAL_REMEDIATING_TRANSITIONS


# ---------------------------------------------------------------------------
# Cycle 11 Commit 6: disposition discriminator fields (Q1 lock extension)
# ---------------------------------------------------------------------------


def test_transition_state_config_rejection_label_field() -> None:
	"""``rejection_label`` is a required str field naming the
	``rejected_states`` member that carries the "rejection" semantic
	(vs. the "deferral" semantic)."""
	transitions = TransitionStateConfig(
		triage_state="t",
		accepted_states=frozenset({"a"}),
		rejected_states=frozenset({"x", "y"}),
		rejection_label="x",
		deferral_label="y",
		approval_state="a",
		remediation_state="a",
		review_state="a",
		closed_state="a",
	)
	assert transitions.rejection_label == "x"
	assert isinstance(transitions.rejection_label, str)


def test_transition_state_config_deferral_label_field() -> None:
	"""``deferral_label`` is a required str field naming the
	``rejected_states`` member that carries the "deferral" semantic."""
	transitions = TransitionStateConfig(
		triage_state="t",
		accepted_states=frozenset({"a"}),
		rejected_states=frozenset({"x", "y"}),
		rejection_label="x",
		deferral_label="y",
		approval_state="a",
		remediation_state="a",
		review_state="a",
		closed_state="a",
	)
	assert transitions.deferral_label == "y"
	assert isinstance(transitions.deferral_label, str)


def test_transition_state_config_label_invariant() -> None:
	"""``__post_init__`` enforces
	``{rejection_label, deferral_label} <= rejected_states``. The
	check fails fast at construction time so handler code never sees
	an inconsistent configuration."""
	# Rejection-label not in rejected_states.
	with pytest.raises(WorkflowConfigError, match=r"must be members of rejected_states"):
		TransitionStateConfig(
			triage_state="t",
			accepted_states=frozenset({"a"}),
			rejected_states=frozenset({"x", "y"}),
			rejection_label="not-in-set",
			deferral_label="y",
			approval_state="a",
			remediation_state="a",
			review_state="a",
			closed_state="a",
		)
	# Deferral-label not in rejected_states.
	with pytest.raises(WorkflowConfigError, match=r"must be members of rejected_states"):
		TransitionStateConfig(
			triage_state="t",
			accepted_states=frozenset({"a"}),
			rejected_states=frozenset({"x", "y"}),
			rejection_label="x",
			deferral_label="also-not-in-set",
			approval_state="a",
			remediation_state="a",
			review_state="a",
			closed_state="a",
		)


def test_owner_gate_transitions_rejection_label() -> None:
	"""Format-compatibility pin: owner_gate's rejection-flavored label."""
	assert _OWNER_GATE_TRANSITIONS.rejection_label == "wont-fix"
	assert _OWNER_GATE_TRANSITIONS.rejection_label in _OWNER_GATE_TRANSITIONS.rejected_states


def test_owner_gate_transitions_deferral_label() -> None:
	"""Format-compatibility pin: owner_gate's deferral-flavored label."""
	assert _OWNER_GATE_TRANSITIONS.deferral_label == "deferred"
	assert _OWNER_GATE_TRANSITIONS.deferral_label in _OWNER_GATE_TRANSITIONS.rejected_states


def test_solo_local_remediating_transitions_rejection_label() -> None:
	"""Format-compatibility pin: solo_local_remediating's
	rejection-flavored label per spec §1.6.3."""
	assert _SOLO_LOCAL_REMEDIATING_TRANSITIONS.rejection_label == "rejected"
	assert (
		_SOLO_LOCAL_REMEDIATING_TRANSITIONS.rejection_label
		in _SOLO_LOCAL_REMEDIATING_TRANSITIONS.rejected_states
	)


def test_solo_local_remediating_transitions_deferral_label() -> None:
	"""Format-compatibility pin: solo_local_remediating's
	deferral-flavored label. ``"deferred"`` is the documented
	cross-profile overlap (see
	``test_solo_local_remediating_distinct_from_owner_gate`` dimension 5)."""
	assert _SOLO_LOCAL_REMEDIATING_TRANSITIONS.deferral_label == "deferred"
	assert (
		_SOLO_LOCAL_REMEDIATING_TRANSITIONS.deferral_label
		in _SOLO_LOCAL_REMEDIATING_TRANSITIONS.rejected_states
	)


# ---------------------------------------------------------------------------
# Cycle 14 Item 9: YAML config frontend -- format-equivalence and guard tests
#
# Implements the cycle-14 design-pass test surface for the additive YAML
# loader (load_workflow_config_yaml). The three fixture-content-varying
# tests below are intentionally implemented as separate named functions
# rather than parametrized cases: the cycle-12/13 parametrize convention
# applies to profile-sweep tests (same fixture body, different config),
# whereas these cases each carry a distinct inline TOML/YAML fixture
# whose readability is the primary value -- forcing them through
# pytest.mark.parametrize would push the fixture strings into a
# parameter table and obscure the side-by-side TOML/YAML comparison.
# ---------------------------------------------------------------------------


from sentinel.workflow.config import load_workflow_config_yaml  # noqa: E402


def _write_yaml(path: Path, body: str) -> Path:
	yaml_path = path / "config.yaml"
	yaml_path.write_text(body, encoding="utf-8")
	return yaml_path


def test_load_yaml_full_config_matches_toml(tmp_path: Path) -> None:
	"""Equivalence case 1 of 3: full owner_gate config with every
	section populated and non-default values throughout."""
	toml_path = _write_config(
		tmp_path,
		"""
[github]
owner = "chemnteach"
repo = "sentinel"
default_assignee = "octocat"
approver_team = "owner-approvers"

[themes]
primary_lens = "structural"

[themes.conceptual_lens]
stability_jaccard_threshold = 0.85

[remediation]
max_open_prs = 8
pr_max_files = 20
pr_max_loc = 750
pilot_gate_enabled = false
pilot_auto_relax_after_n_clean_merges = 5
require_tests_green = false

[profile]
name = "owner_gate"
version = "0.8.0"
override_label = "operator-locked"
""",
	)
	yaml_path = _write_yaml(
		tmp_path,
		"""
github:
  owner: chemnteach
  repo: sentinel
  default_assignee: octocat
  approver_team: owner-approvers

themes:
  primary_lens: structural
  conceptual_lens:
    stability_jaccard_threshold: 0.85

remediation:
  max_open_prs: 8
  pr_max_files: 20
  pr_max_loc: 750
  pilot_gate_enabled: false
  pilot_auto_relax_after_n_clean_merges: 5
  require_tests_green: false

profile:
  name: owner_gate
  version: "0.8.0"
  override_label: operator-locked
""",
	)
	assert load_workflow_config(toml_path) == load_workflow_config_yaml(yaml_path)


def test_load_yaml_minimal_config_matches_toml(tmp_path: Path) -> None:
	"""Equivalence case 2 of 3: minimal owner_gate config with only
	required keys. Exercises every default-population path in
	_build_config_from_dict so any per-loader asymmetry in default
	handling (e.g. YAML None propagating into ``str(...)``) surfaces."""
	toml_path = _write_config(
		tmp_path,
		"""
[github]
owner = "o"
repo = "r"

[profile]
name = "owner_gate"
""",
	)
	yaml_path = _write_yaml(
		tmp_path,
		"""
github:
  owner: o
  repo: r

profile:
  name: owner_gate
""",
	)
	assert load_workflow_config(toml_path) == load_workflow_config_yaml(yaml_path)


def test_load_yaml_solo_remediating_with_override_matches_toml(tmp_path: Path) -> None:
	"""Equivalence case 3 of 3: solo_local_remediating profile with
	explicit override_label. Exercises the second-profile branch in
	_default_profile and the override_label override path."""
	toml_path = _write_config(
		tmp_path,
		"""
[github]
owner = "solo-owner"
repo = "my-repo"

[profile]
name = "solo_local_remediating"
version = "0.8.0"
override_label = "operator-locked"
""",
	)
	yaml_path = _write_yaml(
		tmp_path,
		"""
github:
  owner: solo-owner
  repo: my-repo

profile:
  name: solo_local_remediating
  version: "0.8.0"
  override_label: operator-locked
""",
	)
	assert load_workflow_config(toml_path) == load_workflow_config_yaml(yaml_path)


def test_load_yaml_produces_expected_workflowconfig(tmp_path: Path) -> None:
	"""Inline-expected anchor: guards against the both-loaders-equally-
	wrong failure mode by constructing a WorkflowConfig in Python and
	asserting the YAML loader produces it. Independent of TOML behavior."""
	expected = WorkflowConfig(
		github=GitHubConfig(
			owner="o",
			repo="r",
			default_assignee="octocat",
			approver_team="owner-approvers",
		),
		themes=ThemesConfig(
			primary_lens="conceptual",
		),
		remediation=RemediationConfig(),
		profile=ProfileConfig(
			name="owner_gate",
			version="0.8.0",
			override_label="human-override",
			state_labels=frozenset({
				"triage",
				"owner-assessed",
				"owner-approved",
				"remediating",
				"pr-open",
				"wont-fix",
				"deferred",
				"closed",
			}),
			auto_label="sentinel-auto",
			transitions=_OWNER_GATE_TRANSITIONS,
		),
	)
	yaml_path = _write_yaml(
		tmp_path,
		"""
github:
  owner: o
  repo: r
  default_assignee: octocat
  approver_team: owner-approvers

profile:
  name: owner_gate
  version: "0.8.0"
""",
	)
	assert load_workflow_config_yaml(yaml_path) == expected


def test_load_yaml_raises_when_pyyaml_not_installed(
	tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
	"""Simulate PyYAML absence by setting sys.modules['yaml'] = None;
	per Python's import system that causes a subsequent ``import yaml``
	to raise ImportError. monkeypatch auto-reverts on teardown so the
	rest of the suite is unaffected. Valid YAML fixture written first
	so the error must come from the import guard, not file parsing."""
	import sys

	yaml_path = _write_yaml(
		tmp_path,
		"""
github:
  owner: o
  repo: r

profile:
  name: owner_gate
""",
	)
	monkeypatch.setitem(sys.modules, "yaml", None)
	with pytest.raises(WorkflowConfigError, match=r"PyYAML is required"):
		load_workflow_config_yaml(yaml_path)


def test_load_yaml_empty_file_raises_missing_section(tmp_path: Path) -> None:
	"""Verifies the ``data is None -> {}`` normalization branch in
	load_workflow_config_yaml: an empty YAML document parses to None
	(unlike empty TOML which parses to {}); without the normalization
	_build_config_from_dict would receive None and trip an attribute
	error rather than the documented WorkflowConfigError. This test
	passes only if the normalization is in place."""
	yaml_path = _write_yaml(tmp_path, "")
	with pytest.raises(WorkflowConfigError, match=r"missing required section \[github\]"):
		load_workflow_config_yaml(yaml_path)


def test_load_yaml_malformed_yaml_raises_workflow_config_error(tmp_path: Path) -> None:
	"""Malformed YAML must surface as a WorkflowConfigError naming the config
	path, not leak a raw yaml.YAMLError. (FIX-DESIRED-004)"""
	yaml_path = _write_yaml(tmp_path, "github: [unclosed\n")
	with pytest.raises(WorkflowConfigError, match=r"malformed YAML in workflow config"):
		load_workflow_config_yaml(yaml_path)


# ---------------------------------------------------------------------------
# Cycle 16 Item 22: DashboardConfig
# ---------------------------------------------------------------------------

import dataclasses as _dc_dash

from sentinel.workflow.config import DashboardConfig  # noqa: E402


_DASHBOARD_TOML_FULL = """
[github]
owner = "o"
repo = "r"

[dashboard]
primary_host = "github_branch"
mirror_host = "sharepoint"
branch = "dashboards/main"
output_root = "artifacts/dashboard/"
sharepoint_page_url = "https://example.sharepoint.com/sites/sentinel"
power_automate_flow = "flow-id-1234"
latest_scan_issue = "Sentinel current run"
audience = "owner_pilot"

[profile]
name = "owner_gate"
"""


_DASHBOARD_TOML_PARTIAL = """
[github]
owner = "o"
repo = "r"

[dashboard]
branch = "dashboards/staging"
audience = "owner_pilot"

[profile]
name = "owner_gate"
"""


_DASHBOARD_TOML_ABSENT = """
[github]
owner = "o"
repo = "r"

[profile]
name = "owner_gate"
"""


def test_default_dashboard_has_spec_example_defaults() -> None:
	"""WorkflowConfig.default() materializes DashboardConfig() so unconfigured
	runs inherit the documented Item-22 defaults."""
	config = WorkflowConfig.default()
	d = config.dashboard
	assert d.primary_host == "sharepoint"
	assert d.mirror_host == "github_branch"
	assert d.branch == "dashboard"
	assert d.output_root == "dashboard/"
	assert d.sharepoint_page_url == ""
	assert d.power_automate_flow == ""
	assert d.latest_scan_issue == "Latest Sentinel scan"
	assert d.audience == "owner_approver"


def test_load_toml_full_dashboard_section_populates_all_fields(tmp_path: Path) -> None:
	config = load_workflow_config(_write_config(tmp_path, _DASHBOARD_TOML_FULL))
	d = config.dashboard
	assert d.primary_host == "github_branch"
	assert d.mirror_host == "sharepoint"
	assert d.branch == "dashboards/main"
	assert d.output_root == "artifacts/dashboard/"
	assert d.sharepoint_page_url == "https://example.sharepoint.com/sites/sentinel"
	assert d.power_automate_flow == "flow-id-1234"
	assert d.latest_scan_issue == "Sentinel current run"
	assert d.audience == "owner_pilot"


def test_load_toml_absent_dashboard_section_yields_defaults(tmp_path: Path) -> None:
	config = load_workflow_config(_write_config(tmp_path, _DASHBOARD_TOML_ABSENT))
	assert config.dashboard == DashboardConfig()


def test_load_toml_partial_dashboard_section_merges_defaults(tmp_path: Path) -> None:
	"""Partial sections must apply defaults to unset fields."""
	config = load_workflow_config(_write_config(tmp_path, _DASHBOARD_TOML_PARTIAL))
	d = config.dashboard
	assert d.branch == "dashboards/staging"
	assert d.audience == "owner_pilot"
	assert d.primary_host == "sharepoint"
	assert d.mirror_host == "github_branch"
	assert d.output_root == "dashboard/"
	assert d.sharepoint_page_url == ""
	assert d.power_automate_flow == ""
	assert d.latest_scan_issue == "Latest Sentinel scan"


def test_load_yaml_full_dashboard_section_matches_toml(tmp_path: Path) -> None:
	toml_dir = tmp_path / "toml_dir"
	yaml_dir = tmp_path / "yaml_dir"
	toml_dir.mkdir()
	yaml_dir.mkdir()
	toml_path = _write_config(toml_dir, _DASHBOARD_TOML_FULL)
	yaml_path = _write_yaml(
		yaml_dir,
		"""
github:
  owner: o
  repo: r

dashboard:
  primary_host: github_branch
  mirror_host: sharepoint
  branch: dashboards/main
  output_root: artifacts/dashboard/
  sharepoint_page_url: https://example.sharepoint.com/sites/sentinel
  power_automate_flow: flow-id-1234
  latest_scan_issue: Sentinel current run
  audience: owner_pilot

profile:
  name: owner_gate
""",
	)
	assert load_workflow_config(toml_path) == load_workflow_config_yaml(yaml_path)


def test_load_yaml_absent_dashboard_section_matches_toml_defaults(tmp_path: Path) -> None:
	toml_dir = tmp_path / "toml_dir"
	yaml_dir = tmp_path / "yaml_dir"
	toml_dir.mkdir()
	yaml_dir.mkdir()
	toml_path = _write_config(toml_dir, _DASHBOARD_TOML_ABSENT)
	yaml_path = _write_yaml(
		yaml_dir,
		"""
github:
  owner: o
  repo: r

profile:
  name: owner_gate
""",
	)
	assert load_workflow_config(toml_path) == load_workflow_config_yaml(yaml_path)


def test_dashboard_config_is_frozen_with_slots() -> None:
	d = DashboardConfig()
	with pytest.raises(_dc_dash.FrozenInstanceError):
		d.primary_host = "x"  # type: ignore[misc]
	assert not hasattr(d, "__dict__")


def test_dashboard_config_to_dict_round_trips_all_keys() -> None:
	d = DashboardConfig(
		primary_host="github_branch",
		mirror_host="sharepoint",
		branch="b",
		output_root="o/",
		sharepoint_page_url="https://x",
		power_automate_flow="flow",
		latest_scan_issue="L",
		audience="a",
	)
	dumped = d.to_dict()
	assert set(dumped.keys()) == {
		"primary_host",
		"mirror_host",
		"branch",
		"output_root",
		"sharepoint_page_url",
		"power_automate_flow",
		"latest_scan_issue",
		"audience",
	}
	assert dumped["branch"] == "b"
	assert dumped["audience"] == "a"


# ---------------------------------------------------------------------------
# Cycle 18: Extended ThemesConfig — full spec §10.1 schema
# ---------------------------------------------------------------------------


from sentinel.workflow.config import (  # noqa: E402
	ConceptualLensConfig,
	HdbscanConfig,
	StructuralLensConfig,
)


def test_themes_full_nested_section_populates_all_fields(tmp_path: Path) -> None:
	"""All 11 spec §10.1 themes keys populated at non-default values under
	their spec-correct nested locations. Asserts every sub-config field."""
	config_path = _write_config(
		tmp_path,
		"""
[github]
owner = "o"
repo = "r"

[themes]
primary_lens = "structural"

[themes.structural_lens]
source = "alt_source"
title_strategy = "verbatim"

[themes.conceptual_lens]
enabled = false
embedding_model = "alt-model"
embedding_seed = 99
stability_jaccard_threshold = 0.42
title_strategy = "verbatim"

[themes.conceptual_lens.hdbscan]
min_cluster_size = 7
min_samples = 4
cluster_selection_epsilon = 0.33

[profile]
name = "owner_gate"
""",
	)
	config = load_workflow_config(config_path)
	assert config.themes.primary_lens == "structural"
	assert config.themes.structural_lens.source == "alt_source"
	assert config.themes.structural_lens.title_strategy == "verbatim"
	assert config.themes.conceptual_lens.enabled is False
	assert config.themes.conceptual_lens.embedding_model == "alt-model"
	assert config.themes.conceptual_lens.embedding_seed == 99
	assert config.themes.conceptual_lens.stability_jaccard_threshold == 0.42
	assert config.themes.conceptual_lens.title_strategy == "verbatim"
	assert config.themes.conceptual_lens.hdbscan.min_cluster_size == 7
	assert config.themes.conceptual_lens.hdbscan.min_samples == 4
	assert config.themes.conceptual_lens.hdbscan.cluster_selection_epsilon == 0.33


def test_themes_absent_section_yields_defaults(tmp_path: Path) -> None:
	"""No [themes] section: config.themes == ThemesConfig() (all defaults
	including nested factories)."""
	config_path = _write_config(
		tmp_path,
		"""
[github]
owner = "o"
repo = "r"

[profile]
name = "owner_gate"
""",
	)
	config = load_workflow_config(config_path)
	assert config.themes == ThemesConfig()


def test_themes_partial_structural_lens_only(tmp_path: Path) -> None:
	"""Only [themes.structural_lens] populated. conceptual_lens stays at
	defaults."""
	config_path = _write_config(
		tmp_path,
		"""
[github]
owner = "o"
repo = "r"

[themes.structural_lens]
source = "alt_source"

[profile]
name = "owner_gate"
""",
	)
	config = load_workflow_config(config_path)
	assert config.themes.structural_lens.source == "alt_source"
	assert config.themes.structural_lens.title_strategy == "synthesized"
	assert config.themes.conceptual_lens == ConceptualLensConfig()


def test_themes_partial_hdbscan_only(tmp_path: Path) -> None:
	"""Only [themes.conceptual_lens.hdbscan] populated; other conceptual_lens
	fields stay at defaults."""
	config_path = _write_config(
		tmp_path,
		"""
[github]
owner = "o"
repo = "r"

[themes.conceptual_lens.hdbscan]
min_cluster_size = 11

[profile]
name = "owner_gate"
""",
	)
	config = load_workflow_config(config_path)
	assert config.themes.conceptual_lens.hdbscan.min_cluster_size == 11
	assert config.themes.conceptual_lens.hdbscan.min_samples == 2
	assert config.themes.conceptual_lens.hdbscan.cluster_selection_epsilon == 0.15
	assert config.themes.conceptual_lens.enabled is True
	assert config.themes.conceptual_lens.embedding_model == (
		"sentence-transformers/all-MiniLM-L6-v2"
	)
	assert config.themes.conceptual_lens.embedding_seed == 42
	assert config.themes.conceptual_lens.stability_jaccard_threshold == 0.7
	assert config.themes.conceptual_lens.title_strategy == "synthesized"


def test_load_yaml_full_themes_matches_toml(tmp_path: Path) -> None:
	"""YAML format-equivalence for full themes section."""
	toml_path = _write_config(
		tmp_path,
		"""
[github]
owner = "o"
repo = "r"

[themes]
primary_lens = "structural"

[themes.structural_lens]
source = "alt_source"
title_strategy = "verbatim"

[themes.conceptual_lens]
enabled = false
embedding_model = "alt-model"
embedding_seed = 99
stability_jaccard_threshold = 0.42
title_strategy = "verbatim"

[themes.conceptual_lens.hdbscan]
min_cluster_size = 7
min_samples = 4
cluster_selection_epsilon = 0.33

[profile]
name = "owner_gate"
""",
	)
	yaml_path = _write_yaml(
		tmp_path,
		"""
github:
  owner: o
  repo: r

themes:
  primary_lens: structural
  structural_lens:
    source: alt_source
    title_strategy: verbatim
  conceptual_lens:
    enabled: false
    embedding_model: alt-model
    embedding_seed: 99
    stability_jaccard_threshold: 0.42
    title_strategy: verbatim
    hdbscan:
      min_cluster_size: 7
      min_samples: 4
      cluster_selection_epsilon: 0.33

profile:
  name: owner_gate
""",
	)
	assert load_workflow_config(toml_path) == load_workflow_config_yaml(yaml_path)


def test_themes_flat_stability_jaccard_silently_ignored(tmp_path: Path) -> None:
	"""Intentional silent-drop behavior: stability_jaccard_threshold under
	the old flat [themes] location (pre-cycle-18) is ignored. The
	spec-correct nested location is [themes.conceptual_lens]; only that
	location is read. This test pins the silent-drop as tested behavior,
	not accident."""
	config_path = _write_config(
		tmp_path,
		"""
[github]
owner = "o"
repo = "r"

[themes]
stability_jaccard_threshold = 0.5

[profile]
name = "owner_gate"
""",
	)
	config = load_workflow_config(config_path)
	# Old flat 0.5 ignored; nested default 0.7 wins.
	assert config.themes.conceptual_lens.stability_jaccard_threshold == 0.7


def test_themes_nested_stability_jaccard_loads_correctly(tmp_path: Path) -> None:
	"""Companion to the silent-drop test: when placed at the spec-correct
	nested location, the value loads."""
	config_path = _write_config(
		tmp_path,
		"""
[github]
owner = "o"
repo = "r"

[themes.conceptual_lens]
stability_jaccard_threshold = 0.5

[profile]
name = "owner_gate"
""",
	)
	config = load_workflow_config(config_path)
	assert config.themes.conceptual_lens.stability_jaccard_threshold == 0.5


def test_themes_config_to_dict_includes_nested_sub_configs() -> None:
	"""ThemesConfig().to_dict() returns dict with primary_lens,
	structural_lens (dict), conceptual_lens (dict with hdbscan sub-dict)."""
	dumped = ThemesConfig().to_dict()
	assert set(dumped.keys()) == {"primary_lens", "structural_lens", "conceptual_lens"}
	assert dumped["primary_lens"] == "conceptual"
	assert isinstance(dumped["structural_lens"], dict)
	assert dumped["structural_lens"]["source"] == "root_cause_id"
	assert dumped["structural_lens"]["title_strategy"] == "synthesized"
	assert isinstance(dumped["conceptual_lens"], dict)
	assert dumped["conceptual_lens"]["enabled"] is True
	assert dumped["conceptual_lens"]["embedding_seed"] == 42
	assert dumped["conceptual_lens"]["stability_jaccard_threshold"] == 0.7
	assert isinstance(dumped["conceptual_lens"]["hdbscan"], dict)
	assert dumped["conceptual_lens"]["hdbscan"]["min_cluster_size"] == 3
	assert dumped["conceptual_lens"]["hdbscan"]["min_samples"] == 2
	assert dumped["conceptual_lens"]["hdbscan"]["cluster_selection_epsilon"] == 0.15


def test_hdbscan_config_is_frozen_with_slots() -> None:
	"""Mirrors cycle-16 DashboardConfig frozen+slots test."""
	h = HdbscanConfig()
	with pytest.raises(dataclasses.FrozenInstanceError):
		h.min_cluster_size = 99  # type: ignore[misc]
	assert not hasattr(h, "__dict__")
	# Symmetry: confirm sibling configs are also frozen+slots.
	c = ConceptualLensConfig()
	with pytest.raises(dataclasses.FrozenInstanceError):
		c.embedding_seed = 1  # type: ignore[misc]
	assert not hasattr(c, "__dict__")
	s = StructuralLensConfig()
	with pytest.raises(dataclasses.FrozenInstanceError):
		s.source = "x"  # type: ignore[misc]
	assert not hasattr(s, "__dict__")


def test_state_labels_as_string_is_rejected(tmp_path: Path) -> None:
	"""A scalar string for state_labels must be rejected, not silently exploded
	into single characters by frozenset()."""
	config_path = _write_config(
		tmp_path,
		"""
[github]
owner = "o"
repo = "r"

[profile]
name = "owner_gate"
version = "0.8.0"
state_labels = "triage"
""",
	)
	with pytest.raises(WorkflowConfigError, match="state_labels"):
		load_workflow_config(config_path)


def test_state_labels_as_list_is_accepted(tmp_path: Path) -> None:
	"""A proper list of label strings is accepted verbatim."""
	config_path = _write_config(
		tmp_path,
		"""
[github]
owner = "o"
repo = "r"

[profile]
name = "owner_gate"
version = "0.8.0"
state_labels = ["triage", "closed"]
""",
	)
	config = load_workflow_config(config_path)
	assert config.profile.state_labels == frozenset({"triage", "closed"})
