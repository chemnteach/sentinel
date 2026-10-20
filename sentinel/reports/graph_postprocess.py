"""Post-processing for graphify output so `sentinel graphify` produces a
self-contained, offline-ready, semantically-labeled graph.

graphify's raw graph.html (a) loads vis-network from a CDN with an
``integrity``/``crossorigin`` pair that a ``file://`` or UNC open cannot
satisfy (CORS blocks the script → blank graph), and (b) leaves communities as
"Community N" placeholders (it is run with ``--no-label`` to avoid needing
graphify's own LLM backend). This module fixes both:

* :func:`make_offline` — vendor vis-network.min.js beside graph.html and rewrite
  the script tag to a plain local reference (no SRI/CORS).
* :func:`name_communities` — name each community from its member files via the
  LLM client (grounded in the file names, nothing invented) and inject the
  names into graph.html (legend + node tooltips) and graph.json.

Both degrade gracefully: a failure logs and leaves the graph usable rather than
raising, so a naming hiccup never costs you the graph.
"""
from __future__ import annotations

import json
import logging
import re
import shutil
from collections import Counter, defaultdict
from pathlib import Path

log = logging.getLogger(__name__)

_ASSETS_DIR = Path(__file__).parent / "assets"
_VIS_ASSET = _ASSETS_DIR / "vis-network.min.js"

# Any vis-network script tag (CDN or local, with or without integrity/crossorigin).
_VIS_SCRIPT_RE = re.compile(
    r'<script\b[^>]*\bsrc="[^"]*vis-network\.min\.js"[^>]*>\s*</script>',
    re.DOTALL,
)
_LOCAL_VIS_TAG = '<script src="vis-network.min.js"></script>'

# Naming: how many communities per LLM call, and how many representative files
# to show the model per community. Batched so a reasoning model does not empty
# out on a dense response.
_NAME_BATCH = 20
_REP_FILES = 15

_NAME_SYSTEM = (
    "You name clusters of source files. For each cluster, produce a concise "
    "2-5 word Title Case name for its apparent responsibility, inferred ONLY "
    "from the provided file names and folder — do NOT invent functionality you "
    "cannot see in the names. Names must be specific and distinct from one "
    'another. Respond with STRICT JSON mapping each id (as a string) to its '
    'name, e.g. {"0":"Wafer Map Translation","1":"E90 Substrate Events"}. '
    "No commentary."
)


def make_offline(graph_dir: Path, html_name: str = "graph.html") -> bool:
    """Vendor vis-network.min.js into ``graph_dir`` and rewrite ``graph.html``'s
    script tag to reference it locally (dropping any CDN URL + SRI/CORS attrs).

    Returns True when the graph was made offline-ready, False if there was
    nothing to do (missing html/asset).
    """
    html = graph_dir / html_name
    if not html.exists():
        log.warning("make_offline: %s not found", html)
        return False
    if not _VIS_ASSET.exists():
        log.warning("make_offline: bundled vis-network asset missing at %s", _VIS_ASSET)
        return False

    shutil.copyfile(_VIS_ASSET, graph_dir / "vis-network.min.js")
    text = html.read_text(encoding="utf-8")
    new_text, n = _VIS_SCRIPT_RE.subn(_LOCAL_VIS_TAG, text)
    if n:
        html.write_text(new_text, encoding="utf-8")
    else:
        log.warning("make_offline: no vis-network script tag matched in %s", html)
    return True


def _degree(graph: dict) -> Counter:
    deg: Counter = Counter()
    for link in graph.get("links", []):
        deg[link.get("source")] += 1
        deg[link.get("target")] += 1
    return deg


def _community_profiles(graph: dict) -> dict[int, dict]:
    deg = _degree(graph)
    by_comm: dict[int, list] = defaultdict(list)
    for node in graph.get("nodes", []):
        by_comm[node.get("community")].append(node)

    profiles: dict[int, dict] = {}
    for cid, members in by_comm.items():
        members = sorted(members, key=lambda n: deg.get(n.get("id"), 0), reverse=True)
        files = [str(n.get("label") or n.get("source_file") or "") for n in members]
        files = [f for f in files if f][:_REP_FILES]
        folders: Counter = Counter()
        for n in members:
            parts = [
                p for p in str(n.get("source_file", "")).replace("\\", "/").split("/") if p
            ][:-1]
            if parts:
                folders["/".join(parts[-2:])] += 1
        dominant = folders.most_common(1)[0][0] if folders else ""
        profiles[cid] = {"files": files, "folder": dominant, "size": len(members)}
    return profiles


def _dedupe(cids: list, raw: dict[int, str], profiles: dict[int, dict]) -> dict[int, str]:
    names: dict[int, str] = {}
    seen: dict[str, int] = {}
    for cid in cids:
        nm = raw.get(cid) or (profiles[cid]["folder"].split("/")[-1] or f"Cluster {cid}")
        if nm in seen:
            seen[nm] += 1
            alt = f"{nm} ({profiles[cid]['folder'].split('/')[-1]})"
            nm = alt if alt not in seen else f"{nm} #{seen[nm]}"
        seen.setdefault(nm, 0)
        names[cid] = nm
    return names


def _inject_names(graph_dir: Path, names: dict[int, str]) -> None:
    """Replace the quoted literal ``"Community <cid>"`` (legend label + every node
    community_name) with the JSON-escaped semantic name, in graph.html + graph.json."""
    for fname in ("graph.html", "graph.json"):
        path = graph_dir / fname
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for cid, nm in names.items():
            text = text.replace(f'"Community {cid}"', json.dumps(nm))
        path.write_text(text, encoding="utf-8")


def name_communities(graph_dir: Path, client, batch: int = _NAME_BATCH) -> int:
    """Name each community from its member files via ``client`` and inject the
    names into graph.html + graph.json. Returns the number of communities named.

    ``client`` must expose ``call_model_json(system, user) -> dict``. Degrades to
    0 (leaving "Community N" placeholders) on any failure — never raises.
    """
    gjson = graph_dir / "graph.json"
    if not gjson.exists():
        log.warning("name_communities: %s not found", gjson)
        return 0
    try:
        graph = json.loads(gjson.read_text(encoding="utf-8"))
        profiles = _community_profiles(graph)
        cids = sorted(profiles)
        raw: dict[int, str] = {}
        for i in range(0, len(cids), batch):
            chunk = cids[i : i + batch]
            payload = [
                {"id": cid, "folder": profiles[cid]["folder"], "files": profiles[cid]["files"]}
                for cid in chunk
            ]
            try:
                res = client.call_model_json(
                    _NAME_SYSTEM, "Name these clusters:\n" + json.dumps(payload, indent=1)
                )
            except Exception as exc:  # noqa: BLE001 — degrade per-batch, keep going
                log.warning("name_communities: batch failed (%s) — keeping placeholders", exc)
                continue
            for cid in chunk:
                nm = res.get(str(cid)) or res.get(cid)
                if isinstance(nm, str) and nm.strip():
                    raw[cid] = nm.strip()
        if not raw:
            return 0
        names = _dedupe(cids, raw, profiles)
        _inject_names(graph_dir, names)
        return len(raw)
    except Exception as exc:  # noqa: BLE001 — a naming failure must not lose the graph
        log.warning("name_communities: aborted (%s) — graph left unlabeled", exc)
        return 0


def postprocess_graph(graph_dir: Path, client=None) -> dict:
    """Make the graph offline-ready and (when ``client`` is given) semantically
    name its communities. Returns a small summary dict."""
    graph_dir = Path(graph_dir)
    offline = make_offline(graph_dir)
    named = name_communities(graph_dir, client) if client is not None else 0
    return {"offline": offline, "named": named}


__all__ = ["make_offline", "name_communities", "postprocess_graph"]
