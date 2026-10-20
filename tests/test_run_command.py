"""
tests/test_run_command.py

S4-11 — `sentinel run` single-command chain and `.sentinel/config.yaml`
defaults support.

These tests are hermetic: the heavy phase calls (Orchestrator.run_phase1,
continue_phase1_after_triage, run_phase2, CompletionChecker, decision-brief
render) are stubbed via monkeypatch so no real LLM/network call happens.
"""

from __future__ import annotations

import os
import re

import pytest
from click.testing import CliRunner

import sentinel.cli as cli_module
import sentinel.config as config_module
from sentinel.cli import main
from sentinel.config import Config
from sentinel.orchestrator.main import HumanGateRequired


@pytest.fixture(autouse=True)
def _reset_global_config_singleton() -> None:
    config_module._config = None
    yield
    config_module._config = None


# ---------------------------------------------------------------------------
# .sentinel/config.yaml defaults
# ---------------------------------------------------------------------------


def test_sentinel_yaml_loaded_as_defaults(tmp_path, monkeypatch) -> None:
    """A .sentinel/config.yaml in CWD supplies defaults for unset env vars."""
    sentinel_dir = tmp_path / ".sentinel"
    sentinel_dir.mkdir()
    (sentinel_dir / "config.yaml").write_text(
        "model: gpt-yaml-model\n"
        "operator: yaml-operator\n"
        "operator_email: yaml@example.com\n"
        "github_url: https://example.com/org/repo\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    # Only repo_path set in env; model/operator must come from the yaml file.
    cfg = Config(env={"SENTINEL_REPO_PATH": str(tmp_path)}, load_dotenv_file=False)

    assert cfg.model == "gpt-yaml-model"
    assert cfg.operator == "yaml-operator"
    assert cfg.operator_email == "yaml@example.com"
    assert cfg.github_url == "https://example.com/org/repo"


def test_env_overrides_sentinel_yaml(tmp_path, monkeypatch) -> None:
    """Explicit env vars take precedence over .sentinel/config.yaml defaults."""
    sentinel_dir = tmp_path / ".sentinel"
    sentinel_dir.mkdir()
    (sentinel_dir / "config.yaml").write_text(
        "model: gpt-yaml-model\noperator: yaml-operator\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    cfg = Config(
        env={
            "SENTINEL_REPO_PATH": str(tmp_path),
            "SENTINEL_MODEL": "gpt-env-model",
        },
        load_dotenv_file=False,
    )

    # Env wins for model; yaml still supplies operator (no env override).
    assert cfg.model == "gpt-env-model"
    assert cfg.operator == "yaml-operator"


def test_missing_sentinel_yaml_uses_builtin_defaults(tmp_path, monkeypatch) -> None:
    """No .sentinel/config.yaml → built-in defaults apply unchanged."""
    monkeypatch.chdir(tmp_path)
    cfg = Config(env={"SENTINEL_REPO_PATH": str(tmp_path)}, load_dotenv_file=False)
    assert cfg.model == "gpt-5.1"
    assert cfg.operator == "unknown"


def test_malformed_sentinel_yaml_is_ignored(tmp_path, monkeypatch) -> None:
    """A malformed .sentinel/config.yaml is logged + ignored (no crash)."""
    sentinel_dir = tmp_path / ".sentinel"
    sentinel_dir.mkdir()
    # Invalid YAML (unclosed bracket) — must not raise; built-in defaults apply.
    (sentinel_dir / "config.yaml").write_text("model: [unclosed\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    cfg = Config(env={"SENTINEL_REPO_PATH": str(tmp_path)}, load_dotenv_file=False)
    assert cfg.model == "gpt-5.1"


def test_non_mapping_sentinel_yaml_is_ignored(tmp_path, monkeypatch) -> None:
    """A YAML file that isn't a mapping (e.g. a list) is ignored."""
    sentinel_dir = tmp_path / ".sentinel"
    sentinel_dir.mkdir()
    (sentinel_dir / "config.yaml").write_text("- a\n- b\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    cfg = Config(env={"SENTINEL_REPO_PATH": str(tmp_path)}, load_dotenv_file=False)
    assert cfg.model == "gpt-5.1"


# ---------------------------------------------------------------------------
# `sentinel run` — helpers / stubs
# ---------------------------------------------------------------------------


class _FakeStore:
    """Minimal state store recording tech_debt reads/writes."""

    def __init__(self, tech_debt: list[dict]) -> None:
        self._data: dict[str, object] = {"tech_debt": tech_debt}

    def read(self, key: str):
        return self._data.get(key)

    def write(self, key: str, value) -> None:
        self._data[key] = value


class _FakeOrchestrator:
    """Stand-in Orchestrator context manager.

    Records which phase methods were invoked. ``run_phase1`` raises
    HumanGateRequired to simulate stopping at the tech-debt triage gate.
    """

    instances: list["_FakeOrchestrator"] = []

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.calls: list[str] = []
        self._store = _FakeStore([])
        _FakeOrchestrator.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def run_phase1(self, paths, restricted_files=None):
        self.calls.append("run_phase1")
        raise HumanGateRequired(
            "tech_debt_triage",
            {"message": "triage required"},
        )

    def continue_phase1_after_triage(self):
        self.calls.append("continue_phase1_after_triage")

    def run_phase2(self, task_id=None, limit=None):
        self.calls.append("run_phase2")


def _install_run_stubs(monkeypatch, tech_debt=None):
    """Patch cli.Orchestrator + scope + report so `run` never touches LLM.

    Returns a dict recording whether scope/report ran.
    """
    _FakeOrchestrator.instances = []

    def _make(cfg):
        orch = _FakeOrchestrator(cfg)
        if tech_debt is not None:
            orch._store = _FakeStore([dict(t) for t in tech_debt])
        return orch

    monkeypatch.setattr(cli_module, "Orchestrator", _make)

    recorded: dict[str, bool] = {"scope": False, "report": False}

    def _fake_recommend(repo_path):
        recorded["scope"] = True

        class _Rec:
            def __init__(self, rp):
                self.repo_path = rp
                self.recommended_excludes = []
                self.recommended_includes = []
                self.total_files = 0
                self.total_lines = 0
                self.generated_at = "now"

        return _Rec(str(repo_path))

    monkeypatch.setattr(
        "sentinel.assess.scope_recommender.recommend_scope", _fake_recommend
    )

    def _fake_render(state_dir, *args, **kwargs):
        recorded["report"] = True
        recorded["render_kwargs"] = kwargs
        html = os.path.join(str(state_dir), "brief.html")
        md = os.path.join(str(state_dir), "brief.md")
        return html, md, 0, 0

    monkeypatch.setattr("sentinel.reports.decision_brief.render", _fake_render)

    return recorded


# ---------------------------------------------------------------------------
# `sentinel run`
# ---------------------------------------------------------------------------


def test_run_chains_scope_and_assess(tmp_path, monkeypatch) -> None:
    """`sentinel run --repo` runs scope then assess (up to the triage gate)."""
    recorded = _install_run_stubs(monkeypatch)
    runner = CliRunner()

    result = runner.invoke(
        main,
        ["run", "--repo", str(tmp_path)],
        env={"SENTINEL_REPO_PATH": str(tmp_path)},
    )

    assert result.exit_code == 0, result.output
    assert recorded["scope"] is True
    assert _FakeOrchestrator.instances, "Orchestrator was never constructed"
    assert "run_phase1" in _FakeOrchestrator.instances[0].calls


def test_run_report_defaults_to_no_vscode_links(tmp_path, monkeypatch) -> None:
    """`sentinel run` produces a portable brief by default — its report step has
    no viewer-usable repo root, so vscode:// links would be dead. Default off,
    re-enabled with --vscode-links. (The report step runs only past the triage
    gate, so --auto-approve-triage is needed to reach it.)"""
    tech_debt = [{"id": "TD-0001", "severity": "Critical", "human_triage": None}]
    runner = CliRunner()

    recorded = _install_run_stubs(monkeypatch, tech_debt=tech_debt)
    result = runner.invoke(
        main, ["run", "--repo", str(tmp_path), "--auto-approve-triage"],
        env={"SENTINEL_REPO_PATH": str(tmp_path)},
    )
    assert result.exit_code == 0, result.output
    assert recorded["report"] is True
    assert recorded["render_kwargs"].get("vscode_links") is False

    recorded = _install_run_stubs(monkeypatch, tech_debt=tech_debt)
    result = runner.invoke(
        main, ["run", "--repo", str(tmp_path), "--auto-approve-triage", "--vscode-links"],
        env={"SENTINEL_REPO_PATH": str(tmp_path)},
    )
    assert result.exit_code == 0, result.output
    assert recorded["render_kwargs"].get("vscode_links") is True


def test_run_stops_at_triage_gate_without_auto_approve(tmp_path, monkeypatch) -> None:
    """Without --auto-approve-triage the triage gate is a clean stop (exit 0)."""
    _install_run_stubs(monkeypatch)
    runner = CliRunner()

    result = runner.invoke(
        main,
        ["run", "--repo", str(tmp_path)],
        env={"SENTINEL_REPO_PATH": str(tmp_path)},
    )

    assert result.exit_code == 0, result.output
    assert "triage" in result.output.lower()
    # Assessment continuation must NOT have run — we stopped at the gate.
    assert "continue_phase1_after_triage" not in _FakeOrchestrator.instances[0].calls


def test_run_auto_approve_triage_approves_and_continues(tmp_path, monkeypatch) -> None:
    """--auto-approve-triage approves verified Critical/P1 items then continues."""
    tech_debt = [
        {"id": "TD-0001", "severity": "Critical", "human_triage": None},
        {"id": "TD-0002", "severity": "P1", "human_triage": None},
        {"id": "TD-0003", "severity": "P3", "human_triage": None},
    ]
    _install_run_stubs(monkeypatch, tech_debt=tech_debt)
    runner = CliRunner()

    result = runner.invoke(
        main,
        ["run", "--repo", str(tmp_path), "--auto-approve-triage"],
        env={"SENTINEL_REPO_PATH": str(tmp_path)},
    )

    assert result.exit_code == 0, result.output
    orch = _FakeOrchestrator.instances[0]
    # Continuation ran after the auto-approval.
    assert "continue_phase1_after_triage" in orch.calls
    # Critical + P1 approved; P3 left untouched.
    written = orch._store.read("tech_debt")
    by_id = {t["id"]: t for t in written}
    assert by_id["TD-0001"]["human_triage"] == "APPROVE"
    assert by_id["TD-0002"]["human_triage"] == "APPROVE"
    assert by_id["TD-0003"]["human_triage"] is None


def test_run_auto_approve_reaches_govern_and_report(tmp_path, monkeypatch) -> None:
    """Full auto chain: continue → govern (phase2) → report brief.html printed."""
    tech_debt = [{"id": "TD-0001", "severity": "Critical", "human_triage": None}]
    recorded = _install_run_stubs(monkeypatch, tech_debt=tech_debt)
    runner = CliRunner()

    result = runner.invoke(
        main,
        ["run", "--repo", str(tmp_path), "--auto-approve-triage"],
        env={"SENTINEL_REPO_PATH": str(tmp_path)},
    )

    assert result.exit_code == 0, result.output
    orch = _FakeOrchestrator.instances[0]
    assert "run_phase2" in orch.calls
    assert recorded["report"] is True
    assert "brief.html" in result.output


def test_run_state_dir_auto_generated_from_repo_name(tmp_path, monkeypatch) -> None:
    """When --state-dir omitted, the state dir derives from repo basename + timestamp."""
    _install_run_stubs(monkeypatch)
    repo = tmp_path / "myrepo"
    repo.mkdir()
    runner = CliRunner()

    result = runner.invoke(
        main,
        ["run", "--repo", str(repo)],
        env={"SENTINEL_REPO_PATH": str(repo)},
    )

    assert result.exit_code == 0, result.output
    orch = _FakeOrchestrator.instances[0]
    state_dir = str(orch.cfg.state_dir)
    assert "myrepo" in state_dir
    # A timestamp suffix (at least 8 consecutive digits) is present.
    assert re.search(r"\d{8}", state_dir), state_dir


def test_run_help_shows_options() -> None:
    """`sentinel run --help` documents --repo, --state-dir, --auto-approve-triage."""
    runner = CliRunner()
    result = runner.invoke(main, ["run", "--help"])
    assert result.exit_code == 0
    assert "--repo" in result.output
    assert "--state-dir" in result.output
    assert "--auto-approve-triage" in result.output
