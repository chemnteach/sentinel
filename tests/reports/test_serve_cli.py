"""Test for the `sentinel serve` CLI command (Deliverable 2).

create_app is stubbed so app.run() never actually blocks/binds a port.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from click.testing import CliRunner

import sentinel.reports.server as server_mod
from sentinel.cli import main


def test_serve_prints_url_and_runs(tmp_path, monkeypatch):
    fake_app = MagicMock()
    monkeypatch.setattr(server_mod, "create_app", lambda sd: fake_app)
    monkeypatch.setattr("sentinel.cli.load_config", lambda: None)

    r = CliRunner().invoke(main, ["serve", "--state-dir", str(tmp_path), "--port", "5055"])

    assert r.exit_code == 0, r.output
    assert "Sentinel UI: http://127.0.0.1:5055" in r.output
    fake_app.run.assert_called_once_with(host="127.0.0.1", port=5055)


def test_serve_passes_host_and_state_dir(tmp_path, monkeypatch):
    captured = {}
    fake_app = MagicMock()

    def _fake_create_app(sd):
        captured["state_dir"] = sd
        return fake_app

    monkeypatch.setattr(server_mod, "create_app", _fake_create_app)
    monkeypatch.setattr("sentinel.cli.load_config", lambda: None)

    r = CliRunner().invoke(
        main,
        ["serve", "--state-dir", str(tmp_path), "--host", "0.0.0.0", "--port", "8080",
         "--unsafe-expose"],
    )
    assert r.exit_code == 0, r.output
    assert captured["state_dir"] == str(tmp_path)
    assert "Sentinel UI: http://0.0.0.0:8080" in r.output
    fake_app.run.assert_called_once_with(host="0.0.0.0", port=8080)


def test_graphify_command_registered():
    """Deliverable 4 smoke test — the graphify command is wired into the CLI."""
    assert "graphify" in main.commands
    r = CliRunner().invoke(main, ["graphify", "--help"])
    assert r.exit_code == 0
    assert "--repo-path" in r.output
    assert "--exclude" in r.output


def test_patterns_command_registered():
    """Smoke test — the `sentinel patterns` command is wired into the CLI."""
    assert "patterns" in main.commands
    r = CliRunner().invoke(main, ["patterns", "--help"])
    assert r.exit_code == 0
    assert "--state-dir" in r.output


def test_scope_command_registered():
    """Deliverable 4 — the `sentinel scope` command is wired into the CLI."""
    assert "scope" in main.commands
    r = CliRunner().invoke(main, ["scope", "--help"])
    assert r.exit_code == 0
    assert "--repo-path" in r.output
    assert "--output" in r.output


def test_scope_command_writes_yaml(tmp_path):
    """`sentinel scope` writes scope_config.yaml with exclude/include sections."""
    import yaml

    repo = tmp_path / "repo"
    (repo / "sentinel").mkdir(parents=True)
    (repo / "sentinel" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "guide.md").write_text("# Guide\n", encoding="utf-8")
    out = tmp_path / "scope_config.yaml"

    r = CliRunner().invoke(
        main, ["scope", "--repo-path", str(repo), "--output", str(out)]
    )
    assert r.exit_code == 0, r.output
    assert out.exists()

    data = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert data["repo_path"] == str(repo)
    assert data["generated_at"]
    exclude_paths = {e["path"] for e in data["exclude"]}
    assert any(p.rstrip("/") == "docs" for p in exclude_paths)
    assert "sentinel/" in data["include"]
