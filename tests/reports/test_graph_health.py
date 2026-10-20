"""Tests for sentinel.reports.graph_health — structural-health from a graph.json."""
from __future__ import annotations

import json
from pathlib import Path

from sentinel.reports.graph_health import compute_health, render_health_html


def _graph():
    # Two clusters. Cluster 0 (auth) is self-contained; cluster 1 (data) has a
    # mutual dependency with auth (Db.cs <-> Auth.cs). "string" is a framework
    # type (no repo path) and must NOT appear as a hub.
    nodes = [
        {"id": "auth", "label": "Auth.cs", "source_file": "src/auth/Auth.cs", "community": 0, "community_name": "Auth"},
        {"id": "login", "label": "Login.cs", "source_file": "src/auth/Login.cs", "community": 0, "community_name": "Auth"},
        {"id": "sess", "label": "Session.cs", "source_file": "src/auth/Session.cs", "community": 0, "community_name": "Auth"},
        {"id": "db", "label": "Db.cs", "source_file": "src/data/Db.cs", "community": 1, "community_name": "Data"},
        {"id": "repo", "label": "Repo.cs", "source_file": "src/data/Repo.cs", "community": 1, "community_name": "Data"},
        {"id": "string", "label": "string", "source_file": "", "community": 1, "community_name": "Data"},
    ]
    links = [
        {"source": "auth", "target": "login", "relation": "calls"},
        {"source": "auth", "target": "sess", "relation": "references"},
        {"source": "login", "target": "sess", "relation": "calls"},
        {"source": "db", "target": "repo", "relation": "calls"},
        # cross-cluster + mutual: auth<->db
        {"source": "auth", "target": "db", "relation": "references"},
        {"source": "db", "target": "auth", "relation": "references"},
        # everyone uses the framework type
        {"source": "auth", "target": "string", "relation": "references"},
        {"source": "db", "target": "string", "relation": "references"},
        {"source": "repo", "target": "string", "relation": "references"},
    ]
    return {"nodes": nodes, "links": links}


def _write(tmp_path) -> Path:
    p = tmp_path / "graph.json"
    p.write_text(json.dumps(_graph()), encoding="utf-8")
    return p


def test_compute_health_scale_and_modularity(tmp_path):
    h = compute_health(_write(tmp_path))
    assert h is not None
    assert h["scale"] == {"entities": 6, "relationships": 9, "communities": 2}
    # intra + cross accounted for; intra_pct in range.
    assert 0 <= h["modularity"]["intra_pct"] <= 100


def test_compute_health_excludes_framework_types_from_hubs(tmp_path):
    h = compute_health(_write(tmp_path))
    labels = {hub["label"] for hub in h["hubs"]}
    assert "string" not in labels          # framework type excluded
    assert any(hub["label"].endswith(".cs") for hub in h["hubs"])


def test_compute_health_detects_circular_dependency(tmp_path):
    h = compute_health(_write(tmp_path))
    assert h["circular_deps"]["count"] == 1
    pair = {n for p in h["circular_deps"]["pairs"] for n in p}
    assert pair == {"Auth.cs", "Db.cs"}


def test_compute_health_missing_or_empty_returns_none(tmp_path):
    assert compute_health(tmp_path / "nope.json") is None
    empty = tmp_path / "empty.json"
    empty.write_text('{"nodes": [], "links": []}', encoding="utf-8")
    assert compute_health(empty) is None


def test_render_health_html_contains_sections(tmp_path):
    h = compute_health(_write(tmp_path))
    html = render_health_html(h)
    assert "Structural Health" in html
    assert "Coupling hotspots" in html
    assert "Circular dependencies" in html
    assert "Auth.cs" in html and "Db.cs" in html  # the cycle is shown


def test_render_health_html_clean_when_no_cycles(tmp_path):
    g = _graph()
    # drop the reverse edge so there is no mutual dependency
    g["links"] = [l for l in g["links"]
                  if not (l["source"] == "db" and l["target"] == "auth")]
    p = tmp_path / "g.json"
    p.write_text(json.dumps(g), encoding="utf-8")
    html = render_health_html(compute_health(p))
    assert "None found" in html
