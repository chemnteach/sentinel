from __future__ import annotations

import json
import os
from pathlib import Path

from click.testing import CliRunner

from sentinel.cli import main


def _seed_tech_debt(tmp_path: Path) -> Path:
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    tech_debt_file = state_dir / "tech_debt.json"
    tech_debt_file.write_text(
        json.dumps(
            [
                {
                    "id": "TD-0001",
                    "severity": "P1",
                    "description": "first",
                    "human_triage": None,
                },
                {
                    "id": "TD-0002",
                    "severity": "P2",
                    "description": "second",
                    "human_triage": None,
                },
            ],
            indent=2,
        ),
        encoding="utf-8",
    )
    return state_dir


def _seed_batch_file(tmp_path: Path) -> Path:
    batch_file = tmp_path / "decisions.json"
    batch_file.write_text(
        json.dumps(
            [
                {
                    "td_id": "TD-0001",
                    "decision": "APPROVE",
                    "conditions": None,
                    "rationale": None,
                }
            ],
            indent=2,
        ),
        encoding="utf-8",
    )
    return batch_file


def _simulate_overlapping_write_on_second_read(
    monkeypatch,
    *,
    tech_debt_file: Path,
) -> None:
    original_read_text = Path.read_text
    injected = {"done": False}

    def _patched_read_text(self: Path, *args, **kwargs):
        if self == tech_debt_file:
            if not injected["done"]:
                injected["done"] = True
                return original_read_text(self, *args, **kwargs)

            # Simulate another writer committing an overlapping update after
            # the caller's baseline read but before persistence.
            current = json.loads(original_read_text(self, encoding="utf-8"))
            for item in current:
                if item.get("id") == "TD-0002":
                    item["human_triage"] = "DEFER"
            self.write_text(json.dumps(current, indent=2), encoding="utf-8")
            return original_read_text(self, *args, **kwargs)

        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _patched_read_text)


def _assert_conflict_preserves_competing_update(result, *, tech_debt_file: Path) -> None:
    assert result.exit_code == 1
    assert "conflict" in result.output.lower()

    final_items = json.loads(tech_debt_file.read_text(encoding="utf-8"))
    by_id = {item["id"]: item for item in final_items}
    assert by_id["TD-0002"]["human_triage"] == "DEFER"


def test_single_triage_detects_overlap_conflict_and_does_not_overwrite(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = CliRunner()
    state_dir = _seed_tech_debt(tmp_path)
    tech_debt_file = state_dir / "tech_debt.json"

    _simulate_overlapping_write_on_second_read(
        monkeypatch,
        tech_debt_file=tech_debt_file,
    )

    result = runner.invoke(
        main,
        ["triage", "--approve", "TD-0001", "--state-dir", str(state_dir)],
    )

    _assert_conflict_preserves_competing_update(
        result,
        tech_debt_file=tech_debt_file,
    )


def test_batch_triage_detects_overlap_conflict_and_does_not_overwrite(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = CliRunner()
    state_dir = _seed_tech_debt(tmp_path)
    tech_debt_file = state_dir / "tech_debt.json"
    decisions_file = _seed_batch_file(tmp_path)

    _simulate_overlapping_write_on_second_read(
        monkeypatch,
        tech_debt_file=tech_debt_file,
    )

    result = runner.invoke(
        main,
        [
            "triage",
            "--batch",
            str(decisions_file),
            "--state-dir",
            str(state_dir),
        ],
    )

    _assert_conflict_preserves_competing_update(
        result,
        tech_debt_file=tech_debt_file,
    )


def test_triage_persistence_uses_atomic_replace_for_tech_debt_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = CliRunner()
    state_dir = _seed_tech_debt(tmp_path)
    tech_debt_file = state_dir / "tech_debt.json"

    original_replace = os.replace
    replace_calls: list[tuple[str, str]] = []

    def _patched_replace(src: str, dst: str) -> None:
        replace_calls.append((src, dst))
        original_replace(src, dst)

    monkeypatch.setattr(os, "replace", _patched_replace)

    result = runner.invoke(
        main,
        ["triage", "--approve", "TD-0001", "--state-dir", str(state_dir)],
    )

    assert result.exit_code == 0
    assert any(Path(dst) == tech_debt_file for _, dst in replace_calls)
