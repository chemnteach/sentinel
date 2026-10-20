"""Typed workflow models for findings, triage items, themes, and issues."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


Lens = Literal["struct", "concept"]


# Disposition discriminator for finding-level rejection actions.
# Originally introduced in cycle 11 Commit 6 (``handle_reject_finding``)
# and relocated here in cycle 12 Commit 1 to keep it accessible to both
# the single-item handler (:mod:`sentinel.workflow.triage_handlers`) and
# the bulk-action event payload (:class:`BulkThemeMemberActionEvent`)
# without introducing a circular import (models -> triage_handlers).
# :mod:`sentinel.workflow.triage_handlers` re-exports ``Disposition``
# for backward compatibility with callers that imported it from there.
Disposition = Literal["rejection", "deferral"]


def _validate_lens(lens: Lens) -> None:
	if lens not in {"struct", "concept"}:
		raise ValueError(f"Unsupported workflow lens: {lens}")


def _sorted(values: set[str]) -> list[str]:
	return sorted(values)


@dataclass(frozen=True, slots=True)
class ThemeReference:
	"""Single-theme context attached to a finding issue body."""

	lens: Lens
	theme_id: str
	title: str
	member_count: int
	issue_url: str | None = None

	def __post_init__(self) -> None:
		_validate_lens(self.lens)

	def to_dict(self) -> dict[str, Any]:
		return {
			"lens": self.lens,
			"theme_id": self.theme_id,
			"title": self.title,
			"member_count": self.member_count,
			"issue_url": self.issue_url,
		}


@dataclass(frozen=True, slots=True)
class CrossLensReference:
	"""Link to the corresponding theme in the other lens."""

	theme_id: str
	title: str
	issue_url: str | None = None

	def to_dict(self) -> dict[str, Any]:
		return {
			"theme_id": self.theme_id,
			"title": self.title,
			"issue_url": self.issue_url,
		}


@dataclass(frozen=True, slots=True)
class ThemeMember:
	"""Rendered row for a theme issue's member table."""

	finding_id: str
	severity: str
	file_path: str
	line_range: str
	skill: str
	other_lens_theme_id: str
	summary: str

	def to_dict(self) -> dict[str, Any]:
		return {
			"finding_id": self.finding_id,
			"severity": self.severity,
			"file_path": self.file_path,
			"line_range": self.line_range,
			"skill": self.skill,
			"other_lens_theme_id": self.other_lens_theme_id,
			"summary": self.summary,
		}


@dataclass(frozen=True, slots=True)
class FindingIssueRecord:
	"""Joined raw finding plus Stage 5 triage state for issue emission."""

	triage_item_id: str
	finding_id: str
	summary: str
	severity: str
	skill: str
	module: str
	category: str
	location: str
	description: str
	suggested_fix: str
	status: str
	human_triage: str | None
	effort_estimate: str
	risk_of_deferral: str
	primary_theme: ThemeReference
	struct_theme_id: str
	concept_theme_id: str
	root_cause_id: str = ""
	file: str = ""
	function: str = ""
	line_range: str = ""
	layers: tuple[str, ...] = ()
	consolidated_from: int = 1
	lane_provenance: str = ""
	needs_human_review: bool = False
	first_seen: str = ""

	def to_dict(self) -> dict[str, Any]:
		return {
			"triage_item_id": self.triage_item_id,
			"finding_id": self.finding_id,
			"summary": self.summary,
			"severity": self.severity,
			"skill": self.skill,
			"module": self.module,
			"category": self.category,
			"location": self.location,
			"description": self.description,
			"suggested_fix": self.suggested_fix,
			"status": self.status,
			"human_triage": self.human_triage,
			"effort_estimate": self.effort_estimate,
			"risk_of_deferral": self.risk_of_deferral,
			"primary_theme": self.primary_theme.to_dict(),
			"struct_theme_id": self.struct_theme_id,
			"concept_theme_id": self.concept_theme_id,
			"root_cause_id": self.root_cause_id,
			"file": self.file,
			"function": self.function,
			"line_range": self.line_range,
			"layers": list(self.layers),
			"consolidated_from": self.consolidated_from,
			"lane_provenance": self.lane_provenance,
			"needs_human_review": self.needs_human_review,
			"first_seen": self.first_seen,
		}


@dataclass(slots=True)
class ThemeIssueRecord:
	"""Theme rollup record for structural or conceptual issue rendering."""

	lens: Lens
	theme_id: str
	source_id: str
	title: str
	member_finding_ids: list[str] = field(default_factory=list)
	skills_involved: set[str] = field(default_factory=set)
	categories: set[str] = field(default_factory=set)
	modules_affected: set[str] = field(default_factory=set)
	aggregate_severity: str = "P3"
	aggregate_effort: str = "low"
	aggregate_risk_of_deferral: str = ""
	cross_lens_themes: list[CrossLensReference] = field(default_factory=list)
	root_cause_summary: str = ""
	remediation_strategy: str = ""
	members: list[ThemeMember] = field(default_factory=list)
	state: str = "triage"

	def __post_init__(self) -> None:
		_validate_lens(self.lens)

	def to_dict(self) -> dict[str, Any]:
		return {
			"lens": self.lens,
			"theme_id": self.theme_id,
			"source_id": self.source_id,
			"title": self.title,
			"member_finding_ids": list(self.member_finding_ids),
			"skills_involved": _sorted(self.skills_involved),
			"categories": _sorted(self.categories),
			"modules_affected": _sorted(self.modules_affected),
			"aggregate_severity": self.aggregate_severity,
			"aggregate_effort": self.aggregate_effort,
			"aggregate_risk_of_deferral": self.aggregate_risk_of_deferral,
			"cross_lens_themes": [theme.to_dict() for theme in self.cross_lens_themes],
			"root_cause_summary": self.root_cause_summary,
			"remediation_strategy": self.remediation_strategy,
			"members": [member.to_dict() for member in self.members],
			"state": self.state,
		}


@dataclass(frozen=True, slots=True)
class ConceptTheme:
	"""Persisted conceptual theme record for auditability across scans."""

	concept_theme_id: str
	title: str
	root_cause_summary: str
	remediation_strategy: str
	member_finding_ids: list[str] = field(default_factory=list)
	skills_involved: set[str] = field(default_factory=set)
	categories: set[str] = field(default_factory=set)
	files_affected: set[str] = field(default_factory=set)
	aggregate_severity: str = "P3"
	aggregate_effort: str = "low"
	aggregate_risk_of_deferral: str = ""
	superseded_by: str | None = None

	def to_dict(self) -> dict[str, Any]:
		return {
			"concept_theme_id": self.concept_theme_id,
			"title": self.title,
			"root_cause_summary": self.root_cause_summary,
			"remediation_strategy": self.remediation_strategy,
			"member_finding_ids": list(self.member_finding_ids),
			"skills_involved": _sorted(self.skills_involved),
			"categories": _sorted(self.categories),
			"files_affected": _sorted(self.files_affected),
			"aggregate_severity": self.aggregate_severity,
			"aggregate_effort": self.aggregate_effort,
			"aggregate_risk_of_deferral": self.aggregate_risk_of_deferral,
			"superseded_by": self.superseded_by,
		}

	@classmethod
	def from_dict(cls, data: dict[str, Any]) -> "ConceptTheme":
		return cls(
			concept_theme_id=data["concept_theme_id"],
			title=data.get("title", ""),
			root_cause_summary=data.get("root_cause_summary", ""),
			remediation_strategy=data.get("remediation_strategy", ""),
			member_finding_ids=list(data.get("member_finding_ids", [])),
			skills_involved=set(data.get("skills_involved", [])),
			categories=set(data.get("categories", [])),
			files_affected=set(data.get("files_affected", [])),
			aggregate_severity=data.get("aggregate_severity", "P3"),
			aggregate_effort=data.get("aggregate_effort", "low"),
			aggregate_risk_of_deferral=data.get("aggregate_risk_of_deferral", ""),
			superseded_by=data.get("superseded_by"),
		)


@dataclass(frozen=True, slots=True)
class GitHubIssueSnapshot:
	"""Minimal GitHub issue projection used by sync planning logic."""

	issue_number: int
	title: str
	body: str
	labels: tuple[str, ...] = ()
	html_url: str | None = None

	def to_dict(self) -> dict[str, Any]:
		return {
			"issue_number": self.issue_number,
			"title": self.title,
			"body": self.body,
			"labels": list(self.labels),
			"html_url": self.html_url,
		}


@dataclass(frozen=True, slots=True)
class IssueUpsertPlan:
	"""Desired idempotent GitHub issue mutation outcome."""

	action: Literal["create", "update", "skip"]
	title: str
	body: str
	labels: list[str] = field(default_factory=list)
	issue_number: int | None = None
	issue_url: str | None = None

	def to_dict(self) -> dict[str, Any]:
		return {
			"action": self.action,
			"title": self.title,
			"body": self.body,
			"labels": list(self.labels),
			"issue_number": self.issue_number,
			"issue_url": self.issue_url,
		}


ClosureReason = Literal["no-longer-reproduces", "pr-merged", "superseded"]


DriftKind = Literal[
	"skipped-closure",
	"skipped-upsert",
	"skipped-projection",
	"field-disagreement",
]


@dataclass(frozen=True, slots=True)
class FieldDrift:
	"""One field-level disagreement between Sentinel state and GitHub state.

	Carried by ``DriftReport.drift_details``. ``sentinel_value`` is the
	scan-derived value Sentinel would write; ``github_value`` is what
	currently lives on the GitHub issue (parsed from labels or body).
	Values are stringified for audit-friendly serialization.
	"""

	field_name: str
	sentinel_value: str
	github_value: str

	def to_dict(self) -> dict[str, Any]:
		return {
			"field_name": self.field_name,
			"sentinel_value": self.sentinel_value,
			"github_value": self.github_value,
		}


@dataclass(frozen=True, slots=True)
class DriftReport:
	"""Structured drift surfacing per spec section 7.3.

	Emitted by reconcile-path planning when:
	  * ``skipped-closure`` -- ``plan_finding_closures`` would have
		closed a finding issue but the human-override marker is present.
	  * ``skipped-upsert`` -- ``plan_finding_issue_upsert`` would have
		mutated the issue but the marker is present.
	  * ``skipped-projection`` -- ``reconcile_finding_projection`` would
		have overwritten the local record from GitHub state but the
		marker is present.
	  * ``field-disagreement`` -- the unwired ``detect_drift`` primitive
		reports per-field disagreements between scan-derived facts and
		current GitHub state, for future per-field work and the Phase 3
		dashboard's human-override summary section.

	Commit 4 (Gap B) lands this as the structured replacement for the
	INFO log Commit 1 used; commit 5 (Gap C) will source the override
	label from profile config so the skip sites can substitute it.
	"""

	finding_id: str
	issue_number: int
	drift_kind: DriftKind
	drift_details: tuple[FieldDrift, ...] = ()
	override_label_present: bool = False

	def to_dict(self) -> dict[str, Any]:
		return {
			"finding_id": self.finding_id,
			"issue_number": self.issue_number,
			"drift_kind": self.drift_kind,
			"drift_details": [detail.to_dict() for detail in self.drift_details],
			"override_label_present": self.override_label_present,
		}


@dataclass(frozen=True, slots=True)
class FindingClosurePlanningResult:
	"""Result of ``plan_finding_closures`` carrying plans + drift reports."""

	closure_plans: tuple[IssueClosurePlan, ...] = ()
	drift_reports: tuple[DriftReport, ...] = ()

	def to_dict(self) -> dict[str, Any]:
		return {
			"closure_plans": [plan.to_dict() for plan in self.closure_plans],
			"drift_reports": [report.to_dict() for report in self.drift_reports],
		}


@dataclass(frozen=True, slots=True)
class FindingUpsertPlanningResult:
	"""Result of ``plan_finding_issue_upsert`` carrying plan + optional drift.

	``drift_report`` is populated only when the human-override marker
	blocked a would-be create/update; otherwise the planner produced a
	normal create/update/skip plan and ``drift_report`` is ``None``.
	"""

	upsert_plan: "IssueUpsertPlan"
	drift_report: DriftReport | None = None

	def to_dict(self) -> dict[str, Any]:
		return {
			"upsert_plan": self.upsert_plan.to_dict(),
			"drift_report": self.drift_report.to_dict() if self.drift_report else None,
		}


@dataclass(frozen=True, slots=True)
class FindingProjectionResult:
	"""Result of ``reconcile_finding_projection``.

	``finding`` is the (possibly unchanged) record after projection.
	``drift_report`` is populated when the override marker blocked a
	would-be projection update; otherwise ``None``.
	"""

	finding: "FindingIssueRecord"
	drift_report: DriftReport | None = None

	def to_dict(self) -> dict[str, Any]:
		return {
			"finding": self.finding.to_dict(),
			"drift_report": self.drift_report.to_dict() if self.drift_report else None,
		}


@dataclass(frozen=True, slots=True)
class IssueClosurePlan:
	"""Sentinel-initiated GitHub issue closure intent.

	Emitted by reconcile-driven close (no-longer-reproduces), PR feedback
	handling (pr-merged), and conceptual-theme stability reconciliation
	(superseded). Consumers map ``reason`` to the GitHub ``state_reason``
	field when calling the close API.
	"""

	issue_number: int
	reason: ClosureReason
	successor_issue_number: int | None = None

	def to_dict(self) -> dict[str, Any]:
		return {
			"issue_number": self.issue_number,
			"reason": self.reason,
			"successor_issue_number": self.successor_issue_number,
		}


# ---------------------------------------------------------------------------
# Phase 4 bulk-operation payload + result types (cycle 12 Commit 1).
#
# Bulk theme-member actions cover the §11.2 "bulk operations on theme
# members" deliverable. The event carries a theme envelope plus the
# explicit subset of member finding ids to act on, mirroring the
# :class:`sentinel.workflow.pr_feedback_handler.PerCommitRejection`
# multi-member shape (theme_id + member_finding_ids + caller-supplied
# lookup tables for state and issue numbers). Disposition discrimination
# (rejection vs deferral) reuses the :data:`Disposition` alias declared
# at the top of this module.
#
# The handler (``triage_handlers.handle_bulk_theme_member_action``,
# cycle 12 Commit 2) implements a ``strict-eligibility-gate`` partial-
# failure policy: ANY member that fails the eligibility check
# (missing-from-lookup OR current_state not in
# ``TransitionStateConfig.accepted_states``) causes the entire bulk
# operation to refuse with a structured :class:`BulkTriageEligibilityError`
# carrying the per-member skip reasons. On success the handler returns
# a :class:`BulkThemeMemberActionResult` carrying one
# :class:`sentinel.workflow.pr_feedback_handler.FindingStateTransition`
# per eligible member.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BulkThemeMemberActionEvent:
	"""Code Owner bulk-action over a subset of a theme's members.

	The event envelope is theme-scoped (``theme_id``) per §6.6's
	theme-as-unit-of-approval framing, but the action target is the
	explicit ``member_finding_ids`` subset -- this keeps the handler
	agnostic to *how* the caller assembled the membership (full theme
	expansion, severity filter, UX click-pick, etc.) and matches the
	established multi-member shape in
	:class:`sentinel.workflow.pr_feedback_handler.PerCommitRejection`.

	``finding_id_to_current_state`` and ``finding_id_to_issue_number``
	are caller-supplied lookup tables. The handler reads no state
	store; pure-planning discipline matches the cycle 11 single-item
	handlers (:class:`AcceptThemeEvent` /
	:class:`RejectFindingEvent`).

	``disposition`` reuses the :data:`Disposition` alias so a single
	bulk-action handler covers both rejection and deferral, mirroring
	:func:`sentinel.workflow.triage_handlers.handle_reject_finding`'s
	disposition-parameterized shape (cycle 11 Commit 6).
	"""

	theme_id: str
	member_finding_ids: tuple[str, ...]
	finding_id_to_current_state: dict[str, str]
	finding_id_to_issue_number: dict[str, int]
	disposition: Disposition
	actor: str
	timestamp: str
	reason: str | None = None


@dataclass(frozen=True, slots=True)
class BulkSkippedMember:
	"""One member's eligibility-gate failure reason.

	Carried by :class:`BulkTriageEligibilityError.skipped` only. The
	handler emits no skipped records in the happy path; failures
	cause the entire bulk operation to refuse (strict-eligibility-
	gate policy). ``reason`` is a human-readable explanation
	suitable for surfacing to the caller (CLI, UX layer).
	"""

	finding_id: str
	reason: str


@dataclass(frozen=True, slots=True)
class BulkThemeMemberActionResult:
	"""Result of a successful bulk theme-member action.

	On success, carries one
	:class:`sentinel.workflow.pr_feedback_handler.FindingStateTransition`
	per eligible member. The transition's ``to_state`` resolves to
	the active profile's ``rejection_label`` or ``deferral_label``
	per the event's ``disposition``.

	Cycle 12 Commit 1 deliberately omits a ``skipped`` field: the
	strict-eligibility-gate policy surfaces ineligible members only
	via :class:`BulkTriageEligibilityError`. A ``skipped`` field
	may be added in a future cycle that earns skip-and-report
	semantics; not anticipated now.
	"""

	transitions: tuple["FindingStateTransition", ...]


class BulkTriageEligibilityError(Exception):
	"""Strict-eligibility-gate refusal for a bulk theme-member action.

	Raised by
	:func:`sentinel.workflow.triage_handlers.handle_bulk_theme_member_action`
	when one or more members fail the eligibility check (missing
	from the caller-supplied lookup tables, or
	``current_state`` not in the active profile's
	:attr:`sentinel.workflow.config.TransitionStateConfig.accepted_states`).

	The handler collects ALL eligibility failures before raising; the
	``skipped`` tuple thus surfaces every problem in one round-trip
	so the caller can correct its input once rather than iterating.
	No transitions are emitted when this exception is raised
	(refuse-all semantics; matches the established refusal pattern
	in :func:`sentinel.workflow.pr_feedback_handler.handle_per_commit_rejection`
	which raises ``ValueError`` on any precondition violation).
	"""

	def __init__(self, skipped: tuple[BulkSkippedMember, ...]) -> None:
		self.skipped = skipped
		count = len(skipped)
		ids = ", ".join(member.finding_id for member in skipped)
		super().__init__(
			f"bulk theme-member action refused: {count} ineligible member"
			f"{'s' if count != 1 else ''} [{ids}]"
		)


__all__ = [
	"BulkSkippedMember",
	"BulkThemeMemberActionEvent",
	"BulkThemeMemberActionResult",
	"BulkTriageEligibilityError",
	"ClosureReason",
	"ConceptTheme",
	"CrossLensReference",
	"Disposition",
	"DriftKind",
	"DriftReport",
	"FieldDrift",
	"FindingClosurePlanningResult",
	"FindingIssueRecord",
	"FindingProjectionResult",
	"FindingUpsertPlanningResult",
	"GitHubIssueSnapshot",
	"IssueClosurePlan",
	"IssueUpsertPlan",
	"ThemeIssueRecord",
	"ThemeMember",
	"ThemeReference",
]
