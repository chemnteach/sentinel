"""Markdown renderer for the dashboard bundle (cycle 8 Commit 7).

Consumer side of the Commit 1 data contract: reads a DashboardBundle and
produces human-readable Markdown. Per the Q2 hybrid organizing lock, the
section *builders* (Commits 2-6) are organized by section 9.4 sub-section
number, but this *layout* is organized by the four section 9 foundation
questions (Scope / Risk / Trend / Confidence), with lens divergence
(section 9.5) rendered after them.

Section-to-question mapping (spec section 9 preamble, lines 904-909):
  * Scope      -> theme summary (9.4-2), workflow state summary (9.4-4)
  * Risk       -> severity/risk summary (9.4-1), module/hotspot (9.4-3)
  * Trend      -> trend summary (9.4-7), deferred (multi-scan history)
  * Confidence -> run confidence (9.4-8), human-override (9.4-6),
                  lane-conflict (9.4-5), audit/freshness (9.4-9)

The spec names Scope/Risk/Trend/Confidence content explicitly for sections
1/2/3/4/8; it is silent on where lane-conflict (5), human-override (6), and
audit/freshness (9) belong. They are grouped under Confidence on an
audit/trust-signal rationale (they speak to how much to trust this run). A
fifth "Audit/Governance" heading was considered and rejected as an expansion
of the Q2-locked four-question layout.

ASCII-only per engagement convention (no em-dash / Unicode codepoints).
"""

from __future__ import annotations

from pathlib import Path
from typing import List

from sentinel.workflow.dashboard.contract import (
	DashboardBundle,
	SeverityRiskRow,
	ThemeSummaryRow,
	ModuleHotspotRow,
	WorkflowStateRow,
	LaneConflictRow,
	HumanOverrideRow,
	RunConfidence,
	AuditFreshnessSummary,
	LensDivergence,
)

__all__ = ["render_bundle_markdown", "write_bundle_markdown"]

_TREND_DEFERRED_NOTE = (
	"(Trend reporting requires multi-scan history infrastructure not yet "
	"implemented; see section 9.4-7. Will surface once scan-to-scan history "
	"is persisted.)"
)


def render_bundle_markdown(bundle: DashboardBundle) -> str:
	"""Render a DashboardBundle to Markdown. Pure: bundle in, string out."""
	lines: List[str] = []
	lines.extend(_render_header(bundle))
	lines.append("")
	lines.extend(_render_scope(bundle))
	lines.append("")
	lines.extend(_render_risk(bundle))
	lines.append("")
	lines.extend(_render_trend())
	lines.append("")
	lines.extend(_render_confidence(bundle))
	lines.append("")
	lines.extend(_render_lens_divergence(bundle.lens_divergence))
	return "\n".join(lines) + "\n"


def write_bundle_markdown(bundle: DashboardBundle, output_path: Path) -> None:
	"""Serialize the dashboard bundle to Markdown. Mirrors write_bundle_json."""
	output_path.write_text(render_bundle_markdown(bundle), encoding="utf-8")


# ----------------------------------------------------------------------
# Header + foundation-question sections
# ----------------------------------------------------------------------

def _render_header(bundle: DashboardBundle) -> List[str]:
	md = bundle.metadata
	scan_id = md.scan_id or "(unknown scan)"
	return [
		f"# Sentinel Dashboard -- {scan_id}",
		"",
		f"- **Scan ID:** {md.scan_id}",
		f"- **Profile:** {md.profile}",
		f"- **Profile version:** {md.profile_version}",
		f"- **Generated:** {md.generation_timestamp}",
	]


def _render_scope(bundle: DashboardBundle) -> List[str]:
	lines = ["## Scope", "", "### Theme Summary", ""]
	lines.extend(_render_theme_summary(bundle.theme_summary))
	lines.extend(["", "### Workflow State Summary", ""])
	lines.extend(_render_workflow_state_summary(bundle.workflow_state_summary))
	return lines


def _render_risk(bundle: DashboardBundle) -> List[str]:
	lines = ["## Risk", "", "### Severity / Risk Summary", ""]
	lines.extend(_render_severity_risk_summary(bundle.severity_risk_summary))
	lines.extend(["", "### Module / Hotspot Summary", ""])
	lines.extend(_render_module_hotspot_summary(bundle.module_hotspot_summary))
	return lines


def _render_trend() -> List[str]:
	return ["## Trend", "", _TREND_DEFERRED_NOTE]


def _render_confidence(bundle: DashboardBundle) -> List[str]:
	lines = ["## Confidence", "", "### Run Confidence", ""]
	lines.extend(_render_run_confidence(bundle.run_confidence))
	lines.extend(["", "### Human-Override Summary", ""])
	lines.extend(_render_human_override_summary(bundle.human_override_summary))
	lines.extend(["", "### Lane-Conflict Summary", ""])
	lines.extend(_render_lane_conflict_summary(bundle.lane_conflict_summary))
	lines.extend(["", "### Audit / Freshness Summary", ""])
	lines.extend(_render_audit_freshness_summary(bundle.audit_freshness_summary))
	return lines


# ----------------------------------------------------------------------
# Tabular sections
# ----------------------------------------------------------------------

def _render_severity_risk_summary(rows: List[SeverityRiskRow]) -> List[str]:
	if not rows:
		return [_empty_placeholder("severity/risk rows")]
	table_rows = [
		[r.severity, str(r.count), _render_count_map(r.states)]
		for r in rows
	]
	return _table(["Severity", "Count", "States"], table_rows)


def _render_theme_summary(rows: List[ThemeSummaryRow]) -> List[str]:
	if not rows:
		return [_empty_placeholder("themes")]
	table_rows = [
		[r.lens, r.theme_id, r.title, str(r.member_count), r.aggregate_severity, r.state]
		for r in rows
	]
	return _table(
		["Lens", "Theme ID", "Title", "Members", "Aggregate Severity", "State"],
		table_rows,
	)


def _render_module_hotspot_summary(rows: List[ModuleHotspotRow]) -> List[str]:
	if not rows:
		return [_empty_placeholder("module hotspots")]
	table_rows = [
		[r.module, str(r.open_finding_count), _render_count_map(r.severity_mix)]
		for r in rows
	]
	return _table(["Module", "Open Findings", "Severity Mix"], table_rows)


def _render_workflow_state_summary(rows: List[WorkflowStateRow]) -> List[str]:
	if not rows:
		return [_empty_placeholder("workflow states")]
	table_rows = [[r.state, str(r.count)] for r in rows]
	return _table(["State", "Count"], table_rows)


# ----------------------------------------------------------------------
# Bullet-list sections
# ----------------------------------------------------------------------

def _render_lane_conflict_summary(rows: List[LaneConflictRow]) -> List[str]:
	if not rows:
		return [_empty_placeholder("lane conflicts")]
	return [
		f"- **{r.finding_id}** (theme {r.theme_id or 'n/a'}): needs human review"
		for r in rows
	]


def _render_human_override_summary(rows: List[HumanOverrideRow]) -> List[str]:
	if not rows:
		return [_empty_placeholder("human overrides")]
	out: List[str] = []
	for r in rows:
		drift = r.drift_kind if r.drift_kind else "override present, no field drift detected"
		out.append(f"- **{r.finding_id}** (issue #{r.issue_number}): {drift}")
	return out


# ----------------------------------------------------------------------
# Single-object sections
# ----------------------------------------------------------------------

def _render_run_confidence(rc: RunConfidence) -> List[str]:
	if not rc.confidence_label:
		return [
			"(no confidence label available; quality scorecard not generated "
			"for this scan)"
		]
	lines = [f"- **Confidence label:** {rc.confidence_label}"]
	evidence = rc.supporting_evidence
	if isinstance(evidence, dict):
		metrics = evidence.get("metrics", {})
		gates = evidence.get("gates", {})
		if metrics:
			lines.append("- **Metrics:**")
			lines.extend(f"  - {k}: {v}" for k, v in sorted(metrics.items()))
		if gates:
			lines.append("- **Gates:**")
			lines.extend(f"  - {k}: {v}" for k, v in sorted(gates.items()))
	return lines


def _render_audit_freshness_summary(af: AuditFreshnessSummary) -> List[str]:
	# Always populated by construction; no empty placeholder.
	return [
		f"- **Scan ID:** {af.scan_id}",
		f"- **Generated:** {af.generation_timestamp}",
		f"- **Profile:** {af.profile}",
		f"- **Profile version:** {af.profile_version}",
		f"- **Workflow-store sync status:** {af.workflow_store_sync_status or '(not provided)'}",
		f"- **Artifact status:** {af.artifact_status or '(not provided)'}",
		f"- **Freshness evidence:** {af.freshness_evidence or '(not provided)'}",
	]


# Section 6.8.4 "disclose the degraded mode": each suppression reason
# (section 6.8.2 trigger) maps to a distinct user-facing disclosure string.
_SUPPRESSION_DISCLOSURE = {
	"non-signal-bearing": "conceptual lens not signal-bearing",
	"low-signal": "low signal in conceptual lens",
	"effectively-singleton": "conceptual themes effectively singleton",
	"concept-unstable": "conceptual themes unstable",
}


def _render_lens_divergence(ld: LensDivergence) -> List[str]:
	lines = ["## Lens Divergence", ""]
	# Item 13: explicit signal_bearing suppression replaces the cycle 8 Commit 7
	# both-metrics-zero heuristic. Per section 6.8.4, disclose the degraded mode.
	if not ld.signal_bearing:
		detail = _SUPPRESSION_DISCLOSURE.get(ld.suppression_reason, "see section 6.8")
		lines.append(
			f"(lens divergence not meaningful for this scan: {detail}; see section 6.8)"
		)
		return lines
	lines.append(
		f"- **Consolidation ratio:** {ld.consolidation_ratio} "
		f"({_band_label_for_ratio(ld.consolidation_ratio)})"
	)
	lines.append(
		f"- **Mean conceptual span:** {ld.mean_conceptual_span} "
		f"({_band_label_for_span(ld.mean_conceptual_span)})"
	)
	return lines


# ----------------------------------------------------------------------
# Band-label helpers (section 9.5 interpretation guidance)
# ----------------------------------------------------------------------

def _band_label_for_ratio(value: float) -> str:
	"""Interpretation band for the consolidation ratio (section 9.5-1).

	Spec-named bands: < 1.0 (conceptual lens splits more), == 1.0 (same
	count), > 1.5 (consolidates noticeably), > 3.0 (strong consolidation).

	Spec-silent gap filled here: (1.0, 1.5] has no spec label; it is rendered
	as "mild consolidation", monotonic with the named bands.

	Boundary convention: upper-inclusive within each band (> X and <= Y),
	which preserves the spec's literal ">" semantics at named thresholds --
	a value at a spec-named threshold (e.g. 1.5, 3.0) falls in the band below
	it, per the spec's strict-greater reading. Future spec updates should
	update this helper and its test parametrization together.
	"""
	if value < 1.0:
		return "conceptual lens splits more"
	if value == 1.0:
		return "lenses agree"
	if value <= 1.5:
		return "mild consolidation"
	if value <= 3.0:
		return "noticeable consolidation"
	return "strong consolidation"


def _band_label_for_span(value: float) -> str:
	"""Interpretation band for the mean conceptual span (section 9.5-2).

	Spec-named bands: == 1.0 (1:1 mapping), 2-3 (typical), > 5 (heavy
	consolidation).

	Spec-silent gaps filled here: < 1.0, (1.0, 2.0), and (3.0, 5.0] have no
	spec labels. They are rendered as "below 1:1 ...", folded into "typical"
	(absorbing (1.0, 3.0] into the spec's 2-3 typical band), and "above
	typical" respectively -- monotonic with the named bands.

	Boundary convention: upper-inclusive within each band (> X and <= Y),
	which preserves the spec's literal ">" semantics at named thresholds --
	a value at a spec-named threshold (e.g. 3.0, 5.0) falls in the band below
	it, per the spec's strict-greater reading. Future spec updates should
	update this helper and its test parametrization together.
	"""
	if value < 1.0:
		return "below 1:1 (some conceptual themes span no structural theme)"
	if value == 1.0:
		return "1:1 mapping"
	if value <= 3.0:
		return "typical"
	if value <= 5.0:
		return "above typical"
	return "heavy consolidation"


# ----------------------------------------------------------------------
# Small formatting helpers
# ----------------------------------------------------------------------

def _table(headers: List[str], rows: List[List[str]]) -> List[str]:
	out = [
		"| " + " | ".join(headers) + " |",
		"| " + " | ".join("---" for _ in headers) + " |",
	]
	for row in rows:
		out.append("| " + " | ".join(row) + " |")
	return out


def _render_count_map(counts: dict) -> str:
	if not counts:
		return "(none)"
	return ", ".join(f"{k}: {v}" for k, v in sorted(counts.items()))


def _empty_placeholder(section_noun: str) -> str:
	return f"(no {section_noun} in this scan)"
