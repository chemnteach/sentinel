"""XSS + conditions-panel state-machine cluster for decision_brief.py.

Covers:
  - FIX-REQUIRED-060: no inline event handlers interpolating finding IDs;
    actions bound via data-* attributes + a single delegated listener.
  - FIX-REQUIRED-288: toggleGroupConditions reopens a hidden panel explicitly.
  - FIX-REQUIRED-317: openConditionsPanel guards dataset JSON parsing.
  - FIX-REQUIRED-328: buildConditionsStructured guards dataset JSON parsing.

Tests assert on the generated HTML / JS strings (no browser engine available),
which is the observable contract of the report renderer.
"""
from __future__ import annotations

from sentinel.reports import decision_brief as db


# A finding id crafted to break out of a single-quoted JS string literal.
MALICIOUS_FID = "x');alert(document.cookie);//"


def _fn_body(js: str, name: str) -> str:
    """Return the source of a top-level JS function by name (up to the next one)."""
    start = js.index("function " + name)
    nxt = js.find("\nfunction ", start + 1)
    return js[start: nxt if nxt != -1 else len(js)]


def _all_js() -> str:
    """The full client script as emitted: both JS blocks concatenated."""
    return db._JS + "\n" + db._JS_STATE


# ── FIX-REQUIRED-060 ──────────────────────────────────────────────────────
def test_finding_row_has_no_inline_event_handlers():
    row = db._finding_row_html({"id": "F1", "severity": "P1"}, group_id_safe="G1", row_index=0)
    assert "onchange=" not in row
    assert "oninput=" not in row
    assert "onclick=" not in row
    # Actions are bound declaratively via data attributes.
    assert 'data-act="finding-decision"' in row
    assert 'data-act="finding-rationale"' in row
    assert 'data-act="finding-clear"' in row


def test_finding_row_malicious_fid_never_appears_in_executable_js():
    row = db._finding_row_html({"id": MALICIOUS_FID, "severity": "P1"},
                               group_id_safe="G1", row_index=0)
    # No inline handler surface remains for the payload to execute in.
    assert "onchange=" not in row and "oninput=" not in row and "onclick=" not in row
    # The single-quote used to break out of a JS literal is HTML-escaped, so the
    # raw breakout sequence never appears; the id survives only in escaped form.
    assert "');alert" not in row
    assert "&#x27;" in row


def test_group_disposition_has_no_inline_handlers():
    h = db._group_disposition_html("G1", 3)
    assert "onclick=" not in h
    assert 'data-act="group-decision"' in h
    assert 'data-act="group-toggle-conditions"' in h
    assert 'data-act="group-clear"' in h


def test_conditions_panel_has_no_inline_handlers():
    h = db._conditions_panel_html("G1")
    assert "onclick=" not in h
    assert "onchange=" not in h
    assert "oninput=" not in h
    assert "onkeydown=" not in h
    assert 'data-act="add-criterion"' in h
    assert 'data-act="update-scope"' in h
    assert 'data-act="update-directive"' in h
    assert 'data-act="clear-conditions"' in h


def test_js_registers_delegated_listeners():
    js = _all_js()
    assert "addEventListener('click'" in js
    assert "addEventListener('change'" in js
    assert "addEventListener('input'" in js
    assert "dataset.act" in js


# ── FIX-REQUIRED-288 ─────────────────────────────────────
def test_toggle_group_conditions_reopens_panel_explicitly():
    body = _fn_body(_all_js(), "toggleGroupConditions")
    # The reopen path must explicitly re-show the panel rather than relying only
    # on a decision-change side effect.
    assert "openConditionsPanel(gid)" in body


# ── FIX-REQUIRED-317 ──────────────────────────────────────────────────────
def test_open_conditions_panel_guards_dataset_parse():
    body = _fn_body(_all_js(), "openConditionsPanel")
    assert "JSON.parse(card.dataset.proposedChanges" not in body
    assert "JSON.parse(card.dataset.acceptanceCriteria" not in body
    assert "JSON.parse(card.dataset.groupFiles" not in body
    assert "_parseDataset" in body


# ── FIX-REQUIRED-328 ──────────────────────────────────────────────────────
def test_build_conditions_structured_guards_dataset_parse():
    body = _fn_body(_all_js(), "buildConditionsStructured")
    assert "JSON.parse(card.dataset.changeIndex" not in body
    assert "JSON.parse(card.dataset.criterionIndex" not in body
    assert "_parseDataset" in body
