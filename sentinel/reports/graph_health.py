"""Structural-health read of a codebase from its Graphify graph.

The Decision Brief covers *defects* (is the code correct?); this covers
*architecture* (is it built well?) — the complementary lens. It reads the
graphify ``graph.json`` and derives a few operator-legible signals:

* coupling hotspots — the most-connected owner entities (change-amplifiers /
  potential god-objects),
* modularity — how self-contained the clusters are (intra- vs cross-cluster
  edges),
* least-modular clusters — functional areas most coupled to the rest,
* circular dependencies — files that mutually depend on each other.

Everything degrades gracefully: a missing/unreadable/mis-shaped graph yields
``None`` so the handoff simply omits the section rather than failing.
"""
from __future__ import annotations

import html
import json
import logging
from collections import Counter, defaultdict
from pathlib import Path

log = logging.getLogger(__name__)

# A node counts as "owner code" (vs a framework type like ``string``/``List``)
# when its source_file looks like a real repo source path. Framework types are
# high-degree because everything uses them, and would drown out the real hubs.
_CODE_EXTS = (".cs", ".vb", ".vbs", ".xml", ".config", ".py", ".ts", ".js", ".go", ".rs")

# Dependency relations (exclude structural containment: ``contains``/``method``).
_DEP_RELATIONS = frozenset(
    {"calls", "references", "imports", "inherits", "implements"}
)

_TOP_HUBS = 6
_TOP_CLUSTERS = 5
_MIN_CLUSTER_SIZE = 5   # ignore tiny clusters when ranking modularity
_MAX_CYCLE_EXAMPLES = 6


def _is_owner(node: dict) -> bool:
    sf = str(node.get("source_file", "") or "").replace("\\", "/")
    return "/" in sf and sf.lower().endswith(_CODE_EXTS)


def compute_health(graph_json_path: str | Path) -> dict | None:
    """Compute structural-health signals from a graphify ``graph.json``.

    Returns a dict (see module docstring for the shape) or ``None`` on any
    missing/unreadable/mis-shaped input — callers omit the section on None.
    """
    path = Path(graph_json_path)
    if not path.exists():
        return None
    try:
        g = json.loads(path.read_text(encoding="utf-8"))
        nodes = {n["id"]: n for n in g.get("nodes", []) if isinstance(n, dict) and "id" in n}
        links = [l for l in g.get("links", []) if isinstance(l, dict)]
        if not nodes or not links:
            return None

        deg: Counter = Counter()
        for l in links:
            deg[l.get("source")] += 1
            deg[l.get("target")] += 1

        # Coupling hotspots — most-connected owner entities.
        hubs: list[dict] = []
        for nid, d in deg.most_common():
            n = nodes.get(nid)
            if not n or not _is_owner(n):
                continue
            hubs.append({
                "label": str(n.get("label") or nid),
                "connections": d,
                "area": str(n.get("community_name") or ""),
            })
            if len(hubs) >= _TOP_HUBS:
                break

        # Modularity — intra- vs cross-cluster edges.
        intra = cross = 0
        c_cross: Counter = Counter()
        c_tot: Counter = Counter()
        for l in links:
            s = nodes.get(l.get("source"), {}).get("community")
            t = nodes.get(l.get("target"), {}).get("community")
            if s is None or t is None:
                continue
            c_tot[s] += 1
            c_tot[t] += 1
            if s == t:
                intra += 1
            else:
                cross += 1
                c_cross[s] += 1
                c_cross[t] += 1
        edges_scoped = intra + cross
        intra_pct = round(100 * intra / edges_scoped) if edges_scoped else 0

        # Least-modular clusters — highest external-edge fraction (size-filtered).
        size = Counter(n.get("community") for n in nodes.values())
        cname = {n.get("community"): n.get("community_name") for n in nodes.values()}
        ranked = sorted(
            (
                {
                    "area": str(cname.get(c) or f"Community {c}"),
                    "external_pct": round(100 * c_cross[c] / c_tot[c]),
                    "size": size[c],
                }
                for c in c_tot
                if size[c] >= _MIN_CLUSTER_SIZE and c_tot[c] > 0
            ),
            key=lambda x: -x["external_pct"],
        )
        least_modular = ranked[:_TOP_CLUSTERS]

        # Circular dependencies — file pairs that mutually depend (dep relations).
        fedge: dict[str, set] = defaultdict(set)
        for l in links:
            if l.get("relation") not in _DEP_RELATIONS:
                continue
            s = nodes.get(l.get("source"), {})
            t = nodes.get(l.get("target"), {})
            sf, tf = str(s.get("source_file", "")), str(t.get("source_file", ""))
            if sf and tf and sf != tf and _is_owner(s) and _is_owner(t):
                fedge[sf].add(tf)
        cyc_pairs = set()
        for a, outs in fedge.items():
            for b in outs:
                if a in fedge.get(b, ()):
                    cyc_pairs.add(tuple(sorted((a, b))))

        return {
            "scale": {
                "entities": len(nodes),
                "relationships": len(links),
                "communities": len(size),
            },
            "modularity": {"intra": intra, "cross": cross, "intra_pct": intra_pct},
            "hubs": hubs,
            "least_modular": least_modular,
            "circular_deps": {
                "count": len(cyc_pairs),
                "pairs": [
                    [Path(a).name, Path(b).name]
                    for a, b in list(cyc_pairs)[:_MAX_CYCLE_EXAMPLES]
                ],
            },
        }
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.warning("graph_health: could not compute from %s: %s", path, exc)
        return None


def render_health_html(health: dict) -> str:
    """Render the structural-health section as HTML for the handoff document."""
    e = html.escape
    sc = health["scale"]
    mod = health["modularity"]
    P: list[str] = ["<h2>Structural Health (architecture)</h2>"]
    P.append(
        "<p class=\"meta\">A read of how the code is <b>built</b> — coupling and "
        "modularity — from the codebase graph. This is the complement to the "
        "findings above: the findings say whether the code is <b>correct</b>; "
        "this says whether it is <b>well-structured</b>. It flags no defects.</p>"
    )
    P.append(
        f'<p class="meta">{sc["entities"]} code entities, '
        f'{sc["relationships"]} relationships, {sc["communities"]} functional '
        f'clusters. <b>{mod["intra_pct"]}%</b> of connections stay inside a '
        "cluster (higher = cleaner separation of concerns).</p>"
    )

    # Coupling hotspots.
    if health["hubs"]:
        P.append("<h3 style=\"font-size:15px;margin:14px 0 6px\">Coupling hotspots</h3>")
        P.append(
            "<p class=\"meta\">The most-connected components — heavily depended on, "
            "so changes here ripple furthest. Worth extra review care, not "
            "necessarily wrong.</p>"
        )
        P.append("<table><thead><tr><th>Component</th><th>Connections</th>"
                 "<th>Area</th></tr></thead><tbody>")
        for h in health["hubs"]:
            P.append(
                f"<tr><td>{e(h['label'])}</td><td>{h['connections']}</td>"
                f"<td>{e(h['area'])}</td></tr>"
            )
        P.append("</tbody></table>")

    # Least-modular clusters.
    if health["least_modular"]:
        P.append("<h3 style=\"font-size:15px;margin:14px 0 6px\">Most-coupled areas</h3>")
        P.append(
            "<p class=\"meta\">Functional areas whose connections mostly reach "
            "<i>outside</i> the area — the least self-contained, hardest to change "
            "in isolation.</p>"
        )
        P.append("<table><thead><tr><th>Area</th><th>External connections</th>"
                 "<th>Size</th></tr></thead><tbody>")
        for c in health["least_modular"]:
            P.append(
                f"<tr><td>{e(c['area'])}</td><td>{c['external_pct']}%</td>"
                f"<td>{c['size']} entities</td></tr>"
            )
        P.append("</tbody></table>")

    # Circular dependencies.
    cyc = health["circular_deps"]
    P.append("<h3 style=\"font-size:15px;margin:14px 0 6px\">Circular dependencies</h3>")
    if cyc["count"] == 0:
        P.append(
            "<p class=\"meta\" style=\"color:#1e7e34\">None found &#10003; — no files "
            "mutually depend on each other (a healthy sign).</p>"
        )
    else:
        P.append(
            f"<p class=\"meta\"><b>{cyc['count']}</b> file pair(s) depend on each "
            "other (circular dependencies make changes harder to isolate):</p>"
        )
        P.append("<ul>")
        for a, b in cyc["pairs"]:
            P.append(f"<li><span class=\"mono\">{e(a)}</span> &harr; "
                     f"<span class=\"mono\">{e(b)}</span></li>")
        P.append("</ul>")
        if cyc["count"] > len(cyc["pairs"]):
            P.append(f'<p class="meta">... and {cyc["count"] - len(cyc["pairs"])} more.</p>')
    return "".join(P)


__all__ = ["compute_health", "render_health_html"]
