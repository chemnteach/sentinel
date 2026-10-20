"""Markdown renderers for finding issues."""

from __future__ import annotations

from sentinel.workflow.models import FindingIssueRecord


def build_finding_issue_title(record: FindingIssueRecord) -> str:
	summary = record.summary.strip() if record.summary.strip() else _summary_from_description(record.description)
	# Guarantee a non-empty, human-usable title fragment when neither the
	# summary nor the description yields one. (FIX-DESIRED-007)
	summary = summary or "(no summary available)"
	return f"[{record.severity}] {record.skill}: {summary} ({record.location})"


def render_finding_issue_body(record: FindingIssueRecord) -> str:
	lines = [
		"## Theme",
		(
			f"{record.primary_theme.theme_id} — {record.primary_theme.title} "
			f"({record.primary_theme.member_count} findings in theme)"
		),
	]
	if record.primary_theme.issue_url:
		lines.append(record.primary_theme.issue_url)

	lines.extend([
		"",
		"## Description",
		record.description,
		"",
		"## Suggested fix",
		record.suggested_fix,
		"",
		"## Detection metadata",
		f"- **Severity:** {record.severity}",
		f"- **Skill:** {record.skill}",
		f"- **Module:** {record.module}",
		f"- **Category:** {record.category}",
		f"- **Location:** {record.location}",
		(
			"- **File / function / line range** (reviewer findings only): "
			f"{record.file} / {record.function} / {record.line_range}"
		),
		f"- **Finding ID:** {record.finding_id}",
		f"- **Root cause ID:** {record.root_cause_id or 'n/a'}",
	])

	if record.layers:
		lines.append(f"- **Layers (dedup):** {', '.join(record.layers)}")
	if record.consolidated_from > 1:
		lines.append(f"- **Consolidated from:** {record.consolidated_from}")
	if record.lane_provenance:
		lines.append(f"- **Lane provenance:** {record.lane_provenance}")
	if record.first_seen:
		lines.append(f"- **First seen:** {record.first_seen}")

	lines.extend([
		"",
		"## Triage state",
		f"- **Status:** {record.status}",
		f"- **Human triage:** {_display_human_triage(record.human_triage)}",
		f"- **Effort estimate:** {record.effort_estimate}",
		f"- **Risk of deferral:** {record.risk_of_deferral}",
		f"- **Needs human review (lane conflict):** {_lower_bool(record.needs_human_review)}",
	])
	return "\n".join(lines)


def _display_human_triage(value: str | None) -> str:
	return value if value else "unreviewed"


def _lower_bool(value: bool) -> str:
	return "true" if value else "false"


def _summary_from_description(description: str) -> str:
	summary = description.strip().split(".", 1)[0].strip()
	return summary[:1].upper() + summary[1:] if summary else ""


__all__ = ["build_finding_issue_title", "render_finding_issue_body"]
