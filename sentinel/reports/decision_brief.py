"""Render a Sentinel Remediation Decision Brief (HTML + Markdown) from a run.

Joins findings -> semantic group -> Stage-5b remediation pattern, rolls up by
(module, finding_kind), and renders a triage brief in Markdown AND styled HTML.
The HTML is self-contained: each triage row carries interactive controls
(Approve / Approve with conditions / Defer / Exclude) and an Export Decisions
button that downloads a JSON file consumable by ``sentinel triage --batch``.
It also prints cleanly to PDF from any browser (Print -> Save as PDF).

Public API: :func:`render`. Reached from the CLI via ``sentinel report``; also
runnable standalone (see ``__main__``). All paths come from arguments.
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
from collections import Counter, defaultdict
from pathlib import Path

import click

from sentinel.reports.explainers import EXPLAINERS

log = logging.getLogger(__name__)
from sentinel.reports.source_reader import (
    github_link as _github_link,
    read_finding_source,
    read_symbol_source,
    resolve_start_line as _resolve_start_line,
    vscode_link as _vscode_link,
)


def explainer_for(kind: str) -> dict | None:
    """Curated plain-language explainer for a finding_kind, or None."""
    return EXPLAINERS.get(kind)


def load_consequences(domain_file: str | None = None) -> dict:
    """Load the per-finding_kind consequence spectrums (cycle 49A).

    Reads ``consequences.yaml`` (sibling of this module) unless ``domain_file``
    overrides the path. Returns {} on any error so the brief degrades gracefully
    to its no-consequence form rather than failing to render.
    """
    path = (
        Path(domain_file)
        if domain_file
        else Path(__file__).parent / "consequences.yaml"
    )
    if not path.exists():
        return {}
    try:
        import yaml
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


SEV_ORDER = ["Critical", "P1", "P2", "P3"]
SEV_CLASS = {"Critical": "crit", "P1": "p1", "P2": "p2", "P3": "p3"}


def _effective_sev(f: dict) -> str:
    """The severity that drives prioritization/bucketing for a finding (S4-8).

    Stage 5c verification may store ``recalibrated_severity`` when the verified
    severity differs from the originally-assigned ``severity``. When present,
    the recalibrated value is authoritative; otherwise the original applies.
    """
    return f.get("recalibrated_severity") or f.get("severity", "") or "?"


# ---------------------------------------------------------------- Alarm focus
# Stage 5c-alarm stamps each finding with ``alarm_relevance`` (direct/adjacent/
# none). A rollup row's tier is its most alarm-relevant member: direct beats
# adjacent beats none. The brief surfaces alarm rows first WITHOUT dropping or
# duplicating anything — the full triage table below is unchanged.

def _row_alarm_counts(r: dict) -> Counter:
    """Count member findings by alarm relevance (missing field ⇒ ``none``)."""
    return Counter(_finding_alarm_tier(f) for f in r.get("findings", []))


# The dedicated alarm/WIP-stall specialist (layer2k). Its findings are the
# primary target of the alarm focus even when the keyword-based Stage 5c-alarm
# classifier left them ``none`` (a timeout/wait/abort rarely says "alarm").
_ALARM_SPECIALIST_SKILL = "layer2k-alarm-integrity-specialist"


def _finding_alarm_tier(f: dict) -> str:
    """The alarm/WIP-stall tier of a single finding."""
    relevance = str(f.get("alarm_relevance", "none") or "none")
    if relevance == "direct":
        return "direct"
    if str(f.get("skill", "") or "") == _ALARM_SPECIALIST_SKILL:
        return "direct"
    if relevance == "adjacent":
        return "adjacent"
    return "none"


def _row_alarm_tier(r: dict) -> str:
    """The row's alarm tier: ``direct`` > ``adjacent`` > ``none``."""
    counts = _row_alarm_counts(r)
    if counts.get("direct"):
        return "direct"
    if counts.get("adjacent"):
        return "adjacent"
    return "none"


def alarm_focus(ordered):
    """Alarm-relevant rows as ``(row_number, key, r, tier)``, direct first.

    ``row_number`` is the 1-based position in the full triage table so the
    focus list references (does not copy) the detail rows below. Rows whose
    tier is ``none`` are omitted.
    """
    focus = [
        (i, key, r, _row_alarm_tier(r))
        for i, (key, r) in enumerate(ordered, 1)
    ]
    focus = [t for t in focus if t[3] != "none"]
    focus.sort(key=lambda t: (0 if t[3] == "direct" else 1, t[0]))
    return focus


def _alarm_totals(ordered) -> Counter:
    """Finding-level direct/adjacent/none totals across every row."""
    totals: Counter = Counter()
    for _key, r in ordered:
        totals.update(_row_alarm_counts(r))
    return totals


EXEC_P1_LIMIT = 10

# Short 6-10 word labels for the executive top-N table ONLY (the detailed rows
# below still render the full curated explainer text). Avoids repeating a long
# sentence when the same finding_kind appears in several top-priority rows.
SHORT_LABEL = {
    "logic_bug": "Code that behaves incorrectly",
    "change_verification_gap": "Missing tests for this change surface",
    "structural_complexity": "Code that is hard to understand or change",
    "error_signal_loss": "Errors swallowed or not surfaced",
    "hidden_dependency": "Implicit coupling between components",
    "contract_violation": "API or interface contract broken",
    "timeout_handling_gap": "Missing or inadequate timeout handling",
    "divergent_change": "One module changed for unrelated reasons",
    "observability_gap": "Insufficient logging or monitoring",
    "concurrency_update_risk": "Race condition on shared state",
    "silent_data_loss": "Data discarded without warning",
    "security_misconfiguration": "Insecure security configuration",
    "authorization_gap": "Missing authorization check",
    "sensitive_data_exposure": "Sensitive data exposed",
    "partial_failure_handling_gap": "Partial failures not handled",
}

def _preamble(repo_name: str) -> str:
    subject = repo_name or "the assessed codebase"
    return (
        f"This brief summarizes the results of an automated code review of {subject}. "
        + _PREAMBLE_BODY
    )


_PREAMBLE_BODY = (
    "Each row represents a category of findings (module × issue type) "
    "requiring a triage decision: Approve for remediation, Defer, or Exclude as "
    "false positive. Start with the Critical and P1 rows."
)


def _exec_desc(kind: str) -> str:
    """Short label for the executive top-N table. Prefers the curated SHORT_LABEL;
    falls back to the first ~60 chars (word boundary) of the full explainer text."""
    if kind in SHORT_LABEL:
        return SHORT_LABEL[kind]
    e = explainer_for(kind)
    what = " ".join(str((e or {}).get("what", "")).split()) if e else ""
    if len(what) <= 60:
        return what
    return what[:60].rsplit(" ", 1)[0] + " …"


def _exec_selection(ordered, p1_limit: int = EXEC_P1_LIMIT):
    """Executive top-N: all rows containing a Critical, plus the top P1 rows by
    finding count. Returns (crit, p1) as lists of (orig_index, (mod, kind), r),
    preserving the full-table row numbers for cross-reference."""
    indexed = [(i, key, r) for i, (key, r) in enumerate(ordered, 1)]
    crit = [t for t in indexed if t[2]["sev"].get("Critical", 0) > 0]
    crit_idx = {t[0] for t in crit}
    p1 = [t for t in indexed if t[0] not in crit_idx and t[2]["sev"].get("P1", 0) > 0]
    p1.sort(key=lambda t: -len(t[2]["findings"]))
    return crit, p1[:p1_limit]


def _module(f: dict) -> str:
    m = str(f.get("module", "") or "").split(",")[0].strip()
    if m:
        return m
    fp = str(f.get("file", "") or "")
    return fp.rsplit("/", 1)[0] if "/" in fp else (fp or "(unknown)")


def _sev_summary(counter: Counter) -> str:
    return " · ".join(f"{counter[s]} {s}" for s in SEV_ORDER if counter.get(s))


def _td_num(td_id: str) -> int:
    """Numeric sort key for a TD-xxxx id; 0 if unparseable."""
    try:
        return int(str(td_id).split("-", 1)[1])
    except (IndexError, ValueError):
        return 0


def assessed_commit(state_dir) -> str:
    """Return the assessed commit SHA recorded at Stage 1 (cycle 61).

    Reads ``commit_sha`` from ``scan_metadata.json`` in the run's state dir,
    so ``sentinel report`` can pin GitHub permalinks to the commit the run was
    assessed against without the operator passing ``--github-ref``. Returns ""
    when absent (older runs, or the target was not a git repo).
    """
    p = Path(state_dir) / "scan_metadata.json"
    if not p.exists():
        return ""
    try:
        meta = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""
    return str(meta.get("commit_sha", "") or "")


def assessed_repo_name(state_dir) -> str:
    """Folder name of the assessed repo from ``scan_metadata.json``, or "" if absent."""
    p = Path(state_dir) / "scan_metadata.json"
    if not p.exists():
        return ""
    try:
        meta = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""
    root = str(meta.get("repo_root_path", "") or "").rstrip("/\\")
    return Path(root).name if root else ""


def _build_rollups(state_dir: Path):
    findings = json.loads((state_dir / "findings.json").read_text(encoding="utf-8"))
    groups = json.loads((state_dir / "semantic_groups.json").read_text(encoding="utf-8"))
    patterns = json.loads((state_dir / "remediation_patterns.json").read_text(encoding="utf-8"))
    fid2gid = {fid: g["group_id"] for g in groups for fid in g.get("member_finding_ids", [])}
    gid2pat = {p["group_id"]: p for p in patterns}

    rollups: dict[tuple, dict] = defaultdict(
        lambda: {"findings": [], "sev": Counter(), "files": set(), "pats": {}, "tds": []})
    for f in findings:
        key = (_module(f), str(f.get("finding_kind", "") or "(none)"))
        r = rollups[key]
        r["findings"].append(f)
        r["sev"][_effective_sev(f)] += 1
        if f.get("file"):
            r["files"].add(f["file"])
        pat = gid2pat.get(fid2gid.get(f.get("id")))
        if pat:
            r["pats"][pat["pattern_id"]] = pat
        # Stash this finding's group-level proposed changes for the per-finding
        # "View changes" panel (cycle 48). proposed_changes is per-pattern (per
        # group); findings sharing a group share the same changes.
        f["_group_changes"] = (pat or {}).get("proposed_changes") or []
        # Pattern lineage id — lets the "View changes" panel look up this
        # finding's illustrative pattern diff (cycle 65, A′ triage-time path).
        f["_pattern_id"] = (pat or {}).get("pattern_id") or ""

    # Attach member tech-debt IDs to each rollup row by joining tech_debt.json
    # through finding_id to the finding's (module, finding_kind) — the same keys
    # the rollup is built from. Tolerant: older runs without tech_debt.json just
    # render with empty TD lists (export will be inert).
    td_path = state_dir / "tech_debt.json"
    if td_path.exists():
        fid2f = {f.get("id"): f for f in findings}
        # Guard the load so a missing/corrupt tech_debt.json surfaces a clear
        # operator error rather than an unhandled traceback (F2-0b722c77c508).
        try:
            _td_items = json.loads(td_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise click.ClickException(f"Failed to load tech_debt.json: {e}")
        for t in _td_items:
            f = fid2f.get(t.get("finding_id"))
            if not f or not t.get("id"):
                continue
            key = (_module(f), str(f.get("finding_kind", "") or "(none)"))
            if key in rollups:
                rollups[key]["tds"].append(t["id"])
        for r in rollups.values():
            r["tds"].sort(key=_td_num)

    def sort_key(item):
        _k, r = item
        s = r["sev"]
        return (-(s.get("Critical", 0) > 0), -s.get("Critical", 0), -s.get("P1", 0), -len(r["findings"]))

    ordered = sorted(rollups.items(), key=sort_key)
    ordered = _apply_pattern_groups(ordered)
    sev_tot = Counter(_effective_sev(f) for f in findings)
    return ordered, len(findings), sev_tot


# ---------------------------------------------------------- Pattern-group layer
# A remediation pattern that recurs across many modules is one decision, not
# N. When the SAME pattern_id spans this many distinct modules, its (module,
# finding_kind) rollup rows collapse into a single collapsible "pattern group"
# row the reviewer can approve/defer at once (S3-4).
PATTERN_GROUP_MIN_MODULES = 3


def _row_pattern_id(r: dict) -> str | None:
    """The single remediation pattern_id a rollup row maps to, or None.

    A row qualifies only when every finding that resolves to a pattern resolves
    to the *same* one (``r["pats"]`` holds exactly one pattern_id). Mixed or
    pattern-less rows return None and are never grouped.
    """
    pats = r.get("pats") or {}
    if len(pats) != 1:
        return None
    return next(iter(pats))


def _apply_pattern_groups(ordered):
    """Collapse rollup rows sharing one pattern_id across 3+ modules into a
    single pattern-group row, spliced into ``ordered`` (S3-4).

    Returns a new ``ordered`` list. When no pattern meets the module threshold
    the input is returned unchanged (same object), so downstream rendering is
    byte-for-byte identical for runs without cross-module patterns.
    """
    # Bucket qualifying rows (single pattern_id) by that pattern_id, preserving
    # their position so the group can re-insert where its first member sat.
    by_pat: dict[str, list[int]] = defaultdict(list)
    for idx, ((mod, _kind), r) in enumerate(ordered):
        pid = _row_pattern_id(r)
        if pid is not None:
            by_pat[pid].append(idx)

    def _distinct_modules(indices):
        return {ordered[i][0][0] for i in indices}

    # A pattern only groups when its member rows span 3+ DISTINCT modules.
    groupable = {
        pid: idxs
        for pid, idxs in by_pat.items()
        if len(_distinct_modules(idxs)) >= PATTERN_GROUP_MIN_MODULES
    }
    if not groupable:
        return ordered

    grouped_indices = {i for idxs in groupable.values() for i in idxs}
    pat_group_rows: dict[str, dict] = {}
    insert_at: dict[int, str] = {}
    for pid, idxs in groupable.items():
        member_rows = [ordered[i][1] for i in idxs]
        pat = next(
            (mr["pats"][pid] for mr in member_rows if pid in (mr.get("pats") or {})),
            {},
        )
        modules: list[str] = []
        for i in idxs:
            m = ordered[i][0][0]
            if m not in modules:
                modules.append(m)
        instance_count = sum(len(mr["findings"]) for mr in member_rows)
        # Highest severity across all members drives the group's severity tier.
        sev_total: Counter = Counter()
        for mr in member_rows:
            sev_total.update(mr["sev"])
        severity = next((s for s in SEV_ORDER if sev_total.get(s)), "?")
        # Most common finding_kind across members (by finding count).
        kind_counts: Counter = Counter()
        for i in idxs:
            kind_counts[ordered[i][0][1]] += len(ordered[i][1]["findings"])
        finding_kind = kind_counts.most_common(1)[0][0] if kind_counts else ""
        member_fids = [
            f.get("id")
            for mr in member_rows
            for f in mr["findings"]
            if f.get("id")
        ]
        pat_group_rows[pid] = {
            "is_pattern_group": True,
            "pattern_id": pid,
            "pattern_title": str(pat.get("title", "") or ""),
            "instance_count": instance_count,
            "affected_modules": modules,
            "member_rows": member_rows,
            "member_finding_ids": member_fids,
            "severity": severity,
            "finding_kind": finding_kind,
            # Mirror the rollup shape enough for the sort key + renderers
            # (emit_md/_exec_selection read files/pats/sev/findings directly, so
            # a pattern group renders as an ordinary row there without a branch).
            "findings": [f for mr in member_rows for f in mr["findings"]],
            "sev": sev_total,
            "files": {fp for mr in member_rows for fp in (mr.get("files") or set())},
            "pats": {pid: pat} if pat else {},
        }
        # Insert the group at its highest-severity member's slot (the earliest
        # index, since ``ordered`` is already severity-sorted).
        insert_at[min(idxs)] = pid

    # Rebuild ``ordered``: keep non-grouped rows, drop grouped member rows, and
    # splice each pattern-group row in where its first member sat.
    result = []
    for idx, item in enumerate(ordered):
        if idx in insert_at:
            pid = insert_at[idx]
            pg = pat_group_rows[pid]
            # Synthetic key mirrors (module, finding_kind) for tuple-unpacking
            # consumers; the pattern id keeps it unique across the page.
            result.append(((f"pattern:{pid}", pg["finding_kind"]), pg))
        if idx in grouped_indices:
            continue
        result.append(item)
    return result


# ---------------------------------------------------------------- Pattern synthesis
# Rows with more than this many proposed patterns get a model-generated one-line
# summary of the class of fix (opt-in via --synthesize; one LLM call per row).
SYNTHESIS_MIN_PATTERNS = 3
SYNTHESIS_MAX_PATTERNS = 20
SYNTHESIS_CHANGE_CHARS = 150


def synthesize_patterns(
    kind: str,
    module: str,
    patterns: list[dict],
    client,
) -> str:
    """Model-generated 2-3 sentence summary of the class of fix a row's patterns
    propose. Returns "" when there is nothing to summarize or the call fails —
    the brief degrades to its un-synthesized form rather than erroring."""
    if not patterns or client is None:
        return ""

    pat_summaries = []
    for p in patterns[:SYNTHESIS_MAX_PATTERNS]:
        changes = p.get("proposed_changes") or []
        if changes:
            pat_summaries.append(str(changes[0])[:SYNTHESIS_CHANGE_CHARS])

    if not pat_summaries:
        return ""

    prompt = (
        f"The following are proposed remediations for {len(patterns)} findings "
        f"of type '{kind}' in module '{module}'.\n\n"
        + "\n".join(f"- {s}" for s in pat_summaries)
        + "\n\nIn 2-3 plain sentences, summarize what class of fix these "
        "remediations propose. Write for a technical reviewer who needs to "
        "decide whether to approve this group for remediation. Do not list "
        "individual items — synthesize the pattern."
    )

    try:
        result = client.complete(prompt)
        return result.strip()
    except Exception as exc:
        # Log the failure before degrading to no-synthesis so operators can
        # distinguish "nothing to synthesize" from "synthesis crashed"
        # (F2-3821d3ee49a5 / F2-96026074a378).
        log.warning(
            "Pattern synthesis failed for %s in module %s; "
            "rendering without synthesis: %s: %s",
            kind, module, type(exc).__name__, exc,
        )
        return ""


class _LLMComplete:
    """Adapts an LLM client's ``call_model(system, user)`` to the simple
    ``complete(prompt) -> str`` interface :func:`synthesize_patterns` expects."""

    def __init__(self, client) -> None:
        self._client = client

    def complete(self, prompt: str) -> str:
        return self._client.call_model("", prompt).content


def _build_syntheses(ordered, client=None) -> dict[str, str]:
    """Synthesize the proposed-fix pattern for every row with more than
    SYNTHESIS_MIN_PATTERNS patterns. Returns a ``"kind|module" -> text`` map.

    When ``client`` is None an LLM client is constructed from the default
    Config (wrapped to expose ``.complete``). Results are cached by kind+module
    so a repeated key costs a single call."""
    if client is None:
        from sentinel.api.client import create_model_client
        from sentinel.config import Config
        try:
            client = _LLMComplete(create_model_client(Config()))
        except Exception as exc:
            # Degrade to no synthesis (visible) rather than crashing the whole
            # report when the model backend can't initialize (FIX-REQUIRED-313).
            log.warning(
                "Synthesis client init failed; skipping pattern synthesis: %s: %s",
                type(exc).__name__, exc,
            )
            return {}

    syntheses: dict[str, str] = {}
    for (mod, kind), r in ordered:
        pats = list(r["pats"].values())
        if len(pats) <= SYNTHESIS_MIN_PATTERNS:
            continue
        cache_key = f"{kind}|{mod}"
        if cache_key in syntheses:
            continue
        syntheses[cache_key] = synthesize_patterns(kind, mod, pats, client)
    return syntheses


def _synthesis_html(kind: str, module: str, syntheses: dict | None) -> str:
    """Render the model-generated pattern synthesis for a row, or '' if absent."""
    if not syntheses:
        return ""
    text = str(syntheses.get(f"{kind}|{module}", "")).strip()
    if not text:
        return ""
    return ('<div class="pattern-synthesis"><strong>Proposed fix pattern:</strong> '
            f'<span>{html.escape(text)}</span></div>')


# ---------------------------------------------------------------- Markdown
def _emit_md_alarm_focus(ordered) -> list[str]:
    """Markdown block spotlighting alarm & alarm-adjacent decision rows.

    References the full triage-table row numbers (does not copy detail), so the
    complete set of findings remains available below.
    """
    focus = alarm_focus(ordered)
    totals = _alarm_totals(ordered)
    # No alarms in this codebase: omit the section entirely (non-tool code).
    if not focus:
        return []
    L = ["## Alarm & alarm-adjacent focus", ""]
    L += [
        f"**{totals.get('direct', 0)} direct · {totals.get('adjacent', 0)} adjacent** "
        f"alarm-relevant finding(s) across {len(focus)} decision row(s). "
        "Row numbers match the triage table below.",
        "",
        "| # | Relevance | Module · finding_kind | Direct | Adjacent |",
        "|---|---|---|---|---|",
    ]
    for num, (mod, kind), r, tier in focus:
        counts = _row_alarm_counts(r)
        L.append(
            f"| {num} | {tier} | `{mod}` · `{kind}` "
            f"| {counts.get('direct', 0)} | {counts.get('adjacent', 0)} |"
        )
    L.append("")
    return L


def emit_md(ordered, total, sev_tot, out_md: Path, run_label: str, repo_name: str = ""):
    crit, p1 = _exec_selection(ordered)
    L = [f"# Remediation Decision Brief — {run_label}", "",
         f"**{total} findings → {len(ordered)} decision rows** (`module + finding_kind`). "
         f"Severity: {_sev_summary(sev_tot)}.", "",
         "## Executive summary", "",
         _preamble(repo_name), "",
         f"### Top priorities — all Critical + top {EXEC_P1_LIMIT} P1 categories", "",
         "| # | Module · issue type | Findings | Severity | What it is |",
         "|---|---|---|---|---|"]
    for i, (mod, kind), r in crit + p1:
        L.append(f"| {i} | `{mod}` · `{kind}` | {len(r['findings'])} | {_sev_summary(r['sev'])} "
                 f"| {_exec_desc(kind).replace('|', chr(92) + '|')} |")
    L += ["", "_Full triage table below._", "",
          "Triage each row: **Approve** / **Defer** / **Exclude**. Approval fans out to all "
          "member findings and runs under Phase-2 gates (test-first → QA → docs).", "",
          "> Metrics: **Severity** · **Blast radius** = files / sites.", ""]
    L += _emit_md_alarm_focus(ordered)
    L += ["## Triage table", "",
          "| # | Module · finding_kind | Findings | Severity | Files | Patterns | Decision |",
          "|---|---|---|---|---|---|---|"]
    for i, ((mod, kind), r) in enumerate(ordered, 1):
        L.append(f"| {i} | `{mod}` · `{kind}` | {len(r['findings'])} | {_sev_summary(r['sev'])} "
                 f"| {len(r['files'])} | {len(r['pats'])} | ☐ A ☐ D ☐ E |")
    L += ["", "## Decision rows (detail)", ""]
    for i, ((mod, kind), r) in enumerate(ordered, 1):
        files = sorted(r["files"])
        shown = ", ".join(f"`{x}`" for x in files[:6]) + (f" (+{len(files)-6})" if len(files) > 6 else "")
        proposed = _dedup_cap(c for p in r["pats"].values() for c in (p.get("proposed_changes") or []))
        L += [f"### {i}. `{mod}` · `{kind}`", "",
              f"- **Findings:** {len(r['findings'])} · **Severity:** {_sev_summary(r['sev'])}",
              f"- **Blast radius:** {len(files)} file(s) / {len(r['findings'])} sites — {shown or '_(none)_'}",
              f"- **Proposed fixes ({len(proposed)}):**"]
        for ch in proposed:
            L.append(f"  - {ch}")
        L += ["", "**Decision:** ☐ Approve ☐ Defer ☐ Exclude", "", "---", ""]
    out_md.write_text("\n".join(L), encoding="utf-8")


# ---------------------------------------------------------------- HTML
_CSS = """
:root{--crit:#b00020;--p1:#e8710a;--p2:#c79100;--p3:#5f6368;--ink:#1f2933;--line:#d6dbe1;--bg:#f7f8fa}
*{box-sizing:border-box}body{font:15px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:var(--ink);margin:0;background:var(--bg);display:flex;flex-direction:row;align-items:flex-start}
.wrap{max-width:1400px;width:100%;margin:0 auto;padding:32px 28px 80px;flex:1 1 auto;min-width:0;transition:none;height:100vh;overflow-y:auto}
h1{font-size:26px;margin:0 0 4px}h2{font-size:19px;margin:34px 0 12px;border-bottom:2px solid var(--line);padding-bottom:6px}
.sub{color:#52606d;margin:0 0 14px}
.note{background:#eef2f6;border-left:4px solid #9aa5b1;padding:10px 14px;border-radius:4px;color:#3e4c59;font-size:13.5px}
.alarm-note{background:#fff4e5;border-left-color:#e8710a}
body.alarm-only .card[data-alarm="none"]{display:none}
body.alarm-only .finding-row[data-alarm="none"]{display:none}
table{width:100%;table-layout:auto;border-collapse:collapse;font-size:13.5px;background:#fff;border:1px solid var(--line);border-radius:8px;overflow:hidden}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}
th{background:#eceff3;position:sticky;top:0;font-weight:600}
tr:hover td{background:#f0f5ff}
.badge{display:inline-block;padding:1px 7px;border-radius:10px;color:#fff;font-size:11.5px;font-weight:600;margin-right:3px;white-space:nowrap}
.crit{background:var(--crit)}.p1{background:var(--p1)}.p2{background:var(--p2)}.p3{background:var(--p3)}
.mono{font-family:ui-monospace,Consolas,monospace;font-size:12.5px}
.card{background:#fff;border:1px solid var(--line);border-left:5px solid #9aa5b1;border-radius:8px;padding:14px 18px;margin:14px 0;width:100%;box-sizing:border-box}
pre,code{overflow-x:auto;white-space:pre-wrap;word-break:break-word}
.card.crit{border-left-color:var(--crit)}.card.p1{border-left-color:var(--p1)}.card.p2{border-left-color:var(--p2)}.card.p3{border-left-color:var(--p3)}
.card h3{margin:0 0 8px;font-size:16px}
.meta{color:#52606d;font-size:13px;margin:2px 0}
.decision{margin-top:10px;font-weight:600}
.decision label{margin-right:16px;font-weight:500}
a.rowlink{color:#1a56db;text-decoration:none}a.rowlink:hover{text-decoration:underline}
details.explain{margin:8px 0;border:1px solid #d9e2ec;border-radius:6px;background:#f4f8fb}
details.explain>summary{cursor:pointer;padding:7px 12px;font-weight:600;color:#1a56db;font-size:13px;list-style:none}
details.explain>summary::-webkit-details-marker{display:none}
details.explain>summary::before{content:'\25B6';display:inline-block;margin-right:7px;font-size:10px;transition:transform .15s}
details.explain[open]>summary::before{transform:rotate(90deg)}
.explain .body{padding:2px 14px 12px}
.explain p{margin:6px 0;font-size:13px}.explain .lbl{font-weight:600;color:#334e68}
.explain ul{margin:4px 0 4px 18px;font-size:13px}.explain li{margin:2px 0}
.explain pre{background:#1f2933;color:#e6edf3;padding:8px 10px;border-radius:5px;font-size:12px;overflow:auto;margin:4px 0}
.explain .sk{display:flex;gap:10px;flex-wrap:wrap}.explain .sk>div{flex:1;min-width:240px}
.explain .risky b{color:#b00020}.explain .safer b{color:#2e7d32}
details.more-pats,details.more-files{margin-top:.5rem}
details.more-pats>summary,details.more-files>summary{cursor:pointer;color:#555;font-size:.85rem}
.cluster-narrative{background:#f8f9fa;border-left:3px solid #6c757d;padding:0.5rem 0.75rem;margin:0.5rem 0;font-size:0.9rem}
.pattern-synthesis{background:#fff8e1;border-left:3px solid #ffc107;padding:0.5rem 0.75rem;margin:0.5rem 0;font-size:0.9rem}
/* S3-4 — pattern-group card (cross-module remediation pattern) */
details.pattern-group{border-left-width:6px}
details.pattern-group>summary.pattern-group-summary{cursor:pointer;list-style:none;font-size:15px;user-select:none;margin:-2px 0 6px}
details.pattern-group>summary.pattern-group-summary::-webkit-details-marker{display:none}
details.pattern-group>summary.pattern-group-summary::before{content:'\25B6';display:inline-block;margin-right:8px;font-size:10px;transition:transform .15s}
details.pattern-group[open]>summary.pattern-group-summary::before{transform:rotate(90deg)}
.pattern-group-actions{display:flex;gap:8px;margin:6px 0 8px}
.pg-btn{font:inherit;font-size:12.5px;font-weight:700;padding:5px 13px;border-radius:5px;border:1px solid #b9c4d2;background:#fff;cursor:pointer}
.pg-btn.pg-approve{border-color:#1e7e34;color:#1e7e34}
.pg-btn.pg-defer{border-color:#8a6d00;color:#8a6d00}
.pg-btn:hover{background:#f4f8fb}
.toolbar{position:sticky;top:0;z-index:30;background:#fff;border:1px solid var(--line);border-radius:8px;padding:10px 14px;margin:0 0 18px;display:flex;align-items:center;gap:16px;flex-wrap:wrap;box-shadow:0 1px 5px rgba(0,0,0,.08)}
.toolbar button{font:inherit;font-weight:600;padding:8px 16px;border-radius:6px;border:1px solid #1a56db;background:#1a56db;color:#fff;cursor:pointer}
.toolbar button:disabled{background:#c3cad3;border-color:#c3cad3;cursor:not-allowed}
.toolbar .prog{font-size:13.5px;color:#52606d}
.toolbar .msg{font-size:13px;color:#2e7d32}.toolbar .msg.warn{color:#b00020}
/* cycle 62 — closure indicator (decided rows) + hide-decided toggle */
.toolbar label.toggle{font-size:13.5px;color:#52606d;cursor:pointer;user-select:none}
.toolbar .save-state{font-size:13px;font-weight:600}
.toolbar .save-state.dirty{color:#b00020}
.toolbar .save-state.ok{color:#1e7e34}
/* cycle 63 — group-level disposition fast path + rollup */
.group-disposition{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin:8px 0 6px;padding:8px 10px;background:#f5f8fc;border:1px solid #d6e2f2;border-radius:6px}
.group-disposition .gd-label{font-size:12.5px;font-weight:700;color:#334}
.gd-btn{font:inherit;font-size:12.5px;font-weight:600;padding:4px 11px;border-radius:5px;border:1px solid #b9c4d2;background:#fff;color:#1a2a3a;cursor:pointer}
.gd-btn:hover{background:#eef3f9}
.gd-btn.gd-approve{border-color:#1e7e34;color:#1e7e34}
.gd-btn.gd-exclude{border-color:#b00020;color:#b00020}
.gd-btn.gd-clear{border-color:#888;color:#555}
.group-rollup{font-size:12.5px;font-weight:600;color:#52606d;margin-left:auto}
.group-rollup.ok{color:#1e7e34}
.group-rollup.partial{color:#8a6d00}
.group-rationale{flex:1 1 100%;margin-top:4px;font-size:12.5px;min-height:2.2em;padding:4px 6px;border:1px solid #d6e2f2;border-radius:5px;box-sizing:border-box}
.decided-badge{display:none;font-size:11px;font-weight:700;padding:2px 8px;border-radius:10px;margin-left:8px;vertical-align:middle}
.finding-row.decided .decided-badge{display:inline-block}
.finding-row.decided{opacity:.45;transition:opacity .15s}
.finding-row.decided:hover,.finding-row.decided:focus-within{opacity:1}
.finding-sev-recal{display:inline-flex;align-items:center;gap:4px;cursor:help}
.finding-sev.sev-orig{opacity:.5;text-decoration:line-through;filter:grayscale(1)}
body.hide-decided .finding-row.decided{display:none}
.decided-badge.dec-APPROVE,.decided-badge.dec-APPROVE_WITH_CONDITIONS{background:#e6f4ea;color:#1e7e34}
.decided-badge.dec-DEFER{background:#fff3cd;color:#8a6d00}
.decided-badge.dec-EXCLUDE{background:#f8d7da;color:#b00020}
td.dcell{min-width:236px}
td.dcell .opts{display:flex;flex-direction:column;gap:1px}
td.dcell label.opt{font-weight:500;white-space:nowrap;cursor:pointer;margin:0;font-size:12.5px}
td.dcell .clabel{display:none;font-size:11.5px;font-weight:600;margin-top:5px;color:#334e68}
td.dcell textarea{display:none;width:100%;margin-top:4px;font:inherit;font-size:12px;padding:5px 7px;border:1px solid var(--line);border-radius:5px;resize:vertical;min-height:44px}
tr.decided td{background:#f3fbf4}
tr.needscond td{background:#fdecea}
tr.needscond td.dcell textarea{border-color:#b00020}
/* cycle 48 — sticky toolbar + table header */
#toolbar{position:sticky;top:0;z-index:30;background:#fff;padding:0.5rem 0;border-bottom:1px solid #dee2e6}
thead th{position:sticky;top:0;z-index:20;background:#fff;box-shadow:0 2px 2px rgba(0,0,0,0.1)}
/* cycle 48 — per-finding section + overrides */
details.findings-section{margin-top:0.75rem;border-top:1px solid #eee;padding-top:0.5rem}
details.findings-section>summary{cursor:pointer;color:#555;font-size:0.85rem;user-select:none}
.finding-row{display:flex;flex-wrap:wrap;align-items:baseline;gap:0.4rem;padding:0.4rem 0;border-bottom:1px solid #f0f0f0;font-size:0.85rem}
.finding-row:last-child{border-bottom:none}
.finding-id{font-family:monospace;color:#555}
.finding-loc{color:#52606d}
.finding-desc{flex:1 1 100%;color:#333;font-size:0.8rem}
.desc-more{border:none;background:none;padding:0;margin-left:0.15rem;color:#0d6efd;font-size:0.8rem;cursor:pointer;text-decoration:underline}
.row-diff-toggle{flex:1 1 100%;text-align:left;border:none;background:none;padding:0;margin:0.15rem 0;color:#0071C5;font-size:0.8rem;font-weight:600;cursor:pointer}
.row-diff{flex:1 1 100%;margin:0.2rem 0 0.4rem}
.finding-cond{flex:1 1 100%;margin:0.3rem 0;padding:0.45rem 0.6rem;background:#f6f9fc;border:1px solid #cfe2f3;border-radius:5px}
.fc-head{font-size:0.78rem;font-weight:600;color:#001E50;margin-bottom:0.35rem}
.fc-showall{border:none;background:none;padding:0;margin-top:0.3rem;color:#0071C5;font-size:0.78rem;cursor:pointer;text-decoration:underline}
.bundle-diff{margin-top:0.4rem}
.bundle-diff>summary{cursor:pointer;font-size:0.8rem;color:#0071C5;font-weight:600}
.diff-note{margin:0.4rem 0;padding:0.4rem 0.6rem;font-size:0.72rem;color:#7a4f00;background:#fff7e6;border:1px solid #ffd88a;border-radius:4px}
.diff-change-label{margin:0.6rem 0 0.2rem;font-size:0.76rem;font-weight:600;color:#001E50;display:flex;flex-wrap:wrap;gap:0.4rem;align-items:baseline}
.diff-change-num{flex:0 0 auto;background:#0071C5;color:#fff;border-radius:3px;padding:0.03rem 0.4rem;font-size:0.68rem;font-weight:700;letter-spacing:0.02em}
pre.diff{background:#1f2933;color:#e6edf3;padding:8px 10px;border-radius:5px;font-size:12px;overflow:auto;margin:0;white-space:pre-wrap;word-break:break-word}
/* GitHub-style unified diff: existing code as context, additions green, removals red */
.gh-diff{border:1px solid #d0d7de;border-radius:6px;overflow:auto;margin:0.1rem 0 0.2rem;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px;line-height:1.5;background:#fff}
.gh-line{display:flex;margin:0}
.gh-sign{flex:0 0 1.5em;text-align:center;user-select:none;color:#6e7781;white-space:pre}
.gh-code{flex:1 1 auto;padding-right:8px;white-space:pre-wrap;word-break:break-word}
.gh-ctx{background:#fff;color:#24292f}
.gh-add{background:#e6ffec;color:#24292f}
.gh-add .gh-sign{color:#1a7f37;background:#ccffd8}
.gh-del{background:#ffebe9;color:#24292f}
.gh-del .gh-sign{color:#cf222e;background:#ffd7d5}
.gh-hunk{background:#ddf4ff;color:#0550ae}
.gh-hunk .gh-sign{color:#0550ae}
.gh-file{background:#f6f8fa;color:#57606a;font-weight:600}
.gh-line.gh-del[data-act]{cursor:pointer}
.gh-line.gh-del[data-act]:hover{outline:1px solid #cf222e;outline-offset:-1px}
.view-changes-btn{font-size:0.75rem;padding:0.1rem 0.4rem;border:1px solid #0d6efd;border-radius:3px;cursor:pointer;background:#e8f0fe;color:#0d6efd}
.finding-triage{flex:1 1 100%;display:flex;flex-wrap:wrap;gap:0.6rem;align-items:center;margin-top:0.3rem}
.finding-triage label{font-weight:500;font-size:0.8rem;white-space:nowrap;cursor:pointer}
.ft-clear{font:inherit;font-size:0.72rem;color:#888;background:none;border:1px solid #ccc;border-radius:3px;padding:1px 6px;cursor:pointer}
.ft-clear:hover{background:#f0f0f0;color:#333}
.finding-rationale{width:100%;margin-top:0.25rem;font-size:0.8rem}
.toolbar .brand{font-weight:600;color:var(--ink)}
/* cycle 48 — before/after changes panel */
#changes-panel{flex:0 0 40%;width:40%;max-width:40%;position:sticky;top:0;height:100vh;overflow-y:auto;background:#fff;box-shadow:-4px 0 12px rgba(0,0,0,0.15);padding:1rem;box-sizing:border-box;display:none}
#changes-panel:not([hidden]){display:flex;flex-direction:column;gap:1rem}
#cp-header{display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid #dee2e6;padding-bottom:0.5rem;position:sticky;top:0;background:#fff}
#cp-header button{cursor:pointer;font-size:1rem;border:1px solid #dee2e6;border-radius:4px;background:#f8f9fa;padding:0.1rem 0.5rem}
#cp-title{font-family:ui-monospace,Consolas,monospace;font-size:0.9rem}
#cp-current code{display:block;margin-top:0.4rem;font-size:0.8rem;color:#52606d;word-break:break-all}
#cp-source-code .cp-src-line{display:block;white-space:pre-wrap;word-break:break-word}
#cp-source-code .cp-src-line.cp-hit{background:#fff3bf;box-shadow:inset 3px 0 0 #f59f00;border-radius:2px}
#cp-proposed pre{background:#f8f9fa;padding:0.75rem;border-radius:4px;white-space:pre-wrap;font-size:0.8rem}
.change-item{margin-bottom:1rem;border-bottom:1px solid #eee;padding-bottom:1rem}
/* cycle 49A — consequence spectrum */
.consequences{margin:0.4rem 0;font-size:0.82rem;border-left:3px solid #dc3545;padding-left:0.6rem;flex:1 1 100%}
.csq-label{font-weight:bold;color:#dc3545;margin-bottom:0.2rem;font-size:0.78rem}
.csq-row{display:flex;gap:0.5rem;margin:0.15rem 0;align-items:baseline}
.csq-tag{font-weight:600;min-width:5rem;font-size:0.78rem}
.csq-tag.green{color:#198754}.csq-tag.orange{color:#fd7e14}.csq-tag.red{color:#dc3545}
/* cycle 53 — personalized vs template indicator */
.csq-source{font-size:0.7rem;margin-left:0.3rem;vertical-align:middle}
.csq-personalized{color:#0d6efd}
.csq-template{color:#aaa}
/* cycle 50 — source reader / VS Code links */
.vscode-link{color:#0d6efd;text-decoration:none;font-size:0.82rem}
.vscode-link:hover{text-decoration:underline}
#cp-vscode{display:inline-block;margin:0.3rem 0;color:#0d6efd;font-size:0.85rem}
/* cycle 51 — group-level structured conditions panel */
.conditions-panel{background:#f0f7ff;border:1px solid #0d6efd;border-radius:6px;padding:1rem;margin:0.75rem 0}
.cp-header{margin-bottom:0.75rem}
.cp-subtitle{display:block;font-size:0.8rem;color:#555;margin-top:0.2rem}
.cp-section{margin-bottom:0.75rem}
.cp-section-title{font-weight:600;font-size:0.85rem;display:block;margin-bottom:0.3rem}
.cp-hint{font-weight:normal;color:#888;font-size:0.78rem}
.cp-change-row{display:flex;align-items:flex-start;gap:0.5rem;margin-bottom:0.4rem;font-size:0.82rem}
.cp-change-text{flex:1;min-width:0;color:#333;white-space:normal;overflow-wrap:anywhere}
.cp-radio-lbl{white-space:nowrap;font-size:0.8rem}
.cp-crit-row{display:flex;align-items:flex-start;gap:0.4rem;font-size:0.82rem;margin-bottom:0.3rem}
.cp-crit-text{min-width:0;overflow-wrap:anywhere}
.cp-scope-select{width:100%;min-height:4rem;font-size:0.82rem;border:1px solid #ccc;border-radius:3px}
.cp-toggle{display:flex;align-items:center;gap:0.4rem;font-size:0.85rem;cursor:pointer}
.cp-directive{width:100%;min-height:3rem;font-size:0.82rem;border:1px solid #ccc;border-radius:3px;padding:0.25rem}
.cp-footer{margin-top:0.5rem;text-align:right}
.cp-add-criterion{display:flex;gap:0.4rem;margin-top:0.5rem}
.cp-add-criterion-input{flex:1;font-size:0.82rem;border:1px solid #ccc;border-radius:3px;padding:0.25rem 0.5rem}
.cp-added-crit{display:flex;align-items:flex-start;gap:0.4rem;font-size:0.8rem;color:#198754;margin-top:0.2rem;overflow-wrap:anywhere}
.cp-remove-crit{flex:0 0 auto;border:1px solid #198754;background:#fff;color:#198754;border-radius:3px;cursor:pointer;font-size:0.72rem;line-height:1;padding:1px 5px}
.cp-remove-crit:hover{background:#198754;color:#fff}
@media print{body{background:#fff;display:block}.card{break-inside:avoid;page-break-inside:avoid}th{position:static}.wrap{max-width:none;padding:0;height:auto;overflow:visible}details.explain{break-inside:avoid}.toolbar{display:none}#changes-panel{display:none}}
"""


# Browser JS for interactive triage + export. Plain string (NOT an f-string) so
# JS braces need no escaping; NROWS is injected via a separate <script> tag.
_JS = r"""
/* cycle 49A — per-finding triage is the sole decision surface */
window._findingDecisions = {};
function _updateProgress(){
  var n=Object.keys(window._findingDecisions).filter(function(fid){
    return window._findingDecisions[fid] && window._findingDecisions[fid].decision;
  }).length;
  var el=document.getElementById('progCount'); if(el) el.textContent=n;
}
function setFindingDecision(fid,decision){
  var rec=window._findingDecisions[fid]||{};
  rec.decision=decision;
  var fr=document.getElementById('fr-'+fid);
  var wantR=(decision==='DEFER'||decision==='EXCLUDE');
  if(fr){ fr.hidden=!wantR; if(wantR) fr.focus(); }
  if(!wantR) rec.rationale=null;
  window._findingDecisions[fid]=rec;
  // cycle 65 — the inline per-finding conditions dropdown is revealed/hidden by
  // _applyDecidedUI (which runs for both live selection and reload restore).
  _updateProgress();
}
function setFindingRationale(fid,val){
  var rec=window._findingDecisions[fid]||{};
  rec.rationale=val||null; window._findingDecisions[fid]=rec; _updateProgress();
}
/* cycle 51 — group-level structured conditions */
/* cycle 65 — conditions are keyed PER FINDING (not per group), so "Approve with
   conditions" on one finding captures conditions for that finding alone. The
   panel DOM stays group-scoped and rebinds to the finding(s) it currently edits:
   _panelTargets[gid] is [fid] for a per-finding open, or every member fid for the
   group "Approve w/ conditions" button (which writes the same conditions to all).
   The change list + its change_N index space stay group-level (shared), so
   Phase 2's merge_conditions() still matches REQUIRE/EXCLUDE by identical target. */
window._findingConditions = {};   // keyed by finding_id
window._panelTargets = {};        // gid -> [fid,...] the open panel writes to
function _ensureFinding(fid){
  if(!window._findingConditions[fid])
    window._findingConditions[fid]={changes:[],criteria:[],scope:[],directive:'',added_criteria:[]};
}
function _panelFids(gid){
  var t=window._panelTargets[gid];
  return (t&&t.length)?t:(typeof _groupFids==='function'?_groupFids(gid):[]);
}
/* FIX-REQUIRED-234 — stable identifier for an acceptance criterion, derived
   from its text (djb2). Condition state is keyed by this id so restoration
   survives criteria being reordered between sessions, unlike array position. */
function _critId(s){
  var str=String(s), h=5381, i=str.length;
  while(i) h=(h*33)^str.charCodeAt(--i);
  return 'c'+(h>>>0).toString(36);
}
function updateChange(gid,idx,action){
  _panelFids(gid).forEach(function(fid){
    _ensureFinding(fid);
    var gc=window._findingConditions[fid];
    gc.changes=gc.changes.filter(function(c){return c.idx!==idx;});
    if(action!=='NONE') gc.changes.push({idx:idx,action:action});  // cycle 64 — NONE clears the choice
  });
}
/* cycle 65 — write a single finding's change condition directly (used by the
   inline per-finding "Approve with conditions" dropdown). Bound to ONE fid so it
   is immune to _panelTargets (which the group button mutates); idx is the shared,
   original change_N target so Phase-2 merge_conditions still matches. */
function updateFindingChange(fid,idx,action){
  _ensureFinding(fid);
  var gc=window._findingConditions[fid];
  gc.changes=gc.changes.filter(function(c){return c.idx!==idx;});
  if(action!=='NONE') gc.changes.push({idx:idx,action:action});
}
function updateCriterion(gid,idx,cid,checked,text){
  _panelFids(gid).forEach(function(fid){
    _ensureFinding(fid);
    var gc=window._findingConditions[fid];
    // FIX-REQUIRED-234 — key by the stable criterion id, not the array position.
    gc.criteria=gc.criteria.filter(function(c){return c.cid!==cid;});
    if(checked) gc.criteria.push({idx:idx,cid:cid,text:text});
  });
}
function updateScope(gid){
  var sel=document.getElementById('cps-'+gid);
  var vals=Array.prototype.map.call(sel.selectedOptions,function(o){return o.value;});
  _panelFids(gid).forEach(function(fid){ _ensureFinding(fid); window._findingConditions[fid].scope=vals.slice(); });
}
function updateDirective(gid,val){
  _panelFids(gid).forEach(function(fid){ _ensureFinding(fid); window._findingConditions[fid].directive=val; });
}
function clearConditions(gid){
  // "Clear all conditions" clears every member of the group (also used by
  // clearGroupDecision), regardless of the panel's current per-finding target.
  (_groupFids(gid)||[]).forEach(function(fid){ delete window._findingConditions[fid]; });
  var panel=document.getElementById('cp-'+gid);
  if(!panel) return;
  panel.querySelectorAll('input, textarea, select').forEach(function(el){
    if(el.type==='checkbox'||el.type==='radio') el.checked=false;
    else if(el.tagName==='SELECT'){ Array.prototype.forEach.call(el.options,function(o){o.selected=false;}); }
    else el.value='';
  });
  panel.hidden=true;
}
/* Guarded dataset JSON parsing (FIX-REQUIRED-317 / 328): a malformed embedded
   data-* payload must not throw out of a click handler and wedge the panel or
   abort export. Returns {ok:true,value} or {ok:false,error}. */
function _parseDataset(card,field){
  try{ return {ok:true, value:JSON.parse((card&&card.dataset[field])||'[]')}; }
  catch(e){ return {ok:false, error:e}; }
}
function _showPanelError(gid,msg){
  var panel=document.getElementById('cp-'+gid); if(!panel) return;
  var cd=document.getElementById('cpc-'+gid);
  if(cd){ cd.innerHTML=''; var err=document.createElement('div');
    err.className='cp-error'; err.textContent=msg; cd.appendChild(err); }
  panel.hidden=false;
}
/* Populate + show a group's conditions panel. Rows are built with the DOM API
   (not innerHTML string interpolation) so change/criterion text containing
   quotes or < > can't break the markup. */
function openConditionsPanel(gid){
  /* FIX-REQUIRED-295 — if the panel is already open for the SAME scope, keep it
     open rather than tearing it down and rebuilding (which flickers and drops
     the owner's focus mid-edit). cycle 65: rebuild when the scoped target
     (which finding, or the whole group) changed, so the per-finding change
     filter re-applies when switching findings within a group. */
  var _openEl=document.getElementById('cp-'+gid);
  if(_openEl && !_openEl.hidden && _openEl.dataset.scopedTo===_panelFids(gid).join(',')) return;
  var card=document.querySelector('.card[data-group-id-safe="'+gid+'"]');
  if(!card) return;
  var pc=_parseDataset(card,'proposedChanges'), ac=_parseDataset(card,'acceptanceCriteria'), gf=_parseDataset(card,'groupFiles');
  if(!pc.ok||!ac.ok||!gf.ok){ _showPanelError(gid,'Could not open conditions: this group\'s condition data is malformed.'); return; }
  var changes=pc.value, criteria=ac.value, files=gf.value;
  var cd=document.getElementById('cpc-'+gid); cd.innerHTML='';
  /* cycle 65 — when the panel is scoped to a SINGLE finding, show only the
     change(s) anchored to that finding's file (Part C anchors each change to
     file:line), so a per-finding "Approve with conditions" is easier to consume
     than the whole cluster's list. Each change keeps its ORIGINAL index (i) —
     change_N is a shared, group-level target space, so Phase-2 merge_conditions
     still matches by target. Falls back to all changes when nothing matches
     (e.g. unanchored prose) or when the panel is scoped to the whole group. */
  var _visible=changes.map(function(c,i){return {c:c,i:i};});
  var _tf=_panelFids(gid);
  if(_tf.length===1){
    var _frow=document.querySelector('.finding-row[data-fid="'+_tf[0]+'"]');
    var _ff=_frow?(_frow.dataset.file||''):'';
    if(_ff){
      var _base=_ff.split('/').pop();
      var _m=_visible.filter(function(o){ var s=String(o.c); return s.indexOf(_ff)>=0 || (_base && s.indexOf(_base)>=0); });
      if(_m.length>0) _visible=_m;
    }
  }
  _visible.forEach(function(o,di){
    var c=o.c, i=o.i;  // i = original (shared) change index; di = display order
    var rowEl=document.createElement('div'); rowEl.className='cp-change-row';
    var txt=document.createElement('span'); txt.className='cp-change-text';
    txt.textContent=(di+1)+'. '+String(c);  // full text; wraps via CSS
    rowEl.appendChild(txt);
    // cycle 64 — NONE (default) lets the owner clear a require/exclude choice.
    [['NONE','None'],['REQUIRE','Require'],['EXCLUDE','Exclude']].forEach(function(pair){
      var action=pair[0];
      var lbl=document.createElement('label'); lbl.className='cp-radio-lbl';
      var inp=document.createElement('input');
      inp.type='radio'; inp.name='chg-'+gid+'-'+i; inp.value=action;
      if(action==='NONE') inp.checked=true;
      inp.addEventListener('change',function(){ updateChange(gid,i,action); });
      lbl.appendChild(inp);
      lbl.appendChild(document.createTextNode(' '+pair[1]));
      rowEl.appendChild(lbl);
    });
    cd.appendChild(rowEl);
  });
  var qd=document.getElementById('cpq-'+gid); qd.innerHTML='';
  criteria.forEach(function(q,i){
    var lbl=document.createElement('label'); lbl.className='cp-crit-row';
    var inp=document.createElement('input'); inp.type='checkbox';
    var text=String(q).substring(0,200);
    // FIX-REQUIRED-234 — tag each criterion checkbox with its stable id so
    // rehydration can restore state by identity rather than by position.
    var cid=_critId(text);
    inp.setAttribute('data-cid',cid);
    inp.addEventListener('change',function(){ updateCriterion(gid,i,cid,inp.checked,text); });
    var ctxt=document.createElement('span'); ctxt.className='cp-crit-text';
    ctxt.textContent=' '+String(q);  // full text; wraps via CSS
    lbl.appendChild(inp);
    lbl.appendChild(ctxt);
    qd.appendChild(lbl);
  });
  var ss=document.getElementById('cps-'+gid); ss.innerHTML='';
  files.forEach(function(fp){
    var opt=document.createElement('option'); opt.value=fp; opt.textContent=fp; ss.appendChild(opt);
  });
  // Reflect the panel's current scope (one finding vs the whole group) so the
  // owner knows what these conditions attach to.
  var sub=document.getElementById('cp-sub-'+gid);
  if(sub){ var tf=_panelFids(gid);
    if(tf.length===1){
      var _note=(_visible.length<changes.length)
        ? (' Showing the '+_visible.length+' change'+(_visible.length===1?'':'s')+' touching this finding’s file (of '+changes.length+' in the cluster).')
        : '';
      sub.textContent='Conditions apply to this finding ('+tf[0]+').'+_note+' They will be enforced by Sentinel’s remediation agent.';
    } else {
      sub.textContent='Conditions apply to all '+tf.length+' findings in this cluster. They will be enforced by Sentinel’s remediation agent.';
    }
  }
  var _cpEl=document.getElementById('cp-'+gid);
  _cpEl.dataset.scopedTo=_panelFids(gid).join(',');
  _cpEl.hidden=false;
}
function addCriterion(gid){
  var input=document.getElementById('cpac-'+gid);
  var text=(input&&input.value||'').trim();
  if(!text) return;
  _panelFids(gid).forEach(function(fid){
    _ensureFinding(fid);
    var gc=window._findingConditions[fid];
    gc.added_criteria=gc.added_criteria||[];
    gc.added_criteria.push(text);
  });
  input.value='';
  _renderAddedCriteria(gid);  // renders each added criterion with a remove (✕) control
}
function buildConditionsStructured(fid){
  var gc=window._findingConditions[fid];
  if(!gc) return [];
  // The change/criterion INDEX space stays group-level (shared change_N targets),
  // so merge_conditions() in Phase 2 matches REQUIRE/EXCLUDE by identical target.
  var gid=(window._fidToGroup&&window._fidToGroup[fid])||'';
  var card=document.querySelector('.card[data-group-id-safe="'+gid+'"]');
  var changeIndex=[], critIndex=[];
  if(card){
    var ci=_parseDataset(card,'changeIndex'), qi=_parseDataset(card,'criterionIndex');
    if(!ci.ok||!qi.ok){ alert('Cannot export decisions: condition data for finding "'+fid+'" is malformed. Fix or clear its conditions and retry.'); return null; }
    changeIndex=ci.value; critIndex=qi.value;
  }
  var items=[];
  gc.changes.forEach(function(c){
    var hit=changeIndex.find(function(x){return x.idx===c.idx;});
    var ct=hit?hit.text:null;
    items.push({type:c.action==='REQUIRE'?'REQUIRE_CHANGE':'EXCLUDE_CHANGE',target:'change_'+c.idx,note:ct});
  });
  gc.criteria.forEach(function(q){
    var hit=critIndex.find(function(x){return x.idx===q.idx;});
    var qt=(hit?hit.text:q.text)||null;
    items.push({type:'REQUIRE_CRITERION',target:'criterion_'+q.idx,note:qt?String(qt).substring(0,200):null});
  });
  if(gc.scope.length>0) items.push({type:'SCOPE_LIMIT',target:gc.scope.join(','),note:null});
  if(gc.directive.trim()) items.push({type:'APPROACH_DIRECTIVE',target:null,note:gc.directive.trim().substring(0,500)});
  (gc.added_criteria||[]).forEach(function(text){
    items.push({type:'ADD_CRITERION',target:null,note:text});
  });
  return items;
}
function renderConditionsSummary(items){
  if(!items.length) return null;
  return items.map(function(it){
    if(it.type==='REQUIRE_CHANGE') return 'Require '+it.target;
    if(it.type==='EXCLUDE_CHANGE') return 'Exclude '+it.target;
    if(it.type==='REQUIRE_CRITERION') return 'Require criterion: '+it.note;
    if(it.type==='ADD_CRITERION') return 'Add criterion: '+it.note;
    if(it.type==='SCOPE_LIMIT') return 'Scope to: '+it.target;
    if(it.type==='APPROACH_DIRECTIVE') return 'Directive: '+it.note;
    return it.type;
  }).join('; ');
}
function exportDecisions(){
  // cycle 64 — warn about acceptance criteria typed but not "Add"-ed, so they
  // aren't silently dropped from the export.
  var pending=[];
  document.querySelectorAll('input[id^="cpac-"]').forEach(function(inp){
    if(inp.value && inp.value.trim()) pending.push(inp.value.trim());
  });
  if(pending.length && !confirm('You have '+pending.length+' acceptance criterion(s) typed but not added (e.g. "'+pending[0].substring(0,60)+'"). They will NOT be saved. Click Cancel to go press "Add" (or clear the box), or OK to export without them.')) return;
  var decisions=[];
  var byGroup={};
  for(var fid in window._findingDecisions){
    var dec=window._findingDecisions[fid];
    if(!dec || !dec.decision) continue;
    var gid=(window._fidToGroup && window._fidToGroup[fid])||'__ungrouped__';
    if(!byGroup[gid]) byGroup[gid]=[];
    byGroup[gid].push({fid:fid,dec:dec});
  }
  if(Object.keys(byGroup).length===0){ alert('No decisions made yet.'); return; }
  // cycle 65 — conditions are built PER FINDING from _findingConditions[fid], so
  // two findings in one cluster can carry different conditions. Phase 2 merges
  // them per task via merge_conditions(). An index loop (not forEach) lets a
  // malformed dataset abort the whole export cleanly.
  for(var g in byGroup){
    var entries=byGroup[g];
    var types={};
    entries.forEach(function(en){types[en.dec.decision]=true;});
    var hasConflict=((types['APPROVE']||types['APPROVE_WITH_CONDITIONS'])&&(types['DEFER']||types['EXCLUDE']));
    for(var ei=0;ei<entries.length;ei++){
      var en=entries[ei];
      var cs=buildConditionsStructured(en.fid);
      if(cs===null){ return; }  // malformed dataset; alert shown, abort export
      var entry={
        finding_id:en.fid,
        td_id:(window._fidToTd && window._fidToTd[en.fid])||null,
        decision:en.dec.decision,
        conditions:cs.length?renderConditionsSummary(cs):null,
        conditions_structured:cs.length?cs:null,
        rationale:en.dec.rationale||null
      };
      if(hasConflict) entry.conflict=true;
      // S3-5 — a DEFER with a rationale can seed a suppression rule (learning
      // pass). Read the finding's file/kind/location from its rendered row so
      // the exported decision carries a ready-to-apply suppression_rule object.
      if(en.dec.decision==='DEFER' && en.dec.rationale){
        var meta=(window._fidMeta||{})[en.fid]||{};
        var sr={
          file:meta.file||'',
          finding_kind:meta.finding_kind||'',
          location_pattern:meta.location||'',
          rationale:en.dec.rationale
        };
        if(sr.file || sr.finding_kind) entry.suppression_rule=sr;
      }
      decisions.push(entry);
    }
  }
  if(decisions.length===0){ alert('No decisions made yet.'); return; }
  for(var k=0;k<decisions.length;k++){
    var d=decisions[k];
    if(d.decision==='APPROVE_WITH_CONDITIONS' && !d.conditions){
      alert('Finding '+d.finding_id+' is approved with conditions but no conditions were set. '+
            'Please add conditions or change to Approve.');
      return;
    }
  }
  var blob=new Blob([JSON.stringify(decisions,null,2)],{type:'application/json'});
  // cycle 64 — _saveExport (in the save-state script) offers a real "Save As"
  // (File System Access API) so the owner picks the IP-drive folder directly,
  // falling back to a Downloads download; it also marks the work saved.
  // Stable filename (not dated) so re-exports overwrite one living decisions
  // file the owner re-imports to resume, rather than piling up dated copies.
  _saveExport(blob, 'decisions.json', decisions.length);
}

/* cycle 48 — before/after changes panel */
function showChangesPanel(btn){
  openChangesForRow(btn.closest('.finding-row'));
}
function _renderSourceLines(el, src){
  // Render the Current code as per-line elements so a line can be highlighted +
  // scrolled to when the code owner clicks a red diff line.
  if(!el) return;
  el.textContent='';
  if(!src){ el.textContent='Source not available'; return; }
  String(src).split('\n').forEach(function(line){
    var d=document.createElement('span'); d.className='cp-src-line'; d.textContent=line;
    el.appendChild(d);
  });
}
function openChangesForRow(row){
  if(!row) return;
  var fid=row.dataset.fid, loc=row.dataset.location;
  /* F2-ea9b0ae1dac7 / F2-8ae4a683eacd / F2-89403999c304 / F2-dc2a1cb3d263 —
     guard JSON.parse of the embedded data-changes payload: a malformed blob
     must not throw out of the click handler and wedge the panel. Fall back to
     an empty changes list. */
  var changes;
  try{ changes=JSON.parse(row.dataset.changes||'[]'); }
  catch(e){ console.error('openChangesForRow: malformed data-changes JSON', e); changes=[]; }
  document.getElementById('cp-title').textContent=fid+(loc?' · '+loc:'');
  var src=row.dataset.source||'';
  _renderSourceLines(document.getElementById('cp-source-code'), src);
  var ghLink=row.dataset.github||'';
  var ghEl=document.getElementById('cp-github');
  if(ghEl){
    if(ghLink){ ghEl.href=ghLink; ghEl.textContent='View on GitHub →'; ghEl.hidden=false; }
    else { ghEl.hidden=true; }
  }
  var vsLink=row.dataset.vscode||'';
  var vsEl=document.getElementById('cp-vscode');
  if(vsEl){
    if(vsLink){ vsEl.href=vsLink; vsEl.textContent='Open in VS Code →'; vsEl.hidden=false; }
    else { vsEl.hidden=true; }
  }
  var cd=document.getElementById('cp-changes'); cd.innerHTML='';
  if(changes.length===0){ cd.textContent='No proposed changes recorded'; }
  else{
    changes.forEach(function(c,i){
      var d=document.createElement('div'); d.className='change-item';
      var h=document.createElement('strong'); h.textContent='Change '+(i+1);
      var pre=document.createElement('pre'); pre.textContent=String(c);
      d.appendChild(h); d.appendChild(pre); cd.appendChild(d);
    });
  }
  /* cycle 65 — the illustrative diff now lives on the inline "Show illustrative
     change" toggle on each finding row (more discoverable); it is intentionally
     NOT duplicated here in the 'View changes' side panel, which stays focused on
     the current source + the prose proposed changes. */
  var panel=document.getElementById('changes-panel');
  panel.hidden=false; panel.style.display='flex';
}
function closeChangesPanel(){
  var panel=document.getElementById('changes-panel');
  panel.hidden=true; panel.style.display='none';
}
document.addEventListener('keydown',function(ev){
  if(ev.key==='Escape') closeChangesPanel();
});
"""


# cycle 62 — closure indicator (sense 1 live + sense 2 persisted) + save-state
# (localStorage auto-save + JSON import). Implemented as wrappers over the
# cycle-48/51 triage JS so the originals stay untouched; loaded as a second
# <script> after _JS.
_JS_STATE = r"""
var _DEC_LABEL={APPROVE:'Approved',APPROVE_WITH_CONDITIONS:'Approved w/ conditions',DEFER:'Deferred',EXCLUDE:'Excluded'};
/* cycle 62 (option 1) — the on-drive Export JSON is the durable save; the
   localStorage auto-save is only a same-machine crash backup. _dirty tracks
   "changes not yet written to the drive" so we can nudge before unload. */
window._dirty=false; window._lastSaveName='';
function _hasDecisions(){
  for(var f in window._findingDecisions){ if(window._findingDecisions[f]&&window._findingDecisions[f].decision) return true; }
  return false;
}
function _updateSaveState(){
  var el=document.getElementById('saveState'); if(!el) return;
  if(!_hasDecisions()){ el.textContent=''; el.className='save-state'; return; }
  if(window._dirty){ el.textContent='● Unsaved changes — Export to save them on the drive'; el.className='save-state dirty'; }
  else if(window._lastSaveName){ el.textContent='✓ Saved to '+window._lastSaveName; el.className='save-state ok'; }
  else { el.textContent='✓ Matches applied triage'; el.className='save-state ok'; }
}
function _markDirty(){ window._dirty=true; _updateSaveState(); }
function _markSaved(name){ window._dirty=false; if(name) window._lastSaveName=name; _updateSaveState(); }
function _rowEl(fid){
  var rows=document.querySelectorAll('.finding-row');
  for(var i=0;i<rows.length;i++){ if(rows[i].dataset.fid===fid) return rows[i]; }
  return null;
}
function _applyDecidedUI(fid,decision){
  var row=_rowEl(fid); if(!row) return;
  var has=!!decision; row.classList.toggle('decided',has);
  var b=row.querySelector('.decided-badge');
  if(b){ b.textContent=has?(_DEC_LABEL[decision]||decision):''; b.className='decided-badge'+(has?(' dec-'+decision):''); }
  // cycle 65 — reveal the inline per-finding conditions dropdown for AWC (built
  // lazily + pre-checked from _findingConditions[fid]); hide for other decisions.
  if(decision==='APPROVE_WITH_CONDITIONS'){ revealInlineConditions(fid); }
  else { hideInlineConditions(fid); }
}
function _persistState(){
  try{ if(window._runKey) localStorage.setItem(window._runKey,
    JSON.stringify({v:1,decisions:window._findingDecisions,findings:window._findingConditions})); }catch(e){}
}
function _applyAllDecided(){
  for(var fid in window._findingDecisions){
    var rec=window._findingDecisions[fid]; if(!rec||!rec.decision) continue;
    var radio=document.querySelector('input[name="ft-'+fid+'"][value="'+rec.decision+'"]');
    if(radio) radio.checked=true;
    if(rec.rationale){ var fr=document.getElementById('fr-'+fid); if(fr){fr.hidden=false;fr.value=rec.rationale;} }
    _applyDecidedUI(fid,rec.decision);
  }
  // Re-hydrate each group's comment box from its deferred/excluded members.
  document.querySelectorAll('.group-rationale').forEach(function(gr){
    var fids=_groupFids(gr.dataset.gid||''); var show=false, val='';
    for(var i=0;i<fids.length;i++){ var r=window._findingDecisions[fids[i]];
      if(r&&(r.decision==='DEFER'||r.decision==='EXCLUDE')){ show=true; if(r.rationale){ val=r.rationale; break; } } }
    if(show){ gr.hidden=false; gr.value=val; }
  });
  _updateProgress();
}
/* cycle 63 — group-level disposition. Drive from the full _fidToGroup map so
   it covers findings beyond the FINDINGS_CARD_CAP render cap; rendered rows get
   their radio/UI updated, unrendered ones just get their decision recorded
   (export iterates _findingDecisions, so they are included). */
function _groupFids(gid){
  var out=[]; if(!window._fidToGroup) return out;
  for(var fid in window._fidToGroup){ if(window._fidToGroup[fid]===gid) out.push(fid); }
  return out;
}
function _updateGroupRollup(gid){
  var card=document.querySelector('.card[data-group-id-safe="'+gid+'"]'); if(!card) return;
  var el=card.querySelector('.group-rollup'); if(!el) return;
  var fids=_groupFids(gid); var total=fids.length; var counts={}; var decidedN=0;
  fids.forEach(function(fid){ var rec=window._findingDecisions[fid]; var d=rec&&rec.decision; if(d){ decidedN++; counts[d]=(counts[d]||0)+1; } });
  if(decidedN===0){ el.textContent=''; el.className='group-rollup'; return; }
  var keys=Object.keys(counts);
  if(decidedN===total && keys.length===1){ el.textContent='✓ All '+total+' '+(_DEC_LABEL[keys[0]]||keys[0]); el.className='group-rollup ok'; return; }
  var parts=keys.map(function(k){ return counts[k]+' '+(_DEC_LABEL[k]||k); });
  var undec=total-decidedN; if(undec>0) parts.push(undec+' undecided');
  el.textContent=parts.join(' · '); el.className='group-rollup'+(undec>0?' partial':'');
}
function setGroupDecision(gid,decision){
  var wantR=(decision==='DEFER'||decision==='EXCLUDE');
  _groupFids(gid).forEach(function(fid){
    var rec=window._findingDecisions[fid]||{};
    rec.decision=decision; if(!wantR) rec.rationale=null;
    window._findingDecisions[fid]=rec;
    var _row=_rowEl(fid);
    var radio=_row?_row.querySelector('input[type="radio"][value="'+decision+'"]'):null;
    if(radio) radio.checked=true;
    var fr=document.getElementById('fr-'+fid); if(fr){ fr.hidden=!wantR; if(!wantR) fr.value=''; }
    _applyDecidedUI(fid,decision);
  });
  if(decision==='APPROVE_WITH_CONDITIONS'){ window._panelTargets[gid]=_groupFids(gid); openConditionsPanel(gid); }  // group scope: all members
  var gr=document.getElementById('gr-'+gid);
  if(gr){ gr.hidden=!wantR; if(!wantR){ gr.value=''; } }
  _persistState(); _markDirty(); _updateGroupRollup(gid); _updateProgress();
}
/* Group-level comment: fan one rationale to every deferred/excluded member so
   the Export payload (per-finding rationale) carries it without a new field. */
function setGroupRationale(gid,val){
  var v=val||null;
  _groupFids(gid).forEach(function(fid){
    var rec=window._findingDecisions[fid];
    if(!rec||(rec.decision!=='DEFER'&&rec.decision!=='EXCLUDE')) return;
    rec.rationale=v; window._findingDecisions[fid]=rec;
    var fr=document.getElementById('fr-'+fid); if(fr){ fr.value=val||''; }
  });
  _persistState(); _markDirty();
}
function _updateAllRollups(){
  document.querySelectorAll('.card[data-group-id-safe]').forEach(function(c){ _updateGroupRollup(c.dataset.groupIdSafe); });
}
/* cycle 64 — clear a disposition back to "undecided" (radios can't be
   unselected natively). Per-finding and whole-group variants. */
function clearFindingDecision(fid){
  delete window._findingDecisions[fid];
  var _row=_rowEl(fid); if(_row) _row.querySelectorAll('input[type="radio"]').forEach(function(r){ r.checked=false; });
  var fr=document.getElementById('fr-'+fid); if(fr){ fr.hidden=true; fr.value=''; }
  _applyDecidedUI(fid,null);
  _persistState(); _markDirty(); _updateProgress();
  var row=_rowEl(fid); if(row) _updateGroupRollup(row.dataset.groupIdSafe);
}
function clearGroupDecision(gid){
  _groupFids(gid).forEach(function(fid){
    delete window._findingDecisions[fid];
    var _row=_rowEl(fid); if(_row) _row.querySelectorAll('input[type="radio"]').forEach(function(r){ r.checked=false; });
    var fr=document.getElementById('fr-'+fid); if(fr){ fr.hidden=true; fr.value=''; }
    _applyDecidedUI(fid,null);
  });
  // FIX-REQUIRED-284 — clearing a group's disposition must also reset its
  // conditions-panel state, otherwise the old conditions linger in the panel
  // (and would be re-exported) after the decision that owned them is gone.
  clearConditions(gid);
  var gr=document.getElementById('gr-'+gid); if(gr){ gr.hidden=true; gr.value=''; }
  _persistState(); _markDirty(); _updateGroupRollup(gid); _updateProgress();
}
/* cycle 64 — the group "Approve w/ conditions" button toggles its panel:
   open -> collapse (decision kept), closed -> disposition the group + open. */
function toggleGroupConditions(gid){
  var panel=document.getElementById('cp-'+gid);
  if(panel && !panel.hidden){ panel.hidden=true; return; }
  // Panel hidden: if the group is already Approve-with-conditions, reopen the
  // panel explicitly rather than depending on a decision-change side effect
  // (FIX-REQUIRED-288); otherwise set the disposition (which opens it).
  var alreadyAWC=_groupFids(gid).some(function(fid){
    var rec=window._findingDecisions[fid]; return rec && rec.decision==='APPROVE_WITH_CONDITIONS';
  });
  if(alreadyAWC){ window._panelTargets[gid]=_groupFids(gid); openConditionsPanel(gid); }
  else { setGroupDecision(gid,'APPROVE_WITH_CONDITIONS'); }
}
function _restoreState(){
  /* Seed prior persisted triage from the emitted map — rows are lazy-rendered,
     so they cannot be read from the live DOM at load. */
  if(window._fidTriaged){ for(var _ft in window._fidTriaged){
    window._findingDecisions[_ft]={decision:window._fidTriaged[_ft]}; } }
  /* Overlay in-progress session state from localStorage (more recent wins). */
  var hadLocal=false;
  try{ if(window._runKey){ var raw=localStorage.getItem(window._runKey);
    if(raw){ var s=JSON.parse(raw);
      if(s&&s.decisions){ for(var k in s.decisions){ window._findingDecisions[k]=s.decisions[k]; hadLocal=true; } }
      if(s&&s.findings) window._findingConditions=s.findings;
      else if(s&&s.groups){ /* back-compat: migrate old group-keyed state to each member finding */
        for(var _g in s.groups){ (_groupFids(_g)||[]).forEach(function(_fid){
          window._findingConditions[_fid]=JSON.parse(JSON.stringify(s.groups[_g])); }); } } } } }catch(e){}
  _applyAllDecided();
  /* Restored work from the local backup is not yet on the drive -> flag it so
     the owner is nudged to Export; a backup-only copy is never authoritative. */
  if(hadLocal) _markDirty(); else _updateSaveState();
  _updateAllRollups();
}
function _reverseConditions(items){
  var gc={changes:[],criteria:[],scope:[],directive:'',added_criteria:[]};
  /* F2-307fe9af4438 — non-array input is coerced to an empty result. Surface a
     console.warn so a malformed conditions_structured payload is visible rather
     than silently yielding empty conditions. */
  if(items && !Array.isArray(items)){
    console.warn('_reverseConditions: expected an array of condition items, got', typeof items);
  }
  (Array.isArray(items)?items:[]).forEach(function(it){
    if(it.type==='REQUIRE_CHANGE'||it.type==='EXCLUDE_CHANGE'){
      var i=parseInt(String(it.target||'').replace('change_',''),10);
      if(!isNaN(i)) gc.changes.push({idx:i,action:it.type==='REQUIRE_CHANGE'?'REQUIRE':'EXCLUDE'});
    }else if(it.type==='REQUIRE_CRITERION'){
      var c=parseInt(String(it.target||'').replace('criterion_',''),10);
      // FIX-REQUIRED-234 — carry the stable criterion id (from the note text)
      // so restored criteria match by identity, not position.
      if(!isNaN(c)){ var _ct=it.note||''; gc.criteria.push({idx:c,cid:_ct?_critId(String(_ct).substring(0,200)):null,text:_ct}); }
    }else if(it.type==='ADD_CRITERION'){ if(it.note) gc.added_criteria.push(it.note); }
    else if(it.type==='SCOPE_LIMIT'){ gc.scope=String(it.target||'').split(',').filter(Boolean); }
    else if(it.type==='APPROACH_DIRECTIVE'){ gc.directive=it.note||''; }
  });
  return gc;
}
function _rehydrateConditions(gid){
  var _rep=_panelFids(gid)[0];
  var gc=_rep?window._findingConditions[_rep]:null; if(!gc) return;
  (gc.changes||[]).forEach(function(c){
    var r=document.querySelector('input[name="chg-'+gid+'-'+c.idx+'"][value="'+c.action+'"]'); if(r) r.checked=true; });
  var qd=document.getElementById('cpq-'+gid);
  if(qd){ var bx=qd.querySelectorAll('input[type="checkbox"]');
    (gc.criteria||[]).forEach(function(q){
      // FIX-REQUIRED-234 — match by stable id first; fall back to positional
      // index only for legacy state that predates the criterion id.
      var box=q.cid?qd.querySelector('input[data-cid="'+q.cid+'"]'):null;
      if(!box) box=bx[q.idx];
      if(box) box.checked=true;
    }); }
  var ss=document.getElementById('cps-'+gid);
  if(ss) Array.prototype.forEach.call(ss.options,function(o){ o.selected=(gc.scope||[]).indexOf(o.value)>=0; });
  var dd=document.getElementById('cpd-'+gid); if(dd&&gc.directive) dd.value=gc.directive;
  _renderAddedCriteria(gid);
}
/* cycle 64 — render the owner's manually-added criteria, each with a remove
   (✕) control (previously they were static labels with no way to delete).
   Re-renders the whole added list so index closures stay correct after a
   removal. Called by addCriterion, _removeAddedCriterion, and rehydration. */
function _renderAddedCriteria(gid){
  var qd=document.getElementById('cpq-'+gid); if(!qd) return;
  qd.querySelectorAll('.cp-added-crit').forEach(function(n){ n.remove(); });
  var _rep=_panelFids(gid)[0];
  var gc=_rep?window._findingConditions[_rep]:null; if(!gc) return;
  (gc.added_criteria||[]).forEach(function(text,i){
    var row=document.createElement('div'); row.className='cp-added-crit';
    var span=document.createElement('span'); span.textContent='+ '+text; row.appendChild(span);
    var btn=document.createElement('button'); btn.type='button'; btn.className='cp-remove-crit';
    btn.textContent='✕'; btn.title='Remove this criterion';
    btn.addEventListener('click',function(){ _removeAddedCriterion(gid,i); });
    row.appendChild(btn);
    qd.appendChild(row);
  });
}
function _removeAddedCriterion(gid,idx){
  _panelFids(gid).forEach(function(fid){
    var gc=window._findingConditions[fid]; if(gc&&gc.added_criteria) gc.added_criteria.splice(idx,1);
  });
  _renderAddedCriteria(gid); _persistState(); _markDirty();
}
/* FIX-REQUIRED-231 — structured import validation. Rejects non-array payloads
   and entries whose finding_id/decision are missing or whose decision is not a
   recognized value, so a malformed or partially-bad file surfaces a clear
   user-facing message instead of throwing or silently importing garbage. */
var _VALID_DECISIONS={APPROVE:1,APPROVE_WITH_CONDITIONS:1,DEFER:1,EXCLUDE:1};
function _validateImportedDecisions(parsed){
  if(!Array.isArray(parsed)) return {ok:false,error:'expected a JSON array of decisions'};
  var valid=[], skipped=0, invalid=0;
  parsed.forEach(function(d){
    if(!d || typeof d!=='object' || !d.finding_id || !d.decision){ skipped++; return; }
    if(!_VALID_DECISIONS[d.decision]){ invalid++; return; }
    valid.push(d);
  });
  return {ok:true, valid:valid, skipped:skipped, invalid:invalid};
}
function importDecisions(input){
  var file=input.files&&input.files[0]; if(!file) return;
  var msg=document.getElementById('exportMsg');
  function _fail(text){ if(msg){ msg.className='msg warn'; msg.textContent=text; } input.value=''; }
  var reader=new FileReader();
  reader.onerror=function(){ _fail('Import failed: could not read the selected file'); };
  reader.onload=function(){
    var parsed;
    try{ parsed=JSON.parse(reader.result); }
    catch(err){ _fail('Import failed: not valid JSON ('+err.message+')'); return; }
    var res=_validateImportedDecisions(parsed);
    if(!res.ok){ _fail('Import failed: '+res.error); return; }
    if(res.valid.length===0){ _fail('Import failed: no valid decisions found in the file'); return; }
    var n=0;
    res.valid.forEach(function(d){
      window._findingDecisions[d.finding_id]={decision:d.decision,rationale:d.rationale||null}; n++;
      if(d.conditions_structured){ window._findingConditions[d.finding_id]=_reverseConditions(d.conditions_structured); }
    });
    _applyAllDecided(); _persistState(); _markSaved(file.name);
    if(msg){ msg.className='msg';
      var bad=res.skipped+res.invalid;
      var extra=bad?(' ('+bad+' invalid entr'+(bad===1?'y':'ies')+' skipped)'):'';
      msg.textContent='Imported '+n+' decision(s)'+extra; }
    input.value='';
  };
  reader.readAsText(file);
}
/* cycle 64 — save the decisions JSON where the owner chooses (not just
   Downloads). Uses the File System Access API "Save As" when available
   (Chrome/Edge; remembers the folder via id), else falls back to a download.
   Marks the work saved-to-drive on success; stays dirty if the picker is
   cancelled. */
function _downloadBlob(blob, filename){
  var a=document.createElement('a');
  a.href=URL.createObjectURL(blob); a.download=filename;
  document.body.appendChild(a); a.click();
  setTimeout(function(){ URL.revokeObjectURL(a.href); document.body.removeChild(a); }, 0);
}
function _saveExport(blob, filename, count){
  var msg=document.getElementById('exportMsg');
  function done(name){ if(msg){ msg.className='msg'; msg.textContent='Exported '+count+' decision(s) → '+name; } _markSaved(name); }
  function fallback(){ _downloadBlob(blob, filename); done(filename); }
  if(window.showSaveFilePicker){
    var p;
    try{
      p=window.showSaveFilePicker({
        suggestedName:filename, id:'sentinel-decisions',
        types:[{description:'Decisions JSON', accept:{'application/json':['.json']}}]
      });
    }catch(e){ fallback(); return; }
    p.then(function(handle){
      return handle.createWritable().then(function(w){
        return w.write(blob).then(function(){ return w.close(); }).then(function(){ done(handle.name); });
      });
    }).catch(function(err){
      if(err && err.name==='AbortError'){ if(msg){ msg.className='msg'; msg.textContent='Export cancelled — nothing saved'; } return; }
      fallback();  // picker unavailable/blocked (e.g. some file:// contexts) -> download
    });
    return;
  }
  fallback();
}
(function(){
  var _sfd=setFindingDecision;
  setFindingDecision=function(fid,decision){ _sfd(fid,decision); _applyDecidedUI(fid,decision); _persistState(); _markDirty(); var _r=_rowEl(fid); if(_r) _updateGroupRollup(_r.dataset.groupIdSafe); };
  var _sfr=setFindingRationale;
  setFindingRationale=function(fid,val){ _sfr(fid,val); _persistState(); _markDirty(); };
  var _ocp=openConditionsPanel;
  openConditionsPanel=function(gid){ _ocp(gid); _rehydrateConditions(gid); };
  /* exportDecisions delegates its save to _saveExport (below), which marks the
     work saved-to-drive on a real write/download and leaves it dirty on cancel. */
  ['updateChange','updateCriterion','updateScope','updateDirective','addCriterion','clearConditions'].forEach(function(fn){
    var o=window[fn]; if(typeof o==='function') window[fn]=function(){ var r=o.apply(this,arguments); _persistState(); _markDirty(); return r; };
  });
})();
window.addEventListener('beforeunload',function(ev){
  if(window._dirty){ ev.preventDefault(); ev.returnValue=''; return ''; }
});
/* Single delegated event surface (FIX-REQUIRED-060): elements declare their
   behavior via data-act/data-* attributes and are dispatched here, so no inline
   handler ever interpolates a finding id or other data into executable JS. */
function _actTarget(ev){ return ev.target.closest ? ev.target.closest('[data-act]') : null; }
function _fidOf(t){ var row=t.closest ? t.closest('.finding-row') : null; return row?row.dataset.fid:(t.dataset?t.dataset.fid:''); }
document.addEventListener('click',function(ev){
  var t=_actTarget(ev); if(!t) return;
  var act=t.dataset.act, gid=t.dataset.gid;
  if(act==='finding-clear'){ clearFindingDecision(_fidOf(t)); }
  else if(act==='group-decision'){ setGroupDecision(gid,t.dataset.decision); }
  else if(act==='group-toggle-conditions'){ toggleGroupConditions(gid); }
  else if(act==='group-clear'){ clearGroupDecision(gid); }
  else if(act==='add-criterion'){ addCriterion(gid); }
  else if(act==='clear-conditions'){ clearConditions(gid); }
  else if(act==='show-changes'){ showChangesPanel(t); }
  else if(act==='close-changes'){ closeChangesPanel(); }
  else if(act==='toggle-desc'){ toggleDesc(t); }
  else if(act==='toggle-inline-diff'){ toggleInlineDiff(t); }
  else if(act==='jump-to-source'){ jumpToSourceLine(t); }
});
function toggleInlineDiff(btn){
  var row=btn.closest('.finding-row'); if(!row) return;
  var box=row.querySelector('.row-diff'); if(!box) return;
  var expanded=btn.getAttribute('aria-expanded')==='true';
  if(expanded){
    box.hidden=true; btn.setAttribute('aria-expanded','false');
    btn.innerHTML='&#9656; Show illustrative change'; return;
  }
  if(!box.dataset.filled){
    var pdiff=(window._patternDiffs||{})[row.dataset.patternId||''];
    var note=document.createElement('p'); note.className='diff-note';
    note.textContent='Illustrative preview — NOT the governed Phase 2 change. Decide from the finding and acceptance criteria, not this diff.';
    box.appendChild(note);
    if(Array.isArray(pdiff)){
      if(pdiff.length===0){ _appendNoDiff(box); }
      else {
        var _total=pdiff.filter(function(b){return b && !b.note;}).length;
        var _n=0;
        pdiff.forEach(function(blk){
          var isNote=!!(blk&&blk.note);
          var lbl=document.createElement('div'); lbl.className='diff-change-label';
          if(!isNote){ _n++;
            var num=document.createElement('span'); num.className='diff-change-num';
            num.textContent='Change '+_n+' of '+_total; lbl.appendChild(num);
          }
          var txt=document.createElement('span');
          txt.textContent=String((blk&&blk.label)||''); lbl.appendChild(txt);
          box.appendChild(lbl);
          if(isNote) return;  // overflow note: label only, no code block
          if(blk&&blk.diff){ box.appendChild(_renderGhDiff(String(blk.diff))); }
          else { _appendNoDiff(box); }
        });
      }
    } else if(typeof pdiff==='string' && pdiff){  // legacy single-diff briefs
      box.appendChild(_renderGhDiff(pdiff));
    } else { _appendNoDiff(box); }
    box.dataset.filled='1';
  }
  box.hidden=false; btn.setAttribute('aria-expanded','true');
  btn.innerHTML='&#9662; Hide illustrative change';
}
function _renderGhDiff(text){
  // Parse a unified diff into GitHub-style colored rows: existing code shown as
  // context, additions green (+), removals red (-), hunk/file headers muted.
  // Removed lines are clickable and jump to that line in the Current code panel.
  var wrap=document.createElement('div'); wrap.className='gh-diff';
  String(text).split('\n').forEach(function(raw){
    var line=raw.replace(/\r$/,'');
    var cls='gh-ctx', sign='\u00a0', code=line, isDel=false;
    if(/^\+\+\+ /.test(line) || /^--- /.test(line)){ cls='gh-file'; sign='\u00a0'; }
    else if(/^@@/.test(line)){ cls='gh-hunk'; sign='\u00a0'; }
    else if(line.charAt(0)==='+'){ cls='gh-add'; sign='+'; code=line.slice(1); }
    else if(line.charAt(0)==='-'){ cls='gh-del'; sign='-'; code=line.slice(1); isDel=true; }
    else if(line.charAt(0)===' '){ code=line.slice(1); }
    var row=document.createElement('div'); row.className='gh-line '+cls;
    if(isDel){
      row.dataset.act='jump-to-source'; row.dataset.code=code;
      row.title='Jump to this line in the Current code panel';
    }
    var s=document.createElement('span'); s.className='gh-sign'; s.textContent=sign;
    var c=document.createElement('span'); c.className='gh-code'; c.textContent=code;
    row.appendChild(s); row.appendChild(c); wrap.appendChild(row);
  });
  return wrap;
}
function _normCode(s){ return String(s||'').replace(/\s+/g,''); }
function jumpToSourceLine(delRow){
  // Open the Current code panel for this finding, then scroll to + highlight the
  // source line matching the clicked red (removed) diff line.
  var row=delRow.closest ? delRow.closest('.finding-row') : null; if(!row) return;
  openChangesForRow(row);
  var want=_normCode(delRow.dataset.code); if(!want) return;
  var pre=document.getElementById('cp-source-code'); if(!pre) return;
  var lines=pre.querySelectorAll('.cp-src-line');
  var hit=null;
  for(var i=0;i<lines.length;i++){
    lines[i].classList.remove('cp-hit');
    var ln=_normCode(lines[i].textContent);
    if(!hit && ln && (ln===want || ln.indexOf(want)!==-1 || want.indexOf(ln)!==-1)){ hit=lines[i]; }
  }
  if(hit){ hit.classList.add('cp-hit'); hit.scrollIntoView({block:'center'}); }
  else { pre.scrollTop=0; }
}
function _appendNoDiff(box){
  var pre=document.createElement('pre'); pre.className='diff';
  pre.textContent='(no illustrative change available)'; box.appendChild(pre);
}
/* cycle 65 — inline per-finding "Approve with conditions" change list.
   Lazily built on AWC-reveal; scopes the cluster's change items to THIS finding
   by line proximity (fallback: same file, then all), keeping each change's
   original change_N index so Phase-2 merge_conditions is unaffected. */
var _FC_PROX_LINES=40;
function _fcLines(s){
  var m=String(s||'').match(/^(\d+)(?:-(\d+))?$/); if(!m) return null;
  var a=parseInt(m[1],10); return [a, m[2]?parseInt(m[2],10):a];
}
function _fcChangeLines(text,basename){
  if(!basename) return null;
  var esc=basename.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
  var m=String(text).match(new RegExp(esc+':(\\d+)(?:-(\\d+))?'));
  if(!m) return null;
  var a=parseInt(m[1],10); return [a, m[2]?parseInt(m[2],10):a];
}
function _fcNear(a,b){
  if(!a||!b) return false;
  if(a[0]<=b[1] && b[0]<=a[1]) return true;   // overlap
  return Math.min(Math.abs(a[0]-b[1]),Math.abs(b[0]-a[1]))<=_FC_PROX_LINES;
}
function buildInlineConditions(fid){
  var row=document.querySelector('.finding-row[data-fid="'+fid+'"]'); if(!row) return null;
  var box=row.querySelector('.finding-cond'); if(!box) return null;
  if(box.dataset.filled) return box;
  var gid=row.dataset.groupIdSafe||'';
  var card=document.querySelector('.card[data-group-id-safe="'+gid+'"]');
  var pc=card?_parseDataset(card,'proposedChanges'):{ok:false,value:[]};
  var all=(pc.ok?pc.value:[]).map(function(c,i){return {c:c,i:i};});
  var file=row.dataset.file||''; var base=file.split('/').pop();
  var frange=_fcLines(row.dataset.lineRange||'');
  var byFile=all.filter(function(o){ var s=String(o.c); return (file&&s.indexOf(file)>=0)||(base&&s.indexOf(base)>=0); });
  var byProx=byFile.filter(function(o){ return _fcNear(frange,_fcChangeLines(o.c,base)); });
  var visible = box.dataset.forceAll ? all : (byProx.length?byProx:(byFile.length?byFile:all));
  box.innerHTML='';
  var hdr=document.createElement('div'); hdr.className='fc-head';
  hdr.textContent='Conditions for this finding'+
    (visible.length<all.length?(' — showing '+visible.length+' of '+all.length+' cluster change'+(all.length===1?'':'s')):'');
  box.appendChild(hdr);
  var chosen={}; ((window._findingConditions[fid]||{}).changes||[]).forEach(function(c){ chosen[c.idx]=c.action; });
  visible.forEach(function(o,di){
    var c=o.c, i=o.i;
    var rowEl=document.createElement('div'); rowEl.className='cp-change-row';
    var txt=document.createElement('span'); txt.className='cp-change-text';
    txt.textContent=(di+1)+'. '+String(c); rowEl.appendChild(txt);
    [['NONE','None'],['REQUIRE','Require'],['EXCLUDE','Exclude']].forEach(function(pair){
      var action=pair[0];
      var lbl=document.createElement('label'); lbl.className='cp-radio-lbl';
      var inp=document.createElement('input'); inp.type='radio'; inp.name='chg-'+fid+'-'+i; inp.value=action;
      if((chosen[i]||'NONE')===action) inp.checked=true;
      inp.addEventListener('change',function(){ updateFindingChange(fid,i,action); _persistState(); _markDirty(); });
      lbl.appendChild(inp); lbl.appendChild(document.createTextNode(' '+pair[1]));
      rowEl.appendChild(lbl);
    });
    box.appendChild(rowEl);
  });
  if(visible.length<all.length){
    var sa=document.createElement('button'); sa.type='button'; sa.className='fc-showall';
    sa.textContent='Show all '+all.length+' cluster changes';
    sa.addEventListener('click',function(){ box.dataset.forceAll='1'; delete box.dataset.filled; buildInlineConditions(fid); });
    box.appendChild(sa);
  }
  box.dataset.filled='1';
  return box;
}
function revealInlineConditions(fid){ var b=buildInlineConditions(fid); if(b) b.hidden=false; }
function hideInlineConditions(fid){
  var row=document.querySelector('.finding-row[data-fid="'+fid+'"]'); if(!row) return;
  var box=row.querySelector('.finding-cond'); if(box) box.hidden=true;
}
function toggleDesc(btn){
  var span=btn.parentNode.querySelector('.finding-desc-text'); if(!span) return;
  var expanded=btn.getAttribute('aria-expanded')==='true';
  span.textContent = expanded ? span.dataset.short : span.dataset.full;
  btn.textContent = expanded ? 'more' : 'less';
  btn.setAttribute('aria-expanded', expanded ? 'false' : 'true');
}
document.addEventListener('change',function(ev){
  var t=_actTarget(ev); if(!t) return;
  var act=t.dataset.act;
  if(act==='finding-decision'){ setFindingDecision(_fidOf(t),ev.target.value); }
  else if(act==='update-scope'){ updateScope(t.dataset.gid); }
});
document.addEventListener('input',function(ev){
  var t=_actTarget(ev); if(!t) return;
  var act=t.dataset.act;
  if(act==='finding-rationale'){ setFindingRationale(_fidOf(t),ev.target.value); }
  else if(act==='group-rationale'){ setGroupRationale(t.dataset.gid,ev.target.value); }
  else if(act==='update-directive'){ updateDirective(t.dataset.gid,ev.target.value); }
});
document.addEventListener('keydown',function(ev){
  var t=ev.target.closest ? ev.target.closest('[data-act="add-criterion-input"]') : null; if(!t) return;
  if(ev.key==='Enter'){ ev.preventDefault(); addCriterion(t.dataset.gid); }
});
/* Lazy rows: clone a group's <template> into the live DOM the first time its
   findings section is expanded, then re-apply any recorded decisions. 'toggle'
   does not bubble, so listen in the capture phase. */
document.addEventListener('toggle',function(ev){
  var d=ev.target;
  if(!d||!d.classList||!d.classList.contains('findings-section')||!d.open) return;
  var box=d.querySelector('.findings-rows'); var tpl=d.querySelector('template.gr-tpl');
  if(!box||!tpl||box.dataset.filled) return;
  box.appendChild(tpl.content.cloneNode(true)); box.dataset.filled='1';
  _wireInjectedRows(d.dataset.gid||'');
},true);
function _wireInjectedRows(gid){
  if(!gid) return;
  _groupFids(gid).forEach(function(fid){
    var rec=window._findingDecisions[fid]; if(!rec||!rec.decision) return;
    var row=_rowEl(fid); if(!row) return;
    var radio=row.querySelector('input[type="radio"][value="'+rec.decision+'"]');
    if(radio) radio.checked=true;
    var fr=document.getElementById('fr-'+fid); if(fr&&rec.rationale){ fr.hidden=false; fr.value=rec.rationale; }
    _applyDecidedUI(fid,rec.decision);
  });
}
if(document.readyState!=='loading'){ _restoreState(); }
else { document.addEventListener('DOMContentLoaded',_restoreState); }
"""


def _explainer_html(kind: str) -> str:
    e = explainer_for(kind)
    if not e:
        return ""
    esc = html.escape
    p = [f'<details class="explain"><summary>&#8505; What is <span class="mono">{esc(kind)}</span>?</summary><div class="body">']
    p.append(f'<p><span class="lbl">What it is:</span> {esc(e["what"])}</p>')
    p.append(f'<p><span class="lbl">Why it&#39;s risky:</span> {esc(e["why"])}</p>')
    if e.get("problems"):
        p.append('<p class="lbl">What problems it causes:</p><ul>'
                 + "".join(f"<li>{esc(x)}</li>" for x in e["problems"]) + "</ul>")
    p.append(f'<p><span class="lbl">What to check here:</span> {esc(e["check"])}</p>')
    if e.get("risky") or e.get("safer"):
        sk = '<div class="sk">'
        if e.get("risky"):
            sk += f'<div class="risky"><b>Risky</b><pre>{esc(e["risky"])}</pre></div>'
        if e.get("safer"):
            sk += f'<div class="safer"><b>Safer</b><pre>{esc(e["safer"])}</pre></div>'
        p.append(sk + '</div>')
    p.append('</div></details>')
    return "".join(p)


def _cluster_html(kind: str) -> str:
    """Curated 'why these findings cluster' narrative for a finding_kind, or ''.

    Graceful fallback: a kind with no curated cluster text renders nothing.
    """
    text = str((explainer_for(kind) or {}).get("cluster", "")).strip()
    if not text:
        return ""
    return ('<div class="cluster-narrative"><strong>Why these findings cluster:</strong> '
            f'<span>{html.escape(text)}</span></div>')


def _sev_badges(counter: Counter) -> str:
    out = []
    for s in SEV_ORDER:
        if counter.get(s):
            out.append(f'<span class="badge {SEV_CLASS[s]}">{counter[s]} {s}</span>')
    return "".join(out)


# Per-card cap on individually-rendered findings (cycle 48). Rows can carry up
# to ~78 findings; rendering every one with controls would bloat the page.
FINDINGS_CARD_CAP = 50
FINDING_DESC_CHARS = 120


def _consequence_html(f: dict, kind: str, consequences: dict | None, domain: str) -> str:
    """Best/likely/worst-case consequence spectrum for a finding (cycle 49A).
    Prefers the per-finding personalized consequence (cycle 53, stashed as
    ``_personalized_csq``) over the static YAML template; renders nothing when
    neither is available. Under ``--synthesize`` adds a source indicator
    (✦ personalized / ○ template)."""
    personalized = f.get("_personalized_csq")
    template_csq = ((consequences.get(kind) or {}).get(domain) or {}) if consequences else {}
    domain_csq = personalized or template_csq
    if not domain_csq:
        return ""
    e = html.escape
    bc = str(domain_csq.get("best_case", ""))
    lk = str(domain_csq.get("likely", ""))
    wc = str(domain_csq.get("worst_case", ""))
    indicator = ""
    if f.get("_synth_on"):
        if personalized:
            indicator = ('<span class="csq-source csq-personalized" '
                         'title="Personalized by AI for this finding">&#10022;</span>')
        else:
            indicator = ('<span class="csq-source csq-template" '
                         'title="Template consequence">&#9675;</span>')
    return (
        f'<div class="consequences"><div class="csq-label">If not fixed:{indicator}</div>'
        f'<div class="csq-row csq-best"><span class="csq-tag green">Best case</span><span>{e(bc)}</span></div>'
        f'<div class="csq-row csq-likely"><span class="csq-tag orange">Likely</span><span>{e(lk)}</span></div>'
        f'<div class="csq-row csq-worst"><span class="csq-tag red">Worst case</span><span>{e(wc)}</span></div>'
        '</div>'
    )


def _group_id_safe(group_id: str) -> str:
    """Sanitize a group key (``module|kind``) into an HTML-id-safe token (cycle 51):
    every non-alphanumeric char becomes ``_``."""
    return "".join(ch if ch.isalnum() else "_" for ch in group_id)


def _dedup_cap(items, cap: int = 20) -> list[str]:
    """Order-preserving de-duplication of stringified items, capped at ``cap``."""
    out: list[str] = []
    for x in items:
        s = str(x).strip()
        if s and s not in out:
            out.append(s)
        if len(out) >= cap:
            break
    return out


def _ac_list(v) -> list[str]:
    """acceptance_criteria may be a list or a single string; normalize to a list."""
    if isinstance(v, list):
        return [str(x) for x in v if x]
    if isinstance(v, str) and v:
        return [v]
    return []


def _conditions_panel_html(gid: str) -> str:
    """Group-level structured-conditions panel (cycle 51). Hidden until a finding
    in the group selects 'Approve with conditions'; lists/scope populated by JS
    from the card's data attributes."""
    return (
        f'<div class="conditions-panel" id="cp-{gid}" hidden>'
        '<div class="cp-header"><strong>Approve with conditions</strong>'
        f'<span class="cp-subtitle" id="cp-sub-{gid}">Conditions apply to this finding. '
        'They will be enforced by Sentinel&#39;s remediation agent.</span></div>'
        '<div class="cp-section"><div class="cp-section-title">Proposed changes '
        '<span class="cp-hint">(check to require or exclude)</span></div>'
        f'<div class="cp-changes" id="cpc-{gid}"></div></div>'
        '<div class="cp-section"><div class="cp-section-title">Acceptance criteria '
        '<span class="cp-hint">(check to require in QA gate)</span></div>'
        f'<div class="cp-criteria" id="cpq-{gid}"></div>'
        '<div class="cp-add-criterion">'
        f'<input type="text" id="cpac-{gid}" class="cp-add-criterion-input" '
        'placeholder="Add a new acceptance criterion... (press Enter or click Add)" '
        f'data-act="add-criterion-input" data-gid="{gid}">'
        f'<button data-act="add-criterion" data-gid="{gid}">Add</button></div></div>'
        '<div class="cp-section"><label class="cp-section-title">Scope limit '
        '<span class="cp-hint">(restrict edits to these files)</span></label>'
        f'<select multiple id="cps-{gid}" class="cp-scope-select" '
        f'data-act="update-scope" data-gid="{gid}"></select></div>'
        '<div class="cp-section"><label class="cp-section-title">Approach directive '
        '<span class="cp-hint">(advisory context for the remediation agent)</span></label>'
        f'<textarea id="cpd-{gid}" class="cp-directive" '
        'placeholder="e.g. Do not change the public API. Discuss with the team before '
        'modifying the retry logic." '
        f'data-act="update-directive" data-gid="{gid}"></textarea></div>'
        f'<div class="cp-footer"><button data-act="clear-conditions" data-gid="{gid}">'
        'Clear all conditions</button></div>'
        '</div>'
    )


def _group_disposition_html(gid: str, n: int) -> str:
    """Group-level disposition fast path (cycle 63). One click dispositions all
    findings in the group (fanned out across the full group via _fidToGroup, so
    it covers findings beyond the FINDINGS_CARD_CAP render cap too); per-finding
    radios remain as overrides. The rollup span shows the group's state."""
    plural = "s" if n != 1 else ""
    g = html.escape(gid)
    return (
        f'<div class="group-disposition">'
        f'<span class="gd-label">Disposition all {n} finding{plural}:</span>'
        f'<button class="gd-btn gd-approve" data-act="group-decision" data-gid="{g}" data-decision="APPROVE">Approve</button>'
        f'<button class="gd-btn" data-act="group-toggle-conditions" data-gid="{g}">Approve w/ conditions</button>'
        f'<button class="gd-btn" data-act="group-decision" data-gid="{g}" data-decision="DEFER">Defer</button>'
        f'<button class="gd-btn gd-exclude" data-act="group-decision" data-gid="{g}" data-decision="EXCLUDE">Exclude</button>'
        f'<button class="gd-btn gd-clear" data-act="group-clear" data-gid="{g}" '
        f'title="Clear all decisions in this group (back to undecided)">Clear</button>'
        f'<span class="group-rollup" data-gid="{g}"></span>'
        f'<textarea id="gr-{g}" class="group-rationale" hidden '
        f'placeholder="Reason for defer/exclude (optional, applies to all {n} finding{plural})..." '
        f'data-act="group-rationale" data-gid="{g}"></textarea>'
        f'</div>'
    )


def _finding_row_html(f: dict, consequences: dict | None = None, domain: str = "general",
                      group_id_safe: str = "", row_index: int = 0) -> str:
    """One compact finding row inside a card's collapsed findings section, with
    a per-finding triage control + 'View changes' trigger (cycle 48), a
    domain-specific consequence spectrum (cycle 49A), and a group reference for
    the structured-conditions panel (cycle 51)."""
    e = html.escape
    fid = str(f.get("id", "") or "")
    sev = str(f.get("severity", "") or "?")
    sevcls = SEV_CLASS.get(sev, "p3")
    # S4-8 — Stage 5c may have recalibrated this finding's severity. When it
    # differs from the original, render BOTH badges (original struck-through +
    # recalibrated active) with a tooltip; otherwise the single badge below.
    recal = str(f.get("recalibrated_severity", "") or "")
    recalibrated = bool(recal) and recal != sev
    if recalibrated:
        recal_cls = SEV_CLASS.get(recal, "p3")
        recal_title = (
            f"Severity recalibrated from {sev} to {recal} by Stage 5c verification"
        )
        sev_badge_html = (
            f'<span class="finding-sev-recal" title="{e(recal_title, quote=True)}">'
            f'<span class="finding-sev badge {sevcls} sev-orig">{e(sev)}</span>'
            f'<span class="finding-sev badge {recal_cls}">{e(recal)}</span>'
            f'</span>'
        )
    else:
        sev_badge_html = f'<span class="finding-sev badge {sevcls}">{e(sev)}</span>'
    file = str(f.get("file", "") or "")
    loc = str(f.get("location", "") or "")
    finding_kind = str(f.get("finding_kind", "") or "")
    desc = " ".join(str(f.get("description", "") or "").split())
    desc_truncated = len(desc) > FINDING_DESC_CHARS
    desc_short = desc[:FINDING_DESC_CHARS] + ("…" if desc_truncated else "")
    skill = str(f.get("skill", "") or "")
    changes_attr = e(json.dumps(f.get("_group_changes") or []), quote=True)
    src_attr = e(str(f.get("_source") or ""), quote=True)
    vs_attr = e(str(f.get("_vscode") or ""), quote=True)
    gh_attr = e(str(f.get("_github") or ""), quote=True)
    line_num = f.get("_resolved_line")
    loc_text = loc or file or "(no location)"
    loc_display = loc_text
    if line_num and ":" in loc:
        loc_display = f"{loc}:L{line_num}"
    if f.get("_vscode"):
        loc_html = (f'<a href="{vs_attr}" class="vscode-link mono" '
                    f'title="Open in VS Code line {line_num or "?"}">{e(loc_display)}</a>')
    else:
        loc_html = f'<span class="finding-loc mono">{e(loc_display)}</span>'
    # cycle 61 — primary "open in context" link for the code owner.
    if f.get("_github"):
        loc_html += (f'<a href="{gh_attr}" class="gh-link mono" target="_blank" '
                     f'title="View on GitHub (pinned to assessed commit)">GitHub&#8599;</a>')
    # Radio group name must be unique per row and must not depend on a missing
    # or non-unique finding id (FIX-REQUIRED-311). group_id_safe + row_index is
    # guaranteed unique across the page regardless of the finding id.
    name = f"ft-{group_id_safe}-{row_index}"
    # cycle 62 — closure indicator. Sense 2: a prior persisted decision
    # (tech_debt human_triage) renders the row already-decided (dimmed badge +
    # pre-checked radio); sense 1 (live session decisions) is applied by JS.
    # data-triaged lets the restore JS seed session state on load.
    triaged = str(f.get("_human_triage") or "")
    # Only DEFER/EXCLUDE are "decided" from the CODE OWNER's perspective. A
    # Sentinel-triage APPROVE means "in scope for the code owner to decide", so
    # it renders UNDECIDED (active radios, visible under 'Hide decided').
    code_owner_decided = triaged in ("DEFER", "EXCLUDE")
    seed = triaged if code_owner_decided else ""

    def _ck(v: str) -> str:
        return " checked" if seed == v else ""

    row_cls = "finding-row decided" if code_owner_decided else "finding-row"
    # Inline illustrative-diff affordance: only when this finding's pattern has a
    # generated diff (set on the finding as _has_diff). The diff text itself is
    # pulled from window._patternDiffs on expand (no per-row duplication).
    diff_toggle = (
        '<button type="button" class="row-diff-toggle" data-act="toggle-inline-diff" '
        'aria-expanded="false">&#9656; Show illustrative change</button>'
        '<div class="row-diff" hidden></div>'
    ) if f.get("_has_diff") else ""
    # cycle 65 — inline per-finding "Approve with conditions" change list. Empty
    # container, revealed + lazily populated by JS when this finding's AWC radio
    # is selected (only rendered when the finding's pattern has proposed changes).
    # data-line-range lets the JS scope the change items to this finding by line
    # proximity without parsing the location prose.
    line_range = str(f.get("line_range", "") or "")
    cond_container = (
        '<div class="finding-cond" hidden></div>' if f.get("_group_changes") else ""
    )
    return (
        f'<div class="{row_cls}" data-fid="{e(fid)}" data-file="{e(file)}" '
        f'data-finding-kind="{e(finding_kind)}" '
        f'data-alarm="{_finding_alarm_tier(f)}" '
        f'data-location="{e(loc)}" data-changes="{changes_attr}" '
        f'data-source="{src_attr}" data-vscode="{vs_attr}" data-github="{gh_attr}" '
        f'data-triaged="{e(seed)}" '
        f'data-pattern-id="{e(str(f.get("_pattern_id", "") or ""))}" '
        f'data-line-range="{e(line_range)}" '
        f'data-group-id-safe="{e(group_id_safe)}">'
        f'<span class="finding-id">{e(fid)}</span>'
        f'{sev_badge_html}'
        f'<span class="decided-badge"></span>'
        f'{loc_html}'
        f'<button class="view-changes-btn" data-act="show-changes">View changes</button>'
        f'<span class="finding-desc">'
        f'<span class="finding-desc-text" data-short="{e(desc_short, quote=True)}" '
        f'data-full="{e(desc, quote=True)}">{e(desc_short)}</span>'
        + (' <button type="button" class="desc-more" data-act="toggle-desc" '
           'aria-expanded="false">more</button>' if desc_truncated else "")
        + (f' <span class="mono">[{e(skill)}]</span>' if skill else "")
        + '</span>'
        + diff_toggle
        + _consequence_html(f, str(f.get("finding_kind", "") or ""), consequences, domain)
        + f'<div class="finding-triage">'
        f'<label><input type="radio" name="{e(name)}" value="APPROVE"{_ck("APPROVE")} '
        f'data-act="finding-decision"> Approve</label>'
        f'<label><input type="radio" name="{e(name)}" value="APPROVE_WITH_CONDITIONS"{_ck("APPROVE_WITH_CONDITIONS")} '
        f'data-act="finding-decision"> Approve with conditions</label>'
        f'<label><input type="radio" name="{e(name)}" value="DEFER"{_ck("DEFER")} '
        f'data-act="finding-decision"> Defer</label>'
        f'<label><input type="radio" name="{e(name)}" value="EXCLUDE"{_ck("EXCLUDE")} '
        f'data-act="finding-decision"> Exclude</label>'
        f'<textarea id="fr-{e(fid)}" class="finding-rationale" placeholder="Reason (optional)..." hidden '
        f'data-act="finding-rationale"></textarea>'
        f'<button type="button" class="ft-clear" data-act="finding-clear" '
        f'title="Clear this decision (back to undecided)">Clear</button>'
        f'</div>'
        + cond_container
        + f'</div>'
    )


def _findings_section_html(r: dict, consequences: dict | None = None, domain: str = "general",
                           group_id_safe: str = "") -> str:
    """Collapsed <details> listing the row's individual findings (cycle 48).
    Summary shows the count, severity breakdown, and up to 3 distinct files."""
    e = html.escape
    findings = r["findings"]
    n = len(findings)
    sev = r["sev"]
    sev_part = " · ".join(f"{s} ({sev[s]})" for s in SEV_ORDER if sev.get(s))
    files: list[str] = []
    for f in findings:
        fp = f.get("file")
        if fp and fp not in files:
            files.append(fp)
        if len(files) >= 3:
            break
    summary = f"{n} finding{'s' if n != 1 else ''} — {sev_part}"
    if files:
        summary += " · " + ", ".join(files)
    rows = "".join(_finding_row_html(f, consequences, domain, group_id_safe, i)
                   for i, f in enumerate(findings[:FINDINGS_CARD_CAP]))
    more = ""
    if n > FINDINGS_CARD_CAP:
        more = f'<p class="meta">…and {n - FINDINGS_CARD_CAP} more findings not shown.</p>'
    # Lazy render: rows ship inside a <template> and are cloned into .findings-rows
    # on first expand, so a large brief doesn't build/lay out every row at load.
    return (f'<details class="findings-section" data-gid="{e(group_id_safe)}">'
            f'<summary>{e(summary)}</summary>'
            f'<div class="findings-rows"></div>'
            f'<template class="gr-tpl">{rows}</template>'
            f'{more}</details>')


# Right-side before/after panel (cycle 48). Emitted once near </body>; shown via
# showChangesPanel(). Fixed-position, so DOM placement does not matter.
_CHANGES_PANEL = (
    '<div id="changes-panel" hidden>'
    '<div id="cp-header"><span id="cp-title"></span>'
    '<button id="cp-close" data-act="close-changes" '
    'style="font-size:1.2rem;cursor:pointer;border:none;background:none;padding:0.25rem 0.5rem;">&#10005; Close</button></div>'
    '<div id="cp-current"><h4>Current code</h4>'
    '<a id="cp-github" href="#" target="_blank" hidden>View on GitHub &#8594;</a>'
    '<a id="cp-vscode" href="#" target="_blank" hidden>Open in VS Code &#8594;</a>'
    '<pre id="cp-source-code" style="font-size:0.8rem;overflow-x:auto;white-space:pre-wrap;'
    'background:#f8f9fa;padding:0.75rem;border-radius:4px;"></pre></div>'
    '<div id="cp-proposed"><h4>Proposed fix for this group</h4><div id="cp-changes"></div></div>'
    '</div>'
)


def _tier_rank(r) -> int:
    """Highest-severity tier present in a rollup row (Critical=0 … P3=3, none=4),
    for ordering group cards by priority (cycle 49A)."""
    sev = r["sev"]
    for idx, s in enumerate(SEV_ORDER):
        if sev.get(s):
            return idx
    return len(SEV_ORDER)


# --------------------------------------------------------------- Bundles (S2-2)
_BUNDLE_STATUS_CLASS = {
    "UNDECIDED": "bstatus-undecided",
    "APPROVED": "bstatus-approved",
    "IN_PROGRESS": "bstatus-inprogress",
    "IN_REVIEW": "bstatus-inreview",
    "COMPLETE": "bstatus-complete",
    "REVERTED": "bstatus-reverted",
}


def _bundle_section_html(bundles: list[dict]) -> str:
    """Render the remediation-bundle grouping layer that sits ABOVE the
    module+finding_kind group cards (S2-2, STEP 6).

    Each bundle renders a header (severity badge + root_cause_title;
    "Bundle N · {confidence} · {N} findings"; APPROVE BUNDLE / DEFER BUNDLE /
    ▼ Show cascade), a status badge, and its cascade members collapsed by
    default (root cause greyed, no EXCLUDE; other members carry an EXCLUDE
    button). Returns "" when there are no bundles."""
    e = html.escape
    if not bundles:
        return ""
    parts = [
        '<section class="bundles" id="bundles">',
        f'<h2>Remediation bundles — {len(bundles)} root-cause groups</h2>',
        '<p class="note">Each bundle anchors on a <b>root cause</b>; fixing it is '
        'predicted to resolve the cascade below. Approve or defer the whole bundle, '
        'or exclude an individual cascade member (excluded members are deferred).</p>',
    ]
    total = len(bundles)
    for b in bundles:
        try:
            bid = str(b.get("bundle_id", "") or "")
            number = _bundle_number_from_id(bid)
            confidence = str(b.get("confidence", "") or "")
            root_title = str(b.get("root_cause_title", "") or "")
            root_sev = str(b.get("root_cause_severity", "") or "")
            root_fid = str(b.get("root_cause_finding_id", "") or "")
            status = str(b.get("bundle_status", "UNDECIDED") or "UNDECIDED")
            members = b.get("member_findings", []) or []
        except Exception:  # pragma: no cover - defensive against malformed data
            continue

        sev_badge = (
            f'<span class="badge {SEV_CLASS.get(root_sev, "p3")}">{e(root_sev)}</span>'
            if root_sev else ""
        )
        status_cls = _BUNDLE_STATUS_CLASS.get(status, "bstatus-undecided")
        status_badge = f'<span class="bstatus {status_cls}">{e(status)}</span>'

        rows = []
        for m in members:
            mfid = str(m.get("finding_id", "") or "")
            is_root = mfid == root_fid
            msev = str(m.get("severity", "") or "")
            mtitle = str(m.get("title", "") or "")
            predicted = m.get("predicted_resolved")
            pred_tag = (
                '<span class="pred yes">will resolve</span>'
                if predicted
                else '<span class="pred no">predicted only</span>'
            )
            if is_root:
                exclude = '<span class="root-tag">root cause</span>'
                row_cls = "bmember root"
            else:
                exclude = (
                    f'<button class="bexclude" data-act="bundle-exclude" '
                    f'data-bundle="{e(bid)}" data-fid="{e(mfid)}">EXCLUDE</button>'
                )
                row_cls = "bmember"
            rows.append(
                f'<div class="{row_cls}" data-fid="{e(mfid)}">'
                f'<span class="badge {SEV_CLASS.get(msev, "p3")}">{e(msev)}</span> '
                f'<span class="bmtitle">{e(mtitle)}</span> '
                f'<span class="mono bmfid">{e(mfid)}</span> {pred_tag} {exclude}'
                f'</div>'
            )

        # Part A′ — illustrative diff, collapsed, only when --preview-diffs
        # generated one. Rendered as plain <pre> text (NOT a data-* attribute),
        # so it never enters the decisions-export payload. Persistent disclaimer.
        diff_text = str(b.get("_illustrative_diff", "") or "")
        if diff_text:
            from sentinel.reports.diff_preview import DISCLAIMER
            diff_html = (
                '<details class="bundle-diff"><summary>&#9654; Show exact change '
                '(illustrative)</summary>'
                f'<p class="diff-note">{e(DISCLAIMER)}</p>'
                f'<pre class="diff">{e(diff_text)}</pre></details>'
            )
        else:
            diff_html = ""

        parts.append(
            f'<div class="bundle {SEV_CLASS.get(root_sev, "p3")}" '
            f'data-bundle-id="{e(bid)}" data-bundle-status="{e(status)}">'
            f'<div class="bundle-head">'
            f'{sev_badge} <span class="broot">{e(root_title)}</span> {status_badge}'
            f'<div class="bmeta">Bundle {number} of {total} · '
            f'<b>{e(confidence)}</b> · {len(members)} finding'
            f'{"s" if len(members) != 1 else ""}</div>'
            f'<div class="bactions">'
            f'<button class="bbtn bapprove" data-act="bundle-decision" '
            f'data-bundle="{e(bid)}" data-decision="APPROVE">APPROVE BUNDLE</button>'
            f'<button class="bbtn bdefer" data-act="bundle-decision" '
            f'data-bundle="{e(bid)}" data-decision="DEFER">DEFER BUNDLE</button>'
            f'</div>'
            f'</div>'
            f'<details class="bundle-cascade"><summary>&#9660; Show cascade '
            f'({len(members)})</summary>{"".join(rows)}</details>'
            f'{diff_html}'
            f'</div>'
        )
    parts.append('</section>')
    return "".join(parts)


def _bundle_number_from_id(bundle_id: str) -> int:
    try:
        return int(str(bundle_id).rsplit("-", 1)[-1])
    except (ValueError, IndexError):
        return 0


def _pattern_group_html(i: int, pg: dict, consequences=None, domain="general") -> str:
    """Render a collapsible pattern-group card (S3-4).

    Header: ``Pattern: {title} ({N} instances across {M} modules)`` with a
    severity badge and APPROVE / DEFER PATTERN buttons. Approving the pattern
    dispositions every member finding — the buttons carry ``data-pattern-id``
    and the member finding ids so the decisions-export can pick up the
    pattern-level intent. Body (collapsed) lists the affected modules and each
    member row's findings.
    """
    e = html.escape
    pid = str(pg.get("pattern_id", "") or "")
    title = str(pg.get("pattern_title", "") or "")
    modules = pg.get("affected_modules") or []
    n_inst = int(pg.get("instance_count", 0) or 0)
    n_mods = len(modules)
    sev = str(pg.get("severity", "") or "?")
    sevcls = SEV_CLASS.get(sev, "p3")
    member_fids = [x for x in (pg.get("member_finding_ids") or []) if x]
    fids_attr = e(json.dumps(member_fids), quote=True)
    pid_safe = _group_id_safe(f"pattern:{pid}")
    header = (f'Pattern: {e(title)} ({n_inst} instances across {n_mods} modules)'
              if title else
              f'Pattern {e(pid)} ({n_inst} instances across {n_mods} modules)')
    modules_html = ", ".join(f'<span class="mono">{e(m)}</span>' for m in modules)
    members_html = "".join(
        _findings_section_html(mr, consequences, domain, pid_safe)
        for mr in pg.get("member_rows") or []
    )
    return (
        f'<details class="card pattern-group {sevcls}" open id="patgroup{i}" '
        f'data-alarm="{_row_alarm_tier(pg)}" '
        f'data-pattern-id="{e(pid)}" data-pattern-id-safe="{pid_safe}" '
        f'data-member-finding-ids="{fids_attr}">'
        f'<summary class="pattern-group-summary">'
        f'<span class="badge {sevcls}">{e(sev)}</span> '
        f'<b>{header}</b>'
        f'</summary>'
        f'<div class="pattern-group-actions">'
        f'<button class="pg-btn pg-approve" data-act="pattern-approve" '
        f'data-pattern-id="{e(pid)}" data-member-finding-ids="{fids_attr}" '
        f'data-decision="APPROVE">APPROVE PATTERN</button>'
        f'<button class="pg-btn pg-defer" data-act="pattern-defer" '
        f'data-pattern-id="{e(pid)}" data-member-finding-ids="{fids_attr}" '
        f'data-decision="DEFER">DEFER PATTERN</button>'
        f'</div>'
        f'<p class="meta"><b>Affected modules ({n_mods}):</b> {modules_html}</p>'
        f'{members_html}'
        f'</details>'
    )


def emit_html(ordered, total, sev_tot, out_html: Path, run_label: str, syntheses=None,
              consequences=None, domain="general", fid2td=None, repo_root="", fid2group=None,
              bundles=None, vscode_links=True, pattern_diffs=None):
    e = html.escape
    # Group cards ordered by severity tier (Critical → P3), then finding count desc.
    cards = sorted(ordered, key=lambda kv: (_tier_rank(kv[1]), -len(kv[1]["findings"])))
    P = [f'<!doctype html><html><head><meta charset="utf-8">',
         f'<title>Remediation Decision Brief — {e(run_label)}</title>',
         f'<style>{_CSS}</style></head><body><div class="wrap">',
         '<div class="toolbar" id="toolbar">'
         f'<span class="brand">{e(run_label)}</span>'
         '<span class="prog"><b id="progCount">0</b> of '
         f'<b id="progTotal">{total}</b> findings decided</span>'
         '<button id="exportBtn" onclick="exportDecisions()" '
         'title="Download the decisions JSON and save it on the shared drive — this is the durable save">'
         '&#11015; Export &#8594; save to drive</button>'
         '<button id="importBtn" onclick="document.getElementById(\'importFile\').click()" '
         'title="Resume from a decisions JSON saved on the drive">&#11014; Import</button>'
         '<input type="file" id="importFile" accept="application/json" style="display:none" '
         'onchange="importDecisions(this)">'
         '<label class="toggle"><input type="checkbox" id="hideDecided" '
         'onchange="document.body.classList.toggle(\'hide-decided\',this.checked)"> '
         'Hide decided</label>'
         '<span class="save-state" id="saveState"></span>'
         '<span class="msg" id="exportMsg"></span></div>',
         f'<h1>Remediation Decision Brief — <span class="mono">{e(run_label)}</span></h1>',
         # S4-10 — link to the one-page operator handoff document (rendered
         # alongside this brief in the same state dir; served at /handoff.html).
         '<p><a href="handoff.html" class="handoff-link">&#128196; Assessment Handoff Document</a></p>',
         f'<p class="sub"><b>{total} findings → {len(ordered)} groups</b> '
         f'(grouped by <span class="mono">module + finding_kind</span>) &nbsp; {_sev_badges(sev_tot)}</p>',
         '<p class="note">Triage each <b>finding</b> individually: <b>Approve</b>, '
         '<b>Approve with conditions</b>, <b>Defer</b>, or <b>Exclude</b>. Approved findings execute '
         'under Phase-2 gates (test-first → QA → docs). Groups are ordered by highest severity first; '
         'expand a group to triage its findings.</p>']
    # Stage 5c-alarm — alarm focus banner + filter. References the same cards
    # (via data-alarm); does not duplicate or drop any group.
    _alarm_tot = _alarm_totals(ordered)
    _alarm_rows = alarm_focus(ordered)
    # Only surface the alarm banner + filter when there ARE alarm-relevant
    # findings; a codebase with no alarms shows no alarm UI at all.
    if _alarm_rows:
        P.append(
            '<p class="note alarm-note"><b>Alarm focus:</b> '
            f'{_alarm_tot.get("direct", 0)} direct · {_alarm_tot.get("adjacent", 0)} adjacent '
            f'alarm-relevant finding(s) across {len(_alarm_rows)} group(s). '
            '<label class="toggle"><input type="checkbox" id="alarmOnly" '
            'onchange="document.body.classList.toggle(\'alarm-only\',this.checked)"> '
            'Show alarm-relevant only</label></p>'
        )
    # S2-2 — bundle grouping layer above the module+finding_kind cards. Only
    # rendered when bundles.json existed (bundles passed in); otherwise the flat
    # group list below is unchanged (backward compatible).
    if bundles:
        P.append(_bundle_section_html(bundles))
    P.append('<h2>Groups — highest severity first</h2>')
    for i, ((mod, kind), r) in enumerate(cards, 1):
        # S3-4 — a pattern group collapses many (module, finding_kind) rows that
        # share one remediation pattern into a single collapsible decision.
        if isinstance(r, dict) and r.get("is_pattern_group"):
            P.append(_pattern_group_html(i, r, consequences, domain))
            continue
        top = next((s for s in SEV_ORDER if r["sev"].get(s)), "p3")
        files = sorted(r["files"])
        visible_files = ", ".join(f'<span class="mono">{e(x)}</span>' for x in files[:8]) or "<i>(no file attributed)</i>"
        files_more = ""
        if len(files) > 8:
            rest_files = ", ".join(f'<span class="mono">{e(x)}</span>' for x in files[8:])
            files_more = (f'<details class="more-files"><summary>+{len(files)-8} more files</summary>'
                          f'<p class="meta">{rest_files}</p></details>')
        # cycle 51 — group key + structured-conditions data for the group panel
        group_id = f"{mod}|{kind}"
        gid_safe = _group_id_safe(group_id)
        pats_list = list(r["pats"].values())
        proposed = _dedup_cap(c for p in pats_list for c in (p.get("proposed_changes") or []))
        criteria = _dedup_cap(c for p in pats_list for c in _ac_list(p.get("acceptance_criteria")))
        pc_attr = e(json.dumps(proposed), quote=True)
        ac_attr = e(json.dumps(criteria), quote=True)
        gf_attr = e(json.dumps(files), quote=True)
        # cycle 52 — explicit idx→text so the export carries the actual change/
        # criterion text in `note`, not just the positional target.
        ci_attr = e(json.dumps([{"idx": j, "text": t} for j, t in enumerate(proposed)]), quote=True)
        cri_attr = e(json.dumps([{"idx": j, "text": t} for j, t in enumerate(criteria)]), quote=True)
        P.append(
            f'<div class="card {SEV_CLASS.get(top,"p3")}" id="row{i}" '
            f'data-alarm="{_row_alarm_tier(r)}" '
            f'data-group-id="{e(group_id)}" data-group-id-safe="{gid_safe}" '
            f'data-proposed-changes="{pc_attr}" data-acceptance-criteria="{ac_attr}" '
            f'data-group-files="{gf_attr}" '
            f'data-change-index="{ci_attr}" data-criterion-index="{cri_attr}">'
            f'<h3>{i}. <span class="mono">{e(mod)}</span> · <span class="mono">{e(kind)}</span></h3>'
            f'<p class="meta"><b>{len(r["findings"])} findings</b> &nbsp; {_sev_badges(r["sev"])}</p>'
            + _group_disposition_html(gid_safe, len(r["findings"]))
            + _cluster_html(kind)
            + _conditions_panel_html(gid_safe)
            + _synthesis_html(kind, mod, syntheses)
            + _explainer_html(kind)
            + f'<p class="meta"><b>Blast radius:</b> {len(files)} file(s) / {len(r["findings"])} sites — {visible_files}</p>'
            + files_more
            + _findings_section_html(r, consequences, domain, gid_safe)
        )
        P.append('</div>')
    P.append(f'<script>window._totalFindings={total};</script>')
    P.append('<script>window._fidToTd=' + json.dumps(fid2td or {}) + ';</script>')
    P.append('<script>window._fidToGroup=' + json.dumps(fid2group or {}) + ';</script>')
    # Prior human triage, emitted as a map so _restoreState can seed decisions
    # without reading the (now lazily-rendered) finding rows from the live DOM.
    _fid_triaged = {}
    for _k, _r in ordered:
        for _f in _r["findings"]:
            _t = str(_f.get("_human_triage") or "")
            # Only DEFER/EXCLUDE are "decided" for the code owner; a Sentinel
            # APPROVE is left unseeded so _restoreState keeps it undecided
            # (mirrors _finding_row_html). Otherwise 'Hide decided' would hide
            # the very findings the code owner must decide on.
            if _t in ("DEFER", "EXCLUDE") and _f.get("id"):
                _fid_triaged[str(_f["id"])] = _t
    P.append('<script>window._fidTriaged=' + json.dumps(_fid_triaged) + ';</script>')
    # Per-finding file/kind/location so the export can build a DEFER suppression
    # rule without reading the (lazily-rendered) finding row from the live DOM.
    _fid_meta = {}
    for _k, _r in ordered:
        for _f in _r["findings"]:
            if _f.get("id"):
                _fid_meta[str(_f["id"])] = {
                    "file": str(_f.get("file") or ""),
                    "finding_kind": str(_f.get("finding_kind") or ""),
                    "location": str(_f.get("location") or ""),
                }
    P.append('<script>window._fidMeta=' + json.dumps(_fid_meta) + ';</script>')
    # A′ triage-time diffs: {pattern_id: illustrative_diff}, looked up by the
    # 'View changes' panel via each row's data-pattern-id. Not a data-* attribute
    # and not read by exportDecisions, so it never enters the decisions payload.
    P.append('<script>window._patternDiffs=' + json.dumps(pattern_diffs or {}) + ';</script>')
    P.append('<script>window._repoRoot=' + json.dumps(str(repo_root or "")) + ';</script>')
    P.append('<script>window._runKey=' + json.dumps("sentinel-brief:" + str(run_label)) + ';</script>')
    P.append('<script>' + _JS + '</script>')
    P.append('<script>' + _JS_STATE + '</script>')
    # Drop the "Open in VS Code" panel link when vscode links are suppressed —
    # its #cp-vscode CSS rule overrides the `hidden` attribute, so leaving it in
    # would show a dead href="#" link (reloads the page on click).
    panel = _CHANGES_PANEL if vscode_links else _CHANGES_PANEL.replace(
        '<a id="cp-vscode" href="#" target="_blank" hidden>'
        'Open in VS Code &#8594;</a>', ''
    )
    P.append('</div>' + panel + '</body></html>')
    out_html.write_text("".join(P), encoding="utf-8")


def _attach_bundle_diffs(bundles, all_findings, state_dir):
    """Part A′ — generate + attach an illustrative diff per bundle (``--preview-diffs``).

    Builds one unit per bundle from its members' cited source (``_source``) and
    proposed changes (``_group_changes``), then delegates to
    ``diff_preview.generate_all`` (cache-first, source neutralized there, graceful
    degradation). Attaches ``_illustrative_diff`` onto the bundle dict for the
    renderer. Any client/init failure degrades to no diffs — the brief still
    renders. Nothing here writes to the exported decisions payload.
    """
    from sentinel.reports import diff_preview

    client = None
    try:
        from sentinel.api.client import create_model_client
        from sentinel.config import load_config
        client = create_model_client(load_config())
    except Exception as exc:  # noqa: BLE001 — degrade visibly to no diffs
        log.warning(
            "--preview-diffs requested but LLM client init failed; "
            "skipping diff previews: %s: %s", type(exc).__name__, exc,
        )
        return

    fid2f = {f["id"]: f for f in all_findings if f.get("id")}
    units = []
    for b in bundles:
        srcs, changes, files = [], [], []
        seen_src = set()
        for m in b.get("member_findings") or []:
            mf = fid2f.get(str(m.get("finding_id", "") or ""))
            if not mf:
                continue
            s = mf.get("_source")
            if s and s not in seen_src:
                seen_src.add(s)
                srcs.append(s)
            for c in mf.get("_group_changes") or []:
                if c not in changes:
                    changes.append(c)
            mfile = mf.get("file")
            if mfile and mfile not in files:
                files.append(mfile)
        units.append({
            "id": str(b.get("bundle_id", "") or ""),
            "title": str(b.get("root_cause_title", "") or ""),
            "affected_files": (b.get("affected_files") or files),
            "proposed_changes": changes,
            "source": "\n\n".join(srcs),
        })

    diffs = diff_preview.generate_all(units, state_dir, client)
    for b in bundles:
        d = diffs.get(str(b.get("bundle_id", "") or ""))
        if d:
            b["_illustrative_diff"] = d


_DIFF_WIDE_CONTEXT_LINES = 100
_DIFF_WIDE_MAX_LINES = 200
# Tight window first: ground the illustrative "current code" at the finding's own
# cited lines so it can't be lifted from a neighbouring method. Widen only when
# the tight window yields no grounded diff.
_DIFF_TIGHT_CONTEXT_LINES = 8
_DIFF_TIGHT_MAX_LINES = 45
# Right-panel "Current code" window — wide enough to contain a tight-grounded
# diff's removed lines so a clicked red line can be located and highlighted.
_DIFF_SOURCE_CONTEXT_LINES = 15
_DIFF_SOURCE_MAX_LINES = 90
# Cap illustrative diff blocks per pattern; extra changes get an overflow note.
_DIFF_MAX_BLOCKS_PER_PATTERN = 5


def _pattern_diff_blocks(pid, changes, gen_map, cap=_DIFF_MAX_BLOCKS_PER_PATTERN):
    """Assemble a pattern's ordered illustrative-diff blocks for the brief.

    Returns ``[{"label": <change text>, "diff": <str|None>}, ...]`` for up to
    ``cap`` proposed changes (looked up in ``gen_map`` by ``"{pid}#{idx}"``),
    followed by a single ``{"label": ..., "note": True}`` entry when more changes
    exist than the cap. Pure/testable — no I/O, no LLM."""
    blocks = []
    capped = list(changes)[:cap]
    for idx, change in enumerate(capped):
        blocks.append({"label": str(change), "diff": gen_map.get(f"{pid}#{idx}")})
    extra = len(changes) - len(capped)
    if extra > 0:
        blocks.append({
            "label": f"{extra} more proposed change{'' if extra == 1 else 's'} not previewed",
            "note": True,
        })
    return blocks


def _attach_pattern_diffs(all_findings, state_dir, repo_root):
    """Part A′ (triage-time path) — one illustrative diff per PROPOSED CHANGE.

    Bundles only exist AFTER triage, so the bundle diff can't appear on the
    triage brief where the code owner actually decides. This anchors diffs on the
    remediation pattern's individual ``proposed_changes`` (Phase-1 output present
    at triage): findings are grouped by ``_pattern_id``, and one diff unit is
    built per ``(pattern, change_index)`` (id ``"{pid}#{idx}"``, capped at
    ``_DIFF_MAX_BLOCKS_PER_PATTERN``) from the members' cited source + that single
    change. The result is returned as ``{pattern_id: [{label, diff|None}, ...]}``
    for the per-finding inline diff to look up (labelled by the change it
    supports). Same guarantees as the bundle path (cached, source neutralized in
    diff_preview, illustrative, kept out of the decisions export). Any client
    failure degrades to an empty map — the brief still renders.

    Source width: a per-change unit that ALREADY has a cached diff is reused
    as-is (sticky, never regenerated). Units with no cached diff are fed a WIDER
    window (``read_finding_source`` with more context) plus a named-construct
    window, because a decline is often finding-line-number imprecision — the
    relevant code sits just outside the narrow window (e.g. an FTP credential
    ~50 lines below the cited line). This keeps the re-render bounded.
    """
    from sentinel.reports import diff_preview

    # UI-only re-render: reuse the cached diffs, make no LLM calls (declines are
    # not cached, so a normal render re-attempts them every time — slow). Cached
    # diffs are still reused per-unit only on an input-hash match, so this stays
    # correct; uncached units simply show no illustrative change.
    reuse_only = os.getenv(
        "SENTINEL_DIFF_REUSE_CACHE_ONLY", "").strip().lower() in ("1", "true", "yes")
    client = None
    if not reuse_only:
        try:
            from sentinel.api.client import create_model_client
            from sentinel.config import load_config
            client = create_model_client(load_config())
        except Exception as exc:  # noqa: BLE001 — degrade visibly to no diffs
            log.warning(
                "--preview-diffs requested but LLM client init failed; "
                "skipping diff previews: %s: %s", type(exc).__name__, exc,
            )
            return {}

    # Start empty: every previewed change is (re)built each render and handed to
    # ``generate_all``, whose per-unit input-hash check reuses a cached diff ONLY
    # when the CURRENT source matches — so a change regenerates (and re-grounds)
    # whenever its source window changes. This deliberately does NOT reuse a diff
    # by unit-id alone: an older wide-window diff that grounded in a neighbouring
    # method must not survive the tight-first pass.
    gen: dict[str, str] = {}

    by_pat: dict[str, list] = {}
    for f in all_findings:
        pid = str(f.get("_pattern_id", "") or "")
        if pid:
            by_pat.setdefault(pid, []).append(f)

    pattern_changes: dict[str, list] = {
        pid: (findings[0].get("_group_changes") or [])
        for pid, findings in by_pat.items()
    }

    def _build_units(tight: bool) -> list[dict]:
        """One diff unit per still-ungenerated ``(pattern, change)``. ``tight``
        grounds the source in a small window at the finding's cited lines so the
        illustrative "current code" comes from the right method; the wide fallback
        casts a larger net (+ named-construct window) only for changes the tight
        pass left ungrounded."""
        if tight:
            ctx, mx, use_symbol = (
                _DIFF_TIGHT_CONTEXT_LINES, _DIFF_TIGHT_MAX_LINES, False)
        else:
            ctx, mx, use_symbol = (
                _DIFF_WIDE_CONTEXT_LINES, _DIFF_WIDE_MAX_LINES, True)
        units = []
        for pid, findings in by_pat.items():
            changes = pattern_changes[pid]
            capped = changes[:_DIFF_MAX_BLOCKS_PER_PATTERN]
            needed = [i for i in range(len(capped)) if f"{pid}#{i}" not in gen]
            if not needed:
                continue  # every previewed change already generated — reuse, no I/O
            srcs, files = [], []
            seen_src = set()
            changes_text = " ".join(str(c) for c in changes)
            for f in findings:
                parts = []
                w = read_finding_source(
                    f, repo_root, context_lines=ctx, max_lines=mx)
                if w:
                    parts.append(w)
                if use_symbol:
                    sw = read_symbol_source(f, repo_root, hint_text=changes_text)
                    if sw and sw not in parts:
                        parts.append(sw)
                s = "\n\n".join(parts) or f.get("_source")
                if s and s not in seen_src:
                    seen_src.add(s)
                    srcs.append(s)
                ff = f.get("file")
                if ff and ff not in files:
                    files.append(ff)
            source = "\n\n".join(srcs)
            kind = str(findings[0].get("finding_kind", "") or pid)
            for i in needed:
                units.append({
                    "id": f"{pid}#{i}",
                    "title": str(capped[i])[:120] or kind,
                    "affected_files": files,
                    "proposed_changes": [capped[i]],
                    "source": source,
                })
        return units

    # Tier 1: tight window (accurate "current code" at the cited lines).
    tight_units = _build_units(tight=True)
    if tight_units:
        gen.update(diff_preview.generate_all(tight_units, state_dir, client))
    # Tier 2: for changes the tight pass couldn't ground, cast a larger net.
    wide_units = _build_units(tight=False)
    if wide_units:
        gen.update(diff_preview.generate_all(wide_units, state_dir, client))

    result: dict[str, list] = {}
    for pid, changes in pattern_changes.items():
        blocks = _pattern_diff_blocks(pid, changes, gen)
        if blocks:
            result[pid] = blocks
    return result


def render(state_dir, output_html=None, output_md=None, run_label=None,
           use_synthesis=False, domain="general", domain_file=None,
           repo_root=None,
           github_url="", github_ref="", vscode_links=True,
           preview_diffs=False, github_path_prefix=""):
    """Render the Decision Brief (HTML + Markdown) for a run state directory.

    Reads ``findings.json``, ``semantic_groups.json``, ``remediation_patterns.json``
    and (optionally) ``tech_debt.json`` from ``state_dir``. Output paths default to
    ``<state_dir>/brief.html`` and ``<state_dir>/brief.md``; ``run_label`` defaults
    to the state-dir name. Returns ``(output_html, output_md, total, group_rows)``
    where ``total`` is the finding count and ``group_rows`` is the number of
    semantic-group decision rows rendered in the brief (``len(ordered)``).

    When ``use_synthesis`` is True, each row with more than
    ``SYNTHESIS_MIN_PATTERNS`` patterns gets a model-generated one-paragraph
    summary of the class of fix (one LLM call per such row).
    """
    state_dir = Path(state_dir)
    # Default the source-link root to the current working directory rather than a
    # hardcoded machine-specific path (F2-51ac9bc6a31b).
    if repo_root is None:
        repo_root = str(Path.cwd())
    output_html = Path(output_html) if output_html else state_dir / "brief.html"
    output_md = Path(output_md) if output_md else state_dir / "brief.md"
    run_label = run_label or state_dir.name
    # cycle 61 — pin GitHub permalinks to the assessed commit recorded at
    # Stage 1 when the operator did not pass an explicit --github-ref.
    if github_url and not github_ref:
        github_ref = assessed_commit(state_dir)
    ordered, total, sev_tot = _build_rollups(state_dir)
    # Pre-read source + VS Code link once per finding (cycle 50). Stashed on the
    # finding dict (same dicts live in each rollup's "findings" list).
    for _key, r in ordered:
        for f in r["findings"]:
            if f.get("id"):
                f["_source"] = read_finding_source(
                    f, repo_root,
                    context_lines=_DIFF_SOURCE_CONTEXT_LINES,
                    max_lines=_DIFF_SOURCE_MAX_LINES,
                )
                # VS Code deep-links only resolve on a machine where the repo
                # sits at repo_root; suppress them for portable/offline briefs
                # (e.g. handed to a code owner) where GitHub permalinks + the
                # inline "View changes" source are the reliable paths to code.
                f["_vscode"] = _vscode_link(f, repo_root) if vscode_links else None
                f["_resolved_line"] = _resolve_start_line(f, repo_root)
                # cycle 61 — commit-pinned GitHub permalink for handoff to the
                # code owner (works in a browser, no local checkout). Computed
                # after _resolved_line so it can reuse the resolved line number.
                f["_github"] = _github_link(f, github_url, github_ref, github_path_prefix)
    syntheses = _build_syntheses(ordered) if use_synthesis else None
    # finding_id -> TD id, for per-finding decision export (cycle 49A). If a
    # finding maps to more than one TD, the last one wins.
    fid2td: dict[str, str] = {}
    # cycle 62 — finding_id -> prior human_triage decision (sense 2 of "closed"),
    # so a re-rendered brief shows decisions already applied via triage --batch.
    fid2triage: dict[str, str] = {}
    td_path = state_dir / "tech_debt.json"
    if td_path.exists():
        # Guard the load: a missing/corrupt tech_debt.json must surface a clear
        # operator error, not an unhandled OSError/JSONDecodeError traceback
        # (F2-0b722c77c508).
        try:
            _td_items = json.loads(td_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            raise click.ClickException(f"Failed to load tech_debt.json: {e}")
        for t in _td_items:
            tid, fidv = t.get("id"), t.get("finding_id")
            if tid and fidv:
                fid2td[fidv] = tid
            if fidv and t.get("human_triage"):
                fid2triage[fidv] = str(t.get("human_triage"))
    # finding_id -> group_id_safe, so export can attach group-level conditions (cycle 51).
    fid2group: dict[str, str] = {}
    for (mod, kind), r in ordered:
        gid_safe = _group_id_safe(f"{mod}|{kind}")
        for f in r["findings"]:
            if f.get("id"):
                fid2group[f["id"]] = gid_safe
    consequences = load_consequences(domain_file)
    # cycle 53 — optional per-finding consequence personalization (one LLM call
    # per finding, cached at <state-dir>/consequence_cache.json). Falls back to the
    # YAML template per finding on any failure; without --synthesize, templates only.
    all_findings = [f for _k, r in ordered for f in r["findings"]]
    personalized: dict[str, dict] = {}
    if use_synthesis:
        llm_client = None
        try:
            from sentinel.api.client import create_model_client
            from sentinel.config import load_config
            llm_client = create_model_client(load_config())
        except Exception as exc:
            # Fall back to YAML templates, but make the degradation visible
            # instead of hiding config/import/auth failures behind a silent
            # no-client path (FIX-REQUIRED-313).
            llm_client = None
            log.warning(
                "Synthesis requested (--synthesize) but LLM client init failed; "
                "degrading to template consequences: %s: %s",
                type(exc).__name__, exc,
            )
        from sentinel.reports.consequence_synth import synthesize_all
        source_cache = {f["id"]: f.get("_source") for f in all_findings if f.get("id")}
        personalized = synthesize_all(
            findings=all_findings, consequences=consequences, domain=domain,
            source_cache=source_cache, state_dir=str(state_dir), client=llm_client,
        )
    for f in all_findings:
        fid = f.get("id")
        if fid:
            f["_personalized_csq"] = personalized.get(fid)
            f["_human_triage"] = fid2triage.get(fid)
        f["_synth_on"] = use_synthesis
    # S2-2 — load the remediation bundles when Stage 5d wrote them. Absent /
    # corrupt bundles.json degrades to the flat group list (backward compatible).
    bundles = None
    bundles_path = state_dir / "bundles.json"
    if bundles_path.exists():
        try:
            _loaded = json.loads(bundles_path.read_text(encoding="utf-8"))
            if isinstance(_loaded, list) and _loaded:
                bundles = _loaded
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("Failed to load bundles.json; rendering flat brief: %s", exc)
    # Part A′ — illustrative diff previews (opt-in, --preview-diffs). Anchor on
    # bundles when present (post-triage view), else on remediation PATTERNS so
    # the preview appears on the *triage* brief — where bundles don't exist yet
    # but the owner actually needs to see the potential change. The diff is
    # illustrative, kept out of the exported decisions JSON, and any failure
    # degrades to no-diff so the brief always renders.
    pattern_diffs: dict = {}
    if preview_diffs:
        if bundles:
            _attach_bundle_diffs(bundles, all_findings, str(state_dir))
        else:
            pattern_diffs = _attach_pattern_diffs(all_findings, str(state_dir), repo_root)
    # Flag findings whose pattern produced a diff, so the row renders an inline
    # "Show illustrative change" toggle (the diff is pulled from _patternDiffs).
    if pattern_diffs:
        for f in all_findings:
            f["_has_diff"] = str(f.get("_pattern_id", "") or "") in pattern_diffs
    emit_md(ordered, total, sev_tot, output_md, run_label,
            repo_name=assessed_repo_name(state_dir))
    emit_html(ordered, total, sev_tot, output_html, run_label, syntheses,
              consequences=consequences, domain=domain, fid2td=fid2td, repo_root=repo_root,
              fid2group=fid2group, bundles=bundles, vscode_links=vscode_links,
              pattern_diffs=pattern_diffs)
    return output_html, output_md, total, len(ordered)


def _parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Render a Sentinel Remediation Decision Brief (HTML + Markdown) "
                    "from a run state directory.")
    p.add_argument("--state-dir", type=Path, required=True,
                   help="Run state directory (reads findings.json, semantic_groups.json, "
                        "remediation_patterns.json, tech_debt.json).")
    p.add_argument("--output-html", type=Path, default=None,
                   help="HTML output path. Default: <state-dir>/brief.html")
    p.add_argument("--output-md", type=Path, default=None,
                   help="Markdown output path. Default: <state-dir>/brief.md")
    p.add_argument("--run-label", default=None,
                   help="Label shown in the H1 and <title>. Default: the state-dir name.")
    p.add_argument("--synthesize", action="store_true",
                   help="Add a model-generated 'proposed fix pattern' summary to "
                        "each row with more than 3 patterns (one LLM call per row).")
    p.add_argument("--domain", default="general",
                   help="Consequence-spectrum vocabulary (a key in consequences.yaml). "
                        "Default: general.")
    p.add_argument("--domain-file", default=None,
                   help="Override path to a custom consequences YAML file.")
    p.add_argument("--repo-root", default=None,
                   help="Filesystem root prepended to each finding's file path in the "
                        "'View changes' VS Code deep-link (vscode://file/<root>/<file>). "
                        "Defaults to the current working directory when omitted.")
    p.add_argument("--github-url", default="",
                   help="Base URL of the GitHub repo (https://host/org/repo). When set, "
                        "each finding gets a commit-pinned 'View on GitHub' permalink the "
                        "code owner can open in a browser.")
    p.add_argument("--github-ref", default="",
                   help="Commit SHA (or ref) to pin GitHub permalinks to. Optional: when "
                        "omitted with --github-url, it is auto-filled from the assessed "
                        "commit recorded at Stage 1 so #L line anchors stay accurate.")
    return p.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args()
    out_html, out_md, total, rows = render(
        args.state_dir, args.output_html, args.output_md, args.run_label,
        use_synthesis=args.synthesize, domain=args.domain, domain_file=args.domain_file,
        repo_root=args.repo_root, github_url=args.github_url, github_ref=args.github_ref)
    print(f"Wrote:\n  {out_md}\n  {out_html}")
    print(f"{total} findings -> {rows} decision rows")
