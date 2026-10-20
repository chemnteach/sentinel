"""Tests for sentinel.workflow.triage_handlers."""

from __future__ import annotations

import dataclasses

import pytest

from sentinel.workflow.config import (
	GitHubConfig,
	ProfileConfig,
	RemediationConfig,
	ThemesConfig,
	WorkflowConfig,
	WorkflowConfigError,
)
from sentinel.workflow.pr_feedback_handler import (
	FindingStateTransition,
	ThemeStateTransition,
)
from tests.conftest import advisory_config
from sentinel.workflow.triage_handlers import (
	AcceptThemeEvent,
	RejectFindingEvent,
	handle_accept_theme,
	handle_reject_finding,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _event(current_state: str = "triage", reason: str = "") -> AcceptThemeEvent:
	return AcceptThemeEvent(
		theme_id="T-042",
		current_state=current_state,
		actor="code-owner",
		timestamp="2026-06-01T12:34:56Z",
		reason=reason,
	)


def _solo_config() -> WorkflowConfig:
	return WorkflowConfig.default(profile_name="solo_local_remediating")


# ---------------------------------------------------------------------------
# Dataclass shape
# ---------------------------------------------------------------------------


def test_accept_theme_event_is_frozen_with_slots() -> None:
	event = _event()
	with pytest.raises(dataclasses.FrozenInstanceError):
		event.theme_id = "T-other"  # type: ignore[misc]


def test_accept_theme_event_reason_defaults_to_empty_string() -> None:
	event = AcceptThemeEvent(
		theme_id="T-1",
		current_state="triage",
		actor="code-owner",
		timestamp="2026-06-01T00:00:00Z",
	)
	assert event.reason == ""


def test_accept_theme_event_requires_core_fields() -> None:
	with pytest.raises(TypeError):
		AcceptThemeEvent(theme_id="T-1")  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# handle_accept_theme: format-compat / owner_gate
# ---------------------------------------------------------------------------


def test_handle_accept_theme_owner_gate_transitions_to_owner_approved() -> None:
	"""Cycle 11 Commit 1's test_owner_gate_transitions_default pinned
	that _OWNER_GATE_TRANSITIONS.approval_state == 'owner-approved'.
	Verify the Commit 5 handler emits that exact literal under
	owner_gate."""
	result = handle_accept_theme(_event(), config=WorkflowConfig.default())
	assert isinstance(result, ThemeStateTransition)
	assert result.theme_id == "T-042"
	assert result.from_state == "triage"
	assert result.to_state == "owner-approved"
	assert "theme accepted by code-owner at 2026-06-01T12:34:56Z" in result.reason


def test_handle_accept_theme_preserves_caller_supplied_from_state() -> None:
	"""``current_state`` is caller-supplied (pure-planning discipline);
	the handler passes it through verbatim regardless of profile."""
	result = handle_accept_theme(
		_event(current_state="owner-assessed"),
		config=WorkflowConfig.default(),
	)
	assert result.from_state == "owner-assessed"


def test_handle_accept_theme_embeds_optional_reason_into_transition_reason() -> None:
	result = handle_accept_theme(
		_event(reason="affects pilot SKU yield"),
		config=WorkflowConfig.default(),
	)
	assert result.reason == (
		"theme accepted by code-owner at 2026-06-01T12:34:56Z"
		" -- affects pilot SKU yield"
	)


def test_handle_accept_theme_omits_separator_when_reason_blank() -> None:
	result = handle_accept_theme(_event(reason=""), config=WorkflowConfig.default())
	assert result.reason == "theme accepted by code-owner at 2026-06-01T12:34:56Z"
	assert " -- " not in result.reason


# ---------------------------------------------------------------------------
# handle_accept_theme: solo_local_remediating
# ---------------------------------------------------------------------------


def test_handle_accept_theme_solo_local_remediating_transitions_to_approved() -> None:
	"""Under solo_local_remediating, approval_state == 'approved'
	(canonical synthesis per Commit 2 Q2 spec-divergence resolution)."""
	result = handle_accept_theme(_event(), config=_solo_config())
	assert result.to_state == "approved"


# ---------------------------------------------------------------------------
# handle_accept_theme: advisory profile + no-config fallback
# ---------------------------------------------------------------------------


def test_handle_accept_theme_advisory_profile_raises() -> None:
	with pytest.raises(WorkflowConfigError, match=r"declares no workflow transitions"):
		handle_accept_theme(_event(), config=advisory_config())


def test_handle_accept_theme_no_config_warns_and_falls_back_to_owner_gate() -> None:
	with pytest.warns(DeprecationWarning, match=r"WorkflowConfig not supplied"):
		result = handle_accept_theme(_event())
	assert result.to_state == "owner-approved"


# ---------------------------------------------------------------------------
# Parametrized profile sweep
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
	"profile_name, expected_to_state",
	[
		("owner_gate", "owner-approved"),
		("solo_local_remediating", "approved"),
	],
)
def test_handle_accept_theme_profile_sweep(
	profile_name: str, expected_to_state: str
) -> None:
	"""End-to-end profile sweep: handle_accept_theme returns the active
	profile's approval_state for every supported profile, with zero
	hardcoded owner_gate literals leaking through."""
	config = WorkflowConfig.default(profile_name=profile_name)
	result = handle_accept_theme(_event(), config=config)
	assert result.to_state == expected_to_state
	# Sanity: the resolved to_state equals the profile's declared
	# approval_state field (proves the handler reads from transitions,
	# not from a side-channel).
	assert result.to_state == config.profile.transitions.approval_state


# ---------------------------------------------------------------------------
# Cycle 11 Commit 6: handle_reject_finding (Phase 4 part B)
# ---------------------------------------------------------------------------


def _reject_event(
	disposition: str = "rejection",
	current_state: str = "owner-assessed",
	reason: str = "",
) -> RejectFindingEvent:
	return RejectFindingEvent(
		finding_id="F-007",
		issue_number=42,
		current_state=current_state,
		disposition=disposition,  # type: ignore[arg-type]
		actor="code-owner",
		timestamp="2026-06-01T13:00:00Z",
		reason=reason,
	)


def test_reject_finding_event_dataclass_shape() -> None:
	"""RejectFindingEvent is frozen+slots with the documented field set
	including the mandatory ``issue_number: int`` (matches
	FindingStateTransition's 5-field shape)."""
	event = RejectFindingEvent(
		finding_id="F-1",
		issue_number=10,
		current_state="owner-assessed",
		disposition="rejection",
		actor="alice",
		timestamp="2026-06-01T00:00:00Z",
	)
	# Required fields present.
	assert event.finding_id == "F-1"
	assert event.issue_number == 10
	assert isinstance(event.issue_number, int)
	assert event.current_state == "owner-assessed"
	assert event.disposition == "rejection"
	assert event.actor == "alice"
	assert event.timestamp == "2026-06-01T00:00:00Z"
	# reason default.
	assert event.reason == ""
	# Frozen.
	with pytest.raises(dataclasses.FrozenInstanceError):
		event.finding_id = "F-2"  # type: ignore[misc]
	# Slots (no __dict__).
	assert not hasattr(event, "__dict__")
	# Missing required fields raise TypeError.
	with pytest.raises(TypeError):
		RejectFindingEvent()  # type: ignore[call-arg]


def test_handle_reject_finding_owner_gate_rejection() -> None:
	config = WorkflowConfig.default(profile_name="owner_gate")
	result = handle_reject_finding(
		_reject_event(disposition="rejection"),
		config=config,
	)
	assert result.to_state == "wont-fix"
	assert result.from_state == "owner-assessed"
	assert result.finding_id == "F-007"
	assert result.issue_number == 42
	assert "rejected by code-owner" in result.reason


def test_handle_reject_finding_owner_gate_deferral() -> None:
	config = WorkflowConfig.default(profile_name="owner_gate")
	result = handle_reject_finding(
		_reject_event(disposition="deferral"),
		config=config,
	)
	assert result.to_state == "deferred"
	assert "deferred by code-owner" in result.reason


def test_handle_reject_finding_solo_local_rejection() -> None:
	config = WorkflowConfig.default(profile_name="solo_local_remediating")
	result = handle_reject_finding(
		_reject_event(disposition="rejection"),
		config=config,
	)
	assert result.to_state == "rejected"


def test_handle_reject_finding_solo_local_deferral() -> None:
	config = WorkflowConfig.default(profile_name="solo_local_remediating")
	result = handle_reject_finding(
		_reject_event(disposition="deferral"),
		config=config,
	)
	assert result.to_state == "deferred"


def test_handle_reject_finding_no_config_warns() -> None:
	"""Mirrors the AcceptThemeEvent fallback discipline: no config
	emits DeprecationWarning and falls back to owner_gate."""
	with pytest.warns(DeprecationWarning, match=r"WorkflowConfig not supplied"):
		result = handle_reject_finding(_reject_event(disposition="rejection"))
	assert result.to_state == "wont-fix"


def test_handle_reject_finding_advisory_profile_raises() -> None:
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
		handle_reject_finding(_reject_event(), config=advisory)


def test_handle_reject_finding_invalid_disposition_raises() -> None:
	config = WorkflowConfig.default(profile_name="owner_gate")
	with pytest.raises(ValueError, match=r"unknown disposition"):
		handle_reject_finding(
			_reject_event(disposition="bogus"),
			config=config,
		)


def test_handle_reject_finding_reuses_finding_state_transition() -> None:
	"""The handler returns the existing FindingStateTransition dataclass
	from pr_feedback_handler (Commit 5's ThemeStateTransition reuse
	pattern), not a new shape."""
	config = WorkflowConfig.default(profile_name="owner_gate")
	result = handle_reject_finding(_reject_event(), config=config)
	assert isinstance(result, FindingStateTransition)
	# 5-field shape preserved.
	dumped = result.to_dict()
	assert set(dumped.keys()) == {
		"finding_id",
		"issue_number",
		"from_state",
		"to_state",
		"reason",
	}


def test_handle_reject_finding_optional_reason_embedding() -> None:
	"""Reason embedding follows the Commit 5 convention: actor +
	timestamp + optional reason joined by ' -- '."""
	config = WorkflowConfig.default(profile_name="owner_gate")
	result = handle_reject_finding(
		_reject_event(disposition="rejection", reason="duplicate of F-3"),
		config=config,
	)
	assert result.reason == (
		"finding rejected by code-owner at 2026-06-01T13:00:00Z"
		" -- duplicate of F-3"
	)


@pytest.mark.parametrize(
	"profile_name,disposition,expected_to_state",
	[
		("owner_gate", "rejection", "wont-fix"),
		("owner_gate", "deferral", "deferred"),
		("solo_local_remediating", "rejection", "rejected"),
		("solo_local_remediating", "deferral", "deferred"),
	],
)
def test_handle_reject_finding_parametrized_profiles(
	profile_name: str, disposition: str, expected_to_state: str
) -> None:
	"""End-to-end profile sweep: handle_reject_finding returns the
	active profile's rejection_label/deferral_label for every
	supported (profile, disposition) pair, with zero hardcoded
	owner_gate literals leaking through."""
	config = WorkflowConfig.default(profile_name=profile_name)
	result = handle_reject_finding(
		_reject_event(disposition=disposition),
		config=config,
	)
	assert result.to_state == expected_to_state
	# Sanity: the resolved to_state equals the profile's declared
	# discriminator field (proves the handler reads from transitions).
	transitions = config.profile.transitions
	expected_field = (
		transitions.rejection_label
		if disposition == "rejection"
		else transitions.deferral_label
	)
	assert result.to_state == expected_field
