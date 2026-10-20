"""
tests/test_cli_defect.py

Unit tests for CLI defect and rca commands using Click's CliRunner.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
from click.testing import CliRunner

from sentinel.cli import main
from sentinel.defect.models import DefectReport, DefectStatus


@pytest.fixture
def cli_runner_env():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        state_dir = root / "state"
        state_dir.mkdir(parents=True, exist_ok=True)

        with patch.dict(
            "os.environ",
            {
                "SENTINEL_REPO_PATH": str(root),
                "SENTINEL_STATE_DIR": str(state_dir),
                "SENTINEL_LOCAL_ONLY_MODE": "true",
            },
        ):
            runner = CliRunner()
            yield runner, root, state_dir


def test_cli_defect_submit_and_list(cli_runner_env):
    runner, root, state_dir = cli_runner_env

    # Create submission json
    bug_file = root / "bug.json"
    bug_file.write_text(
        json.dumps({
            "title": "Null pointer on empty input",
            "description": "Parser crashes on {}",
            "symptoms": "500 server error",
            "severity": "HIGH",
            "stack_trace": "File 'app.py', line 10",
        }),
        encoding="utf-8",
    )

    result = runner.invoke(main, ["defect", "--submit", str(bug_file)])
    assert result.exit_code == 0
    assert "Defect submitted successfully" in result.output
    assert "DEF-001" in result.output

    # List defects
    list_res = runner.invoke(main, ["defect", "--list"])
    assert list_res.exit_code == 0
    assert "DEF-001" in list_res.output
    assert "Null pointer on empty input" in list_res.output


def test_cli_defect_triage(cli_runner_env):
    runner, root, state_dir = cli_runner_env

    bug_file = root / "bug.json"
    bug_file.write_text(
        json.dumps({
            "title": "Data truncation in log export",
            "description": "Logs clipped at 100 lines",
            "symptoms": "Missing telemetry",
            "severity": "MEDIUM",
        }),
        encoding="utf-8",
    )
    runner.invoke(main, ["defect", "--submit", str(bug_file)])

    # Triage approve
    triage_res = runner.invoke(main, ["defect", "--triage", "DEF-001", "--approve", "--reason", "Approved for investigation"])
    assert triage_res.exit_code == 0
    assert "triaged: APPROVED" in triage_res.output
    assert "sentinel rca --run DEF-001" in triage_res.output


def test_cli_rca_run_mocked(cli_runner_env):
    runner, root, state_dir = cli_runner_env

    bug_file = root / "bug.json"
    bug_file.write_text(
        json.dumps({
            "title": "Crash in parser",
            "description": "Parser throws TypeError",
            "symptoms": "Crash",
            "severity": "HIGH",
        }),
        encoding="utf-8",
    )
    runner.invoke(main, ["defect", "--submit", str(bug_file)])
    runner.invoke(main, ["defect", "--triage", "DEF-001", "--approve"])

    mock_rca_res = {
        "defect_id": "DEF-001",
        "symptom_analysis": "Crash in parser",
        "proximal_mechanism": "app.py:10",
        "systemic_factor": "Missing check",
        "root_cause": "Contract violation",
        "blast_radius": ["app.py"],
        "remediation_options": [
            {"id": "OPT-1", "strategy": "SURGICAL", "title": "Patch"}
        ],
        "recommended_option": "OPT-1",
    }
    mock_spec_res = {
        "defect_id": "DEF-001",
        "selected_option": "OPT-1",
        "reproduction_test_stubs": {"test_file": "test_app.py", "test_code": "def test(): pass"},
        "proposed_diff": "--- a/app.py\n+++ b/app.py\n@@ -10,1 +10,2 @@\n+ guard",
        "acceptance_criteria": ["No crash"],
    }

    with patch("sentinel.agents.architect.ArchitectAgent.analyze_root_cause", return_value=mock_rca_res), \
         patch("sentinel.agents.architect.ArchitectAgent.write_bug_spec", return_value=mock_spec_res):
        res = runner.invoke(main, ["rca", "--run", "DEF-001"])
        assert res.exit_code == 0
        assert "Root Cause Analysis Complete" in res.output
        assert "Contract violation" in res.output
