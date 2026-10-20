from sentinel.workflow.models import (
    CrossLensReference,
    FindingIssueRecord,
    IssueClosurePlan,
    ThemeIssueRecord,
    ThemeMember,
    ThemeReference,
)


def test_finding_issue_record_to_dict_preserves_joined_fields() -> None:
    record = FindingIssueRecord(
        triage_item_id="TD-0001",
        finding_id="F-123456789abc",
        summary="Unchecked Optional return from get_wafer_state",
        severity="P1",
        skill="layer2a-quick-review",
        module="handlers/wafer_state.py",
        category="error_handling",
        location="handlers/wafer_state.py:147",
        description="Unchecked Optional return from get_wafer_state can raise at runtime.",
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

    data = record.to_dict()

    assert data["triage_item_id"] == "TD-0001"
    assert data["finding_id"] == "F-123456789abc"
    assert data["primary_theme"]["theme_id"] == "T-001"
    assert data["struct_theme_id"] == "T-001"
    assert data["concept_theme_id"] == "C-007"
    assert data["layers"] == ["layer2a-quick-review", "layer3d-error-handling-specialist"]
    assert data["needs_human_review"] is True


def test_theme_issue_record_to_dict_sorts_sets_for_deterministic_output() -> None:
    record = ThemeIssueRecord(
        lens="concept",
        theme_id="C-007",
        source_id="C-007",
        title="Defensive null handling omissions",
        member_finding_ids=["F-123456789abc", "F-fedcba987654"],
        skills_involved={"layer3d-error-handling-specialist", "layer2a-quick-review"},
        categories={"correctness", "error_handling"},
        modules_affected={"queries/wafer_repo.py", "handlers/wafer_state.py"},
        aggregate_severity="P1",
        aggregate_effort="medium",
        aggregate_risk_of_deferral="high — likely to cause production issues",
        cross_lens_themes=[
            CrossLensReference(
                theme_id="T-001",
                title="Unchecked Optional returns from wafer-state queries",
                issue_url="https://github.com/chemnteach/sentinel/issues/42",
            )
        ],
        root_cause_summary="The codebase consistently omits defensive handling for Optional query results.",
        remediation_strategy="Introduce a shared null-safe access helper and update direct callers.",
        members=[
            ThemeMember(
                finding_id="F-123456789abc",
                severity="P1",
                file_path="handlers/wafer_state.py",
                line_range="147",
                skill="layer2a-quick-review",
                other_lens_theme_id="T-001",
                summary="Unchecked Optional return from get_wafer_state",
            )
        ],
    )

    data = record.to_dict()

    assert data["skills_involved"] == [
        "layer2a-quick-review",
        "layer3d-error-handling-specialist",
    ]
    assert data["categories"] == ["correctness", "error_handling"]
    assert data["modules_affected"] == [
        "handlers/wafer_state.py",
        "queries/wafer_repo.py",
    ]
    assert data["cross_lens_themes"][0]["theme_id"] == "T-001"

def test_issue_closure_plan_supports_all_documented_reasons() -> None:
    for reason in ("no-longer-reproduces", "pr-merged", "superseded"):
        plan = IssueClosurePlan(issue_number=101, reason=reason)
        assert plan.reason == reason
        assert plan.successor_issue_number is None
        assert plan.to_dict() == {
            "issue_number": 101,
            "reason": reason,
            "successor_issue_number": None,
        }


def test_issue_closure_plan_carries_successor_for_supersede() -> None:
    plan = IssueClosurePlan(
        issue_number=42,
        reason="superseded",
        successor_issue_number=99,
    )
    assert plan.successor_issue_number == 99
    assert plan.to_dict()["successor_issue_number"] == 99


def test_issue_closure_plan_is_frozen() -> None:
    import dataclasses

    import pytest

    plan = IssueClosurePlan(issue_number=1, reason="no-longer-reproduces")
    with pytest.raises(dataclasses.FrozenInstanceError):
        plan.issue_number = 2  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Phase 4 bulk-operation dataclass unit tests (cycle 12 Commit 1).
#
# These tests cover construction, immutability, and field-type fidelity
# for the new bulk-action payload, result, and skipped-member records,
# plus the :class:`BulkTriageEligibilityError` exception. Handler-level
# semantics (eligibility gate, transition emission, profile portability)
# are exercised separately by the cycle 12 Commit 3 multi-profile
# parametrized tests against
# :func:`sentinel.workflow.triage_handlers.handle_bulk_theme_member_action`.
# ---------------------------------------------------------------------------


def test_bulk_theme_member_action_event_constructs_with_all_fields() -> None:
    from sentinel.workflow.models import BulkThemeMemberActionEvent

    event = BulkThemeMemberActionEvent(
        theme_id="T-001",
        member_finding_ids=("F-001", "F-002", "F-003"),
        finding_id_to_current_state={
            "F-001": "accepted",
            "F-002": "accepted",
            "F-003": "accepted",
        },
        finding_id_to_issue_number={"F-001": 42, "F-002": 43, "F-003": 44},
        disposition="rejection",
        actor="code-owner-alice",
        timestamp="2026-06-01T12:00:00Z",
        reason="Out of scope for current quarter",
    )

    assert event.theme_id == "T-001"
    assert event.member_finding_ids == ("F-001", "F-002", "F-003")
    assert event.finding_id_to_current_state["F-002"] == "accepted"
    assert event.finding_id_to_issue_number["F-003"] == 44
    assert event.disposition == "rejection"
    assert event.actor == "code-owner-alice"
    assert event.timestamp == "2026-06-01T12:00:00Z"
    assert event.reason == "Out of scope for current quarter"


def test_bulk_theme_member_action_event_reason_defaults_to_none() -> None:
    from sentinel.workflow.models import BulkThemeMemberActionEvent

    event = BulkThemeMemberActionEvent(
        theme_id="T-001",
        member_finding_ids=("F-001",),
        finding_id_to_current_state={"F-001": "accepted"},
        finding_id_to_issue_number={"F-001": 42},
        disposition="deferral",
        actor="code-owner-bob",
        timestamp="2026-06-01T12:00:00Z",
    )

    assert event.reason is None


def test_bulk_theme_member_action_event_is_frozen() -> None:
    import dataclasses

    import pytest

    from sentinel.workflow.models import BulkThemeMemberActionEvent

    event = BulkThemeMemberActionEvent(
        theme_id="T-001",
        member_finding_ids=("F-001",),
        finding_id_to_current_state={"F-001": "accepted"},
        finding_id_to_issue_number={"F-001": 42},
        disposition="rejection",
        actor="code-owner-alice",
        timestamp="2026-06-01T12:00:00Z",
    )

    with pytest.raises(dataclasses.FrozenInstanceError):
        event.theme_id = "T-002"  # type: ignore[misc]


def test_bulk_skipped_member_constructs_and_is_frozen() -> None:
    import dataclasses

    import pytest

    from sentinel.workflow.models import BulkSkippedMember

    skipped = BulkSkippedMember(
        finding_id="F-007",
        reason="finding F-007 not present in finding_id_to_current_state",
    )

    assert skipped.finding_id == "F-007"
    assert skipped.reason.startswith("finding F-007 not present")

    with pytest.raises(dataclasses.FrozenInstanceError):
        skipped.finding_id = "F-008"  # type: ignore[misc]


def test_bulk_theme_member_action_result_carries_transitions_tuple() -> None:
    from sentinel.workflow.models import BulkThemeMemberActionResult
    from sentinel.workflow.pr_feedback_handler import FindingStateTransition

    transitions = (
        FindingStateTransition(
            finding_id="F-001",
            issue_number=42,
            from_state="accepted",
            to_state="wont-fix",
            reason="bulk-rejection -- Out of scope",
        ),
        FindingStateTransition(
            finding_id="F-002",
            issue_number=43,
            from_state="accepted",
            to_state="wont-fix",
            reason="bulk-rejection -- Out of scope",
        ),
    )

    result = BulkThemeMemberActionResult(transitions=transitions)

    assert isinstance(result.transitions, tuple)
    assert len(result.transitions) == 2
    assert result.transitions[0].finding_id == "F-001"
    assert result.transitions[1].to_state == "wont-fix"


def test_bulk_theme_member_action_result_is_frozen() -> None:
    import dataclasses

    import pytest

    from sentinel.workflow.models import BulkThemeMemberActionResult

    result = BulkThemeMemberActionResult(transitions=())

    with pytest.raises(dataclasses.FrozenInstanceError):
        result.transitions = ()  # type: ignore[misc]


def test_bulk_triage_eligibility_error_carries_skipped_tuple_and_message() -> None:
    from sentinel.workflow.models import (
        BulkSkippedMember,
        BulkTriageEligibilityError,
    )

    skipped = (
        BulkSkippedMember(
            finding_id="F-009",
            reason="finding F-009 not present in finding_id_to_current_state",
        ),
        BulkSkippedMember(
            finding_id="F-010",
            reason="finding F-010 current_state 'rejected' is not in accepted_states",
        ),
    )

    error = BulkTriageEligibilityError(skipped=skipped)

    assert error.skipped is skipped
    assert len(error.skipped) == 2
    assert "F-009" in str(error)
    assert "F-010" in str(error)
    assert "2 ineligible members" in str(error)


def test_bulk_triage_eligibility_error_singular_member_message() -> None:
    from sentinel.workflow.models import (
        BulkSkippedMember,
        BulkTriageEligibilityError,
    )

    error = BulkTriageEligibilityError(
        skipped=(BulkSkippedMember(finding_id="F-011", reason="ineligible"),)
    )

    message = str(error)
    assert "F-011" in message
    assert "1 ineligible member [F-011]" in message
    assert "1 ineligible members" not in message


def test_bulk_triage_eligibility_error_is_an_exception() -> None:
    from sentinel.workflow.models import BulkTriageEligibilityError

    assert issubclass(BulkTriageEligibilityError, Exception)


def test_disposition_is_reexported_from_triage_handlers_module() -> None:
    """Cycle 12 Commit 1 relocated Disposition's canonical definition
    to models.py; the triage_handlers re-export must still resolve to
    the same type alias for backward compatibility."""
    from sentinel.workflow.models import Disposition as DispositionFromModels
    from sentinel.workflow.triage_handlers import (
        Disposition as DispositionFromTriageHandlers,
    )

    assert DispositionFromModels is DispositionFromTriageHandlers
