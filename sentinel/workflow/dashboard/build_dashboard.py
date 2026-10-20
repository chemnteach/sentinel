
from pathlib import Path
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, List, Dict
from sentinel.workflow.dashboard.contract import (
	DashboardBundle, DashboardMetadata, SeverityRiskRow, LaneConflictRow, HumanOverrideRow, RunConfidence, AuditFreshnessSummary, LensDivergence, ThemeSummaryRow, WorkflowStateRow, ModuleHotspotRow
)
from sentinel.workflow.models import FindingIssueRecord, ThemeIssueRecord
from sentinel.workflow.reconcile_issues import detect_drift, extract_finding_id
from sentinel.workflow.config import WorkflowConfig, OWNER_GATE_STATE_LABELS

__all__ = ["BundleInput", "build_dashboard_bundle", "write_bundle_json"]

def build_severity_risk_summary(findings: List[FindingIssueRecord]) -> List[SeverityRiskRow]:
	"""
	Build the severity/risk summary section for the dashboard bundle.
	Translates internal `status` field to dashboard `states` key for spec alignment.
	"""
	# Group by severity, then by status (as 'state' in output)
	summary: Dict[str, Dict[str, int]] = {}
	for rec in findings:
		sev = rec.severity or "Unspecified"
		state = rec.status or "Unspecified"
		if sev not in summary:
			summary[sev] = {}
		summary[sev][state] = summary[sev].get(state, 0) + 1
	# Sort by severity (descending, fallback to alpha)
	severity_order = ["P0", "P1", "P2", "P3", "P4", "Unspecified"]
	def sev_sort_key(s):
		try:
			return severity_order.index(s)
		except ValueError:
			return len(severity_order)
	rows = []
	for sev in sorted(summary, key=sev_sort_key):
		rows.append(SeverityRiskRow(
			severity=sev,
			count=sum(summary[sev].values()),
			states=summary[sev]
		))
	return rows

def build_lane_conflict_summary(findings: List[FindingIssueRecord]) -> List[LaneConflictRow]:
	"""
	Build the lane conflict summary section for the dashboard bundle.
	Each row represents a finding where needs_human_review is True (per section 9.4-5).
	"""
	rows = []
	for rec in findings:
		if getattr(rec, "needs_human_review", False):
			rows.append(LaneConflictRow(
				finding_id=rec.finding_id,
				theme_id=getattr(rec, "struct_theme_id", ""),
				needs_human_review=True
			))
	return rows

def build_human_override_summary(
	findings: List[FindingIssueRecord],
	issues: List[Any],
	config: WorkflowConfig | None = None,
) -> List[HumanOverrideRow]:
	"""
	Build the human-override summary section (spec section 9.4-6).

	Scoped to *overridden records*: only issues carrying the active
	override marker are surfaced, per section 9.2 ("human overrides marked
	by ``human-override`` or the active profile's equivalent marker") and
	section 7.3. The marker name is read from the threaded WorkflowConfig
	(``config.profile.override_label``) when supplied, defaulting to
	``"human-override"`` otherwise -- matching the cycle 7 reconcile-path
	convention. Per-row field detail comes from ``detect_drift``; an
	overridden record with no detected field drift still surfaces (empty
	``drift_details``) because the gating criterion is override presence,
	not drift presence.
	"""
	effective_label = (
		config.profile.override_label if config is not None else "human-override"
	)
	finding_map = {rec.finding_id: rec for rec in findings}
	rows = []
	for issue in issues:
		if effective_label not in getattr(issue, "labels", ()):
			continue
		fid = extract_finding_id(getattr(issue, "body", "")) or ""
		rec = finding_map.get(fid)
		drift = detect_drift(rec, issue) if rec is not None else None
		rows.append(HumanOverrideRow(
			finding_id=fid,
			issue_number=getattr(issue, "issue_number", 0),
			drift_kind=drift.drift_kind if drift is not None else "",
			drift_details=drift.drift_details if drift is not None else (),
			override_label_present=True,
		))
	return rows

def build_theme_summary(
	struct_themes: List[ThemeIssueRecord],
	concept_themes: List[ThemeIssueRecord],
) -> tuple[ThemeSummaryRow, ...]:
	"""
	Build the theme summary section (spec section 9.4-2): structural and
	conceptual theme rollups in one lens-tagged section. Structural themes
	emit rows with lens="struct"; conceptual themes with lens="concept".
	The param-group is the lens authority (Option 3: the builder takes
	ThemeIssueRecord lists; loading/wiring deferred to engagement item 10).
	Rows are ordered struct-first (by theme_id), then concept (by theme_id).
	"""
	rows: list[ThemeSummaryRow] = []
	for theme in sorted(struct_themes, key=lambda t: t.theme_id):
		rows.append(_theme_summary_row(theme, "struct"))
	for theme in sorted(concept_themes, key=lambda t: t.theme_id):
		rows.append(_theme_summary_row(theme, "concept"))
	return tuple(rows)

def _theme_summary_row(theme: ThemeIssueRecord, lens: str) -> ThemeSummaryRow:
	return ThemeSummaryRow(
		lens=lens,
		theme_id=theme.theme_id,
		title=theme.title,
		member_count=len(theme.member_finding_ids),
		aggregate_severity=theme.aggregate_severity,
		skills_involved=sorted(theme.skills_involved),
		categories=sorted(theme.categories),
		modules_affected=sorted(theme.modules_affected),
		state=theme.state,
	)

def build_workflow_state_summary(
	findings: List[FindingIssueRecord],
	*,
	config: WorkflowConfig | None = None,
) -> tuple[WorkflowStateRow, ...]:
	"""
	Build the workflow state summary section (spec section 9.4-4): a state
	funnel keyed on the active profile's state labels. The state vocabulary
	is read from config.profile.state_labels when a WorkflowConfig is
	threaded in, defaulting to the canonical OWNER_GATE_STATE_LABELS
	otherwise. The model field is `status`; it is translated to the
	dashboard `state` surface at the read site per the Commit 2 lock.

	Include-and-flag for out-of-vocabulary (OOV) states: every configured
	state is emitted (count zero when no findings carry it); any state
	observed in findings but absent from the configured vocabulary is also
	emitted. Sort is two-group: configured states alphabetically first,
	then OOV states alphabetically. Visual distinction lands in Commit 7's
	Markdown rendering.

	Empty-section convention (Q4): when there are no findings, the section
	is empty (returns ()), consistent with every other cycle 8 section and
	with the Commit 1 contract. The configured-state skeleton-with-zero-
	counts is emitted only for the populated case.
	"""
	if not findings:
		return ()
	effective_state_labels = (
		config.profile.state_labels if config is not None else OWNER_GATE_STATE_LABELS
	)
	counts: Dict[str, int] = {}
	for rec in findings:
		state = rec.status or "Unspecified"
		counts[state] = counts.get(state, 0) + 1
	configured_set = set(effective_state_labels)
	rows: list[WorkflowStateRow] = []
	for state in sorted(configured_set):
		rows.append(WorkflowStateRow(state=state, count=counts.get(state, 0)))
	for state in sorted(s for s in counts if s not in configured_set):
		rows.append(WorkflowStateRow(state=state, count=counts[state]))
	return tuple(rows)

def build_module_hotspot_summary(findings: List[FindingIssueRecord]) -> tuple[ModuleHotspotRow, ...]:
	"""
	Build the module/hotspot summary section (spec section 9.4-3), simple form.

	Modules are ranked by finding count descending, with module name as the
	alphabetical tiebreaker for determinism. severity_mix is the per-module
	dict[severity, count]. Spec section 9.4-3 names three "or" alternatives
	for ranking (open finding count, severity mix, or profile-defined risk
	weight); the simple form ranks by count. Profile-defined risk weight is
	deferred per the cycle 8 design pass.

	"Open" finding semantics: the workflow track has no canonical open-finding
	definition (no is_open helper or terminal-status constant). For cycle 8
	all findings are counted; profile-aware open-vs-terminal filtering is part
	of the section 9.4-3 risk-weight deferral. The contract field remains
	open_finding_count for forward compatibility when that filter lands.

	Empty findings -> empty tuple, per the cycle 8 empty-section convention.
	"""
	if not findings:
		return ()
	counts: Dict[str, int] = {}
	severity_mix: Dict[str, Dict[str, int]] = {}
	for rec in findings:
		module = rec.module or "Unspecified"
		counts[module] = counts.get(module, 0) + 1
		sev = rec.severity or "Unspecified"
		severity_mix.setdefault(module, {})
		severity_mix[module][sev] = severity_mix[module].get(sev, 0) + 1
	ordered = sorted(counts, key=lambda m: (-counts[m], m))
	return tuple(
		ModuleHotspotRow(
			module=module,
			open_finding_count=counts[module],
			severity_mix=severity_mix[module],
		)
		for module in ordered
	)

def build_run_confidence(scorecard: dict | None = None) -> RunConfidence:
	"""
	Build the run confidence section (spec section 9.4-8): the current
	confidence_label plus supporting run-level quality data.

	The canonical source is the orchestrator-track quality_scorecard.json
	(spec section 9.2). It is injected as a dict parameter (Option 3); loading
	and wiring are deferred (engagement item 11), keeping the workflow track
	decoupled from the orchestrator track. supporting_evidence carries the
	scorecard's metrics and gates; remediation_guidance is excluded as
	downstream-action-oriented rather than evidence-oriented.

	When no scorecard is supplied (the conditional-absence case, since the
	scorecard is only generated under enable_quality_scorecard), the section
	is empty: confidence_label="" and supporting_evidence=None.
	"""
	if not scorecard:
		return RunConfidence(confidence_label="", supporting_evidence=None)
	return RunConfidence(
		confidence_label=scorecard.get("confidence_label", ""),
		supporting_evidence={
			"metrics": scorecard.get("metrics", {}),
			"gates": scorecard.get("gates", {}),
		},
	)

def build_audit_freshness_summary(
	*,
	scan_id: str = "",
	generation_timestamp: str = "",
	profile: str = "",
	profile_version: str = "",
	artifact_status: str = "",
	freshness_evidence: str = "",
	workflow_store_sync_status: str = "",
) -> AuditFreshnessSummary:
	"""
	Build the audit/freshness summary section (spec section 9.4-9).

	Data points: scan identity, generation time, profile/version,
	artifact/refresh status, workflow-store sync status. Section 9.6 adds the
	profile + profile version requirement (inherited here). All inputs are
	metadata (the section reads no findings/themes/issues), so the signature
	is keyword-only to force clarity at the call site -- distinct from the
	other section builders, whose primary inputs are positional data sources.

	Scope notes:
	  * workflow_store_sync_status is caller-injected (Option 3) and defaults
	    to "" ("not provided / unknown"). The workflow track has no persisted
	    sync timestamp today; wiring is deferred (engagement item 12).
	  * Section 9.6 publication/mirror-success bullets are deferred -- the
	    branch-push and SharePoint bridge are out of scope for cycle 8 (Q1).
	  * Section 13.4 quantitative age-metrics (scan-to-report lag, sync age,
	    commit/refresh age, publication-failure count) are out of scope: they
	    require multi-event timestamp infrastructure not present in the
	    workflow track.
	"""
	return AuditFreshnessSummary(
		scan_id=scan_id,
		generation_timestamp=generation_timestamp,
		profile=profile,
		profile_version=profile_version,
		workflow_store_sync_status=workflow_store_sync_status,
		artifact_status=artifact_status,
		freshness_evidence=freshness_evidence,
	)

def build_lens_divergence(
	struct_themes: List[ThemeIssueRecord],
	concept_themes: List[ThemeIssueRecord],
	*,
	signal_bearing: bool = True,
	suppression_reason: str = "",
) -> LensDivergence:
	"""
	Build the lens divergence sub-metrics (spec section 9.5).

	Two of the three section 9.5 sub-metrics are implemented; cross-lens
	entropy is deferred (the spec names normalized mutual information but is
	silent on the normalization variant -- arithmetic/geometric/min/max mean
	of the two entropies give materially different values, so the variant is
	unspecified, not the metric).

	Data source is the two theme datasets per section 10.1-6 ("build_dashboard
	computes lens-divergence sub-metrics from struct_themes.json and
	concept_themes.json"); findings are not read.

	  * Consolidation ratio: count(struct_themes) / count(concept_themes)
	    (section 9.5-1), or 0.0 when there are no concept themes.
	  * Mean conceptual span: for each conceptual theme, the count of distinct
	    structural themes its members span, averaged across conceptual themes
	    (section 9.5-2). The finding -> struct-theme map is built from struct
	    themes' member_finding_ids; struct themes are sorted by theme_id first
	    so that if a finding appears under more than one struct theme the
	    mapping is deterministic. A member finding absent from the map is
	    skipped silently (edge case). Empty-member conceptual themes contribute
	    0 to the mean, honoring section 9.5's literal "mean across conceptual
	    themes" rather than silently excluding them.

	Section 6.8 signal-bearing suppression (engagement item 13): when the
	conceptual lens is singleton, low-signal, or unstable per section 6.8.2,
	the metrics must be marked not meaningful and the degraded mode disclosed
	(section 6.8.4). signal_bearing / suppression_reason are caller-injected
	(Option-3 injection, the cycle 8 precedent); the upstream signal-bearing
	determination is the concept-theme pipeline's run metadata (section 6.8
	requires recording it), wired in a future cycle. The metrics are still
	computed and stored regardless; the renderer honors signal_bearing to
	decide whether to present or suppress them.
	"""
	consolidation_ratio = (
		len(struct_themes) / len(concept_themes) if concept_themes else 0.0
	)
	finding_to_struct: Dict[str, str] = {}
	for theme in sorted(struct_themes, key=lambda t: t.theme_id):
		for finding_id in theme.member_finding_ids:
			finding_to_struct[finding_id] = theme.theme_id
	if concept_themes:
		spans = []
		for concept in concept_themes:
			distinct = {
				finding_to_struct[fid]
				for fid in concept.member_finding_ids
				if fid in finding_to_struct
			}
			spans.append(len(distinct))
		mean_conceptual_span = sum(spans) / len(spans)
	else:
		mean_conceptual_span = 0.0
	return LensDivergence(
		consolidation_ratio=consolidation_ratio,
		mean_conceptual_span=mean_conceptual_span,
		signal_bearing=signal_bearing,
		suppression_reason=suppression_reason,
	)

@dataclass(frozen=True, slots=True)
class BundleInput:
	"""Aggregated inputs for build_dashboard_bundle (cycle 19)."""
	# Metadata
	scan_id: str = ""
	profile: str = ""
	profile_version: str = ""
	artifact_scope: str = ""
	artifact_status: str = ""
	review_policy: str = ""
	generation_timestamp: str = ""
	freshness_evidence: str = ""
	workflow_store_sync_status: str = ""
	# Data containers
	findings: list[FindingIssueRecord] = field(default_factory=list)
	issues: list[Any] = field(default_factory=list)
	struct_themes: Sequence[ThemeIssueRecord] = ()
	concept_themes: Sequence[ThemeIssueRecord] = ()
	scorecard: dict | None = None
	lens_signal_bearing: bool = True
	lens_suppression_reason: str = ""
	config: WorkflowConfig | None = None


def build_dashboard_bundle(bundle: BundleInput) -> DashboardBundle:
	"""
	Build the dashboard bundle from findings, issues, and metadata.
	Populates severity/risk, lane-conflict, and human-override sections using section builders.
	"""
	scan_id = bundle.scan_id
	profile = bundle.profile
	profile_version = bundle.profile_version
	artifact_scope = bundle.artifact_scope
	artifact_status = bundle.artifact_status
	review_policy = bundle.review_policy
	generation_timestamp = bundle.generation_timestamp
	freshness_evidence = bundle.freshness_evidence
	workflow_store_sync_status = bundle.workflow_store_sync_status
	findings = bundle.findings
	issues = bundle.issues
	struct_themes = bundle.struct_themes
	concept_themes = bundle.concept_themes
	scorecard = bundle.scorecard
	lens_signal_bearing = bundle.lens_signal_bearing
	lens_suppression_reason = bundle.lens_suppression_reason
	config = bundle.config
	metadata = DashboardMetadata(
		scan_id=scan_id,
		profile=profile,
		profile_version=profile_version,
		artifact_scope=artifact_scope,
		artifact_status=artifact_status,
		review_policy=review_policy,
		generation_timestamp=generation_timestamp,
		freshness_evidence=freshness_evidence,
	)
	run_confidence = build_run_confidence(scorecard)
	audit_freshness = build_audit_freshness_summary(
		scan_id=scan_id,
		generation_timestamp=generation_timestamp,
		profile=profile,
		profile_version=profile_version,
		artifact_status=artifact_status,
		freshness_evidence=freshness_evidence,
		workflow_store_sync_status=workflow_store_sync_status,
	)
	lens_divergence = build_lens_divergence(
		struct_themes,
		concept_themes,
		signal_bearing=lens_signal_bearing,
		suppression_reason=lens_suppression_reason,
	)
	return DashboardBundle(
		metadata=metadata,
		severity_risk_summary=build_severity_risk_summary(findings),
		theme_summary=build_theme_summary(struct_themes, concept_themes),
		module_hotspot_summary=build_module_hotspot_summary(findings),
		workflow_state_summary=build_workflow_state_summary(findings, config=config),
		lane_conflict_summary=build_lane_conflict_summary(findings),
		human_override_summary=build_human_override_summary(findings, issues, config=config),
		run_confidence=run_confidence,
		audit_freshness_summary=audit_freshness,
		lens_divergence=lens_divergence,
	)

def write_bundle_json(bundle: DashboardBundle, output_path: Path) -> None:
	"""
	Serialize the dashboard bundle to JSON at the given output path.
	"""
	output_path.write_text(json.dumps(bundle.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
