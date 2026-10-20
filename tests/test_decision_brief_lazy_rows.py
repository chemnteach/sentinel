"""Lazy per-group finding-row rendering (load-performance).

Finding rows are emitted inside a per-group <template> and cloned into the live
DOM only when the group is expanded, so a large brief no longer builds/lays out
every row at load. Row HTML stays literal (in the template) so it remains
greppable and the export/rollup/restore logic (state-driven) is unchanged.
"""
from __future__ import annotations

import json
import re

from sentinel.reports import decision_brief as db
from sentinel.reports.decision_brief import render


def _seed(tmp_path, findings, triaged=None):
    groups = [{"group_id": "G1", "member_finding_ids": [f["id"] for f in findings]}]
    (tmp_path / "findings.json").write_text(json.dumps(findings), encoding="utf-8")
    (tmp_path / "semantic_groups.json").write_text(json.dumps(groups), encoding="utf-8")
    (tmp_path / "remediation_patterns.json").write_text("[]", encoding="utf-8")
    if triaged:
        (tmp_path / "tech_debt.json").write_text(json.dumps(triaged), encoding="utf-8")
    return tmp_path


_FINDINGS = [
    {"id": "F1", "module": "m:core", "finding_kind": "logic_bug",
     "severity": "P1", "file": "a.py", "description": "one"},
    {"id": "F2", "module": "m:core", "finding_kind": "logic_bug",
     "severity": "P2", "file": "b.py", "description": "two"},
]


def _render(tmp_path):
    state = _seed(tmp_path, _FINDINGS)
    out = tmp_path / "brief.html"
    render(state, out, tmp_path / "brief.md", "Run")
    return out.read_text(encoding="utf-8")


def test_finding_rows_live_container_is_empty_at_load(tmp_path):
    html = _render(tmp_path)
    # The findings-rows container ships empty; rows live in the template.
    assert '<div class="findings-rows"></div>' in html


def test_finding_rows_are_in_a_per_group_template(tmp_path):
    html = _render(tmp_path)
    assert 'template class="gr-tpl"' in html
    # A finding row is present INSIDE the file (as literal template content).
    assert 'class="finding-row' in html
    assert 'data-fid="F1"' in html


def test_details_section_carries_gid_for_lazy_wireup(tmp_path):
    html = _render(tmp_path)
    assert re.search(r'<details class="findings-section" data-gid="[^"]+">', html)


def test_toggle_listener_and_wire_present_in_js(tmp_path):
    js = db._JS + "\n" + db._JS_STATE
    # A toggle handler injects the group's rows on first expand.
    assert "addEventListener('toggle'" in js
    assert "gr-tpl" in js
    assert "cloneNode(true)" in js
    assert "function _wireInjectedRows(" in js


def test_restore_seeds_prior_triage_from_js_map_not_live_dom(tmp_path):
    html = _render(tmp_path)
    # Prior triage is seeded from a JS map so it survives lazy rows.
    assert "window._fidTriaged=" in html
    js = db._JS + "\n" + db._JS_STATE
    assert "_fidTriaged" in js
    # Restore no longer depends on querying live .finding-row[data-triaged].
    assert "querySelectorAll('.finding-row[data-triaged]')" not in js


def test_row_content_still_present_regression(tmp_path):
    html = _render(tmp_path)
    # Lazy rendering must not drop row content from the file.
    assert "finding-triage" in html
    assert 'data-act="finding-decision"' in html
    assert 'id="fr-F1"' in html


def test_fid_meta_map_emitted_for_export(tmp_path):
    html = _render(tmp_path)
    # Finding file/kind/location are emitted as a map so the DEFER suppression
    # export works without reading the (lazy) finding row from the live DOM.
    assert "window._fidMeta=" in html
    assert '"F1"' in html


def test_export_defer_suppression_reads_map_not_live_row(tmp_path):
    js = db._JS + "\n" + db._JS_STATE
    # The export's DEFER suppression_rule must be built from _fidMeta, not by
    # querying the live finding row (which may be un-rendered under lazy rows).
    start = js.index("function exportDecisions(")
    nxt = js.find("\nfunction ", start + 1)
    body = js[start: nxt if nxt != -1 else len(js)]
    assert "_fidMeta" in body
    assert "document.querySelector('[data-fid=\"'+en.fid+'\"]')" not in body
