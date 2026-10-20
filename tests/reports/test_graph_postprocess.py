"""Tests for sentinel.reports.graph_postprocess — the graphify post-processing
that makes the graph offline-ready and names its communities.
"""
from __future__ import annotations

import json
from pathlib import Path

from sentinel.reports.graph_postprocess import (
    make_offline,
    name_communities,
    postprocess_graph,
)

# A graphify-style graph.html: CDN vis-network with SRI + crossorigin (the combo
# that blanks the graph on a file:///UNC open), a LEGEND, and node community_name.
_CDN_TAG = (
    '<script src="https://unpkg.com/vis-network@9.1.6/standalone/umd/'
    'vis-network.min.js"\n'
    '        integrity="sha384-DEADBEEF"\n'
    '        crossorigin="anonymous"></script>'
)


def _graph_json():
    return {
        "nodes": [
            {"id": "a", "label": "Auth.cs", "source_file": "src/auth/Auth.cs",
             "community": 0, "community_name": "Community 0"},
            {"id": "b", "label": "Login.cs", "source_file": "src/auth/Login.cs",
             "community": 0, "community_name": "Community 0"},
            {"id": "c", "label": "Db.cs", "source_file": "src/data/Db.cs",
             "community": 1, "community_name": "Community 1"},
        ],
        "links": [
            {"source": "a", "target": "b"},
            {"source": "a", "target": "c"},
        ],
    }


def _graph_html():
    return (
        "<!DOCTYPE html><html><head>\n" + _CDN_TAG + "\n</head><body>\n"
        '<script>\n'
        'const LEGEND = [{"cid": 0, "color": "#111", "label": "Community 0", "count": 2},'
        ' {"cid": 1, "color": "#222", "label": "Community 1", "count": 1}];\n'
        'const RAW_NODES = [{"id":"a","community":0,"community_name":"Community 0"},'
        '{"id":"b","community":0,"community_name":"Community 0"},'
        '{"id":"c","community":1,"community_name":"Community 1"}];\n'
        '</script></body></html>'
    )


def _write(dirp: Path):
    (dirp / "graph.html").write_text(_graph_html(), encoding="utf-8")
    (dirp / "graph.json").write_text(json.dumps(_graph_json()), encoding="utf-8")


class _FakeClient:
    def __init__(self, mapping=None, exc=None):
        self._mapping = mapping or {}
        self._exc = exc
        self.calls = 0

    def call_model_json(self, system, user):
        self.calls += 1
        if self._exc:
            raise self._exc
        return self._mapping


# --------------------------------------------------------------- make_offline

def test_make_offline_vendors_lib_and_strips_cdn_sri_cors(tmp_path):
    _write(tmp_path)
    assert make_offline(tmp_path) is True
    # vis-network.min.js is vendored beside graph.html.
    assert (tmp_path / "vis-network.min.js").exists()
    assert (tmp_path / "vis-network.min.js").stat().st_size > 100_000
    html = (tmp_path / "graph.html").read_text(encoding="utf-8")
    # Clean local tag; no CDN URL, no integrity, no crossorigin.
    assert '<script src="vis-network.min.js"></script>' in html
    assert "unpkg.com" not in html
    assert "integrity" not in html
    assert "crossorigin" not in html


def test_make_offline_missing_html_returns_false(tmp_path):
    assert make_offline(tmp_path) is False


# ------------------------------------------------------------- name_communities

def test_name_communities_injects_names(tmp_path):
    _write(tmp_path)
    client = _FakeClient({"0": "Authentication", "1": "Data Access"})
    n = name_communities(tmp_path, client)
    assert n == 2
    html = (tmp_path / "graph.html").read_text(encoding="utf-8")
    gjson = (tmp_path / "graph.json").read_text(encoding="utf-8")
    # Names injected into legend + node community_name in both files.
    assert "Authentication" in html and "Data Access" in html
    assert "Authentication" in gjson and "Data Access" in gjson
    # No placeholders remain.
    assert '"Community 0"' not in html and '"Community 1"' not in html
    assert '"Community 0"' not in gjson


def test_name_communities_degrades_on_client_error(tmp_path):
    _write(tmp_path)
    client = _FakeClient(exc=RuntimeError("gateway down"))
    n = name_communities(tmp_path, client)
    assert n == 0
    # Placeholders preserved — the graph is still usable.
    html = (tmp_path / "graph.html").read_text(encoding="utf-8")
    assert '"Community 0"' in html


def test_name_communities_no_graph_json_returns_zero(tmp_path):
    assert name_communities(tmp_path, _FakeClient({"0": "x"})) == 0


# ------------------------------------------------------------- postprocess_graph

def test_postprocess_graph_offline_only_without_client(tmp_path):
    _write(tmp_path)
    summary = postprocess_graph(tmp_path, client=None)
    assert summary["offline"] is True
    assert summary["named"] == 0
    # Still offline-ready even though naming was skipped.
    assert (tmp_path / "vis-network.min.js").exists()


def test_postprocess_graph_full(tmp_path):
    _write(tmp_path)
    client = _FakeClient({"0": "Authentication", "1": "Data Access"})
    summary = postprocess_graph(tmp_path, client=client)
    assert summary == {"offline": True, "named": 2}
