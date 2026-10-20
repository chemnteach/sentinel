"""
sentinel/orchestrator/context.py

Context assembly for Sentinel agent calls.

The Orchestrator reconstructs full context from JSON state files before
every agent invocation. This module contains the builders that compose
the user_message payload injected into each agent call.

Context builders are pure functions — they take state data and return
formatted strings. They do not call the API or write state.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

from sentinel.skills.loader import SkillLoader, SkillNotFoundError

log = logging.getLogger(__name__)

_MODULE_FILES_SAMPLE_LIMIT = 40

# Per-pattern char cap for the ### Remediation Patterns and
# ### Registry Blockers blocks rendered by build_fix_planner_context.
# Tier 1 truncation: shorten the pattern's longest string field and
# append _TRUNCATION_MARKER. Tier 2 fallback: raw JSON truncation
# with an out-of-band HTML comment marker.
_PATTERN_CAP_CHARS = 1500
_TRUNCATION_MARKER = " ...[truncated]"


def _truncate_with_marker(text: str, limit: int) -> str:
    """Truncate ``text`` to ``limit`` chars, appending a visible marker.

    Several context builders slice JSON blobs to keep prompts within payload
    limits. A raw slice gives the model no indication the data was cut, so it
    may reason over a silently-truncated blob as if it were complete
    (F2-2ddad949be6c / F2-d5e578d7b76c / F2-379704fa3020 / F2-5b2cbe5f9306 /
    F2-7dcdc0348f3d). Appending an explicit marker makes the elision visible.
    """
    if len(text) <= limit:
        return text
    omitted = len(text) - limit
    return text[:limit] + f"\n# [TRUNCATED — {omitted} chars omitted of {len(text)} total]"


def build_cartographer_context(
    repo_paths: list[str],
    file_tree: str,
    sample_files: dict[str, str] | None = None,
    strict_mode: bool = False,
) -> str:
    """
    Build context for the Codebase Cartographer (Layer 0a).

    Args:
        repo_paths: Absolute paths to all repositories being assessed.
        file_tree: Output of ``find`` or ``tree`` covering all repos.
        sample_files: Optional dict mapping file path → content snippets.

    Returns:
        Formatted context string for the Cartographer agent call.
    """
    repos_block = "\n".join(f"  - {p}" for p in repo_paths)

    samples_block = ""
    if sample_files:
        lines = []
        sample_items = list(sample_files.items()) if strict_mode else list(sample_files.items())[:20]
        for path, content in sample_items:
            rendered = content if strict_mode else content[:2000]
            lines.append(f"\n### {path}\n```\n{rendered}\n```")
        samples_block = "\n".join(lines)

    return (
        f"## Codebase Assessment Request\n\n"
        f"### Repository Paths\n{repos_block}\n\n"
        f"### File Tree\n```\n{file_tree}\n```\n"
        f"{samples_block}\n\n"
        "Produce a complete structural manifest as JSON. "
        "Follow the Codebase Cartographer skill instructions exactly."
    )


def build_dependency_context(
    manifest: dict[str, Any],
    source_snippets: str,
    strict_mode: bool = False,
) -> str:
    """
    Build context for the Dependency Analyzer (Layer 0b).

    Args:
        manifest: The codebase_manifest dict from Stage 1.
        source_snippets: Import statements and coupling-relevant code excerpts.

    Returns:
        Formatted context string.
    """
    manifest_json = json.dumps(manifest, indent=2)
    snippets_block = source_snippets if strict_mode else _truncate_with_marker(source_snippets, 6000)
    manifest_block = manifest_json if strict_mode else _truncate_with_marker(manifest_json, 8000)
    return (
        "## Dependency Analysis Request\n\n"
        "### Stage 1 Manifest\n"
        f"```json\n{manifest_block}\n```\n\n"
        "### Source Snippets (imports, API boundaries, DB access)\n"
        f"```\n{snippets_block}\n```\n\n"
        "Produce a complete dependency analysis as JSON. "
        "Follow the Dependency Analyzer skill instructions exactly."
    )


def build_review_prioritizer_context(
    module: dict[str, Any],
    manifest: dict[str, Any],
    strict_mode: bool = False,
) -> str:
    """
    Build context for Layer 1 (Review Prioritizer).

    Args:
        module: Single module entry from modules.json.
        manifest: Full codebase manifest for cross-module context.

    Returns:
        Formatted context string.
    """
    module_for_context = _compact_module_metadata(module, strict_mode=strict_mode)
    manifest_json = json.dumps(manifest, indent=2)
    return (
        "## Module Classification Request\n\n"
        "### Module\n"
        f"```json\n{json.dumps(module_for_context, indent=2)}\n```\n\n"
        "### Codebase Context\n"
        f"```json\n{manifest_json if strict_mode else _truncate_with_marker(manifest_json, 4000)}\n```\n\n"
        "Classify this module and return the recommended skill sequence as JSON. "
        "Follow the Review Prioritizer skill instructions exactly."
    )


def build_module_review_context(
    module: dict[str, Any],
    source_code: str,
    skill_name: str,
    prior_findings: list[dict[str, Any]] | None = None,
    strict_mode: bool = False,
) -> str:
    """
    Build context for a Layer 2–4 module review.

    Args:
        module: Module metadata dict.
        source_code: Full module source (chunked if necessary).
        skill_name: The skill being applied (for context header).
        prior_findings: Any findings from previous skills on this module.

    Returns:
        Formatted context string.
    """
    module_for_context = _compact_module_metadata(module, strict_mode=strict_mode)
    findings_block = ""
    if prior_findings:
        findings_json = json.dumps(prior_findings, indent=2)
        findings_block = (
            "\n### Prior Findings on This Module\n"
            f"```json\n{findings_json if strict_mode else _truncate_with_marker(findings_json, 3000)}\n```\n"
        )

    return (
        f"## Module Review — {module.get('name', 'unknown')} ({skill_name})\n\n"
        f"**Module metadata:**\n"
        f"```json\n{json.dumps(module_for_context, indent=2)}\n```\n\n"
        f"**Source code:**\n```python\n{source_code}\n```\n"
        f"{findings_block}\n"
        "Review this module and return structured findings as JSON. "
        "Follow the active skill instructions exactly."
    )


def _compact_module_metadata(
    module: dict[str, Any],
    strict_mode: bool = False,
) -> dict[str, Any]:
    """Trim oversized module metadata to keep review prompts within payload limits."""
    if strict_mode:
        return dict(module)
    compact = dict(module)
    files = compact.get("files")
    if isinstance(files, list) and len(files) > _MODULE_FILES_SAMPLE_LIMIT:
        compact["file_count"] = len(files)
        compact["files_sample"] = files[:_MODULE_FILES_SAMPLE_LIMIT]
        compact.pop("files", None)
    return compact


def build_fix_planner_context(
    findings: list[dict[str, Any]],
    modules: list[dict[str, Any]],
    dependencies: list[dict[str, Any]],
    patterns: list[dict[str, Any]] | None = None,
    registry_blockers: list[dict[str, Any]] | None = None,
) -> str:
    """
    Build context for Layer 5 (Fix Planner).

    Args:
        findings: All accumulated findings from Phase 1.
        modules: Module inventory.
        dependencies: Dependency graph.
        patterns: Optional Stage 5b remediation patterns (actionable,
            triage-filtered). When non-empty, rendered as a
            ``### Remediation Patterns`` block.
        registry_blockers: Optional ``pattern_kind == "registry_conflict"``
            patterns. When non-empty, rendered as a ``### Registry Blockers``
            block. Exempt from the actionable-pattern triage filter.

    Returns:
        Formatted context string. When both ``patterns`` and
        ``registry_blockers`` are empty/None the output is byte-identical
        to the pre-extension form (backward-compatible default).
    """
    # S4-8 — prioritize by the *effective* severity: Stage 5c verification may
    # store ``recalibrated_severity`` on a finding when its verified severity
    # differs from the originally-assigned ``severity``. The planner must see the
    # corrected value, so we (a) bucket the summary counts by effective severity
    # and (b) serialize each finding with an explicit ``effective_severity`` field
    # (without mutating the caller's findings).
    def _eff_sev(f: dict[str, Any]) -> str:
        return f.get("recalibrated_severity") or f.get("severity") or ""

    findings = [{**f, "effective_severity": _eff_sev(f)} for f in findings]

    critical = [f for f in findings if f["effective_severity"] == "Critical"]
    p1 = [f for f in findings if f["effective_severity"] == "P1"]
    p2 = [f for f in findings if f["effective_severity"] == "P2"]
    p3 = [f for f in findings if f["effective_severity"] == "P3"]

    patterns = patterns or []
    registry_blockers = registry_blockers or []

    patterns_block = ""
    if patterns:
        patterns_block = (
            "### Remediation Patterns\n"
            f"{_render_patterns(patterns)}\n\n"
        )
    blockers_block = ""
    if registry_blockers:
        blockers_block = (
            "### Registry Blockers\n"
            f"{_render_patterns(registry_blockers)}\n\n"
        )

    closing_extension = ""
    if patterns or registry_blockers:
        closing_extension = (
            " Use the source_pattern field on each plan item to record"
            " single-pattern lineage (empty string when the item spans"
            " multiple patterns or no pattern). Emit one FIX-BLOCKING-*"
            " item per Registry Blocker per the skill instructions."
        )

    return (
        "## Remediation Planning Request\n\n"
        f"**Finding summary:** {len(critical)} Critical, {len(p1)} P1, "
        f"{len(p2)} P2, {len(p3)} P3\n\n"
        "### All Findings\n"
        f"```json\n{_truncate_with_marker(json.dumps(findings, indent=2), 10000)}\n```\n\n"
        "### Module Inventory\n"
        f"```json\n{_truncate_with_marker(json.dumps(modules, indent=2), 4000)}\n```\n\n"
        "### Dependency Graph\n"
        f"```json\n{_truncate_with_marker(json.dumps(dependencies, indent=2), 4000)}\n```\n\n"
        f"{patterns_block}"
        f"{blockers_block}"
        "Produce the master remediation plan as JSON — Blocking → Required → "
        "Desired → Nice-to-have, with batching, sequencing, effort estimates, "
        "dependencies, and verification criteria. "
        "Follow the Fix Planner skill instructions exactly."
        f"{closing_extension}"
    )


def _render_patterns(patterns: list[dict[str, Any]]) -> str:
    """Render a list of patterns as joined fenced JSON blocks.

    Each pattern is rendered via ``_render_single_pattern`` and joined
    with a blank line. Separating the join from the per-pattern logic
    keeps the truncation tiers independently testable.
    """
    return "\n\n".join(_render_single_pattern(p) for p in patterns)


def _render_single_pattern(pattern: dict[str, Any]) -> str:
    """Render one pattern as a fenced JSON block, capped at
    ``_PATTERN_CAP_CHARS`` via longest-string-field truncation (Tier 1)
    with raw-JSON truncation fallback (Tier 2).
    """
    serialised = json.dumps(pattern, indent=2)
    if len(serialised) <= _PATTERN_CAP_CHARS:
        return f"```json\n{serialised}\n```"

    # Tier 1: truncate the longest string field (or longest string element
    # inside a list[str] field) and re-serialise.
    truncated = _truncate_longest_string_field(
        pattern,
        target_size=_PATTERN_CAP_CHARS - 50,  # headroom for JSON overhead
    )
    serialised = json.dumps(truncated, indent=2)
    if len(serialised) <= _PATTERN_CAP_CHARS:
        return f"```json\n{serialised}\n```"

    # Tier 2 fallback: pathological structure -- raw truncation with an
    # out-of-band HTML comment marker. JSON parseability is forfeited;
    # the comment flags the elision.
    pattern_id = pattern.get("pattern_id", "<unknown>")
    raw = serialised[: _PATTERN_CAP_CHARS - 100]
    return (
        f"<!-- pattern {pattern_id} truncated at raw JSON level -->\n"
        f"```json\n{raw}\n```"
    )


def _truncate_longest_string_field(
    pattern: dict[str, Any], target_size: int
) -> dict[str, Any]:
    """Return a copy of ``pattern`` with its longest string field
    truncated so the serialised result fits ``target_size``.

    Considers top-level ``str`` fields and ``str`` elements inside
    top-level ``list[str]`` fields (notably ``proposed_changes``,
    ``acceptance_criteria``, ``scope_signals`` in the Stage 5b
    schema). The truncated string carries ``_TRUNCATION_MARKER`` as
    its suffix so the elision is visible to the planner.

    Returns the unchanged copy when no eligible field exists; the
    caller (Tier 2 in ``_render_single_pattern``) handles that case.
    """
    result = dict(pattern)
    overflow = len(json.dumps(result, indent=2)) - target_size
    if overflow <= 0:
        return result

    longest_field: str | None = None
    longest_len = 0
    longest_index: int | None = None
    for key, value in result.items():
        if isinstance(value, str) and len(value) > longest_len:
            longest_field, longest_len, longest_index = key, len(value), None
        elif isinstance(value, list):
            for i, elem in enumerate(value):
                if isinstance(elem, str) and len(elem) > longest_len:
                    longest_field, longest_len, longest_index = key, len(elem), i

    if longest_field is None:
        return result

    if longest_index is None:
        original = result[longest_field]
        keep = max(0, len(original) - overflow - len(_TRUNCATION_MARKER))
        result[longest_field] = original[:keep] + _TRUNCATION_MARKER
    else:
        items = list(result[longest_field])
        original = items[longest_index]
        keep = max(0, len(original) - overflow - len(_TRUNCATION_MARKER))
        items[longest_index] = original[:keep] + _TRUNCATION_MARKER
        result[longest_field] = items

    return result


def build_verification_context(
    task: dict[str, Any],
    diff: str,
    original_findings: list[dict[str, Any]],
    test_results: str,
    tests_pass: bool | None = None,
    test_summary: str = "",
) -> str:
    """
    Build context for Layer 6a (Verification Reviewer).

    Args:
        task: Full task dict including acceptance_criteria and test_stubs.
        diff: The code diff being verified.
        original_findings: The findings this task was supposed to fix.
        test_results: Test runner output (pytest, etc.).
        tests_pass: cycle 57 — the *real* pass/fail verdict from the live test
            runner (``run_tests``), so QA verifies actual results rather than an
            LLM-asserted bool. ``None`` when unknown (e.g. local_only_mode).
        test_summary: cycle 57 — the runner's one-line summary (e.g. pytest's
            "N passed in Xs"), surfaced alongside the full output.

    Returns:
        Formatted context string.
    """
    stubs = task.get("test_stubs", {})

    # cycle 52 — QA verifies effective criteria (base + REQUIRE/ADD_CRITERION
    # conditions threaded onto the task), so owner-set criteria are enforced.
    from sentinel.govern.conditions import get_effective_criteria

    # cycle 57 — surface the live runner's status + summary so QA judges the
    # real test outcome, not the Coder's self-asserted tests_pass.
    status = "UNKNOWN" if tests_pass is None else ("PASS" if tests_pass else "FAIL")

    return (
        f"## Verification Request — Task {task.get('task_id', '?')}\n\n"
        "### Task Context\n"
        f"**Description:** {task.get('description', '')}\n\n"
        "**Acceptance Criteria:**\n"
        + "\n".join(f"  - {c}" for c in get_effective_criteria(task))
        + "\n\n"
        "### Approved Test Stubs\n"
        f"```json\n{_truncate_with_marker(json.dumps(stubs, indent=2), 3000)}\n```\n\n"
        "### Original Findings Being Fixed\n"
        f"```json\n{_truncate_with_marker(json.dumps(original_findings, indent=2), 3000)}\n```\n\n"
        "### Code Diff\n"
        f"```diff\n{_truncate_with_marker(diff, 6000)}\n```\n\n"
        "### Test Results — REAL TEST RESULTS (live runner)\n"
        f"**Status:** {status}\n"
        f"**Summary:** {test_summary}\n"
        f"```\n{_truncate_with_marker(test_results, 2000)}\n```\n\n"
        "Verify this fix completely, including the mandatory TDD section. "
        "Return your verdict as JSON. "
        "Follow the Verification Reviewer skill instructions exactly."
    )


def build_cross_repo_context(
    task: dict[str, Any],
    diff: str,
    affected_repos: list[str],
    interface_snapshots: dict[str, str],
) -> str:
    """
    Build context for Layer 6b (Cross-Repo Verification).

    Args:
        task: Full task dict.
        diff: The code diff.
        affected_repos: List of potentially affected repository names.
        interface_snapshots: Dict mapping repo → relevant interface code.

    Returns:
        Formatted context string.
    """
    interfaces_block = "\n".join(
        f"\n### {repo}\n```\n{code[:2000]}\n```"
        for repo, code in interface_snapshots.items()
    )

    return (
        f"## Cross-Repo Verification — Task {task.get('task_id', '?')}\n\n"
        "### Code Diff\n"
        f"```diff\n{diff[:4000]}\n```\n\n"
        "### Potentially Affected Repositories\n"
        + "\n".join(f"  - {r}" for r in affected_repos)
        + "\n\n"
        "### Interface Snapshots\n"
        f"{interfaces_block}\n\n"
        "Verify this change does not break cross-repo contracts. "
        "Return your verdict as JSON including deployment ordering. "
        "Follow the Cross-Repo Verification skill instructions exactly."
    )


def build_transition_manifest_context(
    task: dict[str, Any],
    diff: str,
    existing_docs: dict[str, str],
) -> str:
    """
    Build context for the Documentation Agent's transition manifest pass
    at the QA_PASS -> DOC_UPDATED gate.

    The Documentation Agent consumes this context via the
    ``layer6c-transition-manifest-generator`` skill, which produces a
    per-task before/after narrative plus the two gate fields
    (``docs_updated``, ``doc_updated``).

    Args:
        task: Full task dict.
        diff: The merged code diff.
        existing_docs: Dict mapping doc file path -> current content.
            Empty dict signals "no .md files in task.affected_files" and
            is rendered as an explicit sentinel rather than an empty
            block, so the skill's three-case gate-field decision tree
            can dispatch correctly.

    Returns:
        Formatted context string.
    """
    if existing_docs:
        docs_block = "\n".join(
            f"\n### {path}\n```markdown\n{content[:2000]}\n```"
            for path, content in existing_docs.items()
        )
    else:
        docs_block = (
            "(no existing docs resolved -- manifest may still proceed)"
        )

    affected_files_block = "\n".join(
        f"  - {f}" for f in task.get("affected_files", [])
    ) or "  (none)"
    acceptance_block = "\n".join(
        f"  {i+1}. {c}"
        for i, c in enumerate(task.get("acceptance_criteria", []))
    ) or "  (none)"

    return (
        f"## Transition Manifest Generation -- Task {task.get('task_id', '?')}\n\n"
        "### Task Record\n"
        f"**task_id:** {task.get('task_id', '?')}\n\n"
        f"**description:** {task.get('description', '')}\n\n"
        "**affected_files:**\n"
        f"{affected_files_block}\n\n"
        "**acceptance_criteria:**\n"
        f"{acceptance_block}\n\n"
        "### Code Diff\n"
        f"```diff\n{diff[:4000]}\n```\n\n"
        "### Existing Documents in Scope\n"
        f"{docs_block}\n\n"
        "Follow the layer6c-transition-manifest-generator skill "
        "instructions exactly. Echo task_id verbatim. Return your "
        "response as JSON."
    )


# ─────────────────────────────────────────────────────────────────────
# Stage 5b — Remediation Pattern Proposer
# ─────────────────────────────────────────────────────────────────────

_PERSONA_CAP_CHARS = 2000
_PERSONA_BLOCK_RE = re.compile(
    r"(?ms)^##\s+Persona\s*\n(.*?)(?=^\n##\s|\Z)"
)
_FRONTMATTER_BLOCK_RE = re.compile(
    r"\A---\s*\n(?P<body>.*?)\n---\s*\n",
    re.DOTALL,
)
_DESCRIPTION_LINE_RE = re.compile(
    r"(?m)^description:\s*(?P<desc>.+)$"
)

_SYNTHETIC_PATTERN_KIND = "registry_conflict"


def _compute_pattern_id(group: dict[str, Any], suffix: str = "") -> str:
    """Stable 12-char pattern identifier.

    Hashes ``group_id + canonical_emitter + suffix``. Empty
    ``canonical_emitter`` (no Table 2 declaration / preserve-both
    pairs) and empty ``suffix`` both hash through verbatim, so the
    LLM-produced and synthetic paths never collide unless they share
    every input.
    """
    group_id = str(group.get("group_id") or "")
    canonical = str(group.get("canonical_emitter") or "")
    payload = f"{group_id}|{canonical}|{suffix}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _serialise_group(group: dict[str, Any], pattern_id: str) -> dict[str, Any]:
    """Project a SemanticGroup dict into the JSON-renderable record
    the Architect sees in its user_message.

    ``member_finding_ids`` is sorted here to make Stage 5b deterministic
    across input permutations: the grouper preserves bucket-encounter
    order, which depends on classification order across the inputs.
    Sorting at the serialisation boundary is a pure consumer-side
    decision and does not change grouper behaviour.
    """
    member_ids = list(group.get("member_finding_ids") or [])
    contributing = list(group.get("contributing_emitters") or [])
    peers = list(group.get("peer_emitters") or [])
    return {
        "pattern_id": pattern_id,
        "group_id": group.get("group_id"),
        "grouping_key": group.get("grouping_key"),
        "mode": group.get("mode"),
        "severity": group.get("severity"),
        "member_count": group.get("member_count"),
        "member_finding_ids": sorted(member_ids),
        "contributing_emitters": sorted(contributing),
        "canonical_emitter": group.get("canonical_emitter") or "",
        "peer_emitters": sorted(peers),
        "subject_key": group.get("subject_key"),
        "finding_kind": group.get("finding_kind"),
        "file": group.get("file"),
        "function": group.get("function"),
        "category": group.get("category"),
    }


def _extract_canonical_persona_block(
    canonical_emitter: str,
    loader: SkillLoader,
) -> str:
    """Return the canonical emitter's ``## Persona`` section, capped at
    ~2000 chars at the last newline boundary inside the cap.

    Degrades gracefully:
    - empty ``canonical_emitter`` → "(no canonical emitter declared)".
    - skill file not found → placeholder + WARN log.
    - no ``## Persona`` heading in the skill body → returns the first
      ``_PERSONA_CAP_CHARS`` of the body, capped at the last newline.
    """
    if not canonical_emitter:
        return "(no canonical emitter declared for this group)"
    try:
        body = loader.load(canonical_emitter)
    except SkillNotFoundError:
        log.warning(
            "Stage 5b: canonical emitter %r has no skill file; "
            "rendering placeholder authority block",
            canonical_emitter,
        )
        return (
            f"(skill file for canonical emitter {canonical_emitter!r} "
            "not found)"
        )

    match = _PERSONA_BLOCK_RE.search(body)
    raw = match.group(1).strip() if match else body
    if len(raw) <= _PERSONA_CAP_CHARS:
        return raw
    truncated = raw[:_PERSONA_CAP_CHARS]
    last_newline = truncated.rfind("\n")
    if last_newline > 0:
        truncated = truncated[:last_newline]
    return truncated.rstrip() + "\n\n[…persona truncated at ~2000 chars]"


def _read_skill_description(emitter: str, skill_files_dir: Path) -> str:
    """Read the front-matter ``description`` line from an emitter's
    skill file. Returns a placeholder on any failure (file missing,
    no front-matter, no description line)."""
    path = skill_files_dir / f"{emitter}.md"
    if not path.exists():
        return "(no skill file)"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning(
            "Stage 5b: failed to read skill file for %r: %s",
            emitter,
            exc,
        )
        return "(skill file unreadable)"
    fm_match = _FRONTMATTER_BLOCK_RE.match(raw)
    if not fm_match:
        return "(no front-matter)"
    desc_match = _DESCRIPTION_LINE_RE.search(fm_match.group("body"))
    if not desc_match:
        return "(no description line)"
    return desc_match.group("desc").strip()


def _build_peer_oneliners_block(
    canonical: str,
    contributing: tuple[str, ...] | list[str],
    skill_files_dir: Path,
) -> str:
    """Render a Markdown list of one-line descriptions for every
    contributing emitter other than the canonical."""
    peers = [e for e in contributing if e and e != canonical]
    if not peers:
        return "(no peer emitters contributed to this group)"
    lines = []
    for peer in sorted(set(peers)):
        desc = _read_skill_description(peer, skill_files_dir)
        lines.append(f"- **{peer}** — {desc}")
    return "\n".join(lines)


def _build_silent_peer_block(
    silent_peers: tuple[str, ...] | list[str],
    finding_kind: str | None,
) -> str:
    """Render guidance for peer emitters registered on this group's
    finding_kind but absent from the group's contributing set.
    Empty input → block-omitted sentinel."""
    if not silent_peers:
        return "(no silent peers on this finding_kind)"
    kind_label = finding_kind or "(unknown finding_kind)"
    lines = [
        f"The following emitters register for finding_kind "
        f"`{kind_label}` but produced no finding in this group. "
        "Treat their silence as a real coverage gap to surface in "
        "`notes.silent_peer_notes`, not as endorsement.\n"
    ]
    for peer in sorted(set(silent_peers)):
        lines.append(f"- **{peer}**")
    return "\n".join(lines)


def build_remediation_pattern_context(
    group: dict[str, Any],
    member_findings: list[dict[str, Any]],
    pattern_id: str,
    loader: SkillLoader,
    skill_files_dir: Path,
) -> str:
    """Assemble the user_message for one Stage 5b
    ``ArchitectAgent.propose_remediation_pattern`` call.

    Layout:
        ## Remediation Pattern Proposal Request — pattern <id>
        ### Group Record (echo pattern_id and group_id verbatim)
        ### Member Findings
        ### Canonical Emitter Persona — <emitter>
        ### Peer Emitters Present
        ### Silent Peers (if any)
        Imperative tail
    """
    serialised = _serialise_group(group, pattern_id)
    canonical = serialised["canonical_emitter"]
    contributing = serialised["contributing_emitters"]
    peers = list(group.get("peer_emitters") or [])
    silent_peers = [e for e in peers if e and e not in contributing]

    persona_block = _extract_canonical_persona_block(canonical, loader)
    peer_block = _build_peer_oneliners_block(
        canonical, contributing, skill_files_dir,
    )
    silent_block = _build_silent_peer_block(
        silent_peers, group.get("finding_kind"),
    )

    persona_heading = (
        f"### Canonical Emitter Persona — {canonical}"
        if canonical
        else "### Canonical Emitter Persona"
    )

    return (
        f"## Remediation Pattern Proposal Request — pattern {pattern_id}\n\n"
        "### Group Record\n"
        "Echo `pattern_id` and `group_id` verbatim in your response.\n\n"
        f"```json\n{json.dumps(serialised, indent=2, sort_keys=True)}\n```\n\n"
        "### Member Findings\n"
        f"```json\n{json.dumps(member_findings, indent=2, sort_keys=True)}\n```\n\n"
        f"{persona_heading}\n"
        f"{persona_block}\n\n"
        "### Peer Emitters Present\n"
        f"{peer_block}\n\n"
        "### Silent Peers\n"
        f"{silent_block}\n\n"
        "Produce one `RemediationPattern` JSON object. "
        "Follow the Remediation Pattern Proposer skill instructions exactly."
    )


def build_synthetic_conflict_pattern(
    group: dict[str, Any],
    exc: Exception,
) -> dict[str, Any]:
    """Orchestrator-produced pattern for the
    ``CanonicalEmitterConflictError`` short-circuit path on Stage 4b.

    Reserved ``pattern_kind`` value ``"registry_conflict"`` signals to
    the downstream Fix Planner that this entry is BLOCKING and that
    the registry — not the codebase — is the locus of the fix.
    """
    pattern_id = _compute_pattern_id(group, suffix=_SYNTHETIC_PATTERN_KIND)
    conflicting = getattr(exc, "conflicting_canonicals", ())
    contributing = getattr(exc, "contributing_emitters", ())
    finding_kind = getattr(exc, "finding_kind", None) or group.get("finding_kind")
    return {
        "pattern_id": pattern_id,
        "group_id": group.get("group_id"),
        "pattern_kind": _SYNTHETIC_PATTERN_KIND,
        "title": (
            f"Registry conflict: {finding_kind!r} has disagreeing "
            f"canonical_emitter declarations across contributing pairs"
        ),
        "rationale": (
            "Stage 4b detected a CanonicalEmitterConflictError while "
            "resolving the canonical emitter for this group. Two or more "
            "Table 2 pair rules involving the group's contributing "
            "emitters declared different canonical_emitter values. The "
            "registry needs reconciliation before remediation patterns "
            "can be proposed for this finding_kind. "
            f"Conflicting canonicals: {list(conflicting)}. "
            f"Contributing emitters: {list(contributing)}."
        ),
        "proposed_changes": [
            "Reconcile Table 2 entries in subject_key_registry.py so "
            "every pair involving the contributing emitters declares "
            "a consistent canonical_emitter for this finding_kind.",
        ],
        "acceptance_criteria": [
            "Stage 4b runs cleanly on the same input without raising "
            "CanonicalEmitterConflictError.",
        ],
        "scope_signals": {
            "files": ["sentinel/assess/subject_key_registry.py"],
            "functions": [],
            "modules": ["sentinel.assess.subject_key_registry"],
        },
        "confidence": "high",
        "notes": {
            "silent_peer_notes": "",
            "canonical_emitter_caveats": (
                "Synthetic pattern produced by the Orchestrator, not by "
                "the Architect. The Architect was not invoked for this "
                "group because Stage 4b short-circuited on the registry "
                "conflict raise."
            ),
        },
    }
