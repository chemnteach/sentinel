"""S4-10 — one-page assessment handoff document (HTML).

:func:`generate_handoff` renders a single self-contained HTML page an operator
hands to a code owner alongside the Decision Brief. It reads whatever run
artifacts exist in the state directory and degrades gracefully when any are
absent (older runs, partial runs): a missing file simply drops its contribution
rather than raising.

Inputs read (all optional):
  * ``scan_metadata.json``    — commit_sha, model, provider
  * ``completion_report.json``— status (READY / NOT_READY / …)
  * ``findings.json``         — counts by severity + verified counts
  * ``bundles.json``          — bundle count, and per-severity bundle counts
  * ``domain_context.json``   — domain label

The page contains: a title, an assessment metadata line, a severity summary
table (Found / Verified / Bundles / Status per tier), a plain-English "What
this means" section, numbered process steps, a contact line, and a footer.
All user- and data-derived values are HTML-escaped.
"""

from __future__ import annotations

import html
import json
import logging
from datetime import date
from pathlib import Path

log = logging.getLogger(__name__)

# Severity tiers, in display order. Every tier carries a verification status;
# verification runs on all severities, not just the gating ones.
_SEV_ORDER = ("Critical", "P1", "P2", "P3")

# Map a verification-status label to a CSS class (labels contain spaces, so they
# cannot be used as class names directly).
_VERIF_CLASS = {"Fully verified": "verif-full", "Incomplete": "verif-incomplete"}


def _read_json(state_dir: Path, name: str):
    """Read+parse a state JSON file, returning None on any error/absence."""
    path = state_dir / name
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _severity_counts(findings) -> tuple[dict[str, int], dict[str, int]]:
    """Return (found_by_sev, verified_by_sev) for a findings list.

    Counts by EFFECTIVE severity — ``recalibrated_severity`` when Stage 5c
    verification revised the tier, else the original ``severity`` — so this
    table matches the Decision Brief (which buckets by effective severity too).
    ``verified`` is counted only for findings whose ``verified`` field is truthy
    (verification ran and passed). Absent field => not counted as verified.
    """
    found: dict[str, int] = {s: 0 for s in _SEV_ORDER}
    verified: dict[str, int] = {s: 0 for s in _SEV_ORDER}
    if not isinstance(findings, list):
        return found, verified
    for f in findings:
        if not isinstance(f, dict):
            continue
        sev = str(f.get("recalibrated_severity") or f.get("severity", "") or "")
        if sev not in found:
            continue
        found[sev] += 1
        if f.get("verified") is True:
            verified[sev] += 1
    return found, verified


def _bundle_counts(bundles) -> tuple[int, dict[str, int]]:
    """Return (total_bundles, bundles_by_root_severity).

    Per-severity counts key on each bundle's ``root_cause_severity`` when present
    (derivable); bundles without one contribute only to the total.
    """
    by_sev: dict[str, int] = {s: 0 for s in _SEV_ORDER}
    if not isinstance(bundles, list):
        return 0, by_sev
    for b in bundles:
        if not isinstance(b, dict):
            continue
        sev = str(b.get("root_cause_severity", "") or "")
        if sev in by_sev:
            by_sev[sev] += 1
    return len(bundles), by_sev


def _verification_status(
    sev: str, found: dict[str, int], verified: dict[str, int]
) -> str:
    """Human-readable verification status for a severity tier.

    Verification runs on ALL severities — P2 and P3 are checked just like
    Critical and P1. A tier is "Fully verified" when every finding found in it
    was verified against the source (verified >= found, found > 0), "Incomplete"
    otherwise, or "—" when nothing was found at that tier. This reflects only
    whether the findings were double-checked as real — NOT whether they are
    fixed.
    """
    if found.get(sev, 0) == 0:
        return "—"
    return (
        "Fully verified"
        if verified.get(sev, 0) >= found.get(sev, 0)
        else "Incomplete"
    )


_CSS = """
body{font:15px/1.55 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
  color:#1f2933;max-width:820px;margin:0 auto;padding:32px 28px 64px}
h1{font-size:24px;margin:0 0 4px}
h2{font-size:17px;margin:26px 0 10px;border-bottom:2px solid #d6dbe1;padding-bottom:5px}
.meta{color:#52606d;font-size:13.5px;margin:2px 0}
table{width:100%;border-collapse:collapse;font-size:13.5px;margin:8px 0;
  border:1px solid #d6dbe1;border-radius:6px;overflow:hidden}
th,td{padding:7px 10px;text-align:left;border-bottom:1px solid #e4e8ec}
th{background:#eceff3;font-weight:600}
.verif-full{color:#1e7e34;font-weight:600}
.verif-incomplete{color:#b00020;font-weight:600}
.notes{font-size:12.5px;color:#52606d;background:#f7f9fb;border:1px solid #e4e8ec;
  border-radius:6px;padding:10px 14px;margin:10px 0}
.notes dt{font-weight:600;color:#3e4c59;margin-top:6px}
.notes dd{margin:0 0 2px 0}
ol{margin:8px 0 8px 20px}li{margin:4px 0}
.contact{background:#eef2f6;border-left:4px solid #1a56db;padding:10px 14px;
  border-radius:4px;margin:14px 0;font-size:14px}
.footer{color:#8894a0;font-size:12px;margin-top:32px;border-top:1px solid #e4e8ec;
  padding-top:10px}
"""


def generate_handoff(state_dir: Path, config, graph_json=None) -> str:
    """Render the one-page assessment handoff HTML for a run state directory.

    ``config`` supplies ``operator`` and ``operator_email`` (both may be blank).
    When ``graph_json`` points at a graphify ``graph.json``, a "Structural
    Health" section (coupling / modularity / circular deps) is appended — the
    architecture complement to the defect findings; omitted if absent/unreadable.
    Returns a complete, self-contained HTML document string. Never raises on
    missing/corrupt state files — each just drops its contribution.
    """
    state_dir = Path(state_dir)
    e = html.escape

    scan = _read_json(state_dir, "scan_metadata.json") or {}
    completion = _read_json(state_dir, "completion_report.json") or {}
    findings = _read_json(state_dir, "findings.json")
    bundles = _read_json(state_dir, "bundles.json")
    domain_ctx = _read_json(state_dir, "domain_context.json") or {}

    repo_root = str(scan.get("repo_root_path", "") or "")
    repo_name = Path(repo_root).name if repo_root else state_dir.name
    commit = str(scan.get("commit_sha", "") or "")
    commit_short = commit[:12] if commit else "(unknown)"
    model = str(scan.get("model", "") or "(unknown)")
    provider = str(scan.get("provider", "") or "")
    domain = str(domain_ctx.get("domain", "") or "") if isinstance(domain_ctx, dict) else ""
    run_status = str(completion.get("status", "") or "") if isinstance(completion, dict) else ""

    operator = str(getattr(config, "operator", "") or "unknown")
    operator_email = str(getattr(config, "operator_email", "") or "")

    found, verified = _severity_counts(findings)
    total_bundles, bundles_by_sev = _bundle_counts(bundles)
    total_found = sum(found.values())

    today = date.today().isoformat()

    P: list[str] = [
        "<!doctype html><html><head><meta charset=\"utf-8\">",
        f"<title>Sentinel Code Assessment — {e(repo_name)}</title>",
        f"<style>{_CSS}</style></head><body>",
        f"<h1>Sentinel Code Assessment — {e(repo_name)}</h1>",
        f'<p class="meta"><b>Assessment date:</b> {e(today)}</p>',
        f'<p class="meta"><b>Commit:</b> <code>{e(commit_short)}</code>'
        f' &nbsp; <b>Model:</b> {e(model)}'
        + (f" ({e(provider)})" if provider else "")
        + f' &nbsp; <b>Operator:</b> {e(operator)}</p>',
    ]
    if domain:
        P.append(f'<p class="meta"><b>Domain:</b> {e(domain)}</p>')
    if run_status:
        P.append(f'<p class="meta"><b>Run status:</b> {e(run_status)}</p>')

    # -- Finding summary table --------------------------------------------
    P.append("<h2>Finding summary</h2>")
    P.append("<table><thead><tr>"
             "<th>Severity</th><th>Found</th><th>Verified</th>"
             "<th>Bundles</th><th>Verification</th></tr></thead><tbody>")
    for sev in _SEV_ORDER:
        status = _verification_status(sev, found, verified)
        cls = _VERIF_CLASS.get(status)
        status_cell = (
            f'<span class="{cls}">{e(status)}</span>' if cls else e(status)
        )
        P.append(
            f"<tr><td>{e(sev)}</td>"
            f"<td>{found[sev]}</td>"
            f"<td>{verified[sev]}</td>"
            f"<td>{bundles_by_sev[sev]}</td>"
            f"<td>{status_cell}</td></tr>"
        )
    P.append("</tbody></table>")
    P.append(
        f'<p class="meta">{total_found} finding(s) total across '
        f"{total_bundles} remediation bundle(s).</p>"
    )

    # -- Footnote: what each column/term means ----------------------------
    P.append(
        '<dl class="notes">'
        "<dt>Found</dt>"
        "<dd>Issues detected at this severity.</dd>"
        "<dt>Verified</dt>"
        "<dd>How many were re-checked against the actual source code and "
        "confirmed to be real. False positives are removed before this "
        "report — they are not counted or shown.</dd>"
        "<dt>Verification = “Fully verified”</dt>"
        "<dd>Every finding at this severity was double-checked — including P2 "
        "and P3, not just Critical and P1. It means <b>confirmed real</b>, "
        "<b>not fixed</b>: these issues still await your triage decision.</dd>"
        "<dt>Bundles</dt>"
        "<dd>Related findings grouped into a single fix decision. Bundles are "
        "created when you begin triage, so this shows 0 beforehand.</dd>"
        "<dt>Your decision (per group)</dt>"
        "<dd><b>Approve</b> — let Sentinel implement the fix. "
        "<b>Approve w/ conditions</b> — approve, but with constraints you specify "
        "on how it is fixed. <b>Defer</b> — not now, revisit later. "
        "<b>Exclude</b> — dismiss it (e.g. not a real concern for you).</dd>"
        "<dt>Severity</dt>"
        "<dd>Critical and P1 are highest priority; P2 and P3 are lower. Tiers "
        "shown are <b>post-verification</b>: Stage 5c may downgrade an "
        "over-stated finding (e.g. a flagged 'Critical' recalibrated to P1/P2 "
        "once checked against the real code), so a tier can read 0.</dd>"
        "</dl>"
    )

    # -- What this means --------------------------------------------------
    P.append("<h2>What this means</h2>")
    P.append(
        "<p>Sentinel reviewed this codebase and grouped the issues it found into "
        "related sets. Each set is one decision for you — <b>Approve</b> it for an "
        "automated fix, <b>Approve w/ conditions</b>, <b>Defer</b> it for later, "
        "or <b>Exclude</b> it. <b>Critical</b> and <b>P1</b> issues are the ones "
        "to act on first; <b>P2</b> and <b>P3</b> are lower priority. "
        "\"Verified\" means Sentinel double-checked the finding is real. You are "
        "not being asked to fix anything by hand — only to decide what gets "
        "fixed.</p>"
    )

    # -- Process steps ----------------------------------------------------
    # Render the operator's NAME as a mailto link when an email is set, so the
    # name itself is clickable (e.g. "Alex Owner" -> mailto:alex...@example.com).
    operator_link = (
        f'<a href="mailto:{e(operator_email)}">{e(operator)}</a>'
        if operator_email
        else e(operator)
    )
    P.append("<h2>What to do next</h2>")
    P.append("<ol>")
    P.append('<li>Open <b>brief.html</b> in your browser to review the findings.</li>')
    P.append(
        "<li>For each finding group, choose one of <b>Approve</b>, "
        "<b>Approve w/ conditions</b>, <b>Defer</b>, or <b>Exclude</b>.</li>"
    )
    P.append("<li>Click <b>Export decisions</b> to download <code>decisions.json</code>.</li>")
    P.append(
        f"<li>Send <code>decisions.json</code> to {operator_link}.</li>"
    )
    P.append("<li>Sentinel implements the approved fixes and opens pull requests (PRs).</li>")
    P.append("<li>Review and merge the PRs on GitHub.</li>")
    P.append("</ol>")

    # -- Structural Health (architecture lens) — only when a graph is given ---
    if graph_json is not None:
        try:
            from sentinel.reports.graph_health import (
                compute_health,
                render_health_html,
            )

            health = compute_health(graph_json)
            if health:
                P.append(render_health_html(health))
        except Exception as exc:  # noqa: BLE001 — never fail the handoff on this
            log.warning("handoff: structural-health section skipped: %s", exc)

    # -- Contact + footer -------------------------------------------------
    P.append(f'<p class="contact"><b>Contact:</b> {operator_link}</p>')
    P.append(
        f'<p class="footer">Generated by Sentinel | {e(today)} | {e(commit_short)}</p>'
    )
    P.append("</body></html>")
    return "".join(P)


__all__ = ["generate_handoff"]
