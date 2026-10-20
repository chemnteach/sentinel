from __future__ import annotations

from pathlib import Path
from threading import Barrier, Thread

import sentinel.phase3.fer_manager as fer_manager_module
from sentinel.orchestrator.state_store import StateStore
from sentinel.phase3.fer_manager import FERManager, FERTransitionError


def _make_manager(tmp_path: Path) -> tuple[FERManager, StateStore]:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    store = StateStore(state_dir=state_dir, repo_path=tmp_path)
    return FERManager(store=store), store


def _seed_approved_fer(manager: FERManager) -> str:
    fer = manager.submit(
        {
            "title": "Add FER conflict handling",
            "business_justification": "Avoid lost lifecycle transitions under overlap.",
            "priority": "MUST_HAVE",
            "affected_systems": ["phase3"],
        }
    )
    return manager.triage(fer.fer_id, "APPROVED").fer_id


def test_concurrent_start_feasibility_allows_one_success_and_one_conflict(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Overlapping transitions on the same FER must not both succeed silently."""

    manager, store = _make_manager(tmp_path)
    fer_id = _seed_approved_fer(manager)

    # Force both transition calls to overlap at persistence time on the buggy
    # path so stale writes become observable.
    original_upsert = store.upsert_in_list
    overlap_barrier = Barrier(2)

    def _barrier_upsert(
        store_name: str,
        item: dict,
        key_field: str,
        commit: bool = False,
        commit_message: str | None = None,
    ) -> None:
        if store_name == "fers":
            overlap_barrier.wait(timeout=5)
        original_upsert(
            store_name,
            item,
            key_field,
            commit=commit,
            commit_message=commit_message,
        )

    monkeypatch.setattr(store, "upsert_in_list", _barrier_upsert)

    successes: list[str] = []
    failures: list[BaseException] = []

    def _run_transition() -> None:
        try:
            updated = manager.start_feasibility(fer_id)
            successes.append(updated.status)
        except BaseException as exc:  # pragma: no cover - assertion inspects payload
            failures.append(exc)

    t1 = Thread(target=_run_transition)
    t2 = Thread(target=_run_transition)
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert not t1.is_alive()
    assert not t2.is_alive()

    assert len(successes) == 1
    assert successes[0] == "IN_FEASIBILITY"
    assert len(failures) == 1
    assert isinstance(failures[0], FERTransitionError)

    persisted = manager.get(fer_id)
    assert persisted.status == "IN_FEASIBILITY"
    assert len([e for e in persisted.history if e.get("event") == "feasibility_started"]) == 1


def test_concurrent_submit_assigns_unique_ids_and_preserves_both_records(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Concurrent submit calls must never collide and overwrite one submission."""

    manager, store = _make_manager(tmp_path)

    payload_a = {
        "title": "Add dependency heatmap export",
        "business_justification": "Analysts need trend snapshots for release readiness.",
        "priority": "MUST_HAVE",
        "affected_systems": ["phase3"],
    }
    payload_b = {
        "title": "Add spec quality score drilldown",
        "business_justification": "Operators need explainable sub-score visibility.",
        "priority": "SHOULD_HAVE",
        "affected_systems": ["phase3"],
    }

    original_next_fer_id = fer_manager_module._next_fer_id
    overlap_barrier = Barrier(2)

    def _barrier_next_fer_id(existing: list[dict[str, object]]) -> str:
        overlap_barrier.wait(timeout=5)
        return original_next_fer_id(existing)

    monkeypatch.setattr(fer_manager_module, "_next_fer_id", _barrier_next_fer_id)

    submitted_ids: list[str] = []
    failures: list[BaseException] = []

    def _submit(payload: dict[str, object]) -> None:
        try:
            submitted = manager.submit(payload)
            submitted_ids.append(submitted.fer_id)
        except BaseException as exc:  # pragma: no cover - assertion inspects payload
            failures.append(exc)

    t1 = Thread(target=_submit, args=(payload_a,))
    t2 = Thread(target=_submit, args=(payload_b,))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)

    assert not t1.is_alive()
    assert not t2.is_alive()
    assert not failures

    assert len(submitted_ids) == 2
    assert len(set(submitted_ids)) == 2

    persisted = store.read("fers")
    assert len(persisted) == 2
    assert {record["fer_id"] for record in persisted} == set(submitted_ids)
    assert {record["title"] for record in persisted} == {
        payload_a["title"],
        payload_b["title"],
    }
