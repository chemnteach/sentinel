"""
tests/test_synthesizer.py

Tests for ``sentinel.assess.synthesizer.Synthesizer`` -- specifically the
tech-debt register's ``finding_id`` propagation contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sentinel.assess.synthesizer import Synthesizer
from sentinel.orchestrator.state_store import StateStore


def _seed_store(tmp_path: Path, findings: list[dict]) -> StateStore:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    store = StateStore(state_dir=state_dir, repo_path=tmp_path)
    store.write("findings", findings)
    store.write("modules", [{"path": "src/foo.py"}])
    store.write("dependencies", [])
    return store


def test_tech_debt_register_propagates_finding_id_from_finding(
    tmp_path: Path,
) -> None:
    """``Synthesizer._build_tech_debt_register`` must copy the source
    finding's ``id`` field into the register entry's ``finding_id``
    column. Cycle-24 fix: prior behavior emitted the enumeration index
    instead, breaking the audit trail back from tech_debt to the
    originating finding record."""
    store = _seed_store(
        tmp_path,
        findings=[
            {
                "id": "F-0001",
                "module": "src/foo.py",
                "skill": "layer2a-quick-review",
                "severity": "P2",
                "description": "Missing docstring.",
                "suggested_fix": "Add docstring.",
            },
        ],
    )

    Synthesizer(store).run()
    tech_debt = store.read("tech_debt")

    assert len(tech_debt) == 1
    assert tech_debt[0]["finding_id"] == "F-0001"
    # Sanity: the register's own row id is independent of the source
    # finding_id and remains the TD-NNNN sequence.
    assert tech_debt[0]["id"] == "TD-0001"
