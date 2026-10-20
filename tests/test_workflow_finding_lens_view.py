"""Tests for sentinel.workflow.finding_lens_view (Phase 4 part C)."""

from __future__ import annotations

import pytest

from sentinel.workflow.finding_lens_view import render_finding_lens_membership
from sentinel.workflow.models import (
	FindingIssueRecord,
	ThemeIssueRecord,
	ThemeReference,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_finding(
	*,
	struct_theme_id: str = "T-001",
	concept_theme_id: str = "C-007",
	status: str = "triage",
) -> FindingIssueRecord:
	return FindingIssueRecord(
		triage_item_id="TD-0001",
		finding_id="F-001",
		summary="example",
		severity="P2",
		skill="layer2a-quick-review",
		module="m.py",
		category="error_handling",
		location="m.py:1",
		description="desc",
		suggested_fix="fix",
		status=status,
		human_triage=None,
		effort_estimate="low",
		risk_of_deferral="low",
		primary_theme=ThemeReference(
			lens="struct",
			theme_id=struct_theme_id or "T-001",
			title="primary",
			member_count=1,
		),
		struct_theme_id=struct_theme_id,
		concept_theme_id=concept_theme_id,
	)


def _make_theme(
	lens: str = "struct",
	theme_id: str = "T-001",
	title: str = "Struct theme title",
	members: list[str] | None = None,
) -> ThemeIssueRecord:
	return ThemeIssueRecord(
		lens=lens,  # type: ignore[arg-type]
		theme_id=theme_id,
		source_id="src-1",
		title=title,
		member_finding_ids=list(members) if members is not None else ["F-001", "F-002"],
	)


# ---------------------------------------------------------------------------
# Behavior tests
# ---------------------------------------------------------------------------


def test_render_finding_lens_membership_both_lenses() -> None:
	finding = _make_finding()
	struct_theme = _make_theme(
		lens="struct",
		theme_id="T-001",
		title="Struct theme title",
		members=["F-001", "F-002", "F-003"],
	)
	concept_theme = _make_theme(
		lens="concept",
		theme_id="C-007",
		title="Concept theme title",
		members=["F-001", "F-004"],
	)
	body = render_finding_lens_membership(
		finding,
		struct_theme=struct_theme,
		concept_theme=concept_theme,
	)
	# Both H2 headers present.
	assert "## Structural lens membership" in body
	assert "## Conceptual lens membership" in body
	assert "## Cross-lens view" in body
	# Theme titles and ids surface.
	assert "T-001" in body and "Struct theme title" in body
	assert "C-007" in body and "Concept theme title" in body
	# Member counts surface (3 struct, 2 concept).
	assert "3 findings" in body
	assert "2 findings" in body
	# Cross-lens overlap line surfaces (1 shared, 2 struct-only, 1 concept-only).
	assert "1 shared" in body
	assert "2 struct-only" in body
	assert "1 concept-only" in body


def test_render_finding_lens_membership_struct_only() -> None:
	finding = _make_finding(concept_theme_id="")
	struct_theme = _make_theme(lens="struct", theme_id="T-001")
	body = render_finding_lens_membership(
		finding,
		struct_theme=struct_theme,
		concept_theme=None,
	)
	assert "## Structural lens membership" in body
	assert "T-001" in body
	assert "## Conceptual lens membership" in body
	assert "_no themes in this lens_" in body
	# Cross-lens fallback line for struct-only.
	assert "structural-lens theme but no" in body
	assert "conceptual-lens theme" in body


def test_render_finding_lens_membership_concept_only() -> None:
	finding = _make_finding(struct_theme_id="")
	concept_theme = _make_theme(lens="concept", theme_id="C-007")
	body = render_finding_lens_membership(
		finding,
		struct_theme=None,
		concept_theme=concept_theme,
	)
	assert "_no themes in this lens_" in body
	assert "C-007" in body
	assert "conceptual-lens theme but no" in body


def test_render_finding_lens_membership_neither() -> None:
	finding = _make_finding(struct_theme_id="", concept_theme_id="")
	body = render_finding_lens_membership(finding)
	# Both lens sections render the "no themes" indicator.
	assert body.count("_no themes in this lens_") == 2
	assert "no theme membership in either lens" in body


def test_render_finding_lens_membership_markdown_shape() -> None:
	"""Output is well-formed Markdown: three H2 headers, line-joined,
	no trailing whitespace on header lines."""
	body = render_finding_lens_membership(_make_finding(struct_theme_id="", concept_theme_id=""))
	header_lines = [ln for ln in body.splitlines() if ln.startswith("## ")]
	assert header_lines == [
		"## Structural lens membership",
		"## Conceptual lens membership",
		"## Cross-lens view",
	]


def test_render_finding_lens_membership_mismatched_theme_id_marker() -> None:
	"""When the caller supplies a theme whose id does not match the
	finding's recorded id, the renderer surfaces a clear marker
	rather than silently rendering the wrong theme."""
	finding = _make_finding(struct_theme_id="T-001", concept_theme_id="")
	wrong_struct = _make_theme(lens="struct", theme_id="T-WRONG")
	body = render_finding_lens_membership(
		finding,
		struct_theme=wrong_struct,
	)
	assert "lens-membership mismatch" in body
	assert "T-001" in body
	assert "T-WRONG" in body


def test_render_finding_lens_membership_is_profile_agnostic() -> None:
	"""Load-bearing property for the engagement's end-goal recalibration:
	the renderer has no ``config`` parameter and does not interpret
	the finding's ``status`` field. Two findings differing only in
	``status`` (one from each profile's vocabulary) produce identical
	rendered output."""
	# owner_gate-style status label.
	acme = _make_finding(status="owner-approved")
	# solo_local_remediating-style status label.
	solo = _make_finding(status="approved")
	# Same theme records both ways.
	struct_theme = _make_theme(lens="struct", theme_id="T-001")
	concept_theme = _make_theme(lens="concept", theme_id="C-007")
	acme_body = render_finding_lens_membership(
		acme, struct_theme=struct_theme, concept_theme=concept_theme,
	)
	solo_body = render_finding_lens_membership(
		solo, struct_theme=struct_theme, concept_theme=concept_theme,
	)
	assert acme_body == solo_body
	# And neither profile's status label leaks into the output.
	assert "owner-approved" not in acme_body
	assert "approved" not in solo_body


@pytest.mark.parametrize(
	"struct_id,concept_id,expected_substring",
	[
		("T-001", "C-007", "belongs to both lenses"),
		("T-001", "", "structural-lens theme but no"),
		("", "C-007", "conceptual-lens theme but no"),
		("", "", "no theme membership in either lens"),
	],
)
def test_render_finding_lens_membership_cross_lens_summary_branches(
	struct_id: str, concept_id: str, expected_substring: str
) -> None:
	"""Cross-lens view summary line covers all four (struct, concept)
	presence combinations."""
	finding = _make_finding(struct_theme_id=struct_id, concept_theme_id=concept_id)
	struct_theme = _make_theme(lens="struct", theme_id="T-001") if struct_id else None
	concept_theme = _make_theme(lens="concept", theme_id="C-007") if concept_id else None
	body = render_finding_lens_membership(
		finding, struct_theme=struct_theme, concept_theme=concept_theme,
	)
	assert expected_substring in body
