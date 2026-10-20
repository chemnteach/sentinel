"""sentinel/reports/server.py

Phase 3 — minimal Flask UI for Screens 2-5 (Plan Section 9).

``create_app(state_dir)`` builds a Flask app bound to one run's state directory.
It serves four HTML screens and a small JSON API:

  GET  /            -> Screen 2  (pre-remediation conversation)
  GET  /progress    -> Screen 3  (remediation progress)
  GET  /escalated   -> Screen 4  (escalated task review)
  GET  /learning    -> Screen 5  (learning proposals)
  POST /api/recommend    -> LLM narrative recommendation + default iterations
  POST /api/instruction  -> copyable Copilot batch instruction
  GET  /api/progress     -> batch_summary.json (empty structure if absent)

All LLM calls go through ``_get_llm_client()`` (which is exactly
``create_model_client(get_config())``) so the network path is centralized and
tests can stub it. Screens read run artifacts (finding_status.json,
batch_summary.json, learnings/) directly and read-only — the server never
mutates run state.
"""

from __future__ import annotations

import base64
import html
import json
import logging
import math
import os
import re
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from flask import Flask, abort, jsonify, render_template, request, send_from_directory

from sentinel.assess.scope_recommender import recommend_scope
from sentinel.orchestrator.graph_context import (
    find_nodes_for_file,
    get_god_nodes,
    get_node_neighbors,
)

log = logging.getLogger(__name__)

# Phase 5 — Graphify graph output location. Env-overridable so the absolute
# default does not hard-bind the module.
_GRAPHIFY_ROOT_DEFAULT = "D:/Sentinel-Runs/graphify"

_APPROVED_STATUSES = ("APPROVED", "APPROVED_WITH_CONDITIONS")
_GROUPS_PER_ITERATION = 200  # heuristic: ~1 remediation iteration per N approved groups

# FIX 3 — learning -> PR proposal.
_GITHUB_PR_REPO = "chemnteach/sentinel"
_GITHUB_PR_BASE = "main"
_LEARNING_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")  # guards the {id}.json path join
_PR_TITLE_PATTERN_CHARS = 72
_MAX_SUGGESTED_ITERATIONS = 10


def _get_llm_client():
    """Return the runtime LLM client — ``create_model_client(get_config())``.

    Isolated in one function so the single network dependency is easy to stub in
    tests and easy to find in code.
    """
    from sentinel.api.client import create_model_client
    from sentinel.config import get_config

    return create_model_client(get_config())


# ----------------------------------------------------------------- artifact reads


def _read_json(path: Path) -> Any:
    """Read+parse JSON, returning None on any error (missing/corrupt)."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _finding_status_records(state_dir: Path) -> list[dict]:
    data = _read_json(state_dir / "finding_status.json")
    return data if isinstance(data, list) else []


def _approved_count(state_dir: Path) -> int:
    return sum(
        1
        for r in _finding_status_records(state_dir)
        if str(r.get("status")) in _APPROVED_STATUSES
    )


def _escalated_records(state_dir: Path) -> list[dict]:
    return [
        r for r in _finding_status_records(state_dir)
        if str(r.get("status")) == "ESCALATED"
    ]


def _retry_context_for(state_dir: Path, task_id: str) -> str:
    """Latest retry-context markdown for a task, or '' when none was written."""
    if not task_id:
        return ""
    task_dir = state_dir / "retry" / str(task_id)
    if not task_dir.is_dir():
        return ""
    attempts = sorted(
        (p for p in task_dir.glob("attempt-*") if p.is_dir()),
        key=lambda p: p.name,
    )
    for attempt in reversed(attempts):
        ctx = attempt / "context.md"
        if ctx.exists():
            try:
                return ctx.read_text(encoding="utf-8")
            except OSError:
                return ""
    return ""


def _resolve_graph_path(state_dir: Path) -> Path | None:
    """Resolve the run's graph.json across the known locations, or None.

    Lookup order (first existing wins):
      1. ``$SENTINEL_GRAPHIFY_DIR/<basename>/graph.json``  (when the env var is set)
      2. ``D:/Sentinel-Runs/graphify/<basename>/graph.json``
      3. ``D:/Sentinel-Runs/graphify/sentinel/graph.json`` (canonical self-assessment)
      4. ``<state_dir>/graphify/graph.json``
    where ``<basename>`` is ``state_dir.name``. Step 3 lets any run fall back to
    the canonical Sentinel self-assessment graph when it has none of its own."""
    basename = state_dir.name
    candidates: list[Path] = []
    env_root = os.environ.get("SENTINEL_GRAPHIFY_DIR")
    if env_root:
        candidates.append(Path(env_root) / basename / "graph.json")
    candidates.append(Path(_GRAPHIFY_ROOT_DEFAULT) / basename / "graph.json")
    candidates.append(Path(_GRAPHIFY_ROOT_DEFAULT) / "sentinel" / "graph.json")
    candidates.append(state_dir / "graphify" / "graph.json")
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


# ----------------------------------------------------------------- bundles (S4-7)

# Bundle statuses that map to the summary counters. UNDECIDED is the only
# "not yet decided" state for the resume indicator.
_BUNDLE_STATUS_KEYS = {
    "UNDECIDED": "undecided",
    "APPROVED": "approved",
    "IN_PROGRESS": "in_progress",
    "IN_REVIEW": "in_review",
    "COMPLETE": "complete",
    "REVERTED": "reverted",
}
# Root-cause severities whose bundles must be COMPLETE for delivery readiness.
_BLOCKING_BUNDLE_SEVERITIES = {"CRITICAL", "P1"}


def _read_bundles(state_dir: Path) -> list[dict]:
    data = _read_json(state_dir / "bundles.json")
    return [b for b in data if isinstance(b, dict)] if isinstance(data, list) else []


def _bundle_status(b: dict) -> str:
    return str(b.get("bundle_status") or b.get("status") or "").strip().upper()


def _bundle_summary(bundles: list[dict]) -> dict:
    """Per-status counts + total for the bundle set."""
    summary = {"total": len(bundles)}
    for key in _BUNDLE_STATUS_KEYS.values():
        summary[key] = 0
    for b in bundles:
        key = _BUNDLE_STATUS_KEYS.get(_bundle_status(b))
        if key:
            summary[key] += 1
    return summary


def _completion_ready(bundles: list[dict]) -> bool:
    """True iff every Critical/P1 (by root_cause_severity) bundle is COMPLETE.

    An empty bundle set is not delivery-ready.
    """
    if not bundles:
        return False
    blocking = [
        b for b in bundles
        if str(b.get("root_cause_severity") or "").strip().upper()
        in _BLOCKING_BUNDLE_SEVERITIES
    ]
    return all(_bundle_status(b) == "COMPLETE" for b in blocking)


def _severity_band(raw: Any) -> str:
    """Normalise a root-cause severity to Critical / P1 / P2 / P3 (default P3)."""
    s = str(raw or "").strip().lower()
    if s in ("critical", "crit"):
        return "Critical"
    if s in ("p1", "high"):
        return "P1"
    if s in ("p2", "medium", "med"):
        return "P2"
    return "P3"


def _atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON via a temp file + atomic replace so a reader never sees a
    half-written file (temp+replace in the target's own directory)."""
    import tempfile

    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp_name, str(path))
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _bundle_view(b: dict) -> dict:
    """Shape one bundle for the /triage template.

    ``cascade`` is every non-root member (each with an ``excludable`` flag); the
    root cause is surfaced separately and is never excludable.
    """
    root_fid = str(b.get("root_cause_finding_id", "") or "")
    members = b.get("member_findings") or []
    cascade = []
    for m in members:
        fid = str(m.get("finding_id", "") or "")
        if fid == root_fid:
            continue
        cascade.append({
            "finding_id": fid,
            "severity": str(m.get("severity", "") or ""),
            "title": str(m.get("title", "") or ""),
        })
    affected = b.get("affected_files") or []
    return {
        "bundle_id": str(b.get("bundle_id", "") or ""),
        "status": _bundle_status(b),
        "root_severity": _severity_band(b.get("root_cause_severity")),
        "root_title": str(b.get("root_cause_title", "") or ""),
        "root_file": str(b.get("root_cause_file", "") or (affected[0] if affected else "")),
        "root_line": b.get("root_cause_line"),
        "root_finding_id": root_fid,
        "also_resolves": len(cascade),
        "cascade": cascade,
    }


def _learning_proposals(state_dir: Path) -> list[dict]:
    """Read learning proposals from state_dir/learnings/LRN-*.json.

    Maps the real Learning schema (sentinel/learnings/schema.py) onto the
    Screen 5 display fields:
      proposed_change <- pattern
      trigger         <- why
      confidence      <- "high" if applicable_to_generation else "low"
    plus category and severity as display fields. Only ``LRN-*.json`` records
    are read (the store's index.json manifest is skipped)."""
    learnings_dir = state_dir / "learnings"
    if not learnings_dir.is_dir():
        return []
    proposals: list[dict] = []
    for path in sorted(learnings_dir.glob("LRN-*.json")):
        item = _read_json(path)
        if not isinstance(item, dict):
            continue
        proposals.append({
            "id": item.get("id") or "",
            "proposed_change": item.get("pattern") or "",
            "trigger": item.get("why") or "",
            "confidence": "high" if item.get("applicable_to_generation") else "low",
            "category": item.get("category") or "",
            "severity": item.get("severity") or "",
        })
    return proposals


def _progress_payload(state_dir: Path) -> dict:
    """batch_summary.json contents + derived summary counts; empty when absent."""
    data = _read_json(state_dir / "batch_summary.json")
    if not isinstance(data, dict):
        data = {"run_label": state_dir.name, "last_updated": None, "tasks": {}}
    tasks = data.get("tasks") if isinstance(data.get("tasks"), dict) else {}
    counts = {"completed": 0, "escalated": 0, "in_progress": 0, "failed": 0}
    for rec in tasks.values():
        status = str((rec or {}).get("status", "")).upper()
        if status == "COMPLETE":
            counts["completed"] += 1
        elif status == "ESCALATED":
            counts["escalated"] += 1
        elif status == "IN_PROGRESS":
            counts["in_progress"] += 1
        elif status == "FAILED":
            counts["failed"] += 1
    # pending = approved groups not yet represented as tasks in the summary
    counts["pending"] = max(0, _approved_count(state_dir) - len(tasks))
    return {
        "run_label": data.get("run_label", state_dir.name),
        "last_updated": data.get("last_updated"),
        "tasks": tasks,
        "summary": counts,
    }


def _load_learning(state_dir: Path, learning_id: str) -> dict | None:
    """Load one learning record by id, or None if absent/invalid."""
    if not _LEARNING_ID_RE.match(learning_id):
        return None
    data = _read_json(state_dir / "learnings" / f"{learning_id}.json")
    return data if isinstance(data, dict) else None


def _build_pr_content(learning: dict) -> tuple[str, str]:
    """Build the PR title and markdown body from a learning record."""
    category = str(learning.get("category", ""))
    pattern = str(learning.get("pattern", ""))
    title = f"Learning: {category} — {pattern[:_PR_TITLE_PATTERN_CHARS]}"
    finding_ids = learning.get("sentinel_finding_ids") or []
    ids_block = "\n".join(f"- `{fid}`" for fid in finding_ids) or "- (none)"
    body = (
        f"## Learning: {learning.get('id', '')}\n\n"
        f"**Pattern:** {pattern}\n\n"
        f"**Why it matters:** {learning.get('why', '')}\n\n"
        f"**Severity:** {learning.get('severity', '')}\n\n"
        f"**Category:** {category}\n\n"
        f"**Applicable to generation:** {learning.get('applicable_to_generation', False)}\n\n"
        f"**Contributing findings ({len(finding_ids)}):**\n{ids_block}\n"
    )
    return title, body


def _is_http_url(value: Any) -> bool:
    """True only for a plain http/https URL — rejects ``javascript:`` and other
    schemes that would be dangerous when placed in an HTML ``href``."""
    if not isinstance(value, str):
        return False
    try:
        scheme = urllib.parse.urlparse(value).scheme.lower()
    except ValueError:
        return False
    return scheme in ("http", "https")


def _redact(text: str, secret: str) -> str:
    """Replace ``secret`` with ``***`` in ``text`` so a credential (e.g. the
    GITHUB_TOKEN) is never written to logs or returned in an API error body."""
    if secret and secret in text:
        return text.replace(secret, "***")
    return text


def _github_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _is_422_with(resp: httpx.Response, needle: str) -> bool:
    """True if the response is a 422 whose JSON body mentions ``needle``.

    Checks the whole serialized body so it matches both the top-level
    ``message`` (branch-ref conflicts) and nested ``errors[].message`` (PR
    conflicts)."""
    if resp.status_code != 422:
        return False
    try:
        blob = json.dumps(resp.json())
    except Exception:  # noqa: BLE001
        blob = resp.text or ""
    return needle.lower() in blob.lower()


def _open_learning_pr(
    api_base: str,
    token: str,
    learning: dict,
    learning_id: str,
    title: str,
    pr_body: str,
) -> tuple[str | None, int | None]:
    """Create branch -> commit the learning file -> open the PR (steps 1-4).

    Idempotent against pre-existing branch / file / PR (each conflict is handled
    per the route contract). Returns ``(pr_url, pr_number)``. Raises on any
    unhandled GitHub error (the route converts that to a JSON ``error``)."""
    repo = _GITHUB_PR_REPO
    owner = repo.split("/", 1)[0]
    branch = f"learning/{learning_id}"
    headers = _github_headers(token)

    # Track which GitHub step is in flight so a failure names the exact stage
    # rather than surfacing an opaque HTTP error. (FIX-DESIRED-011)
    step = "1:resolve-base-sha"
    try:
        # Step 1 — resolve the current main tip SHA.
        r = httpx.get(f"{api_base}/repos/{repo}/git/ref/heads/{_GITHUB_PR_BASE}",
                      headers=headers, timeout=30.0)
        r.raise_for_status()
        base_sha = r.json()["object"]["sha"]

        # Step 2 — create the branch ref (non-fatal if it already exists).
        step = "2:create-branch"
        r = httpx.post(
            f"{api_base}/repos/{repo}/git/refs",
            headers=headers,
            json={"ref": f"refs/heads/{branch}", "sha": base_sha},
            timeout=30.0,
        )
        if not _is_422_with(r, "Reference already exists"):
            r.raise_for_status()

        # Step 3 — commit the learning record onto the branch (create, or update
        # with the existing blob SHA when the file is already present).
        step = "3:commit-learning-file"
        content_b64 = base64.b64encode(
            json.dumps(learning, indent=2).encode("utf-8")
        ).decode("ascii")
        contents_url = (
            f"{api_base}/repos/{repo}/contents/learnings/"
            f"{urllib.parse.quote(learning_id, safe='')}.json"
        )
        commit_msg = f"learning: add {learning_id}"
        r = httpx.post(
            contents_url,
            headers=headers,
            json={"message": commit_msg, "content": content_b64, "branch": branch},
            timeout=30.0,
        )
        if r.status_code == 422:
            # File exists on the branch — fetch its blob SHA and PUT an update.
            g = httpx.get(contents_url, headers=headers, params={"ref": branch}, timeout=30.0)
            g.raise_for_status()
            blob_sha = g.json().get("sha")
            r = httpx.put(
                contents_url,
                headers=headers,
                json={"message": commit_msg, "content": content_b64,
                      "branch": branch, "sha": blob_sha},
                timeout=30.0,
            )
            r.raise_for_status()
        else:
            r.raise_for_status()

        # Step 4 — open the PR (return the existing one if one is already open).
        step = "4:open-pr"
        r = httpx.post(
            f"{api_base}/repos/{repo}/pulls",
            headers=headers,
            json={"title": title, "body": pr_body, "head": branch, "base": _GITHUB_PR_BASE},
            timeout=30.0,
        )
        if _is_422_with(r, "A pull request already exists"):
            g = httpx.get(
                f"{api_base}/repos/{repo}/pulls",
                headers=headers,
                params={"head": f"{owner}:{branch}", "state": "open"},
                timeout=30.0,
            )
            g.raise_for_status()
            existing = g.json()
            if existing:
                return existing[0].get("html_url"), existing[0].get("number")
            raise RuntimeError("PR already exists but could not be located")
        r.raise_for_status()
        data = r.json()
        # Validate the PR-creation response actually carries the fields the caller
        # relies on, rather than returning a silent (None, None) that looks like a
        # located-but-unusable PR downstream. (FIX-DESIRED-013)
        html_url, number = data.get("html_url"), data.get("number")
        if not html_url or number is None:
            raise RuntimeError(
                f"GitHub PR creation for learning {learning_id} returned an "
                f"unexpected response missing 'html_url'/'number': {data!r}"
            )
        return html_url, number
    except Exception as exc:
        # Redact the token before it can reach the logs or (via the re-raised
        # message) the JSON error body returned to the client.
        safe_msg = _redact(str(exc), token)
        log.error(
            "learning PR for %s failed at step %s: %s", learning_id, step, safe_msg
        )
        raise RuntimeError(safe_msg) from None


def _build_instruction(run_label: str, iterations: int, push_mode: str) -> str:
    """The copyable Copilot batch instruction for the code owner (deterministic)."""
    push_line = (
        "Open a pull request for each task (do NOT push directly to the default branch)."
        if push_mode == "pr"
        else "Commit and push each approved fix directly to the working branch."
    )
    return (
        f"# Sentinel remediation batch — {run_label}\n\n"
        f"Run the approved remediation queue for this engagement.\n\n"
        f"1. Load the approved decisions from `decisions.json` in the run state "
        f"directory (exported from the Decision Brief / applied via "
        f"`sentinel triage --batch`).\n"
        f"2. Drive up to **{iterations} iteration(s)** of the Phase-2 remediation "
        f"loop (test-first -> Architect review -> QA -> docs) via "
        f"`sentinel govern --start`.\n"
        f"3. {push_line}\n"
        f"4. Leave escalated tasks for human review; do not force them through.\n\n"
        f"Iterations: {iterations}\n"
        f"Push mode: {push_mode}\n"
    )


# ----------------------------------------------------------------- app factory


def create_app(state_dir: str) -> Flask:
    """Build the Flask UI app bound to ``state_dir``."""
    app = Flask(__name__, template_folder="templates")
    app.config["STATE_DIR"] = str(state_dir)

    def _state_dir() -> Path:
        return Path(app.config["STATE_DIR"])

    # -- screens --------------------------------------------------------

    @app.get("/scope")
    def screen0():
        sd = _state_dir()
        repo_path = request.args.get("repo_path") or app.config.get("REPO_PATH") or "."
        recommendation = recommend_scope(repo_path)
        existing = _read_json(sd / "scope_config.json")
        return render_template(
            "screen0.html",
            run_label=sd.name,
            rec=recommendation,
            existing=existing if isinstance(existing, dict) else None,
        )

    @app.get("/")
    def screen2():
        sd = _state_dir()
        return render_template(
            "screen2.html",
            run_label=sd.name,
            approved_count=_approved_count(sd),
        )

    @app.get("/progress")
    def screen3():
        return render_template("screen3.html", run_label=_state_dir().name)

    @app.get("/escalated")
    def screen4():
        sd = _state_dir()
        tasks = []
        for r in _escalated_records(sd):
            task_id = r.get("task_id") or r.get("group_id") or ""
            tasks.append({
                "task_id": task_id,
                "description": r.get("canonical_finding_id") or r.get("group_id") or "",
                "severity": r.get("severity") or "",
                "reason": r.get("notes") or "Exhausted remediation retries.",
                "retry_context": _retry_context_for(sd, r.get("task_id") or ""),
            })
        return render_template("screen4.html", run_label=sd.name, tasks=tasks)

    @app.get("/learning")
    def screen5():
        sd = _state_dir()
        return render_template(
            "screen5.html", run_label=sd.name, proposals=_learning_proposals(sd)
        )

    @app.get("/triage")
    def screen6():
        sd = _state_dir()
        bundles = _read_bundles(sd)
        summary = _bundle_summary(bundles)
        decided = summary["total"] - summary["undecided"]
        # Group bundles by root-cause severity band for the three sections.
        sections = {"Critical": [], "P1": [], "improvement": []}
        for b in bundles:
            band = _severity_band(b.get("root_cause_severity"))
            view = _bundle_view(b)
            if band == "Critical":
                sections["Critical"].append(view)
            elif band == "P1":
                sections["P1"].append(view)
            else:
                sections["improvement"].append(view)
        return render_template(
            "screen6_triage.html",
            run_label=sd.name,
            summary=summary,
            decided=decided,
            sections=sections,
        )

    @app.get("/brief.html")
    def brief():
        """Serve the run's Decision Brief so Screen 2's back-link resolves."""
        sd = _state_dir()
        if not (sd / "brief.html").exists():
            abort(404)
        return send_from_directory(str(sd), "brief.html")

    @app.get("/handoff.html")
    def handoff():
        """Serve the run's one-page assessment handoff document (S4-10)."""
        sd = _state_dir()
        if not (sd / "handoff.html").exists():
            abort(404)
        return send_from_directory(str(sd), "handoff.html")

    # -- API ------------------------------------------------------------

    @app.post("/api/recommend")
    def api_recommend():
        sd = _state_dir()
        approved = _approved_count(sd)
        suggested = max(1, min(_MAX_SUGGESTED_ITERATIONS,
                               math.ceil(approved / _GROUPS_PER_ITERATION) or 1))
        system = (
            "You are a remediation planning advisor for the Sentinel code "
            "governance system. Give concise, practical guidance."
        )
        user = (
            f"A code assessment run ('{sd.name}') has {approved} approved finding "
            f"group(s) queued for automated remediation. In 3-5 sentences, "
            f"recommend how the owner should run the remediation batch: how many "
            f"iterations to allow, whether to open PRs or push directly, and what "
            f"to watch for. A reasonable default is {suggested} iteration(s)."
        )
        try:
            recommendation = _get_llm_client().call_model(system, user).content.strip()
        except Exception as exc:  # noqa: BLE001 — degrade to a static recommendation
            recommendation = (
                f"({approved} approved group(s).) Automated recommendation "
                f"unavailable ({exc}). Suggested default: {suggested} iteration(s), "
                f"open PRs for review."
            )
        return jsonify(
            recommendation=recommendation,
            suggested_iterations=suggested,
            approved_count=approved,
        )

    @app.post("/api/instruction")
    def api_instruction():
        body = request.get_json(silent=True) or {}
        try:
            iterations = int(body.get("iterations", 1))
        except (TypeError, ValueError):
            iterations = 1
        iterations = max(1, iterations)
        push_mode = "direct" if str(body.get("push_mode", "pr")).lower() == "direct" else "pr"
        instruction = _build_instruction(_state_dir().name, iterations, push_mode)
        return jsonify(instruction=instruction)

    @app.get("/api/progress")
    def api_progress():
        return jsonify(_progress_payload(_state_dir()))

    # -- S4-7: live bundle status -------------------------------------

    @app.get("/api/bundles")
    def api_bundles():
        bundles = _read_bundles(_state_dir())
        return jsonify(
            bundles=bundles,
            summary=_bundle_summary(bundles),
            completion_ready=_completion_ready(bundles),
        )

    @app.post("/api/bundles/<bundle_id>/decision")
    def api_bundle_decision(bundle_id):
        sd = _state_dir()
        body = request.get_json(silent=True) or {}
        decision = str(body.get("decision", "")).strip().upper()
        excluded = body.get("excluded_finding_ids")
        excluded_ids = {str(x) for x in excluded} if isinstance(excluded, list) else set()

        bundles = _read_bundles(sd)
        target = next(
            (b for b in bundles if str(b.get("bundle_id", "")) == str(bundle_id)), None
        )
        if target is None:
            return jsonify(error=f"bundle {bundle_id} not found"), 404

        # Map the operator decision onto a bundle status: APPROVED -> APPROVED,
        # DEFERRED (and anything else) -> back to UNDECIDED.
        new_status = "APPROVED" if decision == "APPROVED" else "UNDECIDED"
        target["bundle_status"] = new_status

        # Update tech_debt.json for the bundle's member findings: excluded ->
        # DEFER; the rest -> APPROVE when the bundle itself is approved.
        td_raw = _read_json(sd / "tech_debt.json")
        tech_debt = td_raw if isinstance(td_raw, list) else []
        td_by_fid = {
            str(item.get("finding_id")): item
            for item in tech_debt
            if isinstance(item, dict) and item.get("finding_id") is not None
        }
        member_fids = [
            str(m.get("finding_id", "") or "")
            for m in (target.get("member_findings") or [])
            if m.get("finding_id")
        ]
        for fid in member_fids:
            item = td_by_fid.get(fid)
            if item is None:
                item = {"finding_id": fid}
                tech_debt.append(item)
                td_by_fid[fid] = item
            if fid in excluded_ids:
                item["human_triage"] = "DEFER"
            elif new_status == "APPROVED":
                item["human_triage"] = "APPROVE"

        # Atomic writes: bundles first, then tech_debt.
        _atomic_write_json(sd / "bundles.json", bundles)
        _atomic_write_json(sd / "tech_debt.json", tech_debt)
        return jsonify(target)

    @app.post("/api/scope/approve")
    def api_scope_approve():
        sd = _state_dir()
        body = request.get_json(silent=True) or {}
        excludes = body.get("excludes")
        excluded_paths = [str(p) for p in excludes] if isinstance(excludes, list) else []
        repo_path = str(body.get("repo_path") or app.config.get("REPO_PATH") or "")
        config = {
            "repo_path": repo_path,
            "excluded_paths": excluded_paths,
            "approved_at": datetime.now().isoformat(timespec="seconds"),
            "approved_by": "human",
        }
        out_path = sd / "scope_config.json"
        out_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        return jsonify(status="saved", path=str(out_path))

    @app.post("/api/learning/propose-pr")
    def api_propose_pr():
        body = request.get_json(silent=True) or {}
        learning_id = str(body.get("learning_id", "")).strip()
        token = os.environ.get("GITHUB_TOKEN", "")
        if not token:
            return jsonify(error="GITHUB_TOKEN not set"), 400
        if not learning_id:
            return jsonify(error="learning_id required"), 400

        learning = _load_learning(_state_dir(), learning_id)
        if learning is None:
            return jsonify(error=f"learning {learning_id} not found"), 404

        title, pr_body = _build_pr_content(learning)
        api_base = os.environ.get("GITHUB_API_BASE", "https://api.github.com")
        try:
            pr_url, pr_number = _open_learning_pr(
                api_base, token, learning, learning_id, title, pr_body
            )
            # The client renders pr_url into an <a href> via innerHTML. Only allow
            # http(s) URLs and HTML-escape the value so a hostile/compromised
            # gateway response cannot break out of the attribute and inject markup.
            safe_url = pr_url if _is_http_url(pr_url) else ""
            return jsonify(pr_url=html.escape(safe_url, quote=True), pr_number=pr_number)
        except Exception as exc:  # noqa: BLE001 — surface any GitHub failure as JSON
            return jsonify(error=str(exc)), 502

    # -- Phase 5: code knowledge graph ----------------------------------

    @app.get("/graph")
    def graph_view():
        sd = _state_dir()
        gpath = _resolve_graph_path(sd)
        if gpath is None:
            return render_template(
                "graph.html", run_label=sd.name, graph_available=False,
                god_nodes=[], repo_hint=str(sd),
            )
        return render_template(
            "graph.html", run_label=sd.name, graph_available=True,
            god_nodes=get_god_nodes(str(gpath)), repo_hint=str(sd),
        )

    @app.get("/api/graph/data")
    def api_graph_data():
        gpath = _resolve_graph_path(_state_dir())
        if gpath is None:
            return jsonify(error="graph not available"), 404
        data = _read_json(gpath)
        if data is None:
            return jsonify(error="graph unreadable"), 404
        return jsonify(data)

    @app.get("/api/graph/node/<path:node_id>")
    def api_graph_node(node_id):
        gpath = _resolve_graph_path(_state_dir())
        if gpath is None:
            return jsonify(node_id=node_id, neighbors=[], error="graph not available"), 404
        data = _read_json(gpath) or {}
        exists = any(
            n.get("id") == node_id for n in data.get("nodes", []) if isinstance(n, dict)
        )
        if not exists:
            return jsonify(node_id=node_id, neighbors=[], error="node not found"), 404
        return jsonify(node_id=node_id, neighbors=get_node_neighbors(node_id, str(gpath)))

    @app.get("/api/graph/file")
    def api_graph_file():
        gpath = _resolve_graph_path(_state_dir())
        path = request.args.get("path", "")
        if gpath is None:
            return jsonify(nodes=[])
        return jsonify(nodes=find_nodes_for_file(path, str(gpath)))

    return app


__all__ = ["create_app"]
