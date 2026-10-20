"""Group-level comment (rationale) on Defer/Exclude for decision_brief.py.

Code-owner ask: when a whole cluster is Deferred or Excluded from the group
disposition bar, there must be a single place to type a comment. Previously a
rationale box only existed per finding (buried in the collapsed rows).

The group comment fans its text to every member finding's `rationale`, so the
Export payload (which iterates `_findingDecisions`) is unchanged — no new export
field. Tests assert on the generated HTML / JS strings (no browser engine).
"""
from __future__ import annotations

from sentinel.reports import decision_brief as db


def _all_js() -> str:
    return db._JS + "\n" + db._JS_STATE


# ── HTML: the group disposition bar carries a comment box ──────────────────
def test_group_disposition_has_rationale_textarea():
    h = db._group_disposition_html("G1", 3)
    assert 'data-act="group-rationale"' in h
    assert 'id="gr-G1"' in h
    assert 'data-gid="G1"' in h
    # Hidden until the group is deferred/excluded; no inline handlers.
    assert "hidden" in h
    assert "onclick=" not in h and "oninput=" not in h


def test_group_rationale_box_is_a_textarea_with_group_class():
    h = db._group_disposition_html("G1", 3)
    assert "<textarea" in h
    assert "group-rationale" in h


# ── JS: fan-out + reveal/hide wiring ───────────────────────────────────────
def test_set_group_rationale_function_exists():
    js = _all_js()
    assert "function setGroupRationale(" in js


def test_set_group_rationale_fans_to_member_findings():
    js = _all_js()
    start = js.index("function setGroupRationale(")
    nxt = js.find("\nfunction ", start + 1)
    body = js[start: nxt if nxt != -1 else len(js)]
    # Iterates the group's findings and writes each member's rationale.
    assert "_groupFids(" in body
    assert "rec.rationale" in body


def test_group_decision_toggles_rationale_box_visibility():
    js = _all_js()
    start = js.index("function setGroupDecision(")
    nxt = js.find("\nfunction ", start + 1)
    body = js[start: nxt if nxt != -1 else len(js)]
    assert "gr-" in body  # references the group rationale element id


def test_clear_group_decision_clears_rationale_box():
    js = _all_js()
    start = js.index("function clearGroupDecision(")
    nxt = js.find("\nfunction ", start + 1)
    body = js[start: nxt if nxt != -1 else len(js)]
    assert "gr-" in body


def test_input_dispatch_handles_group_rationale():
    js = _all_js()
    assert "act==='group-rationale'" in js


def test_restore_repopulates_group_rationale_box():
    js = _all_js()
    start = js.index("function _applyAllDecided(")
    nxt = js.find("\nfunction ", start + 1)
    body = js[start: nxt if nxt != -1 else len(js)]
    assert "group-rationale" in body
