"""
sentinel/orchestrator/quality_artifacts.py

Phase 0 scaffolding for high-quality review artifacts.

These builders intentionally return empty, stable shapes so downstream
phases can rely on consistent structure while feature flags are introduced.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any


# Cycle 41: a Stage-3 specialist/quick-review chunk can fail for a transient
# transport reason (a retryable upstream 5xx / gateway timeout from the LLM
# gateway) rather than a genuine review gap. Such failures must not block an
# otherwise-complete assessment from advancing past the Quality Scorecard
# gate. We classify failed-chunk-registry entries into transport vs genuine
# and tolerate transport failures up to a small threshold; genuine failures
# (malformed output, schema violations, logic errors) still hard-fail.
TRANSPORT_FAILURE_TOLERANCE = 3

# The API gateway's own retryable flag is the most authoritative signal; the
# explicit-5xx and timeout patterns are anchored on surrounding words so a bare
# "503" appearing elsewhere in a message does not false-positive.
_RETRYABLE_RE = re.compile(r'"?retryable"?\s*[:=]\s*true', re.IGNORECASE)
_TRANSPORT_5XX_RE = re.compile(
    r"\b(?:server error|chat api error|http|status|error)\s*(?:code\s*)?5(?:0[0-9]|2[0-9])\b",
    re.IGNORECASE,
)
_TRANSPORT_PHRASE_RE = re.compile(
    r"\b(?:timeout|timed out|gateway|connection (?:reset|refused|error|aborted)|"
    r"temporarily unavailable|service unavailable)\b",
    re.IGNORECASE,
)


def is_transport_failure(entry: dict[str, Any]) -> bool:
    """True when a failed-chunk-registry entry is a transient transport error
    (retryable upstream 5xx / gateway timeout) rather than a genuine review
    gap. Reads the ``error`` / ``error_type`` (and legacy ``reason``) fields
    the reviewer records on each entry.
    """
    blob = " ".join(
        str(entry.get(key, "")) for key in ("error", "error_type", "reason")
    )
    if _RETRYABLE_RE.search(blob):
        return True
    if _TRANSPORT_5XX_RE.search(blob):
        return True
    if _TRANSPORT_PHRASE_RE.search(blob):
        return True
    return False


def classify_failed_chunks(
    failed_chunk_registry: list[dict[str, Any]] | None,
) -> tuple[int, int]:
    """Split a failed-chunk registry into ``(transport_failures, genuine_failures)``."""
    registry = failed_chunk_registry or []
    transport = sum(1 for entry in registry if is_transport_failure(entry))
    return transport, len(registry) - transport


_SEVERITY_RANK = {
    "Critical": 0,
    "P1": 1,
    "P2": 2,
    "P3": 3,
}

_SCHEMA_POLICY_HINTS = (
    "schema",
    "policy",
    "xml",
    "dependency risk",
    "syntax",
)

_ARCHITECTURAL_HINTS = (
    "architect",
    "cross-module",
    "coupling",
    "interaction",
)


def empty_reconciliation_report() -> dict[str, Any]:
    """Return an empty reconciliation artifact shape."""
    return {
        "summary": {
            "resolved_count": 0,
            "unresolved_count": 0,
            "kept_count": 0,
            "dropped_count": 0,
        },
        "conflicts": [],
        "decisions": [],
    }


def empty_quality_scorecard() -> dict[str, Any]:
    """Return an empty quality scorecard artifact shape."""
    return {
        "confidence_label": "incomplete",
        "metrics": {
            "module_completeness_pct": 0.0,
            "evidence_completeness_pct": 0.0,
            "lane_agreement_pct": 0.0,
            "unresolved_critical_conflicts": 0,
        },
        "gates": {
            "review_completeness": "unknown",
            "lane_conflict_resolution": "unknown",
            "confidence_floor": "unknown",
        },
    }


def deterministic_reconciliation_placeholder(
    deterministic_findings: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a placeholder reconciliation artifact seeded from deterministic findings."""
    finding_count = len(deterministic_findings)
    return {
        "summary": {
            "resolved_count": finding_count,
            "unresolved_count": 0,
            "kept_count": finding_count,
            "dropped_count": 0,
        },
        "conflicts": [],
        "decisions": [
            {
                "lane": "deterministic",
                "action": "queued_for_reconciliation",
                "finding_count": finding_count,
            }
        ],
    }


def reconcile_findings(
    deterministic_findings: list[dict[str, Any]],
    reasoning_findings: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Resolve deterministic and reasoning findings into a governed output."""
    # Index each lane by finding key, but preserve — rather than silently
    # overwrite — any duplicate same-key findings within a lane
    # (FIX-BLOCKING-094). The primary (first) finding per key drives the
    # cross-lane pairing loop below; extra duplicates are surfaced explicitly
    # afterwards so no evidence is lost.
    deterministic_by_key, deterministic_dupes = _index_findings_by_key(
        deterministic_findings
    )
    reasoning_by_key, reasoning_dupes = _index_findings_by_key(reasoning_findings)

    all_keys = sorted(set(deterministic_by_key) | set(reasoning_by_key))

    resolved_findings: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []

    for key in all_keys:
        deterministic = deterministic_by_key.get(key)
        reasoning = reasoning_by_key.get(key)

        if deterministic and reasoning:
            det_severity = _normalize_severity(deterministic.get("severity"))
            rea_severity = _normalize_severity(reasoning.get("severity"))

            if det_severity == rea_severity:
                kept = _with_lane_provenance(deterministic, ["deterministic", "reasoning"])
                resolved_findings.append(kept)
                conflicts.append(
                    {
                        "key": key,
                        "class": "duplicate_agreement",
                        "severity": det_severity,
                        "resolved": True,
                        "escalated": False,
                        "resolution": "agreement",
                        "lanes": ["deterministic", "reasoning"],
                    }
                )
                decisions.append(
                    {
                        "key": key,
                        "action": "keep",
                        "resolution": "agreement",
                    }
                )
                continue

            kept, resolution, resolved, escalated = _resolve_severity_disagreement(
                deterministic,
                reasoning,
            )
            kept_with_provenance = _with_lane_provenance(kept, ["deterministic", "reasoning"])
            if not resolved:
                kept_with_provenance["needs_human_review"] = True
            resolved_findings.append(kept_with_provenance)

            conflicts.append(
                {
                    "key": key,
                    "class": "severity_disagreement",
                    "severity": _more_severe(det_severity, rea_severity),
                    "resolved": resolved,
                    "escalated": escalated,
                    "resolution": resolution,
                    "lanes": ["deterministic", "reasoning"],
                }
            )
            decisions.append(
                {
                    "key": key,
                    "action": "keep",
                    "resolution": resolution,
                }
            )
            continue

        lane = "deterministic" if deterministic else "reasoning"
        kept = deterministic if deterministic else reasoning
        if kept is None:
            continue

        kept_with_provenance = _with_lane_provenance(kept, [lane])
        resolved_findings.append(kept_with_provenance)
        conflicts.append(
            {
                "key": key,
                "class": "presence_disagreement",
                "severity": _normalize_severity(kept.get("severity")),
                "resolved": True,
                "escalated": False,
                "resolution": f"keep_{lane}",
                "lanes": [lane],
            }
        )
        decisions.append(
            {
                "key": key,
                "action": "keep",
                "resolution": f"keep_{lane}",
            }
        )

    # Surface and preserve duplicate same-key findings within a lane
    # (FIX-BLOCKING-094). Each duplicate is kept in the resolved output and an
    # explicit duplicate-key conflict artifact records the full count, so
    # duplicates are never silently dropped or undercounted.
    for lane, dupes in (
        ("deterministic", deterministic_dupes),
        ("reasoning", reasoning_dupes),
    ):
        for key, extras in dupes.items():
            for extra in extras:
                resolved_findings.append(_with_lane_provenance(extra, [lane]))
            conflicts.append(
                {
                    "key": key,
                    "class": "duplicate_key_conflict",
                    "lane": lane,
                    "count": len(extras) + 1,
                    "severity": _normalize_severity(extras[0].get("severity")),
                    "resolved": True,
                    "escalated": False,
                    "resolution": "preserved_duplicates",
                    "lanes": [lane],
                }
            )

    unresolved = [conflict for conflict in conflicts if not conflict.get("resolved", False)]
    unresolved_by_severity = Counter(
        _normalize_severity(conflict.get("severity"))
        for conflict in unresolved
    )

    report = {
        "summary": {
            "resolved_count": len(resolved_findings),
            "unresolved_count": len(unresolved),
            "kept_count": len(resolved_findings),
            "dropped_count": max(0, len(deterministic_findings) + len(reasoning_findings) - len(resolved_findings)),
            "agreement_count": sum(1 for conflict in conflicts if conflict.get("class") == "duplicate_agreement"),
            "severity_disagreement_count": sum(
                1 for conflict in conflicts if conflict.get("class") == "severity_disagreement"
            ),
            "presence_disagreement_count": sum(
                1 for conflict in conflicts if conflict.get("class") == "presence_disagreement"
            ),
            "unresolved_by_severity": dict(unresolved_by_severity),
        },
        "conflicts": conflicts,
        "decisions": decisions,
    }
    return resolved_findings, conflicts, report


def build_quality_scorecard(
    *,
    coverage_report: dict[str, Any],
    retrieval_manifest: list[dict[str, Any]],
    finding_conflicts: list[dict[str, Any]],
    failed_chunk_registry: list[dict[str, Any]] | None = None,
    strict_mode: bool = False,
) -> dict[str, Any]:
    """Build quality metrics, confidence label, and gate decisions."""
    failed_chunk_registry = failed_chunk_registry or []
    module_completeness_pct = float(coverage_report.get("module_completeness_pct", 0.0))
    quick_review_success_pct = float(coverage_report.get("quick_review_success_pct", 0.0))
    failed_chunk_entries = int(coverage_report.get("failed_chunk_entries", len(failed_chunk_registry)))

    # Cycle 41: classify transport vs genuine failures and gate on the genuine
    # signal. Prefer the counts already computed by _build_coverage_report
    # (single source of truth); fall back to classifying the registry directly
    # so this builder stays correct when called standalone (tests/dashboard).
    _transport_classified, _genuine_classified = classify_failed_chunks(failed_chunk_registry)
    transport_failure_chunks = int(
        coverage_report.get("transport_failure_chunks", _transport_classified)
    )
    genuine_failed_chunk_entries = int(
        coverage_report.get("genuine_failed_chunk_entries", _genuine_classified)
    )
    # The gate measures completeness over genuine gaps only; transport-only
    # incomplete modules are not held against it. Falls back to the raw pct.
    gate_completeness_pct = float(
        coverage_report.get("genuine_module_completeness_pct", module_completeness_pct)
    )

    evidence_completeness_pct = _evidence_completeness_pct(retrieval_manifest)

    lane_comparable = [
        conflict for conflict in finding_conflicts
        if conflict.get("class") in {"duplicate_agreement", "severity_disagreement"}
    ]
    # A resolved conflict of ANY comparable class (duplicate_agreement OR a
    # resolved severity_disagreement) counts toward lane agreement. Counting
    # only duplicate_agreement depressed lane_agreement_pct and left resolved
    # severity disagreements masquerading as unresolved (P1 logic_bug).
    agreements = [
        conflict for conflict in lane_comparable
        if conflict.get("resolved", False)
    ]
    lane_agreement_pct = (
        (len(agreements) / len(lane_comparable)) * 100.0
        if lane_comparable
        else 100.0
    )

    unresolved_conflicts = [
        conflict for conflict in finding_conflicts
        if not conflict.get("resolved", False)
    ]
    unresolved_by_severity = Counter(
        _normalize_severity(conflict.get("severity"))
        for conflict in unresolved_conflicts
    )

    unresolved_critical_conflicts = int(unresolved_by_severity.get("Critical", 0))
    failure_telemetry = _failure_telemetry_summary(failed_chunk_registry)

    confidence_label = _confidence_label(
        module_completeness_pct=gate_completeness_pct,
        quick_review_success_pct=quick_review_success_pct,
        evidence_completeness_pct=evidence_completeness_pct,
        lane_agreement_pct=lane_agreement_pct,
        unresolved_critical_conflicts=unresolved_critical_conflicts,
        failed_chunk_entries=genuine_failed_chunk_entries,
        transport_failure_chunks=transport_failure_chunks,
        strict_mode=strict_mode,
    )

    review_module_threshold = 100.0 if strict_mode else 98.0
    review_quick_threshold = 100.0 if strict_mode else 95.0
    lane_agreement_threshold = 100.0 if strict_mode else 0.0
    unresolved_conflicts_total = len(unresolved_conflicts)

    gates = {
        "review_completeness": (
            "pass"
            if (
                gate_completeness_pct >= review_module_threshold
                and quick_review_success_pct >= review_quick_threshold
                and genuine_failed_chunk_entries == 0
                and transport_failure_chunks <= TRANSPORT_FAILURE_TOLERANCE
            )
            else "fail"
        ),
        "lane_conflict_resolution": (
            "pass"
            if (
                unresolved_critical_conflicts == 0
                and (not strict_mode or unresolved_conflicts_total == 0)
                and lane_agreement_pct >= lane_agreement_threshold
            )
            else "fail"
        ),
        "confidence_floor": (
            "pass"
            if (confidence_label == "high" if strict_mode else confidence_label in {"high", "medium"})
            else "fail"
        ),
    }

    remediation_guidance = []
    if gates["review_completeness"] == "fail":
        remediation_guidance.append(
            "Increase module and quick-review coverage before accepting assessment."
        )
    if gates["lane_conflict_resolution"] == "fail":
        remediation_guidance.append(
            "Resolve escalated cross-lane conflicts before proceeding."
        )
    if gates["confidence_floor"] == "fail":
        remediation_guidance.append(
            "Confidence floor not met; rerun with improved evidence and lower transport failures."
        )

    return {
        "confidence_label": confidence_label,
        "metrics": {
            "module_completeness_pct": round(module_completeness_pct, 2),
            "quick_review_success_pct": round(quick_review_success_pct, 2),
            "evidence_completeness_pct": round(evidence_completeness_pct, 2),
            "lane_agreement_pct": round(lane_agreement_pct, 2),
            "unresolved_conflicts_by_severity": dict(unresolved_by_severity),
            "unresolved_critical_conflicts": unresolved_critical_conflicts,
            "failed_chunk_entries": failed_chunk_entries,
            "transport_failure_chunks": transport_failure_chunks,
            "genuine_failed_chunk_entries": genuine_failed_chunk_entries,
            "failure_telemetry_summary": failure_telemetry,
        },
        "gates": gates,
        "remediation_guidance": remediation_guidance,
    }


def build_shadow_compare_report(
    *,
    legacy_findings: list[dict[str, Any]],
    resolved_findings: list[dict[str, Any]],
    finding_conflicts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build Phase 5 shadow-mode comparison and promotion/rollback guidance."""
    legacy_counts = Counter(_severity_bucket(f.get("severity")) for f in legacy_findings)
    resolved_counts = Counter(_severity_bucket(f.get("severity")) for f in resolved_findings)

    unresolved_conflicts = [c for c in finding_conflicts if not c.get("resolved", False)]
    unresolved_critical = sum(
        1 for c in unresolved_conflicts if _normalize_severity(c.get("severity")) == "Critical"
    )
    conflict_rate = (
        len(unresolved_conflicts) / len(finding_conflicts)
        if finding_conflicts
        else 0.0
    )

    severity_delta = {
        sev: int(resolved_counts.get(sev, 0) - legacy_counts.get(sev, 0))
        for sev in ("Critical", "P1", "P2", "P3")
    }
    finding_count_delta = len(resolved_findings) - len(legacy_findings)

    promotion_ready = unresolved_critical == 0 and conflict_rate <= 0.2

    rollback_reason = ""
    if unresolved_critical > 0:
        rollback_reason = "Unresolved critical cross-lane conflicts detected."
    elif conflict_rate > 0.2:
        rollback_reason = "Unresolved conflict rate exceeds rollout threshold."

    return {
        "baseline": {
            "finding_count": len(legacy_findings),
            "severity_distribution": dict(legacy_counts),
        },
        "candidate": {
            "finding_count": len(resolved_findings),
            "severity_distribution": dict(resolved_counts),
        },
        "delta": {
            "finding_count_delta": finding_count_delta,
            "severity_distribution_delta": severity_delta,
            "unresolved_conflict_rate": round(conflict_rate, 4),
        },
        "promotion": {
            "promotion_ready": promotion_ready,
            "criteria": {
                "unresolved_critical_conflicts": unresolved_critical,
                "max_unresolved_conflict_rate": 0.2,
            },
        },
        "rollback": {
            "recommended": not promotion_ready,
            "reason": rollback_reason,
        },
    }


def build_line_coverage_map(
    *,
    module_scope_lines: dict[str, int],
    deterministic_success: dict[str, bool],
    reasoning_ranges: dict[str, list[tuple[int, int]]],
    strict_mode: bool = False,
) -> list[dict[str, Any]]:
    """Build line-level coverage evidence by module."""
    coverage_map: list[dict[str, Any]] = []
    for module in sorted(module_scope_lines):
        total_lines = max(0, int(module_scope_lines.get(module, 0)))
        det_ok = bool(deterministic_success.get(module, False))

        normalized_ranges = _normalize_ranges(reasoning_ranges.get(module, []), total_lines)
        reasoning_reviewed_lines = sum((end - start + 1) for start, end in normalized_ranges)

        deterministic_reviewed_lines = total_lines if det_ok else 0
        combined_reviewed_lines = (
            reasoning_reviewed_lines
            if strict_mode
            else (total_lines if det_ok else reasoning_reviewed_lines)
        )

        uncovered_ranges = (
            []
            if combined_reviewed_lines >= total_lines
            else _complement_ranges(normalized_ranges, total_lines)
        )
        coverage_pct = (
            round((combined_reviewed_lines / total_lines) * 100.0, 2)
            if total_lines > 0
            else 100.0
        )

        coverage_map.append(
            {
                "module": module,
                "total_lines": total_lines,
                "deterministic_reviewed_lines": deterministic_reviewed_lines,
                "reasoning_reviewed_lines": reasoning_reviewed_lines,
                "combined_reviewed_lines": combined_reviewed_lines,
                "coverage_pct": coverage_pct,
                "uncovered_ranges": [[start, end] for start, end in uncovered_ranges],
            }
        )

    return coverage_map


def build_coverage_certificate(
    *,
    line_coverage_map: list[dict[str, Any]],
    threshold_pct: float,
) -> dict[str, Any]:
    """Build aggregate proof artifact and pass/fail status for full-coverage gate."""
    total_in_scope_lines = sum(int(entry.get("total_lines", 0)) for entry in line_coverage_map)
    total_reviewed_lines = sum(int(entry.get("combined_reviewed_lines", 0)) for entry in line_coverage_map)
    uncovered_line_count = max(0, total_in_scope_lines - total_reviewed_lines)

    modules_with_gaps = [
        str(entry.get("module", ""))
        for entry in line_coverage_map
        if int(entry.get("combined_reviewed_lines", 0)) < int(entry.get("total_lines", 0))
    ]
    coverage_pct = (
        round((total_reviewed_lines / total_in_scope_lines) * 100.0, 2)
        if total_in_scope_lines > 0
        else 100.0
    )

    threshold = float(threshold_pct)
    passed = coverage_pct >= threshold and uncovered_line_count == 0 and not modules_with_gaps

    return {
        "status": "pass" if passed else "fail",
        "threshold_pct": threshold,
        "metrics": {
            "total_in_scope_lines": total_in_scope_lines,
            "reviewed_lines": total_reviewed_lines,
            "uncovered_line_count": uncovered_line_count,
            "coverage_pct": coverage_pct,
            "modules_with_gaps": modules_with_gaps,
        },
    }


def _finding_key(finding: dict[str, Any]) -> str:
    module = str(finding.get("module", "")).strip().lower()
    location = str(finding.get("location", "")).strip().lower()
    description = str(finding.get("description", "")).strip().lower()
    return "|".join([module, location, description])


def _index_findings_by_key(
    findings: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """Index findings by ``_finding_key``, preserving duplicates.

    Returns ``(primary_by_key, duplicates_by_key)`` where ``primary_by_key``
    holds the first finding seen per key and ``duplicates_by_key`` holds any
    subsequent same-key findings (so callers can surface them explicitly rather
    than silently overwriting, per FIX-BLOCKING-094).
    """
    primary: dict[str, dict[str, Any]] = {}
    duplicates: dict[str, list[dict[str, Any]]] = {}
    for finding in findings:
        key = _finding_key(finding)
        if key in primary:
            duplicates.setdefault(key, []).append(finding)
        else:
            primary[key] = finding
    return primary, duplicates


def _normalize_ranges(
    ranges: list[tuple[int, int]],
    max_line: int,
) -> list[tuple[int, int]]:
    cleaned: list[tuple[int, int]] = []
    for start, end in ranges:
        s = max(1, int(start))
        e = min(max_line, int(end))
        if max_line <= 0 or s > e:
            continue
        cleaned.append((s, e))
    if not cleaned:
        return []

    cleaned.sort(key=lambda item: item[0])
    merged: list[tuple[int, int]] = [cleaned[0]]
    for start, end in cleaned[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end + 1:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged


def _complement_ranges(
    ranges: list[tuple[int, int]],
    max_line: int,
) -> list[tuple[int, int]]:
    if max_line <= 0:
        return []
    if not ranges:
        return [(1, max_line)]

    gaps: list[tuple[int, int]] = []
    cursor = 1
    for start, end in ranges:
        if cursor < start:
            gaps.append((cursor, start - 1))
        cursor = max(cursor, end + 1)
    if cursor <= max_line:
        gaps.append((cursor, max_line))
    return gaps


def _normalize_severity_strict(value: Any) -> str | None:
    """Return the canonical severity for known values, else ``None``.

    Empty/missing severity defaults to ``P3`` (pre-existing behavior), but a
    non-empty *unrecognized* value returns ``None`` so callers can preserve the
    raw value and flag it instead of silently coercing to ``P3``
    (FIX-BLOCKING-110).
    """
    severity = str(value or "P3").strip()
    if not severity:
        return "P3"
    if severity.lower() == "critical":
        return "Critical"
    upper = severity.upper()
    if upper in {"P1", "P2", "P3"}:
        return upper
    return None


def _normalize_severity(value: Any) -> str:
    """Canonical severity with a ``P3`` fallback for ranking/ordering use."""
    strict = _normalize_severity_strict(value)
    return strict if strict is not None else "P3"


def _severity_bucket(value: Any) -> str:
    """Severity bucket for distribution reporting.

    Known severities map to their canonical label; unrecognized values are
    counted under ``"unknown"`` rather than being folded into ``P3``
    (FIX-BLOCKING-110).
    """
    strict = _normalize_severity_strict(value)
    return strict if strict is not None else "unknown"


def _with_lane_provenance(
    finding: dict[str, Any],
    lanes: list[str],
) -> dict[str, Any]:
    enriched = dict(finding)
    existing = enriched.get("lane_provenance")
    provenance = list(existing) if isinstance(existing, list) else []
    provenance.extend(lanes)
    enriched["lane_provenance"] = sorted(set(str(lane) for lane in provenance if lane))
    strict = _normalize_severity_strict(enriched.get("severity"))
    if strict is None:
        # Unknown severity: preserve the raw value and flag it instead of
        # rewriting it to P3 (FIX-BLOCKING-110).
        enriched["raw_severity"] = str(enriched.get("severity"))
        enriched["severity_validation"] = "unknown_severity"
    else:
        enriched["severity"] = strict
    return enriched


def _resolve_severity_disagreement(
    deterministic: dict[str, Any],
    reasoning: dict[str, Any],
) -> tuple[dict[str, Any], str, bool, bool]:
    det_description = str(deterministic.get("description", "")).lower()
    rea_description = str(reasoning.get("description", "")).lower()

    # Use the strict normaliser so an unrecognised (non-empty) severity yields
    # None instead of being silently coerced to P3. If either lane's severity
    # is unknown we cannot reason about relative severity, so fall back to the
    # deterministic finding unchanged rather than trusting a P3 guess
    # (P1 logic_bug: unknown-severity short-circuit / strict-path consistency).
    det_severity = _normalize_severity_strict(deterministic.get("severity"))
    rea_severity = _normalize_severity_strict(reasoning.get("severity"))
    if det_severity is None or rea_severity is None:
        return deterministic, "deterministic_precedence_unknown_severity", True, False

    # Deterministic schema/policy findings take precedence unconditionally.
    if any(hint in det_description for hint in _SCHEMA_POLICY_HINTS):
        return deterministic, "deterministic_precedence", True, False

    # An architectural-hint in the reasoning lane only overrides the
    # deterministic finding when the reasoning severity is AT LEAST as severe
    # (a lower rank number == more severe). Otherwise a wording-tagged, lower
    # severity reasoning finding could override a deterministic Critical
    # (P1 logic_bug: architectural-hint override ignoring severity).
    if (
        any(hint in rea_description for hint in _ARCHITECTURAL_HINTS)
        and _SEVERITY_RANK[rea_severity] <= _SEVERITY_RANK[det_severity]
    ):
        return reasoning, "reasoning_precedence", True, False

    winning = deterministic if _SEVERITY_RANK[det_severity] <= _SEVERITY_RANK[rea_severity] else reasoning
    conflict_high = det_severity in {"Critical", "P1"} or rea_severity in {"Critical", "P1"}
    return winning, "escalate_high_severity_conflict", not conflict_high, conflict_high


def _more_severe(left: str, right: str) -> str:
    return left if _SEVERITY_RANK[left] <= _SEVERITY_RANK[right] else right


def _evidence_completeness_pct(retrieval_manifest: list[dict[str, Any]]) -> float:
    required = {"module_path", "start_line", "end_line", "snippet_hash", "char_count", "content"}
    total = 0
    complete = 0
    for module in retrieval_manifest:
        for skill_entry in module.get("skills", []):
            for snippet in skill_entry.get("snippets", []):
                total += 1
                if required.issubset(set(snippet.keys())):
                    complete += 1
    if total == 0:
        return 100.0
    return (complete / total) * 100.0


def _failure_telemetry_summary(
    failed_chunk_registry: list[dict[str, Any]],
) -> dict[str, int]:
    summary = {"413": 0, "429": 0, "timeout": 0, "other": 0}
    for entry in failed_chunk_registry:
        reason = str(entry.get("reason", "")).lower()
        if "413" in reason or "payload" in reason:
            summary["413"] += 1
        elif "429" in reason or "rate" in reason:
            summary["429"] += 1
        elif "timeout" in reason:
            summary["timeout"] += 1
        else:
            summary["other"] += 1
    return summary


def _confidence_label(
    *,
    module_completeness_pct: float,
    quick_review_success_pct: float,
    evidence_completeness_pct: float,
    lane_agreement_pct: float,
    unresolved_critical_conflicts: int,
    failed_chunk_entries: int,
    transport_failure_chunks: int = 0,
    strict_mode: bool = False,
) -> str:
    # Cycle 41: ``failed_chunk_entries`` here is the *genuine* (non-transport)
    # count; transport failures are tolerated up to TRANSPORT_FAILURE_TOLERANCE.
    transport_ok = transport_failure_chunks <= TRANSPORT_FAILURE_TOLERANCE
    if unresolved_critical_conflicts > 0:
        return "incomplete"
    if strict_mode:
        if (
            module_completeness_pct >= 100.0
            and quick_review_success_pct >= 100.0
            and evidence_completeness_pct >= 100.0
            and lane_agreement_pct >= 100.0
            and failed_chunk_entries == 0
            and transport_ok
        ):
            return "high"
        return "incomplete"
    if (
        module_completeness_pct >= 98.0
        and quick_review_success_pct >= 95.0
        and evidence_completeness_pct >= 95.0
        and lane_agreement_pct >= 80.0
        and failed_chunk_entries == 0
        and transport_ok
    ):
        return "high"
    if (
        module_completeness_pct >= 90.0
        and quick_review_success_pct >= 85.0
        and evidence_completeness_pct >= 80.0
    ):
        return "medium"
    return "incomplete"
