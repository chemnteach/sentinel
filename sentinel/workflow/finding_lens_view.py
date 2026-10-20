"""Per-finding dual-lens membership Markdown renderer (§6.5 / §6.6 / §11.2).

This module implements the Phase 4 "per-finding view shows membership
in both" affordance described in
``docs/sentinel-bug-tracking-remediation-workflow-spec.md`` §11.2:

    "Both lenses presented; per-finding view shows membership in both."

The renderer is **profile-agnostic by construction**: it takes only a
:class:`FindingIssueRecord` plus the two optional
:class:`ThemeIssueRecord` records (one per lens) and returns Markdown.
There is no ``config`` parameter; the renderer does not read
profile-specific state vocabulary, and the finding's own ``status``
field is rendered verbatim without interpretation. This is the
load-bearing property for the engagement's end-goal recalibration: a
new profile dropped into the codebase should render correctly from
day 1 without renderer changes.

Membership shape (reconnaissance correction, cycle 11 Commit 6 vs.
Commit 7 sub-instance): per-finding lens membership is one-to-one,
not many-to-many. ``FindingIssueRecord`` carries scalar
``struct_theme_id: str`` and ``concept_theme_id: str``; the empty
string is the "no theme in this lens" sentinel (e.g., a singleton in
the §6.8 small-corpus conceptual fallback). The renderer therefore
takes a single optional ``ThemeIssueRecord`` per lens rather than a
sequence. Recorded as a Family-2 prompt-anticipation sub-instance
for cycle 11 closure: reconnaissance-time correction, not
implementation-time; preserves the impl-time sub-pattern dropoff
prediction.

Renderer conventions match :mod:`sentinel.workflow.theme_renderer`
and :mod:`sentinel.workflow.issue_renderer`: ``##`` H2 section
headers, line-joined string output, no module-level state. The
cross-lens-view affordance from §6.5 / §6.6 is mirrored here:
the reviewer can see at a glance whether the conceptual lens groups
this finding with a different set of structurally-distinct findings,
and vice versa.
"""

from __future__ import annotations

from sentinel.workflow.models import FindingIssueRecord, ThemeIssueRecord


_NO_MEMBERSHIP = "_no themes in this lens_"


def render_finding_lens_membership(
	finding: FindingIssueRecord,
	*,
	struct_theme: ThemeIssueRecord | None = None,
	concept_theme: ThemeIssueRecord | None = None,
) -> str:
	"""Render the per-finding dual-lens membership view as Markdown.

	The returned string contains two H2 sections (one per lens) plus
	a cross-lens-view line that summarizes whether the two lenses
	consolidate or split this finding.

	Inputs:
	    finding: the :class:`FindingIssueRecord` whose lens
	        membership is being rendered.
	    struct_theme: the structural-lens :class:`ThemeIssueRecord`
	        for ``finding.struct_theme_id``, or ``None`` if the
	        finding has no structural-lens theme membership (the
	        ``struct_theme_id`` field is empty) or if the caller
	        did not supply the record. The renderer does not look
	        the record up itself; the caller is responsible for the
	        lookup.
	    concept_theme: same shape for the conceptual lens.

	Returns:
	    A Markdown string with three sections: "Structural lens
	    membership", "Conceptual lens membership", "Cross-lens view".

	The renderer is pure: no I/O, no config reads, no
	state-machine interpretation. The finding's ``status`` field is
	NOT rendered here; that lives on the issue-body renderer.
	"""
	lines = [
		f"## Structural lens membership",
		_render_lens_section("struct", finding.struct_theme_id, struct_theme),
		"",
		f"## Conceptual lens membership",
		_render_lens_section("concept", finding.concept_theme_id, concept_theme),
		"",
		f"## Cross-lens view",
		_render_cross_lens(finding, struct_theme, concept_theme),
	]
	return "\n".join(lines)


def _render_lens_section(
	lens: str,
	theme_id: str,
	theme: ThemeIssueRecord | None,
) -> str:
	if not theme_id or theme is None:
		return _NO_MEMBERSHIP
	# Sanity: when both are provided, the ids must agree. This is a
	# caller-side contract; the renderer surfaces a clear marker rather
	# than silently rendering the wrong theme.
	if theme.theme_id != theme_id:
		return (
			f"_lens-membership mismatch: finding records "
			f"{lens}_theme_id={theme_id!r} but supplied theme is "
			f"{theme.theme_id!r}_"
		)
	member_count = len(theme.member_finding_ids)
	return (
		f"- **{theme.theme_id}** — {theme.title}\n"
		f"- **Members in this theme:** {member_count} findings"
	)


def _render_cross_lens(
	finding: FindingIssueRecord,
	struct_theme: ThemeIssueRecord | None,
	concept_theme: ThemeIssueRecord | None,
) -> str:
	has_struct = bool(finding.struct_theme_id) and struct_theme is not None
	has_concept = bool(finding.concept_theme_id) and concept_theme is not None
	if has_struct and has_concept:
		# Cross-lens-view affordance per §6.5 / §6.6: signal whether
		# the two lenses consolidate the finding into different
		# member sets.
		struct_set = set(struct_theme.member_finding_ids)
		concept_set = set(concept_theme.member_finding_ids)
		shared = len(struct_set & concept_set)
		struct_only = len(struct_set - concept_set)
		concept_only = len(concept_set - struct_set)
		return (
			f"- This finding belongs to both lenses. Theme overlap: "
			f"{shared} shared / {struct_only} struct-only / "
			f"{concept_only} concept-only members."
		)
	if has_struct:
		return (
			"- This finding has a structural-lens theme but no "
			"conceptual-lens theme (e.g., singleton under the §6.8 "
			"small-corpus fallback)."
		)
	if has_concept:
		return (
			"- This finding has a conceptual-lens theme but no "
			"structural-lens theme."
		)
	return (
		"- This finding has no theme membership in either lens "
		"(rendered standalone)."
	)


__all__ = ["render_finding_lens_membership"]
