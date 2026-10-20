"""sentinel/orchestrator/graph_context.py

Phase 5 — Graphify integration wrapper.

Thin, read-only helpers over an existing Graphify ``graph.json`` (produced by
``sentinel graphify`` / ``python -m graphify``). Used by the Flask UI to render
the code knowledge graph and by the finding->graph linking hook.

Every function degrades gracefully: a missing or malformed graph yields an empty
result and a logged warning — Sentinel must work without a graph present. The
graph is loaded directly (``json`` + ``networkx``); only ``query_graph`` shells
out, and it does so via ``python -m graphify`` (not the bare ``graphify``
executable) to avoid PATH issues.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

import networkx as nx

log = logging.getLogger(__name__)


def _load_graph(graph_json_path: str) -> dict | None:
    """Load and parse graph.json, or return None (logged) on any problem."""
    path = Path(graph_json_path)
    if not path.exists():
        log.warning("graph.json not found: %s", graph_json_path)
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("graph.json unreadable/malformed (%s): %s", graph_json_path, exc)
        return None
    if not isinstance(data, dict):
        log.warning("graph.json is not a JSON object: %s", graph_json_path)
        return None
    return data


def _edges(data: dict) -> list[dict]:
    edges = data.get("links", data.get("edges", []))
    return edges if isinstance(edges, list) else []


def get_god_nodes(graph_json_path: str, top_n: int = 10) -> list[dict]:
    """Return the ``top_n`` highest-degree ("god") nodes, degree descending.

    Each dict: ``{id, label, source_file, degree, community_name}``. Empty list
    when the graph is missing/malformed."""
    data = _load_graph(graph_json_path)
    if data is None:
        return []
    nodes = data.get("nodes", [])
    if not isinstance(nodes, list) or not nodes:
        return []

    graph = nx.MultiDiGraph()
    nodes_by_id: dict[Any, dict] = {}
    for n in nodes:
        nid = n.get("id")
        if nid is None:
            continue
        nodes_by_id[nid] = n
        graph.add_node(nid)
    for e in _edges(data):
        src, tgt = e.get("source"), e.get("target")
        if src is not None and tgt is not None:
            graph.add_edge(src, tgt)

    ranked = sorted(nodes_by_id, key=lambda nid: graph.degree(nid), reverse=True)
    result: list[dict] = []
    for nid in ranked[: max(0, top_n)]:
        n = nodes_by_id[nid]
        result.append({
            "id": nid,
            "label": n.get("label", ""),
            "source_file": n.get("source_file", ""),
            "degree": int(graph.degree(nid)),
            "community_name": n.get("community_name", ""),
        })
    return result


def query_graph(question: str, graph_dir: str, mode: str = "bfs") -> str:
    """Run ``python -m graphify query "<question>"`` in ``graph_dir``.

    ``mode="dfs"`` appends ``--dfs`` (default BFS traversal). Returns stdout
    stripped, or "" on any error."""
    cmd = [sys.executable, "-m", "graphify", "query", question]
    if mode == "dfs":
        cmd.append("--dfs")
    try:
        result = subprocess.run(
            cmd, cwd=graph_dir, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("graphify query failed: %s", exc)
        return ""
    return (result.stdout or "").strip()


def get_node_neighbors(node_id: str, graph_json_path: str) -> list[dict]:
    """Return all neighbors of ``node_id`` (both edge directions).

    Each dict: ``{id, label, source_file, relation}`` (relation from the
    connecting edge). Empty list when the node is absent or the graph is
    missing/malformed."""
    data = _load_graph(graph_json_path)
    if data is None:
        return []
    nodes_by_id = {n.get("id"): n for n in data.get("nodes", []) if isinstance(n, dict)}
    if node_id not in nodes_by_id:
        return []

    graph = nx.MultiDiGraph()
    for nid in nodes_by_id:
        graph.add_node(nid)
    for e in _edges(data):
        graph.add_edge(e.get("source"), e.get("target"))

    result: list[dict] = []
    for e in _edges(data):
        src, tgt = e.get("source"), e.get("target")
        if src == node_id:
            other = tgt
        elif tgt == node_id:
            other = src
        else:
            continue
        nb = nodes_by_id.get(other)
        if nb is None:
            continue
        result.append({
            "id": other,
            "label": nb.get("label", ""),
            "source_file": nb.get("source_file", ""),
            "relation": e.get("relation", ""),
        })
    return result


def find_nodes_for_file(source_file: str, graph_json_path: str) -> list[dict]:
    """Return nodes whose ``source_file`` matches ``source_file`` (case-
    insensitive, partial). Each dict: ``{id, label, source_file,
    community_name}``. Empty list when none match or the graph is missing."""
    data = _load_graph(graph_json_path)
    if data is None:
        return []
    needle = (source_file or "").lower()
    if not needle:
        return []
    result: list[dict] = []
    for n in data.get("nodes", []):
        if not isinstance(n, dict):
            continue
        node_file = str(n.get("source_file", "") or "").lower()
        if node_file and needle in node_file:
            result.append({
                "id": n.get("id"),
                "label": n.get("label", ""),
                "source_file": n.get("source_file", ""),
                "community_name": n.get("community_name", ""),
            })
    return result


__all__ = [
    "get_god_nodes",
    "query_graph",
    "get_node_neighbors",
    "find_nodes_for_file",
]
