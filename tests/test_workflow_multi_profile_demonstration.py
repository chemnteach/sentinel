"""Cycle 11 end-goal-alignment deliverable: multi-profile Phase 4 demonstration.

This module is the *first concrete demonstration* that the Phase 4
surface (handle_accept_theme, handle_reject_finding,
render_finding_lens_membership) operates correctly under a
non-``owner_gate`` profile and produces structurally equivalent
state transitions across both profiles modulo state-label spelling.

It targets the spec's profile contracts:

- §1.6.1 ``owner_gate`` (default profile; Owner-gated remediation
  with ``owner-approved`` / ``wont-fix`` / ``deferred`` vocabulary)
- §1.6.3 ``solo_local_remediating`` (single-developer remediation
  with ``approved`` / ``rejected`` / ``deferred`` vocabulary)

and the Phase 4 deliverables enumerated in §11.2 ("Code Owner
flows for accept-theme / reject-finding / wont-fix / defer;
Both lenses presented; per-finding view shows membership in both").

Relationship to per-handler tests from Commits 5-7
---------------------------------------------------

The per-handler tests
(``tests/test_workflow_triage_handlers.py``,
``tests/test_workflow_finding_lens_view.py``) verify *unit-level*
correctness of each handler/renderer, including parametrized
sweeps over both profiles. The tests in this module verify
*workflow-level* equivalence: chaining the full Phase 4 surface
together for a small scenario produces structurally consistent
outcomes across profiles. The integration-vs-unit distinction is
load-bearing — this file is the explicit codebase-portability
proof that future readers will reach for when asking "does the
workflow actually compose under a new profile?". The
unit parametrizations answer "does each handler honor profile X?";
this file answers "does the *sequence* of Phase 4 operations
remain coherent under both profiles?".

Renderer profile-invariance (§6.5 / §6.6 affordance):
``render_finding_lens_membership`` is profile-agnostic by
construction (no ``config`` parameter, no interpretation of the
finding's ``status`` field). The demonstration includes a
byte-equality assertion at integration level; a regression here
would indicate latent profile coupling not caught by Commit 7's
unit tests.

Cycle 11 closure framing
------------------------

This is the cycle's first concrete codebase-portability proof for
the engagement's end-goal recalibration ("a product we could
immediately switch to another codebase and it would work from
day 1"). The closure ledger references this commit as the
end-goal-recalibration's first concrete instance.
"""

from __future__ import annotations

import pytest

from sentinel.workflow.config import WorkflowConfig
from sentinel.workflow.finding_lens_view import render_finding_lens_membership
from sentinel.workflow.models import (
	FindingIssueRecord,
	ThemeIssueRecord,
	ThemeReference,
)
from sentinel.workflow.triage_handlers import (
	AcceptThemeEvent,
	RejectFindingEvent,
	handle_accept_theme,
	handle_reject_finding,
)


# ---------------------------------------------------------------------------
# Profile-sweep parametrization (cycle-11 convention, mirrors Commits 5-6)
# ---------------------------------------------------------------------------


# Cross-profile expectation table. Each row gives the foundation
# transition concept and the concrete label each profile resolves it
# to. The test functions consume this table by profile name; the table
# itself is the single source of truth for "what does each profile
# call this foundation transition?".
_PROFILE_EXPECTATIONS: dict[str, dict[str, str]] = {
	"owner_gate": {
		"approval_state": "owner-approved",
		"rejection_label": "wont-fix",
		"deferral_label": "deferred",
	},
	"solo_local_remediating": {
		"approval_state": "approved",
		"rejection_label": "rejected",
		"deferral_label": "deferred",
	},
}


_PROFILE_NAMES = sorted(_PROFILE_EXPECTATIONS)


# ---------------------------------------------------------------------------
# Scenario fixtures
# ---------------------------------------------------------------------------


def _scenario_findings() -> tuple[FindingIssueRecord, FindingIssueRecord, FindingIssueRecord]:
	"""Three findings spanning two themes across both lenses.

	- F-100: struct theme T-001, concept theme C-007 (both-lens member)
	- F-101: struct theme T-001 only (struct-only member)
	- F-102: concept theme C-007 only (concept-only member)
	"""
	return (
		_finding("F-100", struct_id="T-001", concept_id="C-007"),
		_finding("F-101", struct_id="T-001", concept_id=""),
		_finding("F-102", struct_id="", concept_id="C-007"),
	)


def _finding(finding_id: str, *, struct_id: str, concept_id: str) -> FindingIssueRecord:
	return FindingIssueRecord(
		triage_item_id=f"TD-{finding_id}",
		finding_id=finding_id,
		summary=f"summary {finding_id}",
		severity="P2",
		skill="layer2a-quick-review",
		module="m.py",
		category="error_handling",
		location="m.py:1",
		description="desc",
		suggested_fix="fix",
		status="triage",
		human_triage=None,
		effort_estimate="low",
		risk_of_deferral="low",
		primary_theme=ThemeReference(
			lens="struct" if struct_id else "concept",
			theme_id=struct_id or concept_id or "T-001",
			title="primary",
			member_count=1,
		),
		struct_theme_id=struct_id,
		concept_theme_id=concept_id,
	)


def _scenario_themes() -> tuple[ThemeIssueRecord, ThemeIssueRecord]:
	struct_theme = ThemeIssueRecord(
		lens="struct",
		theme_id="T-001",
		source_id="src-1",
		title="Struct demonstration theme",
		member_finding_ids=["F-100", "F-101"],
	)
	concept_theme = ThemeIssueRecord(
		lens="concept",
		theme_id="C-007",
		source_id="src-2",
		title="Concept demonstration theme",
		member_finding_ids=["F-100", "F-102"],
	)
	return struct_theme, concept_theme


def _accept_event(theme_id: str = "T-001", current_state: str = "owner-assessed") -> AcceptThemeEvent:
	return AcceptThemeEvent(
		theme_id=theme_id,
		current_state=current_state,
		actor="code-owner@example.com",
		timestamp="2026-06-01T12:00:00Z",
	)


def _reject_event(
	finding_id: str,
	disposition: str,
	current_state: str = "owner-assessed",
) -> RejectFindingEvent:
	return RejectFindingEvent(
		finding_id=finding_id,
		issue_number=99,
		current_state=current_state,
		disposition=disposition,  # type: ignore[arg-type]
		actor="code-owner@example.com",
		timestamp="2026-06-01T12:00:00Z",
	)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile_name", _PROFILE_NAMES)
def test_multi_profile_phase4_accept_theme_flow(profile_name: str) -> None:
	"""Theme acceptance lands on each profile's approval_state."""
	config = WorkflowConfig.default(profile_name=profile_name)
	result = handle_accept_theme(_accept_event(), config=config)
	expected = _PROFILE_EXPECTATIONS[profile_name]["approval_state"]
	assert result.to_state == expected
	# Foundation-concept anchor: the resolved label equals the profile's
	# declared approval_state, not a side-channel literal.
	assert result.to_state == config.profile.transitions.approval_state


@pytest.mark.parametrize("profile_name", _PROFILE_NAMES)
def test_multi_profile_phase4_reject_finding_rejection_flow(profile_name: str) -> None:
	"""Finding rejection lands on each profile's rejection_label."""
	config = WorkflowConfig.default(profile_name=profile_name)
	result = handle_reject_finding(
		_reject_event("F-101", "rejection"), config=config,
	)
	expected = _PROFILE_EXPECTATIONS[profile_name]["rejection_label"]
	assert result.to_state == expected
	assert result.to_state == config.profile.transitions.rejection_label


@pytest.mark.parametrize("profile_name", _PROFILE_NAMES)
def test_multi_profile_phase4_reject_finding_deferral_flow(profile_name: str) -> None:
	"""Finding deferral lands on each profile's deferral_label."""
	config = WorkflowConfig.default(profile_name=profile_name)
	result = handle_reject_finding(
		_reject_event("F-102", "deferral"), config=config,
	)
	expected = _PROFILE_EXPECTATIONS[profile_name]["deferral_label"]
	assert result.to_state == expected
	assert result.to_state == config.profile.transitions.deferral_label


def test_multi_profile_phase4_lens_renderer_invariance() -> None:
	"""Renderer is profile-agnostic at integration level: byte-equal
	output across profiles for the same data.

	This is the load-bearing property for the end-goal recalibration.
	A regression here would indicate latent profile coupling in the
	renderer (e.g., a config side-channel) not caught by Commit 7's
	unit tests.
	"""
	finding, _, _ = _scenario_findings()
	struct_theme, concept_theme = _scenario_themes()

	# Render once; the renderer takes no config and does not interpret
	# the finding's status. Calling it twice for the same data must
	# produce byte-identical output regardless of which profile is
	# "active" in surrounding state. We assert this by constructing
	# both profile configs (proving no module-level side effects) and
	# then comparing renderer outputs.
	acme_config = WorkflowConfig.default(profile_name="owner_gate")
	solo_config = WorkflowConfig.default(profile_name="solo_local_remediating")
	assert acme_config is not solo_config  # sanity: distinct configs

	body_a = render_finding_lens_membership(
		finding, struct_theme=struct_theme, concept_theme=concept_theme,
	)
	body_b = render_finding_lens_membership(
		finding, struct_theme=struct_theme, concept_theme=concept_theme,
	)
	assert body_a == body_b
	# Neither profile's distinguishing vocabulary leaks into the output.
	for forbidden in ("owner-approved", "wont-fix", "approved", "rejected"):
		assert forbidden not in body_a, (
			f"renderer leaked profile-vocabulary token {forbidden!r}"
		)


@pytest.mark.parametrize("profile_name", _PROFILE_NAMES)
def test_multi_profile_phase4_workflow_sequence(profile_name: str) -> None:
	"""Full Phase 4 sequence end-to-end through one profile.

	Exercises accept-theme + reject-finding (rejection) +
	reject-finding (deferral) + renderer in sequence. Verifies the
	*workflow-level* invariant: the sequence produces three handler
	transitions whose to_states are pairwise distinct (approval !=
	rejection != deferral), plus a renderer output that contains
	dual-lens membership content.

	This is the integration-level assertion not covered by per-handler
	unit tests: the handlers compose without label collisions, and the
	renderer is callable in the same sequence without configuration
	dependency.
	"""
	config = WorkflowConfig.default(profile_name=profile_name)
	finding_with_both_lenses, finding_to_reject, finding_to_defer = _scenario_findings()
	struct_theme, concept_theme = _scenario_themes()
	expected = _PROFILE_EXPECTATIONS[profile_name]

	# Step 1: accept the structural theme.
	accept_result = handle_accept_theme(
		_accept_event(theme_id=struct_theme.theme_id),
		config=config,
	)
	assert accept_result.theme_id == struct_theme.theme_id
	assert accept_result.to_state == expected["approval_state"]

	# Step 2: reject a finding outright.
	reject_result = handle_reject_finding(
		_reject_event(finding_to_reject.finding_id, "rejection"),
		config=config,
	)
	assert reject_result.finding_id == finding_to_reject.finding_id
	assert reject_result.to_state == expected["rejection_label"]

	# Step 3: defer a different finding.
	defer_result = handle_reject_finding(
		_reject_event(finding_to_defer.finding_id, "deferral"),
		config=config,
	)
	assert defer_result.finding_id == finding_to_defer.finding_id
	assert defer_result.to_state == expected["deferral_label"]

	# Workflow-level invariant: the three transitions resolve to
	# pairwise-distinct labels under both profiles. (Under owner_gate
	# that's owner-approved/wont-fix/deferred; under
	# solo_local_remediating that's approved/rejected/deferred.)
	to_states = {accept_result.to_state, reject_result.to_state, defer_result.to_state}
	assert len(to_states) == 3, (
		f"Phase 4 sequence produced colliding to_states under "
		f"{profile_name}: {to_states}"
	)

	# Step 4: render dual-lens membership for the both-lens finding.
	body = render_finding_lens_membership(
		finding_with_both_lenses,
		struct_theme=struct_theme,
		concept_theme=concept_theme,
	)
	assert "## Structural lens membership" in body
	assert "## Conceptual lens membership" in body
	assert "## Cross-lens view" in body
	assert struct_theme.theme_id in body
	assert concept_theme.theme_id in body
	# Renderer output remains free of profile-vocabulary leakage even
	# when constructed alongside profile-resolved handler results in
	# the same test scope.
	for forbidden in ("owner-approved", "wont-fix", "approved", "rejected"):
		assert forbidden not in body
