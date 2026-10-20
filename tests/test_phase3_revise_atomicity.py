"""
tests/test_phase3_revise_atomicity.py

Atomicity tests for FERManager.revise — the spec-revision unit of work.

A spec revision must perform the FER triage transition and the spec-store
deletion as a single atomic operation. If either sub-step fails, neither
durable mutation may survive: the FER must not be left advanced while a
stale spec remains persisted, and the spec must not be removed without the
matching FER transition being reflected consistently.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sentinel.orchestrator.state_store import StateStore, StateStoreError
from sentinel.phase3.fer_manager import FER, FERManager


def _make_manager(tmp_path: Path) -> tuple[FERManager, StateStore]:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    store = StateStore(state_dir=state_dir, repo_path=tmp_path)
    return FERManager(store=store), store


def _seed(store: StateStore, fer_id: str = "FER-001") -> None:
    fer = FER(
        fer_id=fer_id,
        title="Existing feature",
        business_justification="because reasons",
        priority="MUST_HAVE",
        status="PENDING",
    )
    store.write("fers", [fer.to_dict()])
    store.write(
        "specs",
        [
            {"fer_id": fer_id, "feature_summary": "stale spec", "spec_version": "1.0"},
            {"fer_id": "FER-OTHER", "feature_summary": "untouched spec"},
        ],
    )


def test_revise_success_advances_fer_and_clears_spec(tmp_path: Path) -> None:
    """Happy path: the FER is triaged to APPROVED and its stale spec removed,
    while an unrelated spec is preserved."""
    mgr, store = _make_manager(tmp_path)
    _seed(store)

    fer = mgr.revise("FER-001", feedback="needs more detail")

    assert fer.status == "APPROVED"
    assert mgr.get("FER-001").status == "APPROVED"

    specs = store.read("specs")
    assert all(s.get("fer_id") != "FER-001" for s in specs)
    assert any(s.get("fer_id") == "FER-OTHER" for s in specs)


def test_revise_rolls_back_when_spec_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Inject a failure between the FER transition and the spec-store mutation.

    The forward spec write fails after the FER has already been triaged. The
    revise operation must restore consistent state: the FER must NOT be left
    advanced (still PENDING, not APPROVED) and the stale spec must remain
    present. No partial commit may survive.
    """
    mgr, store = _make_manager(tmp_path)
    _seed(store)

    real_write = store.write
    state = {"specs_writes": 0}

    def failing_write(store_name, data, *args, **kwargs):
        # Fail only the FIRST specs write (the forward mutation) so the
        # compensating rollback's restore write can still succeed.
        if store_name == "specs":
            state["specs_writes"] += 1
            if state["specs_writes"] == 1:
                raise StateStoreError("injected specs write failure")
        return real_write(store_name, data, *args, **kwargs)

    monkeypatch.setattr(store, "write", failing_write)

    with pytest.raises(StateStoreError):
        mgr.revise("FER-001", feedback="needs more detail")

    # FER transition rolled back — still PENDING, not advanced to APPROVED.
    assert mgr.get("FER-001").status == "PENDING"

    # Stale spec still present — not removed without the FER transition.
    specs = store.read("specs")
    assert any(s.get("fer_id") == "FER-001" for s in specs)
    assert any(s.get("fer_id") == "FER-OTHER" for s in specs)


def _seed_spec_approved(store: StateStore, fer_id: str = "FER-SA") -> None:
    fer = FER(
        fer_id=fer_id,
        title="Approved-spec feature",
        business_justification="because reasons",
        priority="MUST_HAVE",
        status="SPEC_APPROVED",
    )
    store.write("fers", [fer.to_dict()])
    store.write(
        "specs",
        [{"fer_id": fer_id, "feature_summary": "stale spec", "spec_version": "1.0"}],
    )


def test_revise_from_spec_approved_transitions_to_approved(tmp_path: Path) -> None:
    # FIX-BLOCKING-011: a SPEC_APPROVED FER can be revised to APPROVED via the
    # dedicated transition (not triage) without a FERTransitionError.
    mgr, store = _make_manager(tmp_path)
    _seed_spec_approved(store)

    fer = mgr.revise("FER-SA", feedback="tighten the acceptance criteria")

    assert fer.status == "APPROVED"
    assert mgr.get("FER-SA").status == "APPROVED"
    # Feedback persisted on the FER record (history).
    assert any(
        e.get("details", {}).get("feedback") == "tighten the acceptance criteria"
        for e in fer.history
    )
    specs = store.read("specs")
    assert all(s.get("fer_id") != "FER-SA" for s in specs)


def test_revise_rejects_unsupported_source_state(tmp_path: Path) -> None:
    # FIX-BLOCKING-011 AC4: an unsupported source state raises a transition error.
    from sentinel.phase3.fer_manager import FERTransitionError

    mgr, store = _make_manager(tmp_path)
    fer = FER(
        fer_id="FER-CMP",
        title="Complete feature",
        business_justification="reasons",
        priority="MUST_HAVE",
        status="COMPLETE",
    )
    store.write("fers", [fer.to_dict()])

    with pytest.raises(FERTransitionError):
        mgr.revise("FER-CMP", feedback="too late")
    assert mgr.get("FER-CMP").status == "COMPLETE"


def test_revise_preserves_original_error_when_rollback_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # FIX-REQUIRED-004: when the forward write fails AND the compensating
    # rollback also fails, the original failure is preserved as the primary
    # cause surfaced to the caller.
    mgr, store = _make_manager(tmp_path)
    _seed(store)

    real_write = store.write

    def failing_write(store_name, data, *args, **kwargs):
        if store_name == "specs":
            raise StateStoreError("injected specs write failure")
        return real_write(store_name, data, *args, **kwargs)

    monkeypatch.setattr(store, "write", failing_write)

    def failing_restore(path, snapshot):
        raise OSError("injected rollback failure")

    monkeypatch.setattr(store, "_restore_file", failing_restore)

    with pytest.raises(StateStoreError, match="injected specs write failure"):
        mgr.revise("FER-001", feedback="needs more detail")

