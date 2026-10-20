"""Tests for S4-10 — the one-page assessment handoff document + operator email.

Covers:
  * sentinel.reports.handoff.generate_handoff — HTML rendering from run state
    (all inputs optional/graceful).
  * config.operator_email — new SENTINEL_OPERATOR_EMAIL setting.
  * decision_brief.emit_html — handoff.html link injected into the brief header.
  * server.py — GET /handoff.html route (200 present, 404 absent).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sentinel.config import Config
from sentinel.reports.handoff import generate_handoff


# --------------------------------------------------------------------------- helpers


def _config(operator="Casey Owner", email="casey@example.com"):
    return Config(
        env={
            "SENTINEL_OPERATOR": operator,
            "SENTINEL_OPERATOR_EMAIL": email,
        },
        load_dotenv_file=False,
    )


def _write_state(
    state_dir: Path,
    *,
    findings=None,
    bundles=None,
    scan_metadata=None,
    completion=None,
    domain=None,
):
    if findings is not None:
        (state_dir / "findings.json").write_text(
            json.dumps(findings), encoding="utf-8"
        )
    if bundles is not None:
        (state_dir / "bundles.json").write_text(json.dumps(bundles), encoding="utf-8")
    if scan_metadata is not None:
        (state_dir / "scan_metadata.json").write_text(
            json.dumps(scan_metadata), encoding="utf-8"
        )
    if completion is not None:
        (state_dir / "completion_report.json").write_text(
            json.dumps(completion), encoding="utf-8"
        )
    if domain is not None:
        (state_dir / "domain_context.json").write_text(
            json.dumps(domain), encoding="utf-8"
        )


_FINDINGS = [
    {"id": "F1", "severity": "Critical", "verified": True},
    {"id": "F2", "severity": "Critical", "verified": False},
    {"id": "F3", "severity": "P1", "verified": True},
    {"id": "F4", "severity": "P1"},
    {"id": "F5", "severity": "P2"},
    {"id": "F6", "severity": "P3"},
]

_BUNDLES = [
    {"bundle_id": "B-001", "root_cause_severity": "Critical"},
    {"bundle_id": "B-002", "root_cause_severity": "P1"},
    {"bundle_id": "B-003", "root_cause_severity": "P1"},
]

_SCAN = {
    "commit_sha": "a736a7f907a7856216c7fc2390d3e3395ccf4533",
    "model": "gpt-5.4",
    "provider": "openai",
}


# --------------------------------------------------------------------------- generate_handoff


def test_generate_handoff_returns_nonempty_html(tmp_path):
    _write_state(tmp_path, findings=_FINDINGS, scan_metadata=_SCAN)
    html = generate_handoff(tmp_path, _config())
    assert isinstance(html, str)
    assert html.strip()
    assert "<html" in html.lower()
    assert "<table" in html.lower()


def test_handoff_contains_operator_name(tmp_path):
    _write_state(tmp_path, findings=_FINDINGS, scan_metadata=_SCAN)
    html = generate_handoff(tmp_path, _config(operator="Dana Reviewer"))
    assert "Dana Reviewer" in html


def test_handoff_contains_operator_email(tmp_path):
    _write_state(tmp_path, findings=_FINDINGS, scan_metadata=_SCAN)
    html = generate_handoff(tmp_path, _config(email="dana@example.com"))
    assert "dana@example.com" in html


def test_handoff_operator_name_is_mailto_hyperlink(tmp_path):
    # The operator NAME is the clickable link text (not the raw email), e.g.
    # <a href="mailto:alex.owner@example.com">Alex Owner</a>.
    _write_state(tmp_path, findings=_FINDINGS, scan_metadata=_SCAN)
    html = generate_handoff(
        tmp_path, _config(operator="Alex Owner", email="alex.owner@example.com")
    )
    assert '<a href="mailto:alex.owner@example.com">Alex Owner</a>' in html
    # Appears in both the send step and the contact line.
    assert html.count('<a href="mailto:alex.owner@example.com">Alex Owner</a>') == 2


def test_handoff_operator_plain_text_when_no_email(tmp_path):
    # With no email, the name renders as plain text (no dangling mailto).
    _write_state(tmp_path, findings=_FINDINGS, scan_metadata=_SCAN)
    html = generate_handoff(tmp_path, _config(operator="Casey Owner", email=""))
    assert "Casey Owner" in html
    assert "mailto:" not in html


def test_handoff_contains_finding_counts_by_severity(tmp_path):
    _write_state(tmp_path, findings=_FINDINGS, scan_metadata=_SCAN)
    html = generate_handoff(tmp_path, _config())
    # Severity tiers named in the summary table.
    assert "Critical" in html
    assert "P1" in html
    assert "P2" in html
    assert "P3" in html
    # 2 Critical, 2 P1, 1 P2, 1 P3 findings — the found counts must surface.
    assert ">2<" in html  # Critical / P1 found counts


def test_handoff_contains_verified_counts(tmp_path):
    _write_state(tmp_path, findings=_FINDINGS, scan_metadata=_SCAN)
    html = generate_handoff(tmp_path, _config())
    # 1 verified Critical (F1), 1 verified P1 (F3).
    assert "Verified" in html


def test_handoff_counts_by_effective_severity(tmp_path):
    """The severity table must bucket by recalibrated_severity when present (so it
    matches the Decision Brief), not the original severity."""
    import re
    findings = [
        # Originally Critical, but Stage 5c recalibrated to P1 -> counts as P1.
        {"id": "F1", "severity": "Critical", "recalibrated_severity": "P1",
         "verified": True},
        # Originally Critical, recalibrated to P2 -> counts as P2.
        {"id": "F2", "severity": "Critical", "recalibrated_severity": "P2",
         "verified": True},
        # No recalibration -> original P1.
        {"id": "F3", "severity": "P1", "verified": True},
    ]
    _write_state(tmp_path, findings=findings, scan_metadata=_SCAN)
    html = generate_handoff(tmp_path, _config())

    def _row(sev):
        m = re.search(rf"<td>{sev}</td><td>(\d+)</td><td>(\d+)</td>", html)
        return (int(m.group(1)), int(m.group(2))) if m else (None, None)

    assert _row("Critical") == (0, 0)   # both Criticals recalibrated away
    assert _row("P1") == (2, 2)         # F1 (recal) + F3 (original)
    assert _row("P2") == (1, 1)         # F2 (recal)


def test_handoff_verification_column_and_footnote(tmp_path):
    # All findings verified across every tier => every tier "Fully verified"
    # (including P2/P3), and the explanatory footnote is present.
    findings = [
        {"id": "C1", "severity": "Critical", "verified": True},
        {"id": "P1a", "severity": "P1", "verified": True},
        {"id": "P2a", "severity": "P2", "verified": True},
        {"id": "P3a", "severity": "P3", "verified": True},
    ]
    _write_state(tmp_path, findings=findings, scan_metadata=_SCAN)
    html = generate_handoff(tmp_path, _config())
    # Column renamed away from the ambiguous "Status".
    assert "<th>Verification</th>" in html
    assert ">Status<" not in html
    # New, clearer labels — no legacy COMPLETE/OPEN.
    assert "Fully verified" in html
    assert "COMPLETE" not in html and ">OPEN<" not in html
    # P2/P3 are shown as verified, not "-". Count the cell spans (not the CSS
    # rule) — one per tier, all four fully verified.
    assert html.count('class="verif-full"') == 4
    # Footnote explains the terms, including "not fixed".
    assert "not fixed" in html.lower()
    assert 'class="notes"' in html


def test_handoff_verification_incomplete_when_unverified(tmp_path):
    # A tier with an unverified finding reads "Incomplete", not "Fully verified".
    findings = [
        {"id": "C1", "severity": "Critical", "verified": True},
        {"id": "C2", "severity": "Critical"},  # not verified
    ]
    _write_state(tmp_path, findings=findings, scan_metadata=_SCAN)
    html = generate_handoff(tmp_path, _config())
    assert "Incomplete" in html
    assert "verif-incomplete" in html


def test_handoff_contains_bundle_count(tmp_path):
    _write_state(
        tmp_path, findings=_FINDINGS, bundles=_BUNDLES, scan_metadata=_SCAN
    )
    html = generate_handoff(tmp_path, _config())
    # 3 bundles total; the count must appear somewhere.
    assert ">3<" in html or "3 bundle" in html.lower() or "Bundles" in html


def test_handoff_contains_numbered_process_steps(tmp_path):
    _write_state(tmp_path, findings=_FINDINGS, scan_metadata=_SCAN)
    html = generate_handoff(tmp_path, _config())
    low = html.lower()
    assert "brief.html" in low
    assert "decisions" in low
    assert "pr" in low  # opens PRs / review on GitHub
    assert "<ol" in low or "step" in low


def test_handoff_lists_all_four_decision_options(tmp_path):
    # The handoff must match the brief's actual triage options — not the old
    # two-way "APPROVE or DEFER" framing.
    _write_state(tmp_path, findings=_FINDINGS, scan_metadata=_SCAN)
    html = generate_handoff(tmp_path, _config())
    low = html.lower()
    assert "approve w/ conditions" in low
    assert "exclude" in low
    assert "defer" in low
    assert "approve" in low
    # The old two-option instruction must be gone.
    assert "approve</b> or <b>defer" not in low


def test_handoff_contains_commit_and_model(tmp_path):
    _write_state(tmp_path, findings=_FINDINGS, scan_metadata=_SCAN)
    html = generate_handoff(tmp_path, _config())
    assert "a736a7f9" in html
    assert "gpt-5.4" in html


def test_handoff_graceful_with_empty_state_dir(tmp_path):
    # No state files at all — must still return valid HTML, not raise.
    html = generate_handoff(tmp_path, _config())
    assert "<html" in html.lower()
    assert "<table" in html.lower()


def test_handoff_structural_health_section_when_graph_given(tmp_path):
    # With a graph.json, the handoff gains a Structural Health section; without,
    # it is absent.
    _write_state(tmp_path, findings=_FINDINGS, scan_metadata=_SCAN)
    graph = tmp_path / "graph.json"
    graph.write_text(json.dumps({
        "nodes": [
            {"id": "a", "label": "A.cs", "source_file": "src/A.cs", "community": 0,
             "community_name": "Alpha"},
            {"id": "b", "label": "B.cs", "source_file": "src/B.cs", "community": 0,
             "community_name": "Alpha"},
        ],
        "links": [{"source": "a", "target": "b", "relation": "calls"}],
    }), encoding="utf-8")

    without = generate_handoff(tmp_path, _config())
    assert "Structural Health" not in without

    with_graph = generate_handoff(tmp_path, _config(), graph_json=str(graph))
    assert "Structural Health" in with_graph
    assert "Coupling hotspots" in with_graph


def test_handoff_ignores_bad_graph_path(tmp_path):
    # A non-existent graph path must not break the handoff (section omitted).
    _write_state(tmp_path, findings=_FINDINGS, scan_metadata=_SCAN)
    html = generate_handoff(tmp_path, _config(), graph_json=str(tmp_path / "nope.json"))
    assert "<html" in html.lower()
    assert "Structural Health" not in html


def test_handoff_escapes_values(tmp_path):
    _write_state(
        tmp_path,
        findings=_FINDINGS,
        scan_metadata=_SCAN,
        domain={"domain": "<script>alert(1)</script>"},
    )
    html = generate_handoff(tmp_path, _config(operator="<b>x</b>"))
    assert "<script>alert(1)</script>" not in html
    assert "<b>x</b>" not in html


# --------------------------------------------------------------------------- config


def test_config_operator_email_default_empty():
    cfg = Config(env={}, load_dotenv_file=False)
    assert cfg.operator_email == ""


def test_config_operator_email_from_env():
    cfg = Config(
        env={"SENTINEL_OPERATOR_EMAIL": "owner@example.com"}, load_dotenv_file=False
    )
    assert cfg.operator_email == "owner@example.com"


# --------------------------------------------------------------------------- brief header link


def test_brief_header_contains_handoff_link(tmp_path):
    from sentinel.reports.decision_brief import emit_html

    out = tmp_path / "brief.html"
    emit_html(
        ordered=[],
        total=0,
        sev_tot={},
        out_html=out,
        run_label="run-test",
    )
    html = out.read_text(encoding="utf-8")
    assert "handoff.html" in html
    assert "handoff-link" in html


def test_render_vscode_links_toggle(tmp_path):
    """vscode_links=False must suppress the vscode:// deep-links (for a portable
    brief), while the inline source + GitHub links are unaffected."""
    import json as _json
    from sentinel.reports.decision_brief import render

    state = tmp_path / "state"
    (state / "sentinel").mkdir(parents=True)  # repo_root subtree
    (state / "sentinel" / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    findings = [{
        "id": "F1", "file": "mod.py", "finding_kind": "logic_bug",
        "severity": "Critical", "description": "bug", "line_range": "1",
        "module": "r:pkg:mod.py",
    }]
    (state / "findings.json").write_text(_json.dumps(findings), encoding="utf-8")
    (state / "semantic_groups.json").write_text("[]", encoding="utf-8")
    (state / "remediation_patterns.json").write_text("[]", encoding="utf-8")

    # Default (links on) → vscode:// present.
    render(state, output_html=state / "on.html", repo_root=str(state))
    assert "vscode://" in (state / "on.html").read_text(encoding="utf-8")

    # Suppressed → no vscode:// anywhere, AND the "Open in VS Code" panel link
    # is removed entirely (not just emptied) so no dead href="#" link shows.
    render(state, output_html=state / "off.html", repo_root=str(state),
           vscode_links=False)
    off = (state / "off.html").read_text(encoding="utf-8")
    assert "vscode://" not in off
    # The panel's "Open in VS Code" anchor element is removed entirely, so no
    # dead href="#" link renders. (The string may linger in inert JS guarded by
    # a now-null getElementById; what matters is the element is gone.)
    assert 'id="cp-vscode"' not in off
    # The GitHub panel link is untouched (still present in the panel).
    assert 'id="cp-github"' in off


# --------------------------------------------------------------------------- server route


@pytest.fixture
def state_dir(tmp_path):
    d = tmp_path / "run-handoff"
    d.mkdir()
    return d


@pytest.fixture
def client(state_dir):
    from sentinel.reports.server import create_app

    app = create_app(str(state_dir))
    app.config.update(TESTING=True)
    return app.test_client()


def test_handoff_route_200_when_present(client, state_dir):
    (state_dir / "handoff.html").write_text(
        "<html><body>handoff</body></html>", encoding="utf-8"
    )
    resp = client.get("/handoff.html")
    assert resp.status_code == 200
    assert b"handoff" in resp.data


def test_handoff_route_404_when_absent(client):
    resp = client.get("/handoff.html")
    assert resp.status_code == 404
