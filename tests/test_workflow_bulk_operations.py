"""Cycle 12 Commit 3: multi-profile parametrized tests for
:func:`sentinel.workflow.triage_handlers.handle_bulk_theme_member_action`.

This module is the codebase-portability proof for the Phase 4
"bulk operations on theme members" surface (§11.2). It mirrors the
fixture / parametrize / profile-expectation conventions of
``tests/test_workflow_multi_profile_demonstration.py`` (cycle 11
Commit 8); the two files together constitute the end-goal-recalibration
demonstration for Phase 4 (single-item flows there; bulk flows here).

Bulk operations are a distinct surface from the single-item handlers,
so they live in their own file rather than being appended to the
demonstration. The fixture pattern (``_PROFILE_EXPECTATIONS``,
``_PROFILE_NAMES``, parametrize over profile name + builder helpers)
matches the demonstration file exactly.

Every test in this module exercises the real
:func:`handle_bulk_theme_member_action` implementation; no test
stubs or mocks the handler itself. Test 2 monkeypatches
:func:`sentinel.workflow.config._resolve_transitions` to count
invocations -- the handler is still the real one -- so the
"called exactly once" invariant is observable without altering the
handler's behavior.
"""

from __future__ import annotations

import warnings

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
	BulkThemeMemberActionEvent,
	BulkThemeMemberActionResult,
	BulkTriageEligibilityError,
)
from sentinel.workflow.triage_handlers import handle_bulk_theme_member_action
from tests.conftest import advisory_config


# ---------------------------------------------------------------------------
# Profile-sweep parametrization (mirrors test_workflow_multi_profile_demonstration.py)
# ---------------------------------------------------------------------------


_PROFILE_EXPECTATIONS: dict[str, dict[str, str]] = {
	"owner_gate": {
		"approval_state": "owner-approved",
		"rejection_label": "wont-fix",
		"deferral_label": "deferred",
		# An accepted_states member shared with the foundation
		# concept "work in progress"; chosen here as the fixture
		# pre-bulk-action current_state because it is in
		# ``accepted_states`` under both profiles and so the same
		# fixture-builder helper works across profiles unchanged.
		"eligible_current_state": "remediating",
	},
	"solo_local_remediating": {
		"approval_state": "approved",
		"rejection_label": "rejected",
		"deferral_label": "deferred",
		"eligible_current_state": "remediating",
	},
}


_PROFILE_NAMES = sorted(_PROFILE_EXPECTATIONS)


_DISPOSITIONS = ("rejection", "deferral")


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _bulk_event(
	*,
	member_finding_ids: tuple[str, ...],
	current_state_overrides: dict[str, str] | None = None,
	missing_from_state_lookup: tuple[str, ...] = (),
	missing_from_issue_lookup: tuple[str, ...] = (),
	disposition: str = "rejection",
	eligible_current_state: str = "remediating",
	theme_id: str = "T-001",
	actor: str = "code-owner@example.com",
	timestamp: str = "2026-06-01T12:00:00Z",
	reason: str | None = None,
) -> BulkThemeMemberActionEvent:
	"""Build a :class:`BulkThemeMemberActionEvent` with explicit eligibility controls.

	- ``current_state_overrides`` lets a single member's current_state
	  be set to an ineligible value (e.g., ``"triage"``).
	- ``missing_from_state_lookup`` lists finding ids that should be
	  *excluded* from the ``finding_id_to_current_state`` dict.
	- ``missing_from_issue_lookup`` is the parallel control for
	  ``finding_id_to_issue_number``.
	By default all members are eligible (current_state = the profile's
	``eligible_current_state``, both lookup tables fully populated).
	"""
	current_state_overrides = current_state_overrides or {}
	state_lookup: dict[str, str] = {}
	issue_lookup: dict[str, int] = {}
	for index, finding_id in enumerate(member_finding_ids, start=1):
		if finding_id not in missing_from_state_lookup:
			state_lookup[finding_id] = current_state_overrides.get(
				finding_id, eligible_current_state,
			)
		if finding_id not in missing_from_issue_lookup:
			issue_lookup[finding_id] = 1000 + index
	return BulkThemeMemberActionEvent(
		theme_id=theme_id,
		member_finding_ids=member_finding_ids,
		finding_id_to_current_state=state_lookup,
		finding_id_to_issue_number=issue_lookup,
		disposition=disposition,  # type: ignore[arg-type]
		actor=actor,
		timestamp=timestamp,
		reason=reason,
	)


# ---------------------------------------------------------------------------
# Test 1: all-eligible bulk action emits N transitions on profile labels
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile_name", _PROFILE_NAMES)
@pytest.mark.parametrize("disposition", _DISPOSITIONS)
def test_multi_profile_bulk_action_all_eligible_emits_N_transitions(
	profile_name: str,
	disposition: str,
) -> None:
	"""5 eligible members -> 5 transitions, all landing on the
	profile's rejection_label or deferral_label per disposition.

	Parametrized over both profiles and both dispositions for 4
	cases total. The cross-product is the load-bearing assertion:
	bulk action is profile-portable *and* disposition-portable in a
	single dimension sweep.
	"""
	config = WorkflowConfig.default(profile_name=profile_name)
	expectations = _PROFILE_EXPECTATIONS[profile_name]
	member_ids = ("F-200", "F-201", "F-202", "F-203", "F-204")
	event = _bulk_event(
		member_finding_ids=member_ids,
		disposition=disposition,
		eligible_current_state=expectations["eligible_current_state"],
		reason="Quarterly bulk-rejection sweep",
	)

	result = handle_bulk_theme_member_action(event, config=config)

	assert isinstance(result, BulkThemeMemberActionResult)
	assert len(result.transitions) == 5
	expected_label = expectations[
		"rejection_label" if disposition == "rejection" else "deferral_label"
	]
	for transition, finding_id in zip(result.transitions, member_ids, strict=True):
		assert transition.finding_id == finding_id
		assert transition.to_state == expected_label
		assert transition.from_state == expectations["eligible_current_state"]


# ---------------------------------------------------------------------------
# Test 2: handler resolves transitions exactly once per call
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile_name", _PROFILE_NAMES)
def test_multi_profile_bulk_action_resolves_transitions_exactly_once(
	profile_name: str,
	monkeypatch: pytest.MonkeyPatch,
) -> None:
	"""A 5-member bulk call invokes ``_resolve_transitions`` exactly
	once. Per-member resolution would multiply the call count by 5.

	Monkeypatches the symbol *as imported into triage_handlers*
	(the module-local binding the handler actually calls) so the
	count reflects handler behavior, not the original
	:mod:`sentinel.workflow.config` symbol.
	"""
	import sentinel.workflow.triage_handlers as handlers_module
	from sentinel.workflow import config as config_module

	call_count = 0
	original = config_module._resolve_transitions

	def counting_resolve(config):  # type: ignore[no-untyped-def]
		nonlocal call_count
		call_count += 1
		return original(config)

	monkeypatch.setattr(handlers_module, "_resolve_transitions", counting_resolve)

	config = WorkflowConfig.default(profile_name=profile_name)
	event = _bulk_event(
		member_finding_ids=("F-300", "F-301", "F-302", "F-303", "F-304"),
		disposition="rejection",
		eligible_current_state=_PROFILE_EXPECTATIONS[profile_name][
			"eligible_current_state"
		],
	)

	result = handle_bulk_theme_member_action(event, config=config)

	assert call_count == 1, (
		f"_resolve_transitions called {call_count} times for a 5-member "
		"bulk call under "
		f"{profile_name}; expected exactly 1 (per-call resolution)."
	)
	assert len(result.transitions) == 5


# ---------------------------------------------------------------------------
# Test 3: strict-eligibility-gate (collect-all-failures, refuse-all)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile_name", _PROFILE_NAMES)
def test_multi_profile_bulk_action_strict_eligibility_gate_refuses_all(
	profile_name: str,
) -> None:
	"""Two ineligible members with DIFFERENT failure modes:

	- ``F-401`` is absent from ``finding_id_to_current_state``
	- ``F-403`` has ``current_state == "triage"`` (not in
	  ``accepted_states`` for either profile)

	The handler must:
	  1. raise :class:`BulkTriageEligibilityError`
	  2. carry a ``skipped`` tuple of length 2 (not 1; the
	     refuse-all policy collects both before raising)
	  3. include both finding ids in the skipped tuple
	  4. emit no transitions (verified by confirming the exception
	     path was taken, not the result path)

	A fixture with only one ineligible member cannot distinguish
	collect-all-failures from fail-fast; two failure modes are
	required.
	"""
	config = WorkflowConfig.default(profile_name=profile_name)
	expectations = _PROFILE_EXPECTATIONS[profile_name]
	member_ids = ("F-400", "F-401", "F-402", "F-403", "F-404")
	event = _bulk_event(
		member_finding_ids=member_ids,
		# F-401: absent from current_state lookup
		missing_from_state_lookup=("F-401",),
		# F-403: present in lookup but current_state not in accepted_states
		current_state_overrides={"F-403": "triage"},
		disposition="rejection",
		eligible_current_state=expectations["eligible_current_state"],
	)

	with pytest.raises(BulkTriageEligibilityError) as exc_info:
		handle_bulk_theme_member_action(event, config=config)

	error = exc_info.value
	assert len(error.skipped) == 2, (
		f"strict-eligibility-gate must collect ALL failures before raising; "
		f"got {len(error.skipped)} skipped members (expected 2)"
	)
	skipped_ids = {member.finding_id for member in error.skipped}
	assert skipped_ids == {"F-401", "F-403"}, (
		f"skipped tuple missing expected ineligible ids: got {skipped_ids}"
	)
	# Each skip reason includes the offending finding id so the
	# caller can correlate the failure mode without parsing positions.
	for member in error.skipped:
		assert member.finding_id in member.reason


# ---------------------------------------------------------------------------
# Test 3b: strict-eligibility-gate -- missing-from-issue-lookup branch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile_name", _PROFILE_NAMES)
def test_multi_profile_bulk_action_strict_eligibility_gate_collects_missing_issue_number(
	profile_name: str,
) -> None:
	"""Single-failure-mode coverage for the missing-from-issue-lookup
	branch of the strict-eligibility gate.

	Test 3 covers two of the three eligibility-gate branches
	(missing-from-state-lookup and current-state-not-in-accepted).
	The third branch -- a member present in current_state but absent
	from ``finding_id_to_issue_number`` -- is exercised here in
	isolation so that a regression which silently drops the
	issue-lookup check is caught directly rather than only via the
	collect-all assertion in Test 3.
	"""
	config = WorkflowConfig.default(profile_name=profile_name)
	expectations = _PROFILE_EXPECTATIONS[profile_name]
	member_ids = ("F-500", "F-501", "F-502")
	event = _bulk_event(
		member_finding_ids=member_ids,
		missing_from_issue_lookup=("F-501",),
		disposition="rejection",
		eligible_current_state=expectations["eligible_current_state"],
	)

	with pytest.raises(BulkTriageEligibilityError) as exc_info:
		handle_bulk_theme_member_action(event, config=config)

	error = exc_info.value
	assert len(error.skipped) == 1
	assert error.skipped[0].finding_id == "F-501"
	assert "not present in finding_id_to_issue_number" in error.skipped[0].reason


# ---------------------------------------------------------------------------
# Test 4: rejection and deferral land on disjoint label sets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile_name", _PROFILE_NAMES)
def test_multi_profile_bulk_action_rejection_deferral_pairwise_distinct(
	profile_name: str,
) -> None:
	"""Two separate bulk calls over disjoint member subsets (3
	rejection + 2 deferral). The two to_state sets must be disjoint
	under both profiles; rejection_label != deferral_label is the
	foundation invariant the bulk surface preserves end-to-end.
	"""
	config = WorkflowConfig.default(profile_name=profile_name)
	expectations = _PROFILE_EXPECTATIONS[profile_name]
	rejection_members = ("F-500", "F-501", "F-502")
	deferral_members = ("F-503", "F-504")
	rejection_event = _bulk_event(
		member_finding_ids=rejection_members,
		disposition="rejection",
		eligible_current_state=expectations["eligible_current_state"],
	)
	deferral_event = _bulk_event(
		member_finding_ids=deferral_members,
		disposition="deferral",
		eligible_current_state=expectations["eligible_current_state"],
	)

	rejection_result = handle_bulk_theme_member_action(
		rejection_event, config=config,
	)
	deferral_result = handle_bulk_theme_member_action(
		deferral_event, config=config,
	)

	rejection_to_states = {t.to_state for t in rejection_result.transitions}
	deferral_to_states = {t.to_state for t in deferral_result.transitions}
	assert rejection_to_states.isdisjoint(deferral_to_states), (
		f"rejection and deferral collapsed to the same to_state under "
		f"{profile_name}: rejection={rejection_to_states}, "
		f"deferral={deferral_to_states}"
	)
	assert rejection_to_states == {expectations["rejection_label"]}
	assert deferral_to_states == {expectations["deferral_label"]}


# ---------------------------------------------------------------------------
# Test 5: advisory profile raises WorkflowConfigError (not parametrized)
# ---------------------------------------------------------------------------


def test_multi_profile_bulk_action_advisory_profile_raises_config_error() -> None:
	"""Invoking the bulk handler with an advisory-profile config
	(``transitions=None``) raises :class:`WorkflowConfigError` via
	:func:`_resolve_transitions`. Advisory profiles publish no
	persistent workflow states (§1.6.2); bulk actions cannot
	operate on them.

	Not parametrized over ``_PROFILE_EXPECTATIONS`` -- advisory is a
	separate profile contract surfaced from the remediating profiles
	the rest of this module sweeps.
	"""
	event = _bulk_event(
		member_finding_ids=("F-600", "F-601"),
		disposition="rejection",
	)
	with pytest.raises(WorkflowConfigError):
		handle_bulk_theme_member_action(event, config=advisory_config())


# ---------------------------------------------------------------------------
# Test 6: no-config fallback emits exactly one DeprecationWarning
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile_name", _PROFILE_NAMES)
def test_multi_profile_bulk_action_no_config_emits_single_deprecation_warning(
	profile_name: str,
) -> None:
	"""Invoking with ``config=None`` triggers the three-state
	no-config-fallback discipline: exactly one ``DeprecationWarning``
	is emitted (the fallback resolves to owner_gate regardless of
	caller-side profile expectation).

	``profile_name`` is parametrized for parity with the rest of the
	module; the no-config path always falls back to owner_gate
	independent of the parameter. The warning count assertion is
	the invariant under test.
	"""
	# Reference the parameter so the parametrize sweep is observable;
	# functionally the no-config path is independent of profile_name.
	assert profile_name in _PROFILE_EXPECTATIONS
	event = _bulk_event(
		member_finding_ids=("F-700", "F-701", "F-702"),
		disposition="rejection",
		# owner_gate's eligible state; the no-config fallback resolves
		# to owner_gate so its accepted_states is what gates eligibility.
		eligible_current_state="remediating",
	)

	with warnings.catch_warnings(record=True) as captured:
		warnings.simplefilter("always")
		result = handle_bulk_theme_member_action(event, config=None)

	deprecation_warnings = [
		w for w in captured if issubclass(w.category, DeprecationWarning)
	]
	assert len(deprecation_warnings) == 1, (
		f"expected exactly one DeprecationWarning from no-config fallback; "
		f"got {len(deprecation_warnings)}: "
		f"{[str(w.message) for w in deprecation_warnings]}"
	)
	# Sanity: the call still produced a result (fallback resolution
	# succeeded), so the warning is the only side effect.
	assert len(result.transitions) == 3
