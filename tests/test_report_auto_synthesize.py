"""tests/test_report_auto_synthesize.py

S4-9: the ``report`` command auto-enables ``--synthesize`` (and adopts the
recorded domain) when a ``domain_context.json`` exists in the state dir, unless
the operator explicitly passed ``--synthesize`` or disabled the behaviour via
``SENTINEL_AUTO_SYNTHESIZE=false``.

The ``scope`` command writes ``domain_context.json`` alongside
``scope_config.yaml`` with a detected domain/language.

Tests mock ``decision_brief.render`` so they assert the *resolved* synthesize
and domain values the CLI passes through, without running real synthesis.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

import sentinel.config as config_module
from sentinel.cli import main
from sentinel.config import load_config


def _reload_config():
    """Reset the Config singleton so env changes take effect for this test."""
    config_module._config = None
    return load_config()


# ── helpers ───────────────────────────────────────────────────────────


def _write_domain_context(state_dir: Path, domain: str = "software_python") -> None:
    (state_dir / "domain_context.json").write_text(
        json.dumps(
            {
                "domain": domain,
                "repo_path": str(state_dir),
                "language": "Python",
                "created": "2026-01-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )


def _run_report(runner: CliRunner, state_dir: Path, *extra: str):
    """Invoke ``sentinel report`` with render mocked; return (result, mock)."""
    with patch("sentinel.reports.decision_brief.render") as mock_render:
        mock_render.return_value = (
            str(state_dir / "brief.html"),
            str(state_dir / "brief.md"),
            0,
            0,
        )
        result = runner.invoke(
            main,
            ["report", "--state-dir", str(state_dir), *extra],
        )
    return result, mock_render


def _resolved(mock_render):
    """Return (use_synthesis, domain) the CLI passed to render."""
    _, kwargs = mock_render.call_args
    return kwargs["use_synthesis"], kwargs["domain"]


# ── report: auto-enable from domain_context.json ──────────────────────


def test_domain_context_present_auto_enables_synthesis(tmp_path, monkeypatch):
    monkeypatch.delenv("SENTINEL_AUTO_SYNTHESIZE", raising=False)
    _reload_config()
    _write_domain_context(tmp_path, domain="manufacturing_csharp")

    runner = CliRunner()
    result, mock_render = _run_report(runner, tmp_path)

    assert result.exit_code == 0, result.output
    use_synthesis, domain = _resolved(mock_render)
    assert use_synthesis is True
    assert domain == "manufacturing_csharp"


def test_domain_context_absent_synthesis_stays_false(tmp_path, monkeypatch):
    monkeypatch.delenv("SENTINEL_AUTO_SYNTHESIZE", raising=False)
    _reload_config()
    # No domain_context.json written.

    runner = CliRunner()
    result, mock_render = _run_report(runner, tmp_path)

    assert result.exit_code == 0, result.output
    use_synthesis, domain = _resolved(mock_render)
    assert use_synthesis is False
    assert domain == "general"


def test_explicit_synthesize_flag_wins_over_autodetect(tmp_path, monkeypatch):
    monkeypatch.delenv("SENTINEL_AUTO_SYNTHESIZE", raising=False)
    _reload_config()
    _write_domain_context(tmp_path, domain="manufacturing_csharp")

    runner = CliRunner()
    # Operator explicitly passes --synthesize AND --domain: those win. The
    # auto-detect must not override the explicit --domain the operator chose.
    result, mock_render = _run_report(
        runner, tmp_path, "--synthesize", "--domain", "general"
    )

    assert result.exit_code == 0, result.output
    use_synthesis, domain = _resolved(mock_render)
    assert use_synthesis is True
    assert domain == "general"


def test_auto_synthesize_disabled_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("SENTINEL_AUTO_SYNTHESIZE", "false")
    _reload_config()
    _write_domain_context(tmp_path, domain="manufacturing_csharp")

    runner = CliRunner()
    result, mock_render = _run_report(runner, tmp_path)

    assert result.exit_code == 0, result.output
    use_synthesis, domain = _resolved(mock_render)
    # File exists, but env disables auto-detect: synthesis stays off, domain
    # stays at the CLI default.
    assert use_synthesis is False
    assert domain == "general"


# ── scope: detects domain and writes domain_context.json ──────────────


def _make_repo(tmp_path: Path, ext: str, n: int = 3) -> Path:
    repo = tmp_path / "repo"
    src = repo / "src"
    src.mkdir(parents=True)
    for i in range(n):
        (src / f"mod{i}{ext}").write_text("// code\n", encoding="utf-8")
    return repo


def _run_scope(runner: CliRunner, repo: Path):
    return runner.invoke(main, ["scope", "--repo-path", str(repo)])


def test_scope_csharp_repo_domain_manufacturing(tmp_path, monkeypatch):
    monkeypatch.setenv("SENTINEL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SENTINEL_REPO_PATH", str(tmp_path))
    _reload_config()
    repo = _make_repo(tmp_path, ".cs")

    runner = CliRunner()
    result = _run_scope(runner, repo)
    assert result.exit_code == 0, result.output

    ctx = json.loads((repo / "domain_context.json").read_text(encoding="utf-8"))
    assert ctx["domain"] == "manufacturing_csharp"
    assert ctx["language"] == "C#"
    assert "created" in ctx


def test_scope_python_repo_domain_software(tmp_path, monkeypatch):
    monkeypatch.setenv("SENTINEL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SENTINEL_REPO_PATH", str(tmp_path))
    _reload_config()
    repo = _make_repo(tmp_path, ".py")

    runner = CliRunner()
    result = _run_scope(runner, repo)
    assert result.exit_code == 0, result.output

    ctx = json.loads((repo / "domain_context.json").read_text(encoding="utf-8"))
    assert ctx["domain"] == "software_python"
    assert ctx["language"] == "Python"


def test_scope_other_repo_domain_general(tmp_path, monkeypatch):
    monkeypatch.setenv("SENTINEL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SENTINEL_REPO_PATH", str(tmp_path))
    _reload_config()
    repo = _make_repo(tmp_path, ".txt")

    runner = CliRunner()
    result = _run_scope(runner, repo)
    assert result.exit_code == 0, result.output

    ctx = json.loads((repo / "domain_context.json").read_text(encoding="utf-8"))
    assert ctx["domain"] == "general"
