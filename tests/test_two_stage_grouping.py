"""Tests for the two-stage hybrid semantic grouping (file bucket + embedding/
subject_key confirm)."""
from __future__ import annotations

import math

import numpy as np

from sentinel.assess.grouper import (
    _bucket_by_file,
    _confirm_similarity_within_bucket,
    merge_serialized_groups,
    two_stage_group,
)


class _VecEncoder:
    """Mock encoder: returns a predetermined unit vector per description text,
    so within-pair cosine similarity is exactly controllable in tests."""

    def __init__(self, mapping: dict[str, list[float]]):
        self._m = mapping

    def encode(self, texts, convert_to_numpy=True, normalize_embeddings=True,
               show_progress_bar=False):
        vecs = np.array([self._m[t] for t in texts], dtype=float)
        if normalize_embeddings:
            norms = np.linalg.norm(vecs, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            vecs = vecs / norms
        return vecs


def _unit(cos: float) -> list[float]:
    """2-D unit vector whose cosine with [1,0] is exactly ``cos``."""
    return [cos, math.sqrt(max(0.0, 1.0 - cos * cos))]


# --- 1. coarse bucketing ---

def test_bucket_by_file_groups_same_file_separates_different():
    findings = [
        {"id": "a", "file": "x.py"},
        {"id": "b", "file": "x.py"},
        {"id": "c", "file": "y.py"},
    ]
    buckets = _bucket_by_file(findings)
    assert set(buckets) == {"x.py", "y.py"}
    assert [f["id"] for f in buckets["x.py"]] == ["a", "b"]
    assert [f["id"] for f in buckets["y.py"]] == ["c"]


# --- 2/3/4/6. within-bucket similarity confirmation ---

def test_confirm_merges_high_similarity_pair_via_embedding():
    bucket = [
        {"id": "a", "file": "x.py", "description": "alpha"},
        {"id": "b", "file": "x.py", "description": "beta"},
    ]
    enc = _VecEncoder({"alpha": [1, 0], "beta": _unit(0.95)})  # >= 0.78
    clusters, near = _confirm_similarity_within_bucket(bucket, enc)
    assert len(clusters) == 1 and len(clusters[0]["findings"]) == 2
    assert clusters[0]["merge_reason"] == "embedding"
    assert near == []


def test_confirm_does_not_merge_low_similarity():
    bucket = [
        {"id": "a", "file": "x.py", "description": "alpha"},
        {"id": "b", "file": "x.py", "description": "gamma"},
    ]
    enc = _VecEncoder({"alpha": [1, 0], "gamma": _unit(0.30)})
    clusters, near = _confirm_similarity_within_bucket(bucket, enc)
    assert len(clusters) == 2              # not merged
    assert all(c["merge_reason"] is None for c in clusters)
    assert near == []                      # below near_miss_low (0.70)


def test_confirm_records_near_miss_band_without_merging():
    bucket = [
        {"id": "a", "file": "x.py", "description": "alpha"},
        {"id": "b", "file": "x.py", "description": "delta"},
    ]
    enc = _VecEncoder({"alpha": [1, 0], "delta": _unit(0.75)})  # in [0.70, 0.78)
    clusters, near = _confirm_similarity_within_bucket(bucket, enc)
    assert len(clusters) == 2              # 0.75 < 0.78 merge threshold
    assert len(near) == 1
    assert near[0]["finding_id_a"] == "a" and near[0]["finding_id_b"] == "b"
    assert 0.70 <= near[0]["similarity"] < 0.78


def test_confirm_single_finding_bucket_is_noop():
    bucket = [{"id": "a", "file": "x.py", "description": "alpha"}]
    enc = _VecEncoder({"alpha": [1, 0]})
    clusters, near = _confirm_similarity_within_bucket(bucket, enc)
    assert clusters == [{"findings": bucket, "merge_reason": None}]
    assert near == []


# --- subject_key OR-path (the hybrid's reason for existing) ---

def test_confirm_merges_low_cosine_when_subject_scoped_subject_key_matches():
    # subject-scoped finding_kind (security) + shared subject_key -> merge even
    # at low cosine (the config.py rescue case).
    bucket = [
        {"id": "a", "file": "config.py", "description": "alpha",
         "subject_key": "Config.openai_api_key", "finding_kind": "sensitive_data_exposure"},
        {"id": "b", "file": "config.py", "description": "totally different words",
         "subject_key": "Config.openai_api_key", "finding_kind": "contract_violation"},
    ]
    enc = _VecEncoder({"alpha": [1, 0], "totally different words": _unit(0.50)})
    clusters, near = _confirm_similarity_within_bucket(bucket, enc)
    assert len(clusters) == 1 and len(clusters[0]["findings"]) == 2  # merged
    assert clusters[0]["merge_reason"] == "subject_key"             # via OR-path
    assert near == []  # subject_key match -> merged, not a near-miss


def test_confirm_blocks_subject_key_merge_for_container_scoped_kind():
    # The run-012 false positive: same subject_key but a CONTAINER-scoped
    # finding_kind (complexity smell) must NOT merge via the subject_key path.
    # Cosine in the near-miss band -> recorded as a near-miss instead.
    bucket = [
        {"id": "a", "file": "x.py", "description": "alpha",
         "subject_key": "X._post_with_retry", "finding_kind": "structural_complexity"},
        {"id": "b", "file": "x.py", "description": "different smell",
         "subject_key": "X._post_with_retry", "finding_kind": "structural_complexity"},
    ]
    enc = _VecEncoder({"alpha": [1, 0], "different smell": _unit(0.72)})
    clusters, near = _confirm_similarity_within_bucket(bucket, enc)
    assert len(clusters) == 2          # container-scoped: subject_key path blocked
    assert len(near) == 1              # 0.72 < 0.78 -> recorded as near-miss


def test_confirm_reason_is_both_when_embedding_and_subject_key():
    bucket = [
        {"id": "a", "file": "x.py", "description": "alpha",
         "subject_key": "S.k", "finding_kind": "logic_bug"},
        {"id": "b", "file": "x.py", "description": "beta",
         "subject_key": "S.k", "finding_kind": "logic_bug"},
    ]
    enc = _VecEncoder({"alpha": [1, 0], "beta": _unit(0.95)})  # high cosine too
    clusters, _near = _confirm_similarity_within_bucket(bucket, enc)
    assert len(clusters) == 1
    assert clusters[0]["merge_reason"] == "both"


def test_subject_scoped_set_is_consistent_with_registry():
    from sentinel.assess.finding_kinds import FINDING_KIND_REGISTRY
    from sentinel.assess.grouper import _SUBJECT_SCOPED_FINDING_KINDS
    registry = {e.kind_name for e in FINDING_KIND_REGISTRY}
    assert _SUBJECT_SCOPED_FINDING_KINDS <= registry          # no typos
    assert len(_SUBJECT_SCOPED_FINDING_KINDS) == 27
    assert len(registry - _SUBJECT_SCOPED_FINDING_KINDS) == 11


# --- orchestrator across files ---

def test_two_stage_group_aggregates_across_files():
    findings = [
        {"id": "a", "file": "x.py", "description": "alpha"},
        {"id": "b", "file": "x.py", "description": "beta"},   # x.py merges
        {"id": "c", "file": "y.py", "description": "gamma"},
        {"id": "d", "file": "y.py", "description": "delta"},  # y.py splits
    ]
    enc = _VecEncoder({
        "alpha": [1, 0], "beta": _unit(0.95),
        "gamma": [1, 0], "delta": _unit(0.20),
    })
    clusters, _near = two_stage_group(findings, enc)
    assert sorted(len(c["findings"]) for c in clusters) == [1, 1, 2]


def test_two_stage_group_returns_declared_cluster_record_shape():
    # FIX-BLOCKING-004/023 / FIX-REQUIRED-002: the first element must match the
    # declared tuple[list[dict], list[dict]] contract — cluster *records* with
    # a "findings" list and a "merge_reason" key, not bare finding lists.
    findings = [
        {"id": "a", "file": "x.py", "description": "alpha"},
        {"id": "b", "file": "x.py", "description": "beta"},
    ]
    enc = _VecEncoder({"alpha": [1, 0], "beta": _unit(0.95)})
    clusters, _near = two_stage_group(findings, enc)
    assert clusters, "expected at least one cluster"
    for cluster in clusters:
        assert isinstance(cluster, dict)
        assert "findings" in cluster
        assert "merge_reason" in cluster
        assert isinstance(cluster["findings"], list)


# --- merging serialized groups by cluster ---

def test_merge_serialized_groups_unions_clustered_groups():
    groups = [
        {"group_id": "g1", "grouping_key": "k1", "severity": "P1",
         "member_finding_ids": ["a"], "contributing_emitters": ["e1"]},
        {"group_id": "g2", "grouping_key": "k2", "severity": "Critical",
         "member_finding_ids": ["b"], "contributing_emitters": ["e2"]},
    ]
    clusters = [{"findings": [{"id": "a"}, {"id": "b"}], "merge_reason": "subject_key"}]
    merged = merge_serialized_groups(groups, clusters)
    assert len(merged) == 1
    m = merged[0]
    assert set(m["member_finding_ids"]) == {"a", "b"}
    assert m["member_count"] == 2
    assert m["severity"] == "Critical"          # max across merged
    assert m["group_id"] == "g1"                # representative = lowest group_id
    assert set(m["contributing_emitters"]) == {"e1", "e2"}
    assert m["merged_from_group_ids"] == ["g1", "g2"]
    assert m["merge_reason"] == "subject_key"   # propagated from the cluster


def test_merge_serialized_groups_leaves_singletons_untouched():
    groups = [
        {"group_id": "g1", "member_finding_ids": ["a"], "severity": "P1"},
        {"group_id": "g2", "member_finding_ids": ["b"], "severity": "P2"},
    ]
    clusters = [
        {"findings": [{"id": "a"}], "merge_reason": None},
        {"findings": [{"id": "b"}], "merge_reason": None},
    ]
    merged = merge_serialized_groups(groups, clusters)
    assert len(merged) == 2
    assert all("merge_reason" not in g for g in merged)  # untouched singletons


# --- end-to-end: the 5 confirmed run-012 Critical duplicate pairs ---

def test_end_to_end_critical_pairs_4_embedding_merges_config_near_miss():
    # Per-pair fixture cosine: 4 clear duplicates >= 0.78; config.py kept below
    # 0.78 (and without a matching subject_key here) so it exercises the
    # near-miss path — the dedicated subject_key OR-path test above covers the
    # real config.py rescue mechanism.
    pairs = {
        "agents/coder.py": (
            "Unbound test_results on retry setup failure",
            "Unbound test_results after retry setup failure", 0.97),
        "workflow/remediation/test_runner.py": (
            "Untrusted test command is executed through the shell",
            "Untrusted test command is run through the shell", 1.0),
        "phase3/compat_checker.py": (
            "FAIL verdict is treated as PASS when no BREAKING violations are present",
            "FAIL verdict is treated as success when no BREAKING violations exist", 0.95),
        "chat/session.py": (
            "ChatSession enables self-approval despite architect no-self-approve contract",
            "ChatSession defaults to self-approval in a chat-facing workflow", 0.90),
        "config.py": (
            "Production default API key silently enables invalid credentials",
            "Hardcoded default API key masks missing credential state", 0.74),
    }
    findings: list[dict] = []
    vecmap: dict[str, list[float]] = {}
    for i, (file, (da, db, cos)) in enumerate(pairs.items()):
        findings.append({"id": f"{i}a", "file": file, "description": da})
        findings.append({"id": f"{i}b", "file": file, "description": db})
        vecmap[da] = [1.0, 0.0]
        vecmap[db] = _unit(cos)
    enc = _VecEncoder(vecmap)

    clusters, near = two_stage_group(findings, enc)
    merged_pairs = sum(1 for c in clusters if len(c["findings"]) == 2)
    assert merged_pairs == 4                              # 4 embedding merges
    assert any(nm["file"] == "config.py" for nm in near)  # config.py near-miss


# --- CHANGE 1: subject-key family merge (by_family) ---

def test_family_merge_same_namespace_and_file_different_function():
    # transaction_boundary:<file>:<function> — same namespace+file, different
    # function, same subject-scoped kind, cosine below threshold -> merge by family.
    bucket = [
        {"id": "a", "file": "task_manager.py", "description": "alpha",
         "subject_key": "transaction_boundary:task_manager.py:create_pr",
         "finding_kind": "missing_transaction_boundary"},
        {"id": "b", "file": "task_manager.py", "description": "beta",
         "subject_key": "transaction_boundary:task_manager.py:record_qa_verdict",
         "finding_kind": "missing_transaction_boundary"},
    ]
    enc = _VecEncoder({"alpha": [1, 0], "beta": _unit(0.50)})  # well below 0.77
    clusters, near = _confirm_similarity_within_bucket(bucket, enc)
    assert len(clusters) == 1 and len(clusters[0]["findings"]) == 2  # merged by family
    assert near == []                                               # not a near-miss


def test_family_no_merge_across_different_namespaces():
    # Same file+function tail but DIFFERENT namespace token -> different family
    # prefix -> must NOT merge via by_family.
    bucket = [
        {"id": "a", "file": "task_manager.py", "description": "alpha",
         "subject_key": "transaction_boundary:task_manager.py:create_pr",
         "finding_kind": "missing_transaction_boundary"},
        {"id": "b", "file": "task_manager.py", "description": "beta",
         "subject_key": "catch_site:task_manager.py:create_pr",
         "finding_kind": "missing_transaction_boundary"},
    ]
    enc = _VecEncoder({"alpha": [1, 0], "beta": _unit(0.50)})
    clusters, _near = _confirm_similarity_within_bucket(bucket, enc)
    assert len(clusters) == 2  # different family prefixes -> not merged


def test_family_no_merge_when_no_trailing_segment():
    # method:foo / method:bar — the family prefix would be the bare namespace
    # token ("method"), which has no colon -> no family to merge on.
    bucket = [
        {"id": "a", "file": "x.py", "description": "alpha",
         "subject_key": "method:foo", "finding_kind": "logic_bug"},
        {"id": "b", "file": "x.py", "description": "beta",
         "subject_key": "method:bar", "finding_kind": "logic_bug"},
    ]
    enc = _VecEncoder({"alpha": [1, 0], "beta": _unit(0.50)})
    clusters, _near = _confirm_similarity_within_bucket(bucket, enc)
    assert len(clusters) == 2  # bare namespace token -> not merged


def test_family_no_merge_across_different_finding_kinds():
    # Same family prefix but DIFFERENT finding_kind -> must NOT family-merge.
    bucket = [
        {"id": "a", "file": "task_manager.py", "description": "alpha",
         "subject_key": "transaction_boundary:task_manager.py:create_pr",
         "finding_kind": "missing_transaction_boundary"},
        {"id": "b", "file": "task_manager.py", "description": "beta",
         "subject_key": "transaction_boundary:task_manager.py:record_qa_verdict",
         "finding_kind": "logic_bug"},
    ]
    enc = _VecEncoder({"alpha": [1, 0], "beta": _unit(0.50)})
    clusters, _near = _confirm_similarity_within_bucket(bucket, enc)
    assert len(clusters) == 2  # kind mismatch -> not merged


# --- CHANGE 2: merge threshold 0.78 -> 0.77, near-miss floor 0.70 -> 0.69 ---

def test_threshold_0771_now_merges():
    # 0.771 is >= the new 0.77 merge threshold (would have been a near-miss at 0.78).
    bucket = [
        {"id": "a", "file": "x.py", "description": "alpha"},
        {"id": "b", "file": "x.py", "description": "beta"},
    ]
    enc = _VecEncoder({"alpha": [1, 0], "beta": _unit(0.771)})
    clusters, near = _confirm_similarity_within_bucket(bucket, enc)
    assert len(clusters) == 1 and len(clusters[0]["findings"]) == 2  # merged
    assert clusters[0]["merge_reason"] == "embedding"
    assert near == []


def test_threshold_0769_is_near_miss():
    # 0.769 < 0.77 (no merge) but >= 0.69 floor -> near-miss.
    bucket = [
        {"id": "a", "file": "x.py", "description": "alpha"},
        {"id": "b", "file": "x.py", "description": "gamma"},
    ]
    enc = _VecEncoder({"alpha": [1, 0], "gamma": _unit(0.769)})
    clusters, near = _confirm_similarity_within_bucket(bucket, enc)
    assert len(clusters) == 2  # not merged
    assert len(near) == 1
    assert 0.69 <= near[0]["similarity"] < 0.77


def test_threshold_0689_is_neither():
    # 0.689 < 0.69 floor -> neither merged nor recorded as a near-miss.
    bucket = [
        {"id": "a", "file": "x.py", "description": "alpha"},
        {"id": "b", "file": "x.py", "description": "delta"},
    ]
    enc = _VecEncoder({"alpha": [1, 0], "delta": _unit(0.689)})
    clusters, near = _confirm_similarity_within_bucket(bucket, enc)
    assert len(clusters) == 2  # not merged
    assert near == []          # below the 0.69 near-miss floor
