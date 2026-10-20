"""
sentinel/assess/reviewer.py

Stage 3 — Module-by-Module Review (Layers 1–4).

Feeds each module through the layered skill framework:
  1. Layer 1 (Review Prioritizer) classifies the module and returns the
     recommended skill sequence.
  2. Layer 2a (Quick Review) runs unconditionally on every module.
  3. Specialist skills (2b–2d, 3a–3e, 4a–4b) run when triggered by
     Quick Review findings or Layer 1's risk assessment.

All findings are written to ``sentinel/state/findings.json`` as they
accumulate. The reviewer processes modules in priority order (highest
criticality first, as ordered by the Cartographer).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Literal

from sentinel.agents.architect import ArchitectAgent
from sentinel.assess.dedup import deduplicate
from sentinel.assess.finding_schema import (
    FindingSchemaValidationError,
    _classify_category,
    _extract_file_path,
    _extract_function_name,
    _extract_line_range,
    flatten_blob,
    is_blob_finding,
    materialize_emitter_finding,
    repair_invalid_finding,
)
from sentinel.assess.scan_context import ScanContext
from sentinel.assess.chunker import Chunk, Chunker
from sentinel.config import Config
from sentinel.orchestrator.context import build_module_review_context, build_review_prioritizer_context
from sentinel.orchestrator.state_store import StateStore
from sentinel.skills.loader import Skills

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _PayloadRetryResult:
    # Closed status contract enforced at runtime (FIX-REQUIRED-327): a
    # typing.NamedTuple cannot override __new__/__init__ to validate, so this is
    # a frozen dataclass whose __post_init__ rejects any out-of-set status
    # before it can enter retry accounting or control flow.
    status: Literal["complete", "partial", "failed"]
    findings: list[dict]

    def __post_init__(self) -> None:
        if self.status not in ("complete", "partial", "failed"):
            raise ValueError(
                "_PayloadRetryResult.status must be one of "
                f"'complete', 'partial', 'failed'; got {self.status!r}"
            )


# Skills that run unconditionally (every module)
_MANDATORY_SKILLS: list[str] = [Skills.QUICK_REVIEW]

# Trigger → specialist skill mapping.
# Quick Review findings mentioning these trigger keywords cause the
# corresponding specialist to run.
_SPECIALIST_TRIGGERS: dict[str, list[str]] = {
    "security": [Skills.SECURITY],
    "injection": [Skills.SECURITY],
    "authentication": [Skills.SECURITY],
    "authorization": [Skills.SECURITY],
    "correctness": [Skills.CORRECTNESS],
    "off-by-one": [Skills.CORRECTNESS],
    "race condition": [Skills.CORRECTNESS, Skills.CONCURRENCY],
    "performance": [Skills.PERFORMANCE],
    "n+1": [Skills.PERFORMANCE],
    "memory": [Skills.PERFORMANCE],
    "maintainability": [Skills.MAINTAINABILITY],
    "complexity": [Skills.MAINTAINABILITY],
    "duplication": [Skills.MAINTAINABILITY],
    "testability": [Skills.TESTABILITY],
    "test coverage": [Skills.TESTABILITY],
    "error handling": [Skills.ERROR_HANDLING],
    "exception": [Skills.ERROR_HANDLING],
    "concurrency": [Skills.CONCURRENCY],
    "async": [Skills.CONCURRENCY],
    "data integrity": [Skills.DATA_INTEGRITY],
    "validation": [Skills.DATA_INTEGRITY],
    "transaction": [Skills.DATA_INTEGRITY],
    "code smell": [Skills.CODE_SMELL],
    "refactor": [Skills.REFACTORING_ADVISOR],
    "god class": [Skills.CODE_SMELL, Skills.REFACTORING_ADVISOR],
    # Alarm / WIP-stall (tool-idle) signals route to the equipment-domain
    # specialist (layer2k). Distinctive terms only, to avoid over-triggering.
    "alarm": [Skills.ALARM_INTEGRITY],
    "secs": [Skills.ALARM_INTEGRITY],
    "gem": [Skills.ALARM_INTEGRITY],
    "nuisance": [Skills.ALARM_INTEGRITY],
    "interlock": [Skills.ALARM_INTEGRITY],
    "wip": [Skills.ALARM_INTEGRITY],
    "deadlock": [Skills.ALARM_INTEGRITY, Skills.CONCURRENCY],
}

# Normalized finding fields that contribute to the specialist routing corpus.
# Routing keys off these (not description alone) so a trigger signal carried by
# summary/finding_kind/category/subject_key still selects the right specialist.
_ROUTING_CORPUS_FIELDS: tuple[str, ...] = (
    "summary",
    "description",
    "finding_kind",
    "category",
    "subject_key",
)

# File extensions for languages where "async" is a standard pattern and
# should NOT trigger the concurrency specialist.
_ASYNC_NATIVE_EXTENSIONS = frozenset({".js", ".jsx", ".ts", ".tsx"})

# Keywords that trigger concurrency only because they mention "async" —
# these are suppressed for async-native modules.
_ASYNC_ONLY_TRIGGERS = frozenset({"async", "concurrency"})

# User input always triggers Security specialist
_INPUT_KEYWORDS = frozenset({"user input", "request", "form", "query param", "http"})

_PAYLOAD_RETRY_MAX_LINES = 120
_PAYLOAD_RETRY_OVERLAP_LINES = 20
_PAYLOAD_RETRY_MAX_SUBCHUNKS = 8
_PAYLOAD_RETRY_MAX_CHARS = 1200
_PAYLOAD_RETRY_CHAR_OVERLAP = 120
_PAYLOAD_RETRY_MAX_CHAR_PARTS = 30
_PAYLOAD_RETRY_RATE_LIMIT_ATTEMPTS = 3
# Exponential backoff cap (seconds) between rate-limit retries. Without a sleep,
# a 429 is retried immediately, which neither respects the gateway's rate window
# nor lets the transient limit clear (F2-186e85c44280).
_RATE_LIMIT_BACKOFF_CAP_SECONDS = 30

# Full specialist coverage set (Layer 2b-2d, 3a-3e, 4a-4b).
_FULL_SPECIALIST_SKILLS = set(Skills.SPECIALISTS + [
    Skills.CODE_SMELL,
    Skills.REFACTORING_ADVISOR,
])

_RELIABILITY_FIRST_ALWAYS_ON = {
    Skills.CORRECTNESS,
    Skills.DATA_INTEGRITY,
    Skills.ERROR_HANDLING,
    # Operational Risk (layer2j) runs on every module under reliability-first:
    # the operational-surface lens (unbounded timeouts, partial-failure cascades,
    # weak failure-path observability, unsafe retry) is the primary driver of
    # production idle-time / nuisance-error cycle cost on runtime-control code.
    Skills.OPERATIONAL_RISK,
}

_RELIABILITY_FIRST_ALLOWED = _RELIABILITY_FIRST_ALWAYS_ON | {
    Skills.SECURITY,
    Skills.CONCURRENCY,
}

# Supported values for SENTINEL_REVIEW_PROFILE / the review_profile argument.
_SUPPORTED_REVIEW_PROFILES = frozenset({"triggered", "reliability-first"})

_SECURITY_BOUNDARY_KEYWORDS = frozenset({
    "auth", "token", "password", "secret", "credential", "certificate",
    "tls", "ssl", "xml", "config", "path", "file", "directory",
    "request", "response", "http", "https", "url", "endpoint",
    "command", "process", "open(", "readalltext", "writetext",
})

_SECURITY_BOUNDARY_SUFFIXES = (
    ".xml",
    ".config",
    ".json",
    ".yaml",
    ".yml",
)


def _is_async_native_module(module: dict) -> bool:
    """Check if module is primarily async-native (JS/TS) code.

    Returns True when more than 50% of the module's files have JS/TS
    extensions, indicating that ``async/await`` is a standard language
    feature rather than a concurrency concern.
    """
    files = module.get("files", [])
    if not files:
        return False
    async_native_count = sum(
        1 for f in files
        if any(f.endswith(ext) for ext in _ASYNC_NATIVE_EXTENSIONS)
    )
    return async_native_count > len(files) * 0.5


def _needs_security_review(module: dict | None, module_content: str) -> bool:
    lowered = module_content.lower()
    if any(keyword in lowered for keyword in _INPUT_KEYWORDS):
        return True
    if any(keyword in lowered for keyword in _SECURITY_BOUNDARY_KEYWORDS):
        return True

    if not module:
        return False

    for file_path in module.get("files", []):
        lowered_path = str(file_path).replace("\\", "/").lower()
        if lowered_path.endswith(_SECURITY_BOUNDARY_SUFFIXES):
            return True
        if lowered_path.startswith("config/") or "/config/" in lowered_path:
            return True
    return False


class ReviewOrchestrator:
    """
    Orchestrates the layered skill review for a single module.

    Called by the AssessmentPipeline for each module in priority order.
    Accumulates findings in the state store after each skill run.
    """

    def __init__(
        self,
        architect: ArchitectAgent,
        store: StateStore,
        config: Config,
        scan_context: ScanContext,
    ) -> None:
        self._architect = architect
        self._store = store
        self._config = config
        self._scan_context = scan_context
        self._strict_mode = bool(getattr(config, "strict_quality_mode", False))
        self._repo_root_canonical = scan_context.repo_root_canonical
        # Per-module tally of LLM findings repaired by graceful degradation
        # (out-of-registry finding_kind / subject_key). Reset per module in
        # review_module_with_telemetry; surfaced in coverage_report.json.
        self._invalid_kind_counter: list[int] = [0]
        self._chunker = Chunker(
            max_lines=config.chunk_max_lines,
            overlap_lines=config.chunk_overlap_lines,
        )
        retry_max = min(config.chunk_max_lines, _PAYLOAD_RETRY_MAX_LINES)
        retry_overlap = min(config.chunk_overlap_lines, _PAYLOAD_RETRY_OVERLAP_LINES)
        if retry_overlap >= retry_max:
            retry_overlap = max(0, retry_max - 1)
        self._payload_retry_chunker = Chunker(
            max_lines=retry_max,
            overlap_lines=retry_overlap,
        )

    def review_module(
        self,
        module: dict[str, Any],
        source_code: str,
        manifest: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """
        Run the full skill sequence for a single module.

        Args:
            module: Module metadata dict from modules.json.
            source_code: Full module source code.
            manifest: Codebase manifest for context.

        Returns:
            List of all findings from this module across all skills applied.
        """
        module_name = module.get("name", module.get("path", "unknown"))
        log.info("Reviewing module: %s", module_name)

        # Reset the per-module repaired-kind tally so counts do not leak from a
        # prior review_module call on the same instance (parallels the reset in
        # review_module_with_telemetry).
        self._invalid_kind_counter = [0]
        all_findings: list[dict[str, Any]] = []
        chunks = self._chunker.chunk_string(source_code, file_path=module_name)

        # ── Chunk count cap ───────────────────────────────────────────
        cap = self._config.max_chunks_per_module
        if not self._strict_mode and len(chunks) > cap:
            log.warning(
                "Module %s has %d chunks, capping at %d",
                module_name, len(chunks), cap,
            )
            chunks = chunks[:cap]

        # ── Layer 1: Prioritization ───────────────────────────────────
        prioritizer_context = build_review_prioritizer_context(
            module,
            manifest,
            strict_mode=self._strict_mode,
        )
        try:
            priority_result = self._architect.prioritize_review(prioritizer_context)
        except Exception as exc:
            log.warning("Layer 1 prioritization failed for %s: %s", module_name, exc)
            priority_result = {"recommended_skills": [], "criticality": "medium"}

        recommended_skills: list[str] = priority_result.get("recommended_skills", [])
        log.debug(
            "Module %s: criticality=%s, recommended_skills=%s",
            module_name,
            priority_result.get("criticality"),
            recommended_skills,
        )

        # ── Layer 2a: Quick Review (mandatory, runs on all chunks) ────
        quick_findings: list[dict[str, Any]] = []
        quick_review_failed_chunks = 0
        for chunk in chunks:
            context = build_module_review_context(
                module=module,
                source_code=chunk.content_with_header,
                skill_name=Skills.QUICK_REVIEW,
                prior_findings=None,
                strict_mode=self._strict_mode,
            )
            try:
                result = self._architect.quick_review(context)
                chunk_findings = _extract_findings(
                    result,
                    module_name,
                    Skills.QUICK_REVIEW,
                    override_file=module.get("path", ""),
                    repo_root_canonical=self._repo_root_canonical,
                    invalid_kind_counter=self._invalid_kind_counter,
                )
                quick_findings.extend(chunk_findings)
                log.debug(
                    "Quick Review chunk %d/%d: %d finding(s)",
                    chunk.chunk_index + 1,
                    chunk.total_chunks,
                    len(chunk_findings),
                )
            except FindingSchemaValidationError as exc:
                log.error(
                    "Finding schema validation failed on %s skill %s chunk %d: %s",
                    module_name,
                    Skills.QUICK_REVIEW,
                    chunk.chunk_index,
                    exc,
                )
                raise
            except Exception as exc:
                # Quick Review is mandatory. A failed chunk means this module was
                # not fully reviewed, so mark it as a failed chunk rather than
                # letting the module fall through to specialists and present a
                # false "reviewed" state (F2-44f14e6f2195).
                quick_review_failed_chunks += 1
                log.warning(
                    "Quick Review failed on %s chunk %d: %s",
                    module_name,
                    chunk.chunk_index,
                    exc,
                )

        all_findings.extend(quick_findings)

        # Mandatory-review guard: when any Quick Review chunk failed, the module
        # is only partially reviewed. Do NOT run specialists on top of an
        # incomplete Quick Review pass — that would record specialist findings
        # against a module the mandatory layer never fully covered
        # (F2-44f14e6f2195). Persist whatever Quick Review findings we did get
        # and return early.
        if quick_review_failed_chunks:
            log.warning(
                "Module %s: %d Quick Review chunk(s) failed — skipping "
                "specialists (module not fully reviewed)",
                module_name,
                quick_review_failed_chunks,
            )
            raw_count = len(all_findings)
            all_findings = deduplicate(all_findings)
            log.info("Dedup: %d findings → %d after dedup", raw_count, len(all_findings))
            for finding in all_findings:
                self._store.append_finding(finding)
            return all_findings

        # ── Determine which specialists to run ────────────────────────
        specialists_to_run = set(
            _determine_specialists(
                findings=quick_findings,
                module_content=source_code,
                recommended_skills=recommended_skills,
                module=module,
                enforce_full_specialist_coverage=getattr(
                    self._config,
                    "enforce_full_specialist_coverage",
                    False,
                ),
                review_profile=self._resolved_review_profile(),
                enable_alarm_specialist=getattr(
                    self._config, "enable_alarm_specialist", True),
            )
        )

        log.info(
            "Module %s: running %d specialist skill(s): %s",
            module_name,
            len(specialists_to_run),
            sorted(specialists_to_run),
        )

        # ── Run specialist skills ─────────────────────────────────────
        for skill in _ordered_skills(specialists_to_run):
            specialist_findings = self._run_specialist(
                skill=skill,
                module=module,
                source_code=source_code,
                chunks=chunks,
                prior_findings=quick_findings,
            )
            all_findings.extend(specialist_findings)

        # ── Deduplicate before persisting ─────────────────────────────
        raw_count = len(all_findings)
        all_findings = deduplicate(all_findings)
        log.info(
            "Dedup: %d findings → %d after dedup",
            raw_count,
            len(all_findings),
        )

        # ── Persist findings to state store ───────────────────────────
        for finding in all_findings:
            self._store.append_finding(finding)

        log.info(
            "Module %s: %d total finding(s) across %d skill(s)",
            module_name,
            len(all_findings),
            1 + len(specialists_to_run),
        )
        return all_findings

    def review_module_with_telemetry(
        self,
        module: dict[str, Any],
        source_code: str,
        manifest: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
        """Run module review and return findings plus per-chunk telemetry."""
        module_name = module.get("name", module.get("path", "unknown"))
        log.info("Reviewing module: %s", module_name)

        self._invalid_kind_counter = [0]
        all_findings: list[dict[str, Any]] = []
        failed_chunks: list[dict[str, Any]] = []
        chunks = self._chunker.chunk_string(source_code, file_path=module_name)
        original_chunk_count = len(chunks)

        # ── Chunk count cap ───────────────────────────────────────────
        cap = self._config.max_chunks_per_module
        if not self._strict_mode and len(chunks) > cap:
            log.warning(
                "Module %s has %d chunks, capping at %d",
                module_name,
                len(chunks),
                cap,
            )
            chunks = chunks[:cap]

        # ── Layer 1: Prioritization ───────────────────────────────────
        prioritizer_context = build_review_prioritizer_context(
            module,
            manifest,
            strict_mode=self._strict_mode,
        )
        try:
            priority_result = self._architect.prioritize_review(prioritizer_context)
        except Exception as exc:
            log.warning("Layer 1 prioritization failed for %s: %s", module_name, exc)
            priority_result = {"recommended_skills": [], "criticality": "medium"}

        recommended_skills: list[str] = priority_result.get("recommended_skills", [])
        log.debug(
            "Module %s: criticality=%s, recommended_skills=%s",
            module_name,
            priority_result.get("criticality"),
            recommended_skills,
        )

        # ── Layer 2a: Quick Review (mandatory, runs on all chunks) ────
        quick_findings: list[dict[str, Any]] = []
        quick_failed_chunks = 0
        for chunk in chunks:
            context = build_module_review_context(
                module=module,
                source_code=chunk.content_with_header,
                skill_name=Skills.QUICK_REVIEW,
                prior_findings=None,
                strict_mode=self._strict_mode,
            )
            try:
                result = self._architect.quick_review(context)
                chunk_findings = _extract_findings(
                    result,
                    module_name,
                    Skills.QUICK_REVIEW,
                    override_file=module.get("path", ""),
                    repo_root_canonical=self._repo_root_canonical,
                    invalid_kind_counter=self._invalid_kind_counter,
                )
                quick_findings.extend(chunk_findings)
                log.debug(
                    "Quick Review chunk %d/%d: %d finding(s)",
                    chunk.chunk_index + 1,
                    chunk.total_chunks,
                    len(chunk_findings),
                )
            except FindingSchemaValidationError as exc:
                log.error(
                    "Finding schema validation failed on %s skill %s chunk %d: %s",
                    module_name,
                    Skills.QUICK_REVIEW,
                    chunk.chunk_index,
                    exc,
                )
                raise
            except Exception as exc:
                if _should_retry_with_compact_subchunks(exc):
                    retry_result = self._retry_chunk_for_payload(
                        module=module,
                        module_name=module_name,
                        skill=Skills.QUICK_REVIEW,
                        chunk=chunk,
                        prior_findings=None,
                        runner=lambda ctx: self._architect.quick_review(ctx),
                    )
                    if retry_result.status in ("complete", "partial"):
                        quick_findings.extend(retry_result.findings)
                    if retry_result.status == "complete":
                        continue
                quick_failed_chunks += 1
                failed_chunks.append(
                    {
                        "module": module_name,
                        "module_path": module.get("path", ""),
                        "skill": Skills.QUICK_REVIEW,
                        "chunk_index": chunk.chunk_index,
                        "total_chunks": len(chunks),
                        "start_line": chunk.start_line,
                        "end_line": chunk.end_line,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
                log.warning(
                    "Quick Review failed on %s chunk %d: %s",
                    module_name,
                    chunk.chunk_index,
                    exc,
                )

        all_findings.extend(quick_findings)

        # ── Determine which specialists to run ────────────────────────
        ordered_specialists = _determine_specialists(
            findings=quick_findings,
            module_content=source_code,
            recommended_skills=recommended_skills,
            module=module,
            enforce_full_specialist_coverage=getattr(
                self._config,
                "enforce_full_specialist_coverage",
                False,
            ),
            review_profile=self._resolved_review_profile(),
            enable_alarm_specialist=getattr(
                self._config, "enable_alarm_specialist", True),
        )
        log.info(
            "Module %s: running %d specialist skill(s): %s",
            module_name,
            len(ordered_specialists),
            ordered_specialists,
        )

        specialist_failed_chunks = 0
        specialist_attempts: dict[str, int] = {}
        specialist_failures: dict[str, int] = {}
        for skill in ordered_specialists:
            specialist_findings, skill_attempted, skill_failed, skill_failures = self._run_specialist_with_telemetry(
                skill=skill,
                module=module,
                chunks=chunks,
                prior_findings=quick_findings,
            )
            specialist_attempts[skill] = skill_attempted
            specialist_failures[skill] = skill_failed
            specialist_failed_chunks += skill_failed
            failed_chunks.extend(skill_failures)
            all_findings.extend(specialist_findings)

        raw_count = len(all_findings)
        all_findings = deduplicate(all_findings)
        log.info(
            "Dedup: %d findings → %d after dedup",
            raw_count,
            len(all_findings),
        )

        for finding in all_findings:
            self._store.append_finding(finding)

        partial_coverage = original_chunk_count > len(chunks)
        status = {
            "module": module_name,
            "module_path": module.get("path", ""),
            # Report the pre-cap total so downstream coverage accounting sees the
            # true chunk count, not the truncated one (F2-525566f4ab62). The
            # number actually reviewed is chunks_total - chunks_capped.
            "chunks_total": original_chunk_count,
            "chunks_reviewed": len(chunks),
            "chunks_capped": max(0, original_chunk_count - len(chunks)),
            "partial_coverage": partial_coverage,
            "quick_review_failed_chunks": quick_failed_chunks,
            "specialist_failed_chunks": specialist_failed_chunks,
            "findings_count": len(all_findings),
            "skills_run": [Skills.QUICK_REVIEW] + ordered_specialists,
            "specialist_attempted_chunks": specialist_attempts,
            "specialist_failed_chunks_by_skill": specialist_failures,
            "review_complete": (quick_failed_chunks + specialist_failed_chunks) == 0 and not partial_coverage,
            "repaired_finding_kinds": self._invalid_kind_counter[0],
        }

        log.info(
            "Module %s: %d total finding(s) across %d skill(s)",
            module_name,
            len(all_findings),
            len(status["skills_run"]),
        )
        return all_findings, status, failed_chunks

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolved_review_profile(self) -> str:
        """Read and validate the configured review profile.

        Validates the value against ``_SUPPORTED_REVIEW_PROFILES`` at the call
        site (F2-3978b74637b9 / F2-5875c0e23ed8): an unsupported value (typo,
        stale config) is logged at ERROR and falls back to ``"triggered"`` so
        the misconfiguration is operator-visible rather than silently accepted.
        """
        profile = getattr(self._config, "review_profile", "triggered")
        if profile not in _SUPPORTED_REVIEW_PROFILES:
            log.error(
                "Unsupported review_profile %r; falling back to 'triggered'. "
                "Supported profiles: %s",
                profile,
                sorted(_SUPPORTED_REVIEW_PROFILES),
            )
            return "triggered"
        return profile

    def _run_specialist(
        self,
        skill: str,
        module: dict[str, Any],
        source_code: str,
        chunks: list,
        prior_findings: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Run a single specialist skill across all chunks."""
        module_name = module.get("name", "unknown")
        findings: list[dict[str, Any]] = []

        for chunk in chunks:
            context = build_module_review_context(
                module=module,
                source_code=chunk.content_with_header,
                skill_name=skill,
                prior_findings=(
                    prior_findings
                    if self._strict_mode
                    else prior_findings[:10]
                ),
                strict_mode=self._strict_mode,
            )
            try:
                result = self._architect.specialist_review(skill, context)
                chunk_findings = _extract_findings(
                    result,
                    module_name,
                    skill,
                    override_file=module.get("path", ""),
                    repo_root_canonical=self._repo_root_canonical,
                    invalid_kind_counter=self._invalid_kind_counter,
                )
                findings.extend(chunk_findings)
            except FindingSchemaValidationError as exc:
                log.error(
                    "Finding schema validation failed on %s skill %s chunk %d: %s",
                    module_name,
                    skill,
                    chunk.chunk_index,
                    exc,
                )
                raise
            except Exception as exc:
                if _is_non_json_agent_error(exc):
                    log.warning(
                        "Specialist %s returned non-JSON on %s chunk %d; continuing with empty findings fallback",
                        skill,
                        module_name,
                        chunk.chunk_index,
                    )
                    continue
                log.warning(
                    "Specialist %s failed on %s chunk %d: %s",
                    skill,
                    module_name,
                    chunk.chunk_index,
                    exc,
                )

        return findings

    def _run_specialist_with_telemetry(
        self,
        skill: str,
        module: dict[str, Any],
        chunks: list,
        prior_findings: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], int, int, list[dict[str, Any]]]:
        """Run a specialist and return findings with per-chunk failure details."""
        module_name = module.get("name", "unknown")
        findings: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        attempted_chunks = 0
        failed_chunks = 0

        for chunk in chunks:
            attempted_chunks += 1
            context = build_module_review_context(
                module=module,
                source_code=chunk.content_with_header,
                skill_name=skill,
                prior_findings=(
                    prior_findings
                    if self._strict_mode
                    else prior_findings[:10]
                ),
                strict_mode=self._strict_mode,
            )
            try:
                result = self._architect.specialist_review(skill, context)
                chunk_findings = _extract_findings(
                    result,
                    module_name,
                    skill,
                    override_file=module.get("path", ""),
                    repo_root_canonical=self._repo_root_canonical,
                    invalid_kind_counter=self._invalid_kind_counter,
                )
                findings.extend(chunk_findings)
            except FindingSchemaValidationError as exc:
                log.error(
                    "Finding schema validation failed on %s skill %s chunk %d: %s",
                    module_name,
                    skill,
                    chunk.chunk_index,
                    exc,
                )
                raise
            except Exception as exc:
                initial_exc = exc
                retry_metadata: dict[str, Any] | None = None
                if _should_retry_with_compact_subchunks(exc):
                    retry_metadata = {
                        "initial_attempt_failed": True,
                        "initial_error_type": type(initial_exc).__name__,
                        "initial_error": str(initial_exc),
                        "retry_attempted": True,
                    }
                    try:
                        retry_result = self._retry_chunk_for_payload(
                            module=module,
                            module_name=module_name,
                            skill=skill,
                            chunk=chunk,
                            prior_findings=prior_findings,
                            runner=lambda ctx, s=skill: self._architect.specialist_review(s, ctx),
                        )
                    except Exception as retry_exc:
                        failed_chunks += 1
                        failures.append(
                            {
                                "module": module_name,
                                "module_path": module.get("path", ""),
                                "skill": skill,
                                "chunk_index": chunk.chunk_index,
                                "total_chunks": len(chunks),
                                "start_line": chunk.start_line,
                                "end_line": chunk.end_line,
                                "error_type": type(retry_exc).__name__,
                                "error": str(retry_exc),
                                "failure_metadata": {
                                    **retry_metadata,
                                    "retry_failed": True,
                                    "retry_error_type": type(retry_exc).__name__,
                                    "retry_error": str(retry_exc),
                                    "terminal_outcome": "retry_failure",
                                    "terminal_error_type": type(retry_exc).__name__,
                                    "terminal_error": str(retry_exc),
                                },
                            }
                        )
                        log.warning(
                            "Specialist %s retry failed on %s chunk %d: %s",
                            skill,
                            module_name,
                            chunk.chunk_index,
                            retry_exc,
                        )
                        continue
                    if retry_result.status in ("complete", "partial"):
                        findings.extend(retry_result.findings)
                    if retry_result.status == "complete":
                        continue
                    exc = initial_exc
                    retry_metadata = {
                        **retry_metadata,
                        "retry_failed": True,
                        "terminal_outcome": "initial_failure_with_partial_retry_recovery"
                        if retry_result.status == "partial"
                        else "initial_failure",
                        "terminal_error_type": type(initial_exc).__name__,
                        "terminal_error": str(initial_exc),
                    }
                if _is_non_json_agent_error(exc):
                    # A non-JSON agent response is a genuine coverage loss for
                    # this chunk — record it in failed_chunks/failures so the
                    # scorecard reflects the gap instead of silently swallowing
                    # it (F2-79661ada17ab / F2-d54778f44d63 / F2-a29a986d4019 /
                    # F2-ec040bf5726d).
                    log.warning(
                        "Specialist %s returned non-JSON on %s chunk %d; "
                        "recording as failed chunk (empty findings)",
                        skill,
                        module_name,
                        chunk.chunk_index,
                    )
                    failed_chunks += 1
                    non_json_failure = {
                        "module": module_name,
                        "module_path": module.get("path", ""),
                        "skill": skill,
                        "chunk_index": chunk.chunk_index,
                        "total_chunks": len(chunks),
                        "start_line": chunk.start_line,
                        "end_line": chunk.end_line,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                    if retry_metadata is not None:
                        non_json_failure["failure_metadata"] = retry_metadata
                    failures.append(non_json_failure)
                    continue
                failed_chunks += 1
                failure = {
                    "module": module_name,
                    "module_path": module.get("path", ""),
                    "skill": skill,
                    "chunk_index": chunk.chunk_index,
                    "total_chunks": len(chunks),
                    "start_line": chunk.start_line,
                    "end_line": chunk.end_line,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                }
                if retry_metadata is not None:
                    failure["failure_metadata"] = retry_metadata
                failures.append(failure)
                log.warning(
                    "Specialist %s failed on %s chunk %d: %s",
                    skill,
                    module_name,
                    chunk.chunk_index,
                    exc,
                )

        return findings, attempted_chunks, failed_chunks, failures

    def _retry_chunk_for_payload(
        self,
        module: dict[str, Any],
        module_name: str,
        skill: str,
        chunk: Chunk,
        prior_findings: list[dict[str, Any]] | None,
        runner,
    ) -> _PayloadRetryResult:
        """Retry an oversized chunk as smaller subchunks."""
        retry_chunks = self._payload_retry_chunker.chunk_string(
            chunk.content,
            file_path=f"{module_name}::{skill}::payload-retry",
        )[:_PAYLOAD_RETRY_MAX_SUBCHUNKS]
        if not retry_chunks:
            return _PayloadRetryResult("failed", [])

        recovered: list[dict[str, Any]] = []
        had_failure = False
        for retry_chunk in retry_chunks:
            context = self._build_payload_retry_context(
                module=module,
                source_code=retry_chunk.content_with_header,
                skill_name=skill,
            )
            try:
                result = self._run_with_rate_limit_retry(
                    runner=runner,
                    context=context,
                    module_name=module_name,
                    skill=skill,
                    phase="line-payload-retry",
                )
                recovered.extend(
                    _extract_findings(
                        result,
                        module_name,
                        skill,
                        override_file=module.get("path", ""),
                        repo_root_canonical=self._repo_root_canonical,
                        invalid_kind_counter=self._invalid_kind_counter,
                    )
                )
            except FindingSchemaValidationError as retry_exc:
                log.error(
                    "Finding schema validation failed during payload retry on %s skill %s chunk %d subchunk %d: %s",
                    module_name,
                    skill,
                    chunk.chunk_index,
                    retry_chunk.chunk_index,
                    retry_exc,
                )
                raise
            except Exception as retry_exc:
                if _is_payload_too_large_error(retry_exc):
                    char_result = self._retry_chunk_for_payload_chars(
                        module=module,
                        module_name=module_name,
                        skill=skill,
                        source_code=retry_chunk.content,
                        runner=runner,
                    )
                    if char_result.status == "complete":
                        recovered.extend(char_result.findings)
                        continue
                    elif char_result.status == "partial":
                        recovered.extend(char_result.findings)
                        # partial char recovery = degraded outer recovery;
                        # keep processing remaining subchunks (order-invariant).
                        had_failure = True
                        continue
                    # else: char recovery failed, fall through to warning
                log.warning(
                    "Payload fallback failed on %s skill %s chunk %d: %s",
                    module_name,
                    skill,
                    chunk.chunk_index,
                    retry_exc,
                )
                had_failure = True
                continue

        log.info(
            "Recovered payload-limited chunk for %s skill %s via %d subchunk(s)",
            module_name,
            skill,
            len(retry_chunks),
        )
        if had_failure:
            return _PayloadRetryResult(
                "partial" if recovered else "failed",
                recovered,
            )
        return _PayloadRetryResult("complete", recovered)

    def _retry_chunk_for_payload_chars(
        self,
        module: dict[str, Any],
        module_name: str,
        skill: str,
        source_code: str,
        runner,
    ) -> _PayloadRetryResult:
        """Retry a payload-limited chunk using character-size slicing."""
        segments = _split_by_chars(
            source_code,
            max_chars=_PAYLOAD_RETRY_MAX_CHARS,
            overlap_chars=_PAYLOAD_RETRY_CHAR_OVERLAP,
            max_parts=_PAYLOAD_RETRY_MAX_CHAR_PARTS,
        )
        if not segments:
            return _PayloadRetryResult("failed", [])

        recovered: list[dict[str, Any]] = []
        had_failure = False
        for index, segment in enumerate(segments):
            context = self._build_payload_retry_context(
                module=module,
                source_code=f"# payload-char-retry {index + 1}/{len(segments)}\n{segment}",
                skill_name=skill,
            )
            try:
                result = self._run_with_rate_limit_retry(
                    runner=runner,
                    context=context,
                    module_name=module_name,
                    skill=skill,
                    phase="char-payload-retry",
                )
                recovered.extend(
                    _extract_findings(
                        result,
                        module_name,
                        skill,
                        override_file=module.get("path", ""),
                        repo_root_canonical=self._repo_root_canonical,
                        invalid_kind_counter=self._invalid_kind_counter,
                    )
                )
            except FindingSchemaValidationError as exc:
                log.error(
                    "Finding schema validation failed during char payload retry on %s skill %s segment %d/%d: %s",
                    module_name,
                    skill,
                    index + 1,
                    len(segments),
                    exc,
                )
                raise
            except Exception as exc:
                log.warning(
                    "Char payload retry failed on %s skill %s segment %d/%d: %s",
                    module_name,
                    skill,
                    index + 1,
                    len(segments),
                    exc,
                )
                had_failure = True
                continue

        log.info(
            "Recovered payload-limited chunk for %s skill %s via %d char segment(s)",
            module_name,
            skill,
            len(segments),
        )
        if had_failure:
            return _PayloadRetryResult(
                "partial" if recovered else "failed",
                recovered,
            )
        return _PayloadRetryResult("complete", recovered)

    def _build_payload_retry_context(
        self,
        module: dict[str, Any],
        source_code: str,
        skill_name: str,
    ) -> str:
        """Build a minimized context for payload retry attempts."""
        # Retry contexts must stay provider-safe after a 413. Use compact
        # metadata to avoid repeatedly sending oversized module file lists.
        return build_module_review_context(
            module=module,
            source_code=source_code,
            skill_name=skill_name,
            prior_findings=None,
            strict_mode=False,
        )

    def _run_with_rate_limit_retry(
        self,
        runner,
        context: str,
        module_name: str,
        skill: str,
        phase: str,
    ) -> dict[str, Any]:
        """Retry payload fallback calls when transient 429 responses occur."""
        last_exc: Exception | None = None
        for attempt in range(1, _PAYLOAD_RETRY_RATE_LIMIT_ATTEMPTS + 1):
            try:
                return runner(context)
            except Exception as exc:
                last_exc = exc
                if _is_rate_limited_error(exc) and attempt < _PAYLOAD_RETRY_RATE_LIMIT_ATTEMPTS:
                    # Exponential backoff (capped) before retrying so we respect
                    # the gateway's rate window instead of hammering it
                    # immediately (F2-186e85c44280).
                    backoff = min(2 ** attempt, _RATE_LIMIT_BACKOFF_CAP_SECONDS)
                    log.warning(
                        "Rate limited during %s on %s skill %s (attempt %d/%d); "
                        "backing off %ds before retry",
                        phase,
                        module_name,
                        skill,
                        attempt,
                        _PAYLOAD_RETRY_RATE_LIMIT_ATTEMPTS,
                        backoff,
                    )
                    time.sleep(backoff)
                    continue
                raise

        # Defensive fallback: loop either returns or raises above.
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("unexpected empty retry state")


def _extract_findings(
    result: dict[str, Any],
    module_name: str,
    skill_name: str,
    override_file: str = "",
    repo_root_canonical: str = "",
    invalid_kind_counter: list[int] | None = None,
) -> list[dict[str, Any]]:
    """
    Extract a normalised findings list from an agent result dict.

    Handles various response shapes the model might return.
    """
    if "findings" in result:
        findings_raw = result["findings"]
    elif "issues" in result:
        findings_raw = result["issues"]
    elif "results" in result:
        findings_raw = result["results"]
    else:
        findings_raw = []

    if not isinstance(findings_raw, list):
        # Some skills return a single findings object
        findings_raw = (
            [findings_raw]
            if findings_raw is not None
            else []
        )

    normalised = []
    for f in findings_raw:
        if not isinstance(f, dict):
            continue
        description = f.get("description", f.get("issue", f.get("message", str(f))))

        # ── Blob flattening: detect severity-dict descriptions ────
        if is_blob_finding(str(description)):
            normalised.extend(
                flatten_blob(
                    str(description),
                    module_name,
                    skill_name,
                    repo_root_canonical=repo_root_canonical,
                )
            )
            continue

        severity = f.get("severity", f.get("level", "P3"))
        location = f.get("location", f.get("file", f.get("line", "")))
        suggested_fix = f.get("suggested_fix", f.get("recommendation", f.get("fix", "")))
        location_str = str(location)

        normalised_finding = {
            "module": module_name,
            "skill": skill_name,
            "severity": severity,
            "category": f.get("category", _classify_category(str(description))),
            "location": location,
            "file": f.get("file", _extract_file_path(location_str)),
            "function": f.get("function", _extract_function_name(location_str)),
            "line_range": f.get("line_range", _extract_line_range(location_str)),
            "description": description,
            "suggested_fix": suggested_fix,
            "status": "open",
        }
        if override_file:
            normalised_finding["file"] = override_file
        for identity_field in (
            "summary",
            "finding_kind",
            "subject_key",
            "normalized_location",
            "legacy_finding_id",
        ):
            if identity_field in f:
                normalised_finding[identity_field] = f[identity_field]

        try:
            normalised.append(materialize_emitter_finding(
                normalised_finding,
                emitter_name=skill_name,
                repo_root_canonical=repo_root_canonical,
            ))
        except FindingSchemaValidationError:
            # LLM emitted an out-of-contract finding_kind / subject_key.
            # Repair in place rather than aborting the whole assessment;
            # repair_invalid_finding re-raises only if unrepairable, in
            # which case the caller's outer handler is the backstop.
            normalised.append(repair_invalid_finding(
                normalised_finding,
                emitter_name=skill_name,
                repo_root_canonical=repo_root_canonical,
                invalid_kind_counter=invalid_kind_counter,
            ))

    return normalised


def _ordered_skills(skills: set[str]) -> list[str]:
    """
    Return skills in a deterministic execution order.

    Correctness and Security run before Performance, which runs before
    Maintainability, then Code Smell, then Refactoring Advisor last.
    """
    order = [
        Skills.CORRECTNESS, Skills.SECURITY, Skills.DATA_INTEGRITY,
        Skills.PERFORMANCE, Skills.ERROR_HANDLING, Skills.OPERATIONAL_RISK,
        Skills.ALARM_INTEGRITY,
        Skills.CONCURRENCY,
        Skills.TESTABILITY, Skills.MAINTAINABILITY,
        Skills.CODE_SMELL, Skills.REFACTORING_ADVISOR,
    ]
    ordered = [s for s in order if s in skills]
    # Append any skills not in the predefined order
    ordered.extend(s for s in sorted(skills) if s not in order)
    return ordered


def _determine_specialists(
    findings: list[dict],
    module_content: str,
    recommended_skills: list[str] | None = None,
    module: dict | None = None,
    enforce_full_specialist_coverage: bool = False,
    review_profile: str = "triggered",
    enable_alarm_specialist: bool = True,
) -> list[str]:
    """
    Determine which specialist skills to run based on Quick Review findings.

    Pure function extracted from ReviewOrchestrator.review_module() to
    allow unit testing of the trigger heuristic in isolation.

    Args:
        findings: Quick Review findings from the model.
        module_content: Raw source code of the module.
        recommended_skills: Baseline skills from Layer 1 Review Prioritizer.
        module: Module metadata dict (used for language-aware filtering).

    Returns:
        Ordered list of specialist skill names to execute.
    """
    specialists: set[str] = set(recommended_skills or [])

    # Deterministic safety mode: always run the full specialist suite.
    if enforce_full_specialist_coverage:
        specialists.update(_FULL_SPECIALIST_SKILLS)
        if not enable_alarm_specialist:
            specialists.discard(Skills.ALARM_INTEGRITY)
        return _ordered_skills(specialists)

    # Validate the profile against the supported allowlist so a typo or
    # unsupported value is operator-visible instead of silently routing through
    # the default 'triggered' path.
    if review_profile not in _SUPPORTED_REVIEW_PROFILES:
        log.warning(
            "Unsupported review_profile %r; falling back to 'triggered'. "
            "Supported profiles: %s",
            review_profile,
            sorted(_SUPPORTED_REVIEW_PROFILES),
        )
        review_profile = "triggered"

    finding_text = " ".join(
        " ".join(
            str(f.get(field, "") or "").lower()
            for field in _ROUTING_CORPUS_FIELDS
        )
        for f in findings
    )
    if review_profile == "reliability-first":
        # Audit any Layer-1 recommended specialist that the reliability-first
        # allowlist drops, so a silent coverage gap (e.g. allowlist drift) is
        # visible in the logs. Routing output is unchanged.
        excluded = {s for s in specialists if s not in _RELIABILITY_FIRST_ALLOWED}
        if excluded:
            log.info(
                "reliability-first profile excluded Layer-1 recommended "
                "specialist(s) not on the allowlist: %s",
                sorted(excluded),
            )
        specialists = {
            skill for skill in specialists
            if skill in _RELIABILITY_FIRST_ALLOWED
        }
        specialists.update(_RELIABILITY_FIRST_ALWAYS_ON)

        for keyword, skills in _SPECIALIST_TRIGGERS.items():
            if keyword in finding_text:
                specialists.update(
                    skill for skill in skills
                    if skill in _RELIABILITY_FIRST_ALLOWED
                )

        if _needs_security_review(module, module_content):
            specialists.add(Skills.SECURITY)
    else:
        for keyword, skills in _SPECIALIST_TRIGGERS.items():
            if keyword in finding_text:
                specialists.update(skills)

        if any(kw in module_content.lower() for kw in _INPUT_KEYWORDS):
            specialists.add(Skills.SECURITY)

        if Skills.CODE_SMELL in specialists and any(
            f.get("severity") in ("Critical", "P1") for f in findings
        ):
            specialists.add(Skills.REFACTORING_ADVISOR)

    # Suppress concurrency specialist for async-native (JS/TS) modules
    if module and Skills.CONCURRENCY in specialists and _is_async_native_module(module):
        async_only_triggered = any(
            kw in finding_text and Skills.CONCURRENCY in skills
            for kw, skills in _SPECIALIST_TRIGGERS.items()
            if kw not in _ASYNC_ONLY_TRIGGERS
        )
        if not async_only_triggered:
            specialists.discard(Skills.CONCURRENCY)
            log.info(
                "Module %s: skipping concurrency specialist — "
                "async-native module (JS/TS)",
                module.get("name", "unknown"),
            )

    if not enable_alarm_specialist:
        specialists.discard(Skills.ALARM_INTEGRITY)
    return _ordered_skills(specialists)


def _is_payload_too_large_error(exc: Exception) -> bool:
    """Heuristic detector for HTTP 413 / payload-too-large errors."""
    text = str(exc).lower()
    if "413" in text or "payload too large" in text or "status/413" in text:
        return True

    # Some gateways normalize oversized requests to HTTP 400 instead of 413.
    if "400 bad request" in text or "status/400" in text:
        payload_markers = (
            "request too large",
            "content too large",
            "context length",
            "maximum context",
            "max context",
            "token limit",
            "too many tokens",
            "payload",
        )
        return any(marker in text for marker in payload_markers) or "http error: client error '400 bad request'" in text

    return False


def _is_transient_gateway_error(exc: Exception) -> bool:
    """Heuristic detector for retry-exhausted upstream gateway/server failures."""
    text = str(exc).lower()
    if "504" in text or "gateway time-out" in text or "gateway timeout" in text:
        return True
    return (
        "server error 502" in text
        or "server error 503" in text
        or "server error 504" in text
        or "upstream" in text and "timeout" in text
    )


def _should_retry_with_compact_subchunks(exc: Exception) -> bool:
    """Decide when to degrade a chunk into smaller slices instead of failing it outright."""
    return (
        _is_payload_too_large_error(exc)
        or _is_transient_gateway_error(exc)
        or _is_non_json_agent_error(exc)
    )


def _is_rate_limited_error(exc: Exception) -> bool:
    """Heuristic detector for HTTP 429 / rate-limit errors."""
    text = str(exc).lower()
    return "429" in text or "too many requests" in text or "rate limited" in text


def _is_non_json_agent_error(exc: Exception) -> bool:
    """Heuristic detector for agent responses that failed JSON parsing."""
    text = str(exc).lower()
    return "returned non-json" in text or "not valid json" in text


def _split_by_chars(
    text: str,
    max_chars: int,
    overlap_chars: int,
    max_parts: int,
) -> list[str]:
    """Split text by character window with overlap and a hard part cap.

    Raises:
        ValueError: If ``text`` is non-empty and ``max_parts`` is not positive.
    """
    if not text:
        return []
    if max_parts <= 0:
        raise ValueError("max_parts must be positive for non-empty input")
    if max_chars <= 0:
        return [text]
    if overlap_chars >= max_chars:
        overlap_chars = max(0, max_chars - 1)

    parts: list[str] = []
    start = 0
    text_len = len(text)
    covered = False
    while start < text_len and len(parts) < max_parts:
        end = min(text_len, start + max_chars)
        parts.append(text[start:end])
        if end >= text_len:
            covered = True
            break
        start = end - overlap_chars

    # If the part cap was hit before the whole text was windowed, the remaining
    # suffix would otherwise be silently dropped. Append it as a final part
    # (even if it exceeds max_chars) so no input content is lost.
    if not covered and start < text_len:
        parts.append(text[start:])

    return parts
