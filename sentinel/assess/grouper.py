"""
sentinel/assess/grouper.py

Phase 2 semantic grouping layer.

Consumes the deduped findings produced by ``sentinel.assess.dedup`` and
produces ``SemanticGroup`` buckets keyed by registry-canonical identity
(subject_key, finding_kind) when available, falling back to half-registry
and legacy modes when registry coverage is partial or absent.

Three classification modes:

* ``"registry"`` — finding has both ``subject_key`` and ``finding_kind``;
  Table 2 may declare a canonical emitter and peer set.
* ``"half_registry"`` — finding has ``finding_kind`` but no
  ``subject_key`` (or vice versa). Group key falls back to
  ``(file, function, finding_kind)`` and the case is logged.
* ``"legacy"`` — finding has neither ``subject_key`` nor
  ``finding_kind``. Group key falls back to dedup's
  ``(file, function, category)`` triple.

The grouper is read-only with respect to the registry, dedup, and the
synthesizer. Its only output is the in-memory ``list[SemanticGroup]``
returned from ``SemanticGrouper.group`` and the optional JSON/JSONL
artifacts written by ``write_groups`` and ``FileAuditLog``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any, Literal, Protocol

from sentinel.assess.finding_kinds import get_finding_kind_definition
from sentinel.assess.subject_key_normalizer import canonicalize_subject_key
from sentinel.assess.subject_key_registry import (
    PairRule,
    build_pair_aware_kind_index,
)

log = logging.getLogger(__name__)


# ── Module-level constants ────────────────────────────────────────────

_NO_CANONICAL_EMITTER: str = ""
"""Sentinel for SemanticGroup.canonical_emitter when no canonical
emitter is declared (or every pair present says ``preserve_both``)."""

_SEVERITY_RANK: Mapping[str, int] = {"P3": 0, "P2": 1, "P1": 2, "Critical": 3}
"""Higher rank = more severe. Used for aggregation (group severity =
max member severity) and sort ordering (desc)."""

Mode = Literal["registry", "half_registry", "legacy"]

MalformedSubjectKeyPolicy = Literal["route_to_legacy", "fail_fast"]
UnknownFindingKindPolicy = Literal["fail_fast", "form_empty_group"]

GROUPS_FILENAME = "semantic_groups.json"
AUDIT_FILENAME = "grouper_audit.jsonl"


# ── Exceptions ────────────────────────────────────────────────────────


class GrouperError(Exception):
    """Base for grouper-layer errors."""


class MalformedSubjectKeyError(GrouperError):
    """A finding's ``subject_key`` does not parse as ``<token><body>``."""

    def __init__(self, finding_id: str, subject_key: str) -> None:
        super().__init__(
            f"finding {finding_id!r} has malformed subject_key "
            f"{subject_key!r}: expected '<namespace_token>:<body>'"
        )
        self.finding_id = finding_id
        self.subject_key = subject_key


class UnknownFindingKindError(GrouperError):
    """A finding's ``finding_kind`` is not in ``FINDING_KIND_REGISTRY``."""

    def __init__(self, finding_id: str, finding_kind: str) -> None:
        super().__init__(
            f"finding {finding_id!r} has unknown finding_kind "
            f"{finding_kind!r}: not registered in finding_kinds.py"
        )
        self.finding_id = finding_id
        self.finding_kind = finding_kind


class CanonicalEmitterConflictError(GrouperError):
    """A SemanticGroup's contributing emitters span Table 2 pair
    rules whose ``canonical_emitter`` values disagree.

    Raised at group-build time (not index-build time) and only when the
    group's own data exhibits the disagreement — pairs that don't
    co-occur in any single group do not trip this raise. ``None``
    (preserve-both) values are tolerated alongside a single named
    canonical; only two or more *named* canonicals constitute a
    conflict.
    """

    def __init__(
        self,
        finding_kind: str,
        contributing_emitters: tuple[str, ...],
        conflicting_canonicals: tuple[str, ...],
    ) -> None:
        super().__init__(
            f"finding_kind {finding_kind!r} resolved to conflicting "
            f"canonical_emitter values {conflicting_canonicals} "
            f"across pairs present in this group "
            f"(contributing emitters: {contributing_emitters})"
        )
        self.finding_kind = finding_kind
        self.contributing_emitters = contributing_emitters
        self.conflicting_canonicals = conflicting_canonicals


# ── Dataclasses ───────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Provenance:
    """How a SemanticGroup came to be — what mode classified it and
    which key components were used."""

    mode: Mode
    grouping_key: str
    subject_key: str | None
    finding_kind: str | None
    file: str | None
    function: str | None
    category: str | None


@dataclass(frozen=True, slots=True)
class ConflictRecord:
    """Structured record of a canonical-emitter conflict caught at
    ``_build_group`` time. Serializes to dict for StateStore
    persistence; replaces the live ``CanonicalEmitterConflictError``
    that previously propagated to Stage 4b.

    Field names match the ``getattr`` axes consumed by
    ``build_synthetic_conflict_pattern`` in
    ``sentinel.orchestrator.context`` so instances can be passed
    directly as the ``exc`` argument without an adapter.
    """

    finding_kind: str
    contributing_emitters: tuple[str, ...]
    conflicting_canonicals: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_kind": self.finding_kind,
            "contributing_emitters": list(self.contributing_emitters),
            "conflicting_canonicals": list(self.conflicting_canonicals),
        }


@dataclass(frozen=True, slots=True)
class SemanticGroup:
    """One semantic grouping of one or more deduped findings.

    A SemanticGroup is the unit consumed by the Architect re-entry pass.
    Identity is ``group_id`` (sha256[:12] over ``grouping_key``); a
    group with one member is still a SemanticGroup.
    """

    group_id: str
    grouping_key: str
    mode: Mode
    severity: str
    member_count: int
    member_finding_ids: tuple[str, ...]
    contributing_emitters: tuple[str, ...]
    canonical_emitter: str
    peer_emitters: tuple[str, ...]
    """Full registered peer set for this finding_kind across all
    Table 2 relationships involving any contributing emitter.
    May include emitters that are themselves in
    ``contributing_emitters`` (e.g., layer3d in a group where
    layer3d contributed) — peers are by-pair, not exclusive.
    Consumer-side: silent peers = ``peer_emitters - contributing_emitters``."""
    subject_key: str | None
    finding_kind: str | None
    file: str | None
    function: str | None
    category: str | None
    provenance: Provenance
    canonical_emitter_conflict: ConflictRecord | None = None
    """Populated when ``_build_group`` caught a
    ``CanonicalEmitterConflictError`` while resolving this group's
    canonical emitter. When set, ``canonical_emitter`` is the
    ``_NO_CANONICAL_EMITTER`` sentinel and ``peer_emitters`` is empty;
    Stage 5b emits a synthetic ``registry_conflict`` pattern for this
    group and bypasses the Architect call. ``None`` for clean groups."""


@dataclass(frozen=True, slots=True)
class HalfRegistryAuditEntry:
    """Audit record for a finding routed via half-registry fallback."""

    timestamp: str
    finding_id: str
    subject_key: str | None
    finding_kind: str | None
    file: str | None
    function: str | None
    grouping_key: str


@dataclass(frozen=True, slots=True)
class MalformedSubjectKeyAuditEntry:
    """Audit record for a finding whose subject_key failed to parse."""

    timestamp: str
    finding_id: str
    subject_key: str
    policy: MalformedSubjectKeyPolicy
    grouping_key: str | None


# ── Audit log Protocol + implementations ─────────────────────────────


class AuditLog(Protocol):
    """Pluggable audit sink. Implementations must be append-only."""

    def record_half_registry(self, entry: HalfRegistryAuditEntry) -> None: ...

    def record_malformed_subject_key(
        self, entry: MalformedSubjectKeyAuditEntry
    ) -> None: ...


class NullAuditLog:
    """No-op audit sink (default when grouper is instantiated without
    an explicit audit log)."""

    def record_half_registry(self, entry: HalfRegistryAuditEntry) -> None:
        return

    def record_malformed_subject_key(
        self, entry: MalformedSubjectKeyAuditEntry
    ) -> None:
        return


class FileAuditLog:
    """Append-only JSONL audit sink at
    ``<state_dir>/grouper_audit.jsonl``.

    Each line is one JSON record with a ``record_type`` discriminator
    (``"half_registry"`` or ``"malformed_subject_key"``).

    Concurrency contract: each audit record is serialized to a complete
    line in memory and emitted with a single append write, guarded by the
    process-wide ``_write_lock``. Concurrent writers within the same
    process therefore cannot interleave bytes or leave partial records —
    the file stays valid newline-delimited JSON. Across separate
    processes, integrity relies on the OS append-mode (``O_APPEND``)
    atomicity of that single write for lines under the platform
    atomic-write size; a cross-process file lock is intentionally out of
    scope for this best-effort audit sink.
    """

    # Process-wide so it serializes writes even across distinct instances
    # that target the same audit path.
    _write_lock = threading.Lock()

    def __init__(self, state_dir: Path) -> None:
        self._path = Path(state_dir) / AUDIT_FILENAME
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def record_half_registry(self, entry: HalfRegistryAuditEntry) -> None:
        self._append({
            "record_type": "half_registry",
            "timestamp": entry.timestamp,
            "finding_id": entry.finding_id,
            "subject_key": entry.subject_key,
            "finding_kind": entry.finding_kind,
            "file": entry.file,
            "function": entry.function,
            "grouping_key": entry.grouping_key,
        })

    def record_malformed_subject_key(
        self, entry: MalformedSubjectKeyAuditEntry
    ) -> None:
        self._append({
            "record_type": "malformed_subject_key",
            "timestamp": entry.timestamp,
            "finding_id": entry.finding_id,
            "subject_key": entry.subject_key,
            # policy is a str Literal today, but coerce defensively so an Enum
            # (or any object) is JSON-serializable rather than raising.
            "policy": (
                entry.policy.value
                if hasattr(entry.policy, "value")
                else str(entry.policy)
            ),
            "grouping_key": entry.grouping_key,
        })

    def _append(self, record: dict[str, Any]) -> None:
        # Build the complete JSONL line in memory and emit it with exactly
        # one append write, under the process-wide lock, so concurrent
        # writers cannot interleave a record with another's bytes/newline.
        line = json.dumps(record, sort_keys=True) + "\n"
        with self._write_lock:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(line)


# ── Kind-index builder (pair-aware, Option 4) ─────────────────────────


def _build_kind_index_from(
    registry_module: ModuleType,
) -> Mapping[str, Mapping[str, tuple[PairRule, ...]]]:
    """Walk a registry module's Table 2 relationships once and produce
    a pair-aware index: ``kind -> emitter -> tuple[PairRule, ...]``.

    Private test-injection path used only when ``SemanticGrouper`` is
    constructed with an explicit ``registry_module`` (synthetic
    half-registries in the harness). The default-registry path goes
    through ``subject_key_registry.build_pair_aware_kind_index()``
    instead, which is memoised.

    Each pair rule preserves the registry's ``canonical_emitter`` value
    verbatim (including ``None`` = preserve-both). No conflict raise
    here — conflicts surface at group-resolution time only when a
    group's own contributing emitters span disagreeing pairs.
    """
    index: dict[str, dict[str, list[PairRule]]] = {}
    for rel in registry_module.iter_table_2_relationships():
        for kind in rel.shared_kinds:
            by_emitter = index.setdefault(kind, {})
            for emitter, other in (
                (rel.emitter_a, rel.emitter_b),
                (rel.emitter_b, rel.emitter_a),
            ):
                by_emitter.setdefault(emitter, []).append(
                    PairRule(
                        other_emitter=other,
                        canonical_emitter=rel.canonical_emitter,
                        consolidation=str(rel.consolidation),
                        table_label=rel.table_label,
                    )
                )
    return {
        kind: {e: tuple(rules) for e, rules in by_emitter.items()}
        for kind, by_emitter in index.items()
    }


# ── SemanticGrouper ───────────────────────────────────────────────────


def _safe_key(*parts: str | None) -> str:
    """Build a collision-safe grouping key.

    Each component is escaped so a literal '|'
    in a value cannot be mistaken for a field
    separator. Components are joined with '|'.
    """
    def _escape(s: str | None) -> str:
        return (s or "").replace("\\", "\\\\").replace("|", "\\|")
    return "|".join(_escape(p) for p in parts)


# ── Two-stage similarity grouping (file bucket + hybrid confirm) ──────
#
# Fixes the structural under-merging in the single-pass grouping_key: the same
# defect splits across groups when emitters populate function / finding_kind /
# mode differently. Stage A buckets by file alone (coarse); Stage B confirms
# same-root-cause within a bucket via a HYBRID signal —
#   description-embedding cosine >= MERGE_THRESHOLD_DEFAULT
#   OR a shared, non-empty (canonicalized) subject_key —
# reusing the same MiniLM encoder Stage 4c (concept themes) already loads. The
# subject_key OR-path catches cross-vocabulary duplicates (e.g. config.py's two
# findings, same subject_key but different finding_kind) that embedding alone
# misses. Pure functions — the encoder is injected so callers/tests control it;
# no model is loaded here.

NEAR_MISS_LOW_DEFAULT = 0.69
MERGE_THRESHOLD_DEFAULT = 0.77

# subject_key is only a valid duplicate signal when the finding's claim is ABOUT
# the subject (subject-scoped: the defect is a property of the named entity,
# e.g. a specific config value or identifier). It is NOT valid when the subject
# is merely the container the finding was found inside (container-scoped: e.g.
# "this function is too complex" is true regardless of which function).
# Confirmed via false-positive spot-check on run-012: api/client.py,
# assess/dedup.py, and reports/decision_brief.py all produced false merges when
# container-scoped finding_kinds were allowed through the subject_key path.
# The complement of this set in FINDING_KIND_REGISTRY is the 11 container-scoped
# kinds: structural_complexity, change_verification_gap, observability_gap,
# god_class, feature_envy, shotgun_surgery, primitive_obsession,
# long_parameter_list, divergent_change, data_clump, inappropriate_intimacy.
_SUBJECT_SCOPED_FINDING_KINDS: frozenset[str] = frozenset({
    # Security — the defect is a property of the named subject
    "injection_risk", "trust_boundary_gap", "authorization_gap",
    "sensitive_data_exposure", "insecure_deserialization", "crypto_misuse",
    "security_misconfiguration",
    # Correctness / behavior
    "logic_bug", "contract_violation", "error_signal_loss",
    "timeout_handling_gap", "api_contract_mismatch", "configuration_drift",
    "build_release_integrity_gap", "partial_failure_handling_gap",
    "hidden_dependency",
    # Data integrity / concurrency
    "missing_transaction_boundary", "concurrency_update_risk",
    "data_constraint_mismatch", "silent_data_loss", "idempotency_gap",
    "migration_hazard", "concurrency_safety_gap",
    # Performance / behavioral
    "algorithmic_scaling_gap", "n_plus_one_query", "blocking_io_in_async",
    "cache_correctness_gap",
})


def _finding_description(finding: Mapping[str, Any]) -> str:
    """Text compared for similarity — ``description``, then ``summary``."""
    return str(finding.get("description") or finding.get("summary") or "")


def _bucket_by_file(findings: list[dict]) -> dict[str, list[dict]]:
    """Coarse Stage A: group findings by file only (not file+function+
    finding_kind+subject_key+mode). Produces candidate clusters that MIGHT be
    duplicates, to be confirmed by Stage B."""
    buckets: dict[str, list[dict]] = defaultdict(list)
    for f in findings:
        # Fall back to the finding's own id when file is missing/falsy, so
        # unrelated file-less findings are not all collapsed into one shared
        # "" bucket (which would let Stage B consider them as merge candidates).
        buckets[str(f.get("file") or f.get("id", ""))].append(f)
    return dict(buckets)


def _aggregate_reasons(reasons: set[str]) -> str | None:
    """Collapse a set of per-edge merge reasons to one group-level reason."""
    if not reasons:
        return None
    if reasons == {"embedding"}:
        return "embedding"
    if reasons == {"subject_key"}:
        return "subject_key"
    if reasons == {"family"}:
        return "family"
    return "both"


def _confirm_similarity_within_bucket(
    bucket: list[dict],
    encoder,
    merge_threshold: float = MERGE_THRESHOLD_DEFAULT,
    near_miss_low: float = NEAR_MISS_LOW_DEFAULT,
) -> tuple[list[dict], list[dict]]:
    """Stage B: within a file bucket, merge findings via the HYBRID signal —
    description-embedding cosine >= ``merge_threshold`` OR a shared, non-empty
    (canonicalized) ``subject_key`` (which catches cross-vocabulary duplicates
    that embedding alone misses).

    Returns ``(clusters, near_miss_pairs)`` where each cluster is
    ``{"findings": [...], "merge_reason": "embedding" | "subject_key" | "both"
    | None}`` (None for single-finding clusters). Pairs that do NOT merge but
    score in ``[near_miss_low, merge_threshold)`` are recorded as near-misses
    for manual audit rather than merged or silently dropped.
    """
    if len(bucket) <= 1:
        return ([{"findings": list(bucket), "merge_reason": None}] if bucket else []), []

    import numpy as np

    descriptions = [_finding_description(f) for f in bucket]
    embeddings = np.asarray(
        encoder.encode(
            descriptions,
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        ),
        dtype=float,
    )
    subjects = [
        canonicalize_subject_key(str(f.get("subject_key") or "")) or ""
        for f in bucket
    ]
    kinds = [str(f.get("finding_kind") or "") for f in bucket]

    n = len(bucket)
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

    edges: list[tuple[int, int, str]] = []  # (i, j, reason) for merged pairs
    near_miss_pairs: list[dict] = []
    for i in range(n):
        for j in range(i + 1, n):
            sim = float(np.dot(embeddings[i], embeddings[j]))
            by_embedding = sim >= merge_threshold
            # subject_key is only a valid duplicate signal for subject-scoped
            # finding_kinds (the defect is a property of the named subject), not
            # container-scoped smells where the subject is merely where it lives.
            by_subject = (
                bool(subjects[i])
                and subjects[i] == subjects[j]
                and kinds[i] in _SUBJECT_SCOPED_FINDING_KINDS
                and kinds[j] in _SUBJECT_SCOPED_FINDING_KINDS
            )
            # Subject-key FAMILY merge: same namespace + same body prefix (the
            # subject_key minus its trailing segment, e.g. dropping the function
            # from transaction_boundary:<file>:<function>), same subject-scoped
            # finding_kind. Captures same-root-cause duplicates split only by the
            # trailing anchor segment. The ``":" in prefix`` guard requires a real
            # family prefix (namespace token + at least one body segment) — a bare
            # namespace token like ``method:foo`` (prefix ``"method"``) has none
            # and must not family-merge.
            by_family = (
                bool(subjects[i])
                and bool(subjects[j])
                and subjects[i].rsplit(":", 1)[0] == subjects[j].rsplit(":", 1)[0]
                and ":" in subjects[i].rsplit(":", 1)[0]
                and kinds[i] in _SUBJECT_SCOPED_FINDING_KINDS
                and kinds[j] in _SUBJECT_SCOPED_FINDING_KINDS
                and kinds[i] == kinds[j]
            )
            if by_embedding or by_subject or by_family:
                reason = (
                    "both" if (by_embedding and by_subject)
                    else "embedding" if by_embedding
                    else "subject_key" if by_subject
                    else "family"
                )
                _union(i, j)
                edges.append((i, j, reason))
            elif sim >= near_miss_low and not by_family:
                near_miss_pairs.append({
                    "file": bucket[i].get("file"),
                    "finding_id_a": bucket[i].get("id"),
                    "finding_id_b": bucket[j].get("id"),
                    "similarity": round(sim, 4),
                    "description_a": descriptions[i][:160],
                    "description_b": descriptions[j][:160],
                })

    members_by_root: dict[int, list[int]] = defaultdict(list)
    for idx in range(n):
        members_by_root[_find(idx)].append(idx)

    clusters: list[dict] = []
    for root, idxs in members_by_root.items():
        idxset = set(idxs)
        reasons = {r for (i, j, r) in edges if i in idxset and j in idxset}
        clusters.append({
            "findings": [bucket[i] for i in idxs],
            "merge_reason": _aggregate_reasons(reasons),
        })
    return clusters, near_miss_pairs


def two_stage_group(
    findings: list[dict],
    encoder,
    merge_threshold: float = MERGE_THRESHOLD_DEFAULT,
    near_miss_low: float = NEAR_MISS_LOW_DEFAULT,
) -> tuple[list[dict], list[dict]]:
    """Run Stage A (bucket by file) then Stage B (similarity confirm) over all
    findings. Returns ``(clusters, near_miss_pairs)``.

    Each cluster is a ``{"findings": [...], "merge_reason": "embedding" |
    "subject_key" | "both" | "family" | None}`` record (as produced by
    ``_confirm_similarity_within_bucket``), NOT a bare list of findings — the
    ``merge_reason`` metadata is consumed by ``merge_serialized_groups``. The
    annotation and docstring reflect this cluster-record shape exactly
    (FIX-BLOCKING-004 / FIX-BLOCKING-023 / FIX-REQUIRED-002).
    """
    all_clusters: list[dict] = []
    all_near: list[dict] = []
    for file_key, bucket in _bucket_by_file(findings).items():
        # Isolate encoder failures per file bucket: a single bucket whose
        # embedding fails must not abort grouping for every other file. On
        # failure the bucket's findings pass through as singleton clusters so
        # they are never silently dropped.
        try:
            clusters, near = _confirm_similarity_within_bucket(
                bucket, encoder, merge_threshold, near_miss_low
            )
        except Exception as e:  # noqa: BLE001 — per-bucket isolation is the point
            log.warning("Encoder failed for bucket %r: %s", file_key, e)
            all_clusters.extend(
                {"findings": [f], "merge_reason": None} for f in bucket
            )
            continue
        all_clusters.extend(clusters)
        all_near.extend(near)
    return all_clusters, all_near


def merge_serialized_groups(
    serialized_groups: list[dict], clusters: list[dict]
) -> list[dict]:
    """Merge serialized semantic-group dicts whose member findings co-occur in a
    Stage-B similarity cluster (size > 1). The merged group keeps the
    representative (lowest ``group_id``) group's metadata and unions
    ``member_finding_ids`` / ``member_count`` / ``contributing_emitters``;
    severity becomes the max across merged groups, and ``merge_reason``
    (``embedding`` | ``subject_key`` | ``both``) records which signal(s) drove
    the merge. Singleton groups pass through unchanged. Stable +
    grouping_key-preserving so nothing downstream (Fix Planner / patterns /
    dedup) breaks.

    ``clusters`` are the ``{"findings": [...], "merge_reason": ...}`` dicts
    produced by ``two_stage_group`` / ``_confirm_similarity_within_bucket``.
    """
    fid_to_cluster: dict[str, int] = {}
    cluster_reason: dict[int, str | None] = {}
    for ci, cl in enumerate(clusters):
        findings = cl.get("findings") if isinstance(cl, dict) else cl
        if findings and len(findings) > 1:  # only multi-finding clusters merge
            cluster_reason[ci] = cl.get("merge_reason") if isinstance(cl, dict) else None
            for f in findings:
                fid_to_cluster[str(f.get("id"))] = ci

    n = len(serialized_groups)
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

    cluster_to_groups: dict[int, list[int]] = defaultdict(list)
    for gi, g in enumerate(serialized_groups):
        for fid in (g.get("member_finding_ids") or []):
            ci = fid_to_cluster.get(str(fid))
            if ci is not None and gi not in cluster_to_groups[ci]:
                cluster_to_groups[ci].append(gi)
    for gis in cluster_to_groups.values():
        for k in range(1, len(gis)):
            _union(gis[0], gis[k])

    # Which signal(s) drove each merged group-component.
    comp_reasons: dict[int, set[str]] = defaultdict(set)
    for ci, gis in cluster_to_groups.items():
        if len(gis) > 1:
            reason = cluster_reason.get(ci)
            if reason:
                comp_reasons[_find(gis[0])].add(reason)

    components: dict[int, list[int]] = defaultdict(list)
    for gi in range(n):
        components[_find(gi)].append(gi)

    merged: list[dict] = []
    for root, members in components.items():
        if len(members) == 1:
            merged.append(serialized_groups[members[0]])
            continue
        rep_order = sorted(members, key=lambda gi: str(serialized_groups[gi].get("group_id")))
        rep = dict(serialized_groups[rep_order[0]])
        all_fids: list[str] = []
        all_emit: list[str] = []
        for gi in members:
            g = serialized_groups[gi]
            all_fids.extend(g.get("member_finding_ids") or [])
            all_emit.extend(g.get("contributing_emitters") or [])
        rep["member_finding_ids"] = list(dict.fromkeys(all_fids))
        rep["member_count"] = len(rep["member_finding_ids"])
        rep["contributing_emitters"] = list(dict.fromkeys(all_emit))
        rep["severity"] = max(
            (serialized_groups[gi].get("severity") for gi in members),
            key=lambda s: _SEVERITY_RANK.get(s, 0),
        )
        rep["merged_from_group_ids"] = sorted(
            str(serialized_groups[gi].get("group_id")) for gi in members
        )
        rep["merge_reason"] = _aggregate_reasons(comp_reasons.get(root, set())) or "embedding"
        merged.append(rep)
    return merged


class SemanticGrouper:
    """Group deduped findings into SemanticGroups.

    One instance is reusable across multiple ``group()`` calls within a
    process. The pair-aware kind index is built eagerly at construction
    time (cheap; the underlying registry is immutable).
    """

    def __init__(
        self,
        registry_module: ModuleType | None = None,
        audit_log: AuditLog | None = None,
        malformed_subject_key_policy: MalformedSubjectKeyPolicy = "route_to_legacy",
        unknown_finding_kind_policy: UnknownFindingKindPolicy = "fail_fast",
    ) -> None:
        self._audit = audit_log or NullAuditLog()
        self._malformed_policy: MalformedSubjectKeyPolicy = (
            malformed_subject_key_policy
        )
        self._unknown_kind_policy: UnknownFindingKindPolicy = (
            unknown_finding_kind_policy
        )
        # Pair-aware index: kind -> emitter -> tuple[PairRule, ...].
        # Default-registry path is memoised via
        # subject_key_registry.build_pair_aware_kind_index() (the
        # underlying TABLE_2_RELATIONSHIPS is immutable at runtime).
        # Test-injection path with an explicit registry_module bypasses
        # the cache via the private _build_kind_index_from helper.
        self._kind_index: Mapping[str, Mapping[str, tuple[PairRule, ...]]] = (
            _build_kind_index_from(registry_module)
            if registry_module is not None
            else build_pair_aware_kind_index()
        )

    # -- Public entrypoint --------------------------------------------

    def group(
        self, deduped_findings: Iterable[Mapping[str, Any]]
    ) -> list[SemanticGroup]:
        """Bucket findings into SemanticGroups, sorted by severity
        (desc) then group_id (asc)."""
        buckets: dict[str, list[Mapping[str, Any]]] = {}
        bucket_meta: dict[str, tuple[Mode, Mapping[str, Any]]] = {}

        for finding in deduped_findings:
            mode, key, parsed = self._classify_and_key(finding)
            if key is None:
                # Malformed subject_key under fail_fast policy already
                # raised inside _classify_and_key. Skip defensively.
                continue
            buckets.setdefault(key, []).append(finding)
            bucket_meta.setdefault(key, (mode, parsed))

        groups: list[SemanticGroup] = []
        for key, members in buckets.items():
            mode, parsed = bucket_meta[key]
            groups.append(self._build_group(key, mode, parsed, members))

        return self._sort_groups(groups)

    # -- Classification + keying --------------------------------------

    def _classify_and_key(
        self, finding: Mapping[str, Any]
    ) -> tuple[Mode, str | None, Mapping[str, Any]]:
        """Decide which mode this finding falls into and return its
        grouping key plus a parsed-fields dict used by ``_build_group``.

        Returns ``(mode, grouping_key, parsed)``. ``grouping_key`` is
        ``None`` only when the finding was dropped (malformed under a
        permissive policy that's not currently exposed); under
        ``"fail_fast"`` this raises rather than returns ``None``.
        """
        subject_key = canonicalize_subject_key(
            str(finding.get("subject_key") or "")
        ) or None
        finding_kind = finding.get("finding_kind")
        file_ = finding.get("file")
        function = finding.get("function")
        category = finding.get("category")
        finding_id = str(finding.get("id") or "")

        # Unknown-finding-kind gate (only applies when the finding
        # actually declares a kind). Absence of finding_kind routes to
        # half_registry or legacy below; absence is not "unknown".
        if finding_kind is not None and finding_kind != "":
            if not self._check_finding_kind_known(finding_id, finding_kind):
                # form_empty_group: drop the kind from grouping inputs.
                finding_kind = None

        has_subject = bool(subject_key)
        has_kind = bool(finding_kind)

        if has_subject and has_kind:
            parsed_subject = self._parse_subject_key(finding_id, subject_key)
            if parsed_subject is None:
                # Malformed under route_to_legacy policy — fall through
                # to legacy keying. The audit entry was already
                # recorded in _parse_subject_key.
                key = self._legacy_key(file_, function, category)
                return (
                    "legacy",
                    key,
                    {
                        "subject_key": None,
                        "finding_kind": None,
                        "file": file_,
                        "function": function,
                        "category": category,
                    },
                )
            namespace_token, body = parsed_subject
            key = f"registry|{subject_key}|{finding_kind}"
            return (
                "registry",
                key,
                {
                    "subject_key": subject_key,
                    "finding_kind": finding_kind,
                    "file": file_,
                    "function": function,
                    "category": category,
                    "namespace_token": namespace_token,
                    "body": body,
                },
            )

        if has_kind or has_subject:
            # Half-registry: one of the two registry-shape fields is
            # present. Key by (file, function, finding_kind-or-subject).
            descriminator = finding_kind if has_kind else subject_key
            key = _safe_key("half", file_, function, descriminator)
            self._audit.record_half_registry(
                HalfRegistryAuditEntry(
                    timestamp=_utcnow_iso(),
                    finding_id=finding_id,
                    subject_key=subject_key if has_subject else None,
                    finding_kind=finding_kind if has_kind else None,
                    file=file_,
                    function=function,
                    grouping_key=key,
                )
            )
            return (
                "half_registry",
                key,
                {
                    "subject_key": subject_key if has_subject else None,
                    "finding_kind": finding_kind if has_kind else None,
                    "file": file_,
                    "function": function,
                    "category": category,
                },
            )

        # Legacy.
        key = self._legacy_key(file_, function, category)
        return (
            "legacy",
            key,
            {
                "subject_key": None,
                "finding_kind": None,
                "file": file_,
                "function": function,
                "category": category,
            },
        )

    def _parse_subject_key(
        self, finding_id: str, subject_key: str
    ) -> tuple[str, str] | None:
        """Parse ``<token>:<body>``. Returns ``(token_with_colon, body)``
        on success; ``None`` on malformed under ``route_to_legacy``
        policy; raises ``MalformedSubjectKeyError`` under ``fail_fast``.
        """
        idx = subject_key.find(":")
        if idx <= 0 or idx == len(subject_key) - 1:
            # Either no colon, leading colon, or trailing colon with
            # empty body.
            if self._malformed_policy == "fail_fast":
                raise MalformedSubjectKeyError(finding_id, subject_key)
            self._audit.record_malformed_subject_key(
                MalformedSubjectKeyAuditEntry(
                    timestamp=_utcnow_iso(),
                    finding_id=finding_id,
                    subject_key=subject_key,
                    policy=self._malformed_policy,
                    grouping_key=None,
                )
            )
            return None
        token = subject_key[: idx + 1]  # include trailing colon
        body = subject_key[idx + 1 :]
        return token, body

    @staticmethod
    def _legacy_key(
        file_: str | None, function: str | None, category: str | None
    ) -> str:
        return _safe_key("legacy", file_, function, category)

    def _check_finding_kind_known(
        self, finding_id: str, finding_kind: str
    ) -> bool:
        """Return ``True`` if the kind is registered in
        ``finding_kinds.py``. Under ``fail_fast`` policy, raises
        ``UnknownFindingKindError`` instead of returning ``False``.
        Table 2 absence is a separate concern handled in
        ``_resolve_canonical_and_peers``.
        """
        if get_finding_kind_definition(finding_kind) is not None:
            return True
        if self._unknown_kind_policy == "fail_fast":
            raise UnknownFindingKindError(finding_id, finding_kind)
        return False

    # -- Group construction -------------------------------------------

    def _build_group(
        self,
        grouping_key: str,
        mode: Mode,
        parsed: Mapping[str, Any],
        members: list[Mapping[str, Any]],
    ) -> SemanticGroup:
        finding_kind = parsed.get("finding_kind")
        member_ids = tuple(sorted(str(m.get("id") or "") for m in members))
        contributing = self._collect_contributing_emitters(members)
        conflict_record: ConflictRecord | None = None
        if finding_kind:
            try:
                canonical, peers = self._resolve_canonical_and_peers(
                    finding_kind, contributing
                )
            except CanonicalEmitterConflictError as exc:
                # Per-group quarantine: capture the conflict on the
                # group rather than propagating to Stage 4b. Healthy
                # groups in the same batch proceed normally; Stage 5b
                # emits a synthetic ``registry_conflict`` pattern for
                # the quarantined group and bypasses the Architect.
                canonical = _NO_CANONICAL_EMITTER
                peers = ()
                conflict_record = ConflictRecord(
                    finding_kind=exc.finding_kind,
                    contributing_emitters=exc.contributing_emitters,
                    conflicting_canonicals=exc.conflicting_canonicals,
                )
        else:
            canonical, peers = (_NO_CANONICAL_EMITTER, ())
        severity = self._aggregate_severity(members)
        group_id = hashlib.sha256(grouping_key.encode("utf-8")).hexdigest()[:12]

        provenance = Provenance(
            mode=mode,
            grouping_key=grouping_key,
            subject_key=parsed.get("subject_key"),
            finding_kind=finding_kind,
            file=parsed.get("file"),
            function=parsed.get("function"),
            category=parsed.get("category"),
        )

        return SemanticGroup(
            group_id=group_id,
            grouping_key=grouping_key,
            mode=mode,
            severity=severity,
            member_count=len(members),
            member_finding_ids=member_ids,
            contributing_emitters=contributing,
            canonical_emitter=canonical,
            peer_emitters=peers,
            subject_key=parsed.get("subject_key"),
            finding_kind=finding_kind,
            file=parsed.get("file"),
            function=parsed.get("function"),
            category=parsed.get("category"),
            provenance=provenance,
            canonical_emitter_conflict=conflict_record,
        )

    def _resolve_canonical_and_peers(
        self,
        finding_kind: str,
        contributing_emitters: tuple[str, ...],
    ) -> tuple[str, tuple[str, ...]]:
        """Resolve ``(canonical_emitter, peer_emitters)`` for this
        group via the pair-aware index.

        Honest to the registry's pair shape: canonical is determined
        by the Table 2 pairs whose two emitters both contributed to
        this group, not by an aggregate across the kind. Peers are
        the *potential* peer set per the registry (every emitter that
        peers on this kind with any of the group's contributing
        emitters), so the Architect re-entry can see who *should*
        have something to say even if they didn't contribute here.
        """
        by_emitter = self._kind_index.get(finding_kind, {})
        if not by_emitter:
            # Kind valid but has no Table 2 relationships.
            return (_NO_CANONICAL_EMITTER, ())

        relevant_rules: list[PairRule] = []
        for emitter in contributing_emitters:
            relevant_rules.extend(by_emitter.get(emitter, ()))

        if not relevant_rules:
            # Kind has Table 2 entries, but none involve any
            # contributing emitter of this group.
            return (_NO_CANONICAL_EMITTER, ())

        contributing_set = set(contributing_emitters)
        present_pair_rules = [
            r for r in relevant_rules
            if r.other_emitter in contributing_set
        ]
        canonical = self._select_canonical(
            finding_kind, contributing_emitters, present_pair_rules
        )
        peers = self._collect_peers(relevant_rules)
        return (canonical, peers)

    @staticmethod
    def _select_canonical(
        finding_kind: str,
        contributing_emitters: tuple[str, ...],
        present_pair_rules: list[PairRule],
    ) -> str:
        """Select canonical_emitter from the pair rules whose *both*
        emitters contributed to this group.

        Tolerates ``None`` (preserve-both) alongside a single named
        canonical. Raises ``CanonicalEmitterConflictError`` only when
        two or more distinct named canonicals appear across the pairs
        actually present in this group's data.
        """
        if not present_pair_rules:
            return _NO_CANONICAL_EMITTER

        canonicals = {r.canonical_emitter for r in present_pair_rules}
        if canonicals == {None}:
            # Every present pair says preserve-both.
            return _NO_CANONICAL_EMITTER

        canonicals.discard(None)
        if len(canonicals) == 1:
            return next(iter(canonicals))

        raise CanonicalEmitterConflictError(
            finding_kind=finding_kind,
            contributing_emitters=contributing_emitters,
            conflicting_canonicals=tuple(sorted(canonicals)),
        )

    @staticmethod
    def _collect_peers(
        relevant_rules: list[PairRule],
    ) -> tuple[str, ...]:
        """Sorted, deduped set of every ``other_emitter`` across the
        rules involving this group's contributing emitters."""
        peers = {r.other_emitter for r in relevant_rules}
        return tuple(sorted(peers))

    @staticmethod
    def _collect_contributing_emitters(
        members: list[Mapping[str, Any]],
    ) -> tuple[str, ...]:
        """Union of ``layers`` across all members, deduped and sorted.

        ``layers`` is dedup's consolidation-history stamp — the right
        field for "every emitter that touched this finding" semantics.
        ``skill`` (dedup's primary survivor) is what ``_resolve_emitter``
        consults; the two helpers measure different things.
        """
        collected: set[str] = set()
        for m in members:
            layers = m.get("layers") or ()
            for entry in layers:
                if entry:
                    collected.add(str(entry))
            # Fall back to skill when layers is empty — single-emitter
            # finding that dedup didn't stamp.
            if not layers:
                skill = _resolve_emitter(m)
                if skill:
                    collected.add(skill)
        return tuple(sorted(collected))

    @staticmethod
    def _aggregate_severity(members: list[Mapping[str, Any]]) -> str:
        """Group severity = max member severity."""
        best_rank = -1
        best = "P3"
        for m in members:
            sev = str(m.get("severity") or "P3")
            rank = _SEVERITY_RANK.get(sev, 0)
            if rank > best_rank:
                best_rank = rank
                best = sev
        return best

    @staticmethod
    def _sort_groups(groups: list[SemanticGroup]) -> list[SemanticGroup]:
        """Sort by severity (desc) then group_id (asc) for determinism."""
        return sorted(
            groups,
            key=lambda g: (
                -_SEVERITY_RANK.get(g.severity, 0),
                g.group_id,
            ),
        )


# ── Module-level helpers ──────────────────────────────────────────────


def _utcnow_iso() -> str:
    """UTC timestamp in ISO 8601 with trailing ``Z`` (no microseconds)."""
    return (
        datetime.now(tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _resolve_emitter(finding: Mapping[str, Any]) -> str:
    """Return dedup's chosen primary-survivor emitter.

    Prefers ``skill`` (dedup's primary-survivor selection) over
    ``layers`` (which is arrival-ordered, not canonical-ordered, and
    therefore not a reliable identity claim).
    """
    return str(finding.get("skill") or "")


def _serialize_group(group: SemanticGroup) -> dict[str, Any]:
    return {
        "group_id": group.group_id,
        "grouping_key": group.grouping_key,
        "mode": group.mode,
        "severity": group.severity,
        "member_count": group.member_count,
        "member_finding_ids": list(group.member_finding_ids),
        "contributing_emitters": list(group.contributing_emitters),
        "canonical_emitter": group.canonical_emitter,
        "peer_emitters": list(group.peer_emitters),
        "subject_key": group.subject_key,
        "finding_kind": group.finding_kind,
        "file": group.file,
        "function": group.function,
        "category": group.category,
        "provenance": {
            "mode": group.provenance.mode,
            "grouping_key": group.provenance.grouping_key,
            "subject_key": group.provenance.subject_key,
            "finding_kind": group.provenance.finding_kind,
            "file": group.provenance.file,
            "function": group.provenance.function,
            "category": group.provenance.category,
        },
        "canonical_emitter_conflict": (
            group.canonical_emitter_conflict.to_dict()
            if group.canonical_emitter_conflict is not None
            else None
        ),
    }


def _deserialize_group(payload: Mapping[str, Any]) -> SemanticGroup:
    prov_raw = payload.get("provenance") or {}
    provenance = Provenance(
        mode=prov_raw.get("mode", payload.get("mode", "legacy")),
        grouping_key=prov_raw.get("grouping_key", payload.get("grouping_key", "")),
        subject_key=prov_raw.get("subject_key"),
        finding_kind=prov_raw.get("finding_kind"),
        file=prov_raw.get("file"),
        function=prov_raw.get("function"),
        category=prov_raw.get("category"),
    )
    conflict_data = payload.get("canonical_emitter_conflict")
    if conflict_data:
        conflict = ConflictRecord(
            finding_kind=conflict_data["finding_kind"],
            contributing_emitters=tuple(
                conflict_data.get("contributing_emitters") or ()
            ),
            conflicting_canonicals=tuple(
                conflict_data.get("conflicting_canonicals") or ()
            ),
        )
    else:
        conflict = None
    return SemanticGroup(
        group_id=payload["group_id"],
        grouping_key=payload["grouping_key"],
        mode=payload["mode"],
        severity=payload["severity"],
        member_count=payload["member_count"],
        member_finding_ids=tuple(payload.get("member_finding_ids") or ()),
        contributing_emitters=tuple(payload.get("contributing_emitters") or ()),
        canonical_emitter=payload.get("canonical_emitter", _NO_CANONICAL_EMITTER),
        peer_emitters=tuple(payload.get("peer_emitters") or ()),
        subject_key=payload.get("subject_key"),
        finding_kind=payload.get("finding_kind"),
        file=payload.get("file"),
        function=payload.get("function"),
        category=payload.get("category"),
        provenance=provenance,
        canonical_emitter_conflict=conflict,
    )


def serialize_groups(groups: Iterable[SemanticGroup]) -> list[dict]:
    """Serialize an iterable of ``SemanticGroup`` records to the
    canonical list-of-dicts shape for ``StateStore`` persistence.

    Consumers: orchestrator ``main.py`` (Stage 4b write path) calls
    this immediately before ``StateStore.write("semantic_groups",
    payload, sort_keys=True)`` so the assess track owns the dict
    shape and the orchestrator track owns the persistence path
    (cycle 20 sub-step 2).
    """
    return [_serialize_group(g) for g in groups]


def read_groups(state_dir: Path) -> list[SemanticGroup]:
    """Read groups previously written via the orchestrator/StateStore.

    Returns ``[]`` if the file does not exist.
    """
    path = Path(state_dir) / GROUPS_FILENAME
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        return []
    groups: list[SemanticGroup] = []
    for entry in payload:
        # A single malformed group entry (missing required key / wrong type)
        # must not abort the whole read and drop every well-formed group.
        try:
            groups.append(_deserialize_group(entry))
        except (KeyError, TypeError) as e:
            log.warning("Skipping malformed group: %s", e)
            continue
    return groups


__all__ = [
    "AUDIT_FILENAME",
    "AuditLog",
    "CanonicalEmitterConflictError",
    "ConflictRecord",
    "FileAuditLog",
    "GROUPS_FILENAME",
    "GrouperError",
    "HalfRegistryAuditEntry",
    "MalformedSubjectKeyAuditEntry",
    "MalformedSubjectKeyError",
    "MalformedSubjectKeyPolicy",
    "Mode",
    "NullAuditLog",
    "Provenance",
    "SemanticGroup",
    "SemanticGrouper",
    "UnknownFindingKindError",
    "UnknownFindingKindPolicy",
    "read_groups",
    "serialize_groups",
]
