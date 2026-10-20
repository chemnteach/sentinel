"""Markdown renderers for structural and conceptual theme issues."""

from __future__ import annotations

from sentinel.workflow.models import CrossLensReference, ThemeIssueRecord, ThemeMember


def render_theme_issue_body(record: ThemeIssueRecord) -> str:
	lens_name = _lens_name(record.lens)
	other_lens_plural = "concept themes" if record.lens == "struct" else "structural themes"
	lines = [
		f"## {lens_name} Theme {record.theme_id}: {record.title}",
		"",
		f"**Lens:** {_lens_description(record.lens)}",
		f"**Source ID:** {record.source_id}",
		f"**Members:** {len(record.member_finding_ids)} findings",
		f"**Skills involved:** {', '.join(sorted(record.skills_involved))}",
		f"**Categories:** {', '.join(sorted(record.categories))}",
		f"**Modules / files affected:** {', '.join(sorted(record.modules_affected))}",
		f"**Aggregate severity:** {record.aggregate_severity}",
		f"**Aggregate effort:** {record.aggregate_effort}",
		f"**Aggregate risk of deferral:** {record.aggregate_risk_of_deferral}",
		"",
		"## Cross-lens view",
		(
			f"- **This theme also spans {len(record.cross_lens_themes)} {other_lens_plural}:** "
			f"{_format_cross_lens_links(record.cross_lens_themes)}"
		),
		"- This tells the reviewer whether the other lens splits or consolidates this group.",
		"",
		"## Root cause summary",
		record.root_cause_summary,
		"",
		"## Unified remediation strategy",
		record.remediation_strategy,
		"",
		"## Member findings",
		"| Severity | File:line | Skill | Other-lens theme | One-line summary |",
		"|----------|-----------|-------|------------------|------------------|",
	]
	lines.extend(_member_row(member) for member in record.members)
	return "\n".join(lines)


def _lens_name(lens: str) -> str:
	return "Structural" if lens == "struct" else "Conceptual"


def _lens_description(lens: str) -> str:
	return "Structural (root_cause_id)" if lens == "struct" else "Conceptual (HDBSCAN over description)"


def _format_cross_lens_links(cross_lens_themes: list[CrossLensReference]) -> str:
	if not cross_lens_themes:
		return "none"
	rendered = []
	for theme in cross_lens_themes:
		label = f"{theme.theme_id} — {theme.title}"
		if theme.issue_url:
			rendered.append(f"[{label}]({theme.issue_url})")
		else:
			rendered.append(label)
	return ", ".join(rendered)


def _member_row(member: ThemeMember) -> str:
	location = member.file_path if not member.line_range else f"{member.file_path}:{member.line_range}"
	return (
		f"| {member.severity} | {location} | {member.skill} | "
		f"{member.other_lens_theme_id} | {member.summary} |"
	)


__all__ = ["render_theme_issue_body"]
