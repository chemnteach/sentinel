"""Tests for the Phase 5 Graphify integration wrapper (Deliverable 1).

Fixture-driven with a tiny synthetic graph.json (5 nodes, 6 edges). All
functions must degrade gracefully (empty result, no exception) when the graph
is missing/malformed. query_graph's subprocess is mocked — no real calls.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from sentinel.orchestrator.graph_context import (
    find_nodes_for_file,
    get_god_nodes,
    get_node_neighbors,
    query_graph,
)


def _node(nid, label, source_file, community, community_name):
    return {
        "id": nid, "label": label, "norm_label": label,
        "file_type": "code", "source_file": source_file, "source_location": "L1",
        "_origin": "ast", "community": community, "community_name": community_name,
    }


def _edge(src, tgt, relation="calls"):
    return {
        "source": src, "target": tgt, "relation": relation,
        "confidence": "high", "confidence_score": 0.9, "weight": 1,
        "source_file": "x.py", "source_location": "L1",
    }


@pytest.fixture
def graph_path(tmp_path):
    # A is the hub (degree 4); B/C/D/E each degree 2.
    nodes = [
        _node("A", "main", "orchestrator/main.py", 1, "Community 1"),
        _node("B", "helper_b", "orchestrator/util.py", 1, "Community 1"),
        _node("C", "helper_c", "assess/reviewer.py", 2, "Community 2"),
        _node("D", "helper_d", "assess/grouper.py", 2, "Community 2"),
        _node("E", "helper_e", "reports/server.py", 3, "Community 3"),
    ]
    edges = [
        _edge("A", "B"), _edge("A", "C"), _edge("A", "D"), _edge("A", "E"),
        _edge("B", "C"), _edge("D", "E"),
    ]
    p = tmp_path / "graph.json"
    p.write_text(json.dumps({"nodes": nodes, "links": edges}), encoding="utf-8")
    return str(p)


# -- get_god_nodes ------------------------------------------------------

def test_god_nodes_sorted_by_degree(graph_path):
    gods = get_god_nodes(graph_path, top_n=10)
    assert gods[0]["id"] == "A"
    assert gods[0]["degree"] == 4
    # descending
    degrees = [g["degree"] for g in gods]
    assert degrees == sorted(degrees, reverse=True)
    # shape
    assert set(gods[0].keys()) == {"id", "label", "source_file", "degree", "community_name"}
    assert gods[0]["community_name"] == "Community 1"


def test_god_nodes_respects_top_n(graph_path):
    gods = get_god_nodes(graph_path, top_n=2)
    assert len(gods) == 2
    assert gods[0]["id"] == "A"


# -- get_node_neighbors -------------------------------------------------

def test_node_neighbors_correct(graph_path):
    nbrs = get_node_neighbors("A", graph_path)
    assert {n["id"] for n in nbrs} == {"B", "C", "D", "E"}
    assert set(nbrs[0].keys()) == {"id", "label", "source_file", "relation"}
    assert all(n["relation"] == "calls" for n in nbrs)


def test_node_neighbors_unknown_node_empty(graph_path):
    assert get_node_neighbors("ZZZ", graph_path) == []


# -- find_nodes_for_file ------------------------------------------------

def test_find_nodes_for_file_case_insensitive_partial(graph_path):
    matches = find_nodes_for_file("MAIN.PY", graph_path)
    assert {m["id"] for m in matches} == {"A"}
    assert set(matches[0].keys()) == {"id", "label", "source_file", "community_name"}


def test_find_nodes_for_file_none_returns_empty(graph_path):
    assert find_nodes_for_file("nonexistent_zzz.py", graph_path) == []


# -- graceful degradation (missing graph) -------------------------------

def test_all_functions_empty_when_graph_missing(tmp_path):
    missing = str(tmp_path / "does_not_exist.json")
    assert get_god_nodes(missing) == []
    assert get_node_neighbors("A", missing) == []
    assert find_nodes_for_file("main.py", missing) == []


def test_god_nodes_empty_when_malformed(tmp_path):
    bad = tmp_path / "graph.json"
    bad.write_text("{ not valid json", encoding="utf-8")
    assert get_god_nodes(str(bad)) == []


# -- query_graph (subprocess mocked) ------------------------------------

def test_query_graph_returns_stripped_stdout(tmp_path):
    class _R:
        stdout = "  the answer  \n"
        stderr = ""
        returncode = 0

    with patch("sentinel.orchestrator.graph_context.subprocess.run", return_value=_R()) as m:
        out = query_graph("what calls build_retry_context", str(tmp_path))
    assert out == "the answer"
    # invoked via `python -m graphify`, cwd = graph_dir
    args, kwargs = m.call_args
    cmd = args[0]
    assert cmd[1:4] == ["-m", "graphify", "query"]
    assert "what calls build_retry_context" in cmd
    assert kwargs.get("cwd") == str(tmp_path)


def test_query_graph_dfs_mode_adds_flag(tmp_path):
    class _R:
        stdout = "x"
        stderr = ""
        returncode = 0

    with patch("sentinel.orchestrator.graph_context.subprocess.run", return_value=_R()) as m:
        query_graph("q", str(tmp_path), mode="dfs")
    assert "--dfs" in m.call_args[0][0]


def test_query_graph_returns_empty_on_error(tmp_path):
    with patch("sentinel.orchestrator.graph_context.subprocess.run", side_effect=OSError("boom")):
        assert query_graph("q", str(tmp_path)) == ""
