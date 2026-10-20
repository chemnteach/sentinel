"""Tests for the vendored Decision Brief renderer (sentinel/reports)."""
from __future__ import annotations

import json

from click.testing import CliRunner

from sentinel.assess.finding_kinds import FINDING_KIND_REGISTRY
from sentinel.cli import main
from sentinel.reports.decision_brief import (
    _build_syntheses,
    _synthesis_html,
    assessed_commit,
    render,
    synthesize_patterns,
)
from sentinel.reports.explainers import EXPLAINERS


class _FakeClient:
    """Stand-in for the LLM adapter: records prompts, returns a canned reply."""

    def __init__(self, reply="  These fixes add input validation.  ", raise_exc=False):
        self.reply = reply
        self.raise_exc = raise_exc
        self.prompts = []

    def complete(self, prompt):
        self.prompts.append(prompt)
        if self.raise_exc:
            raise RuntimeError("llm unavailable")
        return self.reply


def _patterns(n):
    return [{"pattern_id": f"P{i}", "title": f"t{i}", "pattern_kind": "k",
             "proposed_changes": [f"change {i}"]} for i in range(n)]


def _seed_state(tmp_path):
    """Write a minimal but valid run state dir; return its Path."""
    findings = [
        {"id": "F1", "module": "demo:core", "finding_kind": "logic_bug",
         "severity": "Critical", "file": "core/a.py"},
        {"id": "F2", "module": "demo:core", "finding_kind": "logic_bug",
         "severity": "P1", "file": "core/b.py"},
        {"id": "F3", "module": "demo:io", "finding_kind": "error_signal_loss",
         "severity": "P2", "file": "io/c.py"},
    ]
    groups = [
        {"group_id": "G1", "member_finding_ids": ["F1", "F2"]},
        {"group_id": "G2", "member_finding_ids": ["F3"]},
    ]
    patterns = [
        {"group_id": "G1", "pattern_id": "P1", "title": "Fix the logic",
         "pattern_kind": "logic-fix", "proposed_changes": ["Correct the boundary check"],
         "acceptance_criteria": ["The off-by-one no longer occurs"]},
        {"group_id": "G2", "pattern_id": "P2", "title": "Surface the error",
         "pattern_kind": "error-handling", "proposed_changes": ["Raise instead of swallow"],
         "acceptance_criteria": ["Failures propagate to the caller"]},
    ]
    tech_debt = [
        {"id": "TD-0001", "finding_id": "F1", "module": "demo:core",
         "severity": "Critical", "human_triage": None},
        {"id": "TD-0002", "finding_id": "F2", "module": "demo:core",
         "severity": "P1", "human_triage": None},
        {"id": "TD-0003", "finding_id": "F3", "module": "demo:io",
         "severity": "P2", "human_triage": None},
    ]
    (tmp_path / "findings.json").write_text(json.dumps(findings), encoding="utf-8")
    (tmp_path / "semantic_groups.json").write_text(json.dumps(groups), encoding="utf-8")
    (tmp_path / "remediation_patterns.json").write_text(json.dumps(patterns), encoding="utf-8")
    (tmp_path / "tech_debt.json").write_text(json.dumps(tech_debt), encoding="utf-8")
    return tmp_path


def test_read_symbol_source_finds_construct_far_from_cited_line(tmp_path):
    """cycle 65 — read_symbol_source locates a named C# construct even when the
    finding's cited line is wrong/absent (the Python def/class fallback misses
    C#). It searches for the identifier named in the change text."""
    from sentinel.reports.source_reader import read_symbol_source

    cs = tmp_path / "Svc.cs"
    body = ["// header"] * 200
    body[150] = "        public object GetAllObjectAttributes(string tool) {"
    body[151] = "            try { return Do(); } catch (Exception e) { return null; }"
    cs.write_text("\n".join(body), encoding="utf-8")

    # finding cites line 10 (wrong) and no function; the change names the method.
    finding = {"file": "Svc.cs", "line_range": "10-12", "location": "Svc.cs:10"}
    out = read_symbol_source(
        finding, str(tmp_path),
        hint_text="Change the GetAllObjectAttributes catch block to rethrow.",
    )
    assert out is not None
    assert "GetAllObjectAttributes" in out
    # unrelated file / no matching symbol -> None
    assert read_symbol_source(
        {"file": "Svc.cs", "location": "Svc.cs:10"}, str(tmp_path),
        hint_text="Configure the thing.",
    ) is None


def test_render_produces_html_file(tmp_path):
    state = _seed_state(tmp_path)
    out_html = tmp_path / "brief.html"
    out_md = tmp_path / "brief.md"

    render(state, out_html, out_md, "Test Run")

    assert out_html.exists()
    html = out_html.read_text(encoding="utf-8")
    assert "Remediation Decision Brief" in html
    assert "Approve" in html
    assert "Defer" in html
    assert "Exclude" in html
    assert "logic_bug" in html  # at least one finding_kind name


def test_render_embeds_commit_pinned_github_permalinks(tmp_path):
    """cycle 61 — with --github-url/--github-ref, findings get a commit-pinned
    'View on GitHub' permalink (for handing the brief to the code owner)."""
    state = _seed_state(tmp_path)
    out_html = tmp_path / "brief.html"
    out_md = tmp_path / "brief.md"
    base = "https://github.example.com/org/repo"
    sha = "deadbeef1234"

    render(state, out_html, out_md, "Test Run", github_url=base, github_ref=sha)

    html = out_html.read_text(encoding="utf-8")
    # Permalink pinned to the SHA, pointing at a finding's file, rendered in the row.
    assert f"{base}/blob/{sha}/core/a.py" in html
    assert "View on GitHub" in html


def test_github_permalink_path_prefix_for_subtree_scan(tmp_path):
    """When the scan was a repo SUBTREE, finding paths are subtree-relative;
    --github-path-prefix restores the repo-root-relative path so the permalink
    resolves instead of 404ing (Contoso: findings 'Config/..' live at
    'Contoso.Line.Tool/Config/..' in the repo)."""
    state = _seed_state(tmp_path)
    out_html = tmp_path / "brief.html"
    base = "https://github.example.com/org/repo"
    sha = "deadbeef1234"

    render(state, out_html, tmp_path / "brief.md", "Test Run",
           github_url=base, github_ref=sha, github_path_prefix="Contoso.Line.Tool")

    html = out_html.read_text(encoding="utf-8")
    assert f"{base}/blob/{sha}/Contoso.Line.Tool/core/a.py" in html
    # the un-prefixed (404) form must NOT appear
    assert f"{base}/blob/{sha}/core/a.py#" not in html


def test_assessed_commit_reads_scan_metadata(tmp_path):
    (tmp_path / "scan_metadata.json").write_text(
        json.dumps({"repo_root_path": "/x", "commit_sha": "cafef00d1234"}),
        encoding="utf-8",
    )
    assert assessed_commit(tmp_path) == "cafef00d1234"


def test_assessed_commit_absent_returns_empty(tmp_path):
    # No scan_metadata.json at all.
    assert assessed_commit(tmp_path) == ""
    # Present but without commit_sha (e.g. older run).
    (tmp_path / "scan_metadata.json").write_text(
        json.dumps({"repo_root_path": "/x"}), encoding="utf-8"
    )
    assert assessed_commit(tmp_path) == ""


def test_render_defaults_github_ref_from_scan_metadata(tmp_path):
    """cycle 61 — with --github-url but no --github-ref, the permalink pins to
    the assessed commit recorded in scan_metadata.json at Stage 1."""
    state = _seed_state(tmp_path)
    (state / "scan_metadata.json").write_text(
        json.dumps({"repo_root_path": "/x", "commit_sha": "abcdef123456"}),
        encoding="utf-8",
    )
    out_html = tmp_path / "brief.html"
    out_md = tmp_path / "brief.md"

    # Note: github_ref intentionally omitted — render() must auto-fill it.
    render(state, out_html, out_md, "Test Run",
           github_url="https://gh.example.com/org/repo")

    html = out_html.read_text(encoding="utf-8")
    assert "https://gh.example.com/org/repo/blob/abcdef123456/core/a.py" in html


def test_render_without_github_url_emits_no_permalink(tmp_path):
    """Backward compatible: no --github-url -> no GitHub permalink, no crash."""
    state = _seed_state(tmp_path)
    out_html = tmp_path / "brief.html"
    out_md = tmp_path / "brief.md"

    render(state, out_html, out_md, "Test Run")

    html = out_html.read_text(encoding="utf-8")
    assert "/blob/" not in html


def test_render_marks_already_triaged_rows(tmp_path):
    """cycle 62 sense 2 — a finding with a prior human_triage decision renders
    already-decided: dimmed row class, data-triaged, and pre-checked radio."""
    state = _seed_state(tmp_path)
    td = json.loads((state / "tech_debt.json").read_text(encoding="utf-8"))
    for t in td:
        if t["finding_id"] == "F1":
            t["human_triage"] = "EXCLUDE"
    (state / "tech_debt.json").write_text(json.dumps(td), encoding="utf-8")
    out_html = tmp_path / "brief.html"

    render(state, out_html, tmp_path / "brief.md", "Run")

    html = out_html.read_text(encoding="utf-8")
    assert 'data-triaged="EXCLUDE"' in html
    assert 'class="finding-row decided"' in html
    assert 'value="EXCLUDE" checked' in html


def test_render_untriaged_rows_not_decided(tmp_path):
    """No human_triage -> no row is pre-decided and no radio is pre-checked."""
    state = _seed_state(tmp_path)  # _seed_state writes human_triage: None
    out_html = tmp_path / "brief.html"

    render(state, out_html, tmp_path / "brief.md", "Run")

    html = out_html.read_text(encoding="utf-8")
    assert 'class="finding-row decided"' not in html
    assert '" checked' not in html  # no radio carries the pre-check attribute


def test_render_includes_save_state_and_toggle_hooks(tmp_path):
    """cycle 62 — the brief ships the save-state + hide-decided machinery."""
    state = _seed_state(tmp_path)
    out_html = tmp_path / "brief.html"

    render(state, out_html, tmp_path / "brief.md", "RunZ")

    html = out_html.read_text(encoding="utf-8")
    assert "sentinel-brief:RunZ" in html       # localStorage key from run label
    assert "_restoreState" in html and "_persistState" in html
    assert 'id="importFile"' in html and "importDecisions(this)" in html
    assert "Hide decided" in html and "toggle('hide-decided'" in html


def test_render_includes_durable_save_nudge(tmp_path):
    """cycle 62 option 1 — the on-drive Export is the durable save: a save-state
    indicator, a relabeled Export button, and a beforeunload unsaved-changes nudge."""
    state = _seed_state(tmp_path)
    out_html = tmp_path / "brief.html"

    render(state, out_html, tmp_path / "brief.md", "Run")

    html = out_html.read_text(encoding="utf-8")
    assert 'id="saveState"' in html
    assert "save to drive" in html               # Export relabeled as the durable save
    assert "addEventListener('beforeunload'" in html
    assert "_markDirty" in html and "_markSaved" in html and "_updateSaveState" in html


def test_render_includes_group_level_disposition(tmp_path):
    """cycle 63 — each group card carries a group-level disposition fast path
    (Approve/Defer/Exclude/Approve-w-conditions) + a rollup, driven by the full
    _fidToGroup map so it covers findings beyond the render cap."""
    state = _seed_state(tmp_path)
    out_html = tmp_path / "brief.html"

    render(state, out_html, tmp_path / "brief.md", "Run")

    html = out_html.read_text(encoding="utf-8")
    assert 'class="group-disposition"' in html
    assert "Disposition all" in html
    assert "setGroupDecision(" in html              # group fast-path wired
    assert 'class="group-rollup"' in html
    assert "_groupFids" in html and "_updateGroupRollup" in html  # full-group fan-out
    # Per-finding radios remain as overrides.
    assert "setFindingDecision(" in html


def test_render_export_offers_save_location(tmp_path):
    """cycle 64 — Export uses the File System Access API 'Save As' (so the owner
    picks the IP-drive folder), with a download fallback for other browsers."""
    state = _seed_state(tmp_path)
    out_html = tmp_path / "brief.html"

    render(state, out_html, tmp_path / "brief.md", "Run")

    html = out_html.read_text(encoding="utf-8")
    assert "showSaveFilePicker" in html              # native Save As when available
    assert "function _downloadBlob" in html          # graceful fallback
    assert "_saveExport(blob" in html                # export delegates to it
    assert "AbortError" in html                      # cancel leaves work unsaved/dirty


def test_render_conditions_text_untruncated_and_removable(tmp_path):
    """cycle 64 — conditions panel shows full proposed-change/criterion text
    (no 120-char cut), wraps it, and added criteria are individually removable."""
    state = _seed_state(tmp_path)
    out_html = tmp_path / "brief.html"

    render(state, out_html, tmp_path / "brief.md", "Run")

    html = out_html.read_text(encoding="utf-8")
    # No display truncation of change/criterion text.
    assert "String(c).substring(0,120)" not in html
    assert "String(q).substring(0,120)" not in html
    # Wrapping is in place.
    assert ".cp-crit-text" in html and "overflow-wrap:anywhere" in html
    # Added criteria are rendered with a per-item remove control.
    assert "_renderAddedCriteria" in html and "_removeAddedCriterion" in html
    assert "cp-remove-crit" in html


def test_render_group_conditions_button_toggles(tmp_path):
    """cycle 64 — the group 'Approve w/ conditions' button toggles its panel:
    click to open, click again to collapse (without losing the decision)."""
    state = _seed_state(tmp_path)
    out_html = tmp_path / "brief.html"

    render(state, out_html, tmp_path / "brief.md", "Run")

    html = out_html.read_text(encoding="utf-8")
    assert "toggleGroupConditions(" in html
    assert "function toggleGroupConditions" in html
    assert "panel.hidden=true; return;" in html  # open -> collapse path


def test_render_decisions_are_clearable(tmp_path):
    """cycle 64 (Finding 2) — dispositions can be cleared back to undecided:
    per-finding + group Clear, and a 'None' option on the condition radios."""
    state = _seed_state(tmp_path)
    out_html = tmp_path / "brief.html"

    render(state, out_html, tmp_path / "brief.md", "Run")

    html = out_html.read_text(encoding="utf-8")
    assert "function clearFindingDecision" in html and "clearFindingDecision(" in html
    assert "function clearGroupDecision" in html and "clearGroupDecision(" in html
    assert "ft-clear" in html and "gd-clear" in html
    # Condition change radios get a default 'None' that clears the choice.
    assert "['NONE','None']" in html and "if(action!=='NONE')" in html


def test_render_add_criterion_enter_and_warning(tmp_path):
    """cycle 64 (Finding 3) — Add-criterion commits on Enter, and Export warns
    about criteria typed but not added (so they aren't silently dropped)."""
    state = _seed_state(tmp_path)
    out_html = tmp_path / "brief.html"

    render(state, out_html, tmp_path / "brief.md", "Run")

    html = out_html.read_text(encoding="utf-8")
    # Enter-to-add is now wired via a delegated keydown listener (FIX-REQUIRED-060)
    # rather than an inline onkeydown handler.
    assert 'data-act="add-criterion-input"' in html
    assert "ev.key==='Enter'" in html              # Enter-to-add (delegated)
    assert "typed but not added" in html           # export-time uncommitted warning


def test_render_produces_markdown_file(tmp_path):
    state = _seed_state(tmp_path)
    out_html = tmp_path / "brief.html"
    out_md = tmp_path / "brief.md"

    render(state, out_html, out_md, "Test Run")

    assert out_md.exists()
    md = out_md.read_text(encoding="utf-8")
    assert md.startswith("# Remediation Decision Brief")


def test_explainers_cover_all_registered_kinds():
    """Every registered finding_kind must have a curated explainer.

    Locks the coverage contract: adding a new finding_kind to the registry
    without an explainer entry will fail this test.
    """
    missing = [e.kind_name for e in FINDING_KIND_REGISTRY if e.kind_name not in EXPLAINERS]
    assert not missing, f"finding_kinds without an explainer: {missing}"


def test_explainers_have_cluster_narrative():
    """Every registered finding_kind must have a non-empty cluster narrative.

    Locks the cycle-47 contract: the Decision Brief renders a 'why these findings
    cluster' line per row, and a missing/blank cluster would render nothing.
    """
    missing = [
        e.kind_name for e in FINDING_KIND_REGISTRY
        if not str(EXPLAINERS.get(e.kind_name, {}).get("cluster", "")).strip()
    ]
    assert not missing, f"finding_kinds without a cluster narrative: {missing}"


def test_render_includes_cluster_narrative(tmp_path):
    state = _seed_state(tmp_path)
    out_html = tmp_path / "brief.html"
    out_md = tmp_path / "brief.md"

    render(state, out_html, out_md, "Test Run")

    html = out_html.read_text(encoding="utf-8")
    assert "cluster-narrative" in html
    assert "Why these findings cluster:" in html
    # the logic_bug row's curated cluster text must appear
    assert "A bug in shared logic tends to manifest" in html


def test_synthesize_patterns_happy_path():
    client = _FakeClient()
    out = synthesize_patterns("injection_risk", "demo:io", _patterns(5), client)
    assert out == "These fixes add input validation."  # stripped
    assert len(client.prompts) == 1
    # prompt synthesizes, not enumerates
    assert "injection_risk" in client.prompts[0]
    assert "demo:io" in client.prompts[0]
    assert "synthesize the pattern" in client.prompts[0]


def test_synthesize_patterns_no_patterns_or_client():
    assert synthesize_patterns("logic_bug", "m", [], _FakeClient()) == ""
    assert synthesize_patterns("logic_bug", "m", _patterns(3), None) == ""


def test_synthesize_patterns_swallows_client_error():
    client = _FakeClient(raise_exc=True)
    assert synthesize_patterns("logic_bug", "m", _patterns(5), client) == ""


def test_synthesize_patterns_skips_patterns_without_changes():
    client = _FakeClient()
    pats = [{"pattern_id": "P0", "proposed_changes": []}]
    assert synthesize_patterns("logic_bug", "m", pats, client) == ""
    assert client.prompts == []  # no call made when nothing to summarize


def test_build_syntheses_only_for_rows_over_threshold():
    ordered = [
        (("demo:a", "logic_bug"), {"pats": {p["pattern_id"]: p for p in _patterns(4)}}),
        (("demo:b", "error_signal_loss"), {"pats": {p["pattern_id"]: p for p in _patterns(3)}}),
    ]
    client = _FakeClient(reply="summary")
    syn = _build_syntheses(ordered, client=client)
    assert syn == {"logic_bug|demo:a": "summary"}  # the 3-pattern row is skipped
    assert len(client.prompts) == 1


def test_synthesis_html_renders_and_escapes():
    syn = {"injection_risk|demo:io": "Validate <input> early."}
    out = _synthesis_html("injection_risk", "demo:io", syn)
    assert "pattern-synthesis" in out
    assert "Proposed fix pattern:" in out
    assert "&lt;input&gt;" in out  # HTML-escaped
    # absent / empty cases render nothing
    assert _synthesis_html("injection_risk", "demo:io", None) == ""
    assert _synthesis_html("missing", "row", syn) == ""


def test_render_with_injected_synthesis(tmp_path, monkeypatch):
    """render(use_synthesis=True) wires synthesis into the HTML without hitting
    LLM, by stubbing the client-construction path. A row earns synthesis only
    when it spans more than 3 patterns; since _build_rollups keys one pattern per
    group, that means 4 findings of the same (module, kind) across 4 groups."""
    import sentinel.reports.decision_brief as db

    findings = [
        {"id": f"F{i}", "module": "demo:core", "finding_kind": "logic_bug",
         "severity": "P1", "file": f"core/{i}.py"} for i in range(4)
    ]
    groups = [{"group_id": f"G{i}", "member_finding_ids": [f"F{i}"]} for i in range(4)]
    patterns = [
        {"group_id": f"G{i}", "pattern_id": f"P{i}", "title": f"t{i}",
         "pattern_kind": "logic-fix", "proposed_changes": [f"change {i}"],
         "acceptance_criteria": ["ok"]} for i in range(4)
    ]
    (tmp_path / "findings.json").write_text(json.dumps(findings), encoding="utf-8")
    (tmp_path / "semantic_groups.json").write_text(json.dumps(groups), encoding="utf-8")
    (tmp_path / "remediation_patterns.json").write_text(json.dumps(patterns), encoding="utf-8")

    real_build = db._build_syntheses

    def _fake_build(ordered, client=None):
        return real_build(ordered, client=_FakeClient(reply="Synthesized fix class."))

    monkeypatch.setattr(db, "_build_syntheses", _fake_build)

    out_html = tmp_path / "brief.html"
    render(tmp_path, out_html, tmp_path / "brief.md", "Test", use_synthesis=True)
    html = out_html.read_text(encoding="utf-8")
    assert "pattern-synthesis" in html
    assert "Synthesized fix class." in html


def test_report_cli_entry_point(tmp_path):
    state = _seed_state(tmp_path)
    out_html = tmp_path / "out.html"
    out_md = tmp_path / "out.md"

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["report", "--state-dir", str(state),
         "--output-html", str(out_html), "--output-md", str(out_md),
         "--run-label", "CLI Test"],
    )

    assert result.exit_code == 0, result.output
    assert out_html.exists()
    assert out_md.exists()
