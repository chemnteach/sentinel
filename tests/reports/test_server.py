"""Tests for the Phase 3 Flask UI server (Deliverable 1).

Exercises create_app + the four screen routes and the three API routes with a
temp state dir. LLM is stubbed via _get_llm_client so no network is hit.
"""

from __future__ import annotations

import base64
import json
import re
from urllib.parse import quote as urllib_quote

import httpx
import pytest
import respx

from sentinel.reports import server as server_mod
from sentinel.reports.server import create_app


class _FakeResp:
    def __init__(self, content):
        self.content = content


class _FakeClient:
    def __init__(self, content="Recommend 3 iterations, PR mode."):
        self._content = content

    def call_model(self, system_prompt, user_message, temperature=0.2, max_tokens=None):
        return _FakeResp(self._content)


@pytest.fixture
def state_dir(tmp_path):
    d = tmp_path / "self-assessment-012"
    d.mkdir()
    return d


@pytest.fixture
def app(state_dir, monkeypatch):
    monkeypatch.setattr(server_mod, "_get_llm_client", lambda: _FakeClient())
    application = create_app(str(state_dir))
    application.config.update(TESTING=True)
    return application


@pytest.fixture
def client(app):
    return app.test_client()


def _write_finding_status(state_dir, statuses):
    """statuses: dict status->count -> writes that many records."""
    records = []
    i = 0
    for status, n in statuses.items():
        for _ in range(n):
            i += 1
            records.append({"group_id": f"g{i}", "status": status,
                            "canonical_finding_id": f"F{i}", "member_finding_ids": [f"F{i}"],
                            "severity": "P1"})
    (state_dir / "finding_status.json").write_text(json.dumps(records), encoding="utf-8")


# -- app construction ---------------------------------------------------

def test_create_app_stores_state_dir(state_dir, monkeypatch):
    monkeypatch.setattr(server_mod, "_get_llm_client", lambda: _FakeClient())
    app = create_app(str(state_dir))
    assert app.config["STATE_DIR"] == str(state_dir)


# -- screen routes ------------------------------------------------------

def test_screen2_root_renders(client, state_dir):
    _write_finding_status(state_dir, {"APPROVED": 5, "DEFERRED": 2})
    r = client.get("/")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Back to triage brief" in body


def test_screen2_shows_approved_count(client, state_dir):
    _write_finding_status(state_dir, {"APPROVED": 7, "DEFERRED": 3})
    r = client.get("/")
    assert "7" in r.get_data(as_text=True)


def test_screen3_progress_renders(client):
    assert client.get("/progress").status_code == 200


def test_screen4_escalated_renders(client):
    assert client.get("/escalated").status_code == 200


def test_screen5_learning_renders(client):
    assert client.get("/learning").status_code == 200


def test_nav_links_present_on_every_screen(client):
    for path in ("/", "/progress", "/escalated", "/learning"):
        body = client.get(path).get_data(as_text=True)
        for href in ("/progress", "/escalated", "/learning"):
            assert href in body


# -- API: recommend -----------------------------------------------------

def test_api_recommend_returns_narrative_and_iterations(client):
    r = client.post("/api/recommend")
    assert r.status_code == 200
    data = r.get_json()
    assert "recommendation" in data and data["recommendation"]
    assert isinstance(data["suggested_iterations"], int)
    assert data["suggested_iterations"] >= 1


# -- API: instruction ---------------------------------------------------

def test_api_instruction_includes_iterations_and_mode(client):
    r = client.post("/api/instruction", json={"iterations": 4, "push_mode": "pr"})
    assert r.status_code == 200
    text = r.get_json()["instruction"]
    assert "4" in text
    assert "pr" in text.lower() or "pull request" in text.lower()


def test_api_instruction_direct_mode(client):
    r = client.post("/api/instruction", json={"iterations": 2, "push_mode": "direct"})
    text = r.get_json()["instruction"]
    assert "direct" in text.lower()


# -- API: progress ------------------------------------------------------

def test_api_progress_empty_when_no_file(client):
    data = client.get("/api/progress").get_json()
    assert data["tasks"] == {}
    assert data["summary"]["completed"] == 0


def test_api_progress_reads_batch_summary(client, state_dir):
    (state_dir / "batch_summary.json").write_text(json.dumps({
        "run_label": "self-assessment-012",
        "last_updated": "2026-07-07T00:00:00Z",
        "tasks": {
            "t1": {"status": "COMPLETE", "attempt_count": 1, "last_updated": "x"},
            "t2": {"status": "ESCALATED", "attempt_count": 4, "last_updated": "x"},
            "t3": {"status": "IN_PROGRESS", "attempt_count": 2, "last_updated": "x"},
        },
    }), encoding="utf-8")
    data = client.get("/api/progress").get_json()
    assert set(data["tasks"]) == {"t1", "t2", "t3"}
    assert data["summary"]["completed"] == 1
    assert data["summary"]["escalated"] == 1
    assert data["summary"]["in_progress"] == 1


# -- FIX 2: Screen 5 real-schema field mapping -------------------------

def _write_learning(state_dir, **fields):
    rec = {
        "id": "LRN-20260627-001",
        "source_repo": "sentinel",
        "authored_by_foundry": False,
        "category": "maintainability",
        "severity": "P3",
        "pattern": "TODO markers found in source code.",
        "anti_pattern_code": None,
        "correct_pattern_code": None,
        "applicable_to_generation": False,
        "why": "Found in 8 findings",
        "sentinel_finding_ids": ["F2-a", "F2-b"],
        "extracted_at": "2026-06-27T00:34:07Z",
    }
    rec.update(fields)
    ld = state_dir / "learnings"
    ld.mkdir(exist_ok=True)
    (ld / f"{rec['id']}.json").write_text(json.dumps(rec), encoding="utf-8")
    return rec


def test_screen5_maps_real_schema_fields(client, state_dir):
    _write_learning(
        state_dir,
        id="LRN-20260627-002",
        category="security",
        severity="P1",
        pattern="SQL built via string concatenation",
        why="Found in 4 findings",
        applicable_to_generation=True,
    )
    body = client.get("/learning").get_data(as_text=True)
    assert "SQL built via string concatenation" in body   # proposed_change <- pattern
    assert "Found in 4 findings" in body                   # trigger <- why
    assert "high" in body                                  # confidence <- applicable True
    assert "security" in body                              # category display field
    assert "P1" in body                                    # severity display field


def test_screen5_confidence_low_when_not_applicable(client, state_dir):
    _write_learning(state_dir, applicable_to_generation=False)
    body = client.get("/learning").get_data(as_text=True)
    assert "low" in body


def test_screen5_ignores_index_json(client, state_dir):
    # A real learnings dir also holds index.json (a manifest, not a record).
    _write_learning(state_dir, id="LRN-20260627-003")
    (state_dir / "learnings" / "index.json").write_text(
        json.dumps({"learnings": [{"id": "LRN-20260627-003"}]}), encoding="utf-8"
    )
    body = client.get("/learning").get_data(as_text=True)
    assert "1 proposal(s)" in body   # only the LRN record, not the index manifest


# -- FIX 3 / Phase 4b: Screen 5 propose-PR route (branch + commit + PR) -----

_REPO_API = "https://api.github.com/repos/chemnteach/sentinel"


def _mock_github_sequence(
    *,
    refs_status=201,
    refs_json=None,
    contents_status=201,
    contents_json=None,
    pulls_status=201,
    pulls_json=None,
    base_sha="BASESHA123",
):
    """Register respx mocks for the full step-1..4 GitHub sequence.

    Returns the four route objects (ref_get, refs_post, contents_post, pulls_post)
    so a test can inspect the recorded requests.
    """
    ref_get = respx.get(f"{_REPO_API}/git/ref/heads/main").mock(
        return_value=httpx.Response(200, json={"object": {"sha": base_sha}})
    )
    refs_post = respx.post(f"{_REPO_API}/git/refs").mock(
        return_value=httpx.Response(refs_status, json=refs_json if refs_json is not None else {})
    )
    contents_post = respx.post(
        url__regex=rf"{re.escape(_REPO_API)}/contents/learnings/.+\.json"
    ).mock(
        return_value=httpx.Response(
            contents_status, json=contents_json if contents_json is not None else {}
        )
    )
    pulls_post = respx.post(f"{_REPO_API}/pulls").mock(
        return_value=httpx.Response(
            pulls_status,
            json=pulls_json if pulls_json is not None else {
                "html_url": "https://github.com/chemnteach/sentinel/pull/42",
                "number": 42,
            },
        )
    )
    return ref_get, refs_post, contents_post, pulls_post


def test_propose_pr_400_without_token(client, state_dir, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    _write_learning(state_dir, id="LRN-20260627-050")
    r = client.post("/api/learning/propose-pr", json={"learning_id": "LRN-20260627-050"})
    assert r.status_code == 400
    assert r.get_json()["error"] == "GITHUB_TOKEN not set"


def test_propose_pr_missing_learning_returns_error(client, state_dir, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok-123")
    r = client.post("/api/learning/propose-pr", json={"learning_id": "LRN-does-not-exist"})
    data = r.get_json()
    assert "error" in data


@respx.mock
def test_propose_pr_full_sequence_endpoints_and_payload(client, state_dir, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok-123")
    long_pattern = ("SQL built via string concatenation across a dozen call sites "
                    "in the data layer and the reporting layer and more")  # > 72 chars
    _write_learning(
        state_dir, id="LRN-20260627-051", category="security", severity="P1",
        pattern=long_pattern, why="Found in 4 findings", applicable_to_generation=True,
        sentinel_finding_ids=["F2-aaa", "F2-bbb"],
    )
    ref_get, refs_post, contents_post, pulls_post = _mock_github_sequence()

    r = client.post("/api/learning/propose-pr", json={"learning_id": "LRN-20260627-051"})
    assert r.status_code == 200, r.get_data(as_text=True)
    data = r.get_json()
    assert data["pr_url"] == "https://github.com/chemnteach/sentinel/pull/42"
    assert data["pr_number"] == 42

    # All four calls fired, same Bearer header.
    for route in (ref_get, refs_post, contents_post, pulls_post):
        assert route.called
        assert route.calls[0].request.headers["Authorization"] == "Bearer tok-123"

    # Step 2 — branch ref with correct ref + sha from step 1.
    refs_body = json.loads(refs_post.calls[0].request.content)
    assert refs_body["ref"] == "refs/heads/learning/LRN-20260627-051"
    assert refs_body["sha"] == "BASESHA123"

    # Step 4 — PR payload.
    pr_body = json.loads(pulls_post.calls[0].request.content)
    assert pr_body["title"] == f"Learning: security — {long_pattern[:72]}"
    assert pr_body["head"] == "learning/LRN-20260627-051"
    assert pr_body["base"] == "main"
    assert long_pattern in pr_body["body"]
    assert "F2-aaa" in pr_body["body"] and "F2-bbb" in pr_body["body"]


@respx.mock
def test_propose_pr_file_content_is_valid_base64(client, state_dir, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok-123")
    _write_learning(state_dir, id="LRN-20260627-052", pattern="Broad except swallows errors")
    _, _, contents_post, _ = _mock_github_sequence()

    r = client.post("/api/learning/propose-pr", json={"learning_id": "LRN-20260627-052"})
    assert r.status_code == 200, r.get_data(as_text=True)

    body = json.loads(contents_post.calls[0].request.content)
    assert body["branch"] == "learning/LRN-20260627-052"
    assert body["message"] == "learning: add LRN-20260627-052"
    # content must decode as base64 -> valid JSON of the learning record.
    decoded = base64.b64decode(body["content"]).decode("utf-8")
    record = json.loads(decoded)
    assert record["id"] == "LRN-20260627-052"
    assert record["pattern"] == "Broad except swallows errors"


@respx.mock
def test_propose_pr_branch_already_exists_is_non_fatal(client, state_dir, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok-123")
    _write_learning(state_dir, id="LRN-20260627-053")
    # Step 2 returns 422 "Reference already exists" — must continue to PR creation.
    _, refs_post, _, pulls_post = _mock_github_sequence(
        refs_status=422, refs_json={"message": "Reference already exists"},
    )
    r = client.post("/api/learning/propose-pr", json={"learning_id": "LRN-20260627-053"})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert refs_post.called
    assert pulls_post.called  # proceeded past the existing-branch 422
    assert r.get_json()["pr_number"] == 42


@respx.mock
def test_propose_pr_failure_logs_the_failing_step(client, state_dir, monkeypatch, caplog):
    # A GitHub failure mid-flow must name the exact step in the diagnostic log
    # rather than surfacing an opaque HTTP error. (FIX-DESIRED-011)
    import logging

    monkeypatch.setenv("GITHUB_TOKEN", "tok-123")
    _write_learning(state_dir, id="LRN-20260627-0119")
    # Step 2 (create-branch) fails hard with a 500 (not the benign 422).
    _mock_github_sequence(refs_status=500, refs_json={"message": "boom"})

    with caplog.at_level(logging.ERROR):
        r = client.post(
            "/api/learning/propose-pr", json={"learning_id": "LRN-20260627-0119"}
        )
    assert "error" in r.get_json()
    assert any(
        "failed at step 2:create-branch" in record.message
        for record in caplog.records
    )


@respx.mock
def test_propose_pr_existing_pr_returns_existing(client, state_dir, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok-123")
    _write_learning(state_dir, id="LRN-20260627-054")
    # Steps 1-3 succeed; step 4 POST /pulls 422 "A pull request already exists".
    _mock_github_sequence(
        pulls_status=422,
        pulls_json={"message": "Validation Failed",
                    "errors": [{"message": "A pull request already exists for chemnteach:learning/LRN-20260627-054."}]},
    )
    list_route = respx.get(f"{_REPO_API}/pulls").mock(
        return_value=httpx.Response(200, json=[{
            "html_url": "https://github.com/chemnteach/sentinel/pull/7", "number": 7,
        }])
    )
    r = client.post("/api/learning/propose-pr", json={"learning_id": "LRN-20260627-054"})
    assert r.status_code == 200, r.get_data(as_text=True)
    data = r.get_json()
    assert data["pr_url"] == "https://github.com/chemnteach/sentinel/pull/7"
    assert data["pr_number"] == 7
    assert list_route.called
    # The list query targeted the correct head + open state.
    q = list_route.calls[0].request.url.params
    assert q["head"] == "chemnteach:learning/LRN-20260627-054"
    assert q["state"] == "open"


# -- Security: injection + secret-exposure hardening -------------------

@respx.mock
def test_propose_pr_url_escaped_and_scheme_validated(client, state_dir, monkeypatch):
    # A hostile/compromised gateway returning a markup-bearing or non-http
    # html_url must not reach the client's innerHTML sink unescaped. (injection)
    monkeypatch.setenv("GITHUB_TOKEN", "tok-123")
    _write_learning(state_dir, id="LRN-20260627-060")
    _mock_github_sequence(
        pulls_json={
            "html_url": 'javascript:alert(1)//"><img src=x onerror=alert(1)>',
            "number": 9,
        },
    )
    r = client.post("/api/learning/propose-pr", json={"learning_id": "LRN-20260627-060"})
    assert r.status_code == 200, r.get_data(as_text=True)
    pr_url = r.get_json()["pr_url"]
    # javascript: scheme is rejected -> empty; no raw markup or quote survives.
    assert pr_url == ""
    assert "<img" not in pr_url and '"' not in pr_url


@respx.mock
def test_propose_pr_valid_https_url_passes_through(client, state_dir, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "tok-123")
    _write_learning(state_dir, id="LRN-20260627-061")
    _mock_github_sequence(
        pulls_json={"html_url": "https://github.com/chemnteach/sentinel/pull/5",
                    "number": 5},
    )
    r = client.post("/api/learning/propose-pr", json={"learning_id": "LRN-20260627-061"})
    assert r.get_json()["pr_url"] == "https://github.com/chemnteach/sentinel/pull/5"


@respx.mock
def test_propose_pr_contents_url_is_percent_encoded(client, state_dir, monkeypatch):
    # The learning_id is percent-encoded (urllib.parse.quote) where it is
    # interpolated into the contents-URL path string, so a value cannot break
    # out of the intended path segment. (URL-construction escaping)
    import sentinel.reports.server as srv

    seen = {}
    real_quote = urllib_quote

    def _spy(value, safe="/"):
        seen["called_with"] = (value, safe)
        return real_quote(value, safe=safe)

    monkeypatch.setattr(srv.urllib.parse, "quote", _spy)
    monkeypatch.setenv("GITHUB_TOKEN", "tok-123")
    _write_learning(state_dir, id="LRN-20260627-062")
    _mock_github_sequence()

    r = client.post("/api/learning/propose-pr", json={"learning_id": "LRN-20260627-062"})
    assert r.status_code == 200, r.get_data(as_text=True)
    # quote() was invoked on the learning_id with an empty safe set (full encoding).
    assert seen["called_with"] == ("LRN-20260627-062", "")


@respx.mock
def test_propose_pr_error_does_not_leak_token(client, state_dir, monkeypatch, caplog):
    # A GitHub failure whose message happens to contain the token must be
    # redacted in both the JSON error body and the server log. (secret exposure)
    import logging

    token = "ghp_SUPERSECRETTOKENVALUE123"
    monkeypatch.setenv("GITHUB_TOKEN", token)
    _write_learning(state_dir, id="LRN-20260627-063")
    # Step 1 raises an error whose text embeds the token.
    respx.get(f"{_REPO_API}/git/ref/heads/main").mock(
        side_effect=httpx.RequestError(f"connect failed using Bearer {token}")
    )
    with caplog.at_level(logging.ERROR):
        r = client.post("/api/learning/propose-pr", json={"learning_id": "LRN-20260627-063"})
    assert r.status_code == 502
    assert token not in r.get_json()["error"]
    assert "***" in r.get_json()["error"]
    for record in caplog.records:
        assert token not in record.getMessage()


# -- Phase 5: /graph + graph API routes --------------------------------

@pytest.fixture
def graphify_root(tmp_path, monkeypatch):
    """Point the graphify default root at an empty tmp dir and clear the env
    override, so _resolve_graph_path can't reach the real machine-wide
    D:/Sentinel-Runs/graphify/sentinel/graph.json (step 3). Returns the tmp
    root so tests can seed a <root>/sentinel/graph.json for the fallback case."""
    root = tmp_path / "graphify-root"
    root.mkdir()
    monkeypatch.setattr(server_mod, "_GRAPHIFY_ROOT_DEFAULT", str(root))
    monkeypatch.delenv("SENTINEL_GRAPHIFY_DIR", raising=False)
    return root


def _write_graph_at(target_dir):
    """Write the small synthetic graph.json into ``target_dir`` (created)."""
    nodes = [
        {"id": "A", "label": "main", "source_file": "orchestrator/main.py",
         "community": 1, "community_name": "Community 1"},
        {"id": "B", "label": "helper", "source_file": "orchestrator/util.py",
         "community": 1, "community_name": "Community 1"},
        {"id": "C", "label": "review", "source_file": "assess/reviewer.py",
         "community": 2, "community_name": "Community 2"},
    ]
    links = [
        {"source": "A", "target": "B", "relation": "calls"},
        {"source": "A", "target": "C", "relation": "imports"},
    ]
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "graph.json").write_text(
        json.dumps({"nodes": nodes, "links": links}), encoding="utf-8"
    )


def _write_graph(state_dir):
    """Write a small graph.json at the fallback location state_dir/graphify/."""
    nodes = [
        {"id": "A", "label": "main", "source_file": "orchestrator/main.py",
         "community": 1, "community_name": "Community 1"},
        {"id": "B", "label": "helper", "source_file": "orchestrator/util.py",
         "community": 1, "community_name": "Community 1"},
        {"id": "C", "label": "review", "source_file": "assess/reviewer.py",
         "community": 2, "community_name": "Community 2"},
    ]
    links = [
        {"source": "A", "target": "B", "relation": "calls"},
        {"source": "A", "target": "C", "relation": "imports"},
    ]
    gdir = state_dir / "graphify"
    gdir.mkdir(exist_ok=True)
    (gdir / "graph.json").write_text(json.dumps({"nodes": nodes, "links": links}), encoding="utf-8")


def test_graph_page_200_when_graph_exists(client, state_dir, graphify_root):
    _write_graph(state_dir)
    r = client.get("/graph")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "main" in body            # god node label rendered in sidebar


def test_graph_page_not_available_message(client, state_dir, graphify_root):
    r = client.get("/graph")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "not available" in body.lower()
    assert "sentinel graphify" in body


def test_api_graph_node_returns_neighbors(client, state_dir, graphify_root):
    _write_graph(state_dir)
    data = client.get("/api/graph/node/A").get_json()
    assert data["node_id"] == "A"
    assert {n["id"] for n in data["neighbors"]} == {"B", "C"}


def test_api_graph_node_404_when_missing(client, state_dir, graphify_root):
    _write_graph(state_dir)
    r = client.get("/api/graph/node/ZZZ")
    assert r.status_code == 404


def test_api_graph_file_returns_nodes(client, state_dir, graphify_root):
    _write_graph(state_dir)
    data = client.get("/api/graph/file", query_string={"path": "MAIN.PY"}).get_json()
    assert {n["id"] for n in data["nodes"]} == {"A"}


def test_api_graph_data_returns_raw_graph(client, state_dir, graphify_root):
    _write_graph(state_dir)
    data = client.get("/api/graph/data").get_json()
    assert len(data["nodes"]) == 3
    assert len(data["links"]) == 2


def test_api_graph_data_404_when_missing(client, state_dir, graphify_root):
    assert client.get("/api/graph/data").status_code == 404


def test_nav_has_graph_link(client, state_dir, graphify_root):
    body = client.get("/progress").get_data(as_text=True)
    assert "/graph" in body


# -- Phase 5b: fallback to the canonical "sentinel" graph (step 3) ------

def test_graph_falls_back_to_sentinel_when_basename_mismatch(client, state_dir, graphify_root):
    # state_dir basename is "self-assessment-012" (no matching <basename> dir,
    # no local <state_dir>/graphify/) — only the canonical sentinel graph exists.
    _write_graph_at(graphify_root / "sentinel")
    r = client.get("/graph")
    assert r.status_code == 200
    assert "main" in r.get_data(as_text=True)   # god node from the sentinel graph
    # And the data endpoint serves that same canonical graph.
    data = client.get("/api/graph/data").get_json()
    assert len(data["nodes"]) == 3


def test_graph_prefers_basename_over_sentinel_fallback(client, state_dir, graphify_root):
    # A graph at <root>/<basename>/ (step 2) must win over <root>/sentinel/ (step 3).
    _write_graph_at(graphify_root / state_dir.name)
    (graphify_root / "sentinel").mkdir()
    (graphify_root / "sentinel" / "graph.json").write_text(
        json.dumps({"nodes": [{"id": "X", "label": "other", "source_file": "z.py",
                               "community": 9, "community_name": "C9"}], "links": []}),
        encoding="utf-8",
    )
    data = client.get("/api/graph/data").get_json()
    # Got the basename graph (3 nodes), not the 1-node sentinel fallback.
    assert len(data["nodes"]) == 3
