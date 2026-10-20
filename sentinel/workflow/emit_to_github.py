"""Idempotent emission of findings and themes to GitHub Issues."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sentinel.workflow.config import WorkflowConfig
from sentinel.workflow.issue_renderer import build_finding_issue_title, render_finding_issue_body
from sentinel.workflow.label_taxonomy import build_finding_labels, build_theme_labels
from sentinel.workflow.models import (
	DriftReport,
	FindingIssueRecord,
	FindingUpsertPlanningResult,
	GitHubIssueSnapshot,
	IssueClosurePlan,
	IssueUpsertPlan,
	ThemeIssueRecord,
)
from sentinel.workflow.theme_renderer import render_theme_issue_body


# Default human-override label. Commit 5 (Gap C) will wire profile config
# to substitute this; keeping a named constant marks the swap point.
_HUMAN_OVERRIDE_LABEL = "human-override"


def plan_finding_issue_upsert(
	finding: FindingIssueRecord,
	existing_issue: GitHubIssueSnapshot | None = None,
	*,
	override_label: str = _HUMAN_OVERRIDE_LABEL,
	config: WorkflowConfig | None = None,
) -> FindingUpsertPlanningResult:
	"""Plan a finding-issue upsert, respecting the human-override marker.

	When ``existing_issue`` carries ``override_label`` (default
	``human-override``), the locked Gap B record-level skip applies:
	the returned ``IssueUpsertPlan`` has ``action="skip"`` and a
	``skipped-upsert`` ``DriftReport`` is attached per spec section
	7.3. Otherwise the usual create/update/skip planning runs and
	``drift_report`` is ``None``. When ``config`` is supplied,
	``config.profile.override_label`` takes precedence over the
	``override_label`` keyword.
	"""
	effective_label = config.profile.override_label if config is not None else override_label
	title = build_finding_issue_title(finding)
	body = render_finding_issue_body(finding)
	labels = build_finding_labels(finding)
	if existing_issue is not None and effective_label in existing_issue.labels:
		skip_plan = IssueUpsertPlan(
			action="skip",
			title=title,
			body=body,
			labels=labels,
			issue_number=existing_issue.issue_number,
			issue_url=existing_issue.html_url,
		)
		drift = DriftReport(
			finding_id=finding.finding_id,
			issue_number=existing_issue.issue_number,
			drift_kind="skipped-upsert",
			override_label_present=True,
		)
		return FindingUpsertPlanningResult(upsert_plan=skip_plan, drift_report=drift)
	upsert_plan = _plan_issue_upsert(title, body, labels, existing_issue)
	return FindingUpsertPlanningResult(upsert_plan=upsert_plan, drift_report=None)


def plan_theme_issue_upsert(
	theme: ThemeIssueRecord,
	existing_issue: GitHubIssueSnapshot | None = None,
) -> IssueUpsertPlan:
	title = build_theme_issue_title(theme)
	body = render_theme_issue_body(theme)
	labels = build_theme_labels(theme)
	return _plan_issue_upsert(title, body, labels, existing_issue)


def build_theme_issue_title(theme: ThemeIssueRecord) -> str:
	return f"[Theme][{theme.theme_id}] {theme.title}"


def sync_finding_issue(client: object, finding: FindingIssueRecord) -> GitHubIssueSnapshot:
	existing_issue = client.find_finding_issue(finding.finding_id)
	result = plan_finding_issue_upsert(finding, existing_issue=existing_issue)
	return _execute_issue_upsert(client, result.upsert_plan, existing_issue)


def sync_theme_issue(client: object, theme: ThemeIssueRecord) -> GitHubIssueSnapshot:
	existing_issue = client.find_theme_issue(theme.theme_id, theme.lens)
	plan = plan_theme_issue_upsert(theme, existing_issue=existing_issue)
	return _execute_issue_upsert(client, plan, existing_issue)


def _plan_issue_upsert(
	title: str,
	body: str,
	labels: list[str],
	existing_issue: GitHubIssueSnapshot | None,
) -> IssueUpsertPlan:
	if existing_issue is None:
		return IssueUpsertPlan(action="create", title=title, body=body, labels=labels)
	if _matches_existing_issue(existing_issue, title, body, labels):
		return IssueUpsertPlan(
			action="skip",
			title=title,
			body=body,
			labels=labels,
			issue_number=existing_issue.issue_number,
			issue_url=existing_issue.html_url,
		)
	return IssueUpsertPlan(
		action="update",
		title=title,
		body=body,
		labels=labels,
		issue_number=existing_issue.issue_number,
		issue_url=existing_issue.html_url,
	)


def _matches_existing_issue(
	existing_issue: GitHubIssueSnapshot,
	title: str,
	body: str,
	labels: list[str],
) -> bool:
	return (
		existing_issue.title == title
		and existing_issue.body == body
		and sorted(existing_issue.labels) == sorted(labels)
	)


def _execute_issue_upsert(
	client: object,
	plan: IssueUpsertPlan,
	existing_issue: GitHubIssueSnapshot | None,
) -> GitHubIssueSnapshot:
	if plan.action == "create":
		return client.create_issue(plan.title, plan.body, plan.labels)
	if plan.action == "update":
		if plan.issue_number is None:
			raise ValueError("update plan requires an issue number")
		return client.update_issue(
			plan.issue_number,
			title=plan.title,
			body=plan.body,
			labels=plan.labels,
		)
	if existing_issue is None:
		raise ValueError("skip plan requires an existing issue snapshot")
	return existing_issue


__all__ = [
	"ThemeSupersedePlan",
	"build_theme_issue_title",
	"plan_finding_issue_upsert",
	"plan_theme_issue_upsert",
	"plan_theme_supersede",
	"sync_finding_issue",
	"sync_theme_issue",
]


@dataclass(frozen=True, slots=True)
class ThemeSupersedePlan:
	"""Bundle of operations to retire a concept theme replaced by a successor.

	Emitted by :func:`plan_theme_supersede` when stability reconciliation
	(spec section 6.2 step 5) mints a new ``C-NNN`` ID for a cluster whose
	max Jaccard against prior conceptual themes fell below the threshold.
	The consumer first posts ``comment_body`` to the prior rollup issue
	via :meth:`GitHubWorkflowClient.post_issue_comment`, then closes that
	issue via :meth:`GitHubWorkflowClient.close_issue` using
	``closure_plan``.
	"""

	prior_theme_id: str
	successor_theme_id: str
	prior_issue_number: int
	comment_body: str
	closure_plan: IssueClosurePlan

	def to_dict(self) -> dict[str, Any]:
		return {
			"prior_theme_id": self.prior_theme_id,
			"successor_theme_id": self.successor_theme_id,
			"prior_issue_number": self.prior_issue_number,
			"comment_body": self.comment_body,
			"closure_plan": self.closure_plan.to_dict(),
		}


def plan_theme_supersede(
	*,
	prior_theme_id: str,
	successor_theme_id: str,
	prior_issue_number: int,
	successor_issue_number: int,
	successor_issue_url: str | None = None,
) -> ThemeSupersedePlan:
	"""Plan the supersede flow for a retired conceptual theme.

	Spec section 6.2 step 5: when a new cluster's max Jaccard against
	prior conceptual themes falls below the stability threshold, Sentinel
	mints a new ``C-NNN`` ID and closes the prior rollup issue as
	superseded with a link to the successor.

	The returned plan carries the comment body (with a successor link
	rendered as Markdown when ``successor_issue_url`` is provided, falling
	back to ``#<issue-number>`` shorthand otherwise) and an
	:class:`IssueClosurePlan` with ``reason='superseded'`` and
	``successor_issue_number`` populated. Invocation from
	:mod:`sentinel.workflow.build_concept_themes` is deferred beyond
	cycle 7; this function is the emission primitive.
	"""
	if successor_issue_url:
		link = f"[{successor_theme_id} (#{successor_issue_number})]({successor_issue_url})"
	else:
		link = f"{successor_theme_id} (#{successor_issue_number})"
	comment_body = (
		f"This conceptual theme has been superseded by {link}. "
		f"Stability reconciliation (spec section 6.2) minted a new C-NNN identifier "
		f"because the cluster's max Jaccard against prior themes fell below the threshold. "
		f"Future remediation tracking continues on the successor issue."
	)
	closure_plan = IssueClosurePlan(
		issue_number=prior_issue_number,
		reason="superseded",
		successor_issue_number=successor_issue_number,
	)
	return ThemeSupersedePlan(
		prior_theme_id=prior_theme_id,
		successor_theme_id=successor_theme_id,
		prior_issue_number=prior_issue_number,
		comment_body=comment_body,
		closure_plan=closure_plan,
	)
