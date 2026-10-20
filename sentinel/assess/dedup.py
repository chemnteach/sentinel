"""
sentinel/assess/dedup.py

Post-processing deduplication for findings.

Runs after all layers complete for a module. Removes exact duplicates
and consolidates semantic duplicates (same root cause reported by
multiple layers) into single findings with multi-layer attribution.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from typing import Any

from sentinel.assess.subject_key_normalizer import canonicalize_subject_key
from sentinel.assess.subject_key_registry import (
    get_co_emission_consolidation_rule,
    get_namespace_owner,
    get_table_2_peers,
)

_SEVERITY_RANK = {"Critical": 0, "P1": 1, "P2": 2, "P3": 3}


@dataclass(frozen=True, slots=True)
class RegistryConsolidationPlan:
    key: str
    relationship_rule: str | None
    canonical_emitter: str | None
    namespace: str
    subject_key: str
    finding_kind: str
    preserve_both: bool = False


def deduplicate(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove exact duplicates and consolidate semantic duplicates.

    Returns deduplicated findings list. Findings consolidated from
    multiple layers get a ``layers`` list and the highest severity wins.
    """
    if not findings:
        return []

    # Pass 1: exact dedup by fingerprint
    seen: dict[str, dict] = {}
    for f in findings:
        fp = _fingerprint(f)
        if fp in seen:
            merged = _merge_into(seen[fp], f)
            if merged is None:
                seen[f"{fp}|{uuid.uuid4().hex}"] = {**f, "layers": [f.get("skill", "")]}
            else:
                seen[fp] = merged
        else:
            seen[fp] = {**f, "layers": [f.get("skill", "")]}

    # Pass 2: semantic dedup by root cause
    groups: dict[str, list[dict]] = {}
    for f in seen.values():
        key = _root_cause_key(f)
        groups.setdefault(key, []).append(f)

    result = []
    for group in groups.values():
        if len(group) == 1:
            item = group[0]
            if _registry_consolidation_plan(item) is not None:
                item = {**item}
                item["consolidated_from"] = 1
                key = _root_cause_key(item)
                item["root_cause_id"] = hashlib.sha256(key.encode()).hexdigest()[:12]
            result.append(item)
        else:
            result.append(_consolidate_group(group))

    # Pass 3: intra-specialist overlapping-line collapse.
    return _collapse_intra_specialist_overlaps(result)


# ── Pass 3: intra-specialist overlapping-line duplicate collapse ──────────
#
# The same specialist sometimes emits two findings for one issue at slightly
# different line bounds (same file, same skill, same finding_kind, overlapping
# line_range). Passes 1/2 miss these — the descriptions/line strings differ, so
# fingerprints and root-cause keys diverge. Cross-specialist overlaps (different
# skill) are intentionally left for the grouper (multi-perspective = confidence
# signal, not noise).


def _parse_line_range(line_range: object) -> tuple[int, int] | None:
    """Parse a line reference into ``(start, end)`` ints.

    Handles bare integers (``"42"``), ranges (``"42-50"`` / ``"42:50"``), and
    decorated forms with a leading label or trailing context, e.g.
    ``"line 42"``, ``"lines 42-50"``, or ``"42:some_function"``. The leading
    ``line``/``lines`` label is stripped and the leading digit group is
    extracted so decorated inputs still participate in the overlap pass.

    Empty or unparseable input returns ``None`` — such findings cannot
    participate in the overlap pass and pass through unchanged.
    """
    if not line_range:
        return None
    text = str(line_range).strip()
    if not text:
        return None
    # Strip a leading "line"/"lines" label, then extract the leading digit
    # group and an optional second digit group after a ':' or '-' separator.
    stripped = re.sub(r"(?i)^lines?\s+", "", text).strip()
    match = re.match(r"(\d+)(?:[:-](\d+))?", stripped)
    if not match:
        return None
    try:
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) is not None else start
        return (start, end)
    except (ValueError, TypeError):
        return None


def _ranges_overlap(r1: tuple[int, int], r2: tuple[int, int]) -> bool:
    """Two ranges overlap when ``s1 <= e2 and s2 <= e1``."""
    s1, e1 = r1
    s2, e2 = r2
    return s1 <= e2 and s2 <= e1


def _select_overlap_survivor(members: list[dict]) -> dict:
    """Highest severity wins; ties broken by longest description (as Pass 1)."""
    best_rank = min(_severity_rank(m) for m in members)
    top = [m for m in members if _severity_rank(m) == best_rank]
    return max(top, key=lambda m: len(m.get("description", "")))


def _collapse_intra_specialist_overlaps(findings: list[dict]) -> list[dict]:
    """Collapse same-(file, skill, finding_kind) findings whose line ranges
    overlap (transitively, via union-find). Order-preserving: a cluster's
    survivor is emitted at the position of its first member; non-participating
    findings (no parseable line_range) pass through unchanged."""
    n = len(findings)
    ranges: list[tuple[int, int] | None] = []
    keys: list[tuple[str, str, str, str] | None] = []
    for f in findings:
        file, line, _function = _location_fields(f)
        rng = _parse_line_range(line)
        skill = str(f.get("skill", "") or "")
        kind = str(f.get("finding_kind", "") or "")
        subject_key = canonicalize_subject_key(
            str(f.get("subject_key", "") or "").strip())
        # Participate only with a real (file, skill, finding_kind) identity and a
        # parseable range. Missing file/skill/finding_kind or an unparseable
        # line_range -> pass through unchanged (an empty finding_kind is not a
        # match, so such findings must not be collapsed against each other).
        # subject_key is part of the identity so two distinct bugs from the same
        # specialist on overlapping lines (different subjects) are not merged.
        if rng is None or not file or not skill or not kind:
            ranges.append(None)
            keys.append(None)
        else:
            ranges.append(rng)
            keys.append((file, skill, kind, subject_key))

    parent = list(range(n))

    def _find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def _union(a: int, b: int) -> None:
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    by_key: dict[tuple[str, str, str, str], list[int]] = {}
    for i in range(n):
        if keys[i] is not None:
            by_key.setdefault(keys[i], []).append(i)
    for idxs in by_key.values():
        for a in range(len(idxs)):
            for b in range(a + 1, len(idxs)):
                i, j = idxs[a], idxs[b]
                if _ranges_overlap(ranges[i], ranges[j]):  # type: ignore[arg-type]
                    _union(i, j)

    members_by_root: dict[int, list[int]] = {}
    for i in range(n):
        members_by_root.setdefault(_find(i), []).append(i)

    out: list[dict] = []
    emitted_roots: set[int] = set()
    for i in range(n):
        root = _find(i)
        members = members_by_root[root]
        if len(members) == 1:
            out.append(findings[i])
            continue
        if root in emitted_roots:
            continue
        emitted_roots.add(root)
        member_findings = [findings[m] for m in members]
        survivor = {**_select_overlap_survivor(member_findings)}
        union_layers: list[str] = []
        for mf in member_findings:
            for layer in mf.get("layers", []):
                if layer and layer not in union_layers:
                    union_layers.append(layer)
        survivor["layers"] = union_layers
        out.append(survivor)
    return out


def _location_fields(finding: dict) -> tuple[str, str, str]:
    """Return ``(file, line, function)`` using nested-location precedence.

    The active finding contract stores file/line/function under a nested
    ``location`` object (``location.file`` / ``location.line`` /
    ``location.function``). Read those nested values first, falling back to the
    legacy top-level ``file`` / ``line_range`` / ``function`` keys only when a
    nested value is absent. A non-dict ``location`` (e.g. a flattened string)
    contributes no nested values, so legacy top-level keys are used unchanged.
    """
    raw_location = finding.get("location")
    nested = raw_location if isinstance(raw_location, dict) else {}

    def _coalesce(*candidates: object) -> str:
        for candidate in candidates:
            text = str(candidate or "").strip()
            if text:
                return text
        return ""

    file = _coalesce(nested.get("file"), finding.get("file"))
    line = _coalesce(
        nested.get("line"),
        nested.get("line_range"),
        finding.get("line_range"),
        finding.get("line"),
    )
    function = _coalesce(nested.get("function"), finding.get("function"))
    return file, line, function


def _fingerprint(finding: dict) -> str:
    """Exact-match fingerprint over canonical
    dedup identity fields."""
    file, line, function = _location_fields(finding)
    content = "|".join([
        str(finding.get("finding_kind", "") or ""),
        str(finding.get("category", "") or ""),
        canonicalize_subject_key(
            str(finding.get(
                "subject_key", "") or "").strip()
        ),
        str(finding.get("summary", "") or ""),
        f"{file}|{line}|{function}",
        finding.get("description", "") or "",
    ])
    return hashlib.sha256(
        content.encode()
    ).hexdigest()[:16]


def _root_cause_key(finding: dict) -> str:
    """Group key for semantic dedup.

    Registry-backed findings use subject_key-driven consolidation.
    Findings without subject_key fall back to the legacy file + function
    + category grouping key.
    """
    plan = _registry_consolidation_plan(finding)
    if plan is not None:
        return plan.key
    return _legacy_consolidation_key(finding)


def _legacy_consolidation_key(finding: dict) -> str:
    """Legacy file + function + category semantic key."""
    file, _line, function = _location_fields(finding)
    if not file and not function:
        return f"_unique_{uuid.uuid4().hex}"
    category = finding.get("category", "")
    return f"{file}|{function}|{category}"


def _subject_key_namespace(subject_key: str) -> str | None:
    """Return the namespace token prefix from a subject_key, if present."""
    if ":" not in subject_key:
        return None
    return f"{subject_key.split(':', 1)[0]}:"


def _registry_consolidation_plan(finding: dict[str, Any]) -> RegistryConsolidationPlan | None:
    """Return registry-driven consolidation details for a finding.

    Findings without subject_key stay on the legacy path. Registry-backed
    findings are keyed by namespace + subject_key, with rule-sensitive
    suffixes for collision disambiguation or preserve-both-attribution.
    """
    subject_key = canonicalize_subject_key(str(finding.get("subject_key", "")))
    if not subject_key:
        return None

    skill = str(finding.get("skill", "")).strip()
    finding_kind = str(finding.get("finding_kind", "")).strip()
    if not skill or not finding_kind:
        return None

    namespace = _subject_key_namespace(subject_key)
    if namespace is None or not get_namespace_owner(namespace):
        return None

    owners = get_namespace_owner(namespace)
    shared_namespace = len(owners) > 1

    # Default registry path: namespace + subject_key only. Rule-sensitive
    # suffixes are added when the registry requires separate attribution.
    key = f"registry|{namespace}|{subject_key}"
    relationship_rule: str | None = None
    canonical_emitter: str | None = None
    preserve_both = False

    peers = get_table_2_peers(skill, finding_kind)
    for peer in peers:
        rule = get_co_emission_consolidation_rule(skill, peer, namespace)
        if rule is None:
            continue
        relationship_rule = rule
        if rule == "preserve_both_with_attribution":
            # Deliberate choice: preserve_both_with_attribution intentionally
            # yields one record per emitter, each with its own root_cause_id.
            preserve_both = True
            key = f"{key}|{skill}"
            canonical_emitter = skill
            break
        if rule == "specialist_wins_over_quick":
            canonical_emitter = peer if skill == "layer2a-quick-review" else skill
            break
        if rule == "collapse_on_shared_subject_key":
            break

    if namespace == "migration:":
        key = f"{key}|{finding_kind}"
    elif not shared_namespace:
        # Layer-exclusive registry namespace: same finding_kind on the same
        # subject_key should collapse, but kind stays in the key so distinct
        # finding kinds on the same surface remain separated.
        key = f"{key}|{finding_kind}"

    return RegistryConsolidationPlan(
        key=key,
        relationship_rule=relationship_rule,
        canonical_emitter=canonical_emitter,
        namespace=namespace,
        subject_key=subject_key,
        finding_kind=finding_kind,
        preserve_both=preserve_both,
    )


def _merge_into(existing: dict, duplicate: dict) -> dict | None:
    """Merge an exact duplicate into the existing finding.

    Adds the duplicate's skill to the layers list and keeps the higher
    severity.
    """
    existing_plan = _registry_consolidation_plan(existing)
    duplicate_plan = _registry_consolidation_plan(duplicate)

    # Registry-backed findings only merge when they resolve to the same
    # consolidation key. Otherwise the exact-fingerprint collision is kept
    # separate so the registry can preserve distinct semantic roots.
    if (existing_plan is None) != (duplicate_plan is None):
        return None
    if existing_plan is not None and duplicate_plan is not None:
        if existing_plan.key != duplicate_plan.key:
            return None

    skill = duplicate.get("skill", "")
    primary = _select_primary_finding(existing, duplicate, existing_plan, duplicate_plan)
    secondary = duplicate if primary is existing else existing

    primary_layers = list(primary.get("layers", []))
    for layer in secondary.get("layers", []):
        if layer and layer not in primary_layers:
            primary_layers.append(layer)

    if skill and skill not in primary_layers:
        primary_layers.append(skill)

    primary.setdefault("layers", [])
    primary["layers"] = primary_layers

    # Keep the higher severity (lower rank = higher severity). Compare the
    # secondary against the primary rather than duplicate-vs-primary: ``primary``
    # may itself be the duplicate, in which case ``duplicate < primary`` is
    # always false and the ``existing`` finding's (possibly higher) severity
    # would be silently dropped.
    if _severity_rank(secondary) < _severity_rank(primary):
        primary["severity"] = secondary["severity"]

    return primary


def _select_primary_finding(
    existing: dict,
    duplicate: dict,
    existing_plan: RegistryConsolidationPlan | None,
    duplicate_plan: RegistryConsolidationPlan | None,
) -> dict:
    """Choose the primary finding for a merge.

    Canonical-emitter preference wins when the registry provides one.
    Otherwise the longest description remains the fallback.
    """
    preferred_emitter = _preferred_emitter(existing, duplicate, existing_plan, duplicate_plan)
    if preferred_emitter is not None:
        existing_is_preferred = existing.get("skill", "") == preferred_emitter
        duplicate_is_preferred = duplicate.get("skill", "") == preferred_emitter
        if existing_is_preferred and not duplicate_is_preferred:
            return existing
        if duplicate_is_preferred and not existing_is_preferred:
            return duplicate

    return existing if len(existing.get("description", "")) >= len(duplicate.get("description", "")) else duplicate


def _preferred_emitter(
    existing: dict,
    duplicate: dict,
    existing_plan: RegistryConsolidationPlan | None,
    duplicate_plan: RegistryConsolidationPlan | None,
) -> str | None:
    for plan in (existing_plan, duplicate_plan):
        if plan is not None and plan.canonical_emitter:
            return plan.canonical_emitter
    return None


def _consolidate_group(group: list[dict]) -> dict:
    """Consolidate semantically related findings into one.

    - Keeps the longest description (most detailed)
    - Keeps the highest severity
    - Merges all layers
    - Adds ``consolidated_from`` count
    - Adds ``root_cause_id``
    """
    plan = _registry_consolidation_plan(group[0])
    best = min(group, key=_severity_rank)
    highest_severity_members = [f for f in group if _severity_rank(f) == _severity_rank(best)]
    primary = _select_primary_for_group(highest_severity_members, plan)
    result = {**primary}

    # Merge layers from all findings in the group
    all_layers: list[str] = []
    for f in group:
        for layer in f.get("layers", []):
            if layer and layer not in all_layers:
                all_layers.append(layer)
    result["layers"] = all_layers

    # Highest severity wins
    result["severity"] = best["severity"]

    # Metadata
    result["consolidated_from"] = len(group)

    # Deterministic root cause ID from the group key
    key = _root_cause_key(result)
    result["root_cause_id"] = hashlib.sha256(key.encode()).hexdigest()[:12]

    return result


def _select_primary_for_group(
    group: list[dict],
    plan: RegistryConsolidationPlan | None,
) -> dict:
    """Select the primary finding for a semantic group.

    Registry-backed groups prefer the canonical emitter first, then the
    longest description within that emitter. Legacy groups keep the prior
    longest-description rule.
    """
    if plan is None or not plan.canonical_emitter:
        return max(group, key=lambda f: len(f.get("description", "")))

    canonical_members = [f for f in group if f.get("skill", "") == plan.canonical_emitter]
    if canonical_members:
        return max(canonical_members, key=lambda f: len(f.get("description", "")))
    return max(group, key=lambda f: len(f.get("description", "")))


def _severity_rank(finding: dict) -> int:
    """Return numeric rank for severity (lower = more severe)."""
    return _SEVERITY_RANK.get(finding.get("severity", "P3"), 3)
