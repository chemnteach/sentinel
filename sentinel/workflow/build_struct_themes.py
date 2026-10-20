"""Workflow-track producer for structural themes per spec section 6.4.

Consumes grouper SemanticGroup output (via SemanticGroupRecord) plus per-finding
FindingIssueRecord summaries to synthesize section-6.4-faithful titles and the
dashboard-relevant rollup fields. Cycle-10 item 14 discharge; replaces the
cycle-9 adapter (struct_theme_loader.adapt_semantic_group_to_theme_issue_record),
which used a "Structural group: <grouping_key>" placeholder title and defaulted
the richer fields.

Producer-richness family per the cycle 9 classification. Cycle-15 item 15
discharge extended this producer to populate the section-6.5 GitHub-issue
body content (root_cause_summary, remediation_strategy, members rows with
per-finding other_lens_theme_id, and aggregate_effort / aggregate_risk_of_deferral).
The cross_lens_themes list and the state field remain at dataclass defaults:
cross_lens_themes requires concept-themes input plumbing and member-set overlap
detection (deferred); state mutates via Phase 4 workflow handlers, not at
build time.

Structurally parallel to build_concept_themes for the structural lens: the
title-synthesis, max-ranked-severity, common-prefix, root-cause-summary,
remediation-strategy, and effort/risk-ranking helpers mirror the
conceptual-side shapes (duplicated, not imported, to keep this module's
SemanticGroupRecord-driven logic self-contained).
"""

# Produces ThemeIssueRecord(lens="struct") from SemanticGroupRecord +
# FindingIssueRecord inputs. Format authority for grouper input:
# sentinel.assess.grouper._serialize_group (via struct_theme_loader's
# SemanticGroupRecord). Format authority for finding input:
# sentinel.workflow.models.FindingIssueRecord. Mirror of build_concept_themes
# for the structural lens.

from __future__ import annotations

import logging
from typing import Sequence

from sentinel.workflow.models import FindingIssueRecord, ThemeIssueRecord, ThemeMember
from sentinel.workflow.struct_theme_loader import SemanticGroupRecord

log = logging.getLogger(__name__)

# Mirror of build_concept_themes._SEVERITY_RANK / _EFFORT_RANK / _RISK_RANK
# (lower rank = more severe / costlier / riskier-to-defer); duplicated to keep
# this module self-contained.
_SEVERITY_RANK = {"Critical": 0, "P1": 1, "P2": 2, "P3": 3}
_EFFORT_RANK = {"high": 0, "medium-high": 1, "medium": 2, "low": 3}
_RISK_RANK = {
	"unacceptable — must fix before next release": 0,
	"high — likely to cause production issues": 1,
	"medium — technical debt accumulates": 2,
	"low — cosmetic or nice-to-have": 3,
}

__all__ = [
	"synthesize_struct_theme_title",
	"build_struct_theme",
	"build_struct_themes",
]


def _common_prefix(values: list[str]) -> str:
	prefix = values[0]
	for value in values[1:]:
		limit = min(len(prefix), len(value))
		index = 0
		while index < limit and prefix[index] == value[index]:
			index += 1
		prefix = prefix[:index]
		if not prefix:
			break
	return prefix


def _max_ranked(values: list[str], ranking: dict[str, int], default: str) -> str:
	if not values:
		return default
	return min(values, key=lambda value: ranking.get(value, ranking.get(default, 999)))


def _synthesize_root_cause_summary(group: Sequence[FindingIssueRecord]) -> str:
	descriptions = [finding.description.strip() for finding in group if finding.description.strip()]
	if not descriptions:
		return ""
	if len(descriptions) == 1:
		return descriptions[0]
	first = descriptions[0].rstrip(".")
	second = descriptions[1].rstrip(".")
	if first == second:
		return descriptions[0]
	return f"{first}. {second}."


def _synthesize_remediation_strategy(group: Sequence[FindingIssueRecord]) -> str:
	fixes = [finding.suggested_fix.strip() for finding in group if finding.suggested_fix.strip()]
	if not fixes:
		return ""
	counts: dict[str, int] = {}
	for fix in fixes:
		counts[fix] = counts.get(fix, 0) + 1
	return max(counts.items(), key=lambda item: (item[1], -len(item[0]), item[0]))[0]


def synthesize_struct_theme_title(
	group: SemanticGroupRecord,
	member_findings: Sequence[FindingIssueRecord],
) -> str:
	"""Synthesize a section-6.4-faithful structural-theme title.

	Section 6.4: titles are "short human-readable strings produced from member
	summary values ... they must reference the pattern, not any single file."
	Mirrors build_concept_themes._synthesize_title: a long-enough common prefix
	across member summaries names the shared pattern; otherwise the shortest
	summary is the most pattern-like single representative. When no member
	findings are available (degenerate; e.g. findings not plumbed in or absent
	from the lookup), falls back to a grouping_key placeholder -- expected for
	the empty case, not an error.
	"""
	summaries = [f.summary.strip() for f in member_findings if f.summary.strip()]
	if not summaries:
		return f"Structural group: {group.grouping_key}"
	if len(summaries) == 1:
		return summaries[0]
	prefix = _common_prefix(summaries).strip(" -_:;,.\t")
	if len(prefix) >= 16:
		return prefix
	return min(summaries, key=lambda summary: (len(summary), summary.lower()))


def build_struct_theme(
	group: SemanticGroupRecord,
	member_findings: Sequence[FindingIssueRecord],
) -> ThemeIssueRecord:
	"""Produce one ThemeIssueRecord(lens="struct") from a group + its findings.

	Field sources:
	  * SemanticGroup-derived: theme_id <- group_id; source_id <- grouping_key;
	    member_finding_ids.
	  * FindingIssueRecord-derived (with SemanticGroup fallback when no members):
	    title (section 6.4 synthesis); aggregate_severity (max across members, or
	    group.severity); categories / skills_involved / modules_affected (union
	    across members, or the SemanticGroup contributing_emitters / category /
	    file fallbacks the cycle-9 adapter used).
	  * FindingIssueRecord-derived per cycle-15 item 15 (section 6.5 GitHub
	    issue-body content): root_cause_summary, remediation_strategy, members
	    (one ThemeMember row per member finding, with per-row other_lens_theme_id
	    sourced from finding.concept_theme_id), aggregate_effort,
	    aggregate_risk_of_deferral. Empty member_findings -> these revert to
	    dataclass defaults (empty strings / lists, "low" effort).
	  * Still defaulted: cross_lens_themes (requires concept-themes input and
	    member-set overlap detection); state (mutates via Phase 4 handlers).

	The file -> module mapping (modules_affected from finding.module, falling
	back to {group.file}) supersedes the cycle-9 adapter's lossy {group.file}
	approximation: FindingIssueRecord carries a real module field.
	"""
	if member_findings:
		title = synthesize_struct_theme_title(group, member_findings)
		aggregate_severity = _max_ranked(
			[f.severity for f in member_findings if f.severity], _SEVERITY_RANK, group.severity
		)
		categories = {f.category for f in member_findings if f.category}
		if group.category:
			categories.add(group.category)
		skills_involved = {f.skill for f in member_findings if f.skill}
		modules_affected = {f.module for f in member_findings if f.module}
		members = [
			ThemeMember(
				finding_id=f.finding_id,
				severity=f.severity,
				file_path=f.file or f.module,
				line_range=f.line_range,
				skill=f.skill,
				other_lens_theme_id=f.concept_theme_id,
				summary=f.summary,
			)
			for f in member_findings
		]
		aggregate_effort = _max_ranked(
			[f.effort_estimate for f in member_findings if f.effort_estimate],
			_EFFORT_RANK,
			"low",
		)
		aggregate_risk_of_deferral = _max_ranked(
			[f.risk_of_deferral for f in member_findings if f.risk_of_deferral],
			_RISK_RANK,
			"",
		)
		root_cause_summary = _synthesize_root_cause_summary(member_findings)
		remediation_strategy = _synthesize_remediation_strategy(member_findings)
	else:
		# Degenerate fallback: no findings plumbed in -> SemanticGroup-only values,
		# matching the cycle-9 adapter behavior for absent findings.
		title = synthesize_struct_theme_title(group, member_findings)
		aggregate_severity = group.severity
		categories = {group.category} if group.category else set()
		skills_involved = set(group.contributing_emitters) if group.contributing_emitters else set()
		modules_affected = {group.file} if group.file else set()
		members = []
		aggregate_effort = "low"
		aggregate_risk_of_deferral = ""
		root_cause_summary = ""
		remediation_strategy = ""
	# Contract: emit only member ids that resolved to a FindingIssueRecord. When
	# no findings are plumbed in (degenerate/empty-member path), preserve the
	# group's declared ids so the theme still records its membership; otherwise
	# writing all group ids while some silently dropped would misreport the
	# theme's actual resolved membership.
	if member_findings:
		resolved_ids = {f.finding_id for f in member_findings}
		emitted_member_ids = [fid for fid in group.member_finding_ids if fid in resolved_ids]
	else:
		emitted_member_ids = list(group.member_finding_ids)
	return ThemeIssueRecord(
		lens="struct",
		theme_id=group.group_id,
		source_id=group.grouping_key,
		title=title,
		member_finding_ids=emitted_member_ids,
		skills_involved=skills_involved,
		categories=categories,
		modules_affected=modules_affected,
		aggregate_severity=aggregate_severity,
		aggregate_effort=aggregate_effort,
		aggregate_risk_of_deferral=aggregate_risk_of_deferral,
		root_cause_summary=root_cause_summary,
		remediation_strategy=remediation_strategy,
		members=members,
	)


def build_struct_themes(
	groups: Sequence[SemanticGroupRecord],
	findings: Sequence[FindingIssueRecord],
) -> tuple[ThemeIssueRecord, ...]:
	"""Produce structural themes for a set of groups from their member findings.

	Builds a finding_id -> FindingIssueRecord lookup, gathers each group's member
	findings via the lookup, and produces one ThemeIssueRecord per group. Member
	ids absent from the lookup are dropped from the theme's member_finding_ids
	and logged at WARNING (P1 wave-3 fix: no longer silently swallowed, and no
	longer written to member_finding_ids); a group whose members are all absent
	produces a theme via the empty-member fallback path in build_struct_theme,
	which preserves the group's declared ids so membership is still recorded.
	"""
	by_id = {f.finding_id: f for f in findings}
	out = []
	for group in groups:
		missing = [fid for fid in group.member_finding_ids if fid not in by_id]
		if missing:
			log.warning(
				"Group %s: %d member finding IDs unresolved", group.group_id, len(missing)
			)
		member_findings = [by_id[fid] for fid in group.member_finding_ids if fid in by_id]
		out.append(build_struct_theme(group, member_findings))
	return tuple(out)
