"""Tests for the Fix Planner cross-batch dedup (``_dedup_plan_items``).

The Fix Planner runs once per pattern batch, so a finding that belongs to
multiple SemanticGroups is planned repeatedly across batches. ``_dedup_plan_items``
collapses those duplicate plan items by exact ``source_findings`` set.
"""
from __future__ import annotations

from sentinel.orchestrator.main import _dedup_plan_items

_TIERS = ("blocking", "required", "desired", "nice_to_have")


def _acc(**tiers):
    base = {t: [] for t in _TIERS}
    base.update(tiers)
    return base


def _item(item_id, source_findings):
    return {"id": item_id, "source_findings": source_findings}


def test_duplicate_source_findings_across_batches_keeps_first():
    # Two pattern batches each produced a 'required' item for the same finding.
    acc = _acc(required=[_item("R1", ["F-1"]), _item("R2", ["F-1"])])
    _dedup_plan_items(acc, _TIERS)
    assert [i["id"] for i in acc["required"]] == ["R1"]


def test_blocking_wins_over_required_for_same_finding_set():
    acc = _acc(blocking=[_item("B1", ["F-1"])], required=[_item("R1", ["F-1"])])
    _dedup_plan_items(acc, _TIERS)
    assert [i["id"] for i in acc["blocking"]] == ["B1"]
    assert acc["required"] == []


def test_different_source_findings_sets_are_kept():
    acc = _acc(required=[_item("R1", ["F-1"]), _item("R2", ["F-1", "F-2"])])
    _dedup_plan_items(acc, _TIERS)
    assert [i["id"] for i in acc["required"]] == ["R1", "R2"]


def test_same_multi_finding_set_in_any_order_is_deduped():
    # Order within source_findings must not matter (frozenset key).
    acc = _acc(required=[_item("R1", ["F-1", "F-2"]), _item("R2", ["F-2", "F-1"])])
    _dedup_plan_items(acc, _TIERS)
    assert [i["id"] for i in acc["required"]] == ["R1"]


def test_items_without_source_findings_are_always_kept():
    acc = _acc(required=[_item("R1", []), _item("R2", [])])
    _dedup_plan_items(acc, _TIERS)
    assert [i["id"] for i in acc["required"]] == ["R1", "R2"]
