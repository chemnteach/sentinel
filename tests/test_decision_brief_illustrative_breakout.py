"""Illustrative-change break-out: one labeled diff block per Proposed Fix.

Code-owner ask (run-004 follow-up): the "Show illustrative change" preview must
break out ONE labeled code block per proposed fix (a pattern with 2
`proposed_changes` -> 2 labeled diffs), and the redundant "Proposed patterns (N)"
panel must be removed from both the HTML and Markdown briefs.

Payload contract: ``window._patternDiffs[pid]`` becomes a list of
``{"label": <change text>, "diff": <str|None>}`` in change order, capped at
``_DIFF_MAX_BLOCKS_PER_PATTERN`` with a trailing ``{"label": ..., "note": True}``
overflow entry. The renderer type-detects (array = new, string = legacy).

Tests assert on the pure assembly helper + the generated JS/source strings
(no browser engine, no LLM calls).
"""
from __future__ import annotations

import inspect

from sentinel.reports import decision_brief as db


# ── assembly helper: per-change blocks ─────────────────────────────────────
def test_two_changes_yield_two_labeled_blocks():
    changes = ["Encrypt the FTP password", "Move endpoint to SFTP"]
    gen = {"P1#0": "--- a\n+++ b\n@@\n-x\n+y", "P1#1": "--- c\n+++ d\n@@\n-m\n+n"}
    blocks = db._pattern_diff_blocks("P1", changes, gen)
    assert len(blocks) == 2
    assert blocks[0]["label"] == "Encrypt the FTP password"
    assert blocks[0]["diff"] == gen["P1#0"]
    assert blocks[1]["label"] == "Move endpoint to SFTP"
    assert blocks[1]["diff"] == gen["P1#1"]


def test_change_without_generated_diff_gets_none():
    blocks = db._pattern_diff_blocks("P1", ["only change"], gen_map={})
    assert len(blocks) == 1
    assert blocks[0]["label"] == "only change"
    assert blocks[0]["diff"] is None


def test_cap_limits_blocks_and_adds_overflow_note():
    changes = [f"change {i}" for i in range(7)]
    gen = {f"P1#{i}": f"diff{i}" for i in range(7)}
    blocks = db._pattern_diff_blocks("P1", changes, gen)
    # 5 real blocks + 1 overflow note.
    assert db._DIFF_MAX_BLOCKS_PER_PATTERN == 5
    assert len(blocks) == 6
    assert all(not b.get("note") for b in blocks[:5])
    note = blocks[-1]
    assert note.get("note") is True
    assert "2 more" in note["label"]


def test_overflow_note_is_singular_for_one_extra():
    changes = [f"change {i}" for i in range(6)]
    blocks = db._pattern_diff_blocks("P1", changes, gen_map={})
    note = blocks[-1]
    assert note.get("note") is True
    assert "1 more proposed change" in note["label"]
    assert "changes" not in note["label"]


def test_empty_changes_yield_no_blocks():
    assert db._pattern_diff_blocks("P1", [], gen_map={}) == []


# ── JS renderer: labeled blocks + legacy/empty handling ────────────────────
def _js() -> str:
    return db._JS + "\n" + db._JS_STATE


def test_toggle_inline_diff_handles_array_payload():
    js = _js()
    start = js.index("function toggleInlineDiff(")
    nxt = js.find("\nfunction ", start + 1)
    body = js[start: nxt if nxt != -1 else len(js)]
    assert "Array.isArray(pdiff)" in body
    assert "diff-change-label" in body


def test_toggle_inline_diff_keeps_legacy_string_path():
    js = _js()
    start = js.index("function toggleInlineDiff(")
    nxt = js.find("\nfunction ", start + 1)
    body = js[start: nxt if nxt != -1 else len(js)]
    assert "typeof pdiff==='string'" in body or "typeof pdiff === 'string'" in body


def test_toggle_inline_diff_handles_empty_list():
    js = _js()
    start = js.index("function toggleInlineDiff(")
    nxt = js.find("\nfunction ", start + 1)
    body = js[start: nxt if nxt != -1 else len(js)]
    assert "length===0" in body or "length === 0" in body


def test_diff_change_label_css_present():
    assert ".diff-change-label" in db._CSS


# ── GitHub-style colored diff rendering ────────────────────────────────────
def test_github_style_diff_css_present():
    css = db._CSS
    # addition (green), removal (red), hunk header, and container classes.
    assert ".gh-add" in css
    assert ".gh-del" in css
    assert ".gh-hunk" in css
    assert ".gh-diff" in css


def _render_gh_body(js: str) -> str:
    start = js.index("function _renderGhDiff(")
    nxt = js.find("\nfunction ", start + 1)
    return js[start: nxt if nxt != -1 else len(js)]


def test_render_gh_diff_function_exists():
    assert "function _renderGhDiff(" in _js()


def test_render_gh_diff_classifies_add_del_and_hunk():
    body = _render_gh_body(_js())
    # additions -> gh-add, removals -> gh-del, hunk header -> gh-hunk.
    assert "gh-add" in body
    assert "gh-del" in body
    assert "gh-hunk" in body
    # file headers (+++/---) must be detected before the single +/- lines.
    assert "gh-file" in body


def test_toggle_inline_diff_uses_github_renderer_not_plain_pre():
    js = _js()
    start = js.index("function toggleInlineDiff(")
    nxt = js.find("\nfunction ", start + 1)
    body = js[start: nxt if nxt != -1 else len(js)]
    # The array path renders via the GitHub-style helper.
    assert "_renderGhDiff(" in body


def test_change_label_is_numbered_which_change_it_applies_to():
    js = _js()
    start = js.index("function toggleInlineDiff(")
    nxt = js.find("\nfunction ", start + 1)
    body = js[start: nxt if nxt != -1 else len(js)]
    # Communicates which change each diff applies to: "Change N of M".
    assert "Change " in body
    assert " of " in body


# --- click a red (removed) diff line -> jump to that line in Current code -----
def test_gh_del_rows_are_clickable_jump_targets():
    body = _render_gh_body(_js())
    assert "jump-to-source" in body    # removal rows carry the jump action
    assert "dataset.code" in body      # the removed code is stored for matching


def test_jump_to_source_line_function_present():
    assert "function jumpToSourceLine(" in _js()


def test_click_dispatch_routes_jump_to_source():
    assert "act==='jump-to-source'" in _js()


def test_changes_panel_renders_addressable_source_lines():
    # The Current-code panel is built as per-line elements so one can be
    # scrolled to and highlighted.
    assert "cp-src-line" in _js()


def test_source_line_and_highlight_css_present():
    css = db._CSS
    assert ".cp-src-line" in css
    assert ".cp-hit" in css


def test_gh_del_clickable_cursor_css_present():
    assert "gh-del[data-act]" in db._CSS


# ── removal: "Proposed patterns" panel gone from HTML + MD, helper dead ─────
def test_pattern_html_helper_removed():
    assert not hasattr(db, "_pattern_html")


def test_emit_html_no_longer_renders_proposed_patterns_panel():
    src = inspect.getsource(db.emit_html)
    assert "Proposed patterns" not in src
    assert "patterns-section" not in src


def test_emit_md_no_longer_renders_proposed_patterns_section():
    src = inspect.getsource(db.emit_md)
    assert "Proposed patterns" not in src
