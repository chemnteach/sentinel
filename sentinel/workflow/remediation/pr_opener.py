"""PR opening helpers for workflow-managed remediation branches (cycle 58).

Real GitHub PR creation via the ``gh`` CLI, replacing the compare-URL
placeholder. Every path degrades gracefully: if ``gh`` is missing, not
authenticated, errors, or times out, ``open_pr`` returns ``None`` so the
caller can fall back to the compare URL — a PR failure never aborts approval.
"""

from __future__ import annotations

import logging
import shutil
import subprocess

log = logging.getLogger(__name__)

_GH_TIMEOUT_SECONDS = 60


def _find_gh() -> str | None:
    """Return the path to the ``gh`` CLI, or ``None`` if it is not installed."""
    return shutil.which("gh")


def open_pr(
    repo_root: str,
    branch: str,
    title: str,
    body: str,
    base_branch: str = "main",
) -> str | None:
    """Open a GitHub PR with ``gh pr create``.

    Returns the PR URL on success, ``None`` on any failure (gh missing,
    unauthenticated, non-zero exit, timeout). The branch must already be
    pushed to the remote — the caller is responsible for that.
    """
    gh_path = _find_gh()
    if not gh_path:
        log.warning(
            "gh CLI not found — cannot open PR. Push the branch and open manually."
        )
        return None

    try:
        result = subprocess.run(
            [
                gh_path, "pr", "create",
                "--title", title,
                "--body", body,
                "--base", base_branch,
                "--head", branch,
            ],
            cwd=repo_root,
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=_GH_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        log.warning("gh pr create timed out")
        return None
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("gh pr create error: %s", exc)
        return None

    if result.returncode == 0:
        pr_url = result.stdout.strip()
        log.info("PR opened: %s", pr_url)
        return pr_url or None

    log.warning("gh pr create failed: %s", (result.stderr or "").strip())
    return None


def build_pr_title(task, bundle: dict | None = None, total_bundles: int = 0) -> str:
    """Build the PR title.

    When the task belongs to a bundle, use the root-cause-anchored form::

        [Sentinel] Fix: {root_cause_title} (Bundle {N} of {total})

    Otherwise fall back to the legacy per-task title (unchanged, backward
    compatible). ``bundle`` is the bundle dict from ``bundles.json``.
    """
    bundle_id = getattr(task, "bundle_id", None)
    if bundle_id and bundle:
        root_title = str(bundle.get("root_cause_title", "") or "").strip() or (
            task.description or task.task_id
        )
        number = _bundle_number(bundle_id)
        return f"[Sentinel] Fix: {root_title} (Bundle {number} of {total_bundles})"

    # Legacy per-task title.
    if task.description:
        return f"sentinel: {task.task_id} — {task.description[:60]}"
    return f"sentinel: {task.task_id}"


def _bundle_number(bundle_id: str) -> int:
    """Extract the numeric suffix from ``BUNDLE-00N`` (0 when unparseable)."""
    try:
        return int(str(bundle_id).rsplit("-", 1)[-1])
    except (ValueError, IndexError):
        return 0


def build_pr_body(
    task,
    qa_verdict: str = "",
    doc_summary: str = "",
    bundle: dict | None = None,
    state_dir: str = "",
    model: str = "",
    operator: str = "",
) -> str:
    """Build a structured PR body from task metadata.

    Includes the task description, files changed, acceptance criteria, any
    reviewer conditions (conditions_structured), and optional QA/doc summaries.

    When ``bundle`` is supplied and the task carries a ``bundle_id``, a
    bundle-aware header is prepended (root cause / severity / confidence /
    findings resolved / cascade findings predicted to resolve / validation
    command). Without a bundle the legacy per-task body is produced unchanged.
    """
    bundle_id = getattr(task, "bundle_id", None)
    if bundle_id and bundle:
        return _build_bundle_body(
            task, bundle, qa_verdict, doc_summary, state_dir, model, operator
        )

    lines = [
        f"## Sentinel Task: {task.task_id}",
        "",
        "### Description",
        task.description or "",
        "",
        "### Files Changed",
    ]
    for f in (task.files_changed or []):
        lines.append(f"- `{f}`")

    if task.acceptance_criteria:
        lines += ["", "### Acceptance Criteria"]
        for c in task.acceptance_criteria:
            lines.append(f"- {c}")

    if task.conditions_structured:
        lines += ["", "### Reviewer Conditions"]
        for c in task.conditions_structured:
            ctype = c.get("type", "")
            note = c.get("note", "") or c.get("target", "")
            lines.append(f"- **{ctype}**: {note}")

    if qa_verdict:
        lines += ["", "### QA Verdict", qa_verdict]

    if doc_summary:
        lines += ["", "### Documentation", doc_summary]

    lines += [
        "",
        "---",
        "_Generated by Sentinel governed remediation pipeline_",
    ]
    return "\n".join(lines)


def _build_bundle_body(
    task,
    bundle: dict,
    qa_verdict: str,
    doc_summary: str,
    state_dir: str,
    model: str,
    operator: str,
) -> str:
    """Bundle-aware PR body (S2-2)."""
    members = bundle.get("member_findings", []) or []
    root_fid = str(bundle.get("root_cause_finding_id", "") or "")
    resolved = [m for m in members if m.get("predicted_resolved")]
    cascade_predicted = [
        m
        for m in members
        if not m.get("predicted_resolved") and m.get("finding_id") != root_fid
    ]

    lines = [
        f"## [Sentinel] Fix: {bundle.get('root_cause_title', '') or task.task_id}",
        "",
        f"- **Root cause:** {bundle.get('root_cause_title', '') or '(unknown)'} "
        f"(`{root_fid}`)",
        f"- **Severity:** {bundle.get('root_cause_severity', '') or '(unknown)'}",
        f"- **Bundle confidence:** {bundle.get('confidence', '') or '(unknown)'}",
        f"- **Bundle:** `{bundle.get('bundle_id', '')}`",
        "",
        "### Findings resolved",
    ]
    for m in resolved:
        lines.append(
            f"- `{m.get('finding_id', '')}` "
            f"[{m.get('severity', '')}] {m.get('title', '')}"
        )
    if not resolved:
        lines.append("- (none)")

    lines += ["", "### Cascade findings predicted to resolve"]
    if cascade_predicted:
        for m in cascade_predicted:
            lines.append(
                f"- `{m.get('finding_id', '')}` "
                f"[{m.get('severity', '')}] {m.get('title', '')} "
                "_(predicted, verify separately)_"
            )
    else:
        lines.append("- (none)")

    pre_fix_sha = getattr(task, "pre_fix_sha", "") or ""
    lines += [
        "",
        "### Validation",
        (
            f"Re-run the affected review with `--changed-since {pre_fix_sha}` "
            "to confirm the cascade resolved:"
            if pre_fix_sha
            else "Re-run the affected review to confirm the cascade resolved."
        ),
    ]
    if pre_fix_sha:
        lines.append(f"```\nsentinel assess --changed-since {pre_fix_sha}\n```")

    if qa_verdict:
        lines += ["", "### QA Verdict", qa_verdict]
    if doc_summary:
        lines += ["", "### Documentation", doc_summary]

    footer_bits = []
    if state_dir:
        footer_bits.append(f"state_dir=`{state_dir}`")
    if model:
        footer_bits.append(f"model=`{model}`")
    if operator:
        footer_bits.append(f"operator=`{operator}`")
    lines += [
        "",
        "---",
        "_Generated by Sentinel governed remediation pipeline_"
        + (" · " + " · ".join(footer_bits) if footer_bits else ""),
    ]
    return "\n".join(lines)
