"""
sentinel/orchestrator/main.py

Main Orchestrator for Sentinel.

The Orchestrator is pure Python with no AI component. It manages state
machine transitions, assembles context for each agent call, routes findings
to the correct skill, tracks token usage, and is the sole component that
writes to JSON state files.

It runs autonomously until a human gate requires input. Human gates:
  - Phase 1 → Phase 2 transition (plan approval)
  - Tech debt triage (Stage 5)
  - Disputed decision packets (escalated disagreements)
  - PR review and merge

Invoked from ``sentinel.cli`` or directly via Python.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import re
import subprocess
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from sentinel.agents.architect import ArchitectAgent
from sentinel.agents.base import AgentError, AgentResponseError
from sentinel.agents.coder import CoderAgent
from sentinel.agents.docs import DocsAgent
from sentinel.agents.qa import QAAgent
from sentinel.api.client import NoOpModelClient, create_model_client
from sentinel.assess.arch_checker import ArchChecker
from sentinel.assess.cartographer import Cartographer
from sentinel.assess.file_filter import is_operational_code, should_exclude_path
from sentinel.assess.dependency import DependencyAnalyzer
from sentinel.assess.deterministic import DeterministicAnalyzer
from sentinel.assess.grouper import (
    ConflictRecord,
    FileAuditLog,
    SemanticGrouper,
    serialize_groups,
)
from sentinel.assess.retrieval import RetrievalPackager
from sentinel.assess.reviewer import ReviewOrchestrator
from sentinel.assess.scan_context import ScanContext
from sentinel.assess.synthesizer import Synthesizer
from sentinel.workflow.config import (
    WorkflowConfig,
    load_workflow_config_yaml,
)
from sentinel.workflow.build_concept_themes import (
    build_concept_themes_from_descriptions,
)
from sentinel.workflow.finding_loader import load_findings
from sentinel.workflow.models import ConceptTheme
from sentinel.config import Config
from sentinel.orchestrator.context import (
    build_fix_planner_context,
    build_verification_context,
    build_transition_manifest_context,
    build_remediation_pattern_context,
    build_synthetic_conflict_pattern,
    _compute_pattern_id,
)
from sentinel.orchestrator.state_store import StateStore
from sentinel.orchestrator.quality_artifacts import (
    TRANSPORT_FAILURE_TOLERANCE,
    build_coverage_certificate,
    build_line_coverage_map,
    build_quality_scorecard,
    build_shadow_compare_report,
    classify_failed_chunks,
    is_transport_failure,
    reconcile_findings,
)
from sentinel.orchestrator.retry_context_builder import build_retry_context, write_batch_summary
from sentinel.orchestrator.task_manager import Task, TaskManager, _get_git_diff
from sentinel.orchestrator.todo_schema import TodoValidationError
from sentinel.orchestrator.state_machine import GateViolationError, TaskState
from sentinel.skills.loader import SkillLoader, Skills

log = logging.getLogger(__name__)

# Source file extensions to include when a module path is a directory.
_SOURCE_EXTS = {
    ".py", ".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs", ".cs", ".xml", ".vb", ".vbs",
    ".csproj", ".sln", ".wxs", ".wixproj", ".config"
}

# Skip reasons that mean a module had nothing reviewable. These are reported
# as not-applicable rather than counted against review completeness. A genuine
# failure such as "read_error" is deliberately NOT included — it stays counted
# as incomplete because it represents a real coverage gap.
_NOT_APPLICABLE_SKIP_REASONS: frozenset[str] = frozenset(
    {"no_source_files", "path_outside_repo"}
)

# Canonical finding severities. Nonstandard casing is normalized; anything else
# is treated as unknown/invalid by callers rather than silently dropped.
_CANONICAL_SEVERITIES: dict[str, str] = {
    "critical": "Critical",
    "p1": "P1",
    "p2": "P2",
    "p3": "P3",
}

# Standard working day used to derive critical-path days from per-item effort
# hours when recomputing the deduplicated plan summary (FIX-REQUIRED-003).
_WORK_HOURS_PER_DAY = 8.0

# Maximum number of dispute → resolve → re-run cycles a single task may go
# through before it is escalated to a human instead of re-entering _run_task.
# Bounds the otherwise-unbounded dispute-resolution loop.
_MAX_DISPUTE_ROUNDS = 3

# Stage 5c caps the number of findings sent to the verifier in a single call.
# An uncapped per-file batch makes a reasoning model (e.g. o3) exhaust its
# completion budget on high-finding-count files and return empty/error output,
# which then falls back to keep-by-default and silently defeats verification.
# Used when the operator has not set SENTINEL_VERIFIABILITY_MAX_FINDINGS_PER_CHUNK.
_STAGE5C_MAX_FINDINGS_PER_BATCH = 25

# Attempts per Stage 5c verifier call before falling back to keep-by-default.
# Mops up transient gateway {"detail": ...} envelopes and empty completions.
_STAGE5C_VERIFY_ATTEMPTS = 3

# Stage 5c-alarm caps findings per alarm-classification call. Classification
# reads only lightweight metadata (title/description/file), so the batch can be
# larger than the verifier's source-carrying batch.
_STAGE5C_ALARM_MAX_FINDINGS_PER_BATCH = 50

# Attempts per alarm-classification call before falling back to the
# deterministic seed label for that batch (degrade, never abort).
_STAGE5C_ALARM_CLASSIFY_ATTEMPTS = 2

# Signals (case-insensitive) that a model call failed because the input exceeded
# the provider's context window / maximum input size, as opposed to a genuine
# auth/network/server failure. Used to decide whether an oversized verifiability
# chunk should be split and retried.
_CONTEXT_WINDOW_ERROR_SIGNALS: tuple[str, ...] = (
    "context window",
    "context length",
    "maximum context",
    "maximum input",
    "exceeds the context",
    "too many tokens",
    "input is too large",
    "reduce the length",
)


def _is_context_window_error(exc: BaseException) -> bool:
    """Return True when *exc* (or a cause in its chain) indicates the model input
    exceeded the provider context window / maximum input size."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        message = str(cur).lower()
        if any(signal in message for signal in _CONTEXT_WINDOW_ERROR_SIGNALS):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _canonical_severity(raw: str) -> str | None:
    """Return the canonical severity (Critical/P1/P2/P3) for ``raw``.

    Case-insensitive; returns ``None`` for any unrecognized/malformed value so
    the caller can route it to an explicit unknown bucket (FIX-REQUIRED-023).
    """
    return _CANONICAL_SEVERITIES.get(str(raw or "").strip().lower())


def _changed_files_from_diff(diff: str) -> list[str]:
    """Best-effort extraction of changed file paths from a unified diff, for the
    SCOPE_LIMIT gate (cycle 52). Returns [] when the text isn't a parseable diff
    (so a non-diff summary yields no false scope violations)."""
    files: list[str] = []
    for line in (diff or "").splitlines():
        m = re.match(r"^\+\+\+ b/(.+)$", line) or re.match(r"^diff --git a/\S+ b/(.+)$", line)
        if m:
            f = m.group(1).strip()
            if f and f != "/dev/null" and f not in files:
                files.append(f)
    return files


def _normalize_module_dir(path: str) -> str:
    """Normalize a module path to a repo-relative POSIX directory string.

    Returns ``""`` for the repo-root module (path ``"."`` or empty) so that
    descendant-ownership checks can treat the root uniformly.
    """
    norm = (path or "").replace("\\", "/").strip("/")
    return "" if norm in ("", ".") else norm


def _normalize_affected_files(files: list[str], repo_root: str) -> list[str]:
    """Make task affected_files repo-root-relative for Phase 2.

    A subtree assessment records paths like ``govern/conditions.py`` (relative to
    the scanned ``sentinel/`` subtree). Phase 2 — edit application, source
    context, and the Coder's generated imports — needs repo-root-relative paths
    (``sentinel/govern/conditions.py``). Prepend ``sentinel/`` when the path
    isn't already prefixed and the sentinel/ version exists on disk; otherwise
    leave the path untouched.
    """
    from pathlib import Path as _Path

    normalized: list[str] = []
    for f in files:
        if f.startswith("sentinel/") or f.startswith("sentinel\\"):
            normalized.append(f)
            continue
        candidate = _Path(repo_root) / "sentinel" / f
        if candidate.exists():
            normalized.append("sentinel/" + f.replace("\\", "/"))
        else:
            normalized.append(f)
    return normalized


def _module_in_affected(module: str, affected_files: list[str]) -> bool:
    """Case-insensitive, separator-independent membership test (FIX 5).

    A finding's ``module`` path may use Windows separators or differing case
    versus a task's ``affected_files`` (e.g. Line_tool paths). Exact string
    comparison silently missed those, leaving QA with no original-finding
    context. Both sides are normalized with ``Path(...).as_posix().lower()``
    before the membership test.
    """
    norm_module = Path(module or "").as_posix().lower()
    norm_affected = {
        Path(p or "").as_posix().lower() for p in (affected_files or [])
    }
    return bool(module) and norm_module in norm_affected


def _module_descendant_dirs(cur_norm: str, all_module_dirs: set[str]) -> list[str]:
    """Return the normalized dirs of modules nested strictly inside ``cur_norm``.

    Used so a directory module does not re-review files that belong to a
    deeper module (each file is reviewed under its deepest-owning module
    only — mirroring the Cartographer's assignment). For the root module
    (``cur_norm == ""``) every other module is a descendant.
    """
    return [
        d
        for d in all_module_dirs
        if d and d != cur_norm and (cur_norm == "" or d.startswith(cur_norm + "/"))
    ]


def _extract_and_store_learnings(
    state_dir: Path,
    repo_paths: list[Path],
    authored_by_foundry: bool = False,
    learnings_path: Path | None = None,
) -> int | None:
    """Extract learnings from current findings and store them.

    This is a module-level helper so it can be tested without instantiating
    the full Orchestrator (which requires model clients).

    Args:
        state_dir: Path to the sentinel state directory.
        repo_paths: Repositories that were assessed.
        authored_by_foundry: Whether the assessed code was authored by Foundry.
        learnings_path: Directory to store learnings. Defaults to state_dir / "learnings".

    Returns:
        Number of learnings extracted, or ``None`` when findings.json could not
        be read/parsed. ``None`` (read failure) is deliberately distinct from
        ``0`` (findings read successfully but yielded no learnings) so callers
        can tell a genuine failure apart from an empty result.
    """
    from sentinel.learnings.extractor import LearningExtractor
    from sentinel.learnings.store import LearningStore

    findings_file = state_dir / "findings.json"
    if not findings_file.exists():
        return 0

    try:
        findings = json.loads(findings_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        # A corrupt/unreadable findings.json is a real failure, not "zero
        # learnings" — log at ERROR and return None so the caller can tell the
        # two apart.
        log.error(
            "Failed to read findings for learning extraction", exc_info=True
        )
        return None

    if not findings:
        return 0

    store_path = learnings_path or (state_dir / "learnings")
    store = LearningStore(store_path)
    total_learnings: list = []

    for repo_path in repo_paths:
        source_repo = repo_path.name
        extractor = LearningExtractor(
            findings,
            source_repo=source_repo,
            authored_by_foundry=authored_by_foundry,
        )
        learnings = extractor.extract()
        if learnings:
            store.save(learnings)
            total_learnings.extend(learnings)

    log.info("Extracted %d learnings from assessment", len(total_learnings))
    return len(total_learnings)


def _dedup_plan_items(acc: dict[str, list[Any]], tiers: tuple[str, ...]) -> None:
    """Drop plan items whose ``source_findings`` set was already planned.

    The Fix Planner runs once per pattern batch (see
    ``Orchestrator.continue_phase1_after_triage``); a finding that belongs to
    multiple SemanticGroups is fed to multiple batches, so each batch
    independently emits items for it. This collapses items across batches by
    exact ``frozenset(source_findings)``, keeping the first occurrence. Tier
    order in ``tiers`` decides precedence (blocking > required > desired), and
    items with no ``source_findings`` are always kept. Mutates ``acc`` in place.
    """
    seen_finding_sets: set[frozenset] = set()
    for tier in tiers:
        deduped = []
        for item in acc[tier]:
            key = frozenset(item.get("source_findings", []))
            if not key or key not in seen_finding_sets:
                deduped.append(item)
                if key:
                    seen_finding_sets.add(key)
        acc[tier] = deduped


class HumanGateRequired(Exception):
    """
    Raised when the Orchestrator reaches a human gate.

    The caller (CLI) displays the gate payload to the operator and waits
    for input before resuming.
    """

    def __init__(self, gate_type: str, payload: dict[str, Any]) -> None:
        self.gate_type = gate_type
        self.payload = payload
        super().__init__(f"Human gate required: {gate_type}")


class Orchestrator:
    """
    The Sentinel Orchestrator — coordinates all agents across both phases.

    Instantiate with a Config and call ``run_phase1()`` or ``run_phase2()``.
    Both methods may raise ``HumanGateRequired`` when operator input is
    needed. The CLI handles these interruptions.
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._store = StateStore(
            state_dir=config.state_dir,
            repo_path=config.repo_path,
        )

        # Initialise shared infrastructure
        try:
            self._client = create_model_client(config)
        except EnvironmentError as exc:
            if self._config.strict_quality_mode and not self._config.chat_native_mode:
                raise EnvironmentError(
                    "Strict quality mode requires external model inference. "
                    "Model client initialization failed; refusing local-only fallback."
                ) from exc
            log.warning(
                "Model client unavailable (%s). Falling back to local-only mode.",
                exc,
            )
            self._config.local_only_mode = True
            self._client = NoOpModelClient()

        if (
            self._config.strict_quality_mode
            and isinstance(self._client, NoOpModelClient)
            and not self._config.chat_native_mode
        ):
            raise EnvironmentError(
                "Strict quality mode requires external model inference and cannot run in local-only mode."
            )
        self._loader = SkillLoader(config.skill_files_dir)

        # Instantiate agents
        self._architect = ArchitectAgent(config, self._client, self._loader)
        self._coder = CoderAgent(config, self._client, self._loader)
        self._qa = QAAgent(config, self._client, self._loader)
        self._docs = DocsAgent(config, self._client, self._loader)

        # Assess pipeline components
        self._cartographer = Cartographer(self._architect, self._store, config)
        self._dependency_analyzer = DependencyAnalyzer(self._architect, self._store, config)
        self._deterministic_analyzer = DeterministicAnalyzer()
        self._retrieval_packager = RetrievalPackager(config)
        self._reviewer: ReviewOrchestrator | None = None
        self._synthesizer = Synthesizer(self._store)

        # Govern pipeline
        self._task_manager = TaskManager(self._store)

        # Sub-step 8-E: lazy WorkflowConfig cache populated on first
        # ``_workflow_config`` property access. ``None`` sentinel keeps
        # ``load_workflow_config_yaml`` off the hot init path.
        self._workflow_config_cache: WorkflowConfig | None = None

    # ------------------------------------------------------------------
    # Phase 1 — Assessment
    # ------------------------------------------------------------------

    def run_phase1(
        self,
        repo_paths: list[Path],
        restricted_files: list[str] | None = None,
        resume_review: bool = False,
    ) -> None:
        """
        Execute Phase 1: full codebase assessment.

        Stages:
          1. Repository Discovery (Layer 0a)
          2. Dependency Mapping (Layer 0b)
          3. Module-by-Module Review (Layers 1–4)
          4. Cross-Module Synthesis
          5. Tech Debt Triage → HUMAN GATE
          6. Remediation Planning (Layer 5) → HUMAN GATE

        Args:
            repo_paths: Repositories to assess.
            restricted_files: Optional repo-relative source paths. When provided
                (``sentinel assess --changed-since``), Stage 1 restricts the
                manifest to only these files, turning the run into a targeted
                verification pass over changed files.
            resume_review: When True (``sentinel assess --resume-review``),
                Stage 3 skips review units whose prior ``module_review_status``
                entry is ``review_complete``, reusing their persisted findings
                and coverage; only incomplete units are re-reviewed. Recovers
                an interrupted run (provider outage / budget exhaustion) in
                the same state dir without repeating completed work.

        Raises:
            HumanGateRequired: At Stage 5 (triage) and Stage 6 (plan approval).
        """
        self._audit("orchestrator", "phase1_started", {"repos": [str(r) for r in repo_paths]})
        self._run_phase0_quality_foundations()
        log.info("=== Phase 1: Codebase Assessment ===")

        # Stage 1
        log.info("--- Stage 1: Repository Discovery ---")
        manifest = self._cartographer.run(repo_paths, restricted_files=restricted_files)
        manifest_repositories = manifest.get("repositories", [])
        # Resolve the target root from the manifest when available, else fall
        # back to the first provided repo path — but guard the empty case so an
        # absent manifest path AND empty repo_paths fails with a clear error
        # instead of an IndexError (FIX-BLOCKING-017).
        if manifest_repositories and manifest_repositories[0].get("path"):
            target_root = Path(manifest_repositories[0]["path"])
        elif repo_paths:
            target_root = repo_paths[0]
        else:
            raise ValueError(
                "No repository path could be resolved: the Stage 1 manifest "
                "provided no repository path and repo_paths is empty."
            )
        scan_context = ScanContext.from_repo_root(target_root)
        scan_metadata = scan_context.to_metadata()
        # cycle 61 — capture the assessed commit SHA so `sentinel report` can pin
        # GitHub permalinks to it without the operator passing --github-ref.
        scan_metadata["commit_sha"] = self._git_head_sha(target_root)
        # Record the review model + provider so a run's baseline is verifiable
        # after the fact (previously unrecorded, making cross-run comparisons
        # ambiguous).
        scan_metadata["model"] = self._config.model
        scan_metadata["provider"] = self._config.provider
        self._store.write("scan_metadata", scan_metadata)
        self._reviewer = ReviewOrchestrator(
            self._architect,
            self._store,
            self._config,
            scan_context=scan_context,
        )
        self._store.commit_state("sentinel: Stage 1 complete")

        # Stage 2
        log.info("--- Stage 2: Dependency Mapping ---")
        self._dependency_analyzer.run()
        self._store.commit_state("sentinel: Stage 2 complete")

        # Stage 2.5 — Architectural Checklist (codebase-level absence detection)
        log.info("--- Stage 2.5: Architectural Checklist ---")
        arch_checker = ArchChecker(self._architect, self._store)
        arch_findings = arch_checker.run()
        log.info("Stage 2.5: Architectural checklist — %d findings", len(arch_findings))

        # Stage 3
        log.info("--- Stage 3: Module-by-Module Review ---")
        modules = self._store.read("modules")
        # Validate the modules payload at the read boundary so a missing or
        # malformed artifact fails with a clear controlled error instead of
        # crashing inside sorted(...) (FIX-REQUIRED-021).
        if modules is None:
            raise RuntimeError(
                "Stage 3: 'modules' artifact is missing (store returned None). "
                "Run Stage 1 discovery before module review."
            )
        if not isinstance(modules, list):
            raise RuntimeError(
                "Stage 3: 'modules' artifact is malformed — expected a JSON "
                f"list, got {type(modules).__name__}."
            )
        deterministic_findings: list[dict[str, Any]] = []
        retrieval_manifest: list[dict[str, Any]] = []
        reasoning_findings: list[dict[str, Any]] = []
        # Always-accumulated reviewer findings, independent of reasoning-lane
        # packaging, so downstream reconciliation preserves them even when the
        # findings store is empty/unchanged (FIX-REQUIRED-018).
        accumulated_module_findings: list[dict[str, Any]] = []
        module_scope_lines: dict[str, int] = {}
        deterministic_success: dict[str, bool] = {}
        reasoning_ranges: dict[str, list[tuple[int, int]]] = {}
        module_statuses: list[dict[str, Any]] = []
        failed_chunk_registry: list[dict[str, Any]] = []
        # Explicit, machine-readable Stage 3 lane failure state so a caught lane
        # exception is recorded rather than only logged (FIX-REQUIRED-006/022).
        deterministic_lane_failures: dict[str, str] = {}
        retrieval_lane_failures: dict[str, str] = {}
        strict_mode = self._config.strict_quality_mode
        # Sort by criticality (highest risk first, as ordered by Cartographer)
        modules_sorted = sorted(
            modules,
            key=lambda m: {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(
                m.get("criticality", "medium"), 2
            ),
        )

        # Normalized dir of every module; used to keep a directory module from
        # re-reviewing files owned by a deeper module (notably the repo-root
        # module, path '.', which would otherwise sweep the whole tree).
        all_module_dirs = {
            _normalize_module_dir(m.get("path", "")) for m in modules_sorted
        }

        # Targeted pass (--files / --changed-since): the directory-module
        # enumeration below re-walks the disk, so without this set it would
        # sweep files Stage 1 already restricted out of the manifest.
        restricted_set: set[str] | None = None
        if restricted_files is not None:
            restricted_set = {f.replace("\\", "/") for f in restricted_files}

        # Resume (--resume-review): reload the prior run's per-unit state so
        # completed units are skipped with their findings and coverage intact.
        # The lane accumulators must be re-seeded because reconciliation below
        # REWRITES findings.json from this run's deterministic/reasoning lists;
        # without seeding, skipped units' findings would vanish from the
        # canonical output.
        resume_statuses: dict[str, dict[str, Any]] = {}
        resume_det: dict[str, list[dict[str, Any]]] = {}
        resume_rea: dict[str, list[dict[str, Any]]] = {}
        resume_legacy: dict[str, list[dict[str, Any]]] = {}
        resume_manifest: dict[str, dict[str, Any]] = {}
        if resume_review:
            for status in self._store.read("module_review_status") or []:
                unit = status.get("module")
                if unit and status.get("review_complete"):
                    resume_statuses[unit] = status
            for finding in self._store.read("deterministic_findings") or []:
                resume_det.setdefault(finding.get("module", ""), []).append(finding)
            for finding in self._store.read("reasoning_findings") or []:
                resume_rea.setdefault(finding.get("module", ""), []).append(finding)
            for entry in self._store.read("retrieval_manifest") or []:
                unit = entry.get("module")
                if unit:
                    resume_manifest[unit] = entry
            # Prune legacy findings.json to completed units: the reviewer
            # appends as it re-reviews, so stale partial findings from the
            # interrupted attempt would otherwise duplicate.
            kept_legacy: list[dict[str, Any]] = []
            for finding in self._store.read("findings") or []:
                unit = finding.get("module", "")
                if unit in resume_statuses:
                    kept_legacy.append(finding)
                    resume_legacy.setdefault(unit, []).append(finding)
            self._store.write("findings", kept_legacy)
            log.info(
                "Phase 1 resume: %d complete unit(s) will be skipped "
                "(%d preserved finding(s)); incomplete units re-review",
                len(resume_statuses),
                len(kept_legacy),
            )
            self._audit(
                "orchestrator",
                "phase1_review_resumed",
                {
                    "complete_units_skipped": len(resume_statuses),
                    "preserved_findings": len(kept_legacy),
                },
            )

        for module in modules_sorted:
            module_path = module.get("path", "")
            if not module_path:
                continue
            module_name = module.get("name", module_path)
            module_scope_lines[module_name] = 0
            deterministic_success[module_name] = False
            reasoning_ranges[module_name] = []
            try:
                full_path = Path(module_path)
                if not full_path.is_absolute():
                    full_path = target_root / module_path

                # Validate no path traversal
                try:
                    full_path.resolve().relative_to(target_root.resolve())
                except ValueError:
                    log.warning("Skipping module with path outside repo: %s", module_path)
                    module_statuses.append(
                        {
                            "module": module.get("name", module_path),
                            "module_path": module_path,
                            "chunks_total": 0,
                            "chunks_capped": 0,
                            "quick_review_failed_chunks": 0,
                            "specialist_failed_chunks": 0,
                            "findings_count": 0,
                            "skills_run": [],
                            "specialist_attempted_chunks": {},
                            "specialist_failed_chunks_by_skill": {},
                            "review_complete": False,
                            "skip_reason": "path_outside_repo",
                        }
                    )
                    continue

                review_units: list[tuple[dict[str, Any], str]] = []
                if full_path.is_dir():
                    # Enumerate source files within the directory, skipping any
                    # that belong to a deeper module so each file is reviewed
                    # under exactly one module (see _module_descendant_dirs).
                    # Each file is reviewed on its own (not concatenated into a
                    # blob) so findings carry the real source file path.
                    descendant_dirs = _module_descendant_dirs(
                        _normalize_module_dir(module_path), all_module_dirs
                    )
                    for src_file in sorted(full_path.rglob("*")):
                        if should_exclude_path(src_file.relative_to(full_path)):
                            continue
                        if not (src_file.is_file() and src_file.suffix.lower() in _SOURCE_EXTS):
                            continue
                        file_rel = src_file.relative_to(target_root).as_posix()
                        if restricted_set is not None and file_rel not in restricted_set:
                            continue
                        # Operational scope: skip config/data, scripts, project/
                        # build files, and tests so Stage 3 reviews only the code
                        # that does the work. "all" preserves legacy behavior.
                        if (
                            self._config.assessment_scope == "operational"
                            and not is_operational_code(src_file.relative_to(target_root))
                        ):
                            continue
                        if any(
                            file_rel == d or file_rel.startswith(d + "/")
                            for d in descendant_dirs
                        ):
                            continue
                        rel = src_file.relative_to(full_path)
                        file_module = dict(module)
                        file_module["name"] = f"{module_name}:{rel.as_posix()}"
                        file_module["path"] = file_rel
                        content = src_file.read_text(encoding="utf-8", errors="replace")
                        review_units.append((file_module, content))
                    if not review_units:
                        log.warning("No source files in directory module %s", module_path)
                        module_statuses.append(
                            {
                                "module": module.get("name", module_path),
                                "module_path": module_path,
                                "chunks_total": 0,
                                "chunks_capped": 0,
                                "quick_review_failed_chunks": 0,
                                "specialist_failed_chunks": 0,
                                "findings_count": 0,
                                "skills_run": [],
                                "specialist_attempted_chunks": {},
                                "specialist_failed_chunks_by_skill": {},
                                "review_complete": False,
                                "skip_reason": "no_source_files",
                            }
                        )
                        continue
                    # Per-file review supersedes the module-level seed entries
                    # (set at loop top); coverage is tracked per file instead.
                    module_scope_lines.pop(module_name, None)
                    deterministic_success.pop(module_name, None)
                    reasoning_ranges.pop(module_name, None)
                else:
                    source = full_path.read_text(encoding="utf-8", errors="replace")
                    review_units.append((module, source))
            except OSError as exc:
                log.warning("Cannot read module %s: %s", module_path, exc)
                module_statuses.append(
                    {
                        "module": module.get("name", module_path),
                        "module_path": module_path,
                        "chunks_total": 0,
                        "chunks_capped": 0,
                        "quick_review_failed_chunks": 0,
                        "specialist_failed_chunks": 0,
                        "findings_count": 0,
                        "skills_run": [],
                        "specialist_attempted_chunks": {},
                        "specialist_failed_chunks_by_skill": {},
                        "review_complete": False,
                        "skip_reason": f"read_error: {exc}",
                    }
                )
                continue

            for unit_module, source in review_units:
                unit_name = unit_module.get("name", module_path)
                module_scope_lines[unit_name] = len(source.splitlines())

                # Resume: reuse the prior complete review of this unit.
                prior_status = resume_statuses.get(unit_name)
                if prior_status is not None:
                    log.info("Resume: skipping completed unit %s", unit_name)
                    module_statuses.append(prior_status)
                    deterministic_success[unit_name] = (
                        "deterministic_lane_error" not in prior_status
                    )
                    if self._config.enable_deterministic_lane:
                        deterministic_findings.extend(resume_det.get(unit_name, []))
                    if self._config.enable_retrieval_reasoning_lane:
                        reasoning_findings.extend(resume_rea.get(unit_name, []))
                        if strict_mode:
                            total_lines = module_scope_lines.get(unit_name, 0)
                            if total_lines > 0:
                                reasoning_ranges[unit_name] = [(1, total_lines)]
                        else:
                            entry = resume_manifest.get(unit_name)
                            if entry is not None:
                                retrieval_manifest.append(entry)
                                reasoning_ranges[unit_name] = (
                                    self._extract_manifest_ranges(entry)
                                )
                    else:
                        # Lane off: reconciliation input is the accumulated
                        # reviewer findings, so seed from the preserved legacy
                        # findings; coverage credit mirrors the reviewed path.
                        accumulated_module_findings.extend(
                            resume_legacy.get(unit_name, [])
                        )
                        total_lines = module_scope_lines.get(unit_name, 0)
                        if total_lines > 0:
                            reasoning_ranges[unit_name] = [(1, total_lines)]
                    continue

                if self._config.enable_deterministic_lane:
                    try:
                        deterministic_findings.extend(
                            self._deterministic_analyzer.review_module(
                                unit_module,
                                source,
                                repo_root_canonical=scan_context.repo_root_canonical,
                            )
                        )
                        deterministic_success[unit_name] = True
                    except Exception as exc:
                        # Record the lane failure explicitly (not only a warning)
                        # so completeness/gating consume it; deterministic_success
                        # stays False, reducing recorded coverage
                        # (FIX-REQUIRED-006/022).
                        deterministic_lane_failures[unit_name] = str(exc)
                        log.warning(
                            "Deterministic lane failed on module %s: %s",
                            unit_module.get("name", module_path),
                            exc,
                        )

                review_source = source
                if self._config.enable_retrieval_reasoning_lane:
                    if strict_mode:
                        total_lines = module_scope_lines.get(unit_name, 0)
                        if total_lines > 0:
                            reasoning_ranges[unit_name] = [(1, total_lines)]
                    else:
                        try:
                            module_manifest = self._retrieval_packager.build_module_manifest(unit_module, source)
                            retrieval_manifest.append(module_manifest)
                            reasoning_ranges[unit_name] = self._extract_manifest_ranges(module_manifest)
                            review_source = self._retrieval_packager.render_reasoning_source(module_manifest)
                        except Exception as exc:
                            # Packaging failed: record it and leave reasoning
                            # coverage for this unit empty so full-file coverage
                            # is NOT credited unless the reasoning path actually
                            # succeeds (FIX-REQUIRED-006/022).
                            retrieval_lane_failures[unit_name] = str(exc)
                            log.warning(
                                "Retrieval packaging failed on module %s: %s",
                                unit_module.get("name", module_path),
                                exc,
                            )

                if self._reviewer is None:
                    raise RuntimeError("ReviewOrchestrator not initialized with scan context")
                module_findings, module_status, failed_chunks = self._reviewer.review_module_with_telemetry(
                    unit_module,
                    review_source,
                    manifest,
                )
                # Preserve reviewer findings regardless of reasoning-lane
                # packaging (FIX-REQUIRED-018).
                accumulated_module_findings.extend(module_findings)
                if self._config.enable_retrieval_reasoning_lane:
                    reasoning_findings.extend(module_findings)
                elif module_status.get("review_complete"):
                    total_lines = module_scope_lines.get(unit_name, 0)
                    if total_lines > 0:
                        reasoning_ranges[unit_name] = [(1, total_lines)]
                # Attach explicit lane-failure markers so module_review_status is
                # machine-readable and operator status distinguishes partial
                # analysis from success (FIX-REQUIRED-006/022).
                if unit_name in deterministic_lane_failures:
                    module_status["deterministic_lane_error"] = (
                        deterministic_lane_failures[unit_name]
                    )
                if unit_name in retrieval_lane_failures:
                    module_status["retrieval_lane_error"] = (
                        retrieval_lane_failures[unit_name]
                    )
                    module_status["reasoning_coverage_degraded"] = True
                module_statuses.append(module_status)
                failed_chunk_registry.extend(failed_chunks)

        coverage_report = self._build_coverage_report(
            modules=modules_sorted,
            module_statuses=module_statuses,
            failed_chunk_registry=failed_chunk_registry,
        )
        self._store.write("module_review_status", module_statuses)
        self._store.write("failed_chunk_registry", failed_chunk_registry)
        self._store.write("coverage_report", coverage_report)

        scope_manifest = {
            "total_modules": len(modules_sorted),
            "total_in_scope_lines": sum(module_scope_lines.values()),
            "modules": [
                {"module": module_name, "total_lines": total_lines}
                for module_name, total_lines in sorted(module_scope_lines.items())
            ],
        }
        line_coverage_map = build_line_coverage_map(
            module_scope_lines=module_scope_lines,
            deterministic_success=deterministic_success,
            reasoning_ranges=reasoning_ranges,
            strict_mode=strict_mode,
        )
        coverage_certificate = build_coverage_certificate(
            line_coverage_map=line_coverage_map,
            threshold_pct=self._config.full_coverage_threshold_pct,
        )

        self._store.write("scope_manifest", scope_manifest)
        self._store.write("line_coverage_map", line_coverage_map)
        self._store.write("coverage_certificate", coverage_certificate)
        if self._config.enable_deterministic_lane:
            self._store.write("deterministic_findings", deterministic_findings)
        if self._config.enable_retrieval_reasoning_lane:
            self._store.write("retrieval_manifest", retrieval_manifest)
            self._store.write("reasoning_findings", reasoning_findings)

        # Legacy findings are persisted by the reviewer during Stage 3 and are
        # retained for shadow-mode comparisons even when reconciliation rewrites
        # the canonical findings output.
        legacy_findings = self._store.read("findings")
        finding_conflicts: list[dict[str, Any]] = []

        if self._config.enable_reconciliation:
            reasoning_input = (
                reasoning_findings
                if self._config.enable_retrieval_reasoning_lane
                else accumulated_module_findings
            )
            resolved_findings, finding_conflicts, reconciliation_report = reconcile_findings(
                deterministic_findings=deterministic_findings,
                reasoning_findings=reasoning_input,
            )
            self._store.write("finding_conflicts", finding_conflicts)
            self._store.write(
                "reconciliation_report",
                reconciliation_report,
            )
            self._store.write("findings", resolved_findings)

        if self._config.enable_quality_scorecard:
            scorecard = build_quality_scorecard(
                coverage_report=coverage_report,
                retrieval_manifest=retrieval_manifest,
                finding_conflicts=finding_conflicts,
                failed_chunk_registry=failed_chunk_registry,
                strict_mode=strict_mode,
            )
            self._store.write("quality_scorecard", scorecard)
            self._enforce_quality_scorecard_gates(scorecard)

        if self._config.shadow_compare_mode:
            # read() returns None when findings.json was never written; coerce
            # to an empty list so build_shadow_compare_report gets an iterable.
            final_findings = self._store.read("findings") or []
            shadow_report = build_shadow_compare_report(
                legacy_findings=legacy_findings,
                resolved_findings=final_findings,
                finding_conflicts=finding_conflicts,
            )
            self._store.write("shadow_compare_report", shadow_report)

        if self._config.enforce_review_completeness_gate:
            self._enforce_review_completeness_gate(coverage_report)
        if self._config.enforce_full_coverage_gate:
            self._enforce_full_coverage_gate(coverage_certificate)

        self._store.commit_state("sentinel: Stage 3 complete")

        # Stage 4
        log.info("--- Stage 4: Cross-Module Synthesis ---")
        synthesis = self._synthesizer.run()
        analysis_pack = self._run_additional_phase1_assessments(synthesis)
        self._store.write("analysis_pack", analysis_pack)
        # Gate Phase 1 completion on required additional assessments: their
        # failure metadata is persisted above so reviewers can see which failed,
        # but a required failure must not advance to normal Phase 1 completion
        # (FIX-REQUIRED-014/016).
        #
        # A transient transport failure (gateway timeout / retryable 5xx) on a
        # required assessment is TOLERATED — logged, not blocking — so a single
        # flaky gateway call cannot discard an otherwise-complete Phase 1 run.
        # Genuine failures (malformed response, non-transport error) still
        # hard-fail. Mirrors the Stage-3 coverage gate's transport tolerance
        # (cycle 41) so the two completion gates classify failures consistently.
        required_error_entries = [
            entry
            for entry in analysis_pack
            if entry.get("status") == "error" and entry.get("required")
        ]
        tolerated_transport = [
            entry.get("assessment_id")
            for entry in required_error_entries
            if is_transport_failure(entry)
        ]
        genuine_assessment_failures = [
            entry.get("assessment_id")
            for entry in required_error_entries
            if not is_transport_failure(entry)
        ]
        if tolerated_transport:
            log.warning(
                "Tolerating transient transport failure(s) on required additional "
                "assessment(s): %s — Phase 1 not blocked (transport-incomplete).",
                ", ".join(tolerated_transport),
            )
        if genuine_assessment_failures:
            raise RuntimeError(
                "Phase 1 incomplete: required additional Architect assessment(s) "
                f"failed to execute: {', '.join(genuine_assessment_failures)}. "
                "See analysis_pack.json for the recorded error details."
            )
        phase1_assessment = self._build_phase1_assessment_report(synthesis)
        self._store.write("phase1_assessment", phase1_assessment)
        self._write_phase1_assessment_markdown(phase1_assessment)
        self._store.commit_state("sentinel: Stage 4 complete")

        log.info(
            "Synthesis: %d finding(s) — Critical=%s P1=%s P2=%s P3=%s",
            synthesis["total_findings"],
            synthesis["severity_counts"].get("Critical", 0),
            synthesis["severity_counts"].get("P1", 0),
            synthesis["severity_counts"].get("P2", 0),
            synthesis["severity_counts"].get("P3", 0),
        )

        # Auto-extract learnings after synthesis
        if self._config.auto_extract_learnings:
            learnings_path = (
                Path(self._config.learnings_path)
                if self._config.learnings_path
                else None
            )
            learning_count = _extract_and_store_learnings(
                state_dir=self._config.state_dir,
                repo_paths=repo_paths,
                authored_by_foundry=self._config.authored_by_foundry,
                learnings_path=learnings_path,
            )
            if learning_count is None:
                # findings.json was unreadable — record the failure distinctly
                # rather than reporting zero learnings as a normal outcome.
                log.error("Learning extraction failed: findings unreadable")
                self._audit(
                    "orchestrator",
                    "learnings_extraction_failed",
                    {"reason": "findings_unreadable"},
                )
            else:
                log.info(
                    "Extracted %d learnings from assessment", learning_count
                )
                self._audit(
                    "orchestrator",
                    "learnings_extracted",
                    {"count": learning_count},
                )

        # Stage 4b — Semantic grouping of deduped findings
        self._run_semantic_grouping()

        # Stage 4c — Conceptual theme assembly
        #             (description-embedding clustering)
        self._run_concept_themes()

        # Stage 4d — Apply operator-authored suppression rules (learning pass).
        # Suppressed findings are diverted to a sidecar (not deleted) so the
        # suppression decision stays auditable; active findings are written back.
        if self._config.state_dir:
            from sentinel.orchestrator.suppression import SuppressionManager

            sm = SuppressionManager(Path(self._config.state_dir))
            rules = sm.load_rules()
            if rules:
                findings = self._store.read("findings") or []
                active, suppressed = sm.apply(findings)
                self._store.write("findings", active)
                if suppressed:
                    (
                        Path(self._config.state_dir) / "suppressed_findings.json"
                    ).write_text(
                        json.dumps(suppressed, indent=2), encoding="utf-8"
                    )
                    log.info(
                        "Stage 4d: %d findings suppressed by %d rules",
                        len(suppressed),
                        len(rules),
                    )

        # Stage 5b — Per-group remediation pattern proposals (Architect)
        self._run_remediation_pattern_proposals()

        # Stage 5c — Automated finding verification (Architect adjudicates each
        # finding against its cited source before human triage).
        self._run_finding_verification()

        # Stage 5c-alarm — Alarm relevance classification (Architect labels each
        # verified finding direct/adjacent/none so the Decision Brief can focus
        # on alarm and alarm-adjacent defects). Runs AFTER verification so only
        # surviving findings are classified.
        self._run_alarm_classification()

        # Stage 5 — Human gate: tech debt triage
        tech_debt = self._store.read("tech_debt")
        self._audit("orchestrator", "triage_gate_reached", {"items": len(tech_debt)})

        raise HumanGateRequired(
            gate_type="tech_debt_triage",
            payload={
                "message": (
                    "Stage 5: Tech Debt Triage\n\n"
                    f"{len(tech_debt)} items require human triage. "
                    "For each item, set human_triage to APPROVE, DEFER, or EXCLUDE.\n\n"
                    "Use: sentinel triage --approve <id> | --defer <id> | --exclude <id>\n"
                    "When complete, run: sentinel assess --continue"
                ),
                "tech_debt_file": str(self._config.state_dir / "tech_debt.json"),
                "phase1_assessment_file": str(self._config.state_dir / "phase1_assessment.json"),
                "phase1_assessment_markdown_file": str(self._config.state_dir / "phase1_assessment.md"),
                "analysis_pack_file": str(self._config.state_dir / "analysis_pack.json"),
                "item_count": len(tech_debt),
            },
        )

    def _run_additional_phase1_assessments(self, synthesis: dict[str, Any]) -> list[dict[str, Any]]:
        """Run additional cross-repo risk analyses through the Architect."""
        findings = self._store.read("findings")
        modules = self._store.read("modules")
        dependencies = self._store.read("dependencies")
        if self._config.strict_quality_mode:
            context_payload = {
                "synthesis": synthesis,
                "module_count": len(modules),
                "dependency_count": len(dependencies),
                "findings": findings,
                "dependencies": dependencies,
                "modules": modules,
            }
            serialized_context = json.dumps(context_payload, indent=2)
        else:
            context_payload = {
                "synthesis": synthesis,
                "module_count": len(modules),
                "dependency_count": len(dependencies),
                "findings_sample": findings[:200],
                "dependencies_sample": dependencies[:200],
                "modules_sample": modules[:200],
            }
            serialized_context = json.dumps(context_payload, indent=2)[:30000]
        context = (
            "## Additional Phase 1 Assessment Context\n"
            f"```json\n{serialized_context}\n```\n\n"
            "Return valid JSON with keys: findings (array), assessment_summary (string), and recommended_actions (array)."
        )

        assessments = [
            ("api_contract_compatibility", "API Contract Compatibility", Skills.API_CONTRACT),
            ("data_flow_trust_boundary", "Data Flow and Trust Boundary", Skills.DATA_FLOW_TRUST),
            ("config_environment_drift", "Config and Environment Drift", Skills.CONFIG_DRIFT),
            ("build_release_integrity", "Build and Release Integrity", Skills.BUILD_RELEASE),
            ("change_impact_testability", "Change Impact and Testability", Skills.CHANGE_IMPACT),
            ("operational_risk_failure_modes", "Operational Risk and Failure Modes", Skills.OPERATIONAL_RISK),
        ]

        pack: list[dict[str, Any]] = []
        # In strict quality mode these cross-cutting assessments are required for
        # complete Phase 1 coverage; outside strict mode they are optional and a
        # failure degrades (but does not block) the run (FIX-REQUIRED-014/016).
        required = self._config.strict_quality_mode
        for assessment_id, title, skill in assessments:
            try:
                result = self._architect.specialist_review(skill, context)
                raw_findings = result.get("findings", []) if isinstance(result, dict) else []
                findings_list = raw_findings if isinstance(raw_findings, list) else []
                summary = ""
                if isinstance(result, dict):
                    summary = str(result.get("assessment_summary", result.get("summary", "")))
                recommended_actions = []
                if isinstance(result, dict):
                    raw_actions = result.get("recommended_actions", [])
                    if isinstance(raw_actions, list):
                        recommended_actions = [str(a) for a in raw_actions if str(a).strip()]
                pack.append(
                    {
                        "assessment_id": assessment_id,
                        "title": title,
                        "skill": skill,
                        "required": required,
                        "status": "completed",
                        "summary": summary,
                        "recommended_actions": recommended_actions,
                        "findings": findings_list,
                    }
                )
            except Exception as exc:
                # Record machine-readable failure metadata (assessment id,
                # required/optional, original exception) rather than presenting
                # an empty-findings entry as a normal result
                # (FIX-REQUIRED-014/016).
                log.warning(
                    "Additional assessment %s failed (required=%s): %s",
                    assessment_id, required, exc,
                )
                pack.append(
                    {
                        "assessment_id": assessment_id,
                        "title": title,
                        "skill": skill,
                        "required": required,
                        "status": "error",
                        "summary": "assessment failed during execution",
                        "recommended_actions": [],
                        "findings": [],
                        "error": str(exc),
                        "error_type": type(exc).__name__,
                    }
                )

        return pack

    def _build_phase1_assessment_report(self, synthesis: dict[str, Any]) -> dict[str, Any]:
        """Build a single triage-ready Phase 1 assessment report.

        Combines findings and dependency risk indicators so operators can
        decide whether to include external/cross-repo scope before Phase 2.
        """
        findings = self._store.read("findings")
        dependencies = self._store.read("dependencies")
        analysis_pack = self._store.read("analysis_pack")
        tech_debt = self._store.read("tech_debt")
        repositories = self._store.read("repositories")
        current_repo = repositories[0].get("name", str(self._config.repo_path)) if repositories else str(self._config.repo_path)

        severity_counts: dict[str, int] = {"Critical": 0, "P1": 0, "P2": 0, "P3": 0}
        unknown_severity_count = 0
        findings_snapshot: list[dict[str, Any]] = []
        for index, finding in enumerate(findings or [], start=1):
            # Guard against malformed entries (str/None/int): calling .get() on a
            # non-dict raises AttributeError. Skip anything that is not a mapping.
            if not isinstance(finding, dict):
                continue
            raw_severity = str(finding.get("severity", "P3")).strip()
            canonical_severity = _canonical_severity(raw_severity)
            if canonical_severity is not None:
                severity = canonical_severity
                severity_counts[severity] += 1
            else:
                # Malformed/nonstandard severity: never silently dropped — kept
                # verbatim on the finding and counted in an explicit unknown
                # bucket so summary counts reconcile with emitted findings
                # (FIX-REQUIRED-023).
                severity = raw_severity or "unknown"
                unknown_severity_count += 1
            finding_id = str(finding.get("id", "")).strip() or f"F-{index:04d}"
            trace_id = hashlib.sha1(
                (
                    f"{finding_id}|{finding.get('module', '')}|"
                    f"{finding.get('description', '')}|{severity}"
                ).encode("utf-8", errors="replace")
            ).hexdigest()[:12]
            findings_snapshot.append(
                {
                    "id": finding_id,
                    "trace_id": f"TR-F-{trace_id}",
                    "module": finding.get("module", "unknown"),
                    "severity": severity,
                    "skill": finding.get("skill", "unknown"),
                    "description": finding.get("description", ""),
                    "suggested_fix": finding.get("suggested_fix", ""),
                }
            )

        # Surface any nonstandard severities as an explicit, visible bucket so
        # summary counts reconcile with the emitted findings (FIX-REQUIRED-023).
        if unknown_severity_count:
            severity_counts["unknown"] = unknown_severity_count

        def _looks_like_file_path(value: str) -> bool:
            if not value:
                return False
            return any(
                value.lower().endswith(ext)
                for ext in (
                    ".py", ".cs", ".xml", ".json", ".yaml", ".yml", ".toml",
                    ".csproj", ".wixproj", ".vb", ".vbs", ".ts", ".tsx", ".js", ".jsx",
                )
            )

        def _review_targets_for_edge(source: str, target: str, kind: str) -> list[str]:
            targets: list[str] = []
            if _looks_like_file_path(source):
                targets.append(source)
            if kind in {"project_reference", "assembly_reference"} or _looks_like_file_path(target):
                targets.append(target)
            # Deduplicate while preserving order.
            seen: set[str] = set()
            deduped: list[str] = []
            for candidate in targets:
                candidate = candidate.strip()
                if not candidate or candidate in seen:
                    continue
                seen.add(candidate)
                deduped.append(candidate)
            return deduped

        def _dependency_risk(kind: str, is_external: bool) -> tuple[int, str]:
            base = {
                "project_reference": 75,
                "assembly_reference": 70,
                "package_reference": 55,
                "python_import": 35,
            }.get(kind, 40)
            score = min(100, base + (20 if is_external else 0))
            if score >= 90:
                severity = "Critical"
            elif score >= 75:
                severity = "P1"
            elif score >= 55:
                severity = "P2"
            else:
                severity = "P3"
            return score, severity

        def _dependency_id(source: str, target: str, kind: str) -> str:
            token = f"{source}|{target}|{kind}".encode("utf-8", errors="replace")
            return f"DEP-{hashlib.sha1(token).hexdigest()[:12]}"

        def _external_repo_key(target: str, kind: str) -> str:
            normalized = target.replace("\\", "/").strip()
            if kind == "project_reference" and normalized:
                parts = [segment for segment in normalized.split("/") if segment and segment != "."]
                while parts and parts[0] == "..":
                    parts = parts[1:]
                if parts:
                    return parts[0]
            if "/" in normalized:
                return normalized.split("/")[0]
            return normalized or "(unknown)"

        def _external_candidate_sort_key(item: dict[str, Any]) -> tuple[str, str, str, str, str]:
            target_value = str(item.get("target", "")).strip()
            kind_value = str(item.get("kind", "unknown")).strip()
            return (
                _external_repo_key(target_value, kind_value).lower(),
                target_value.lower(),
                str(item.get("source", "")).strip().lower(),
                kind_value.lower(),
                str(item.get("dependency_id", "")).strip().lower(),
            )

        dependency_items: list[dict[str, Any]] = []
        cross_repo_candidates: list[dict[str, Any]] = []
        repo_chain: list[dict[str, Any]] = [
            {
                "chain_id": "CHAIN-ROOT",
                "repo": current_repo,
                "depth": 0,
                "parent": None,
                "status": "assessed",
            }
        ]
        for edge in dependencies:
            if not isinstance(edge, dict):
                continue
            source = str(edge.get("source", "")).strip()
            target = str(edge.get("target", "")).strip()
            if not source and not target:
                continue

            kind = str(edge.get("kind", "unknown"))
            is_external = bool(edge.get("is_external", False))
            review_targets = _review_targets_for_edge(source, target, kind)
            risk_score, risk_severity = _dependency_risk(kind, is_external)
            dep_id = _dependency_id(source, target, kind)
            item = {
                "dependency_id": dep_id,
                "trace_id": f"TR-D-{dep_id[4:]}",
                "source": source,
                "target": target,
                "kind": kind,
                "is_external": is_external,
                "review_targets": review_targets,
                "follow_on_scope_required": is_external,
                "risk_score": risk_score,
                "severity": risk_severity,
                "triage_decision": None,
                "triage_rationale": "",
                "triage_owner": "",
                "access_status": "requires_customer_access" if is_external else "in_scope",
                "triage_recommendation": (
                    "include_in_scope_or_explicitly_defer" if is_external else "track_in_current_repo"
                ),
            }
            dependency_items.append(item)
            if is_external:
                cross_repo_candidates.append(item)
                repo_chain.append(
                    {
                        "chain_id": f"CHAIN-{dep_id[4:]}",
                        "repo": target,
                        "depth": 1,
                        "parent": current_repo,
                        "status": "pending_access",
                        "dependency_id": dep_id,
                    }
                )

        cross_repo_candidates = sorted(cross_repo_candidates, key=_external_candidate_sort_key)

        cross_repo_groups_map: dict[str, list[dict[str, Any]]] = {}
        for item in cross_repo_candidates:
            group_key = _external_repo_key(
                str(item.get("target", "")).strip(),
                str(item.get("kind", "unknown")).strip(),
            )
            cross_repo_groups_map.setdefault(group_key, []).append(item)

        cross_repo_groups: list[dict[str, Any]] = []
        for repo_name in sorted(cross_repo_groups_map, key=lambda name: str(name).lower()):
            grouped_items = cross_repo_groups_map[repo_name]
            cross_repo_groups.append(
                {
                    "repo": repo_name,
                    "count": len(grouped_items),
                    "dependency_ids": [item.get("dependency_id", "") for item in grouped_items],
                    "sources": sorted(
                        {
                            str(item.get("source", "")).strip()
                            for item in grouped_items
                            if str(item.get("source", "")).strip()
                        }
                    ),
                    "targets": sorted(
                        {
                            str(item.get("target", "")).strip()
                            for item in grouped_items
                            if str(item.get("target", "")).strip()
                        }
                    ),
                    "items": grouped_items,
                }
            )

        additional_assessment_findings = sum(
            len(item.get("findings", []))
            for item in analysis_pack
            if isinstance(item, dict)
        ) if isinstance(analysis_pack, list) else 0

        report = {
            "title": "Phase 1 Assessment",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "summary": {
                "total_findings": len(findings_snapshot),
                "severity_counts": severity_counts,
                "total_dependencies": len(dependency_items),
                "external_dependencies": len(cross_repo_candidates),
                "additional_assessment_findings": additional_assessment_findings,
                "tech_debt_items": len(tech_debt),
                "systemic_patterns": synthesis.get("systemic_patterns", []),
            },
            "roadmap": {
                "objective": (
                    "Provide one customer-triage report before Phase 2 that includes "
                    "bug findings by severity and dependency links that may require "
                    "cross-repo assessment."
                ),
                "mode": "iterative_phase1",
                "decision_points": [
                    "Select findings to APPROVE, DEFER, or EXCLUDE.",
                    "Decide whether external/cross-repo dependencies should be added to assessment scope.",
                    "Proceed to Phase 2 only for approved items across all assessed repositories.",
                ],
                "iteration_policy": [
                    "For each external dependency candidate, request access to the referenced repository.",
                    "Run Phase 1 on the newly accessible repository and append its findings/dependencies to customer triage.",
                    "Repeat until there are no new external dependencies or the customer explicitly defers them.",
                ],
            },
            "repository_chain": repo_chain,
            "findings": findings_snapshot,
            "additional_assessments": analysis_pack if isinstance(analysis_pack, list) else [],
            "dependencies": {
                "items": dependency_items,
                "cross_repo_candidates": cross_repo_candidates,
                "cross_repo_groups": cross_repo_groups,
            },
            "review_scope_queue": {
                "current_repo_files": list(dict.fromkeys(
                    target
                    for item in dependency_items
                    for target in item.get("review_targets", [])
                    if not item.get("is_external")
                )),
                "external_repo_candidates": [
                    {
                        "dependency": item,
                        "access_status": "requires_customer_access",
                        "next_action": "run_phase1_on_referenced_repo_when_available",
                    }
                    for item in cross_repo_candidates
                ],
            },
        }
        report["summary"]["untriaged_external_dependencies"] = sum(
            1
            for item in report["dependencies"]["cross_repo_candidates"]
            if not item.get("triage_decision")
        )
        return report

    def _write_phase1_assessment_markdown(self, report: dict[str, Any]) -> None:
        """Write a human-readable markdown summary for customer triage."""
        summary = report.get("summary", {})
        findings = report.get("findings", [])
        additional_assessments = report.get("additional_assessments", [])
        dependencies = report.get("dependencies", {}).get("cross_repo_candidates", [])
        dependency_groups = report.get("dependencies", {}).get("cross_repo_groups", [])

        lines: list[str] = [
            "# Phase 1 Assessment",
            "",
            f"Generated: {report.get('generated_at', '')}",
            "",
            "## Summary",
            f"- Total findings: {summary.get('total_findings', 0)}",
            f"- Findings by severity: {summary.get('severity_counts', {})}",
            f"- Total dependencies: {summary.get('total_dependencies', 0)}",
            f"- External dependencies: {summary.get('external_dependencies', 0)}",
            f"- Untriaged external dependencies: {summary.get('untriaged_external_dependencies', 0)}",
            f"- Additional assessment findings: {summary.get('additional_assessment_findings', 0)}",
            "",
            "## Findings",
        ]

        if findings:
            for finding in findings:
                lines.append(
                    "- "
                    f"{finding.get('id')} [{finding.get('severity')}] "
                    f"{finding.get('module')}: {finding.get('description')}"
                )
        else:
            lines.append("- No findings recorded.")

        lines.extend(["", "## External Dependency Groups (By Repo)"])
        if dependency_groups:
            for group in sorted(
                dependency_groups,
                key=lambda entry: (-int(entry.get("count", 0)), str(entry.get("repo", "")),),
            ):
                lines.append(
                    "- "
                    f"{group.get('repo')} ({group.get('count', 0)}) "
                    f"dependency_ids={group.get('dependency_ids', [])}"
                )
        else:
            lines.append("- No grouped external dependencies.")

        lines.extend(["", "## External Dependency Candidates"])
        if dependencies:
            for dep in dependencies:
                lines.append(
                    "- "
                    f"{dep.get('dependency_id')} [{dep.get('severity')}] "
                    f"{dep.get('source')} -> {dep.get('target')} "
                    f"(review targets: {dep.get('review_targets', [])})"
                )
        else:
            lines.append("- No external dependency candidates.")

        lines.extend(["", "## Additional Architect Assessments"])
        if additional_assessments:
            for assessment in additional_assessments:
                lines.append(
                    "- "
                    f"{assessment.get('title')} [{assessment.get('status', 'unknown')}]: "
                    f"{assessment.get('summary', '')}"
                )
                for action in assessment.get("recommended_actions", [])[:3]:
                    lines.append(f"  - action: {action}")
        else:
            lines.append("- No additional assessments recorded.")

        lines.extend([
            "",
            "## Iterative Scope Policy",
            "- Request access for each external dependency candidate.",
            "- Run Phase 1 in each newly accessible repository.",
            "- Merge findings and dependency candidates, then repeat until closure.",
            "",
            "## Phase 2 Readiness",
            "- Phase 2 is blocked when external dependencies remain untriaged.",
            "",
        ])

        (self._config.state_dir / "phase1_assessment.md").write_text(
            "\n".join(lines),
            encoding="utf-8",
        )

    def _enforce_phase2_dependency_scope_gate(self) -> None:
        """Block Phase 2 when external dependency scope has not been triaged."""
        report = self._store.read("phase1_assessment")
        if not isinstance(report, dict) or not report:
            return

        unresolved: list[dict[str, Any]] = []
        for dep in report.get("dependencies", {}).get("cross_repo_candidates", []):
            decision = str(dep.get("triage_decision", "")).strip().upper()
            if decision not in {"INCLUDE", "DEFER", "EXCLUDE"}:
                unresolved.append(dep)

        if not unresolved:
            return

        self._audit(
            "orchestrator",
            "dependency_scope_gate_reached",
            {"untriaged_external_dependencies": len(unresolved)},
        )

        raise HumanGateRequired(
            gate_type="dependency_scope_triage",
            payload={
                "message": (
                    "Phase 2 Dependency Scope Gate\n\n"
                    f"{len(unresolved)} external dependency candidate(s) are still untriaged.\n"
                    "Set each dependency triage decision to INCLUDE, DEFER, or EXCLUDE before starting Phase 2.\n\n"
                    "Use: sentinel triage --list-dependencies\n"
                    "Then: sentinel triage --include-dependency <id> | --defer-dependency <id> | --exclude-dependency <id>"
                ),
                "phase1_assessment_file": str(self._config.state_dir / "phase1_assessment.json"),
                "phase1_assessment_markdown_file": str(self._config.state_dir / "phase1_assessment.md"),
                "untriaged_dependency_ids": [dep.get("dependency_id", "") for dep in unresolved],
            },
        )

    def _build_coverage_report(
        self,
        modules: list[dict[str, Any]],
        module_statuses: list[dict[str, Any]],
        failed_chunk_registry: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Build aggregate Stage 3 review coverage telemetry."""
        total_modules = len(modules)
        # Modules with nothing reviewable (no source files, or skipped for path
        # safety) are reported as not-applicable instead of being held against
        # completeness. Completeness is measured over reviewable modules only.
        modules_not_applicable = sum(
            1
            for status in module_statuses
            if str(status.get("skip_reason", "")) in _NOT_APPLICABLE_SKIP_REASONS
        )
        reviewable_modules = max(0, len(module_statuses) - modules_not_applicable)
        # Count completeness over reviewable modules only, so a not-applicable
        # module that happens to be marked review_complete cannot push
        # modules_complete above reviewable_modules (FIX-REQUIRED-002).
        modules_complete = sum(
            1
            for status in module_statuses
            if status.get("review_complete")
            and str(status.get("skip_reason", "")) not in _NOT_APPLICABLE_SKIP_REASONS
        )
        modules_incomplete = max(0, reviewable_modules - modules_complete)

        # Cycle 41: classify failed chunks as transient transport errors
        # (retryable upstream 5xx / gateway timeout) vs genuine review gaps.
        # A reviewable module whose ONLY failures are transport is counted as
        # "transport-incomplete" rather than a genuine gap, so a single flaky
        # upstream call does not block an otherwise-complete assessment.
        transport_failure_chunks, genuine_failed_chunk_entries = classify_failed_chunks(
            failed_chunk_registry
        )
        not_applicable_names = {
            str(status.get("module", ""))
            for status in module_statuses
            if str(status.get("skip_reason", "")) in _NOT_APPLICABLE_SKIP_REASONS
        }
        registry_by_module: dict[str, list[dict[str, Any]]] = {}
        for entry in failed_chunk_registry:
            registry_by_module.setdefault(str(entry.get("module", "")), []).append(entry)
        transport_incomplete_modules = sum(
            1
            for status in module_statuses
            if not status.get("review_complete")
            and str(status.get("module", "")) not in not_applicable_names
            and registry_by_module.get(str(status.get("module", "")))
            and all(
                is_transport_failure(entry)
                for entry in registry_by_module[str(status.get("module", ""))]
            )
        )
        # Clamp to reviewable_modules so genuine completeness can never exceed
        # the reviewable population or yield a >100% percentage
        # (FIX-REQUIRED-002).
        genuine_modules_complete = min(
            modules_complete + transport_incomplete_modules, reviewable_modules
        )

        total_chunks = sum(int(status.get("chunks_total", 0)) for status in module_statuses)
        total_capped_chunks = sum(int(status.get("chunks_capped", 0)) for status in module_statuses)
        # chunks_total is now the pre-cap total (F2-525566f4ab62); the chunks
        # actually attempted are total minus the capped (never-reviewed) ones.
        # Quick-review success is a fraction of *reviewed* chunks, so use the
        # reviewed denominator to keep the metric's meaning unchanged.
        total_reviewed_chunks = max(0, total_chunks - total_capped_chunks)
        quick_review_failed_chunks = sum(
            int(status.get("quick_review_failed_chunks", 0)) for status in module_statuses
        )
        specialist_failed_chunks = sum(
            int(status.get("specialist_failed_chunks", 0)) for status in module_statuses
        )
        repaired_finding_kinds = sum(
            int(status.get("repaired_finding_kinds", 0)) for status in module_statuses
        )

        quick_review_success_pct = (
            ((total_reviewed_chunks - quick_review_failed_chunks) / total_reviewed_chunks) * 100.0
            if total_reviewed_chunks
            else 100.0
        )
        module_completeness_pct = (
            (modules_complete / reviewable_modules) * 100.0 if reviewable_modules else 100.0
        )
        genuine_module_completeness_pct = (
            (genuine_modules_complete / reviewable_modules) * 100.0
            if reviewable_modules
            else 100.0
        )

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "total_modules": total_modules,
            "reviewed_modules": len(module_statuses),
            "reviewable_modules": reviewable_modules,
            "modules_not_applicable": modules_not_applicable,
            "modules_complete": modules_complete,
            "modules_incomplete": modules_incomplete,
            "module_completeness_pct": round(module_completeness_pct, 2),
            "transport_incomplete_modules": transport_incomplete_modules,
            "genuine_modules_complete": genuine_modules_complete,
            "genuine_module_completeness_pct": round(genuine_module_completeness_pct, 2),
            "total_chunks": total_chunks,
            "total_capped_chunks": total_capped_chunks,
            "quick_review_failed_chunks": quick_review_failed_chunks,
            "quick_review_success_pct": round(quick_review_success_pct, 2),
            "specialist_failed_chunks": specialist_failed_chunks,
            "repaired_finding_kinds": repaired_finding_kinds,
            "failed_chunk_entries": len(failed_chunk_registry),
            "transport_failure_chunks": transport_failure_chunks,
            "genuine_failed_chunk_entries": genuine_failed_chunk_entries,
        }

    def _enforce_review_completeness_gate(self, coverage_report: dict[str, Any]) -> None:
        """Raise a human gate when Stage 3 coverage falls below configured thresholds."""
        # Cycle 41: gate on the genuine (non-transport) coverage signal. A
        # reviewable module incomplete only due to a transient upstream 5xx is
        # not held against completeness, and transport failures are tolerated up
        # to TRANSPORT_FAILURE_TOLERANCE; genuine review gaps still hard-fail.
        module_pct = float(
            coverage_report.get(
                "genuine_module_completeness_pct",
                coverage_report.get("module_completeness_pct", 0.0),
            )
        )
        quick_pct = float(coverage_report.get("quick_review_success_pct", 0.0))
        genuine_failed_entries = int(
            coverage_report.get(
                "genuine_failed_chunk_entries",
                coverage_report.get("failed_chunk_entries", 0),
            )
        )
        transport_failed = int(coverage_report.get("transport_failure_chunks", 0))

        module_threshold = float(self._config.review_module_completeness_threshold)
        quick_threshold = float(self._config.review_quick_success_threshold)

        if (
            module_pct >= module_threshold
            and quick_pct >= quick_threshold
            and genuine_failed_entries == 0
            and transport_failed <= TRANSPORT_FAILURE_TOLERANCE
        ):
            return

        self._audit(
            "orchestrator",
            "review_completeness_gate_reached",
            {
                "module_completeness_pct": module_pct,
                "module_threshold": module_threshold,
                "quick_review_success_pct": quick_pct,
                "quick_threshold": quick_threshold,
                "genuine_failed_chunk_entries": genuine_failed_entries,
                "transport_failure_chunks": transport_failed,
                "transport_failure_tolerance": TRANSPORT_FAILURE_TOLERANCE,
            },
        )

        raise HumanGateRequired(
            gate_type="review_completeness",
            payload={
                "message": (
                    "Stage 3: Review Completeness Gate\n\n"
                    "Coverage requirements were not met for this run.\n"
                    f"Module completeness (genuine): {module_pct:.2f}% "
                    f"(threshold: {module_threshold:.2f}%)\n"
                    f"Quick Review success: {quick_pct:.2f}% "
                    f"(threshold: {quick_threshold:.2f}%)\n"
                    f"Genuine failed chunks: {genuine_failed_entries}\n"
                    f"Transient transport failures: {transport_failed} "
                    f"(tolerance: {TRANSPORT_FAILURE_TOLERANCE})\n\n"
                    "Inspect coverage_report.json, module_review_status.json, "
                    "and failed_chunk_registry.json before retrying."
                ),
                "coverage_report_file": str(self._config.state_dir / "coverage_report.json"),
                "module_review_status_file": str(self._config.state_dir / "module_review_status.json"),
                "failed_chunk_registry_file": str(self._config.state_dir / "failed_chunk_registry.json"),
                "coverage_report": coverage_report,
            },
        )

    def _enforce_full_coverage_gate(self, coverage_certificate: dict[str, Any]) -> None:
        """Raise a human gate when line-level full coverage requirements are unmet."""
        if coverage_certificate.get("status") == "pass":
            return

        metrics = coverage_certificate.get("metrics", {})
        self._audit(
            "orchestrator",
            "full_coverage_gate_reached",
            {
                "coverage_pct": metrics.get("coverage_pct", 0.0),
                "threshold_pct": coverage_certificate.get("threshold_pct", 100.0),
                "uncovered_line_count": metrics.get("uncovered_line_count", 0),
                "modules_with_gaps": metrics.get("modules_with_gaps", []),
            },
        )

        raise HumanGateRequired(
            gate_type="full_coverage",
            payload={
                "message": (
                    "Stage 3: Full Coverage Gate\n\n"
                    "Line-level coverage requirements were not met for this run.\n"
                    f"Coverage: {metrics.get('coverage_pct', 0.0):.2f}% "
                    f"(threshold: {float(coverage_certificate.get('threshold_pct', 100.0)):.2f}%)\n"
                    f"Uncovered lines: {metrics.get('uncovered_line_count', 0)}\n"
                    f"Modules with gaps: {len(metrics.get('modules_with_gaps', []))}\n\n"
                    "Inspect coverage_certificate.json and line_coverage_map.json before retrying."
                ),
                "scope_manifest_file": str(self._config.state_dir / "scope_manifest.json"),
                "line_coverage_map_file": str(self._config.state_dir / "line_coverage_map.json"),
                "coverage_certificate_file": str(self._config.state_dir / "coverage_certificate.json"),
                "coverage_certificate": coverage_certificate,
            },
        )

    def _extract_manifest_ranges(self, module_manifest: dict[str, Any]) -> list[tuple[int, int]]:
        """Collect snippet line ranges from retrieval manifest entries for coverage accounting."""
        ranges: list[tuple[int, int]] = []
        for skill_entry in module_manifest.get("skills", []):
            for snippet in skill_entry.get("snippets", []):
                try:
                    start_line = int(snippet.get("start_line", 0))
                    end_line = int(snippet.get("end_line", 0))
                except (TypeError, ValueError):
                    continue
                if start_line <= 0 or end_line <= 0:
                    continue
                if end_line < start_line:
                    start_line, end_line = end_line, start_line
                ranges.append((start_line, end_line))
        return ranges

    def _run_phase0_quality_foundations(self) -> None:
        """Phase 0 extension point. Intentionally no-op unless flags are enabled."""
        if not any(
            [
                self._config.strict_quality_mode,
                self._config.enable_deterministic_lane,
                self._config.enable_retrieval_reasoning_lane,
                self._config.enable_reconciliation,
                self._config.enable_quality_scorecard,
                self._config.shadow_compare_mode,
            ]
        ):
            return
        log.debug("Phase 0 quality foundations enabled (scaffolding only)")

    def _enforce_quality_scorecard_gates(self, scorecard: dict[str, Any]) -> None:
        """Raise a human gate when Phase 4 quality gates fail."""
        gates = scorecard.get("gates", {})
        failed = [name for name, status in gates.items() if status == "fail"]
        if not failed:
            return

        preferred_order = ["lane_conflict_resolution", "confidence_floor", "review_completeness"]
        gate_type = next((name for name in preferred_order if name in failed), failed[0])

        self._audit(
            "orchestrator",
            "quality_scorecard_gate_reached",
            {
                "failed_gates": failed,
                "confidence_label": scorecard.get("confidence_label", "incomplete"),
            },
        )

        raise HumanGateRequired(
            gate_type=gate_type,
            payload={
                "message": (
                    "Stage 3: Quality Scorecard Gate\n\n"
                    f"Failed gates: {', '.join(failed)}\n"
                    f"Confidence label: {scorecard.get('confidence_label', 'incomplete')}\n\n"
                    "Review quality_scorecard.json and reconciliation artifacts, then rerun with improved coverage/conflict resolution."
                ),
                "quality_scorecard_file": str(self._config.state_dir / "quality_scorecard.json"),
                "coverage_report_file": str(self._config.state_dir / "coverage_report.json"),
                "reconciliation_report_file": str(self._config.state_dir / "reconciliation_report.json"),
                "finding_conflicts_file": str(self._config.state_dir / "finding_conflicts.json"),
                "quality_scorecard": scorecard,
            },
        )

    def _classify_findings_adaptive(
        self,
        findings: list[dict],
        repo_root: str,
        min_chunk: int = 5,
    ) -> dict[str, str]:
        """
        Run the verifiability gate with adaptive chunking.

        Classifies ``findings`` against their cited source in a single call;
        on a JSON parse failure (the capped model output truncated the
        classifications array) it splits the list in half and retries each half
        recursively. When a chunk at ``min_chunk`` size still fails it fails
        open — every finding in that chunk defaults to VERIFIABLE (logged so it
        stays auditable). Returns a dict mapping ``finding_id -> verdict``.
        """
        if not findings:
            return {}

        # Proactive initial chunk cap: for smaller/slower providers, break the
        # batch into fixed-size chunks BEFORE any API call so each call fits the
        # provider's context window and finishes before the gateway timeout,
        # instead of sending one oversized call and splitting reactively on a
        # 400/504/truncation failure. 0 = unbounded (single call — preserves
        # LLM's large-context behavior). The reactive split below still runs
        # per chunk as a safety net.
        max_chunk = getattr(self._config, "verifiability_max_chunk", 0)
        if isinstance(max_chunk, int) and max_chunk > 0 and len(findings) > max_chunk:
            merged: dict[str, str] = {}
            for offset in range(0, len(findings), max_chunk):
                if offset:
                    time.sleep(self._config.verifiability_gate_chunk_delay)
                batch = findings[offset:offset + max_chunk]
                merged.update(
                    self._classify_findings_adaptive(batch, repo_root, min_chunk)
                )
            return merged

        from sentinel.orchestrator.source_retrieval import (
            build_verifiability_context,
        )

        context = build_verifiability_context(findings, repo_root)
        try:
            result = self._architect.classify_findings(context)
            return {
                c["finding_id"]: c.get("verdict", "VERIFIABLE")
                for c in result.get("classifications", [])
                if "finding_id" in c
            }
        except (ValueError, AgentError) as exc:
            # Recoverable-by-splitting failures:
            #   * ValueError / AgentResponseError — the capped model output
            #     truncated the classifications JSON.
            #   * A context-window AgentError — the chunk exceeded the
            #     provider's input window (e.g. a smaller-context provider such
            #     as a smaller provider returns a 400 that base.py wraps as AgentError, not
            #     AgentResponseError).
            # Any other AgentError (auth, network, server error) is a genuine
            # failure and must propagate rather than be masked as VERIFIABLE.
            recoverable = (
                isinstance(exc, (ValueError, AgentResponseError))
                or _is_context_window_error(exc)
            )
            if not recoverable:
                raise
            if len(findings) <= min_chunk:
                # Cannot split further — fail open to VERIFIABLE and log so the
                # dropped classification is auditable.
                log.warning(
                    "Verifiability gate: chunk of %d finding(s) failed after "
                    "splitting to minimum size (%s) — defaulting all to "
                    "VERIFIABLE",
                    len(findings), exc,
                )
                # Key on the SAME field the normal path uses (``finding_id``),
                # falling back to ``id``, so fail-open verdicts actually match
                # the findings when the payload uses ``finding_id``.
                fail_open: dict[str, str] = {}
                for f in findings:
                    key = f.get("finding_id") or f.get("id")
                    if key:
                        fail_open[key] = "VERIFIABLE"
                return fail_open
            mid = len(findings) // 2
            left = self._classify_findings_adaptive(
                findings[:mid], repo_root, min_chunk
            )
            # Throttle between chunk calls so a large split doesn't burst the
            # LLM per-window rate limit (configurable; default 0.5s).
            time.sleep(self._config.verifiability_gate_chunk_delay)
            right = self._classify_findings_adaptive(
                findings[mid:], repo_root, min_chunk
            )
            return {**left, **right}

    def continue_phase1_after_triage(self) -> None:
        """
        Continue Phase 1 after human triage of the tech debt register.

        Runs Stage 6 (Remediation Planning) and raises HumanGateRequired
        for plan approval.
        """
        log.info("--- Stage 6: Remediation Planning ---")

        # Filter to only triaged-APPROVE items
        # Guard against a missing/empty state file: read() may return None,
        # which would make the iterations below raise TypeError.
        tech_debt = self._store.read("tech_debt") or []
        approved_items = [
            item for item in tech_debt
            if item.get("human_triage") == "APPROVE"
        ]
        deferred_count = sum(
            1 for item in tech_debt if item.get("human_triage") == "DEFER"
        )
        excluded_count = sum(
            1 for item in tech_debt if item.get("human_triage") == "EXCLUDE"
        )

        log.info(
            "Triage results: %d approved, %d deferred, %d excluded",
            len(approved_items),
            deferred_count,
            excluded_count,
        )

        if not approved_items:
            log.warning("No tech debt items approved for remediation — Phase 2 not needed")
            self._audit(
                "orchestrator",
                "phase1_complete_no_items",
                {"message": "All items deferred or excluded"},
            )
            return

        # Build Fix Planner context and call Architect
        synthesis_ctx = self._synthesizer.build_fix_planner_context()

        # Read Stage 5b remediation patterns and the SemanticGroups they
        # reference. The group_member_map is built once per invocation
        # (the harness guards against per-pattern rebuilds via
        # test_semantic_groups_read_once_per_invocation).
        #
        # semantic_groups read via StateStore (promoted cycle 17 sub-step 1B).
        all_patterns = self._store.read("remediation_patterns") or []
        all_groups = self._store.read("semantic_groups")
        group_member_map: dict[str, list[str]] = {
            group.get("group_id"): group.get("member_finding_ids", []) or []
            for group in all_groups
        }

        # Partition by pattern_kind. Registry blockers are exempt from
        # the triage filter; actionable patterns flow only when at least
        # one member finding survived APPROVE.
        registry_blockers = [
            p for p in all_patterns
            if p.get("pattern_kind") == "registry_conflict"
        ]
        actionable_candidates = [
            p for p in all_patterns
            if p.get("pattern_kind") != "registry_conflict"
        ]

        all_findings = self._store.read("findings") or []
        triage_by_finding_id: dict[str, str] = {}
        for item in tech_debt:
            fid = item.get("finding_id")
            if fid:
                triage_by_finding_id[fid] = item.get("human_triage", "")
        approved_finding_ids = {
            item.get("finding_id")
            for item in approved_items
        }

        actionable_patterns: list[dict[str, Any]] = []
        for pattern in actionable_candidates:
            member_ids = group_member_map.get(pattern.get("group_id"), [])
            if any(fid in approved_finding_ids for fid in member_ids):
                actionable_patterns.append(pattern)
                continue
            # Skip + audit. Build triage summary by walking member findings;
            # 0 members is a distinct skip shape (orphan pattern) but uses
            # the same event name with member_finding_count: 0.
            triage_summary: dict[str, int] = {}
            for fid in member_ids:
                tag = triage_by_finding_id.get(fid, "UNKNOWN")
                triage_summary[tag] = triage_summary.get(tag, 0) + 1
            self._audit(
                "orchestrator",
                "remediation_pattern_skipped",
                {
                    "pattern_id": pattern.get("pattern_id"),
                    "reason": "no_approved_members",
                    "member_finding_count": len(member_ids),
                    "member_triage_summary": triage_summary,
                },
            )

        # Scope the Fix Planner to the approved findings only — a partial triage
        # should plan just the approved subset, not the full findings list.
        approved_findings = [
            f for f in synthesis_ctx["findings"]
            if f.get("id") in approved_finding_ids
        ]

        # Build the shared context suffix (approved tech debt + dependency
        # scope) once, outside the per-pattern loop. phase1_assessment is read
        # here (once); semantic_groups was read above (group_member_map) — no
        # file reads occur inside the loop.
        context_suffix = (
            f"\n\n### Approved Tech Debt Items ({len(approved_items)})\n"
            f"```json\n{json.dumps(approved_items, indent=2)[:6000]}\n```"
        )
        phase1_assessment = self._store.read("phase1_assessment")
        if isinstance(phase1_assessment, dict) and phase1_assessment:
            dependency_scope = phase1_assessment.get("dependencies", {}).get("cross_repo_candidates", [])
            context_suffix += (
                f"\n\n### Dependency Scope Decisions ({len(dependency_scope)})\n"
                f"```json\n{json.dumps(dependency_scope, indent=2)[:6000]}\n```"
            )

        # Batch the Fix Planner so no single response must hold the whole plan
        # (token-limit safety): one call for registry blockers, one call per
        # actionable pattern (findings scoped to that pattern's approved
        # members), and a single fallback call when there are neither — so at
        # least one call is always made and contexts[0] exists.
        _TIERS = ("blocking", "required", "desired", "nice_to_have")
        _PREFIX = {
            "blocking": "FIX-BLOCKING",
            "required": "FIX-REQUIRED",
            "desired": "FIX-DESIRED",
            "nice_to_have": "FIX-NICE-TO-HAVE",
        }
        acc: dict[str, list[Any]] = {t: [] for t in _TIERS}
        acc["deferred"] = []
        notes_parts: list[str] = []
        per_call_plans: list[dict[str, Any]] = []

        # --- Verifiability gate (adaptive chunking) ---
        # Classify all approved findings against their cited source before the
        # planner loop. On truncated/invalid JSON the classify call is split in
        # half and retried recursively (_classify_findings_adaptive), so a large
        # approved set no longer fails the whole gate. ADVISORY findings (false
        # preconditions / advisory concerns) are filtered out so they never
        # become tasks; FIXTURE-COMPLEX are flagged but kept.
        _verdict_map = self._classify_findings_adaptive(
            approved_findings,
            str(self._config.repo_path),
        )
        _advisory_ids = {
            fid for fid, v in _verdict_map.items() if v == "ADVISORY"
        }
        _fixture_ids = {
            fid for fid, v in _verdict_map.items() if v == "FIXTURE-COMPLEX"
        }
        log.info(
            "Verifiability gate: %d VERIFIABLE, %d FIXTURE-COMPLEX, "
            "%d ADVISORY (filtered from planner)",
            len(_verdict_map) - len(_advisory_ids) - len(_fixture_ids),
            len(_fixture_ids),
            len(_advisory_ids),
        )
        approved_findings = [
            f for f in approved_findings
            if f.get("id") not in _advisory_ids
        ]

        # Persist advisory findings as review notes (filtered, not lost). The
        # adaptive gate returns verdicts only, so per-finding rationale is no
        # longer captured here.
        self._store.write(
            "advisory_findings",
            [{"finding_id": fid, "rationale": ""} for fid in _advisory_ids],
        )

        # Stage 5d — bundle the (verifiability-filtered) approved findings into
        # root-cause-anchored remediation bundles. Persisted to bundles.json so
        # the decision brief and PR builder can group by root cause. Bundles are
        # stamped onto plan items just before the plan is persisted (below).
        bundles: list[dict[str, Any]] = []
        if self._config.verification_stage:
            from sentinel.orchestrator.bundle_planner import BundlePlanner

            bundles = BundlePlanner().build_bundles(approved_findings)
            self._store.write("bundles", bundles)
            _conf_counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0, "SINGLETON": 0}
            for _b in bundles:
                _conf_counts[_b.get("confidence", "SINGLETON")] = (
                    _conf_counts.get(_b.get("confidence", "SINGLETON"), 0) + 1
                )
            log.info(
                "Stage 5d: %d findings -> %d bundles "
                "(%d HIGH, %d MEDIUM, %d LOW, %d singletons)",
                len(approved_findings),
                len(bundles),
                _conf_counts["HIGH"],
                _conf_counts["MEDIUM"],
                _conf_counts["LOW"],
                _conf_counts["SINGLETON"],
            )

        def _run_planner(findings_subset, patterns_subset, blockers_subset):
            ctx = build_fix_planner_context(
                findings=findings_subset,
                modules=synthesis_ctx["modules"],
                dependencies=synthesis_ctx["dependencies"],
                patterns=patterns_subset,
                registry_blockers=blockers_subset,
            )
            ctx += context_suffix
            result = self._architect.build_remediation_plan(ctx)
            # Validate the planner response shape before dereferencing it. A
            # non-dict payload (e.g. a bare list or null) would otherwise raise
            # an opaque AttributeError on ``.get`` mid-loop and corrupt the
            # accumulated plan (FIX-BLOCKING-061).
            if not isinstance(result, dict):
                raise AgentResponseError(
                    "Remediation planner returned "
                    f"{type(result).__name__}, expected a JSON object."
                )
            for tier in _TIERS:
                acc[tier].extend(result.get(tier, []) or [])
            acc["deferred"].extend(result.get("deferred", []) or [])
            if result.get("notes"):
                notes_parts.append(str(result["notes"]))
            per_call_plans.append(result)

        made_call = False
        if registry_blockers:
            _run_planner(approved_findings, [], registry_blockers)
            made_call = True
        for pattern in actionable_patterns:
            member_ids = group_member_map.get(pattern.get("group_id"), [])
            pattern_findings = [
                f for f in approved_findings
                if f.get("id") in member_ids and f.get("id") in approved_finding_ids
            ]
            _run_planner(pattern_findings, [pattern], [])
            made_call = True
        if not made_call:
            _run_planner(approved_findings, [], [])

        # Dedup: the planner runs once per pattern batch, so a finding that is
        # a member of multiple SemanticGroups gets planned repeatedly across
        # batches. Collapse plan items by exact source_findings set, keeping the
        # first (tier order in _TIERS preserves blocking > required > desired).
        _dedup_plan_items(acc, _TIERS)

        # Renumber item IDs globally per tier (each call starts at -001, so
        # accumulation would collide otherwise). Only `id` is rewritten.
        for tier in _TIERS:
            for i, item in enumerate(acc[tier], start=1):
                item["id"] = f"{_PREFIX[tier]}-{i:03d}"

        def _num(value: Any) -> float:
            try:
                return float(str(value).strip())
            except (TypeError, ValueError):
                return 0.0

        # Recompute effort/critical-path from the final deduplicated plan items
        # (not the per-call planner summaries), so a finding planned across
        # multiple pattern batches is counted exactly once (FIX-REQUIRED-003).
        total_effort_hours = sum(
            _num(item.get("effort_hours"))
            for tier in _TIERS
            for item in acc[tier]
        )
        critical_path_hours = sum(
            _num(item.get("effort_hours"))
            for tier in ("blocking", "required")
            for item in acc[tier]
        )
        critical_path_days = (
            int(critical_path_hours // _WORK_HOURS_PER_DAY)
            + (1 if critical_path_hours % _WORK_HOURS_PER_DAY else 0)
        )

        plan = {
            "plan_version": per_call_plans[0].get("plan_version", "1.0"),
            "generated_from_findings": len(approved_findings),
            "summary": {
                "blocking": len(acc["blocking"]),
                "required": len(acc["required"]),
                "desired": len(acc["desired"]),
                "nice_to_have": len(acc["nice_to_have"]),
                "total_effort_hours": total_effort_hours,
                "critical_path_days": critical_path_days,
            },
            "blocking": acc["blocking"],
            "required": acc["required"],
            "desired": acc["desired"],
            "nice_to_have": acc["nice_to_have"],
            "deferred": acc["deferred"],
            "notes": "\n\n".join(notes_parts),
        }

        # Stage 5d — stamp bundle_id + bundle_status onto each plan item by
        # matching its source_findings to the bundle whose member findings
        # contain them (items with no bundle match are left unstamped).
        if bundles:
            from sentinel.orchestrator.bundle_planner import (
                stamp_plan_items_with_bundles,
            )

            stamp_plan_items_with_bundles(plan, bundles)

        # Stage 6 persistence (site 6): the remediation plan and its
        # "ready for approval" audit entry commit together so a crash can never
        # leave a plan on disk without its audit record (or vice versa).
        plan_audit = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agent": "orchestrator",
            "action": "plan_ready_for_approval",
            "details": {"plan_keys": list(plan.keys())},
        }
        audit_log = self._store.read("audit_log")
        if not isinstance(audit_log, list):
            audit_log = []
        audit_log.append(plan_audit)
        self._store.atomic_write(
            [("plan", plan), ("audit_log", audit_log)],
            commit=True,
            commit_message="sentinel: Stage 6 — remediation plan",
        )

        raise HumanGateRequired(
            gate_type="plan_approval",
            payload={
                "message": (
                    "Stage 6: Remediation Plan Ready for Approval\n\n"
                    "Review the plan and approve or reject each item.\n"
                    "Use: sentinel plan --approve | --reject\n"
                    "When approved, run: sentinel phase2 --start"
                ),
                "plan_file": str(self._config.state_dir / "plan.json"),
            },
        )

    # ------------------------------------------------------------------
    # Phase 2 — Governed Remediation
    # ------------------------------------------------------------------

    def _scope_changed_files(self, impl_result: dict, diff_summary: str) -> list[str]:
        """Reliable changed-file source for the SCOPE_LIMIT gate (cycle 55):
        prefer the coder's structured ``files_changed`` list, then the git
        working tree (``classify_changed_files``), then — last resort — the prose
        diff summary. The coder is LLM-prose-based and does not commit per task,
        so a ``HEAD~1..HEAD`` git diff is not used here (it would return unrelated
        prior-commit files); ``get_changed_files_from_git`` is the tested utility
        for explicit ref-diffing once changes are actually committed."""
        files = [
            str(f) for f in ((impl_result or {}).get("files_changed") or [])
            if str(f).strip()
        ]
        if files:
            self._scope_files_source = "coder_structured"
            return files
        try:
            from sentinel.govern.review_policy import (
                ReviewPolicyError,
                classify_changed_files,
            )
            buckets = classify_changed_files(self._config.repo_path)
            files = buckets.review_required + buckets.auto_accept
            if files:
                self._scope_files_source = "git_classification"
                return files
            self._scope_files_source = "git_classification_empty"
        except (ReviewPolicyError, OSError) as exc:
            # Expected classification failures (git error / unreadable repo) fall
            # back to diff-summary parsing, but the degradation is made explicit
            # with the repo path and exception context (FIX-REQUIRED-004/007).
            # Unexpected exception types are intentionally not caught here — they
            # propagate rather than being silently swallowed.
            self._scope_files_source = "degraded_diff_fallback"
            log.warning(
                "Changed-file classification failed for repo %s (%s: %s); "
                "falling back to diff-summary parsing — scope accuracy degraded",
                self._config.repo_path, type(exc).__name__, exc,
            )
            return _changed_files_from_diff(diff_summary)
        return _changed_files_from_diff(diff_summary)

    def run_phase2(self, task_id: str | None = None, limit: int | None = None) -> None:
        """
        Execute Phase 2: governed remediation of approved plan items.

        Decomposes the plan into atomic tasks and drives each task through
        the full lifecycle: PLANNED → ASSIGNED → TODO → review → QA →
        DOC → PR → HUMAN_MERGED → COMPLETE.

        Raises HumanGateRequired when:
          - A task is escalated (dispute not resolved)
          - A PR is ready for human review and merge
        """
        plan = self._store.read("plan")
        if not plan:
            raise RuntimeError("No approved plan found. Run Phase 1 first.")

        self._enforce_phase2_dependency_scope_gate()

        self._audit("orchestrator", "phase2_started", {})
        log.info("=== Phase 2: Governed Remediation ===")

        if task_id:
            log.info("Running targeted task only: %s", task_id)
            task = self._task_manager.load_task(task_id)
            if task.state == TaskState.COMPLETE:
                log.info("Task %s already COMPLETE", task_id)
                return
            self._run_task(task)
            self._audit("orchestrator", "phase2_targeted_task_complete", {"task_id": task_id})
            return

        # Decompose plan into atomic tasks
        plan_items = self._extract_plan_items(plan)
        log.info("Decomposing %d plan items into atomic tasks", len(plan_items))

        # cycle 52 — thread structured triage conditions onto tasks via the
        # pattern → group → findings lineage. Built once per Phase-2 run.
        from sentinel.govern.conditions import get_task_finding_ids, merge_conditions
        _fid_conditions: dict[str, list[dict]] = {}
        for _td in (self._store.read("tech_debt") or []):
            _fid = _td.get("finding_id")
            _cs = _td.get("conditions_structured")
            if _fid and _cs:
                _fid_conditions[_fid] = _cs
        _pattern_to_group = {
            p.get("pattern_id"): p.get("group_id")
            for p in (self._store.read("remediation_patterns") or [])
            if p.get("pattern_id")
        }
        _group_members = {
            g.get("group_id"): (g.get("member_finding_ids") or [])
            for g in (self._store.read("semantic_groups") or [])
        }

        processed = 0
        completed = 0
        escalated = 0
        errors = 0

        for item in plan_items:
            plan_id = item.get("id", "unknown")
            task = self._task_manager.load_task_by_plan_id(plan_id)
            if task is None:
                # Q8c key-presence disambiguation: absent key -> default to
                # the standard verification skill (preserves pre-Commit-3
                # plan.json semantics where every item ran layer6a).
                # Present key (even empty list) -> honor as-is, which lets
                # FIX-BLOCKING-* registry blockers signal "no automated
                # verification" via an explicit empty list.
                if "verification_required" in item:
                    verification_required = list(item["verification_required"])
                else:
                    verification_required = ["layer6a-verification-reviewer"]

                # Q8d skill validation: fail-fast at the planner -> executor
                # boundary. SkillLoader.load() raises SkillNotFoundError on
                # miss; we let it propagate so unknown skill names surface
                # immediately rather than deep in the QA pipeline.
                for skill_name in verification_required:
                    self._loader.load(skill_name)

                # cycle 52 — resolve + merge structured conditions for this task
                # via its pattern lineage; orphan (pattern but no findings) warns.
                source_pattern = item.get("source_pattern", "")
                member_fids = get_task_finding_ids(
                    {"pattern_id": source_pattern}, _pattern_to_group, _group_members
                )
                if source_pattern and not member_fids:
                    log.warning(
                        "Plan item %s references pattern %s with no resolvable "
                        "findings; no conditions attached", plan_id, source_pattern
                    )
                task_conditions = merge_conditions(
                    [_fid_conditions.get(fid) for fid in member_fids]
                )

                # Phase 2A — the hard scope gate begins at decomposition: every
                # task carries a SCOPE_LIMIT condition built from its
                # affected_files, so the gate is never a no-op (conditions_
                # structured was null on most tasks before, making it silent).
                normalized_affected = _normalize_affected_files(
                    item.get("affected_files", []),
                    str(self._config.repo_path),
                )
                if normalized_affected:
                    from sentinel.govern.conditions import ConditionItem
                    scope_condition = ConditionItem(
                        type="SCOPE_LIMIT",
                        target=",".join(normalized_affected),
                        note="Agentic executor must edit only the listed files",
                    ).model_dump()
                    task_conditions = list(task_conditions or []) + [scope_condition]

                # S2-2 — thread bundle lineage from the plan item (stamped at
                # Stage 5d). Absent on pre-S2-2 plans -> None / UNDECIDED.
                from sentinel.assess.finding_status import BundleStatus as _BundleStatus
                task = self._task_manager.create_task(
                    plan_id=plan_id,
                    description=item.get("description", ""),
                    acceptance_criteria=item.get("acceptance_criteria", []),
                    affected_files=normalized_affected,
                    scope_boundary=item.get("scope_boundary", ""),
                    required_skills=item.get("required_skills", []),
                    pattern_id=source_pattern,
                    verification_required=verification_required,
                    conditions_structured=task_conditions,
                    bundle_id=item.get("bundle_id"),
                    bundle_status=item.get("bundle_status", _BundleStatus.UNDECIDED),
                )
            elif task.state in (TaskState.COMPLETE, TaskState.CANCELLED):
                # CANCELLED is terminal: a batch driver that cancels a failed
                # task must not have the next `govern --start` re-run it (which
                # would stall a --limit 1 loop on the first failure forever).
                log.info(
                    "Skipping %s task %s (%s)",
                    task.state.value, task.task_id, plan_id,
                )
                continue
            elif task.state == TaskState.PR_CREATED:
                sync = self.sync_pr_status(task.task_id)
                sync_outcome = sync.get("outcome", "PENDING")
                if sync_outcome in {"MERGED", "REJECTED"}:
                    task = self._task_manager.load_task(task.task_id)
                    if task.state == TaskState.COMPLETE:
                        continue
                    if task.state in {TaskState.IN_PROGRESS, TaskState.REVIEW_FAIL}:
                        self._run_task(task)
                        continue

                raise HumanGateRequired(
                    gate_type="pr_review",
                    payload={
                        "task_id": task.task_id,
                        "pr_url": task.pr_url,
                        "message": (
                            f"Task {task.task_id} is awaiting human PR decision.\n"
                            f"PR: {task.pr_url or 'check sentinel/state/tasks.json'}\n\n"
                            f"Auto-sync status: {sync_outcome}\n"
                            f"Auto-sync detail: {sync.get('message', '')}\n\n"
                            "To refresh from GitHub now: sentinel task --sync <task_id>\n"
                            "Approve + merge: sentinel task --approve <task_id> --comment \"optional note\"\n"
                            "Reject (required reason): sentinel task --reject <task_id> --comment \"required reason\""
                        ),
                    },
                )
            # A single task's escalation must not abort the whole batch: record
            # it and continue so `--limit N` attempts all N tasks. Expected
            # escalations are HumanGateRequired (review/PR/doc gates) and
            # RuntimeError (e.g. no file changes / old_text mismatch). Any other
            # exception is an unknown failure and still propagates — we never
            # swallow it.
            try:
                self._run_task(task)
                completed += 1
            except HumanGateRequired as gate:
                escalated += 1
                log.warning(
                    "Task %s escalated to human gate (%s); recording ESCALATED "
                    "and continuing batch",
                    task.task_id, gate.gate_type,
                )
                self._record_batch_progress(task, "ESCALATED")
            except RuntimeError as exc:
                errors += 1
                log.warning(
                    "Task %s failed (%s); recording ESCALATED and continuing batch",
                    task.task_id, exc,
                )
                self._record_batch_progress(task, "ESCALATED")

            processed += 1
            if limit is not None and processed >= limit:
                log.info("Phase 2 limit reached: processed %d task(s)", processed)
                break

        log.info(
            "Wave complete: %d complete, %d escalated, %d errors",
            completed, escalated, errors,
        )
        self._audit(
            "orchestrator",
            "phase2_complete",
            {"completed": completed, "escalated": escalated, "errors": errors},
        )
        log.info("=== Phase 2 Complete ===")

    def _record_batch_progress(self, task: Task, status: str) -> None:
        """Best-effort write of this task's status to batch_summary.json (Screen 3
        progress). Advisory UI data — a write failure must never break the run."""
        try:
            write_batch_summary(
                self._config.state_dir, task.task_id, status, task.review_attempts
            )
        except Exception as exc:  # noqa: BLE001 — progress summary is advisory
            log.debug("batch_summary write skipped for %s: %s", task.task_id, exc)

    @staticmethod
    def _qa_failure_feedback(
        per_skill_results: list[dict],
        per_skill_verdicts: list[dict],
    ) -> str:
        """Select deterministic QA failure feedback from the failing skill(s).

        Feedback is drawn from the skills whose verdict is not PASS, in
        execution order, each labelled with its skill name (FIX-BLOCKING-005).
        Falls back to the first available non-empty feedback so a failure is
        never reported without any feedback (e.g. when only a synthetic gate
        such as scope-limit fails and has no per-skill model result).
        """
        parts: list[str] = []
        for result, verdict in zip(per_skill_results, per_skill_verdicts):
            if str(verdict.get("verdict", "")).upper() != "PASS":
                feedback = str(result.get("feedback", "")).strip()
                skill = verdict.get("skill", "qa")
                if feedback:
                    parts.append(f"[{skill}] {feedback}")
        if parts:
            return "\n\n".join(parts)
        for result in per_skill_results:
            feedback = str(result.get("feedback", "")).strip()
            if feedback:
                return feedback
        return ""

    def _run_task(self, task: Task) -> None:
        """Drive a single task through its complete lifecycle."""
        log.info("--- Task %s: %s ---", task.task_id, task.description[:60])

        # Resume from intermediate states when rerunning Phase 2.
        if task.state == TaskState.PR_CREATED and self._config.self_approve_mode:
            raise HumanGateRequired(
                gate_type="local_review",
                payload={
                    "task_id": task.task_id,
                    "message": (
                        f"Task {task.task_id} is ready for local review.\n"
                        f"Show diff: sentinel task --show-diff {task.task_id}\n"
                        f"Request changes: sentinel task --request-changes {task.task_id} --comment \"required reason\"\n"
                        f"Approve + commit + push: sentinel task --approve-local {task.task_id} --comment \"optional note\""
                    ),
                },
            )

        resume_from_implementation = task.state in {
            TaskState.REVIEW_FAIL,
            TaskState.QA_FAIL,
            TaskState.IN_PROGRESS,
        }

        diff_summary = ""
        impl_result: dict[str, Any] = {}

        if task.state in {TaskState.PLANNED, TaskState.ASSIGNED, TaskState.TODO_SUBMITTED, TaskState.TODO_APPROVED} or resume_from_implementation:
            if resume_from_implementation:
                if task.state in {TaskState.REVIEW_FAIL, TaskState.QA_FAIL}:
                    task = self._task_manager.start_implementation(task)
                coding_context = self._task_manager.build_coding_context(task)
            else:
            # PLANNED → ASSIGNED
                if task.state == TaskState.PLANNED:
                    task = self._task_manager.assign_task(task, agent="coder")
                coding_context = self._task_manager.build_coding_context(task)

                if task.state in {TaskState.ASSIGNED, TaskState.TODO_SUBMITTED}:
                    # TODO submission loop (Coder submits, Orchestrator validates, Architect reviews)
                    max_todo_attempts = 5
                    for attempt in range(1, max_todo_attempts + 1):
                        log.info("Task %s: TODO submission attempt %d/%d", task.task_id, attempt, max_todo_attempts)

                        try:
                            todo_raw = self._coder.submit_todo(coding_context)
                            todo_json = json.dumps(todo_raw)
                            todo = self._task_manager.receive_todo(task, todo_json)
                        except TodoValidationError as exc:
                            log.warning("Task %s: TODO rejected — %s", task.task_id, exc)
                            if attempt >= max_todo_attempts:
                                raise RuntimeError(
                                    f"Task {task.task_id}: max TODO attempts reached"
                                ) from exc
                            # Feed back rejection to coder
                            coding_context = (
                                coding_context
                                + f"\n\n## TODO Rejected (Attempt {attempt})\n\n"
                                + str(exc)
                                + "\n\nPlease revise and resubmit."
                            )
                            continue

                        # Architect reviews TODO
                        architect_context = self._task_manager.build_architect_todo_context(task, todo)
                        try:
                            verdict = self._architect.review_todo(architect_context)
                        except Exception as exc:
                            log.error("Task %s: Architect TODO review failed: %s", task.task_id, exc)
                            raise

                        impl_verdict = verdict.get("implementation_plan_verdict", "REVISE")
                        stubs_verdict = verdict.get("test_stubs_verdict", "REVISE")
                        task = self._task_manager.record_todo_approval(
                            task,
                            impl_verdict=impl_verdict,
                            stubs_verdict=stubs_verdict,
                            impl_feedback=verdict.get("implementation_plan_feedback", ""),
                            stubs_feedback=verdict.get("test_stubs_feedback", ""),
                        )

                        if task.state.value == "TODO_APPROVED":
                            break

                        # Revise — update coding context with feedback
                        coding_context = (
                            self._task_manager.build_coding_context(task)
                            + f"\n\n## Revision Feedback (Attempt {attempt})\n\n"
                            + f"Implementation: {verdict.get('implementation_plan_feedback', '')}\n"
                            + f"Test stubs: {verdict.get('test_stubs_feedback', '')}"
                        )
                    else:
                        raise RuntimeError(f"Task {task.task_id}: TODO approval loop exhausted")

                # TODO_APPROVED → IN_PROGRESS (TDD gate enforced here)
                if task.state == TaskState.TODO_APPROVED:
                    try:
                        task = self._task_manager.start_implementation(task)
                    except GateViolationError as exc:
                        log.error("GOVERNANCE VIOLATION: %s", exc)
                        raise

            # Capture the pre-fix HEAD before the Coder edits, for the semantic
            # verification gate (SENTINEL_VERIFICATION_GATE). Idempotent.
            task = self._capture_pre_fix_sha(task)

            # Coder implements
            impl_context = self._task_manager.build_implementation_context(
                task, repo_root=str(self._config.repo_path))
            impl_result = self._coder.implement(impl_context)

            # cycle 58 — persist files_changed + test fields onto the task so
            # the scoped commit and PR body can use them at approval time.
            task = self._task_manager.record_implementation_result(task, impl_result)

            # Coder self-review
            diff_summary = impl_result.get("diff_summary", "")
            self_review = self._coder.self_review(f"## Diff\n\n{diff_summary}")
            self_review_findings = self_review.get("findings", [])

            critical_self = [
                f for f in self_review_findings
                if f.get("severity") in ("Critical", "P1")
            ]
            if critical_self:
                # Self-review gate: Critical/P1 findings must NOT reach the
                # Architect diff review. Send the task back to the Coder for
                # revision (it stays IN_PROGRESS), mirroring the REVIEW_FAIL
                # send-back and bounded by the shared review_attempts cap. On
                # exhaustion, escalate to a human the same way the Architect /
                # QA / compat exhaustion paths do.
                task = dataclasses.replace(
                    task, review_attempts=task.review_attempts + 1
                )
                self_review_feedback = "; ".join(
                    f"{f.get('severity', '?')}: "
                    f"{f.get('description') or f.get('summary') or f.get('title') or ''}"
                    for f in critical_self
                )
                retry_md = build_retry_context(
                    task_id=task.task_id,
                    gate_name="self_review",
                    gate_feedback=self_review_feedback,
                    test_failures=None,
                    attempt_number=task.review_attempts + 1,
                    base_dir=self._config.state_dir,
                )
                task = dataclasses.replace(task, retry_context=retry_md)
                self._task_manager._persist(task)
                if task.review_attempts >= 3:
                    log.warning(
                        "Task %s: self-review found Critical/P1 issues — "
                        "escalating after %d attempt(s)",
                        task.task_id, task.review_attempts,
                    )
                    # ESCALATED is only reachable from IN_PROGRESS via the
                    # SUBMITTED → DISPUTED → ESCALATED path (the transition map
                    # is unchanged). No Architect review runs — we route
                    # straight to a human, mirroring the empty-diff escalation.
                    task = self._task_manager.submit_for_review(
                        task, diff_summary=diff_summary
                    )
                    task = self._task_manager.mark_disputed(task)
                    task = self._task_manager.escalate_dispute(task)
                    self._record_batch_progress(task, "ESCALATED")
                    raise HumanGateRequired(
                        gate_type="self_review_failure",
                        payload={
                            "task_id": task.task_id,
                            "feedback": self_review_feedback,
                            "review_attempts": task.review_attempts,
                            "message": (
                                f"Coder self-review found unresolved Critical/P1 "
                                f"issues for task {task.task_id} after "
                                f"{task.review_attempts} attempts. Human review "
                                "required."
                            ),
                        },
                    )
                log.warning(
                    "Task %s: self-review found Critical/P1 issues — "
                    "sending back for revision (attempt %d/3)",
                    task.task_id, task.review_attempts,
                )
                self._record_batch_progress(task, "IN_PROGRESS")
                # Re-run from IN_PROGRESS with the self-review feedback injected.
                self._run_task(task)
                return

            # IN_PROGRESS → SUBMITTED
            task = self._task_manager.submit_for_review(task, diff_summary=diff_summary)

        if task.state == TaskState.SUBMITTED:
            local_only = getattr(self._config, "local_only_mode", False)
            if not local_only and not task.files_changed:
                # Record the failure for Screen 3 and escalate the task before
                # crashing the batch — otherwise the run leaves stale "pending"
                # progress and the task stuck in SUBMITTED. Escalation mirrors
                # the QA / Architect REVIEW_FAIL exhaustion paths
                # (mark_disputed → escalate_dispute → ESCALATED).
                write_batch_summary(
                    state_dir=str(self._config.state_dir),
                    task_id=task.task_id,
                    status="ESCALATED",
                    attempt_count=task.review_attempts,
                )
                task = self._task_manager.mark_disputed(task)
                task = self._task_manager.escalate_dispute(task)
                raise RuntimeError(
                    f"Task {task.task_id}: submission contains no "
                    f"file changes — either the Coder edit did not "
                    f"apply (old_text mismatch) or the edit applied "
                    f"but tests failed and were rolled back. Check "
                    f"the test_results field for details."
                )

            # Architect code review
            review_result = self._architect.review_diff(
                skill_name="layer2a-quick-review",
                diff_context=self._task_manager.build_diff_review_context(
                    task,
                    diff_summary=diff_summary,
                    repo_root=str(self._config.repo_path),
                ),
            )
            review_verdict = review_result.get("verdict", "REJECT")

            if review_verdict.upper() == "DISPUTE":
                self._handle_dispute(task, review_result)
                return

            task = self._task_manager.record_review_verdict(
                task,
                verdict=review_verdict,
                feedback=review_result.get("feedback", ""),
            )

            if task.state.value == "REVIEW_FAIL":
                task = dataclasses.replace(
                    task, review_attempts=task.review_attempts + 1
                )
                # Phase 2 retry loop — capture WHY the Architect rejected this
                # diff so the Coder's next attempt is not blind (fixes the
                # lost-feedback defect). Persisted onto the task and injected by
                # build_implementation_context.
                retry_md = build_retry_context(
                    task_id=task.task_id,
                    gate_name="Architect diff review",
                    gate_feedback=review_result.get("feedback", ""),
                    test_failures=None,
                    attempt_number=task.review_attempts + 1,
                    base_dir=self._config.state_dir,
                )
                task = dataclasses.replace(task, retry_context=retry_md)
                self._task_manager._persist(task)
                if task.review_attempts >= 3:
                    # Step 4 — escalate non-blockingly (mark_disputed →
                    # escalate_dispute → ESCALATED), the same pattern the QA
                    # path uses, instead of crashing the batch with RuntimeError.
                    log.info(
                        "Task %s: review failed — escalating after %d attempt(s)",
                        task.task_id, task.review_attempts,
                    )
                    task = self._task_manager.mark_disputed(task)
                    task = self._task_manager.escalate_dispute(task)
                    self._record_batch_progress(task, "ESCALATED")
                    raise HumanGateRequired(
                        gate_type="review_failure",
                        payload={
                            "task_id": task.task_id,
                            "feedback": review_result.get("feedback", ""),
                            "review_attempts": task.review_attempts,
                            "message": (
                                f"Architect review failed for task {task.task_id} "
                                f"after {task.review_attempts} attempts. "
                                "Human review required."
                            ),
                        },
                    )
                log.info(
                    "Task %s: review failed — sending back "
                    "for revision (attempt %d/3)",
                    task.task_id,
                    task.review_attempts,
                )
                self._record_batch_progress(task, "IN_PROGRESS")
                # Re-run from IN_PROGRESS with feedback
                self._run_task(task)
                return

        if task.state == TaskState.REVIEW_PASS:
            # cycle 59 — Phase 3 backwards-compatibility gate (Layer 7c). Runs
            # only for is_phase3 tasks, between Architect REVIEW_PASS and QA. A
            # FAIL routes the task back to the Coder (QA_FAIL → IN_PROGRESS) with
            # structured feedback, mirroring the REVIEW_FAIL send-back; any
            # checker error degrades to compatible so it never blocks a task.
            if task.is_phase3:
                log.info(
                    "Task %s: Phase 3 — running backwards compatibility check "
                    "before QA",
                    task.task_id,
                )
                compat_result = self._run_compat_check(task, diff_summary)
                if not compat_result.get("compatible", True):
                    task = self._task_manager.record_compat_failure(task, compat_result)
                    task = dataclasses.replace(
                        task, review_attempts=task.review_attempts + 1
                    )
                    # Phase 2 retry loop — capture the backwards-compat rejection
                    # reason for the Coder's next attempt.
                    retry_md = build_retry_context(
                        task_id=task.task_id,
                        gate_name="Backwards-compatibility",
                        gate_feedback=compat_result.get("summary", ""),
                        test_failures=None,
                        attempt_number=task.review_attempts + 1,
                        base_dir=self._config.state_dir,
                    )
                    task = dataclasses.replace(task, retry_context=retry_md)
                    self._task_manager._persist(task)
                    # Share the QA gate's retry cap exactly (>=3): the compat and
                    # QA gates increment the same review_attempts counter, so an
                    # off-by-one (>3) here would grant compat one extra retry.
                    if task.review_attempts >= 3:
                        # Step 4 — escalate non-blockingly instead of crashing
                        # (mirrors the Architect and QA exhaustion paths).
                        log.warning(
                            "Task %s: compat check failed — escalating after "
                            "%d attempt(s)",
                            task.task_id, task.review_attempts,
                        )
                        task = self._task_manager.mark_disputed(task)
                        task = self._task_manager.escalate_dispute(task)
                        self._record_batch_progress(task, "ESCALATED")
                        raise HumanGateRequired(
                            gate_type="compat_failure",
                            payload={
                                "task_id": task.task_id,
                                "feedback": compat_result.get("summary", ""),
                                "issues": compat_result.get("issues", []),
                                "review_attempts": task.review_attempts,
                                "message": (
                                    f"Backwards-compatibility check failed for task "
                                    f"{task.task_id} after {task.review_attempts} "
                                    "attempts. Human review required."
                                ),
                            },
                        )
                    log.warning(
                        "Task %s: compat check failed — %s",
                        task.task_id,
                        compat_result.get("summary", ""),
                    )
                    self._record_batch_progress(task, "IN_PROGRESS")
                    self._run_task(task)
                    return

            # QA verification — dispatch routing from task.verification_required.
            #
            # Empty list short-circuits to a human gate (registry_resolution)
            # BEFORE any QA context is built or the QA agent is invoked. This
            # is the documented escape hatch for tasks the planner cannot
            # route to an automated verifier (e.g. FIX-BLOCKING-* registry
            # blockers). The message text branches on plan_id to distinguish
            # the expected case from a planner-emission anomaly.
            #
            # Multi-skill lists execute every skill and aggregate verdicts
            # conjunctively (all-must-pass). Per-skill verdicts are recorded
            # in the QA audit row and the qa_failure HumanGate payload.
            if not task.verification_required:
                if task.plan_id.startswith("FIX-BLOCKING-"):
                    message = (
                        f"Task {task.task_id} has no automated verification "
                        f"path (FIX-BLOCKING-* registry blocker). Human "
                        f"reviewer must verify resolution manually before "
                        f"advancing. Approve via: sentinel task "
                        f"--registry-approve {task.task_id}"
                    )
                else:
                    message = (
                        f"Task {task.task_id} has empty verification_required "
                        f"but is not a FIX-BLOCKING-* registry blocker. "
                        f"Human verification required. This may indicate a "
                        f"planner emission anomaly or hand-edited plan.json "
                        f"— verify the task's source before approving. "
                        f"Approve via: sentinel task --registry-approve "
                        f"{task.task_id}"
                    )
                raise HumanGateRequired(
                    gate_type="registry_resolution",
                    payload={
                        "task_id": task.task_id,
                        "plan_id": task.plan_id,
                        "verification_required": list(task.verification_required),
                        "message": message,
                    },
                )

            original_findings = [
                f for f in self._store.get_all_findings()
                if _module_in_affected(f.get("module", ""), task.affected_files)
            ]
            # On a state resume (e.g. SUBMITTED), implement() is skipped so
            # impl_result is empty. Fall back to the task's persisted fields and
            # the real working-tree diff so QA verifies against actual evidence
            # rather than an empty context. Mirrors the Architect-context fix.
            diff_for_qa = diff_summary or _get_git_diff(
                repo_root=str(self._config.repo_path),
                files=task.files_changed or [],
            )
            # Append any new test files the Coder wrote
            # that are not in affected_files — same pattern
            # as the Architect review context (6f32aac).
            from pathlib import Path as _Path
            _test_files_shown: set[str] = set()
            _unreadable_changed_files: list[str] = []
            for _f in (task.files_changed or []):
                if _f in _test_files_shown:
                    continue
                if _f in (task.affected_files or []):
                    continue
                _full = _Path(str(self._config.repo_path)) / _f
                if _full.exists():
                    try:
                        _tc = _full.read_text(
                            encoding="utf-8", errors="replace"
                        )
                        diff_for_qa += (
                            f"\n## NEW FILE: {_f}\n"
                            f"```python\n"
                            f"{_tc[:8000]}\n"
                            f"```\n"
                        )
                        _test_files_shown.add(_f)
                    except OSError as exc:
                        # A changed file that cannot be read leaves the QA
                        # context partial — record it explicitly rather than
                        # silently dropping it (FIX-REQUIRED-010).
                        _unreadable_changed_files.append(_f)
                        log.warning(
                            "QA context for task %s: could not read changed "
                            "file %s (%s: %s) — verification context is "
                            "incomplete",
                            task.task_id, _f, type(exc).__name__, exc,
                        )
            if _unreadable_changed_files:
                # Surface an explicit incomplete-context marker so verification
                # does not proceed as if the context were complete.
                diff_for_qa += (
                    "\n## INCOMPLETE QA CONTEXT\n"
                    "The following changed files could not be read and are "
                    "missing from this verification context:\n"
                    + "\n".join(f"- {p}" for p in _unreadable_changed_files)
                    + "\nTreat verification as operating on partial context.\n"
                )
            test_results_for_qa = (
                impl_result.get("test_results")
                or getattr(task, "test_results", "")
                or ""
            )
            tests_pass_for_qa = (
                impl_result.get("tests_pass")
                if impl_result.get("tests_pass") is not None
                else getattr(task, "tests_pass", None)
            )
            test_summary_for_qa = (
                impl_result.get("test_summary")
                or getattr(task, "test_summary", "")
                or ""
            )
            qa_context = build_verification_context(
                task=task.to_dict(),
                diff=diff_for_qa,
                original_findings=original_findings,
                test_results=test_results_for_qa,
                tests_pass=tests_pass_for_qa,
                test_summary=test_summary_for_qa,
            )

            # Multi-skill conjunctive aggregation: execute every skill,
            # collect per-skill verdicts, aggregate to PASS only if all
            # skills return PASS. No short-circuit: full per-skill list
            # is always populated for audit completeness.
            per_skill_results: list[dict] = []
            per_skill_verdicts: list[dict] = []
            for skill in task.verification_required:
                skill_result = self._qa.verify_fix(
                    qa_context,
                    skill_name=skill,
                )
                skill_verdict = str(
                    skill_result.get("verdict", "FAIL")
                ).upper()
                per_skill_results.append(skill_result)
                per_skill_verdicts.append(
                    {"skill": skill, "verdict": skill_verdict}
                )

            aggregated_verdict = (
                "PASS"
                if all(v["verdict"] == "PASS" for v in per_skill_verdicts)
                else "FAIL"
            )

            # cycle 52 — SCOPE_LIMIT enforcement: edits outside the owner-approved
            # scope fail QA even if the verification skills passed.
            # cycle 55 — source changed files reliably (not by parsing prose):
            # the coder's structured files_changed list, then the git working
            # tree, then (last resort) the prose diff summary.
            if task.conditions_structured:
                from sentinel.govern.conditions import check_scope_limit
                scope_violations = check_scope_limit(
                    task.conditions_structured,
                    self._scope_changed_files(impl_result, diff_summary),
                )
                if scope_violations and aggregated_verdict == "PASS":
                    aggregated_verdict = "FAIL"
                    per_skill_verdicts.append({"skill": "scope-limit", "verdict": "FAIL"})
                    log.warning(
                        "Task %s failed SCOPE_LIMIT gate: %s",
                        task.task_id, "; ".join(scope_violations[:3]),
                    )

            task = self._task_manager.record_qa_verdict(
                task,
                verdict=aggregated_verdict,
                tdd_assessment=(
                    per_skill_results[0].get("tdd_assessment")
                    if per_skill_results
                    else None
                ),
                per_skill_verdicts=per_skill_verdicts,
                feedback=(
                    self._qa_failure_feedback(per_skill_results, per_skill_verdicts)
                    if aggregated_verdict == "FAIL" else ""
                ),
            )

            if task.state.value == "QA_FAIL":
                # Mirror the Architect REVIEW_FAIL send-back: a QA failure routes
                # the task back to the Coder with the QA feedback for revision
                # (bounded by the shared review_attempts cap — the same counter
                # the Phase-3 compat path uses), instead of escalating on the
                # first failure. Escalate to a human only once revisions are
                # exhausted. This dampens QA stochasticity and lets a task fix a
                # genuine QA concern, matching how Architect failures are handled.
                qa_feedback = (
                    self._qa_failure_feedback(per_skill_results, per_skill_verdicts)
                )
                task = dataclasses.replace(
                    task,
                    review_attempts=task.review_attempts + 1,
                    last_human_review_comment=(
                        "QA verification failed — you MUST address this before "
                        f"resubmitting:\n{qa_feedback}"
                    ),
                )
                # Phase 2 retry loop — capture the QA rejection reason for the
                # Coder's next attempt.
                retry_md = build_retry_context(
                    task_id=task.task_id,
                    gate_name="QA",
                    gate_feedback=qa_feedback,
                    test_failures=None,
                    attempt_number=task.review_attempts + 1,
                    base_dir=self._config.state_dir,
                )
                task = dataclasses.replace(task, retry_context=retry_md)
                self._task_manager._persist(task)
                if task.review_attempts >= 3:
                    log.info(
                        "Task %s: QA failed — escalating after %d revision attempt(s) "
                        "(retry limit 3)",
                        task.task_id, task.review_attempts,
                    )
                    task = self._task_manager.mark_disputed(task)
                    task = self._task_manager.escalate_dispute(task)
                    self._record_batch_progress(task, "ESCALATED")
                    raise HumanGateRequired(
                        gate_type="qa_failure",
                        payload={
                            "task_id": task.task_id,
                            "qa_result": (
                                per_skill_results[0]
                                if per_skill_results
                                else {}
                            ),
                            "per_skill_results": per_skill_results,
                            "per_skill_verdicts": per_skill_verdicts,
                            "message": f"QA failed for task {task.task_id}. Human review required.",
                        },
                    )
                log.info(
                    "Task %s: QA failed — sending back for revision (attempt %d/3)",
                    task.task_id, task.review_attempts,
                )
                self._record_batch_progress(task, "IN_PROGRESS")
                self._run_task(task)
                return

        if task.state == TaskState.QA_PASS:
            # Documentation transition manifest (hard gate at
            # QA_PASS -> DOC_UPDATED). Resolve .md files declared in
            # task.affected_files (case-insensitive); missing files are
            # logged and skipped so the manifest can still proceed on
            # diff context alone.
            existing_docs: dict[str, str] = {}
            for path_str in task.affected_files:
                path = Path(path_str)
                if path.suffix.lower() != ".md":
                    continue
                full_path = self._config.repo_path / path
                try:
                    existing_docs[path_str] = full_path.read_text(
                        encoding="utf-8"
                    )
                except FileNotFoundError:
                    log.warning(
                        "transition manifest: doc %s listed in "
                        "task.affected_files did not resolve at %s; "
                        "manifest will use diff context only",
                        path_str,
                        full_path,
                    )

            doc_context = build_transition_manifest_context(
                task=task.to_dict(),
                diff=diff_summary,
                existing_docs=existing_docs,
            )
            doc_result = self._docs.generate_transition_manifest(doc_context)
            docs_updated = doc_result.get("docs_updated", [])

            # Persist the transition manifest (append-only). Persist on
            # all paths -- doc_updated=False manifests still record the
            # gate failure bibliographically.
            manifests = self._store.read("transition_manifests")
            manifests.append(doc_result)
            self._store.write(
                "transition_manifests",
                manifests,
                commit=True,
                commit_message=(
                    f"sentinel: transition manifest for {task.task_id}"
                ),
            )

            # FIX 4 — the documentation gate must reflect real on-disk changes,
            # not just a model attestation. When the task has .md files in scope,
            # at least one path in docs_updated must actually exist on disk;
            # otherwise the gate failed. Code-only tasks (no .md in
            # affected_files) are exempt so docs_updated=[] does not falsely fail.
            md_in_scope = any(
                Path(p).suffix.lower() == ".md" for p in task.affected_files
            )
            docs_present = any(
                (self._config.repo_path / p).exists()
                for p in (docs_updated or [])
            )
            if md_in_scope and not docs_present:
                log.warning(
                    "Task %s: documentation gate failed — no documentation "
                    "files were actually updated",
                    task.task_id,
                )
                # Send back for another documentation attempt (task stays at
                # QA_PASS so the doc step re-runs), bounded by the shared
                # review_attempts cap — mirrors the QA_FAIL retry structure. The
                # transition map has no QA_PASS → IN_PROGRESS/ESCALATED edge, so
                # exhaustion raises a human gate directly (as the registry /
                # local-review gates do) rather than forcing an invalid
                # transition.
                task = dataclasses.replace(
                    task, review_attempts=task.review_attempts + 1
                )
                retry_md = build_retry_context(
                    task_id=task.task_id,
                    gate_name="documentation",
                    gate_feedback=(
                        "Documentation gate failed: no documentation files were "
                        "actually updated on disk. Write the .md file(s) in scope "
                        "and list them in docs_updated."
                    ),
                    test_failures=None,
                    attempt_number=task.review_attempts + 1,
                    base_dir=self._config.state_dir,
                )
                task = dataclasses.replace(task, retry_context=retry_md)
                self._task_manager._persist(task)
                if task.review_attempts >= 3:
                    log.warning(
                        "Task %s: documentation gate failed — escalating after "
                        "%d attempt(s)",
                        task.task_id, task.review_attempts,
                    )
                    self._record_batch_progress(task, "ESCALATED")
                    raise HumanGateRequired(
                        gate_type="documentation_failure",
                        payload={
                            "task_id": task.task_id,
                            "review_attempts": task.review_attempts,
                            "message": (
                                f"Documentation gate failed for task "
                                f"{task.task_id} after {task.review_attempts} "
                                "attempts — no documentation files were actually "
                                "updated. Human review required."
                            ),
                        },
                    )
                log.info(
                    "Task %s: documentation gate failed — retrying doc step "
                    "(attempt %d/3)",
                    task.task_id, task.review_attempts,
                )
                self._record_batch_progress(task, "QA_PASS")
                self._run_task(task)
                return

            task = self._task_manager.mark_documentation_updated(
                task,
                docs_updated=docs_updated,
            )

        if task.state == TaskState.DOC_UPDATED:
            # Semantic verification gate (SENTINEL_VERIFICATION_GATE): after QA,
            # re-review the changed files to confirm the original finding is
            # resolved. On failure the task is reopened to IN_PROGRESS and
            # re-run; verified/skip falls through to the local review gate.
            reopened = self._run_verification_gate(task, diff_summary)
            if reopened is not None:
                self._run_task(reopened)
                return

            # Local review gate (applies to both self-approve and GitHub PR modes)
            raise HumanGateRequired(
                gate_type="local_review",
                payload={
                    "task_id": task.task_id,
                    "message": (
                        f"Task {task.task_id} is ready for local review.\n"
                        f"Description: {task.description}\n"
                        f"Show diff: sentinel task --show-diff {task.task_id}\n"
                        f"Request changes: sentinel task --request-changes {task.task_id} --comment \"required reason\"\n"
                        +
                        (
                            f"Approve + commit + push directly: sentinel task --approve-local {task.task_id} --comment \"optional note\""
                            if self._config.self_approve_mode
                            else f"Approve local review + submit GitHub PR: sentinel task --approve-local {task.task_id} --comment \"optional note\""
                        )
                    ),
                },
            )

    def _capture_pre_fix_sha(self, task: Task) -> Task:
        """Record the repo HEAD before the Coder edits (idempotent).

        Captured once when the Coder enters IN_PROGRESS so the semantic
        verification gate can run a ``--changed-since`` re-review after QA.
        """
        if task.pre_fix_sha:
            return task
        sha = self._git_head_sha(self._config.repo_path)
        task = dataclasses.replace(task, pre_fix_sha=sha)
        self._task_manager._persist(task)
        return task

    def _resolve_task_severity(self, task: Task) -> str | None:
        """Resolve a task's canonical severity via ``pattern_id`` → group →
        finding_status. Returns Critical/P1/P2/P3 or None when unresolvable."""
        pid = getattr(task, "pattern_id", "") or ""
        if not pid:
            return None
        group_id = self._build_pattern_to_group().get(pid)
        if not group_id:
            return None
        try:
            from sentinel.assess.finding_status import StatusManager

            base = self._config.state_dir
            mgr = StatusManager(
                base, base / "semantic_groups.json", base / "tech_debt.json"
            )
            record = mgr.get(group_id)
            if record and record.severity:
                return _canonical_severity(str(record.severity)) or str(record.severity)
        except Exception as exc:  # pragma: no cover - defensive
            log.debug("severity resolution failed for task %s: %s", task.task_id, exc)
        return None

    def _read_module_source(self, module: dict[str, Any]) -> str:
        """Concatenate the on-disk source of a module's files for review."""
        parts: list[str] = []
        for rel in module.get("files", []):
            path = Path(self._config.repo_path) / rel
            try:
                parts.append(
                    f"# === {rel} ===\n"
                    + path.read_text(encoding="utf-8", errors="replace")
                )
            except OSError:
                continue
        return "\n\n".join(parts)

    def _run_semantic_verification(
        self,
        task: Task,
        pre_fix_sha: str,
    ) -> tuple[bool, str]:
        """Timeout-bounded targeted re-review of a task's affected files.

        Wraps :meth:`_do_semantic_verification` with a wall-clock cap
        (``SENTINEL_VERIFICATION_TIMEOUT_SECONDS``, default 60s). If the work
        exceeds the timeout — or raises — verification **fails closed**
        (``verified=False``): a hung or erroring verification gate must never
        silently pass, otherwise the security control is bypassed. The caller
        decides whether to escalate.

        Returns ``(verified, reason)``.
        """
        affected = [
            f for f in (task.affected_files or [])
            if str(f).lower().endswith((".py", ".cs"))
        ]
        if not affected:
            return True, "no source files in scope to verify"

        import concurrent.futures

        timeout = self._config.verification_timeout_seconds
        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(self._do_semantic_verification, task)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            log.warning(
                "Task %s: semantic verification timed out after %ss "
                "(fail-closed — gate not cleared)",
                task.task_id, timeout,
            )
            return False, "verification timed out — gate not cleared"
        except Exception as exc:
            log.warning(
                "Semantic verification errored (fail-closed): %s", exc
            )
            return False, f"verification error: {exc}"
        finally:
            # Do not block on a timed-out worker; the lingering thread finishes
            # on its own (Python cannot force-kill it).
            executor.shutdown(wait=False)

    def _do_semantic_verification(self, task: Task) -> tuple[bool, str]:
        """Restricted cartographer + Architect reviewer over a task's affected
        source files (Stages 1 & 3 only), in a temporary state dir. Returns
        ``(verified, reason)``; ``verified=False`` when a Critical/P1 finding
        persists. May raise — the caller wraps this with a timeout/fail-open."""
        affected = [
            f for f in (task.affected_files or [])
            if str(f).lower().endswith((".py", ".cs"))
        ]
        if not affected:
            return True, "no source files in scope to verify"

        import tempfile

        from sentinel.orchestrator.state_store import StateStore

        with tempfile.TemporaryDirectory(prefix="sentinel-verify-") as tmp:
            verify_store = StateStore(Path(tmp))
            cartographer = Cartographer(self._architect, verify_store, self._config)
            manifest = cartographer.run(
                [Path(self._config.repo_path)], restricted_files=affected
            )
            modules = manifest.get("modules", [])
            if not modules:
                return True, "no modules resolved for affected files"
            scan_context = ScanContext.from_repo_root(Path(self._config.repo_path))
            reviewer = ReviewOrchestrator(
                self._architect, verify_store, self._config,
                scan_context=scan_context,
            )
            for module in modules:
                source_code = self._read_module_source(module)
                findings = reviewer.review_module(module, source_code, manifest)
                for finding in findings:
                    severity = _canonical_severity(str(finding.get("severity", "")))
                    if severity in ("Critical", "P1"):
                        detail = (
                            finding.get("summary")
                            or finding.get("description")
                            or ""
                        )
                        return False, (
                            f"{severity} finding persists in "
                            f"{module.get('name')}: {detail[:80]}"
                        )
        return True, "no Critical/P1 findings remain in affected files"

    def _run_verification_gate(self, task: Task, diff_summary: str) -> Task | None:
        """Semantic verification gate, fired at DOC_UPDATED (after QA passes).

        Risk-stratified: only Critical/P1 tasks are verified. Returns the
        reopened task (state IN_PROGRESS) when the finding persists so the caller
        re-runs it; returns ``None`` to proceed to local review (verified, gate
        disabled, or non-blocking severity). Escalates via ``HumanGateRequired``
        once the shared retry cap is exceeded.
        """
        if not self._config.verification_gate:
            return None
        severity = self._resolve_task_severity(task)
        if severity not in ("Critical", "P1"):
            log.info(
                "Task %s: semantic verification skipped (severity=%s)",
                task.task_id, severity,
            )
            return None

        verified, reason = self._run_semantic_verification(task, task.pre_fix_sha)
        if verified:
            log.info("Task %s: semantic verification PASSED — %s", task.task_id, reason)
            return None

        task = dataclasses.replace(task, review_attempts=task.review_attempts + 1)
        retry_md = build_retry_context(
            task_id=task.task_id,
            gate_name="semantic_verification",
            gate_feedback=reason,
            test_failures=None,
            attempt_number=task.review_attempts + 1,
            base_dir=self._config.state_dir,
        )
        task = dataclasses.replace(task, retry_context=retry_md)

        if task.review_attempts >= 3:
            self._task_manager._persist(task)
            log.warning(
                "Task %s: semantic verification failed — escalating after "
                "%d attempt(s)",
                task.task_id, task.review_attempts,
            )
            self._record_batch_progress(task, "ESCALATED")
            raise HumanGateRequired(
                gate_type="semantic_verification_failure",
                payload={
                    "task_id": task.task_id,
                    "review_attempts": task.review_attempts,
                    "reason": reason,
                    "message": (
                        f"Semantic verification failed for task {task.task_id} "
                        f"after {task.review_attempts} attempts: the original "
                        f"finding persists ({reason}). Human review required."
                    ),
                },
            )

        log.info(
            "Task %s: semantic verification FAILED — reopening (attempt %d/3): %s",
            task.task_id, task.review_attempts, reason,
        )
        task = self._task_manager.reopen_after_local_review(
            task, reason=f"Semantic verification failed: {reason}"
        )
        self._record_batch_progress(task, "IN_PROGRESS")
        return task

    def record_pr_approved(self, task_id: str, comment: str = "") -> None:
        """Record human approval/merge and advance task to COMPLETE.

        The PR review row, the ``PR_CREATED→HUMAN_MERGED→COMPLETE`` transitions,
        and their audit trail commit in ONE atomic transaction (site 4) so a
        crash mid-sequence can never leave an orphan review or a half-merged
        task. Batch-progress (advisory Screen-3 data) stays a separate
        best-effort write by design.
        """
        task = self._task_manager.load_task(task_id)
        review = self._task_manager.build_pr_review_record(task, "APPROVED", comment)
        review_audit = self._task_manager._audit_record(
            task.task_id, "human", "pr_review_decision", review
        )
        task = self._task_manager.atomic_task_write(
            task,
            [(TaskState.HUMAN_MERGED, "human"), (TaskState.COMPLETE, "orchestrator")],
            extra_operations=[
                # Transform read inside the lock so concurrent approvals cannot
                # clobber each other (FIX-CRITICAL run-020).
                ("pr_reviews", lambda existing: (existing if isinstance(existing, list) else []) + [review]),
            ],
            extra_audit_events=[review_audit],
            commit=True,
            commit_message=f"sentinel: task {task_id} complete",
        )
        self._record_batch_progress(task, "COMPLETE")
        log.info("Task %s: COMPLETE (PR approved/merged)", task_id)

    def record_pr_rejected(self, task_id: str, reason: str) -> None:
        """Record human PR rejection and reopen task for implementation.

        The PR review row, the ``PR_CREATED→IN_PROGRESS`` reopen, and their audit
        trail commit atomically (site 5). Batch-progress stays a separate
        best-effort write.
        """
        task = self._task_manager.load_task(task_id)
        review = self._task_manager.build_pr_review_record(task, "REJECTED", reason)
        review_audit = self._task_manager._audit_record(
            task.task_id, "human", "pr_review_decision", review
        )
        task.last_human_review_comment = reason
        task = self._task_manager.atomic_task_write(
            task,
            [(TaskState.IN_PROGRESS, "human")],
            reason="Human rejected PR",
            extra_operations=[
                # Transform read inside the lock so concurrent rejections cannot
                # clobber each other (FIX-CRITICAL run-020).
                ("pr_reviews", lambda existing: (existing if isinstance(existing, list) else []) + [review]),
            ],
            extra_audit_events=[review_audit],
            commit=True,
            commit_message=f"sentinel: task {task_id} rejected at PR review",
        )
        # Record batch progress for the reopened task (advisory, best-effort),
        # consistent with the other reopen transitions (FIX-REQUIRED-023).
        self._record_batch_progress(task, "IN_PROGRESS")
        log.info("Task %s: reopened after PR rejection", task_id)

    def record_pr_merged(self, task_id: str) -> None:
        """Backward-compatible alias for record_pr_approved()."""
        self.record_pr_approved(task_id, comment="Recorded via --merged legacy command")

    def record_pr_reverted(self, task: Task, revert_pr_number: int) -> None:
        """Record that a merged/completed task's PR was reverted on the remote (S3-6).

        Transitions the task ``COMPLETE``/``HUMAN_MERGED`` → ``REVERTED`` through the
        same atomic persist path used by ``record_pr_approved``/``record_pr_rejected``
        (snapshot/restore on failure is handled inside ``atomic_task_write``), appends
        a ``pr_reverted`` audit event carrying ``revert_pr_number``, reopens the linked
        finding group back to ``APPROVED`` (the inverse of the REMEDIATED marking done
        on merge), and flips the linked bundle's ``bundle_status`` to
        ``BundleStatus.REVERTED`` in ``bundles.json``.

        Finding and bundle updates are best-effort: a task with no single-pattern
        lineage, an unknown group, or no matching bundle simply skips that side effect.
        """
        # Reload under the manager so the revision guard sees the freshest snapshot.
        task = self._task_manager.load_task(task.task_id)
        revert_audit = self._task_manager._audit_record(
            task.task_id,
            "human",
            "pr_reverted",
            {"revert_pr_number": revert_pr_number},
        )
        task = self._task_manager.atomic_task_write(
            task,
            [(TaskState.REVERTED, "human")],
            reason=f"PR reverted by revert PR #{revert_pr_number}",
            extra_audit_events=[revert_audit],
            commit=True,
            commit_message=f"sentinel: task {task.task_id} reverted (revert PR #{revert_pr_number})",
        )
        # Reopen the linked finding group (inverse of _mark_task_group_remediated).
        self._reopen_task_group(task)
        # Flip the linked bundle's status to REVERTED (best-effort).
        self._mark_bundle_reverted(task)
        log.info(
            "Task %s: REVERTED (detected revert PR #%s)",
            task.task_id, revert_pr_number,
        )

    def _reopen_task_group(self, task: Task) -> None:
        """Set the reverted task's finding group back to APPROVED (best-effort).

        Mirrors ``_mark_task_group_remediated`` in reverse: resolves the group via
        ``task.pattern_id`` → pattern ``group_id`` and reopens it for remediation.
        No-op when the task has no single-pattern lineage or the group is unknown.
        """
        from sentinel.assess.finding_status import StatusManager, StatusValue

        pattern_to_group = self._build_pattern_to_group()
        group_id = pattern_to_group.get(task.pattern_id or "")
        if not group_id:
            return
        base = self._config.state_dir
        mgr = StatusManager(
            base,
            base / "semantic_groups.json",
            base / "tech_debt.json",
        )
        if mgr.get(group_id) is None:
            return
        mgr.update(
            group_id,
            StatusValue.APPROVED,
            updated_by="sentinel-sync-prs",
            task_id=task.task_id,
        )

    def _mark_bundle_reverted(self, task: Task) -> None:
        """Set the reverted task's bundle ``bundle_status`` to REVERTED (best-effort).

        Matches by ``task.bundle_id`` in ``bundles.json``. No-op when the task has no
        bundle lineage or the bundle is missing.
        """
        from sentinel.assess.finding_status import BundleStatus

        bundle_id = getattr(task, "bundle_id", None)
        if not bundle_id:
            return
        bundles = self._store.read("bundles") or []
        changed = False
        for bundle in bundles:
            if bundle.get("bundle_id") == bundle_id:
                bundle["bundle_status"] = BundleStatus.REVERTED
                changed = True
        if changed:
            self._store.write("bundles", bundles)

    def record_registry_approved(self, task_id: str, comment: str = "") -> None:
        """Record human registry-resolution approval and advance task to QA_PASS.

        Resolves the ``registry_resolution`` human gate raised by the QA
        dispatch site when ``task.verification_required`` is empty
        (e.g. FIX-BLOCKING-* registry blockers with no automated
        verification path). The human reviewer's PASS verdict is recorded
        via ``record_qa_verdict`` with ``actor='human_registry_reviewer'``
        so the audit trail distinguishes this verdict from the
        automated QA agent's verdicts.
        """
        task = self._task_manager.load_task(task_id)
        if task.state != TaskState.REVIEW_PASS:
            raise RuntimeError(
                f"Task {task_id} is in state {task.state.value}; "
                f"expected REVIEW_PASS for registry-resolution approval"
            )
        task = self._task_manager.record_qa_verdict(
            task,
            verdict="PASS",
            tdd_assessment={
                "registry_resolution": True,
                "reviewer_comment": comment,
            },
            actor="human_registry_reviewer",
        )
        # Assign the returned task rather than assuming in-place mutation, so the
        # QA_PASS advance is captured even if the task manager returns a new
        # object (FIX-REQUIRED-020).
        if task.state != TaskState.QA_PASS:
            raise RuntimeError(
                f"Task {task_id} did not advance to QA_PASS after registry "
                f"approval (state={task.state.value})"
            )
        self._store.commit_state(
            f"sentinel: task {task_id} registry-approved (human verdict)"
        )
        log.info(
            "Task %s: QA_PASS recorded via human registry-resolution approval",
            task_id,
        )

    def request_local_changes(self, task_id: str, reason: str) -> None:
        """Record local review feedback and reopen task for another remediation pass."""
        task = self._task_manager.load_task(task_id)
        if task.state not in {TaskState.DOC_UPDATED, TaskState.PR_CREATED}:
            raise RuntimeError(
                f"Task {task_id} is in state {task.state.value}; expected DOC_UPDATED or PR_CREATED for local review"
            )
        self._task_manager.record_pr_review(
            task,
            decision="CHANGES_REQUESTED_LOCAL",
            comment=reason,
        )
        task = self._task_manager.reopen_after_local_review(task, reason=reason)
        self._store.commit_state(f"sentinel: task {task_id} local changes requested")
        log.info("Task %s: reopened after local review changes requested", task_id)

    def approve_local_and_push(self, task_id: str, comment: str = "") -> None:
        """Approve local review; either push direct (self mode) or submit a single GitHub PR."""
        task = self._task_manager.load_task(task_id)
        if task.state != TaskState.DOC_UPDATED:
            raise RuntimeError(
                f"Task {task_id} is in state {task.state.value}; expected DOC_UPDATED for local approval"
            )

        # Phase 2B — hard scope gate before any commit reaches the repo.
        # TODO: wire the worktree merge (merge_worktree_to_main) here when the
        # agentic executor is active; until then, pre-check the Coder's
        # changed-file list directly. A violation cancels the task — no copy.
        if task.affected_files and task.files_changed:
            from sentinel.orchestrator.worktree_manager import (
                ScopeViolation,
                enforce_scope,
            )
            try:
                enforce_scope(
                    worktree_path=None,
                    affected_files=task.affected_files,
                    changed_files=task.files_changed,
                )
            except ScopeViolation as exc:
                log.warning(
                    "Task %s blocked by hard scope gate: %s",
                    task_id, exc.violations,
                )
                task.state = TaskState.CANCELLED
                task.last_human_review_comment = (
                    f"Hard scope gate: edited files outside scope: {exc.violations}"
                )
                # Site 8: persist the CANCELLED state and its audit record in one
                # atomic write (was a bare upsert_task with no audit trail).
                cancel_audit = self._task_manager._audit_record(
                    task.task_id,
                    "orchestrator",
                    "task_cancelled_scope_gate",
                    {"violations": list(exc.violations)},
                )
                self._store.persist_task_with_audit(
                    task.to_dict(), [cancel_audit], commit=False
                )
                return

        self._task_manager.record_pr_review(
            task,
            decision="APPROVED_LOCAL",
            comment=comment or "Approved in local self-approve workflow.",
        )
        # No task-scoped committable files means a publication would be a no-op.
        # Refuse it explicitly rather than pushing an empty branch and recording
        # PR-created / merged / COMPLETE success bookkeeping (FIX-REQUIRED-024).
        if not (task.files_changed or []):
            task.state = TaskState.DOC_UPDATED
            task.last_human_review_comment = (
                "Self-approve publication blocked: task has no committable file "
                "changes (files_changed is empty)."
            )
            self._store.upsert_task(task.to_dict())
            raise RuntimeError(
                f"Task {task_id} has no committable file changes; refusing to "
                "publish an empty self-approve. Reopen the task to produce edits."
            )
        if self._config.self_approve_mode:
            files_to_commit = task.files_changed or []
            try:
                if files_to_commit:
                    self._git_commit_scoped(
                        files_to_commit,
                        f"sentinel: task {task_id} complete (local self-approve)",
                    )
                else:
                    log.warning("No files_changed on task %s — skipping commit", task_id)
                self._git_push_current_branch()
            except Exception as exc:
                # Keep failures explicitly non-terminal and recoverable.
                # Terminal transitions are intentionally deferred until after
                # successful commit + push publication.
                task.state = TaskState.DOC_UPDATED
                task.last_human_review_comment = (
                    "Self-approve publish failed before terminal state transition: "
                    f"{exc}"
                )
                self._store.upsert_task(task.to_dict())
                raise

            # Workflow invariant: terminal merged/complete states are persisted
            # only after repository publication succeeds. The create-PR →
            # merged → complete transition chain now commits atomically (site 3)
            # so publication success can never leave a half-transitioned task.
            task.pr_url = f"local-direct://{task.task_id}"
            pr_created_audit = self._task_manager._audit_record(
                task.task_id, "orchestrator", "pr_created", {"pr_url": task.pr_url}
            )
            task = self._task_manager.atomic_task_write(
                task,
                [
                    (TaskState.PR_CREATED, "orchestrator"),
                    (TaskState.HUMAN_MERGED, "human"),
                    (TaskState.COMPLETE, "orchestrator"),
                ],
                extra_audit_events=[pr_created_audit],
                commit=False,
            )
            self._record_batch_progress(task, "COMPLETE")
            log.info("Task %s: COMPLETE (local self-approve committed + pushed)", task_id)

            # Acceptance-criteria completeness gate (layer5b, SHADOW MODE,
            # non-blocking). Runs AFTER the task is already COMPLETE — it logs a
            # verdict only and NEVER blocks or changes task state. The whole
            # block is guarded so a gate failure can never affect the outcome.
            # NOTE: calibrated against Sentinel's own (vibe-coded) failure
            # patterns only — explicitly NOT trusted for Line_tool yet (see
            # docs/Sentinel_Agentic_Architecture.md Section 6 + layer5b skill).
            try:
                from sentinel.orchestrator.task_packet import (
                    generate_acceptance_check_context,
                )
                diff_summary = task.latest_diff_summary or ""
                executor_self_report = task.test_summary or task.test_results or ""
                _ac_context = generate_acceptance_check_context(
                    task, diff_summary, executor_self_report
                )
                _ac_result = self._architect.check_acceptance_criteria(_ac_context)
                log.info(
                    "Acceptance-criteria gate (SHADOW MODE, non-blocking): "
                    "task=%s verdict=%s premise_mismatch=%s",
                    task.plan_id,
                    _ac_result.get("overall_verdict"),
                    _ac_result.get("premise_mismatch_flag"),
                )
                self._store.append_to_list(
                    "acceptance_gate_log",
                    {
                        "task_id": task.task_id,
                        "plan_id": task.plan_id,
                        "verdict": _ac_result.get("overall_verdict"),
                        "criteria_results": _ac_result.get("criteria_results", []),
                        "premise_mismatch_flag": _ac_result.get(
                            "premise_mismatch_flag", False
                        ),
                    },
                )
            except Exception as exc:  # shadow mode: never block a completed task
                log.warning(
                    "Acceptance-criteria gate (shadow) errored for task %s — "
                    "ignored, task remains COMPLETE: %s",
                    task_id, exc,
                )
            return

        # Non-self-approve: submit one GitHub PR only after local approval.
        files_to_commit = task.files_changed or []
        if files_to_commit:
            self._git_commit_scoped(
                files_to_commit,
                f"sentinel: task {task_id} approved locally and ready for PR",
            )
        else:
            log.warning("No files_changed on task %s — skipping commit", task_id)

        # cycle 58 — open a real GitHub PR via gh, falling back to a compare
        # URL when gh is unavailable/unauthenticated.
        from sentinel.workflow.remediation.pr_opener import (
            build_pr_body,
            build_pr_title,
            open_pr,
        )

        # S2-2 — when the task belongs to a bundle, build a root-cause-anchored
        # title + body. Absent bundle_id (or no bundles.json) -> legacy format.
        bundle = self._load_bundle_for_task(task)
        total_bundles = len(self._store.read("bundles") or [])

        branch = f"sentinel/{task.task_id}"
        self._git_push_branch(branch)
        pr_body = build_pr_body(
            task,
            qa_verdict=getattr(task, "qa_verdict", ""),
            doc_summary=getattr(task, "doc_summary", ""),
            bundle=bundle,
            state_dir=str(self._config.state_dir),
            model=getattr(self._config, "model", ""),
            operator=os.environ.get("USER") or os.environ.get("USERNAME") or "",
        )
        title = build_pr_title(task, bundle, total_bundles=total_bundles)
        pr_url = open_pr(
            repo_root=str(self._config.repo_path),
            branch=branch,
            title=title,
            body=pr_body,
            base_branch=self._config.base_branch,
        )
        if pr_url is None:
            pr_url = self._create_pr(task)
            log.info("Fell back to compare URL: %s", pr_url)
        self._task_manager.create_pr(task, pr_url=pr_url)
        self._store.commit_state(f"sentinel: task {task_id} PR submitted after local approval")
        log.info("Task %s: PR submitted after local approval (%s)", task_id, pr_url)

    def get_task_diff(self, task_id: str) -> str:
        """Return the latest task diff summary for local review display."""
        task = self._task_manager.load_task(task_id)
        if task.latest_diff_summary:
            return task.latest_diff_summary
        return "(No diff summary found on task record.)"

    def sync_pr_status(self, task_id: str) -> dict[str, Any]:
        """
        Synchronize one task's PR review state from GitHub.

        Returns a structured status dict with fields:
          - outcome: MERGED | REJECTED | APPROVED | PENDING | NOT_FOUND | ERROR
          - message: human-readable detail
        """
        if self._config.self_approve_mode:
            return {
                "task_id": task_id,
                "outcome": "LOCAL_MODE",
                "message": "Self-approve local mode active: GitHub sync is disabled.",
            }

        task = self._task_manager.load_task(task_id)
        if task.state != TaskState.PR_CREATED:
            return {
                "task_id": task_id,
                "outcome": "ERROR",
                "message": f"Task is in state {task.state.value}; expected PR_CREATED",
            }

        repo_slug = self._github_repo_slug()
        if not repo_slug:
            return {
                "task_id": task_id,
                "outcome": "ERROR",
                "message": "Could not determine GitHub origin remote",
            }

        token = self._config.github_api_token
        if not token:
            return {
                "task_id": task_id,
                "outcome": "ERROR",
                "message": "No GITHUB_API_TOKEN/GITHUB_TOKEN configured for GitHub API",
            }

        api_base = self._config.github_api_base.rstrip("/")
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        owner, repo = repo_slug.split("/", 1)
        pr_number = task.pr_number or self._extract_pr_number(task.pr_url)
        pr_data: dict[str, Any] | None = None

        try:
            with httpx.Client(timeout=20.0) as client:
                if pr_number is None:
                    list_url = f"{api_base}/repos/{owner}/{repo}/pulls"
                    branch_name = f"sentinel/{task.task_id}"
                    list_resp = client.get(
                        list_url,
                        params={
                            "state": "all",
                            "head": f"{owner}:{branch_name}",
                            "per_page": 1,
                        },
                        headers=headers,
                    )
                    if list_resp.status_code == 200:
                        pulls = list_resp.json()
                        if pulls:
                            pr_data = pulls[0]
                            pr_number = pr_data.get("number")
                            task = self._task_manager.update_pr_metadata(
                                task,
                                pr_url=pr_data.get("html_url"),
                                pr_number=pr_number,
                            )
                    else:
                        return {
                            "task_id": task_id,
                            "outcome": "ERROR",
                            "message": f"GitHub API list pulls failed: {list_resp.status_code}",
                        }

                if pr_number is None:
                    return {
                        "task_id": task_id,
                        "outcome": "NOT_FOUND",
                        "message": "No GitHub PR found yet for this task branch",
                    }

                if pr_data is None:
                    pr_url = f"{api_base}/repos/{owner}/{repo}/pulls/{pr_number}"
                    pr_resp = client.get(pr_url, headers=headers)
                    if pr_resp.status_code != 200:
                        return {
                            "task_id": task_id,
                            "outcome": "ERROR",
                            "message": f"GitHub API get pull failed: {pr_resp.status_code}",
                        }
                    pr_data = pr_resp.json()

                task = self._task_manager.update_pr_metadata(
                    task,
                    pr_url=pr_data.get("html_url"),
                    pr_number=pr_number,
                )

                if pr_data.get("merged"):
                    self.record_pr_approved(
                        task_id,
                        comment=f"Auto-detected merged PR #{pr_number}",
                    )
                    return {
                        "task_id": task_id,
                        "outcome": "MERGED",
                        "message": f"PR #{pr_number} is merged",
                    }

                if pr_data.get("state") == "closed":
                    reason = f"Auto-detected closed PR #{pr_number} without merge"
                    self.record_pr_rejected(task_id, reason=reason)
                    return {
                        "task_id": task_id,
                        "outcome": "REJECTED",
                        "message": reason,
                    }

                reviews_url = f"{api_base}/repos/{owner}/{repo}/pulls/{pr_number}/reviews"
                reviews_resp = client.get(reviews_url, params={"per_page": 100}, headers=headers)
                if reviews_resp.status_code != 200:
                    return {
                        "task_id": task_id,
                        "outcome": "PENDING",
                        "message": f"PR #{pr_number} open; unable to read reviews ({reviews_resp.status_code})",
                    }

                reviews = reviews_resp.json()
                # The GitHub reviews endpoint is documented to return a JSON
                # array, but a proxy/error page could yield a dict or other
                # shape. Guard before iterating so a malformed body degrades to
                # PENDING rather than raising mid-sync.
                if not isinstance(reviews, list):
                    log.warning(
                        "PR #%s reviews response was not a list (got %s); "
                        "treating as pending.",
                        pr_number, type(reviews).__name__,
                    )
                    return {
                        "task_id": task_id,
                        "outcome": "PENDING",
                        "message": (
                            f"PR #{pr_number} open; reviews response was not a "
                            "list"
                        ),
                    }
                # Derive the PR outcome from each reviewer's *latest* actionable
                # review, resolved by submission time so the result is
                # independent of the order GitHub returns review objects
                # (FIX-REQUIRED-017). Non-actionable states are excluded.
                latest_by_reviewer: dict[str, dict] = {}
                for r in reviews:
                    if not isinstance(r, dict):
                        continue
                    state = str(r.get("state", "")).upper()
                    if state not in {"APPROVED", "CHANGES_REQUESTED"}:
                        continue
                    reviewer = (r.get("user") or {}).get("login", "reviewer")
                    submitted = str(r.get("submitted_at", ""))
                    existing = latest_by_reviewer.get(reviewer)
                    if existing is None or self._ts_ge(
                        submitted, str(existing.get("submitted_at", ""))
                    ):
                        latest_by_reviewer[reviewer] = r

                # Any reviewer whose latest actionable review requests changes
                # blocks approval, regardless of other reviewers' approvals.
                changes_requesters = [
                    reviewer
                    for reviewer, review in latest_by_reviewer.items()
                    if str(review.get("state", "")).upper() == "CHANGES_REQUESTED"
                ]
                if changes_requesters:
                    reviewer = sorted(changes_requesters)[0]
                    body = (latest_by_reviewer[reviewer].get("body") or "").strip()
                    reason = (
                        f"GitHub requested changes by {reviewer}: "
                        f"{body or 'No comment provided.'}"
                    )
                    self.record_pr_rejected(task_id, reason=reason)
                    return {
                        "task_id": task_id,
                        "outcome": "REJECTED",
                        "message": reason,
                    }

                approvers = [
                    reviewer
                    for reviewer, review in latest_by_reviewer.items()
                    if str(review.get("state", "")).upper() == "APPROVED"
                ]
                if approvers:
                    return {
                        "task_id": task_id,
                        "outcome": "APPROVED",
                        "message": (
                            f"PR #{pr_number} approved by "
                            f"{', '.join(sorted(approvers))}; waiting for merge"
                        ),
                    }

                return {
                    "task_id": task_id,
                    "outcome": "PENDING",
                    "message": f"PR #{pr_number} is open and awaiting review",
                }
        except httpx.HTTPError as exc:
            return {
                "task_id": task_id,
                "outcome": "ERROR",
                "message": f"GitHub API request failed: {exc}",
            }

    def sync_all_prs(self) -> dict[str, Any]:
        """Reconcile every open-PR task against GitHub in one pass.

        Iterates tasks in ``PR_CREATED`` (the open-PR state) and calls
        :meth:`sync_pr_status` for each, which advances a merged PR to COMPLETE
        (``record_pr_approved``) or reopens a rejected PR (``record_pr_rejected``).
        When a task's PR is merged, the linked finding group — resolved via
        ``task.pattern_id`` → pattern ``group_id`` — is marked ``REMEDIATED`` in
        ``finding_status.json``.

        A second pass (S3-6) re-checks every ``COMPLETE``/``HUMAN_MERGED`` task for
        a later revert of its merged PR (:meth:`detect_revert`); a detected revert
        moves the task to ``REVERTED``, reopens the finding group to ``APPROVED``,
        and flips the linked bundle to ``BundleStatus.REVERTED``.

        Returns a summary dict: ``merged``, ``rejected``, ``still_open``,
        ``reverted``, ``errors`` counts, ``nothing_to_sync`` (True when no
        PR_CREATED or settled tasks exist), and per-task ``results``. ``errors``
        counts tasks whose sync returned ``ERROR`` — they are *not* rolled into
        ``still_open`` so a failed API call is not mistaken for a genuinely open PR.

        Raises:
            RuntimeError: If PR tasks exist but no GitHub API token is configured.
        """
        task_dicts = self._store.read("tasks") or []
        pr_tasks = [
            Task.from_dict(t)
            for t in task_dicts
            if t.get("state") == TaskState.PR_CREATED.value
        ]
        # S3-6: tasks already merged/completed are re-checked for a later revert.
        settled_states = {TaskState.COMPLETE.value, TaskState.HUMAN_MERGED.value}
        settled_tasks = [
            Task.from_dict(t)
            for t in task_dicts
            if t.get("state") in settled_states
        ]
        if not pr_tasks and not settled_tasks:
            return {
                "nothing_to_sync": True,
                "merged": 0,
                "rejected": 0,
                "still_open": 0,
                "reverted": 0,
                "errors": 0,
                "results": [],
            }

        if not self._config.github_api_token:
            raise RuntimeError(
                "No GITHUB_API_TOKEN/GITHUB_TOKEN configured; cannot sync PR "
                "status from GitHub."
            )

        pattern_to_group = self._build_pattern_to_group()
        merged = rejected = still_open = reverted = errors = 0
        results: list[dict[str, Any]] = []

        for task in pr_tasks:
            result = self.sync_pr_status(task.task_id)
            outcome = result.get("outcome")
            if outcome == "MERGED":
                merged += 1
                self._mark_task_group_remediated(task, pattern_to_group)
                log.info(
                    "Task %s: PR #%s merged — marked COMPLETE",
                    task.task_id, task.pr_number,
                )
            elif outcome == "REJECTED":
                rejected += 1
                log.info(
                    "Task %s: PR #%s rejected — reopened for revision",
                    task.task_id, task.pr_number,
                )
            elif outcome == "ERROR":
                # A failed sync is not an open PR — count it separately so the
                # summary does not mask API failures as still-open work.
                errors += 1
                log.warning(
                    "Task %s: PR sync error — %s",
                    task.task_id, result.get("message", ""),
                )
            else:
                still_open += 1
            results.append(result)

        # S3-6: second pass — check already-settled tasks for a later revert.
        for task in settled_tasks:
            result = self.detect_revert(task)
            outcome = result.get("outcome")
            if outcome == "REVERTED":
                reverted += 1
                log.info(
                    "Task %s: PR #%s reverted — reopened finding",
                    task.task_id, task.pr_number,
                )
            elif outcome == "ERROR":
                errors += 1
                log.warning(
                    "Task %s: revert detection error — %s",
                    task.task_id, result.get("message", ""),
                )
            results.append(result)

        log.info(
            "Sync complete: %d merged, %d rejected, %d still open, %d reverted, %d errors",
            merged, rejected, still_open, reverted, errors,
        )
        return {
            "nothing_to_sync": False,
            "merged": merged,
            "rejected": rejected,
            "still_open": still_open,
            "reverted": reverted,
            "errors": errors,
            "results": results,
        }

    def _build_pattern_to_group(self) -> dict[str, str]:
        """Map ``pattern_id`` → ``group_id`` from remediation_patterns.json."""
        patterns = self._store.read("remediation_patterns") or []
        return {
            p.get("pattern_id"): p.get("group_id")
            for p in patterns
            if p.get("pattern_id") and p.get("group_id")
        }

    def _mark_task_group_remediated(
        self,
        task: Task,
        pattern_to_group: dict[str, str],
    ) -> None:
        """Set the merged task's finding group to REMEDIATED (best-effort).

        Resolves the group via ``task.pattern_id`` → pattern ``group_id``. No-op
        when the task has no single-pattern lineage or the group is unknown.
        """
        from sentinel.assess.finding_status import StatusManager, StatusValue

        group_id = pattern_to_group.get(task.pattern_id or "")
        if not group_id:
            return
        base = self._config.state_dir
        mgr = StatusManager(
            base,
            base / "semantic_groups.json",
            base / "tech_debt.json",
        )
        if mgr.get(group_id) is None:
            return
        mgr.update(
            group_id,
            StatusValue.REMEDIATED,
            updated_by="sentinel-sync-prs",
            task_id=task.task_id,
        )

    def detect_revert(self, task: Task) -> dict[str, Any]:
        """Detect whether a COMPLETE/HUMAN_MERGED task's merged PR was reverted (S3-6).

        Best-effort: fetches the original merged PR (to learn its ``title`` and
        ``merge_commit_sha``), then scans recent all-state PRs for a MERGED revert
        whose title matches ``Revert "<original title>"`` OR whose title/body
        references the original ``merge_commit_sha``. On a match, calls
        ``record_pr_reverted`` and returns ``{"outcome": "REVERTED"}``.

        Every GitHub call is guarded: any network error, non-200, malformed body,
        or missing config degrades to ``{"outcome": "NO_REVERT"}`` (or ``ERROR``)
        so a lookup failure never crashes the enclosing sync.
        """
        task_id = task.task_id
        if self._config.self_approve_mode:
            return {"task_id": task_id, "outcome": "NO_REVERT",
                    "message": "Self-approve local mode: revert detection disabled."}

        repo_slug = self._github_repo_slug()
        token = self._config.github_api_token
        if not repo_slug or not token:
            return {"task_id": task_id, "outcome": "NO_REVERT",
                    "message": "No GitHub origin/token; skipping revert detection."}

        pr_number = task.pr_number or self._extract_pr_number(task.pr_url)
        if pr_number is None:
            return {"task_id": task_id, "outcome": "NO_REVERT",
                    "message": "Task has no PR number; nothing to check for revert."}

        api_base = self._config.github_api_base.rstrip("/")
        owner, repo = repo_slug.split("/", 1)
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        try:
            with httpx.Client(timeout=20.0) as client:
                # 1. Fetch the original PR to learn its title + merge commit sha.
                orig_resp = client.get(
                    f"{api_base}/repos/{owner}/{repo}/pulls/{pr_number}",
                    headers=headers,
                )
                if orig_resp.status_code != 200:
                    return {"task_id": task_id, "outcome": "NO_REVERT",
                            "message": f"Could not read original PR #{pr_number} "
                                       f"({orig_resp.status_code})."}
                orig = orig_resp.json()
                if not isinstance(orig, dict):
                    return {"task_id": task_id, "outcome": "NO_REVERT",
                            "message": "Original PR response was not an object."}
                orig_title = str(orig.get("title") or "").strip()
                merge_sha = str(orig.get("merge_commit_sha") or "").strip()

                # 2. Scan recent all-state PRs for a merged revert.
                list_resp = client.get(
                    f"{api_base}/repos/{owner}/{repo}/pulls",
                    params={"state": "all", "per_page": 100, "sort": "updated",
                            "direction": "desc"},
                    headers=headers,
                )
                if list_resp.status_code != 200:
                    return {"task_id": task_id, "outcome": "NO_REVERT",
                            "message": f"Could not list PRs ({list_resp.status_code})."}
                candidates = list_resp.json()
                if not isinstance(candidates, list):
                    return {"task_id": task_id, "outcome": "NO_REVERT",
                            "message": "PR list response was not a list."}

                revert_pr = self._find_revert_pr(
                    candidates, orig_title, merge_sha, original_pr_number=pr_number
                )
                if revert_pr is None:
                    return {"task_id": task_id, "outcome": "NO_REVERT",
                            "message": f"No merged revert found for PR #{pr_number}."}

                revert_number = revert_pr.get("number")
                self.record_pr_reverted(task, revert_pr_number=int(revert_number))
                return {"task_id": task_id, "outcome": "REVERTED",
                        "message": f"PR #{pr_number} reverted by PR #{revert_number}."}
        except httpx.HTTPError as exc:
            # Network failure must not crash the sync — treat as "no revert found".
            log.warning("Task %s: revert detection failed — %s", task_id, exc)
            return {"task_id": task_id, "outcome": "NO_REVERT",
                    "message": f"Revert detection GitHub request failed: {exc}"}

    @staticmethod
    def _find_revert_pr(
        candidates: list[dict[str, Any]],
        original_title: str,
        merge_sha: str,
        original_pr_number: int | None = None,
    ) -> dict[str, Any] | None:
        """Return the first MERGED revert PR from ``candidates``, or None.

        A candidate matches when it is merged and either:
          * its title matches ``^Revert\\s+"<original_title>"`` (GitHub's default
            revert-PR title form), or
          * its title or body references the original ``merge_commit_sha``.

        The original PR itself is never considered a revert of itself.
        """
        title_pattern = None
        if original_title:
            title_pattern = re.compile(
                r'^\s*Revert\s+"' + re.escape(original_title) + r'"',
                re.IGNORECASE,
            )
        for cand in candidates:
            if not isinstance(cand, dict):
                continue
            if not cand.get("merged"):
                continue
            if original_pr_number is not None and cand.get("number") == original_pr_number:
                continue
            cand_title = str(cand.get("title") or "")
            cand_body = str(cand.get("body") or "")
            if title_pattern is not None and title_pattern.search(cand_title):
                return cand
            if merge_sha and (merge_sha in cand_title or merge_sha in cand_body):
                return cand
        return None

    @staticmethod
    def _extract_pr_number(pr_url: str | None) -> int | None:
        if not pr_url:
            return None
        match = re.search(r"/pull/(\d+)", pr_url)
        if not match:
            return None
        try:
            return int(match.group(1))
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # Dispute protocol
    # ------------------------------------------------------------------

    def _handle_dispute(
        self,
        task: Task,
        triggering_result: dict[str, Any],
    ) -> None:
        """
        Run the structured disagreement protocol.

        One round of negotiation. If unresolved, generate a human decision
        packet and raise HumanGateRequired.
        """
        log.info("Task %s: dispute protocol activated", task.task_id)

        # Architect position
        arch_position = self._architect.build_position_statement(
            dispute_context=(
                f"## Dispute Context — Task {task.task_id}\n\n"
                f"Your review verdict: {triggering_result.get('verdict')}\n"
                f"Findings: {json.dumps(triggering_result.get('findings', []), indent=2)}\n\n"
                "Build your position statement as JSON."
            )
        )

        # Coder position
        coder_position = self._coder.build_position_statement(
            dispute_context=(
                f"## Dispute Context — Task {task.task_id}\n\n"
                f"Architect verdict: {triggering_result.get('verdict')}\n"
                f"Architect feedback: {triggering_result.get('feedback', '')}\n\n"
                "Build your position statement as JSON."
            )
        )

        # One round of negotiation
        arch_response = self._architect.build_position_statement(
            dispute_context=(
                f"## Negotiation Round — Task {task.task_id}\n\n"
                f"Coder's position:\n{json.dumps(coder_position, indent=2)}\n\n"
                "Do you agree with the coder's approach? "
                "If yes, return {\"resolved\": true, \"agreed_approach\": \"...\"}. "
                "If no, explain why and return {\"resolved\": false, \"counter\": \"...\"}."
            )
        )

        dispute_record = {
            "task_id": task.task_id,
            "initiator": "architect",
            "architect_position": arch_position,
            "coder_position": coder_position,
            "negotiation_result": arch_response,
            "escalated_to_human": not arch_response.get("resolved", False),
        }
        # Site 7: the dispute record, its audit, and the task transition commit
        # in ONE atomic write per branch so a crash can never leave a dispute
        # record without the matching task-state change (or vice versa).
        disputes = self._store.read("disputes")
        if not isinstance(disputes, list):
            disputes = []
        disputes.append(dispute_record)

        if arch_response.get("resolved"):
            log.info("Task %s: dispute resolved in negotiation", task.task_id)
            # Cycle guard: bound how many times a task may go through the
            # dispute → resolve → _run_task loop. A task that keeps failing
            # would otherwise re-enter _run_task forever. Once the cap is
            # exceeded, escalate to a human (map-valid DISPUTED → ESCALATED)
            # instead of restarting the Coder.
            task = dataclasses.replace(
                task, dispute_rounds=task.dispute_rounds + 1
            )
            if task.dispute_rounds > _MAX_DISPUTE_ROUNDS:
                log.warning(
                    "Task %s: dispute resolved but exceeded %d dispute round(s) "
                    "— escalating to human instead of re-running",
                    task.task_id, _MAX_DISPUTE_ROUNDS,
                )
                task = self._task_manager.atomic_task_write(
                    task,
                    [
                        (TaskState.DISPUTED, "orchestrator"),
                        (TaskState.ESCALATED, "orchestrator"),
                    ],
                    extra_operations=[("disputes", disputes)],
                    commit=False,
                )
                self._record_batch_progress(task, "ESCALATED")
                raise HumanGateRequired(
                    gate_type="dispute_escalation",
                    payload={
                        "task_id": task.task_id,
                        "architect_position": arch_position,
                        "coder_position": coder_position,
                        "message": (
                            f"Task {task.task_id} exceeded {_MAX_DISPUTE_ROUNDS} "
                            "dispute-resolution rounds without converging. "
                            "Human review required."
                        ),
                    },
                )
            # Incorporate the agreed approach into the next implementation
            # attempt: persist it as retry context, then restart the Coder from
            # IN_PROGRESS (via the map-valid SUBMITTED → DISPUTED → IN_PROGRESS
            # path) instead of dropping the resolution on the floor.
            agreed = (
                arch_response.get("agreed_approach")
                or arch_response.get("counter")
                or "Proceed with the approach agreed during dispute negotiation."
            )
            retry_md = build_retry_context(
                task_id=task.task_id,
                gate_name="dispute_resolution",
                gate_feedback=agreed,
                test_failures=None,
                attempt_number=task.review_attempts + 1,
                base_dir=self._config.state_dir,
            )
            task = dataclasses.replace(task, retry_context=retry_md)
            resolved_audit = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "agent": "orchestrator",
                "action": "dispute_resolved",
                "details": {"task_id": task.task_id},
            }
            task = self._task_manager.atomic_task_write(
                task,
                [
                    (TaskState.DISPUTED, "orchestrator"),
                    (TaskState.IN_PROGRESS, "orchestrator"),
                ],
                extra_operations=[("disputes", disputes)],
                extra_audit_events=[resolved_audit],
                commit=False,
            )
            log.info(
                "Task %s: dispute resolved — restarting implementation with "
                "agreed approach", task.task_id,
            )
            self._run_task(task)
            return

        # Unresolved — escalate to human. The task is SUBMITTED here, so it must
        # pass through DISPUTED before ESCALATED (map-valid path); the dispute
        # record + both transitions commit atomically.
        log.warning("Task %s: dispute unresolved — escalating to human", task.task_id)
        task = self._task_manager.atomic_task_write(
            task,
            [
                (TaskState.DISPUTED, "orchestrator"),
                (TaskState.ESCALATED, "orchestrator"),
            ],
            extra_operations=[("disputes", disputes)],
            commit=False,
        )
        self._record_batch_progress(task, "ESCALATED")

        raise HumanGateRequired(
            gate_type="dispute_escalation",
            payload={
                "task_id": task.task_id,
                "architect_position": arch_position,
                "coder_position": coder_position,
                "message": (
                    f"Unresolved dispute for task {task.task_id}.\n"
                    "Both agent positions are in sentinel/state/disputes.json.\n"
                    "Use: sentinel dispute --resolve <task_id> --winner architect|coder"
                ),
            },
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _extract_plan_items(self, plan: dict[str, Any]) -> list[dict[str, Any]]:
        """Extract a flat list of plan items from the Fix Planner output."""
        items: list[dict[str, Any]] = []
        for priority in ("blocking", "required", "desired", "nice_to_have"):
            batch = plan.get(priority, plan.get(priority.replace("_", "-"), []))
            if isinstance(batch, list):
                items.extend(batch)
        if not items:
            # Fallback: plan might be a flat list
            if isinstance(plan, list):
                items = plan
            else:
                items = plan.get("items", plan.get("tasks", []))
        return items

    def _load_bundle_for_task(self, task: Task) -> dict[str, Any] | None:
        """Return the bundle record for ``task.bundle_id`` (S2-2), or None.

        Reads bundles.json; a task with no ``bundle_id`` (pre-S2-2 plans) or a
        missing bundle yields None so the PR builder falls back to the legacy
        per-task format.
        """
        bundle_id = getattr(task, "bundle_id", None)
        if not bundle_id:
            return None
        for b in self._store.read("bundles") or []:
            if b.get("bundle_id") == bundle_id:
                return b
        return None

    def _create_pr(self, task: Task) -> str | None:
        """
        Create a Git PR for the task.

        Pushes HEAD to ``sentinel/<task_id>`` on origin, then returns a GitHub
        compare URL when the origin remote is GitHub (or a placeholder otherwise).

        Raises:
            RuntimeError: if the branch push fails — a compare URL for a branch
            that was never pushed would point at a non-existent ref, so we must
            surface the failure rather than return a misleading URL.
        """
        branch_name = f"sentinel/{task.task_id}"
        base_branch = self._git_current_branch() or self._config.base_branch

        if not self._git_push_branch(branch_name):
            raise RuntimeError(
                "git push failed — PR URL may be invalid "
                f"(branch '{branch_name}' was not pushed to origin)"
            )

        repo_slug = self._github_repo_slug()
        if repo_slug:
            return f"https://github.com/{repo_slug}/compare/{base_branch}...{branch_name}?expand=1"

        log.info("Using placeholder PR URL for branch: %s", branch_name)
        return f"https://github.com/<org>/<repo>/compare/{base_branch}...{branch_name}"

    def _git_current_branch(self) -> str | None:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=self._config.repo_path,
                check=True,
                capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                timeout=30,
            )
            return result.stdout.strip() or None
        except subprocess.CalledProcessError as exc:
            log.warning("Unable to determine current git branch: %s", exc)
            return None

    def _git_head_sha(self, repo_path) -> str:
        """Best-effort full HEAD SHA of ``repo_path`` (cycle 61).

        Returns "" when the path is not a git repo or git is unavailable —
        the assessed commit is recorded in scan_metadata for later GitHub
        permalink pinning, so a miss simply means the operator must pass
        ``--github-ref`` by hand.
        """
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(repo_path),
                check=True,
                capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                timeout=30,
            )
            return (getattr(result, "stdout", "") or "").strip()
        except (subprocess.CalledProcessError, OSError) as exc:
            log.warning("Unable to capture assessed commit SHA for %s: %s", repo_path, exc)
            return ""

    def _git_push_branch(self, branch_name: str) -> bool:
        """Push HEAD to ``branch_name`` on origin.

        Returns ``True`` on success, ``False`` when the push fails. Callers that
        derive a PR URL from the pushed branch must not treat a failed push as a
        valid PR.
        """
        try:
            subprocess.run(
                ["git", "push", "-u", "origin", f"HEAD:refs/heads/{branch_name}"],
                cwd=self._config.repo_path,
                check=True,
                capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                timeout=120,
            )
            log.info("Pushed task branch to origin: %s", branch_name)
            return True
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip()
            log.warning("Failed to push task branch '%s': %s", branch_name, stderr or exc)
            return False

    def _git_commit_all(self, message: str) -> None:
        try:
            subprocess.run(
                ["git", "add", "-A"],
                cwd=self._config.repo_path,
                check=True,
                capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                timeout=30,
            )
            subprocess.run(
                ["git", "commit", "-m", message],
                cwd=self._config.repo_path,
                check=True,
                capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                timeout=30,
            )
            log.info("Git commit: %s", message)
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip().lower()
            if "nothing to commit" in stderr:
                log.info("No file changes to commit before push")
                return
            raise RuntimeError(f"Failed to commit local approval changes: {exc.stderr or exc}") from exc

    def _run_compat_check(self, task: Task, diff_summary: str) -> dict[str, Any]:
        """Run the Layer 7c BackwardsCompatChecker for a Phase 3 task (cycle 59).

        Normalizes the checker's real API (returns the Layer 7c dict on PASS,
        raises ``CompatibilityViolation`` on FAIL) to ``{compatible, status,
        summary, issues}``. ``status`` is one of ``compatible`` /
        ``incompatible`` / ``checker_error``. A checker *execution* failure is
        reported as ``compatible=False, status="checker_error"`` — an
        indeterminate result that must not be treated as confirmed
        compatibility (FIX-BLOCKING-006).
        """
        from sentinel.phase3.compat_checker import (
            CompatChecker,
            CompatibilityViolation,
        )

        checker = CompatChecker(self._qa, self._store)
        try:
            checker.check(task, diff_summary)
            return {
                "compatible": True,
                "status": "compatible",
                "summary": "Backwards compatible",
                "issues": [],
            }
        except CompatibilityViolation as exc:
            issues = [
                f"[{v.get('severity', '?')}] {v.get('location', '?')}: "
                f"{v.get('description', '?')}"
                for v in exc.violations
            ]
            return {
                "compatible": False,
                "status": "incompatible",
                "summary": f"{len(exc.violations)} breaking change(s) detected",
                "issues": issues,
            }
        except Exception as exc:
            log.warning(
                "Compat check raised: %s — treating as indeterminate "
                "(non-passing), not compatible", exc
            )
            return {
                "compatible": False,
                "status": "checker_error",
                "summary": f"Compat check could not complete ({exc})",
                "issues": [],
            }

    @staticmethod
    def _ts_ge(a: str, b: str) -> bool:
        """Return True if timestamp ``a`` is at or after ``b``.

        Parses ISO-8601 timestamps (tolerating a trailing ``Z``) so ordering is
        correct even when the strings are not identically normalised. Falls back
        to raw string comparison if either value cannot be parsed.
        """
        def _parse(s: str) -> datetime | None:
            if not s:
                return None
            try:
                return datetime.fromisoformat(s.replace("Z", "+00:00"))
            except ValueError:
                return None

        pa, pb = _parse(a), _parse(b)
        if pa is not None and pb is not None:
            return pa >= pb
        return a >= b

    def _git_commit_scoped(self, files: list[str], message: str) -> None:
        """Stage only the given files and commit (cycle 58).

        Unlike ``_git_commit_all`` (``git add -A``), this commits only the
        Coder's actual edits — never state JSON or other working-tree changes.
        Files that cannot be staged (rolled back, missing) are skipped rather
        than aborting the approval. If nothing ends up staged, it is a no-op.
        """
        repo = self._config.repo_path
        # Isolate the index: clear any pre-staged changes from an earlier
        # operation so ONLY the files passed here land in this commit.
        # Best-effort — a repo with no commits yet has nothing to unstage.
        try:
            subprocess.run(
                ["git", "reset", "--", "."],
                cwd=repo,
                check=False,
                capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                timeout=30,
            )
        except OSError as exc:
            log.warning("Could not reset index before scoped commit: %s", exc)
        for f in files:
            try:
                subprocess.run(
                    ["git", "add", "--", f],
                    cwd=repo,
                    check=True,
                    capture_output=True,
                    text=True, encoding="utf-8", errors="replace",
                    timeout=30,
                )
            except subprocess.CalledProcessError as exc:
                log.warning("Could not stage %s: %s", f, (exc.stderr or "").strip())

        # check=True so a failed probe does not masquerade as "nothing staged":
        # without it a non-zero git exit yields empty stdout and we would skip
        # the commit and report success, silently dropping the Coder's edits.
        try:
            staged = subprocess.run(
                ["git", "diff", "--cached", "--name-only"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                timeout=30,
            )
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(
                f"Failed to probe staged files for scoped commit: "
                f"{(exc.stderr or '').strip() or exc}"
            ) from exc
        if not staged.stdout.strip():
            log.info("Nothing staged to commit (scoped)")
            return

        try:
            subprocess.run(
                ["git", "commit", "-m", message],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                timeout=30,
            )
            log.info("Scoped commit: %s", message)
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or "").strip().lower()
            if "nothing to commit" in stderr:
                log.info("No file changes to commit (scoped)")
                return
            raise RuntimeError(f"Failed scoped commit: {exc.stderr or exc}") from exc

    def _git_push_current_branch(self) -> None:
        branch = self._git_current_branch()
        if not branch:
            raise RuntimeError("Unable to determine current git branch for push")
        try:
            subprocess.run(
                ["git", "push", "origin", branch],
                cwd=self._config.repo_path,
                check=True,
                capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                timeout=120,
            )
            log.info("Pushed current branch to origin: %s", branch)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"Failed to push branch '{branch}': {exc.stderr or exc}") from exc

    def _github_repo_slug(self) -> str | None:
        try:
            result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=self._config.repo_path,
                check=True,
                capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                timeout=30,
            )
            remote = result.stdout.strip()
        except subprocess.CalledProcessError as exc:
            log.warning("Unable to read git remote origin: %s", exc)
            return None

        https_match = re.search(r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$", remote)
        if not https_match:
            return None
        owner = https_match.group("owner")
        repo = https_match.group("repo")
        return f"{owner}/{repo}"

    def _run_semantic_grouping(self) -> None:
        """Stage 4b: cluster deduped findings into semantic groups.

        Reads ``findings.json`` (post-dedup, post-reconciliation), runs
        ``SemanticGrouper.group`` with a ``FileAuditLog`` rooted at the
        state directory, and persists ``semantic_groups.json`` via the
        grouper's public ``write_groups`` writer (preserves
        ``sort_keys=True`` for deterministic diffs). The git commit is
        landed via ``StateStore.commit_state``, which stages the entire
        state directory (so the grouper's audit JSONL is included in the
        same commit).

        Canonical-emitter conflicts are caught per-group inside
        ``SemanticGrouper._build_group`` and surface as a populated
        ``canonical_emitter_conflict`` field on the affected
        ``SemanticGroup``; the grouper never raises
        ``CanonicalEmitterConflictError`` to this method. Healthy
        groups in the same batch proceed normally; Stage 5b emits a
        synthetic ``registry_conflict`` pattern for each quarantined
        group and bypasses the Architect call for those groups only.
        """
        log.info("--- Stage 4b: Semantic Grouping ---")
        deduped = self._store.read("findings")
        grouper = SemanticGrouper(
            audit_log=FileAuditLog(self._config.state_dir),
        )
        groups = grouper.group(deduped)
        serialized = serialize_groups(groups)

        # Stage 4b.2 — two-stage hybrid similarity merge (ON by default).
        # Collapses same-file/same-root-cause groups that the single-pass
        # grouping_key splits apart (embedding >= 0.78 OR subject-scoped shared
        # subject_key). Set SENTINEL_SEMANTIC_SIMILARITY_MERGE=false to disable
        # (e.g. when the optional MiniLM encoder is unavailable). See
        # grouper.two_stage_group / merge_serialized_groups.
        if os.getenv("SENTINEL_SEMANTIC_SIMILARITY_MERGE", "true").lower() in {
            "1", "true", "yes", "on",
        }:
            serialized = self._apply_similarity_merge(serialized, deduped)

        self._store.write("semantic_groups", serialized, sort_keys=True)
        self._store.commit_state("sentinel: Stage 4b — semantic groups")
        self._audit(
            "orchestrator",
            "semantic_groups_written",
            {"finding_count": len(deduped), "group_count": len(serialized)},
        )
        log.info(
            "Semantic grouping: %d findings → %d groups",
            len(deduped),
            len(serialized),
        )

    def _apply_similarity_merge(
        self, serialized_groups: list[dict], findings: list[dict]
    ) -> list[dict]:
        """Stage 4b.2 (gated): collapse same-file/same-root-cause groups via
        description-embedding similarity, reusing the Stage 4c MiniLM encoder.
        Writes near-miss pairs to ``semantic_groups_near_misses.json``. Returns
        the merged group list, or the input unchanged if the encoder is
        unavailable (e.g. optional ``workflow`` extras absent)."""
        import concurrent.futures

        from sentinel.assess.grouper import (
            merge_serialized_groups,
            two_stage_group,
        )

        def _embed_and_cluster() -> tuple[list, list]:
            from sentinel.workflow.build_concept_themes import _load_embedder
            # Same local MiniLM encoder Stage 4c (concept themes) loads.
            encoder = _load_embedder("sentence-transformers/all-MiniLM-L6-v2", 42)
            return two_stage_group(findings, encoder)

        # Model load + embedding can hang on a slow/absent model download.
        # Bound the whole embedder section to a 120s deadline (mirrors the
        # semantic-verification ThreadPoolExecutor pattern) — on timeout, skip
        # the merge and return the groups unchanged rather than block Stage 4b.
        _embed_timeout = 120
        _executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        _future = _executor.submit(_embed_and_cluster)
        try:
            clusters, near_misses = _future.result(timeout=_embed_timeout)
        except concurrent.futures.TimeoutError:
            log.warning(
                "Similarity merge skipped — embedder exceeded %ss deadline",
                _embed_timeout,
            )
            return serialized_groups
        except ImportError as exc:
            # The embedder helper module itself is unavailable — the documented
            # optional-encoder-unavailable condition. Skip and return unchanged.
            log.warning(
                "Similarity merge skipped — optional embedder module unavailable: %s",
                exc,
            )
            return serialized_groups
        except RuntimeError as exc:
            if isinstance(exc.__cause__, ImportError):
                # _load_embedder wraps the missing optional 'workflow' extras
                # (ImportError) as RuntimeError — the documented optional
                # encoder-unavailable skip path (FIX-REQUIRED-009).
                log.warning(
                    "Similarity merge skipped — optional embedder extras "
                    "unavailable: %s", exc,
                )
                return serialized_groups
            # An unexpected embedder initialization failure (e.g. model load)
            # must NOT be reported as the same success-like skip — surface it
            # distinctly and do not silently degrade.
            log.error(
                "Similarity merge failed — unexpected embedder initialization "
                "error: %s", exc,
            )
            raise
        finally:
            _executor.shutdown(wait=False)
        merged = merge_serialized_groups(serialized_groups, clusters)
        self._store.write(
            "semantic_groups_near_misses", near_misses, sort_keys=True
        )
        log.info(
            "Similarity merge: %d → %d groups, %d near-miss pair(s)",
            len(serialized_groups), len(merged), len(near_misses),
        )
        return merged

    @property
    def _workflow_config(self) -> WorkflowConfig:
        """Lazy, cached ``WorkflowConfig`` for the orchestrator.

        Sub-step 8-E (cycle 31): on first access, loads the YAML at
        ``self._config.workflow_config_yaml_path`` if set, else falls
        back to ``WorkflowConfig.default()``. Cached on
        ``self._workflow_config_cache`` so repeated access is free.
        """
        if self._workflow_config_cache is None:
            path = self._config.workflow_config_yaml_path
            self._workflow_config_cache = (
                load_workflow_config_yaml(Path(path))
                if path
                else WorkflowConfig.default()
            )
        return self._workflow_config_cache

    def _run_concept_themes(self) -> None:
        """Stage 4c: assemble conceptual themes from finding descriptions.

        Reads ``findings.json`` via ``load_findings`` (the regime-boundary
        rehydration bridge), reads any prior ``concept_themes.json`` for
        within-run stability reconciliation, and invokes
        ``build_concept_themes_from_descriptions`` to cluster findings
        into ``ConceptTheme`` records. Writes ``concept_themes.json``
        via ``StateStore.write(..., sort_keys=True)`` and lands a git
        commit through ``StateStore.commit_state``.

        Sub-step 8-E (cycle 31): the 5 producer parameters
        (``embedding_model``, ``embedding_seed``, ``min_cluster_size``,
        ``min_samples``, ``cluster_selection_epsilon``) are threaded
        from ``self._workflow_config.themes.conceptual_lens``. The
        count-based short-circuit floor is also config-derived so the
        guard and the producer agree on the same ``min_cluster_size``.

        Best-effort enrichment: when the optional ``workflow`` extras
        (``sentence-transformers`` / ``hdbscan``) are absent, the
        producer's ``_load_embedder`` / ``_load_clusterer`` guards
        re-raise as ``RuntimeError``. This method catches that
        ``RuntimeError``, logs a warning, writes an empty
        ``concept_themes.json``, commits with a distinct skip-message,
        records a distinct ``concept_themes_skipped`` audit event, and
        returns cleanly so Stage 5b proceeds unimpeded.
        """
        log.info("--- Stage 4c: Concept Themes ---")
        # Master switch (cycle 40): skip the conceptual lens entirely when the
        # orchestrator-level env toggle SENTINEL_ENABLE_CONCEPT_THEMES is off,
        # OR when the WorkflowConfig conceptual_lens.enabled field is false.
        # Both default True, so this preserves the historic always-run
        # behavior. Mirrors the two skip branches below: writes an empty
        # concept_themes.json for artifact completeness, records a distinct
        # concept_themes_skipped audit event, and returns so Stage 5b
        # proceeds unimpeded. (Short-circuit order keeps the env switch a
        # true master kill-switch -- the workflow config is not even consulted
        # when the env toggle is off.)
        if not (
            self._config.enable_concept_themes
            and self._workflow_config.themes.conceptual_lens.enabled
        ):
            log.info(
                "Stage 4c: concept themes disabled "
                "(SENTINEL_ENABLE_CONCEPT_THEMES / conceptual_lens.enabled);"
                " writing empty themes."
            )
            self._store.write("concept_themes", [], sort_keys=True)
            self._store.commit_state(
                "sentinel: Stage 4c — concept themes (skipped, disabled)"
            )
            self._audit(
                "orchestrator",
                "concept_themes_skipped",
                {"reason": "disabled_by_config"},
            )
            return
        findings = list(load_findings(self._config.state_dir))
        conceptual = self._workflow_config.themes.conceptual_lens
        # Config-derived clustering floor (sub-step 8-E): the guard and
        # the producer must agree on the same ``min_cluster_size`` so
        # lowering it via YAML does not leave findings short-circuited
        # under the old hardcoded 3.
        min_floor = conceptual.hdbscan.min_cluster_size
        if len(findings) < min_floor:
            log.info(
                "Stage 4c: %d finding(s) below clustering threshold"
                " (%d); writing empty concept themes.",
                len(findings),
                min_floor,
            )
            self._store.write("concept_themes", [], sort_keys=True)
            self._store.commit_state(
                "sentinel: Stage 4c — concept themes"
                " (skipped, insufficient findings)"
            )
            self._audit(
                "orchestrator",
                "concept_themes_skipped",
                {
                    "reason": "insufficient_findings",
                    "finding_count": len(findings),
                    "min_cluster_size": min_floor,
                },
            )
            return
        previous_themes = [
            ConceptTheme.from_dict(d)
            for d in self._store.read("concept_themes")
        ]
        try:
            themes = build_concept_themes_from_descriptions(
                findings,
                previous_themes=previous_themes,
                embedding_model=conceptual.embedding_model,
                embedding_seed=conceptual.embedding_seed,
                min_cluster_size=conceptual.hdbscan.min_cluster_size,
                min_samples=conceptual.hdbscan.min_samples,
                cluster_selection_epsilon=(
                    conceptual.hdbscan.cluster_selection_epsilon
                ),
            )
        except RuntimeError as exc:
            # Only a *missing optional dependency* justifies the silent skip.
            # The producer's _load_embedder / _load_clusterer guards re-raise
            # the underlying ImportError as a RuntimeError whose message names
            # the optional 'workflow' extras / the pip install remedy; a raw
            # ImportError message ("No module" / "cannot import") is also
            # accepted. Any OTHER RuntimeError (bad data, internal logic error)
            # is a genuine failure and must not be swallowed.
            msg = str(exc).lower()
            missing_dep_markers = (
                "no module",
                "cannot import",
                "workflow' dependencies",
                "pip install",
            )
            if not any(marker in msg for marker in missing_dep_markers):
                raise
            log.warning(
                "Stage 4c concept-theme assembly skipped: %s. "
                "Install with `pip install -e \".[workflow]\"` to enable.",
                exc,
            )
            self._store.write("concept_themes", [], sort_keys=True)
            self._store.commit_state(
                "sentinel: Stage 4c — concept themes (skipped, extras missing)"
            )
            self._audit(
                "orchestrator",
                "concept_themes_skipped",
                {
                    "reason": "workflow_extras_missing",
                    "finding_count": len(findings),
                },
            )
            return

        self._store.write(
            "concept_themes",
            [t.to_dict() for t in themes],
            sort_keys=True,
        )
        self._store.commit_state("sentinel: Stage 4c — concept themes")
        self._audit(
            "orchestrator",
            "concept_themes_written",
            {
                "finding_count": len(findings),
                "theme_count": len(themes),
            },
        )
        log.info(
            "Concept themes: %d findings → %d themes",
            len(findings),
            len(themes),
        )

    def _run_remediation_pattern_proposals(self) -> None:
        """Stage 5b: propose one RemediationPattern per SemanticGroup
        via the Architect.

        Reads ``semantic_groups.json``, iterates each group, assembles
        a per-group user_message via
        ``build_remediation_pattern_context``, invokes
        ``ArchitectAgent.propose_remediation_pattern``, and writes the
        full list of patterns to ``remediation_patterns.json``.

        Per-group canonical-emitter conflicts surface as a populated
        ``canonical_emitter_conflict`` field on the serialised group
        dict. For those groups, a synthetic
        ``registry_conflict`` pattern is emitted via
        ``build_synthetic_conflict_pattern`` and the Architect is not
        invoked — quarantine is per-group; healthy groups in the same
        batch proceed normally.

        Per-group failures (Architect ``AgentResponseError``) are logged
        and skipped; the group is still recorded in the audit log so
        the gap is visible.
        """
        log.info("--- Stage 5b: Remediation Pattern Proposals ---")
        # Read serialised groups via StateStore (promoted cycle 17 sub-step
        # 1B) — they're already dict-shaped via grouper.write_groups and
        # that's the shape build_remediation_pattern_context expects.
        groups = self._store.read("semantic_groups")
        if not groups:
            log.info("Stage 5b: no semantic groups to process.")
            self._store.write(
                "remediation_patterns",
                [],
                commit=True,
                commit_message="sentinel: Stage 5b — no groups",
            )
            return

        all_findings = {
            f.get("id"): f for f in self._store.read("findings")
        }

        patterns: list[dict[str, Any]] = []
        conflict_count = 0
        for group_dict in groups:
            conflict_data = group_dict.get("canonical_emitter_conflict")
            if conflict_data:
                conflict_record = ConflictRecord(
                    finding_kind=conflict_data["finding_kind"],
                    contributing_emitters=tuple(
                        conflict_data.get("contributing_emitters") or ()
                    ),
                    conflicting_canonicals=tuple(
                        conflict_data.get("conflicting_canonicals") or ()
                    ),
                )
                synthetic = build_synthetic_conflict_pattern(
                    group_dict, conflict_record
                )
                self._audit(
                    "orchestrator",
                    "stage5b_group_conflict",
                    {
                        "group_id": group_dict.get("group_id"),
                        "finding_kind": conflict_record.finding_kind,
                        "contributing_emitters": list(
                            conflict_record.contributing_emitters
                        ),
                        "conflicting_canonicals": list(
                            conflict_record.conflicting_canonicals
                        ),
                        "synthetic_pattern_id": synthetic["pattern_id"],
                    },
                )
                patterns.append(synthetic)
                conflict_count += 1
                continue

            member_ids = group_dict.get("member_finding_ids") or []
            member_findings = [
                all_findings[fid]
                for fid in member_ids
                if fid in all_findings
            ]
            pattern_id = _compute_pattern_id(group_dict)
            ctx = build_remediation_pattern_context(
                group=group_dict,
                member_findings=member_findings,
                pattern_id=pattern_id,
                loader=self._loader,
                skill_files_dir=self._config.skill_files_dir,
            )
            # Throttle Stage-5b to avoid bursting the LLM per-window quota:
            # ~160 proposals spread out instead of ~1s (configurable; default
            # 0.5s — shares the verifiability-gate chunk-delay setting).
            time.sleep(self._config.verifiability_gate_chunk_delay)
            try:
                pattern = self._architect.propose_remediation_pattern(ctx)
            except (AgentResponseError, TimeoutError) as exc:
                # The agent call carries its own internal (HTTP) timeout; a
                # TimeoutError here means that deadline elapsed. Log-and-continue
                # so one slow/hung group cannot stall the whole Stage 5b loop.
                log.error(
                    "Stage 5b: Architect failed on group %s: %s",
                    group_dict.get("group_id"),
                    exc,
                )
                self._audit(
                    "orchestrator",
                    "remediation_pattern_failed",
                    {
                        "group_id": group_dict.get("group_id"),
                        "pattern_id": pattern_id,
                        "error": str(exc),
                    },
                )
                continue
            patterns.append(pattern)

        self._store.write(
            "remediation_patterns",
            patterns,
            commit=True,
            commit_message="sentinel: Stage 5b — remediation patterns",
        )
        self._audit(
            "orchestrator",
            "remediation_patterns_written",
            {
                "group_count": len(groups),
                "pattern_count": len(patterns),
                "conflict_count": conflict_count,
                "failed_count": len(groups) - len(patterns),
            },
        )
        log.info(
            "Stage 5b: %d groups → %d patterns (%d conflict, %d failed)",
            len(groups),
            len(patterns),
            conflict_count,
            len(groups) - len(patterns) - conflict_count,
        )

    # ------------------------------------------------------------------
    # Stage 5c — Automated finding verification
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_language(file_path: str) -> str:
        """Map a finding's file extension to a human-readable language name
        for the Stage 5c verifier prompt."""
        suffix = Path(file_path).suffix.lower().lstrip(".")
        mapping = {
            "py": "Python",
            "cs": "C#",
            "cpp": "C++",
            "cc": "C++",
            "h": "C++",
            "hpp": "C++",
            "js": "JavaScript",
            "ts": "TypeScript",
            "tsx": "TypeScript",
        }
        return mapping.get(suffix, "generic")

    def _run_finding_verification(self) -> None:
        """Stage 5c: adjudicate each finding against its cited source.

        Findings are grouped by ``file`` (one Architect call per file, all of
        that file's findings batched together). Each finding is updated with a
        verdict, verified location, evidence, and — when the verifier judges the
        severity miscalibrated — a ``recalibrated_severity``. Findings whose
        verdict is UNVERIFIED are removed from ``findings.json`` and written to
        ``unverified_findings.json`` (operator-visible quarantine, never shown
        to the code owner). Tech-debt items whose finding is now unverified are
        dropped.

        Degradation: a single ``verify_finding`` failure on one file is logged
        and that file's findings are KEPT (treated as verified) rather than
        dropped — one bad model call must never delete real findings or abort
        Phase 1.
        """
        if not self._config.verification_stage:
            return

        log.info("--- Stage 5c: Automated Finding Verification ---")
        findings = self._store.read("findings") or []

        # Resolve each finding against its OWN repository root. Findings carry
        # subtree-relative file paths whose authoritative root is the discovered
        # repository path (repositories.json) — NOT config.repo_path, which is
        # the CWD default (".") when the target repo is passed as a CLI argument
        # rather than via SENTINEL_REPO_PATH. Using config.repo_path here makes
        # every source lookup miss and quarantines all findings as UNVERIFIED.
        # (FIX: Stage 5c "Source not found" — smoke-test-caught.)
        repositories = self._store.read("repositories") or []
        repo_root_by_name = {
            r["name"]: str(r["path"])
            for r in repositories
            if isinstance(r, dict) and r.get("name") and r.get("path")
        }
        default_repo_root = (
            str(repositories[0]["path"])
            if repositories
            and isinstance(repositories[0], dict)
            and repositories[0].get("path")
            else str(self._config.repo_path)
        )

        def _repo_root_for(finding: dict[str, Any]) -> str:
            # Findings key their repo via the ``module`` prefix
            # ("<repo>:<pkg>:<file>"); fall back to the first repo, then config.
            module = str(finding.get("module", "") or "")
            repo_name = module.split(":", 1)[0] if ":" in module else ""
            return repo_root_by_name.get(repo_name, default_repo_root)

        # Group findings by (repo root, cited file), preserving first-seen order.
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            file_key = finding.get("file", "") or ""
            groups.setdefault(
                (_repo_root_for(finding), file_key), []
            ).append(finding)

        # Chunk each file's findings into capped sub-batches so a single
        # oversized call can never blank out a reasoning model's output and
        # sink a whole file's worth of findings into keep-by-default. Honor the
        # operator cap, else a safe default.
        max_per_batch = (
            self._config.verifiability_max_chunk
            or _STAGE5C_MAX_FINDINGS_PER_BATCH
        )

        for (repo_root, file_key), group in groups.items():
            language = self._detect_language(file_key)
            for offset in range(0, len(group), max_per_batch):
                sub = group[offset:offset + max_per_batch]
                self._verify_findings_adaptive(
                    file_key, language, sub, repo_root
                )

        # Partition into verified / unverified.
        verified_findings: list[dict[str, Any]] = []
        unverified_findings: list[dict[str, Any]] = []
        for _group_key, group in groups.items():
            for finding in group:
                if finding.get("verified", True):
                    verified_findings.append(finding)
                else:
                    unverified_findings.append(finding)

        self._store.write("findings", verified_findings)
        self._store.write("unverified_findings", unverified_findings)

        # Drop tech-debt items whose finding is now unverified.
        unverified_ids = {
            f.get("id") for f in unverified_findings if f.get("id")
        }
        if unverified_ids:
            tech_debt = self._store.read("tech_debt") or []
            kept_debt = [
                item
                for item in tech_debt
                if item.get("finding_id") not in unverified_ids
            ]
            if len(kept_debt) != len(tech_debt):
                self._store.write("tech_debt", kept_debt)

        verified_count = len(verified_findings)
        unverified_count = len(unverified_findings)
        total = verified_count + unverified_count
        precision = round(100 * verified_count / total) if total else 100
        log.info(
            "Stage 5c: %d verified, %d unverified (%d%% precision)",
            verified_count, unverified_count, precision,
        )
        self._audit(
            "orchestrator",
            "finding_verification_complete",
            {
                "verified": verified_count,
                "unverified": unverified_count,
                "precision_pct": precision,
            },
        )

    def _run_alarm_classification(self) -> None:
        """Stage 5c-alarm: label each verified finding's alarm relevance.

        Every finding is stamped with ``alarm_relevance`` (``direct`` |
        ``adjacent`` | ``none``) and ``alarm_rationale``. A deterministic
        keyword classifier seeds every finding; in local-only mode (or when the
        Architect pass fails for a batch) the seed is authoritative. Otherwise
        the Architect refines the labels one batch at a time.

        Degradation: a failed classification call on one batch falls back to
        that batch's deterministic seed labels — one bad model call must never
        blank labels or abort Phase 1.
        """
        if not self._config.enable_alarm_classification:
            return

        from sentinel.assess.alarm_classifier import seed_alarm_relevance

        log.info("--- Stage 5c-alarm: Alarm Relevance Classification ---")
        findings = self._store.read("findings") or []
        classifiable = [f for f in findings if isinstance(f, dict)]
        if not classifiable:
            self._store.commit_state(
                "sentinel: Stage 5c-alarm — alarm classification (no findings)"
            )
            return

        seeds = seed_alarm_relevance(classifiable)

        # Deterministic seed is authoritative in local-only mode; otherwise it
        # primes the Architect refinement pass.
        if self._config.local_only_mode:
            for finding, seed in zip(classifiable, seeds):
                finding["alarm_relevance"] = seed["alarm_relevance"]
                finding["alarm_rationale"] = seed["alarm_rationale"]
        else:
            for offset in range(
                0, len(classifiable), _STAGE5C_ALARM_MAX_FINDINGS_PER_BATCH
            ):
                sub = classifiable[
                    offset:offset + _STAGE5C_ALARM_MAX_FINDINGS_PER_BATCH
                ]
                sub_seeds = seeds[
                    offset:offset + _STAGE5C_ALARM_MAX_FINDINGS_PER_BATCH
                ]
                self._classify_alarm_batch(sub, sub_seeds)

        self._store.write("findings", findings)

        counts = Counter(
            str(f.get("alarm_relevance", "none")) for f in classifiable
        )
        log.info(
            "Stage 5c-alarm: %d direct, %d adjacent, %d none",
            counts.get("direct", 0),
            counts.get("adjacent", 0),
            counts.get("none", 0),
        )
        self._audit(
            "orchestrator",
            "alarm_classification_complete",
            {
                "direct": counts.get("direct", 0),
                "adjacent": counts.get("adjacent", 0),
                "none": counts.get("none", 0),
            },
        )
        self._store.commit_state(
            "sentinel: Stage 5c-alarm — alarm classification"
        )

    def _classify_alarm_batch(
        self, sub: list[dict[str, Any]], sub_seeds: list[dict[str, Any]],
    ) -> None:
        """Classify one batch of findings via the Architect, seed on failure.

        Applies the deterministic seed first (so every finding is always
        labeled), then overlays the Architect's refined verdict when the call
        succeeds and returns a valid label for that finding's batch index.
        """
        # Seed every finding up front — this is the fallback if the model call
        # fails or drops an element.
        for finding, seed in zip(sub, sub_seeds):
            finding["alarm_relevance"] = seed["alarm_relevance"]
            finding["alarm_rationale"] = seed["alarm_rationale"]

        context = self._build_alarm_classification_batch(sub, sub_seeds)

        result: dict[str, Any] | None = None
        for attempt in range(1, _STAGE5C_ALARM_CLASSIFY_ATTEMPTS + 1):
            try:
                result = self._architect.classify_alarm_relevance(context)
                break
            except Exception as exc:  # noqa: BLE001 — degrade, never abort
                log.warning(
                    "Stage 5c-alarm: classify attempt %d/%d failed "
                    "(%d findings): %s",
                    attempt, _STAGE5C_ALARM_CLASSIFY_ATTEMPTS, len(sub), exc,
                )

        if result is None:
            self._audit(
                "orchestrator",
                "alarm_classification_failed",
                {"count": len(sub), "fallback": "deterministic_seed"},
            )
            return

        allowed = {"direct", "adjacent", "none"}
        by_index = {
            c.get("index"): c
            for c in result.get("classifications", [])
            if isinstance(c, dict)
        }
        for position, finding in enumerate(sub):
            classification = by_index.get(position)
            if not isinstance(classification, dict):
                continue
            relevance = str(
                classification.get("alarm_relevance", "")
            ).strip().lower()
            if relevance not in allowed:
                continue
            finding["alarm_relevance"] = relevance
            finding["alarm_rationale"] = str(
                classification.get("alarm_rationale", "") or ""
            )

    def _build_alarm_classification_batch(
        self, sub: list[dict[str, Any]], sub_seeds: list[dict[str, Any]],
    ) -> str:
        """Assemble the Stage 5c-alarm batch context (lightweight metadata)."""
        batch = []
        for position, (finding, seed) in enumerate(zip(sub, sub_seeds)):
            batch.append(
                {
                    "index": position,
                    "title": str(finding.get("title", "") or ""),
                    "finding_kind": str(
                        finding.get("finding_kind", finding.get("kind", "")) or ""
                    ),
                    "description": str(finding.get("description", "") or ""),
                    "file": str(finding.get("file", "") or ""),
                    "seed_relevance": seed["alarm_relevance"],
                }
            )
        return (
            "Classify the ALARM relevance of each finding below.\n\n"
            "findings = " + json.dumps(batch, indent=2)
        )

    def _verify_batch_with_retry(
        self, batch_context: str, file_key: str, count: int,
    ) -> tuple[dict[str, Any] | None, Exception | None]:
        """Call the architect verifier with a bounded retry.

        Returns ``(result, None)`` on success, or ``(None, last_exc)`` when every
        attempt failed (transient gateway ``{"detail": ...}`` envelopes, empty
        completions, etc.). The caller decides whether to bisect-and-recurse or,
        at a single finding, keep-by-default — one bad model call must never
        delete findings.
        """
        last_exc: Exception | None = None
        for attempt in range(1, _STAGE5C_VERIFY_ATTEMPTS + 1):
            try:
                return self._architect.verify_finding(batch_context), None
            except Exception as exc:  # noqa: BLE001 — degrade, never abort
                last_exc = exc
                log.warning(
                    "Stage 5c: verify attempt %d/%d failed for %s "
                    "(%d findings): %s",
                    attempt, _STAGE5C_VERIFY_ATTEMPTS,
                    file_key or "<no file>", count, exc,
                )
        return None, last_exc

    def _verify_findings_adaptive(
        self,
        file_key: str,
        language: str,
        sub: list[dict[str, Any]],
        repo_root: str,
    ) -> None:
        """Verify one batch of a file's findings, bisecting on failure.

        A whole-batch failure is most often an oversized/dense request that
        exhausts a reasoning model's output budget and returns empty/non-JSON.
        Rather than dumping the entire batch to keep-by-default, split it in half
        and recurse so the maximum number of findings still get a real verdict.
        Only a single finding that still fails after retries is kept-by-default
        (``VERIFICATION_ERROR``) — never dropped.
        """
        if not sub:
            return
        batch_context = self._build_verification_batch(
            file_key, language, sub, repo_root
        )
        result, last_exc = self._verify_batch_with_retry(
            batch_context, file_key, len(sub)
        )

        if result is None:
            if len(sub) > 1:
                # Bisect and recurse — a smaller request usually fits the budget.
                mid = len(sub) // 2
                self._verify_findings_adaptive(
                    file_key, language, sub[:mid], repo_root
                )
                # Throttle between split calls so a deep bisection doesn't burst
                # the LLM per-window rate limit.
                time.sleep(self._config.verifiability_gate_chunk_delay)
                self._verify_findings_adaptive(
                    file_key, language, sub[mid:], repo_root
                )
                return
            # Single finding still failing — keep it, auditable.
            log.error(
                "Stage 5c: verification failed for %s (1 finding) after "
                "bisecting to minimum size: %s — keeping finding",
                file_key or "<no file>", last_exc,
            )
            self._audit(
                "orchestrator",
                "finding_verification_failed",
                {"file": file_key, "count": 1, "error": str(last_exc)},
            )
            for finding in sub:
                finding["verified"] = True
                finding.setdefault("verification_verdict", "VERIFICATION_ERROR")
            return

        by_index = {
            v.get("index"): v
            for v in result.get("verifications", [])
            if isinstance(v, dict)
        }
        # Verifier indices are 1-based within the batch it was given, so
        # enumerate this sub-batch (not the whole file's group).
        for position, finding in enumerate(sub, start=1):
            verification = by_index.get(position)
            if verification is None:
                # Model returned valid JSON but dropped this finding — keep it.
                log.warning(
                    "Stage 5c: no verification for finding %d in %s — keeping",
                    position, file_key or "<no file>",
                )
                finding["verified"] = True
                finding.setdefault(
                    "verification_verdict", "VERIFICATION_MISSING"
                )
                continue
            self._apply_verification(finding, verification)

    def _build_verification_batch(
        self,
        file_path: str,
        language: str,
        findings: list[dict[str, Any]],
        repo_root: str,
    ) -> str:
        """Assemble the Stage 5c batch context for one file's findings."""
        from sentinel.orchestrator.source_retrieval import (
            get_source_for_finding,
            _neutralize_code_fences,
        )

        parts = [f"File: {file_path} (Language: {language})", ""]
        for position, finding in enumerate(findings, start=1):
            source = _neutralize_code_fences(
                get_source_for_finding(finding, repo_root)
            )
            parts.append(f"Finding {position}:")
            parts.append(str(finding.get("title", "") or ""))
            parts.append(
                str(finding.get("finding_kind", finding.get("kind", "")) or "")
            )
            parts.append(str(finding.get("description", "") or ""))
            parts.append(
                f"Cited location: {finding.get('line_range', '') or 'n/a'}"
            )
            parts.append("Source at cited location:")
            parts.append(source)
            parts.append("")
        return "\n".join(parts)

    def _apply_verification(
        self,
        finding: dict[str, Any],
        verification: dict[str, Any],
    ) -> None:
        """Merge one verifier verdict into its finding in place."""
        verdict = str(verification.get("verdict", "")).strip().upper()
        finding["verified"] = verdict in {"VERIFIED", "RELOCATED"}
        finding["verification_verdict"] = verdict or "UNKNOWN"
        finding["verified_location"] = {
            "start": verification.get("actual_line_start"),
            "end": verification.get("actual_line_end"),
        }
        finding["verification_evidence"] = verification.get("evidence", "")

        # Only record a recalibrated severity when it actually differs from the
        # finding's current severity.
        recommended = verification.get("recommended_severity")
        if recommended and recommended != finding.get("severity"):
            finding["recalibrated_severity"] = recommended

    def _audit(
        self,
        agent: str,
        action: str,
        details: dict[str, Any],
    ) -> None:
        self._store.append_audit({
            "agent": agent,
            "action": action,
            "details": details,
        })

    def close(self) -> None:
        """Close the API client connection."""
        self._client.close()
        log.info("API usage: %s", self._client.usage.report())

    def __enter__(self) -> "Orchestrator":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
