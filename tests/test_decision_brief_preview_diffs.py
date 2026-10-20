"""Render-side tests for Part A′ — illustrative per-bundle diff previews.

Verifies the --preview-diffs wiring in ``decision_brief.render``: off by default,
embedded collapsed with a disclaimer when on, and — crucially — the illustrative
diff is confined to its ``<pre>`` and never leaks into a ``data-*`` attribute that
the decisions export would pick up.
"""
from __future__ import annotations

import json
import re

import sentinel.reports.decision_brief as db
from sentinel.reports.decision_brief import render

_FAKE_DIFF = (
    "--- a/core/a.py\n"
    "+++ b/core/a.py\n"
    "@@ -40,2 +40,3 @@\n"
    "-do_thing()\n"
    "+guard()\n"
    "+do_thing()"
)


class _FakeDiffClient:
    def call_model(self, system_prompt="", user_message="", **kw):
        return type("R", (), {"content": _FAKE_DIFF})()


def _seed_with_bundle(tmp_path):
    findings = [
        {"id": "F1", "module": "demo:core", "finding_kind": "logic_bug",
         "severity": "P1", "file": "core/a.py"},
        {"id": "F2", "module": "demo:core", "finding_kind": "logic_bug",
         "severity": "P2", "file": "core/a.py"},
    ]
    groups = [{"group_id": "G1", "member_finding_ids": ["F1", "F2"]}]
    patterns = [
        {"group_id": "G1", "pattern_id": "P1", "title": "Fix the boundary",
         "pattern_kind": "logic-fix",
         "proposed_changes": ["Guard the call at core/a.py:40"],
         "acceptance_criteria": ["The off-by-one no longer occurs"]},
    ]
    bundles = [{
        "bundle_id": "B-001",
        "root_cause_finding_id": "F1",
        "root_cause_title": "Fix the boundary",
        "root_cause_severity": "P1",
        "bundle_status": "UNDECIDED",
        "confidence": "HIGH",
        "affected_files": ["core/a.py"],
        "member_findings": [
            {"finding_id": "F1", "severity": "P1", "title": "root",
             "predicted_resolved": True},
            {"finding_id": "F2", "severity": "P2", "title": "cascade",
             "predicted_resolved": True},
        ],
    }]
    (tmp_path / "findings.json").write_text(json.dumps(findings), encoding="utf-8")
    (tmp_path / "semantic_groups.json").write_text(json.dumps(groups), encoding="utf-8")
    (tmp_path / "remediation_patterns.json").write_text(json.dumps(patterns), encoding="utf-8")
    (tmp_path / "bundles.json").write_text(json.dumps(bundles), encoding="utf-8")
    return tmp_path


def _patch_client(monkeypatch):
    import sentinel.api.client as apiclient
    import sentinel.config as cfg
    monkeypatch.setattr(apiclient, "create_model_client", lambda *a, **k: _FakeDiffClient())
    monkeypatch.setattr(cfg, "load_config", lambda: object())


def test_preview_diffs_off_by_default(tmp_path, monkeypatch):
    _patch_client(monkeypatch)
    state = _seed_with_bundle(tmp_path)
    out_html = tmp_path / "brief.html"
    render(state, out_html, tmp_path / "brief.md", "Run")  # preview_diffs defaults False
    html = out_html.read_text(encoding="utf-8")
    # ``bundle-diff`` also appears in the CSS; assert on the rendered element.
    assert 'class="bundle-diff"' not in html
    assert "guard()" not in html


def test_preview_diffs_embeds_collapsed_diff_with_disclaimer(tmp_path, monkeypatch):
    _patch_client(monkeypatch)
    state = _seed_with_bundle(tmp_path)
    out_html = tmp_path / "brief.html"
    render(state, out_html, tmp_path / "brief.md", "Run", preview_diffs=True)
    html = out_html.read_text(encoding="utf-8")
    assert 'class="bundle-diff"' in html
    assert "Show exact change" in html
    assert "NOT the governed Phase 2 change" in html
    assert "guard()" in html  # the illustrative diff body is present


def test_preview_diff_not_in_export_data_attributes(tmp_path, monkeypatch):
    """Governance guard: the illustrative diff must live only in its <pre>, never
    in a data-* attribute the decisions export reads."""
    _patch_client(monkeypatch)
    state = _seed_with_bundle(tmp_path)
    out_html = tmp_path / "brief.html"
    render(state, out_html, tmp_path / "brief.md", "Run", preview_diffs=True)
    html = out_html.read_text(encoding="utf-8")
    # Remove every <pre>...</pre> block; the diff text must not survive anywhere
    # else (e.g. an attribute), so nothing downstream treats it as sanctioned.
    without_pre = re.sub(r"<pre[^>]*>.*?</pre>", "", html, flags=re.S)
    assert "guard()" not in without_pre


def _seed_no_bundle(tmp_path):
    """A run parked at the triage gate: findings/groups/patterns, but NO bundles."""
    findings = [
        {"id": "F1", "module": "demo:core", "finding_kind": "logic_bug",
         "severity": "P1", "file": "core/a.py"},
        {"id": "F2", "module": "demo:core", "finding_kind": "logic_bug",
         "severity": "P2", "file": "core/a.py"},
    ]
    groups = [{"group_id": "G1", "member_finding_ids": ["F1", "F2"]}]
    patterns = [
        {"group_id": "G1", "pattern_id": "P1", "title": "Fix the boundary",
         "pattern_kind": "logic-fix",
         "proposed_changes": ["Guard the call at core/a.py:40"],
         "acceptance_criteria": ["ok"]},
    ]
    (tmp_path / "findings.json").write_text(json.dumps(findings), encoding="utf-8")
    (tmp_path / "semantic_groups.json").write_text(json.dumps(groups), encoding="utf-8")
    (tmp_path / "remediation_patterns.json").write_text(json.dumps(patterns), encoding="utf-8")
    return tmp_path


def test_preview_diffs_pattern_anchored_when_no_bundles(tmp_path, monkeypatch):
    """At triage (no bundles.json), the diff anchors on the remediation pattern and
    is exposed via window._patternDiffs + data-pattern-id, so the 'View changes'
    panel can show it. This is the path that makes previews available DURING triage."""
    _patch_client(monkeypatch)
    state = _seed_no_bundle(tmp_path)
    out_html = tmp_path / "brief.html"
    render(state, out_html, tmp_path / "brief.md", "Run", preview_diffs=True)
    html = out_html.read_text(encoding="utf-8")
    assert "window._patternDiffs=" in html
    assert '"P1"' in html          # keyed by pattern id
    assert "guard()" in html       # the illustrative diff body is embedded
    assert 'data-pattern-id="P1"' in html
    assert 'class="bundle-diff"' not in html  # no bundle path at triage


def test_pattern_diff_inline_toggle_present(tmp_path, monkeypatch):
    """Findings whose pattern has a diff get an inline 'Show illustrative change'
    toggle on the row (pulls from window._patternDiffs on expand)."""
    _patch_client(monkeypatch)
    state = _seed_no_bundle(tmp_path)
    out_html = tmp_path / "brief.html"
    render(state, out_html, tmp_path / "brief.md", "Run", preview_diffs=True)
    html = out_html.read_text(encoding="utf-8")
    assert 'data-act="toggle-inline-diff"' in html
    assert 'class="row-diff-toggle"' in html
    assert "Show illustrative change" in html
    assert "function toggleInlineDiff" in html


def test_inline_conditions_container_rendered(tmp_path, monkeypatch):
    """Findings whose pattern has proposed changes get an empty inline conditions
    container + data-line-range on the row (populated lazily by JS on AWC)."""
    _patch_client(monkeypatch)
    state = _seed_no_bundle(tmp_path)
    out_html = tmp_path / "brief.html"
    render(state, out_html, tmp_path / "brief.md", "Run")
    html = out_html.read_text(encoding="utf-8")
    assert 'class="finding-cond"' in html
    assert "data-line-range=" in html


def test_no_inline_toggle_without_preview_diffs(tmp_path, monkeypatch):
    _patch_client(monkeypatch)
    state = _seed_no_bundle(tmp_path)
    out_html = tmp_path / "brief.html"
    render(state, out_html, tmp_path / "brief.md", "Run")  # off
    html = out_html.read_text(encoding="utf-8")
    assert 'data-act="toggle-inline-diff"' not in html


def test_pattern_diff_not_in_row_data_attributes(tmp_path, monkeypatch):
    """Governance: the pattern diff lives only in the window._patternDiffs script,
    never in a data-* attribute the decisions export could read."""
    _patch_client(monkeypatch)
    state = _seed_no_bundle(tmp_path)
    out_html = tmp_path / "brief.html"
    render(state, out_html, tmp_path / "brief.md", "Run", preview_diffs=True)
    html = out_html.read_text(encoding="utf-8")
    without_scripts = re.sub(r"<script>.*?</script>", "", html, flags=re.S)
    assert "guard()" not in without_scripts


def test_preview_diffs_off_no_pattern_diffs(tmp_path, monkeypatch):
    _patch_client(monkeypatch)
    state = _seed_no_bundle(tmp_path)
    out_html = tmp_path / "brief.html"
    render(state, out_html, tmp_path / "brief.md", "Run")  # preview_diffs defaults False
    html = out_html.read_text(encoding="utf-8")
    assert "window._patternDiffs={}" in html  # empty map, no generation
    assert "guard()" not in html


def test_attach_bundle_diffs_degrades_when_client_init_fails(tmp_path, monkeypatch):
    """If LLM client construction fails, diffs are skipped and the brief still
    renders (no bundle-diff markup, no crash)."""
    import sentinel.api.client as apiclient

    def _boom(*a, **k):
        raise RuntimeError("no gateway")

    monkeypatch.setattr(apiclient, "create_model_client", _boom)
    state = _seed_with_bundle(tmp_path)
    out_html = tmp_path / "brief.html"
    render(state, out_html, tmp_path / "brief.md", "Run", preview_diffs=True)
    html = out_html.read_text(encoding="utf-8")
    assert 'class="bundle-diff"' not in html
    assert "Remediation Decision Brief" in html  # brief still rendered


# --- tight-first, widen-on-empty grounding (fixes wrong-method "current code") ---
class _GroundedFakeClient:
    """Returns a diff that removes ONE specific target line, regardless of prompt.
    Grounding in _parse_response gates it against the unit's source window."""

    def __init__(self, target_line: str, new_line: str = "+// fixed"):
        self._diff = (
            "--- a/Svc.cs\n+++ b/Svc.cs\n@@ -2,1 +2,1 @@\n"
            f"-{target_line}\n{new_line}"
        )
        self.calls = 0

    def call_model(self, system_prompt="", user_message="", **kw):
        self.calls += 1
        return type("R", (), {"content": self._diff})()


def _write_svc(tmp_path, target_line: str):
    # target line at line 2; finding cites line 55 (far below the tight window).
    lines = ["// file header", target_line]
    lines += [f"    // filler line {i}" for i in range(3, 61)]
    (tmp_path / "Svc.cs").write_text("\n".join(lines), encoding="utf-8")


def _svc_finding():
    return {
        "id": "F1", "file": "Svc.cs", "line_range": "55",
        "_pattern_id": "P1", "_group_changes": ["harden the call at Svc.cs:55"],
        "finding_kind": "logic_bug",
    }


def test_attach_pattern_diffs_widens_when_tight_window_ungrounded(tmp_path, monkeypatch):
    """The editable target sits far from the finding's cited line, so the TIGHT
    window misses it (tier-1 grounding fails) and the WIDER fallback recovers it —
    'if it shows no current code, loop back and cast a larger net'."""
    _patch_client(monkeypatch)
    target = "var conn = Open();  // GROUND_TARGET"
    _write_svc(tmp_path, target)
    monkeypatch.setattr(
        "sentinel.api.client.create_model_client",
        lambda *a, **k: _GroundedFakeClient(target),
    )
    out = db._attach_pattern_diffs([_svc_finding()], str(tmp_path), str(tmp_path))
    assert "P1" in out
    blocks = out["P1"]
    assert blocks and blocks[0].get("diff")
    assert "GROUND_TARGET" in blocks[0]["diff"]


def test_attach_pattern_diffs_drops_diff_when_no_window_grounds_it(tmp_path, monkeypatch):
    """When the model's 'current code' exists in NO window (pure fabrication),
    even the wide fallback rejects it -> no misleading diff block."""
    _patch_client(monkeypatch)
    _write_svc(tmp_path, "var conn = Open();  // REAL")
    monkeypatch.setattr(
        "sentinel.api.client.create_model_client",
        lambda *a, **k: _GroundedFakeClient("this line is nowhere in the file"),
    )
    out = db._attach_pattern_diffs([_svc_finding()], str(tmp_path), str(tmp_path))
    blocks = out.get("P1") or []
    assert all(b.get("diff") is None for b in blocks)


def test_reuse_only_never_constructs_client(tmp_path, monkeypatch):
    """SENTINEL_DIFF_REUSE_CACHE_ONLY renders illustrative diffs from the cache
    only (UI re-render): the LLM client is never constructed, so no calls."""
    monkeypatch.setenv("SENTINEL_DIFF_REUSE_CACHE_ONLY", "1")
    import sentinel.api.client as apiclient
    calls = {"n": 0}

    def _spy(*a, **k):
        calls["n"] += 1
        raise RuntimeError("no gateway")

    monkeypatch.setattr(apiclient, "create_model_client", _spy)
    (tmp_path / "Svc.cs").write_text("// a\n// b\n// c\n", encoding="utf-8")
    finding = {"id": "F1", "file": "Svc.cs", "line_range": "1",
               "_pattern_id": "P1", "_group_changes": ["x"],
               "finding_kind": "logic_bug"}
    db._attach_pattern_diffs([finding], str(tmp_path), str(tmp_path))
    assert calls["n"] == 0   # reuse-only must not construct the client


def test_attach_pattern_diffs_regenerates_stale_wrongmethod_cache(tmp_path, monkeypatch):
    """A stale cached diff (from a wide window) that removes a line NOT at the
    finding's cited location must not be reused blindly — the tiered pass
    regenerates tight-first and grounds against the cited lines."""
    from sentinel.reports.diff_preview import save_cache
    _patch_client(monkeypatch)
    target = "var conn = Open();  // AT_CITED_55"
    lines = ["// header", "return ret;  // DECOY far away"]
    lines += [f"    // filler {i}" for i in range(3, 55)]
    lines += [target]                    # cited line 55
    lines += ["    // tail"]
    (tmp_path / "Svc.cs").write_text("\n".join(lines), encoding="utf-8")
    # Stale cache: the wrong-method DECOY line, under an old (mismatched) hash.
    stale = (
        "--- a/Svc.cs\n+++ b/Svc.cs\n@@ -2,1 +2,1 @@\n"
        "-return ret;  // DECOY far away\n+throw new Exception();"
    )
    save_cache(str(tmp_path), {"P1#0": {"input_hash": "OLD", "diff": stale}})
    monkeypatch.setattr(
        "sentinel.api.client.create_model_client",
        lambda *a, **k: _GroundedFakeClient(target),
    )
    out = db._attach_pattern_diffs([_svc_finding()], str(tmp_path), str(tmp_path))
    diff = (out.get("P1") or [{}])[0].get("diff") or ""
    assert "AT_CITED_55" in diff   # regenerated, grounded at the cited line
    assert "DECOY" not in diff     # stale wrong-method diff not reused


