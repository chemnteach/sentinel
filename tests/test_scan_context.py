"""Tests for scan-scoped repository identity context."""

from __future__ import annotations

import json
from pathlib import Path, PureWindowsPath

import pytest

from sentinel.assess.finding_schema import materialize_emitter_finding
from sentinel.assess.scan_context import ScanContext


def test_from_repo_root_canonicalizes_windows_drive_backslashes_and_trailing_slash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows roots use lowercase drives, forward slashes, and no trailing slash."""

    def fake_resolve(self: Path) -> PureWindowsPath:
        return PureWindowsPath("D:\\Git\\sentinel\\")

    monkeypatch.setattr(Path, "resolve", fake_resolve)

    context = ScanContext.from_repo_root(Path("ignored"))

    assert context.repo_root_path == PureWindowsPath(r"D:\Git\sentinel")
    assert context.repo_root_canonical == "d:/Git/sentinel"


def test_from_repo_root_preserves_windows_drive_root_trailing_slash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive roots keep the slash that distinguishes ``d:/`` from ``d:``."""

    def fake_resolve(self: Path) -> PureWindowsPath:
        return PureWindowsPath("D:/")

    monkeypatch.setattr(Path, "resolve", fake_resolve)

    context = ScanContext.from_repo_root(Path("ignored"))

    assert context.repo_root_canonical == "d:/"


def test_from_repo_root_resolves_symlink(tmp_path: Path) -> None:
    """Symlink spellings canonicalize to the resolved filesystem target."""

    real_root = tmp_path / "real"
    real_root.mkdir()
    link_root = tmp_path / "link"
    try:
        link_root.symlink_to(real_root, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    context = ScanContext.from_repo_root(link_root)

    assert context.repo_root_path == real_root.resolve()
    assert context.repo_root_canonical == real_root.resolve().as_posix()


def test_metadata_round_trip_preserves_identity(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    context = ScanContext.from_repo_root(repo_root)

    rehydrated = ScanContext.from_metadata(context.to_metadata())

    assert rehydrated == context


def test_from_metadata_missing_repo_root_path_raises() -> None:
    with pytest.raises(ValueError, match="scan metadata missing repo_root_path"):
        ScanContext.from_metadata({"repo_root_canonical": "d:/Git/sentinel"})


def test_from_metadata_missing_repo_root_canonical_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="scan metadata missing repo_root_canonical"):
        ScanContext.from_metadata({"repo_root_path": str(tmp_path)})


def test_from_metadata_does_not_validate_filesystem_existence(tmp_path: Path) -> None:
    missing_root = tmp_path / "missing-repo"

    context = ScanContext.from_metadata(
        {
            "repo_root_path": missing_root.as_posix(),
            "repo_root_canonical": "missing/repo",
        }
    )

    assert not missing_root.exists()
    assert context.repo_root_path == missing_root
    assert context.repo_root_canonical == "missing/repo"


def test_f2_ids_stable_across_scan_metadata_round_trip(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    context = ScanContext.from_repo_root(repo_root)
    metadata_path = tmp_path / "scan_metadata.json"
    metadata_path.write_text(json.dumps(context.to_metadata()), encoding="utf-8")

    rehydrated_context = ScanContext.from_metadata(
        json.loads(metadata_path.read_text(encoding="utf-8"))
    )

    raw_findings = [
        {
            "module": "sentinel.assess.reviewer",
            "skill": "layer2b-correctness-specialist",
            "severity": "P1",
            "category": "correctness",
            "location": f"{context.repo_root_canonical}/sentinel/assess/reviewer.py:42",
            "description": "Review module violates its return contract.",
            "suggested_fix": "Return the documented tuple shape.",
            "finding_kind": "contract_violation",
            "subject_key": "return-contract:review_module",
            "status": "open",
        },
        {
            "module": "sentinel.scan",
            "skill": "layer2a-quick-review",
            "severity": "P2",
            "category": "correctness",
            "location": f"{context.repo_root_canonical}/sentinel/scan.py:88",
            "description": "Standalone quick scan uses the wrong root.",
            "suggested_fix": "Use the scan context root.",
            "finding_kind": "logic_bug",
            "status": "open",
        },
        {
            "module": "sentinel.orchestrator.main",
            "skill": "layer3b-maintainability-specialist",
            "severity": "P3",
            "category": "maintainability",
            "location": f"{context.repo_root_canonical}/sentinel/orchestrator/main.py:227",
            "description": "Root identity setup adds structural complexity.",
            "suggested_fix": "Keep the setup isolated in ScanContext.",
            "finding_kind": "structural_complexity",
            "status": "open",
        },
    ]

    original_ids = [
        materialize_emitter_finding(
            finding,
            emitter_name=str(finding["skill"]),
            repo_root_canonical=context.repo_root_canonical,
        )["id"]
        for finding in raw_findings
    ]
    rehydrated_ids = [
        materialize_emitter_finding(
            finding,
            emitter_name=str(finding["skill"]),
            repo_root_canonical=rehydrated_context.repo_root_canonical,
        )["id"]
        for finding in raw_findings
    ]

    assert rehydrated_ids == original_ids