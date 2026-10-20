"""Tests for Screen 0 (scope recommendation) routes in the Flask server.

Covers GET /scope and POST /api/scope/approve. No LLM/network is hit.
"""

from __future__ import annotations

import json

import pytest

from sentinel.reports import server as server_mod
from sentinel.reports.server import create_app


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "sentinel").mkdir(parents=True)
    (root / "sentinel" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "guide.md").write_text("# Guide\n", encoding="utf-8")
    return root


@pytest.fixture
def state_dir(tmp_path):
    d = tmp_path / "self-assessment-016"
    d.mkdir()
    return d


@pytest.fixture
def client(state_dir, monkeypatch):
    monkeypatch.setattr(server_mod, "_get_llm_client", lambda: None)
    app = create_app(str(state_dir))
    app.config.update(TESTING=True)
    return app.test_client()


def test_scope_route_returns_200(client, repo):
    resp = client.get(f"/scope?repo_path={repo}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "docs/" in body
    assert "Scope" in body


def test_scope_approve_writes_config(client, state_dir, repo):
    resp = client.post(
        "/api/scope/approve",
        json={"excludes": ["docs/", "vendor/"], "repo_path": str(repo)},
    )
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["status"] == "saved"

    config_path = state_dir / "scope_config.json"
    assert config_path.exists()
    assert payload["path"] == str(config_path)

    saved = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved["repo_path"] == str(repo)
    assert saved["excluded_paths"] == ["docs/", "vendor/"]
    assert saved["approved_by"] == "human"
    assert saved["approved_at"]


def test_scope_route_shows_approved_banner_when_config_exists(client, state_dir, repo):
    (state_dir / "scope_config.json").write_text(
        json.dumps(
            {
                "repo_path": str(repo),
                "excluded_paths": ["docs/"],
                "approved_at": "2026-07-09T10:00:00",
                "approved_by": "human",
            }
        ),
        encoding="utf-8",
    )
    resp = client.get(f"/scope?repo_path={repo}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Scope approved on" in body
