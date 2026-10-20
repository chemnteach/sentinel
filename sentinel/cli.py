"""
sentinel/cli.py

Command-line interface for Sentinel.

All commands operate against the repository at SENTINEL_REPO_PATH (or
current directory by default). State lives in sentinel/state/ and is
committed to git after each significant step.

Usage:
    sentinel assess <repo_path> [<repo_path> ...]
    sentinel assess --continue              # Resume after triage gate
    sentinel triage --approve <id>          # Approve a tech debt item
    sentinel triage --defer <id>            # Defer a tech debt item
    sentinel triage --exclude <id>          # Exclude a tech debt item
    sentinel triage --batch decisions.json  # Apply a Decision Brief export in one pass
    sentinel report --state-dir <path>      # Render the Decision Brief (HTML + Markdown)
    sentinel triage --list-dependencies      # List dependency candidates
    sentinel triage --include-dependency <id> # Include external dependency in scope
    sentinel triage --defer-dependency <id>   # Defer dependency scope expansion
    sentinel triage --exclude-dependency <id> # Exclude dependency from current cycle
    sentinel plan --show                    # Show the current plan
    sentinel plan --approve                 # Approve the plan → start Phase 2
    sentinel govern --start                 # Start Phase 2 remediation
    sentinel task --merged <task_id>        # Record a merged PR
    sentinel task --list                    # List all tasks and states
    sentinel review --changes               # Show review-required vs auto-accept files
    sentinel dispute --resolve <task_id> --winner architect|coder
    sentinel status                         # Show overall system status
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import click
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

from sentinel.assess.grouper_view import (
    apply_group_triage_decision,
    generate_grouped_triage_view,
    merge_groups,
)
from sentinel.cli_display import display_gate, display_task_table, display_tech_debt_table
from sentinel.cli_phase3 import register_phase3_commands
from sentinel.cli_defect import register_defect_commands
from sentinel.config import load_config
from sentinel.govern.review_policy import classify_changed_files, ReviewPolicyError
from sentinel.orchestrator.completion_checker import (
    COMPLETION_REPORT_FILENAME,
    CompletionChecker,
)
from sentinel.orchestrator.main import HumanGateRequired, Orchestrator

console = Console()
log = logging.getLogger(__name__)

_TECH_DEBT_ID_PATTERN = re.compile(r"^TD-\d{3,5}$")
_DEPENDENCY_ID_PATTERN = re.compile(r"^DEP-[a-f0-9]{12}$")
_SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MAX_SUBMIT_JSON_BYTES = 1_000_000
_MAX_FEEDBACK_CHARS = 1_000
_TECH_DEBT_LOCK_TIMEOUT_SECONDS = 5.0
_TECH_DEBT_LOCK_POLL_SECONDS = 0.05


class TechDebtConflictError(RuntimeError):
    """Raised when a triage write detects an overlapping tech-debt writer."""


class TechDebtStateError(RuntimeError):
    """Raised when tech_debt.json is malformed for triage operations."""


class _DependencyNotFound(RuntimeError):
    """Internal signal: the requested dependency id was not found during an
    atomic phase1_assessment update, so the write is aborted with no change."""


def _is_safe_id(value: str) -> bool:
    return bool(_SAFE_ID_PATTERN.fullmatch(value)) and ".." not in value


def _validate_positive_int(ctx, param, value):
    """Click callback: reject non-positive values for optional count limits.

    ``--limit 0`` (or a negative) would silently process zero tasks; require a
    positive count so the flag can't be a confusing no-op.
    """
    if value is not None and value <= 0:
        raise click.BadParameter("must be a positive integer (>= 1).")
    return value


def _validate_fer_id(value: str, label: str = "FER_ID") -> None:
    if not _is_safe_id(value):
        console.print(
            f"[bold red]Invalid {label} format.[/] "
            "Use 1-64 chars from [A-Za-z0-9._-] and avoid '..'."
        )
        sys.exit(1)


def _resolve_operator_identity() -> str:
    """Resolve operator identity from env or GitHub CLI auth context."""
    explicit = os.environ.get("SENTINEL_OPERATOR", "").strip()
    if explicit:
        return explicit

    actor = os.environ.get("GITHUB_ACTOR", "").strip()
    if actor:
        return actor

    try:
        proc = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=10,
        )
    except OSError:
        return ""

    if proc.returncode == 0:
        return (proc.stdout or "").strip()
    return ""


def _enforce_single_approver_for_pr_actions(cfg, action_label: str) -> None:
    """Block PR approval/merge actions unless performed by designated approver."""
    if not getattr(cfg, "enforce_single_pr_approver", False):
        return

    # ``getattr`` returns the attribute value when it exists, so a config that
    # sets ``required_pr_approver = None`` would reach ``.strip()`` and raise
    # AttributeError, crashing the approver gate instead of reporting a clean
    # "not configured" error. Coerce a missing/None value to "" first.
    required = (getattr(cfg, "required_pr_approver", "") or "").strip()
    if not required:
        console.print(
            "[bold red]Single-approver policy is enabled but not configured.[/] "
            "Set [bold]SENTINEL_REQUIRED_PR_APPROVER[/] to the GitHub login of the designated approver."
        )
        sys.exit(1)

    current = _resolve_operator_identity()
    if not current:
        console.print(
            "[bold red]Could not resolve operator identity for approver policy.[/] "
            "Set [bold]SENTINEL_OPERATOR[/] (or authenticate [bold]gh[/]) and retry."
        )
        sys.exit(1)

    if current.lower() != required.lower():
        console.print(
            f"[bold red]Not authorized to {action_label}.[/] "
            f"Designated approver: [bold]{required}[/]. Current operator: [bold]{current}[/]."
        )
        sys.exit(1)


# ---------------------------------------------------------------------------
# Root group
# ---------------------------------------------------------------------------

@click.group()
@click.version_option(package_name="sentinel")
def main() -> None:
    """Sentinel — multi-agent code governance system."""
    # No TLS bootstrap needed: HTTP clients use the platform trust store\n\1# defaults.
    pass


# ---------------------------------------------------------------------------
# assess
# ---------------------------------------------------------------------------

_CHANGED_SINCE_SOURCE_EXTS = (".py", ".cs")


def _changed_source_files_since(repo_path: Path, since_sha: str) -> list[str]:
    """Return repo-relative ``.py``/``.cs`` files changed since *since_sha*.

    Runs ``git diff --name-only <since_sha> HEAD`` in *repo_path* and filters to
    source files. The commit is validated first so an invalid SHA raises a clear
    error (rather than an empty result indistinguishable from "no changes").

    Raises:
        click.ClickException: If *since_sha* is not a valid commit or git fails.
    """
    import subprocess

    verify = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "--verify", "--quiet",
         f"{since_sha}^{{commit}}"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=30,
    )
    if verify.returncode != 0:
        raise click.ClickException(
            f"Invalid commit SHA '{since_sha}': not a valid commit in "
            f"{repo_path}."
        )

    result = subprocess.run(
        ["git", "-C", str(repo_path), "diff", "--name-only", since_sha, "HEAD"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=30,
    )
    if result.returncode != 0:
        raise click.ClickException(
            f"git diff failed for '{since_sha}': {result.stderr.strip()}"
        )

    changed = [
        line.strip().replace("\\", "/")
        for line in result.stdout.splitlines()
        if line.strip()
    ]
    return [f for f in changed if f.lower().endswith(_CHANGED_SINCE_SOURCE_EXTS)]


@main.command("assess")
@click.argument("repo_paths", nargs=-1, type=click.Path(exists=True, file_okay=False))
@click.option("--continue", "resume", is_flag=True, help="Resume after tech debt triage")
@click.option(
    "--changed-since", "changed_since", default=None, metavar="SHA",
    help="Only assess source files changed since SHA (targeted verification pass)",
)
@click.option(
    "--profile", "profile", default=None,
    type=click.Choice(["full-coverage"]),
    help="Run preset. 'full-coverage' = strict mode, all skills, all lanes, "
         "operational scope, and alarm classification (the handoff default).",
)
@click.option(
    "--scope", "scope", default=None,
    type=click.Choice(["operational", "all"]),
    help="Which files to assess. 'operational' (default) = production/runtime "
         "source only; 'all' = every non-excluded file.",
)
@click.option(
    "--files", "files_csv", default=None, metavar="PATHS",
    help="Comma-separated repo-relative file paths to assess (targeted pass). "
         "Unlike --changed-since, works on any files regardless of git history.",
)
@click.option(
    "--no-alarms", "no_alarms", is_flag=True, default=False,
    help="Disable the alarm / WIP-stall specialist AND alarm classification. "
         "Use for codebases with no alarms (non-tool code).",
)
@click.option(
    "--resume-review", "resume_review", is_flag=True, default=False,
    help="Resume an interrupted Stage 3 review in the SAME state dir: units "
         "whose prior module_review_status entry is review_complete are "
         "skipped (their findings preserved); only incomplete units are "
         "re-reviewed. Use after a provider outage/budget failure.",
)
def assess_cmd(
    repo_paths: tuple[str, ...],
    resume: bool,
    changed_since: str | None,
    profile: str | None,
    scope: str | None,
    files_csv: str | None,
    no_alarms: bool,
    resume_review: bool,
) -> None:
    """
    Run Phase 1: codebase assessment.

    Pass one or more REPO_PATHS to assess. Omit paths with --continue to
    resume after completing tech debt triage. Use --changed-since <sha> to
    restrict the assessment to only source files changed since that commit.
    Use --profile full-coverage for a complete, all-skills, operational-scope
    run in one switch.
    """
    # CLI flags win over ambient env; set before load_config() reads them.
    if profile:
        os.environ["SENTINEL_PROFILE"] = profile
    if scope:
        os.environ["SENTINEL_ASSESSMENT_SCOPE"] = scope
    if no_alarms:
        os.environ["SENTINEL_ENABLE_ALARM_SPECIALIST"] = "false"
        os.environ["SENTINEL_ENABLE_ALARM_CLASSIFICATION"] = "false"
    cfg = load_config()

    try:
        orch_cm = Orchestrator(cfg)
    except EnvironmentError as exc:
        console.print(f"[bold red]Error:[/] {exc}")
        sys.exit(1)

    with orch_cm as orch:
        if resume:
            console.print("[bold cyan]Resuming Phase 1 after triage...[/]")
            try:
                orch.continue_phase1_after_triage()
            except HumanGateRequired as gate:
                display_gate(console, gate)
                sys.exit(0)
            except Exception as exc:
                console.print(f"[bold red]Error:[/] {exc}")
                sys.exit(1)
            return

        if not repo_paths:
            console.print("[bold red]Error:[/] Provide at least one repo path, or use --continue")
            sys.exit(1)

        paths = [Path(p).resolve() for p in repo_paths]

        restricted_files: list[str] | None = None
        # Explicit --files targeting: repo-relative paths, normalized to '/'.
        if files_csv:
            restricted_files = [
                f.strip().replace("\\", "/")
                for f in files_csv.split(",")
                if f.strip()
            ]
            console.print(
                f"[cyan]Targeted assessment: {len(restricted_files)} explicit "
                f"file(s) via --files[/]"
            )
        if changed_since:
            # Union changed files across every repo path, not just the first —
            # otherwise a multi-repo assess would silently ignore changes in
            # paths[1:]. Seed from any --files list so the two can combine.
            union: list[str] = list(restricted_files or [])
            seen: set[str] = set(union)
            for repo in paths:
                for f in _changed_source_files_since(repo, changed_since):
                    if f not in seen:
                        seen.add(f)
                        union.append(f)
            restricted_files = union
            if not restricted_files:
                console.print(
                    f"[yellow]No source files changed since {changed_since}; "
                    f"nothing to assess.[/]"
                )
                sys.exit(0)
            console.print(
                f"[cyan]Targeted assessment: {len(restricted_files)} changed "
                f"source file(s) since {changed_since}[/]"
            )

        console.print(
            Panel(
                f"[bold]Phase 1: Assessment[/]\n\n"
                + "\n".join(f"  • {p}" for p in paths),
                title="Sentinel",
                border_style="cyan",
            )
        )

        try:
            orch.run_phase1(
                paths,
                restricted_files=restricted_files,
                resume_review=resume_review,
            )
        except HumanGateRequired as gate:
            display_gate(console, gate)
            sys.exit(0)
        except Exception as exc:
            console.print(f"[bold red]Error:[/] {exc}")
            raise


# ---------------------------------------------------------------------------
# triage
# ---------------------------------------------------------------------------

# Decision aliases accepted in a batch file; "approve with conditions" collapses
# to APPROVE (the conditions text is carried in the item's `conditions` field).
_BATCH_DECISION_ALIASES = {
    "APPROVE_COND": "APPROVE",
    "APPROVE_WITH_CONDITIONS": "APPROVE",
    "APPROVE WITH CONDITIONS": "APPROVE",
}
_BATCH_VALID_DECISIONS = {"APPROVE", "DEFER", "EXCLUDE"}


def _acquire_tech_debt_lock(lock_path: Path) -> int:
    """Acquire an interprocess lock for triage writes to tech_debt.json."""

    deadline = time.monotonic() + _TECH_DEBT_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            return os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except FileExistsError:
            # Genuine lock contention: another process holds the lock. Retry
            # until the timeout, then report a contention-style failure.
            if time.monotonic() >= deadline:
                raise TechDebtConflictError(
                    "Concurrent tech debt update conflict: lock wait timed out. "
                    "Retry the triage command."
                )
            time.sleep(_TECH_DEBT_LOCK_POLL_SECONDS)
        except PermissionError as exc:
            # A permission/filesystem access failure is deterministic, not
            # contention: surface it immediately as an access error instead of
            # polling until timeout behind a misleading conflict message
            # (FIX-BLOCKING-031).
            raise TechDebtStateError(
                f"Cannot acquire triage lock at {lock_path}: {exc}"
            ) from exc


def _release_tech_debt_lock(lock_path: Path, lock_fd: int) -> None:
    """Release the interprocess lock for triage writes."""

    try:
        os.close(lock_fd)
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
        except PermissionError:
            # Delete-pending races on Windows should not mask the primary result.
            pass


def _atomic_write_json(path: Path, data: object) -> None:
    """Write JSON to *path* via a temp file + atomic replace (UTF-8).

    The document is fully written to a sibling temp file which then atomically
    replaces the target, so a crash mid-write cannot truncate or corrupt the
    existing file.
    """
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent), text=True
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(data, indent=2))
        os.replace(str(temp_path), str(path))
    except BaseException:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def _load_state_json(path: Path, label: str):
    """Load JSON state, reporting corruption/access errors as controlled CLI errors.

    Distinguishes a read/access failure (``OSError``) from a JSON parse failure
    so operators get a clear message naming the file, and neither raises an
    uncaught traceback (FIX-BLOCKING-037 / FIX-BLOCKING-052).
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        console.print(f"[bold red]Could not read {label} ({path}):[/] {exc}")
        sys.exit(1)
    try:
        return json.loads(raw)
    except ValueError as exc:
        console.print(
            f"[bold red]{label} is corrupted (invalid JSON) at {path}:[/] {exc}"
        )
        sys.exit(1)


def _persist_tech_debt_updates(
    tech_debt_file: Path,
    mutator: Callable[[list[dict]], None],
) -> list[dict]:
    """Serialize tech_debt.json updates and persist via temp-file atomic replace."""
    lock_path = tech_debt_file.with_suffix(f"{tech_debt_file.suffix}.lock")
    lock_fd = _acquire_tech_debt_lock(lock_path)
    temp_path: Path | None = None

    try:
        # An unreadable file (OSError) or malformed JSON (ValueError) here would
        # otherwise escape uncaught. Convert both to TechDebtStateError so the
        # existing caller handlers report a clean error and the lock still
        # releases via ``finally``.
        try:
            baseline_text = tech_debt_file.read_text(encoding="utf-8")
            items = json.loads(baseline_text)
        except OSError as exc:
            raise TechDebtStateError(
                f"Cannot read state file {tech_debt_file}: {exc}"
            ) from exc
        except ValueError as exc:
            raise TechDebtStateError(
                f"state file is corrupted (invalid JSON) at {tech_debt_file}: {exc}"
            ) from exc
        if not isinstance(items, list):
            raise TechDebtStateError(
                "state file contains invalid data (expected JSON array)"
            )

        mutator(items)

        fd, temp_name = tempfile.mkstemp(
            prefix=f".{tech_debt_file.name}.",
            suffix=".tmp",
            dir=str(tech_debt_file.parent),
            text=True,
        )
        temp_path = Path(temp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_handle:
            tmp_handle.write(json.dumps(items, indent=2))

        current_text = tech_debt_file.read_text(encoding="utf-8")
        if current_text != baseline_text:
            raise TechDebtConflictError(
                "Concurrent tech debt update conflict detected; "
                "retry the triage command."
            )

        os.replace(str(temp_path), str(tech_debt_file))
        temp_path = None
        return items
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
        _release_tech_debt_lock(lock_path, lock_fd)


def _persist_phase1_assessment_updates(
    phase1_file: Path,
    mutator: Callable[[dict], None],
) -> dict:
    """Serialize phase1_assessment.json updates via a conflict-safe boundary.

    Mirrors ``_persist_tech_debt_updates`` but for the single-object
    phase1_assessment document (FIX-BLOCKING-007): an exclusive lock spans the
    full read-modify-write, the update is written to a temp file and finalized
    with an atomic ``os.replace``, and a stale write is rejected if the on-disk
    file changed after the baseline read.
    """
    lock_path = phase1_file.with_suffix(f"{phase1_file.suffix}.lock")
    lock_fd = _acquire_tech_debt_lock(lock_path)
    temp_path: Path | None = None

    try:
        baseline_text = phase1_file.read_text(encoding="utf-8")
        data = json.loads(baseline_text)
        if not isinstance(data, dict):
            raise TechDebtStateError(
                "phase1_assessment.json is invalid (expected a JSON object)"
            )

        mutator(data)

        fd, temp_name = tempfile.mkstemp(
            prefix=f".{phase1_file.name}.",
            suffix=".tmp",
            dir=str(phase1_file.parent),
            text=True,
        )
        temp_path = Path(temp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_handle:
            tmp_handle.write(json.dumps(data, indent=2))

        current_text = phase1_file.read_text(encoding="utf-8")
        if current_text != baseline_text:
            raise TechDebtConflictError(
                "Concurrent phase1 assessment update conflict detected; "
                "retry the dependency-triage command."
            )

        os.replace(str(temp_path), str(phase1_file))
        temp_path = None
        return data
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
        _release_tech_debt_lock(lock_path, lock_fd)


def _apply_batch_decisions(tech_debt_file: Path, batch_file: Path) -> None:
    """Apply a JSON decisions file to the tech-debt register in one atomic pass.

    The file is a list of objects: {td_id, decision, conditions, rationale}.
    `decision` is APPROVE / DEFER / EXCLUDE ("approve with conditions" collapses
    to APPROVE). All td_ids and decisions are validated BEFORE any write, so a
    bad batch leaves tech_debt.json untouched and exits non-zero.
    """
    try:
        raw_decisions = json.loads(batch_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        console.print(f"[bold red]Could not read decisions file:[/] {exc}")
        sys.exit(1)
    if not isinstance(raw_decisions, list):
        console.print("[bold red]Decisions file must contain a JSON array.[/]")
        sys.exit(1)

    # cycle 52 — validate the decision payload at the boundary (Pydantic) before
    # any write: enforces the decision vocabulary, truncates over-long
    # condition note/target, and requires conditions_structured on AWC.
    from sentinel.govern.conditions import DecisionBatch
    from pydantic import ValidationError
    try:
        batch = DecisionBatch.from_list(raw_decisions)
    except ValidationError as exc:
        console.print(f"[bold red]Invalid decisions file:[/] {exc}")
        sys.exit(1)

    counts = {"APPROVE": 0, "DEFER": 0, "EXCLUDE": 0}

    def _mutate(items: list[dict]) -> None:
        by_id = {i.get("id"): i for i in items}
        missing = [str(fd.td_id) for fd in batch.decisions if fd.td_id not in by_id]
        if missing:
            shown = ", ".join(missing[:10]) + (" ..." if len(missing) > 10 else "")
            raise TechDebtStateError(
                f"{len(missing)} td_id(s) not found in tech_debt.json: {shown}"
            )

        for fd in batch.decisions:
            item = by_id[fd.td_id]
            decision = _BATCH_DECISION_ALIASES.get(fd.decision, fd.decision)
            item["human_triage"] = decision
            if fd.conditions is not None:
                item["conditions"] = fd.conditions
            item["conditions_structured"] = (
                [c.model_dump() for c in fd.conditions_structured]
                if fd.conditions_structured else None
            )
            if fd.rationale is not None:
                item["rationale"] = fd.rationale
            item["conflict"] = fd.conflict
            counts[decision] += 1

    try:
        _persist_tech_debt_updates(tech_debt_file, _mutate)
    except TechDebtConflictError as exc:
        console.print(f"[bold red]{exc}[/]")
        sys.exit(1)
    except TechDebtStateError as exc:
        message = str(exc)
        if "not found in tech_debt.json" in message:
            console.print(f"[bold red]{message}[/]")
        else:
            click.echo(f"Error: {message}", err=True)
        sys.exit(1)

    console.print(
        f"[green]Applied {len(batch.decisions)} decisions:[/] "
        f"{counts['APPROVE']} approved, {counts['DEFER']} deferred, {counts['EXCLUDE']} excluded"
    )


# ---------------------------------------------------------------------------
# Group-aware triage (Phase 1b) — display + decision propagation via
# sentinel.assess.grouper_view. Activated only when semantic_groups.json is
# present alongside tech_debt.json; without it the per-finding paths above run
# unchanged (backward compatible).
# ---------------------------------------------------------------------------


def _display_grouped_triage(console: Console, view: list[dict]) -> None:
    """Render the grouped triage view: one row per semantic group, with a
    corroboration note for multi-member groups (a confidence signal, not
    noise)."""
    from rich.text import Text

    total_findings = sum(int(e.get("member_count") or 0) for e in view)
    console.print(f"{len(view)} group(s) covering {total_findings} finding(s)")

    table = Table(title="Tech Debt - grouped by root cause")
    table.add_column("Group")
    table.add_column("Sev")
    table.add_column("Members", justify="right")
    table.add_column("Triage")
    table.add_column("Description")
    for e in view:
        desc = str(e.get("description") or "").split("\n", 1)[0]
        if len(desc) > 70:
            desc = desc[:67] + "..."
        table.add_row(
            str(e.get("group_id") or ""),
            str(e.get("severity") or ""),
            str(e.get("member_count") or 0),
            str(e.get("human_triage") or "—"),
            Text(desc),
        )
    console.print(table)

    # Corroboration notes on their own lines (markup disabled so the literal
    # square brackets survive and the phrase is never clipped by table layout).
    for e in view:
        if int(e.get("member_count") or 0) > 1:
            console.print(
                f"  {e.get('group_id')}: [Corroborated by "
                f"{e.get('member_count')} findings across "
                f"{len(e.get('skills_corroborating') or [])} skills]",
                markup=False,
            )


def _show_near_misses(
    near_miss_file: Path, tech_debt_file: Path, groups_file: Path
) -> None:
    """Display the 'Possible duplicates' section: near-miss pairs (similarity
    0.70-0.85) the grouper almost merged but didn't, with an interactive
    [M]erge / [K]eep choice per pair. On [M] the two findings' groups are
    merged via ``merge_groups``.
    """
    pairs = json.loads(near_miss_file.read_text(encoding="utf-8"))
    if not isinstance(pairs, list) or not pairs:
        console.print("[dim]No near-miss pairs found.[/]")
        return

    console.print(
        f"[bold]Possible duplicates[/] - {len(pairs)} near-miss pair(s) "
        "(similarity 0.70-0.85) the grouper did not merge:"
    )
    for idx, pair in enumerate(pairs, 1):
        # Re-read groups each pair so cascade merges resolve correctly.
        groups = json.loads(groups_file.read_text(encoding="utf-8"))
        gmap: dict[str, str] = {}
        for g in groups:
            for fid in g.get("member_finding_ids") or []:
                gmap[str(fid)] = g.get("group_id")

        fa = str(pair.get("finding_id_a"))
        fb = str(pair.get("finding_id_b"))
        ga = gmap.get(fa)
        gb = gmap.get(fb)
        da = (pair.get("description_a") or "").replace("\n", " ")[:100]
        db = (pair.get("description_b") or "").replace("\n", " ")[:100]

        console.print(
            f"\nPair {idx} (similarity {pair.get('similarity')}) in "
            f"{pair.get('file', '')}",
            markup=False,
        )
        console.print(f"  A: {fa}  [group {ga}]  {da}", markup=False)
        console.print(f"  B: {fb}  [group {gb}]  {db}", markup=False)
        console.print("  [M] Merge into one group    [K] Keep as distinct", markup=False)

        if ga and gb and ga == gb:
            console.print("  (already in the same group - nothing to merge)", markup=False)
            continue
        if not ga or not gb:
            console.print("  (a finding is not in any group - cannot merge)", markup=False)
            continue

        choice = click.prompt("  Choice", default="K").strip().upper()
        if choice == "M":
            survivor = merge_groups(ga, gb, groups_file, tech_debt_file)
            console.print(f"  Merged group {gb} into {survivor}.", markup=False)
        else:
            console.print("  Kept as distinct.", markup=False)


def _resolve_group_for_td(
    tech_debt_file: Path, groups_file: Path, td_id: str
) -> Optional[str]:
    """Resolve a TD-id to the group_id whose members include that item's
    finding. Returns None if the item or its group can't be found."""
    tech_debt = json.loads(tech_debt_file.read_text(encoding="utf-8"))
    item = next((i for i in tech_debt if i.get("id") == td_id), None)
    if item is None:
        return None
    fid = str(item.get("finding_id") or "")
    if not fid:
        return None
    groups = json.loads(groups_file.read_text(encoding="utf-8"))
    for g in groups:
        if fid in {str(x) for x in (g.get("member_finding_ids") or [])}:
            return g.get("group_id")
    return None


def _sync_group_status(
    base_dir: Path,
    tech_debt_file: Path,
    groups_file: Path,
    group_id: str,
    decision: str,
    conditions_text: str = "",
) -> None:
    """Best-effort sync of finding_status.json after a group triage decision.
    Status tracking is advisory here — a failure must never break an
    already-successful triage, so problems are downgraded to a dim note."""
    from sentinel.assess.finding_status import StatusManager, StatusValue

    status = {
        "APPROVE": StatusValue.APPROVED,
        "DEFER": StatusValue.DEFERRED,
        "EXCLUDE": StatusValue.EXCLUDE,
        "APPROVE_WITH_CONDITIONS": StatusValue.APPROVED_WITH_CONDITIONS,
    }.get(decision, StatusValue.UNTRIAGED)
    approval_type = decision if decision in ("APPROVE", "APPROVE_WITH_CONDITIONS") else None
    try:
        mgr = StatusManager(base_dir, groups_file, tech_debt_file)
        mgr.update(
            group_id=group_id,
            status=status,
            updated_by="user",
            approval_type=approval_type,
            conditions=conditions_text,
        )
    except Exception as exc:  # noqa: BLE001 — status is advisory; never break triage
        # Advisory sync must not break an already-successful triage, but a
        # failure means finding_status.json may now be stale/inconsistent with
        # tech_debt.json, so surface it at error visibility (not a dim note) so
        # the operator can reconcile it rather than silently trusting success.
        console.print(
            f"[bold red]ERROR:[/] finding status not synced for group "
            f"{group_id}; finding_status.json may be stale: {exc}"
        )


def _apply_batch_decisions_grouped(
    tech_debt_file: Path, groups_file: Path, batch_file: Path, base_dir: Path
) -> None:
    """Group-aware batch triage: each decision references a finding (canonical
    OR member finding_id) or a td_id, and the decision propagates to every
    member of that finding's semantic group. Validated and applied atomically
    (reusing ``_persist_tech_debt_updates``) — an id that resolves to no group
    leaves tech_debt.json untouched and exits non-zero.

    After a successful write, finding_status.json is synced (best-effort) for
    every group in the batch, the same way the single-decision path does.
    """
    from pydantic import ValidationError

    from sentinel.govern.conditions import DecisionBatch

    try:
        raw_decisions = json.loads(batch_file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        console.print(f"[bold red]Could not read decisions file:[/] {exc}")
        sys.exit(1)
    if not isinstance(raw_decisions, list):
        console.print("[bold red]Decisions file must contain a JSON array.[/]")
        sys.exit(1)
    try:
        batch = DecisionBatch.from_list(raw_decisions)
    except ValidationError as exc:
        console.print(f"[bold red]Invalid decisions file:[/] {exc}")
        sys.exit(1)

    groups = json.loads(groups_file.read_text(encoding="utf-8"))
    group_by_fid: dict[str, str] = {}
    members_by_gid: dict[str, set[str]] = {}
    for g in groups:
        gid = g.get("group_id")
        members = {str(x) for x in (g.get("member_finding_ids") or [])}
        members_by_gid[gid] = members
        for fid in members:
            group_by_fid[fid] = gid

    counts = {"APPROVE": 0, "APPROVE_WITH_CONDITIONS": 0, "DEFER": 0, "EXCLUDE": 0}
    stats = {"groups": 0, "findings": 0}
    # (group_id, decision, conditions) per applied group, for the status sync
    # below. Reset each _mutate call so a persistence retry never duplicates.
    applied: list[tuple[str, str, str]] = []

    def _mutate(items: list[dict]) -> None:
        applied.clear()
        fid_by_tdid = {i.get("id"): str(i.get("finding_id") or "") for i in items}
        td_by_fid: dict[str, list[dict]] = defaultdict(list)
        for i in items:
            td_by_fid[str(i.get("finding_id") or "")].append(i)

        plan: list = []
        unresolved: list[str] = []
        for fd in batch.decisions:
            fid = str(fd.finding_id or "") or fid_by_tdid.get(fd.td_id, "")
            gid = group_by_fid.get(fid)
            if not gid:
                unresolved.append(str(fd.finding_id or fd.td_id or "?"))
                continue
            plan.append((gid, fd))
        if unresolved:
            shown = ", ".join(unresolved[:10]) + (" ..." if len(unresolved) > 10 else "")
            raise TechDebtStateError(
                f"{len(unresolved)} id(s) not found in any semantic group: {shown}"
            )

        for gid, fd in plan:
            conditions = fd.conditions or ""
            if fd.decision == "APPROVE_WITH_CONDITIONS":
                cs = (
                    [c.model_dump() for c in fd.conditions_structured]
                    if fd.conditions_structured
                    else [{"type": "APPROACH_DIRECTIVE", "target": None, "note": conditions}]
                )
            else:
                cs = None
            # The DECISION (human_triage/rationale) fans out to every group
            # member — group-level triage semantics locked by test_triage_cli.
            # But the CONDITIONS are scoped to the decision's OWN finding, so a
            # per-finding "Approve with conditions" no longer overwrites its group
            # peers' conditions. Phase 2 reconciles the per-finding conditions per
            # task via merge_conditions(); a task's constraint set is the merge of
            # its member findings' conditions, so a single owner's conditions still
            # reach the whole task.
            own_fid = str(fd.finding_id or "") or fid_by_tdid.get(fd.td_id, "")
            for fid in members_by_gid.get(gid, set()):
                is_owner = fid == own_fid
                for item in td_by_fid.get(fid, []):
                    item["human_triage"] = fd.decision
                    if fd.rationale:
                        item["rationale"] = fd.rationale
                    if is_owner:
                        if conditions:
                            item["conditions"] = conditions
                        item["conditions_structured"] = cs
                    stats["findings"] += 1
            counts[fd.decision] += 1
            stats["groups"] += 1
            applied.append((gid, fd.decision, conditions))

    try:
        _persist_tech_debt_updates(tech_debt_file, _mutate)
    except TechDebtConflictError as exc:
        console.print(f"[bold red]{exc}[/]")
        sys.exit(1)
    except TechDebtStateError as exc:
        message = str(exc)
        if "not found in any semantic group" in message:
            console.print(f"[bold red]{message}[/]")
        else:
            click.echo(f"Error: {message}", err=True)
        sys.exit(1)

    console.print(
        f"[green]Applied {stats['groups']} group decision(s)[/] to "
        f"{stats['findings']} finding(s): "
        f"{counts['APPROVE']} approved, "
        f"{counts['APPROVE_WITH_CONDITIONS']} approved-with-conditions, "
        f"{counts['DEFER']} deferred, {counts['EXCLUDE']} excluded"
    )

    # Sync finding_status.json for every group in the batch (best-effort, the
    # same way the single-decision path does via _sync_group_status).
    for gid, decision, conditions in applied:
        _sync_group_status(base_dir, tech_debt_file, groups_file, gid, decision, conditions)


@main.command("triage")
@click.option("--approve", "approve_id", default=None, metavar="ID", help="Approve item")
@click.option("--defer", "defer_id", default=None, metavar="ID", help="Defer item")
@click.option("--exclude", "exclude_id", default=None, metavar="ID", help="Exclude item")
@click.option("--approve-with-conditions", "approve_with_conditions_id", default=None,
              metavar="GROUP_ID",
              help="Approve a group with conditions (prompts for the conditions text)")
@click.option("--list", "list_items", is_flag=True, help="List tech debt items")
@click.option("--show-near-misses", "show_near_misses", is_flag=True,
              help="Show possible-duplicate near-miss pairs (interactive merge) before the triage list")
@click.option("--list-dependencies", "list_dependencies", is_flag=True, help="List dependency scope candidates")
@click.option("--include-dependency", "include_dependency_id", default=None, metavar="ID", help="Include dependency in current scope")
@click.option("--defer-dependency", "defer_dependency_id", default=None, metavar="ID", help="Defer dependency scope expansion")
@click.option("--exclude-dependency", "exclude_dependency_id", default=None, metavar="ID", help="Exclude dependency from current cycle")
@click.option("--dependency-rationale", default="", help="Optional rationale for dependency decision")
@click.option("--dependency-owner", default="", help="Optional owner for dependency follow-up")
@click.option("--batch", "batch_file", type=click.Path(exists=True), default=None,
              help="Apply a JSON decisions file ([{td_id,decision,conditions,rationale}]) in one pass")
@click.option("--state-dir", "state_dir", type=click.Path(exists=True), default=None,
              help="State directory containing tech_debt.json (defaults to configured state dir)")
def triage_cmd(
    approve_id: Optional[str],
    defer_id: Optional[str],
    exclude_id: Optional[str],
    approve_with_conditions_id: Optional[str],
    list_items: bool,
    show_near_misses: bool,
    list_dependencies: bool,
    include_dependency_id: Optional[str],
    defer_dependency_id: Optional[str],
    exclude_dependency_id: Optional[str],
    dependency_rationale: str,
    dependency_owner: str,
    batch_file: Optional[str],
    state_dir: Optional[str],
) -> None:
    """Triage tech debt items from Phase 1."""
    base_dir = Path(state_dir) if state_dir else load_config().state_dir
    tech_debt_file = base_dir / "tech_debt.json"

    if not tech_debt_file.exists():
        console.print("[bold red]No tech debt register found.[/] Run Phase 1 first.")
        sys.exit(1)

    if batch_file:
        groups_file = base_dir / "semantic_groups.json"
        if groups_file.exists():
            _apply_batch_decisions_grouped(tech_debt_file, groups_file, Path(batch_file), base_dir)
        else:
            _apply_batch_decisions(tech_debt_file, Path(batch_file))
        return

    if show_near_misses:
        groups_file = base_dir / "semantic_groups.json"
        if not groups_file.exists():
            console.print("[bold red]No semantic groups found.[/] Run Phase 1 grouping first.")
            sys.exit(1)
        near_miss_file = base_dir / "semantic_groups_near_misses.json"
        if near_miss_file.exists():
            _show_near_misses(near_miss_file, tech_debt_file, groups_file)
        else:
            console.print("[dim]No near-miss file found for this run.[/]")
        # Then show the main grouped triage list.
        view = generate_grouped_triage_view(tech_debt_file, groups_file)
        _display_grouped_triage(console, view)
        return

    phase1_assessment_file = base_dir / "phase1_assessment.json"
    phase1_assessment = {}
    if phase1_assessment_file.exists():
        phase1_assessment = json.loads(phase1_assessment_file.read_text(encoding="utf-8"))

    if list_items:
        items = json.loads(tech_debt_file.read_text(encoding="utf-8"))
        if not isinstance(items, list):
            click.echo("Error: state file contains invalid data (expected JSON array)", err=True)
            raise SystemExit(1)
        groups_file = base_dir / "semantic_groups.json"
        if groups_file.exists():
            view = generate_grouped_triage_view(tech_debt_file, groups_file)
            _display_grouped_triage(console, view)
        else:
            display_tech_debt_table(console, items)
        return

    if list_dependencies:
        if not isinstance(phase1_assessment, dict) or not phase1_assessment:
            console.print("[bold red]No Phase 1 assessment report found.[/] Run Phase 1 first.")
            sys.exit(1)
        dependency_items = phase1_assessment.get("dependencies", {}).get("cross_repo_candidates", [])
        table = Table(title="Dependency Scope Candidates")
        table.add_column("ID")
        table.add_column("Severity")
        table.add_column("Source")
        table.add_column("Target")
        table.add_column("Decision")
        if dependency_items:
            for dep in dependency_items:
                table.add_row(
                    str(dep.get("dependency_id", "")),
                    str(dep.get("severity", "")),
                    str(dep.get("source", "")),
                    str(dep.get("target", "")),
                    str(dep.get("triage_decision", "")) or "PENDING",
                )
        else:
            table.add_row("(none)", "", "", "", "")
        console.print(table)
        return

    dep_action_id = include_dependency_id or defer_dependency_id or exclude_dependency_id
    if dep_action_id:
        if not isinstance(phase1_assessment, dict) or not phase1_assessment:
            console.print("[bold red]No Phase 1 assessment report found.[/] Run Phase 1 first.")
            sys.exit(1)
        if not _DEPENDENCY_ID_PATTERN.fullmatch(dep_action_id):
            console.print(
                "[bold red]Invalid dependency ID format.[/] "
                "Use format [bold]DEP-<12 hex chars>[/]."
            )
            sys.exit(1)

        dep_decision = (
            "INCLUDE"
            if include_dependency_id
            else ("DEFER" if defer_dependency_id else "EXCLUDE")
        )

        # Apply the dependency-triage mutation to phase1_assessment.json inside
        # one conflict-safe atomic boundary (lock + temp-file atomic replace +
        # stale-write rejection) rather than an unguarded read/write_text
        # sequence (FIX-BLOCKING-007).
        result: dict[str, bool] = {"updated": False}

        def _mutate_phase1(assessment: dict) -> None:
            all_items = assessment.get("dependencies", {}).get("items", [])
            for dep in all_items:
                if dep.get("dependency_id") == dep_action_id:
                    dep["triage_decision"] = dep_decision
                    dep["triage_rationale"] = dependency_rationale
                    dep["triage_owner"] = dependency_owner
                    if dep_decision == "INCLUDE":
                        dep["access_status"] = "approved_for_scope_expansion"
                    elif dep_decision == "DEFER":
                        dep["access_status"] = "deferred_by_customer"
                    else:
                        dep["access_status"] = "excluded_by_customer"
                    result["updated"] = True
                    break

            external_items = assessment.get("dependencies", {}).get("cross_repo_candidates", [])
            for dep in external_items:
                if dep.get("dependency_id") == dep_action_id:
                    dep.update(next((d for d in all_items if d.get("dependency_id") == dep_action_id), dep))
                    result["updated"] = True
                    break

            queue = assessment.get("review_scope_queue", {}).get("external_repo_candidates", [])
            for entry in queue:
                dep = entry.get("dependency", {})
                if dep.get("dependency_id") == dep_action_id:
                    entry["dependency"] = next((d for d in all_items if d.get("dependency_id") == dep_action_id), dep)
                    if dep_decision == "INCLUDE":
                        entry["access_status"] = "approved_for_scope_expansion"
                        entry["next_action"] = "run_phase1_on_referenced_repo"
                    elif dep_decision == "DEFER":
                        entry["access_status"] = "deferred_by_customer"
                        entry["next_action"] = "revisit_in_next_cycle"
                    else:
                        entry["access_status"] = "excluded_by_customer"
                        entry["next_action"] = "no_action_in_current_cycle"

            if not result["updated"]:
                # Nothing to persist — abort the atomic write without changes.
                raise _DependencyNotFound(dep_action_id)

            assessment.setdefault("summary", {})["untriaged_external_dependencies"] = sum(
                1
                for dep in external_items
                if str(dep.get("triage_decision", "")).strip().upper()
                not in {"INCLUDE", "DEFER", "EXCLUDE"}
            )

        try:
            _persist_phase1_assessment_updates(phase1_assessment_file, _mutate_phase1)
        except _DependencyNotFound:
            console.print(f"[bold red]Dependency '{dep_action_id}' not found.[/]")
            sys.exit(1)
        except TechDebtConflictError as exc:
            console.print(f"[bold red]{exc}[/]")
            sys.exit(1)

        console.print(f"[green]✓[/] Dependency {dep_action_id}: {dep_decision}")
        return

    if approve_with_conditions_id:
        groups_file = base_dir / "semantic_groups.json"
        if not groups_file.exists():
            console.print(
                "[bold red]No semantic groups found.[/] "
                "APPROVE_WITH_CONDITIONS operates on a group; run Phase 1 grouping first."
            )
            sys.exit(1)
        conditions = click.prompt("Enter conditions for this approval").strip()
        try:
            n = apply_group_triage_decision(
                approve_with_conditions_id, "APPROVE_WITH_CONDITIONS",
                "", conditions, tech_debt_file, groups_file,
            )
        except ValueError as exc:
            console.print(f"[bold red]{exc}[/]")
            sys.exit(1)
        _sync_group_status(
            base_dir, tech_debt_file, groups_file, approve_with_conditions_id,
            "APPROVE_WITH_CONDITIONS", conditions,
        )
        console.print(
            f"[green]✓[/] Group {approve_with_conditions_id}: "
            f"APPROVE_WITH_CONDITIONS — {n} finding(s) updated"
        )
        return

    action_id = approve_id or defer_id or exclude_id
    if not action_id:
        console.print(
            "Specify --approve/--defer/--exclude/--list "
            "or dependency options (--list-dependencies, --include-dependency, --defer-dependency, --exclude-dependency)."
        )
        sys.exit(1)

    if not _TECH_DEBT_ID_PATTERN.fullmatch(action_id):
        console.print(
            "[bold red]Invalid tech debt ID format.[/] "
            "Use format [bold]TD-0001[/] (TD- + 3-5 digits)."
        )
        sys.exit(1)

    decision = "APPROVE" if approve_id else ("DEFER" if defer_id else "EXCLUDE")

    groups_file = base_dir / "semantic_groups.json"
    if groups_file.exists():
        gid = _resolve_group_for_td(tech_debt_file, groups_file, action_id)
        if gid is None:
            console.print(
                f"[bold red]Item '{action_id}' not found in any semantic group.[/]"
            )
            sys.exit(1)
        try:
            n = apply_group_triage_decision(
                gid, decision, "", "", tech_debt_file, groups_file
            )
        except ValueError as exc:
            console.print(f"[bold red]{exc}[/]")
            sys.exit(1)
        _sync_group_status(base_dir, tech_debt_file, groups_file, gid, decision)
        console.print(
            f"[green]✓[/] Group {gid} (via {action_id}): {decision} — "
            f"{n} finding(s) updated"
        )
        items = json.loads(tech_debt_file.read_text(encoding="utf-8"))
        untriaged = sum(1 for i in items if i.get("human_triage") is None)
        if untriaged:
            console.print(f"  {untriaged} item(s) still need triage")
        else:
            console.print(
                "[bold green]All items triaged.[/] "
                "Run [bold]sentinel assess --continue[/] to proceed."
            )
        return

    def _mutate_single(items: list[dict]) -> None:
        for item in items:
            if item.get("id") == action_id:
                item["human_triage"] = decision
                return
        raise TechDebtStateError(f"Item '{action_id}' not found.")

    try:
        items = _persist_tech_debt_updates(tech_debt_file, _mutate_single)
    except TechDebtConflictError as exc:
        console.print(f"[bold red]{exc}[/]")
        sys.exit(1)
    except TechDebtStateError as exc:
        message = str(exc)
        if message.startswith("Item '"):
            console.print(f"[bold red]{message}[/]")
        else:
            click.echo(f"Error: {message}", err=True)
        sys.exit(1)

    console.print(f"[green]✓[/] Item {action_id}: {decision}")

    # Show remaining untriaged count
    untriaged = sum(1 for i in items if i.get("human_triage") is None)
    if untriaged:
        console.print(f"  {untriaged} item(s) still need triage")
    else:
        console.print(
            "[bold green]All items triaged.[/] "
            "Run [bold]sentinel assess --continue[/] to proceed."
        )


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------

@main.command("report")
@click.option("--state-dir", "state_dir", required=True, type=click.Path(exists=True),
              help="Run state directory (findings.json, semantic_groups.json, "
                   "remediation_patterns.json, tech_debt.json)")
@click.option("--output-html", "output_html", type=click.Path(), default=None,
              help="HTML output path (default: <state-dir>/brief.html)")
@click.option("--output-md", "output_md", type=click.Path(), default=None,
              help="Markdown output path (default: <state-dir>/brief.md)")
@click.option("--run-label", "run_label", default=None,
              help="Label shown in the brief H1/title (default: the state-dir name)")
@click.option("--synthesize", "synthesize", is_flag=True, default=False,
              help="Add a model-generated 'proposed fix pattern' summary to each "
                   "row with more than 3 patterns (one LLM call per row)")
@click.option("--domain", "domain", default="general",
              help="Consequence-spectrum vocabulary (a key in consequences.yaml). "
                   "Default: general")
@click.option("--domain-file", "domain_file", type=click.Path(), default=None,
              help="Override path to a custom consequences YAML file")
@click.option("--repo-root", "repo_root", default=None,
              help="Filesystem root prepended to each finding's file path in the "
                   "'View changes' VS Code deep-link. Defaults to the current "
                   "working directory when omitted.")
@click.option("--github-url", "github_url", default="",
              help="Base URL of the GitHub repo (https://host/org/repo). When set with "
                   "--github-ref, each finding gets a commit-pinned 'View on GitHub' "
                   "permalink for handing the brief to the code owner.")
@click.option("--github-ref", "github_ref", default="",
              help="Commit SHA (or ref) to pin GitHub permalinks to. Required with "
                   "--github-url; use the assessed SHA so #L line anchors stay accurate.")
@click.option("--github-path-prefix", "github_path_prefix", default="",
              help="Repo-root-relative prefix to prepend to each finding's file in the "
                   "GitHub permalink. Use when the assessment scanned a SUBTREE of the "
                   "repo (e.g. 'Contoso.Line.Tool') so finding paths are subtree-relative; "
                   "without it those permalinks 404. Leave empty when the scan root was "
                   "the repo root.")
@click.option("--handoff", "generate_handoff_doc", is_flag=True, default=True,
              help="Generate handoff.html alongside brief")
@click.option("--graph-json", "graph_json", type=click.Path(exists=True), default=None,
              help="Path to a graphify graph.json. When given, the handoff gains a "
                   "'Structural Health' section (coupling / modularity / circular "
                   "deps) — the architecture complement to the defect findings.")
@click.option("--no-vscode-links", "no_vscode_links", is_flag=True, default=False,
              help="Omit the 'open in VS Code' deep-links on finding locations. "
                   "Use for a portable/offline brief (e.g. handed to a code owner "
                   "on a shared drive): vscode:// links only resolve where the "
                   "repo sits at --repo-root and fail on UNC paths. GitHub "
                   "permalinks and the inline 'View changes' source still work.")
@click.option("--preview-diffs", "preview_diffs", is_flag=True, default=False,
              help="Generate ILLUSTRATIVE diffs (one LLM call per unit, cached at "
                   "<state-dir>/diff_preview_cache.json) so the code owner sees a "
                   "potential change. Anchors on remediation PATTERNS on the triage "
                   "brief (shown in the per-finding 'View changes' panel), or on "
                   "bundles once they exist post-triage. Opt-in only; the diff is a "
                   "preview to gauge impact — NOT the governed Phase 2 change — and "
                   "is excluded from the exported decisions JSON.")
@click.pass_context
def report_cmd(
    ctx: click.Context,
    state_dir: str,
    output_html: Optional[str],
    output_md: Optional[str],
    run_label: Optional[str],
    synthesize: bool,
    domain: str,
    domain_file: Optional[str],
    repo_root: Optional[str],
    github_url: str,
    github_ref: str,
    github_path_prefix: str,
    generate_handoff_doc: bool,
    graph_json: Optional[str],
    no_vscode_links: bool,
    preview_diffs: bool,
) -> None:
    """Render the Remediation Decision Brief (HTML + Markdown) for a run."""
    from sentinel.reports.decision_brief import assessed_commit, render

    # S4-9: auto-enable synthesis from a run's domain_context.json (written by
    # `sentinel scope`). This only fires when the operator did NOT pass
    # --synthesize on the command line. Explicitness is detected via Click's
    # parameter source: COMMANDLINE means the operator typed `--synthesize`, so
    # their choice (True) wins and auto-detect is skipped. When --synthesize was
    # not typed, the source is DEFAULT and auto-detect may enable it. An
    # explicit --domain likewise wins over the file's recorded domain.
    synth_source = ctx.get_parameter_source("synthesize")
    synth_explicit = synth_source == click.core.ParameterSource.COMMANDLINE
    domain_explicit = (
        ctx.get_parameter_source("domain")
        == click.core.ParameterSource.COMMANDLINE
    )
    domain_ctx_file = Path(state_dir) / "domain_context.json"
    config = load_config()
    if (
        not synth_explicit
        and config.auto_synthesize
        and domain_ctx_file.exists()
    ):
        log.info("Auto-enabling synthesis from domain_context.json")
        synthesize = True
        if not domain_explicit:
            try:
                domain = json.loads(
                    domain_ctx_file.read_text(encoding="utf-8")
                ).get("domain", "general")
            except (OSError, ValueError):
                domain = "general"

    # Resolve the ref once (explicit --github-ref, else the assessed commit from
    # scan_metadata) and pass that *resolved* value to render. Previously render
    # received the raw github_ref, so an assessed-commit fallback was logged but
    # never actually applied to the permalinks.
    ref = github_ref
    if github_url:
        ref = github_ref or assessed_commit(state_dir)
        if ref:
            source = "explicit" if github_ref else "assessed commit from scan_metadata"
            console.print(f"[dim]GitHub permalinks pinned to {ref[:12]} ({source}).[/]")
        else:
            console.print(
                "[yellow]Warning:[/] --github-url set but no commit ref found "
                "(none passed and scan_metadata has no commit_sha); GitHub "
                "permalinks will be skipped. Pass --github-ref <commit-sha>."
            )

    out_html, out_md, total, rows = render(
        state_dir, output_html, output_md, run_label, use_synthesis=synthesize,
        domain=domain, domain_file=domain_file, repo_root=repo_root,
        github_url=github_url, github_ref=ref, vscode_links=not no_vscode_links,
        preview_diffs=preview_diffs, github_path_prefix=github_path_prefix)
    console.print(f"[green]Wrote[/] {out_html}")
    console.print(f"[green]Wrote[/] {out_md}")

    # S4-10 — one-page operator handoff document, written into the run state
    # dir (alongside brief.html) so the brief's handoff-link resolves and
    # /handoff.html can serve it.
    if generate_handoff_doc:
        from sentinel.reports.handoff import generate_handoff

        out_handoff = Path(state_dir) / "handoff.html"
        out_handoff.write_text(
            generate_handoff(state_dir, load_config(), graph_json=graph_json),
            encoding="utf-8",
        )
        console.print(f"[green]Wrote[/] {out_handoff}")

    console.print(f"{total} findings -> {rows} decision rows")


# ---------------------------------------------------------------------------
# serve — Phase 3 Flask UI (Screens 2-5)
# ---------------------------------------------------------------------------

@main.command("serve")
@click.option("--state-dir", "state_dir", required=True, type=click.Path(exists=True),
              help="Run state directory the UI reads from (findings, finding_status, "
                   "batch_summary, brief.html, etc.)")
@click.option("--port", "port", default=5000, type=int, help="Port to bind (default 5000)")
@click.option("--host", "host", default="127.0.0.1", help="Host to bind (default 127.0.0.1)")
@click.option(
    "--unsafe-expose",
    "unsafe_expose",
    is_flag=True,
    default=False,
    help="Allow binding to a non-loopback host (INSECURE: the dev server has no "
         "auth/TLS). Only use on a trusted, isolated network.",
)
def serve_cmd(state_dir: str, port: int, host: str, unsafe_expose: bool) -> None:
    """Serve the Sentinel remediation UI (Screens 2-5) for a run."""
    from sentinel.reports.server import create_app

    # The built-in Flask server is development-only: no authentication, TLS, or
    # request-timeout protections. Default to loopback and refuse to bind to a
    # non-loopback interface unless the operator explicitly opts in via
    # --unsafe-expose (FIX-BLOCKING-069).
    _LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
    if host not in _LOOPBACK_HOSTS and not unsafe_expose:
        console.print(
            f"[bold red]Refusing to bind the development UI to non-loopback host "
            f"'{host}'.[/] The built-in server has no authentication or TLS. "
            "Bind to 127.0.0.1, or pass [bold]--unsafe-expose[/] to override on a "
            "trusted, isolated network."
        )
        sys.exit(1)
    if host not in _LOOPBACK_HOSTS:
        console.print(
            "[yellow]WARNING:[/] serving on a non-loopback interface. The built-in "
            "server is development-only and not intended for unauthenticated "
            "network exposure."
        )

    # Load config so LLM calls (Screen 2 /api/recommend) can resolve the client.
    load_config()
    app = create_app(state_dir)
    console.print(f"Sentinel UI: http://{host}:{port}")
    app.run(host=host, port=port)


# ---------------------------------------------------------------------------
# scope — Screen 0 rule-based scope recommendation
# ---------------------------------------------------------------------------

# Source-file extension → (domain, language). A repo is classified by whichever
# of these extensions has the most files; anything else falls back to "general".
_DOMAIN_BY_EXTENSION = {
    ".cs": ("manufacturing_csharp", "C#"),
    ".py": ("software_python", "Python"),
}


def _detect_domain(repo_path: str) -> tuple[str, str]:
    """Classify a repo's domain/language by dominant source-file extension.

    Returns ``(domain, language)``. C#-dominant → ("manufacturing_csharp",
    "C#"); Python-dominant → ("software_python", "Python"); otherwise
    ("general", "unknown").
    """
    counts: dict[str, int] = {ext: 0 for ext in _DOMAIN_BY_EXTENSION}
    for path in Path(repo_path).rglob("*"):
        if path.is_file():
            suffix = path.suffix.lower()
            if suffix in counts:
                counts[suffix] += 1

    dominant = max(counts, key=lambda ext: counts[ext])
    if counts[dominant] == 0:
        return ("general", "unknown")
    return _DOMAIN_BY_EXTENSION[dominant]


@main.command("scope")
@click.option("--repo-path", "repo_path", required=True, type=click.Path(exists=True),
              help="Path to the repository to recommend an assessment scope for")
@click.option("--output", "output", default=None,
              help="Where to write scope_config.yaml "
                   "(default: <repo_path>/scope_config.yaml)")
def scope_cmd(repo_path: str, output: Optional[str]) -> None:
    """Recommend which directories to include/exclude before `sentinel assess`.

    Rule-based (no model calls): scans the repo, prints a table of recommended
    exclusions, and writes scope_config.yaml.
    """
    import yaml

    from sentinel.assess.scope_recommender import recommend_scope

    rec = recommend_scope(repo_path)

    table = Table(title=f"Scope recommendation - {rec.repo_path}")
    table.add_column("Path", style="cyan", no_wrap=True)
    table.add_column("Category")
    table.add_column("Files", justify="right")
    table.add_column("Confidence")
    table.add_column("Reason")
    for e in rec.recommended_excludes:
        table.add_row(e.path, e.category, str(e.file_count), e.confidence, e.reason)
    console.print(table)
    console.print(
        f"[dim]{rec.total_files} files, {rec.total_lines} lines. "
        f"In scope: {', '.join(rec.recommended_includes) or '(all)'}[/]"
    )

    if not output:
        output = str(Path(repo_path) / "scope_config.yaml")

    data = {
        "repo_path": rec.repo_path,
        "generated_at": rec.generated_at,
        "exclude": [
            {
                "path": e.path,
                "reason": e.reason,
                "category": e.category,
                "confidence": e.confidence,
            }
            for e in rec.recommended_excludes
        ],
        "include": list(rec.recommended_includes),
    }
    Path(output).write_text(
        yaml.safe_dump(data, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    console.print(f"[green]Wrote scope config to[/] {output}")

    # S4-9: also record a domain_context.json so `sentinel report` can
    # auto-enable synthesis with the detected domain. Written next to
    # scope_config.yaml, and (if distinct) into the configured state dir.
    domain, language = _detect_domain(repo_path)
    context = {
        "domain": domain,
        "repo_path": rec.repo_path,
        "language": language,
        "created": datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
    }
    context_json = json.dumps(context, indent=2)
    targets = {Path(output).resolve().parent}
    try:
        targets.add(load_config().state_dir.resolve())
    except Exception:  # noqa: BLE001 — config is best-effort here
        pass
    for target_dir in targets:
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / "domain_context.json").write_text(
                context_json, encoding="utf-8"
            )
            console.print(
                f"[green]Wrote domain context to[/] "
                f"{target_dir / 'domain_context.json'} "
                f"[dim](domain={domain})[/]"
            )
        except OSError:
            continue


# ---------------------------------------------------------------------------
# graphify — Phase 5 code knowledge graph builder
# ---------------------------------------------------------------------------

_GRAPHIFY_DEFAULT_EXCLUDES = ("*.md", "*.yaml", "*.yml", "*.json", "*.txt")


@main.command("graphify")
@click.option("--repo-path", "repo_path", required=True, type=click.Path(exists=True),
              help="Path to the codebase to graph")
@click.option("--out-dir", "out_dir", default=None,
              help="Where to write the graph output "
                   "(default: D:/Sentinel-Runs/graphify/<repo basename>/)")
@click.option("--exclude", "excludes", multiple=True,
              help="Glob pattern to exclude (repeatable). "
                   "Default: *.md *.yaml *.json *.txt")
@click.option("--no-name", "no_name", is_flag=True, default=False,
              help="Skip semantic community naming (keeps 'Community N' "
                   "placeholders; no model calls).")
def graphify_cmd(repo_path: str, out_dir: Optional[str], excludes: tuple[str, ...],
                 no_name: bool) -> None:
    """Build a Graphify knowledge graph of a codebase for the /graph UI.

    Post-processes graphify's raw output into a self-contained, offline-ready
    graph: vendors vis-network.min.js locally (graphify's CDN reference + SRI/CORS
    attrs blank the graph on a file://+UNC open) and names each community from its
    member files via the LLM client (graphify itself is run with --no-label).
    """
    patterns = list(excludes) or list(_GRAPHIFY_DEFAULT_EXCLUDES)
    if not out_dir:
        base = Path(repo_path).resolve().name
        out_dir = f"D:/Sentinel-Runs/graphify/{base}"

    exclude_args: list[str] = []
    for p in patterns:
        exclude_args += ["--exclude", p]

    extract_cmd = [sys.executable, "-m", "graphify", "extract", repo_path,
                   *exclude_args, "--output", out_dir]
    cluster_cmd = [sys.executable, "-m", "graphify", "cluster-only", repo_path,
                   "--no-label", "--output", out_dir]

    # Stream live (no capture) so the user sees Graphify's progress. Each step
    # must succeed before the next runs; a non-zero exit is surfaced as a clear
    # error and aborts. Graphify can be slow on large repos — allow 5 min each.
    console.print(f"[dim]$ {' '.join(extract_cmd)}[/]")
    if subprocess.run(extract_cmd, timeout=300).returncode != 0:
        console.print("[bold red]Graphify extract failed.[/] Graph not written.")
        sys.exit(1)
    console.print(f"[dim]$ {' '.join(cluster_cmd)}[/]")
    if subprocess.run(cluster_cmd, timeout=300).returncode != 0:
        console.print("[bold red]Graphify cluster-only failed.[/] Graph not written.")
        sys.exit(1)

    # Locate the real output. graphify writes to <repo>/graphify-out regardless
    # of --output, so prefer that, then fall back to out_dir.
    repo_out = Path(repo_path) / "graphify-out"
    graph_src = next(
        (c for c in (repo_out, Path(out_dir)) if (c / "graph.html").exists()), None
    )
    if graph_src is None:
        console.print("[bold red]Graphify produced no graph.html.[/]")
        sys.exit(1)

    # Post-process: offline-ready + semantic community names (best-effort; a
    # naming failure leaves a usable, offline graph rather than aborting).
    from sentinel.reports.graph_postprocess import postprocess_graph
    client = None
    if not no_name:
        try:
            cfg = load_config()
            if not getattr(cfg, "local_only_mode", False):
                from sentinel.api.client import create_model_client
                client = create_model_client(cfg)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[yellow]Community naming skipped (no model client): {exc}[/]")
    summary = postprocess_graph(graph_src, client=client)

    # Move artifacts into out_dir and clean up the repo-local graphify-out so the
    # target repository is not polluted by build output.
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    if graph_src.resolve() != out.resolve():
        for f in graph_src.iterdir():
            if f.is_file():
                shutil.copyfile(f, out / f.name)
        if (graph_src.name == "graphify-out"
                and graph_src.parent.resolve() == Path(repo_path).resolve()):
            shutil.rmtree(graph_src, ignore_errors=True)

    console.print(f"[green]Graph written to[/] {out}")
    console.print(
        f"  offline-ready: {summary['offline']} | "
        f"communities named: {summary['named']}"
    )
    console.print("Open graph.html directly in a browser, or `sentinel serve` -> /graph.")


# ---------------------------------------------------------------------------
# patterns — targeted Stage-5b re-run
# ---------------------------------------------------------------------------

@main.command("patterns")
@click.option("--state-dir", "state_dir", required=True, type=click.Path(exists=True),
              help="Run state directory to regenerate remediation_patterns.json for")
def patterns_cmd(state_dir: str) -> None:
    """Re-run Stage 5b: regenerate remediation pattern proposals for a run.

    Reads semantic_groups.json + findings from the state dir and rewrites
    remediation_patterns.json — useful when Stage 5b failed mid-assessment
    (e.g. an LLM quota burst) but the rest of Phase 1 is intact.
    """
    cfg = load_config()
    cfg.state_dir = Path(state_dir)

    try:
        orch_cm = Orchestrator(cfg)
    except EnvironmentError as exc:
        console.print(f"[bold red]Error:[/] {exc}")
        sys.exit(1)

    groups_file = Path(state_dir) / "semantic_groups.json"
    group_count = 0
    if groups_file.exists():
        try:
            group_count = len(json.loads(groups_file.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            group_count = 0
    console.print(f"Generating patterns for {group_count} groups...")

    with orch_cm as orch:
        try:
            orch._run_remediation_pattern_proposals()
        except Exception as exc:  # noqa: BLE001
            # A quota error surfaces wrapped (ModelClient APIError -> AgentError),
            # so match on the message rather than the exception type.
            message = str(exc)
            if "limit_type" in message or "window_days" in message:
                console.print(
                    "[bold red]LLM quota exceeded — re-run sentinel patterns "
                    "when quota resets[/]"
                )
            else:
                console.print(f"[bold red]Error:[/] {exc}")
            sys.exit(1)

    patterns_file = Path(state_dir) / "remediation_patterns.json"
    written = 0
    if patterns_file.exists():
        try:
            written = len(json.loads(patterns_file.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            written = 0
    console.print(f"Wrote {written} patterns to {state_dir}/remediation_patterns.json")


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------

# Priority buckets in a plan.json, highest priority first.
_PLAN_BUCKETS = ("blocking", "required", "desired", "nice_to_have", "deferred")
_PLAN_FILES_MAX = 120  # truncate a very long affected-files list to this width


def _collect_plan_items(plan: object) -> list[tuple[str, dict]]:
    """Flatten a plan into ``[(priority_label, item), ...]``.

    Handles the bucketed schema (blocking/required/desired/...), a flat
    ``items``/``plan_items``/``fixes`` list, or a bare list.
    """
    out: list[tuple[str, dict]] = []
    if isinstance(plan, dict):
        matched = False
        for bucket in _PLAN_BUCKETS:
            v = plan.get(bucket)
            if isinstance(v, list):
                matched = True
                out.extend((bucket, it) for it in v if isinstance(it, dict))
        if not matched:
            for key in ("items", "plan_items", "fixes"):
                v = plan.get(key)
                if isinstance(v, list):
                    out.extend(
                        (str(it.get("priority", "")), it)
                        for it in v
                        if isinstance(it, dict)
                    )
                    break
    elif isinstance(plan, list):
        out.extend((str(it.get("priority", "")), it) for it in plan if isinstance(it, dict))
    return out


def _display_plan(plan: object) -> None:
    """Render a remediation plan as a readable per-item summary (no truncation
    of titles; never re-parses sliced JSON, which was the old crash)."""
    items = _collect_plan_items(plan)

    affected: set[str] = set()
    for _priority, item in items:
        for f in item.get("affected_files") or []:
            affected.add(str(f))

    console.print(f"[bold]Plan:[/] {len(items)} items, {len(affected)} files affected")
    if not items:
        console.print("[dim]No plan items to display.[/]")
        return

    for idx, (priority, item) in enumerate(items, 1):
        ident = item.get("id") or item.get("fix_id") or item.get("plan_id") or "(no id)"
        title = item.get("title") or item.get("description") or "(untitled)"
        # Severity is not a plan-item field in the current schema; fall back to
        # the priority bucket the item lives in.
        severity = item.get("severity") or priority or "-"
        files = [str(f) for f in (item.get("affected_files") or [])]
        files_str = ", ".join(files) or "(none)"
        if len(files_str) > _PLAN_FILES_MAX:
            files_str = files_str[: _PLAN_FILES_MAX - 3] + "..."
        # No "proposed_changes" on plan items; count the concrete changes the
        # fix derives from (source findings) as the closest real signal.
        changes = item.get("proposed_changes") or item.get("source_findings") or []
        changes_count = len(changes) if isinstance(changes, list) else 0

        # ASCII-only output — the Windows console (cp1252) can't encode em-dash,
        # box-drawing, or ellipsis, and would raise UnicodeEncodeError.
        console.print(f"[bold cyan]{idx}. {ident}[/] - {title}")
        console.print(f"   Severity: {severity}   Proposed changes: {changes_count}")
        console.print(f"   Files: {files_str}")
        console.print("   " + "-" * 60)


@main.command("plan")
@click.option("--show", is_flag=True, help="Display the current plan")
@click.option("--approve", is_flag=True, help="Approve the plan and prepare Phase 2")
def plan_cmd(show: bool, approve: bool) -> None:
    """View or approve the remediation plan."""
    cfg = load_config()
    plan_file = cfg.state_dir / "plan.json"

    if not plan_file.exists():
        console.print("[bold red]No plan found.[/] Complete Phase 1 first.")
        sys.exit(1)

    plan = _load_state_json(plan_file, "plan.json")

    if show or not approve:
        _display_plan(plan)
        return

    if approve:
        # Write approval to plan
        if isinstance(plan, dict):
            plan["human_approved"] = True
        try:
            _atomic_write_json(plan_file, plan)
        except OSError as exc:
            console.print(f"[bold red]Could not write plan.json:[/] {exc}")
            sys.exit(1)
        console.print("[bold green]Plan approved.[/] Run [bold]sentinel govern --start[/]")


# ---------------------------------------------------------------------------
# govern
# ---------------------------------------------------------------------------

@main.command("govern")
@click.option("--start", is_flag=True, help="Start Phase 2 governed remediation")
@click.option(
    "--self-approve",
    is_flag=True,
    help="Single-operator mode: auto-approve and complete PR gate without a second reviewer",
)
@click.option("--task", "task_id", default=None, metavar="TASK_ID", help="Run only one specific task")
@click.option("--limit", default=None, type=int, callback=_validate_positive_int,
              help="Process at most N tasks this run (must be >= 1)")
@click.option(
    "--sync-prs", "sync_prs", is_flag=True,
    help="Reconcile open-PR tasks against GitHub: merged→COMPLETE, rejected→reopened",
)
@click.option(
    "--verify-complete", "verify_complete", is_flag=True,
    help="S2-3 completion gate: certify delivery readiness (exit 0 READY / 1 NOT READY)",
)
def govern_cmd(
    start: bool,
    self_approve: bool,
    task_id: Optional[str],
    limit: Optional[int],
    sync_prs: bool,
    verify_complete: bool,
) -> None:
    """Run Phase 2: governed remediation."""
    if verify_complete:
        cfg = load_config()
        try:
            orch_cm = Orchestrator(cfg)
        except EnvironmentError as exc:
            console.print(f"[bold red]Error:[/] {exc}")
            sys.exit(1)
        with orch_cm as orch:
            checker = CompletionChecker(orch._store, cfg)
            report = checker.check()
            checker.print_report(report)
        report_path = cfg.state_dir / COMPLETION_REPORT_FILENAME
        try:
            _atomic_write_json(report_path, report)
        except OSError as exc:
            console.print(f"[bold red]Could not write completion_report.json:[/] {exc}")
            sys.exit(1)
        console.print(f"[dim]Wrote {report_path}[/]")
        raise SystemExit(0 if report["status"] == "READY" else 1)

    if sync_prs:
        cfg = load_config()
        try:
            orch_cm = Orchestrator(cfg)
        except EnvironmentError as exc:
            console.print(f"[bold red]Error:[/] {exc}")
            sys.exit(1)
        with orch_cm as orch:
            try:
                summary = orch.sync_all_prs()
            except Exception as exc:
                console.print(f"[bold red]Error:[/] {exc}")
                sys.exit(1)
        if summary.get("nothing_to_sync"):
            console.print("[yellow]No tasks in PR review — nothing to sync.[/]")
        else:
            console.print(
                f"[bold green]Sync complete:[/] {summary['merged']} merged, "
                f"{summary['rejected']} rejected, {summary['still_open']} still open, "
                f"{summary.get('reverted', 0)} reverted"
            )
        return

    if not start:
        console.print("Use --start to begin Phase 2")
        return

    if task_id and not _is_safe_id(task_id):
        console.print(
            "[bold red]Invalid TASK_ID format.[/] "
            "Use 1-64 chars from [A-Za-z0-9._-] and avoid '..'."
        )
        sys.exit(1)

    cfg = load_config()
    if self_approve and getattr(cfg, "enforce_single_pr_approver", False):
        console.print(
            "[bold red]Self-approve mode is disabled under single-approver policy.[/] "
            "Run without [bold]--self-approve[/] and complete approvals through GitHub PR review."
        )
        sys.exit(1)
    if self_approve:
        cfg.self_approve_mode = True
        console.print("[yellow]Self-approve mode enabled for this run.[/]")
    if cfg.self_approve_mode and getattr(cfg, "enforce_single_pr_approver", False):
        console.print(
            "[bold red]SENTINEL_SELF_APPROVE is incompatible with single-approver policy.[/] "
            "Unset [bold]SENTINEL_SELF_APPROVE[/] or disable policy enforcement."
        )
        sys.exit(1)
    plan_file = cfg.state_dir / "plan.json"
    # Read plan.json through the shared loader so a missing/corrupt/unreadable
    # file yields a clear controlled error (exit 1) instead of an uncaught
    # OSError/JSONDecodeError traceback. A truly absent file keeps the prior
    # "not approved" path via an empty dict.
    plan = _load_state_json(plan_file, "plan.json") if plan_file.exists() else {}
    if not isinstance(plan, dict):
        console.print("[bold red]plan.json is invalid (expected a JSON object).[/]")
        sys.exit(1)

    if not plan.get("human_approved"):
        console.print("[bold red]Plan not approved.[/] Run [bold]sentinel plan --approve[/] first.")
        sys.exit(1)

    console.print(Panel("[bold]Phase 2: Governed Remediation[/]", title="Sentinel", border_style="green"))

    try:
        orch_cm = Orchestrator(cfg)
    except EnvironmentError as exc:
        console.print(f"[bold red]Error:[/] {exc}")
        sys.exit(1)

    with orch_cm as orch:
        try:
            orch.run_phase2(task_id=task_id, limit=limit)
        except HumanGateRequired as gate:
            display_gate(console, gate)
            sys.exit(0)
        except Exception as exc:
            console.print(f"[bold red]Error:[/] {exc}")
            raise


# ---------------------------------------------------------------------------
# run — single-command chain (scope → assess → triage gate → continue →
#       plan gate → govern → report → verify-complete). S4-11.
# ---------------------------------------------------------------------------

# Severities auto-approved by `sentinel run --auto-approve-triage`. Only the
# highest-signal, already-verified (survived Stage 5c) findings are approved;
# lower-severity debt still requires an explicit human decision.
_RUN_AUTO_APPROVE_SEVERITIES = {"Critical", "P1"}
_RUN_GOVERN_TASK_LIMIT = 30


def _derive_run_state_dir(cfg, repo: Path) -> Path:
    """Return a per-run state dir: <base>/<repo-basename>-<timestamp>.

    The base is ``cfg.state_dir_base`` when set, otherwise a ``.sentinel-runs``
    directory beside the repo. The timestamp keeps successive runs isolated.
    """
    from datetime import datetime

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = cfg.state_dir_base.strip() if getattr(cfg, "state_dir_base", "") else ""
    base_dir = Path(base) if base else (repo.parent / ".sentinel-runs")
    return base_dir / f"{repo.name}-{timestamp}"


def _run_scope_step(repo: Path) -> None:
    """Write scope_config.yaml for *repo* (rule-based, no model calls)."""
    import yaml

    from sentinel.assess.scope_recommender import recommend_scope

    rec = recommend_scope(str(repo))
    data = {
        "repo_path": rec.repo_path,
        "generated_at": rec.generated_at,
        "exclude": [
            {
                "path": e.path,
                "reason": e.reason,
                "category": e.category,
                "confidence": e.confidence,
            }
            for e in rec.recommended_excludes
        ],
        "include": list(rec.recommended_includes),
    }
    scope_path = repo / "scope_config.yaml"
    scope_path.write_text(
        yaml.safe_dump(data, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    console.print(f"[green]Scope written to[/] {scope_path}")


def _auto_approve_tech_debt(orch) -> int:
    """Bulk-approve verified Critical/P1 tech-debt items. Returns count approved.

    Items reaching triage have already survived Stage 5c verification, so the
    remaining Critical/P1 register entries are the auto-approve set.
    """
    tech_debt = orch._store.read("tech_debt") or []
    approved = 0
    for item in tech_debt:
        if item.get("severity") in _RUN_AUTO_APPROVE_SEVERITIES:
            if item.get("human_triage") != "APPROVE":
                item["human_triage"] = "APPROVE"
                approved += 1
    if approved:
        orch._store.write("tech_debt", tech_debt)
    return approved


@main.command("run")
@click.option("--repo", "repo", required=True, type=click.Path(exists=True, file_okay=False),
              help="Path to the repository to assess and remediate")
@click.option("--state-dir", "state_dir", default=None,
              help="Run state directory (default: derived from repo name + timestamp)")
@click.option("--auto-approve-triage", "auto_approve_triage", is_flag=True, default=False,
              help="Auto-approve verified Critical/P1 findings and the plan, then "
                   "run govern -> report -> verify-complete without stopping at the "
                   "human gates")
@click.option("--no-vscode-links/--vscode-links", "no_vscode_links", default=True,
              help="Whether the Decision Brief includes 'open in VS Code' deep-links. "
                   "Default: omitted (portable brief) - the run's report step has no "
                   "viewer-usable repo root, so vscode:// links would be dead. Pass "
                   "--vscode-links to include them for local use.")
def run_cmd(repo: str, state_dir: Optional[str], auto_approve_triage: bool,
            no_vscode_links: bool) -> None:
    """Run the full Sentinel pipeline as one command.

    Chains: scope -> assess -> (tech-debt triage gate) -> assess --continue ->
    (plan-approval gate) -> govern --start -> report -> govern --verify-complete.

    Without --auto-approve-triage the command stops cleanly at each human gate
    and prints how to resume (via `sentinel triage`/`sentinel plan --approve`,
    or the `sentinel serve` UI). With --auto-approve-triage the gates are
    satisfied automatically for verified Critical/P1 findings.
    """
    repo_path = Path(repo).resolve()
    cfg = load_config()

    # Derive and pin the per-run state dir before constructing any Orchestrator.
    resolved_state = (
        Path(state_dir).resolve() if state_dir else _derive_run_state_dir(cfg, repo_path)
    )
    cfg.state_dir = resolved_state
    cfg.ensure_state_dir()
    console.print(
        Panel(
            f"[bold]Sentinel run[/]\n\n"
            f"  repo:  {repo_path}\n"
            f"  state: {resolved_state}",
            title="Sentinel",
            border_style="cyan",
        )
    )

    # ── Step 1: scope ────────────────────────────────────────────────
    try:
        _run_scope_step(repo_path)
    except Exception as exc:  # noqa: BLE001 — scope is advisory, never fatal
        console.print(f"[yellow]Scope step skipped:[/] {exc}")

    # ── Step 2: assess (Phase 1) → tech-debt triage gate ─────────────
    try:
        orch_cm = Orchestrator(cfg)
    except EnvironmentError as exc:
        console.print(f"[bold red]Error:[/] {exc}")
        sys.exit(1)

    with orch_cm as orch:
        reached_triage_gate = False
        try:
            orch.run_phase1([repo_path])
        except HumanGateRequired as gate:
            reached_triage_gate = True
            display_gate(console, gate)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[bold red]Assessment error:[/] {exc}")
            sys.exit(1)

        if reached_triage_gate and not auto_approve_triage:
            console.print(
                "[yellow]Stopped at the tech-debt triage gate.[/] "
                "Review findings then run [bold]sentinel triage[/] "
                "(or open /triage in [bold]sentinel serve[/]), and re-run "
                "[bold]sentinel run --auto-approve-triage[/] or continue "
                "manually with [bold]sentinel assess --continue[/]."
            )
            console.print(f"[dim]State: {resolved_state}[/]")
            sys.exit(0)

        # ── Step 3: auto-approve triage + continue Phase 1 ───────────
        if reached_triage_gate:
            approved = _auto_approve_tech_debt(orch)
            console.print(
                f"[green]Auto-approved {approved} verified Critical/P1 item(s).[/]"
            )
            try:
                orch.continue_phase1_after_triage()
            except HumanGateRequired:
                # Plan-approval gate — auto-approve below.
                pass
            except Exception as exc:  # noqa: BLE001
                console.print(f"[bold red]Continuation error:[/] {exc}")
                sys.exit(1)

        # ── Step 4: plan approval gate ───────────────────────────────
        plan_file = cfg.state_dir / "plan.json"
        if plan_file.exists():
            plan = _load_state_json(plan_file, "plan.json")
            if isinstance(plan, dict) and not plan.get("human_approved"):
                plan["human_approved"] = True
                try:
                    _atomic_write_json(plan_file, plan)
                except OSError as exc:
                    console.print(f"[bold red]Could not approve plan.json:[/] {exc}")
                    sys.exit(1)
                console.print("[green]Plan auto-approved.[/]")

        # ── Step 5: govern (Phase 2) ─────────────────────────────────
        console.print(
            Panel("[bold]Phase 2: Governed Remediation[/]", title="Sentinel",
                  border_style="green")
        )
        try:
            orch.run_phase2(limit=_RUN_GOVERN_TASK_LIMIT)
        except HumanGateRequired as gate:
            display_gate(console, gate)
        except Exception as exc:  # noqa: BLE001
            console.print(f"[bold red]Remediation error:[/] {exc}")
            sys.exit(1)

    # ── Step 6: report (Decision Brief) ──────────────────────────────
    brief_path: Optional[str] = None
    try:
        from sentinel.reports.decision_brief import render

        out_html, out_md, total, rows = render(
            str(resolved_state), None, None, None,
            use_synthesis=bool(getattr(cfg, "auto_synthesize", False)),
            vscode_links=not no_vscode_links,
        )
        brief_path = out_html
        console.print(f"[green]Brief:[/] {out_html}")
        console.print(f"{total} findings -> {rows} decision rows")
    except Exception as exc:  # noqa: BLE001 — report is best-effort
        console.print(f"[yellow]Report step skipped:[/] {exc}")

    # ── Step 7: verify-complete (completion certificate) ─────────────
    try:
        with Orchestrator(cfg) as vc_orch:
            checker = CompletionChecker(vc_orch._store, cfg)
            report = checker.check()
            checker.print_report(report)
        report_path = cfg.state_dir / COMPLETION_REPORT_FILENAME
        try:
            _atomic_write_json(report_path, report)
        except OSError as exc:
            console.print(f"[yellow]Could not write completion report:[/] {exc}")
    except Exception as exc:  # noqa: BLE001 — verification is best-effort here
        console.print(f"[yellow]Completion check skipped:[/] {exc}")

    # ── Optional: open the brief in a browser (interactive only) ─────
    if brief_path and sys.stdout.isatty():
        try:
            if click.confirm("Open brief in browser?", default=True):
                import webbrowser

                webbrowser.open(brief_path)
        except Exception:  # noqa: BLE001 — never let the prompt break the run
            pass

    console.print(
        Panel(
            f"[bold]Run complete.[/]\n\n"
            f"  state: {resolved_state}\n"
            + (f"  brief: {brief_path}\n" if brief_path else "")
            + f"\nServe with: [bold]sentinel serve --state-dir {resolved_state}[/]",
            title="Sentinel",
            border_style="cyan",
        )
    )


# ---------------------------------------------------------------------------
# task
# ---------------------------------------------------------------------------

@main.command("task")
@click.option("--merged", "merged_id", default=None, metavar="TASK_ID", help="Record merged PR")
@click.option("--approve", "approved_id", default=None, metavar="TASK_ID", help="Approve + merge PR for task")
@click.option("--reject", "rejected_id", default=None, metavar="TASK_ID", help="Reject PR for task and send back")
@click.option("--sync", "sync_id", default=None, metavar="TASK_ID", help="Sync PR status from GitHub for task")
@click.option("--show-diff", "show_diff_id", default=None, metavar="TASK_ID", help="Show task diff summary for local review")
@click.option("--request-changes", "request_changes_id", default=None, metavar="TASK_ID", help="Request local changes and reopen task")
@click.option("--approve-local", "approve_local_id", default=None, metavar="TASK_ID", help="Approve locally, commit, and push directly")
@click.option("--registry-approve", "registry_approve_id", default=None, metavar="TASK_ID", help="Human-approve a task held at the registry_resolution gate (REVIEW_PASS -> QA_PASS)")
@click.option("--comment", default="", help="Reviewer comment (required for --reject and --request-changes)")
@click.option("--list", "list_tasks", is_flag=True, help="List all tasks")
def task_cmd(
    merged_id: Optional[str],
    approved_id: Optional[str],
    rejected_id: Optional[str],
    sync_id: Optional[str],
    show_diff_id: Optional[str],
    request_changes_id: Optional[str],
    approve_local_id: Optional[str],
    registry_approve_id: Optional[str],
    comment: str,
    list_tasks: bool,
) -> None:
    """Manage individual tasks."""
    cfg = load_config()
    tasks_file = cfg.state_dir / "tasks.json"

    if list_tasks:
        if not tasks_file.exists():
            console.print("No tasks yet.")
            return
        tasks = _load_state_json(tasks_file, "tasks.json")
        if not isinstance(tasks, list):
            click.echo("Error: state file contains invalid data (expected JSON array)", err=True)
            raise SystemExit(1)
        display_task_table(console, tasks)
        return

    actions = [
        bool(merged_id),
        bool(approved_id),
        bool(rejected_id),
        bool(sync_id),
        bool(show_diff_id),
        bool(request_changes_id),
        bool(approve_local_id),
        bool(registry_approve_id),
    ]
    if sum(actions) > 1:
        console.print("[red]Choose only one action.[/]")
        sys.exit(1)

    task_ids_to_validate = [
        merged_id,
        approved_id,
        rejected_id,
        sync_id,
        show_diff_id,
        request_changes_id,
        approve_local_id,
        registry_approve_id,
    ]
    for candidate in task_ids_to_validate:
        if candidate and not _is_safe_id(candidate):
            console.print(
                "[bold red]Invalid TASK_ID format.[/] "
                "Use 1-64 chars from [A-Za-z0-9._-] and avoid '..'."
            )
            sys.exit(1)

    if show_diff_id:
        try:
            orch_cm = Orchestrator(cfg)
        except EnvironmentError as exc:
            console.print(f"[bold red]Error:[/] {exc}")
            sys.exit(1)

        try:
            with orch_cm as orch:
                diff_text = orch.get_task_diff(show_diff_id)
        except (KeyError, ValueError, RuntimeError, OSError) as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        console.print(Panel(diff_text[:12000], title=f"Task Diff Summary: {show_diff_id}", border_style="cyan"))
        return

    if request_changes_id:
        if not comment.strip():
            console.print("[red]--comment is required with --request-changes[/]")
            sys.exit(1)
        try:
            orch_cm = Orchestrator(cfg)
        except EnvironmentError as exc:
            console.print(f"[bold red]Error:[/] {exc}")
            sys.exit(1)

        try:
            with orch_cm as orch:
                orch.request_local_changes(request_changes_id, reason=comment)
        except (KeyError, ValueError, RuntimeError, OSError) as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        console.print(
            f"[bold yellow]↺[/] Task {request_changes_id} reopened with local review feedback\n"
            f"Run [bold]sentinel govern --start --self-approve --task {request_changes_id}[/] to re-run this item."
        )
        return

    if approve_local_id:
        if cfg.self_approve_mode and getattr(cfg, "enforce_single_pr_approver", False):
            console.print(
                "[bold red]Local direct-push approval is disabled under single-approver policy.[/] "
                "Use GitHub PR mode (disable [bold]SENTINEL_SELF_APPROVE[/])."
            )
            sys.exit(1)
        try:
            orch_cm = Orchestrator(cfg)
        except EnvironmentError as exc:
            console.print(f"[bold red]Error:[/] {exc}")
            sys.exit(1)

        self_mode = cfg.self_approve_mode
        try:
            with orch_cm as orch:
                orch.approve_local_and_push(approve_local_id, comment=comment)
        except (KeyError, ValueError, RuntimeError, OSError) as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        if self_mode:
            console.print(
                f"[bold green]✓[/] Task {approve_local_id} locally approved, committed, and pushed directly.\n"
                "Run [bold]sentinel govern --start --self-approve[/] to continue."
            )
        else:
            console.print(
                f"[bold green]✓[/] Task {approve_local_id} locally approved and PR submitted for this single task.\n"
                "Review/merge PR, then run [bold]sentinel task --sync <task_id>[/] or [bold]sentinel task --approve <task_id>[/]."
            )
        return

    if sync_id:
        try:
            orch_cm = Orchestrator(cfg)
        except EnvironmentError as exc:
            console.print(f"[bold red]Error:[/] {exc}")
            sys.exit(1)

        try:
            with orch_cm as orch:
                result = orch.sync_pr_status(sync_id)
        except (KeyError, ValueError, RuntimeError, OSError) as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)

        outcome = result.get("outcome", "UNKNOWN")
        message = result.get("message", "")
        colour = "green" if outcome in {"MERGED", "APPROVED"} else ("yellow" if outcome == "PENDING" else "red")
        console.print(f"[{colour}]PR sync: {outcome}[/] — {message}")
        return

    task_id = approved_id or merged_id
    if task_id:
        _enforce_single_approver_for_pr_actions(cfg, "approve/merge PR")
        try:
            orch_cm = Orchestrator(cfg)
        except EnvironmentError as exc:
            console.print(f"[bold red]Error:[/] {exc}")
            sys.exit(1)

        try:
            with orch_cm as orch:
                orch.record_pr_approved(task_id, comment=comment)
        except (KeyError, ValueError, RuntimeError, OSError) as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        console.print(f"[bold green]✓[/] Task {task_id} approved and marked as COMPLETE")
        return

    if rejected_id:
        if not comment.strip():
            console.print("[red]--comment is required with --reject[/]")
            sys.exit(1)

        try:
            orch_cm = Orchestrator(cfg)
        except EnvironmentError as exc:
            console.print(f"[bold red]Error:[/] {exc}")
            sys.exit(1)

        try:
            with orch_cm as orch:
                orch.record_pr_rejected(rejected_id, reason=comment)
        except (KeyError, ValueError, RuntimeError, OSError) as e:
            click.echo(f"Error: {e}", err=True)
            sys.exit(1)
        console.print(
            f"[bold yellow]↺[/] Task {rejected_id} rejected and moved back to IN_PROGRESS\n"
            "Run [bold]sentinel govern --start[/] to continue remediation."
        )
        return

    if registry_approve_id:
        try:
            orch_cm = Orchestrator(cfg)
        except EnvironmentError as exc:
            console.print(f"[bold red]Error:[/] {exc}")
            sys.exit(1)

        try:
            with orch_cm as orch:
                orch.record_registry_approved(registry_approve_id, comment=comment)
        except KeyError as exc:
            console.print(f"[bold red]Error:[/] {exc}")
            sys.exit(1)
        except RuntimeError as exc:
            console.print(f"[bold red]Error:[/] {exc}")
            sys.exit(1)
        console.print(
            f"[bold green]✓[/] Task {registry_approve_id} registry-approved "
            f"(QA_PASS recorded via human verdict).\n"
            "Run [bold]sentinel govern --start[/] to continue remediation."
        )
        return

    console.print("Use [bold]sentinel task --list[/] or a task action option.")


# ---------------------------------------------------------------------------
# dispute
# ---------------------------------------------------------------------------

@main.command("dispute")
@click.option("--resolve", "task_id", default=None, metavar="TASK_ID")
@click.option("--winner", type=click.Choice(["architect", "coder"]))
def dispute_cmd(task_id: Optional[str], winner: Optional[str]) -> None:
    """Resolve an escalated dispute."""
    if not task_id or not winner:
        console.print("Usage: sentinel dispute --resolve <task_id> --winner architect|coder")
        sys.exit(1)

    if not _is_safe_id(task_id):
        console.print(
            "[bold red]Invalid TASK_ID format.[/] "
            "Use 1-64 chars from [A-Za-z0-9._-] and avoid '..'."
        )
        sys.exit(1)

    cfg = load_config()
    disputes_file = cfg.state_dir / "disputes.json"

    if not disputes_file.exists():
        console.print("No disputes on record.")
        return

    disputes = _load_state_json(disputes_file, "disputes.json")
    if not isinstance(disputes, list):
        click.echo("Error: state file contains invalid data (expected JSON array)", err=True)
        raise SystemExit(1)

    # Apply the resolution under the shared lock + temp-file atomic replace
    # boundary so concurrent invocations cannot lose updates and a crash mid
    # write cannot truncate disputes.json (FIX-BLOCKING-008).
    def _mutate(items: list[dict]) -> None:
        for dispute in items:
            if dispute.get("task_id") == task_id:
                dispute["human_resolution"] = winner
                dispute["escalated_to_human"] = True
                return
        raise TechDebtStateError(f"Dispute for task '{task_id}' not found.")

    try:
        _persist_tech_debt_updates(disputes_file, _mutate)
    except TechDebtConflictError:
        console.print(
            "[bold red]Concurrent dispute update conflict detected; "
            "retry the command.[/]"
        )
        sys.exit(1)
    except TechDebtStateError as exc:
        console.print(f"[red]{exc}[/]")
        sys.exit(1)

    console.print(
        f"[bold green]✓[/] Dispute for {task_id} resolved: {winner} approach approved.\n"
        "The Orchestrator will use this decision when the task resumes."
    )


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

@main.command("status")
@click.option("--state-dir", "state_dir", type=click.Path(exists=True), default=None,
              help="State directory to inspect (defaults to the configured state dir)")
def status_cmd(state_dir: Optional[str]) -> None:
    """Show overall system status."""
    cfg = load_config()
    base_dir = Path(state_dir) if state_dir else cfg.state_dir

    console.print(Panel("[bold]Sentinel Status[/]", title="Sentinel", border_style="cyan"))
    console.print(f"  Repo:       {cfg.repo_path}")
    console.print(f"  State dir:  {base_dir}")
    console.print(f"  Model:      {cfg.model}")
    console.print()

    state_files = {
        "repositories": "Repositories",
        "modules": "Modules",
        "findings": "Findings",
        "tasks": "Tasks",
        "disputes": "Disputes",
        "audit_log": "Audit entries",
    }

    for store_name, label in state_files.items():
        path = base_dir / f"{store_name}.json"
        if path.exists():
            data = _load_state_json(path, f"{store_name}.json")
            count = len(data) if isinstance(data, list) else "—"
            console.print(f"  {label:<20} {count}")
        else:
            console.print(f"  [dim]{label:<20} (not yet generated)[/]")

    # Phase 1d — persistent per-group status summary (when grouping exists).
    groups_path = base_dir / "semantic_groups.json"
    tech_debt_path = base_dir / "tech_debt.json"
    if groups_path.exists() and tech_debt_path.exists():
        from sentinel.assess.finding_status import StatusManager, _STATUS_ORDER

        mgr = StatusManager(base_dir, groups_path, tech_debt_path)
        summary = mgr.get_summary()
        resume = len(mgr.get_resume_queue())

        console.print()
        console.print("=== SENTINEL STATUS ===")
        for s in _STATUS_ORDER:
            console.print(f"{s + ':':<26}{summary.get(s, 0)}")
        console.print("-" * 24)
        console.print(f"{'Total:':<26}{summary.get('total', 0)}")
        console.print()
        console.print(f"Ready to queue: {resume} (APPROVED, not yet in a batch)")


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------

@main.command("review")
@click.option("--changes", is_flag=True, help="Classify git changes by local review policy")
def review_cmd(changes: bool) -> None:
    """Show files that require human review vs auto-accept support/state files."""
    if not changes:
        console.print("Use --changes to show the current review policy classification.")
        return

    cfg = load_config()
    try:
        buckets = classify_changed_files(cfg.repo_path)
    except ReviewPolicyError as exc:
        console.print(f"[bold red]Error:[/] {exc}")
        sys.exit(1)

    if not buckets.review_required and not buckets.auto_accept:
        console.print("No local changes.")
        return

    if cfg.auto_approve_without_review:
        console.print(
            "[bold yellow]Auto-approve mode:[/] "
            "SENTINEL_AUTO_APPROVE_WITHOUT_REVIEW=true (review-required files are still listed for visibility)."
        )

    review_table = Table(title="Review Required")
    review_table.add_column("File")
    if buckets.review_required:
        for path in buckets.review_required:
            review_table.add_row(path)
    else:
        review_table.add_row("[dim](none)[/]")
    console.print(review_table)

    auto_table = Table(title="Auto-Accept (State/Docs/Support)")
    auto_table.add_column("File")
    if buckets.auto_accept:
        for path in buckets.auto_accept:
            auto_table.add_row(path)
    else:
        auto_table.add_row("[dim](none)[/]")
    console.print(auto_table)



# ---------------------------------------------------------------------------
# ingest-findings
# ---------------------------------------------------------------------------

@main.command("ingest-findings")
@click.argument("findings_file", type=click.Path(exists=True))
@click.option("--source-repo", required=True, help="Repository the findings came from")
@click.option("--authored-by-foundry", is_flag=True, default=False, help="Whether the code was generated by Foundry")
def ingest_findings_cmd(findings_file: str, source_repo: str, authored_by_foundry: bool) -> None:
    """Ingest external findings into Sentinel's learning pipeline."""
    cfg = load_config()

    findings_path = Path(findings_file)
    try:
        findings = json.loads(findings_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        console.print(f"[bold red]Error reading findings file:[/] {exc}")
        sys.exit(1)

    if not isinstance(findings, list):
        console.print("[bold red]Error:[/] Findings file must contain a JSON array.")
        sys.exit(1)

    from sentinel.learnings.extractor import LearningExtractor
    from sentinel.learnings.store import LearningStore

    extractor = LearningExtractor(
        findings, source_repo=source_repo, authored_by_foundry=authored_by_foundry,
    )
    try:
        learnings = extractor.extract()
        store = LearningStore(cfg.state_dir / "learnings")
        saved_paths = store.save(learnings)
    except (OSError, ValueError) as e:
        console.print(f"[bold red]Error ingesting findings:[/] {e}")
        sys.exit(1)

    console.print(
        f"[green]Ingested {len(findings)} findings from {source_repo}.[/]\n"
        f"Extracted {len(learnings)} learning(s), saved to {len(saved_paths)} file(s)."
    )


# ---------------------------------------------------------------------------
# export-learnings
# ---------------------------------------------------------------------------

@main.command("export-learnings")
@click.option("--category", type=str, default=None, help="Filter by learning category")
@click.option("--generation-only", is_flag=True, default=False, help="Only show learnings applicable to code generation")
@click.option("--format", "output_format", type=click.Choice(["json", "text"]), default="text", help="Output format")
def export_learnings_cmd(category: str | None, generation_only: bool, output_format: str) -> None:
    """Export learnings for consumption by external tools."""
    cfg = load_config()

    from sentinel.learnings.store import LearningStore

    store = LearningStore(cfg.state_dir / "learnings")
    learnings = store.query(
        category=category,
        applicable_to_generation=True if generation_only else None,
    )

    if output_format == "json":
        data = [l.to_dict() for l in learnings]
        click.echo(json.dumps(data, indent=2))
    else:
        if not learnings:
            console.print("[dim]No learnings found.[/]")
            return
        for learning in learnings:
            console.print(f"[bold]{learning.id}[/] [{learning.severity}] {learning.category.value}")
            console.print(f"  Pattern: {learning.pattern}")
            console.print(f"  Applicable to generation: {learning.applicable_to_generation}")
            console.print(f"  Source: {learning.source_repo}")
            console.print()


# ---------------------------------------------------------------------------
# retro
# ---------------------------------------------------------------------------

@main.command("retro")
@click.option("--state-dir", type=click.Path(exists=True), default=None,
              help="State directory to analyze")
@click.option("--format", "output_format", type=click.Choice(["markdown", "json", "text"]),
              default="markdown")
@click.option("--output", "-o", type=click.Path(), default=None,
              help="Save report to file")
def retro_cmd(state_dir: str | None, output_format: str, output: str | None) -> None:
    """Generate retrospective report from assessment data."""
    cfg = load_config()
    from sentinel.retro import RetroAnalyzer

    target_state_dir = Path(state_dir) if state_dir else cfg.state_dir
    learnings_path = (
        Path(cfg.learnings_path) if cfg.learnings_path else target_state_dir / "learnings"
    )

    analyzer = RetroAnalyzer(
        state_store_path=target_state_dir,
        learnings_path=learnings_path,
    )
    report = analyzer.analyze()

    if output_format == "json":
        content = json.dumps(report.to_dict(), indent=2)
    elif output_format == "markdown":
        content = report.to_markdown()
    else:
        # text format — compact summary
        content = (
            f"Retrospective: {report.repo_name}\n"
            f"Findings: {report.total_findings}\n"
            f"Learnings: {report.learnings_extracted}\n"
            f"Top categories: {', '.join(f'{c}({n})' for c, n in report.top_categories)}\n"
        )
        if report.trigger_gaps:
            content += f"Trigger gaps: {', '.join(report.trigger_gaps)}\n"

    if output:
        try:
            Path(output).write_text(content, encoding="utf-8")
        except OSError as e:
            console.print(f"[bold red]Error writing report:[/] {e}")
            sys.exit(1)
        console.print(f"[green]Report saved to {output}[/]")
    else:
        console.print(content)


register_phase3_commands(
    main,
    console,
    is_safe_id=_is_safe_id,
    max_submit_json_bytes=_MAX_SUBMIT_JSON_BYTES,
    max_feedback_chars=_MAX_FEEDBACK_CHARS,
)

register_defect_commands(
    main,
    console,
    is_safe_id=_is_safe_id,
    max_submit_json_bytes=_MAX_SUBMIT_JSON_BYTES,
)


# ---------------------------------------------------------------------------
# scan (quick assessment)
# ---------------------------------------------------------------------------

@main.command("scan")
@click.argument("target", type=click.Path(exists=True))
@click.option("--extensions", "-e", multiple=True, help="File extensions to include (e.g., .py .js)")
@click.option("--format", "output_format", type=click.Choice(["json", "text"]), default="text")
def scan_cmd(target: str, extensions: tuple[str, ...], output_format: str) -> None:
    """Quick scan of a file or directory without full cartography."""
    from sentinel.scan import Scanner

    scanner = Scanner()
    target_path = Path(target).resolve()

    if target_path.is_file():
        results = [scanner.scan_file(target_path)]
    elif target_path.is_dir():
        ext_list = list(extensions) if extensions else None
        results = scanner.scan_directory(target_path, extensions=ext_list)
    else:
        console.print(f"[bold red]Error:[/] '{target}' is neither a file nor a directory.")
        sys.exit(1)

    if not results:
        console.print("[dim]No files matched the scan criteria.[/]")
        return

    if output_format == "json":
        import dataclasses
        data = [dataclasses.asdict(r) for r in results]
        click.echo(json.dumps(data, indent=2))
    else:
        total_findings = sum(len(r.findings) for r in results)
        has_blockers = any(r.has_blockers() for r in results)

        console.print(
            Panel(
                f"[bold]Quick Scan[/]\n\n"
                f"  Target: {target_path}\n"
                f"  Files scanned: {len(results)}\n"
                f"  Total findings: {total_findings}",
                title="Sentinel",
                border_style="cyan" if not has_blockers else "red",
            )
        )

        for result in results:
            console.print(result.summary_text())
            console.print()


# ---------------------------------------------------------------------------
# triage-findings (interactive finding triage)
# ---------------------------------------------------------------------------

@main.command("triage-findings")
@click.option("--state-dir", type=click.Path(exists=True), default=None,
              help="State directory with findings.json")
@click.option("--findings-file", type=click.Path(exists=True), default=None,
              help="Direct path to findings JSON file")
@click.option("--severity", type=click.Choice(["all", "critical-p1", "critical"]),
              default="all", help="Which severities to triage")
@click.option("--output", "-o", type=click.Path(), default=None,
              help="Save triage results to file")
def triage_findings_cmd(
    state_dir: Optional[str],
    findings_file: Optional[str],
    severity: str,
    output: Optional[str],
) -> None:
    """Interactive finding-by-finding triage."""
    from sentinel.triage import TriageDecision, TriageManager

    # Load findings
    if findings_file:
        findings_path = Path(findings_file)
    elif state_dir:
        findings_path = Path(state_dir) / "findings.json"
    else:
        cfg = load_config()
        findings_path = cfg.state_dir / "findings.json"

    if not findings_path.exists():
        console.print(f"[bold red]No findings file found at {findings_path}.[/]")
        sys.exit(1)

    try:
        findings = json.loads(findings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        console.print(f"[bold red]Error reading findings file:[/] {e}")
        sys.exit(1)
    if not isinstance(findings, list):
        console.print("[bold red]Error:[/] Findings file must contain a JSON array.")
        sys.exit(1)

    # Filter by severity
    if severity == "critical-p1":
        findings = [f for f in findings if f.get("severity") in ("Critical", "P1")]
    elif severity == "critical":
        findings = [f for f in findings if f.get("severity") == "Critical"]

    if not findings:
        console.print("[dim]No findings match the severity filter.[/]")
        return

    manager = TriageManager(findings)
    decision_map = {"a": TriageDecision.ACCEPT, "b": TriageDecision.DEFER,
                    "c": TriageDecision.WONT_FIX, "d": TriageDecision.DISPUTE}

    console.print(
        Panel(
            f"[bold]Interactive Triage[/]\n\n"
            f"  Findings to triage: {len(findings)}\n"
            f"  Severity filter: {severity}",
            title="Sentinel",
            border_style="cyan",
        )
    )

    while True:
        finding = manager.get_next()
        if finding is None:
            break

        console.print()
        console.print(Panel(
            manager.format_finding_for_display(finding),
            border_style="yellow",
        ))

        choice = click.prompt(
            "Decision",
            type=click.Choice(["a", "b", "c", "d", "q"], case_sensitive=False),
        )

        if choice.lower() == "q":
            console.print("[yellow]Triage interrupted.[/]")
            break

        decision = decision_map[choice.lower()]
        rationale = None
        if decision in (TriageDecision.WONT_FIX, TriageDecision.DISPUTE):
            rationale = click.prompt("Rationale (optional)", default="", show_default=False)
            rationale = rationale or None

        fid = finding.get("id") or finding.get("finding_id")
        if not fid:
            console.print("[yellow]Skipping finding with no id.[/]")
            continue
        manager.record_decision(fid, decision, rationale=rationale)
        console.print(f"[green]Recorded:[/] {decision.value}")

    # Show summary
    session = manager.get_session()
    summary = session.summary()
    console.print()
    console.print(
        Panel(
            f"[bold]Triage Summary[/]\n\n"
            f"  Total triaged: {summary['total']}\n"
            f"  Accepted:      {summary['accepted']}\n"
            f"  Deferred:      {summary['deferred']}\n"
            f"  Won't fix:     {summary['wont_fix']}\n"
            f"  Disputed:      {summary['disputed']}",
            title="Results",
            border_style="green" if not session.has_open_blockers() else "red",
        )
    )

    if output:
        try:
            manager.save_results(Path(output))
        except (OSError, ValueError) as e:
            console.print(f"[bold red]Error saving triage results:[/] {e}")
            sys.exit(1)
        console.print(f"[green]Results saved to {output}[/]")


# ---------------------------------------------------------------------------
# suppress — manage finding-suppression rules (S3-5 learning pass)
# ---------------------------------------------------------------------------

@main.command("suppress")
@click.option("--state-dir", type=click.Path(exists=True), default=None,
              help="Run state directory holding suppression_rules.json")
@click.option("--list", "list_rules", is_flag=True, default=False,
              help="List all suppression rules")
@click.option("--add", "add_rule", is_flag=True, default=False,
              help="Add a suppression rule (requires --file and --kind)")
@click.option("--remove", "remove_id", default=None,
              help="Remove a suppression rule by rule_id")
@click.option("--file", "rule_file", default=None,
              help="File the rule matches (exact or glob)")
@click.option("--kind", "rule_kind", default=None,
              help="finding_kind the rule matches")
@click.option("--pattern", "location_pattern", default=None,
              help="Optional substring the finding.location must contain")
@click.option("--rationale", default=None,
              help="Why this suppression rule exists")
def suppress_cmd(
    state_dir: Optional[str],
    list_rules: bool,
    add_rule: bool,
    remove_id: Optional[str],
    rule_file: Optional[str],
    rule_kind: Optional[str],
    location_pattern: Optional[str],
    rationale: Optional[str],
) -> None:
    """Manage suppression rules that filter findings before triage (Stage 4d).

    Examples:
        sentinel suppress --list
        sentinel suppress --add --file src/x.py --kind hardcoded_secret \
            --rationale "env-injected"
        sentinel suppress --remove <rule_id>
    """
    from sentinel.orchestrator.suppression import SuppressionManager

    resolved_dir = Path(state_dir) if state_dir else load_config().state_dir
    if not resolved_dir.exists():
        console.print(f"[bold red]State directory not found:[/] {resolved_dir}")
        sys.exit(1)

    sm = SuppressionManager(resolved_dir)

    # Exactly one action.
    actions = sum(bool(x) for x in (list_rules, add_rule, remove_id))
    if actions != 1:
        console.print(
            "[bold red]Specify exactly one of --list, --add, or --remove.[/]"
        )
        sys.exit(1)

    if remove_id:
        if not any(r.get("rule_id") == remove_id for r in sm.list_rules()):
            console.print(f"[bold red]No suppression rule with id {remove_id}.[/]")
            sys.exit(1)
        sm.remove_rule(remove_id)
        console.print(f"[green]Removed suppression rule {remove_id}.[/]")
        return

    if add_rule:
        if not rule_file or not rule_kind:
            console.print(
                "[bold red]--add requires both --file and --kind.[/]"
            )
            sys.exit(1)
        rule: dict = {"file": rule_file, "finding_kind": rule_kind}
        if location_pattern:
            rule["location_pattern"] = location_pattern
        rule["rationale"] = rationale or ""
        sm.add_rule(rule)
        new_id = sm.list_rules()[-1].get("rule_id")
        console.print(f"[green]Added suppression rule {new_id}.[/]")
        return

    # --list
    rules = sm.list_rules()
    if not rules:
        console.print("[dim]No suppression rules.[/]")
        return
    table = Table(title="Suppression Rules")
    table.add_column("rule_id", style="cyan")
    table.add_column("file")
    table.add_column("finding_kind")
    table.add_column("location_pattern")
    table.add_column("rationale")
    for r in rules:
        table.add_row(
            str(r.get("rule_id", "")),
            str(r.get("file", "")),
            str(r.get("finding_kind", "")),
            str(r.get("location_pattern", "") or "-"),
            str(r.get("rationale", "") or "-"),
        )
    console.print(table)


if __name__ == "__main__":
    main()
