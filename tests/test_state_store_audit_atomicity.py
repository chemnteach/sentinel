from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sentinel.orchestrator.state_store import StateStore


def _run_concurrent_append_round(state_dir: Path, *, run_id: int, writers: int) -> list[dict]:
    """Execute one concurrent append round and return persisted audit events."""
    store = StateStore(state_dir=state_dir)
    original_read = store.read

    def delayed_read(store_name: str):
        data = original_read(store_name)
        # Expand the stale-snapshot race window in read-modify-write append paths.
        if store_name == "audit_log":
            time.sleep(0.005)
        return data

    store.read = delayed_read  # type: ignore[method-assign]
    barrier = threading.Barrier(writers)

    def writer(index: int) -> None:
        barrier.wait(timeout=10)
        store.append_audit(
            {
                "event_id": f"run-{run_id}-event-{index}",
                "actor": "concurrency-test",
                "action": "append",
            }
        )

    with ThreadPoolExecutor(max_workers=writers) as pool:
        futures = [pool.submit(writer, i) for i in range(writers)]
        for future in futures:
            future.result(timeout=20)

    persisted = original_read("audit_log")
    assert isinstance(persisted, list)
    return persisted


def test_append_audit_concurrent_writers_are_lossless_across_repeated_runs(
    tmp_path: Path,
) -> None:
    """Concurrent append_audit calls must not lose, duplicate, or overwrite events."""
    writers = 20
    rounds = 5

    for run_id in range(rounds):
        events = _run_concurrent_append_round(
            tmp_path / f"round_{run_id}",
            run_id=run_id,
            writers=writers,
        )

        assert len(events) == writers
        event_ids = [str(event.get("event_id", "")) for event in events]
        assert len(set(event_ids)) == writers
        assert all(event.get("timestamp") for event in events)
