"""
sentinel/orchestrator/bundle_planner.py

S2-2 — Bundle-aware Fix Planner engine (Stage 5d).

A *bundle* is a cluster of findings that a single remediation is expected to
resolve together (or that are causally related and should be reviewed as a
set). Bundling turns a flat list of findings into root-cause-anchored units so
the operator triages "fix this one root cause" instead of N independent
symptoms, and the resulting PR describes the whole cascade.

Confidence levels
-----------------
HIGH      Same file + overlapping/adjacent lines OR same file + same method.
          One fix resolves every member (all ``predicted_resolved=True``).
MEDIUM    Same file + a known causal kind pair OR same file + same class/module.
          One fix is expected to resolve the cascade (``predicted_resolved=True``).
LOW       Cross-file findings with a causal relationship. RELATED, not merged:
          they share a ``bundle_id`` but remain separate tasks; cascade members
          are ``predicted_resolved=False`` (predicted only, not co-fixed).
SINGLETON A finding that bundles with nothing — its own one-member bundle.

The engine never crashes on a finding lacking line information: line lookup
degrades gracefully (verified_location -> line_range -> location parse -> None).
"""

from __future__ import annotations

import logging
from typing import Any

from sentinel.assess.finding_status import BundleStatus

log = logging.getLogger(__name__)


class BundlePlanner:
    """Cluster findings into root-cause-anchored remediation bundles."""

    # Two findings in the same file whose line windows are within this many
    # lines of each other are treated as a single high-confidence unit.
    HIGH_CONFIDENCE_LINE_THRESHOLD = 30

    # root cause kind -> kinds it plausibly *causes*. Used (either direction) to
    # decide MEDIUM (same file) / LOW (cross file) causal relationships.
    CAUSAL_PAIRS: dict[str, list[str]] = {
        "timeout_handling_gap": [
            "error_signal_loss",
            "partial_failure_handling_gap",
            "observability_gap",
        ],
        "missing_transaction_boundary": [
            "silent_data_loss",
            "concurrency_update_risk",
        ],
        "logic_bug": [
            "error_signal_loss",
            "contract_violation",
        ],
        "injection_risk": [
            "trust_boundary_gap",
            "authorization_gap",
        ],
    }

    # Severity ordering for root-cause selection (higher = more severe).
    _SEVERITY_RANK = {
        "CRITICAL": 4,
        "P1": 3,
        "P2": 2,
        "P3": 1,
    }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build_bundles(self, findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Group ``findings`` into bundles.

        Returns a list of bundle dicts (see module docstring for the schema),
        with deterministic ``BUNDLE-00N`` ids assigned in a stable order.
        """
        if not findings:
            return []

        # Deterministic input order: preserve the caller's order but make the
        # union-find and id assignment stable by indexing.
        indexed = list(enumerate(findings))

        # Union-Find over finding indices. We record, per merged edge, the
        # confidence it carries so a bundle's overall confidence is the weakest
        # (least certain) edge that formed it: HIGH > MEDIUM > LOW.
        parent = list(range(len(indexed)))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                # Keep the lower root for determinism.
                if ra < rb:
                    parent[rb] = ra
                else:
                    parent[ra] = rb

        # Track the strongest relationship found between any two findings that
        # end up in the same bundle, and per-pair causal direction so root-cause
        # tie-breaks can count "how many others causally depend on me".
        # edge_confidence[(i, j)] where i < j -> "HIGH"|"MEDIUM"|"LOW"
        edge_confidence: dict[tuple[int, int], str] = {}
        # causal_dependents[i] = number of findings that i causally causes
        causal_dependents: dict[int, int] = {i: 0 for i, _ in indexed}
        # cross_file_pairs: (i, j) pairs joined only by a LOW cross-file edge
        cross_file_edges: set[tuple[int, int]] = set()

        n = len(indexed)
        for a in range(n):
            for b in range(a + 1, n):
                fa = indexed[a][1]
                fb = indexed[b][1]
                rel = self._relationship(fa, fb)
                if rel is None:
                    continue
                confidence, direction = rel
                union(a, b)
                key = (a, b)
                # Keep the strongest confidence seen for this pair.
                prior = edge_confidence.get(key)
                if prior is None or self._confidence_rank(confidence) > self._confidence_rank(prior):
                    edge_confidence[key] = confidence
                if confidence == "LOW":
                    cross_file_edges.add(key)
                # Record causal direction for the tie-break (direction is the
                # index — a or b — that is the causal *root*).
                if direction is not None:
                    dependent = b if direction == a else a
                    causal_dependents[direction] = causal_dependents.get(direction, 0) + 1

        # Collect components.
        components: dict[int, list[int]] = {}
        for i, _ in indexed:
            components.setdefault(find(i), []).append(i)

        # Stable component order: by the smallest member index (deterministic).
        ordered_roots = sorted(components, key=lambda r: min(components[r]))

        bundles: list[dict[str, Any]] = []
        for seq, root in enumerate(ordered_roots, start=1):
            members = sorted(components[root])  # by original index
            bundle = self._build_bundle(
                seq=seq,
                member_indices=members,
                indexed=indexed,
                edge_confidence=edge_confidence,
                causal_dependents=causal_dependents,
                cross_file_edges=cross_file_edges,
            )
            bundles.append(bundle)
        return bundles

    # ------------------------------------------------------------------
    # Relationship classification
    # ------------------------------------------------------------------

    def _relationship(
        self, fa: dict[str, Any], fb: dict[str, Any]
    ) -> tuple[str, int | None] | None:
        """Classify the relationship between two findings.

        Returns ``(confidence, causal_root_marker)`` or ``None`` when unrelated.
        ``causal_root_marker`` is a sentinel used by ``build_bundles`` to record
        which finding is the causal root; here it is ``0`` for ``fa`` and ``1``
        for ``fb`` (translated to indices by the caller), or ``None`` for a
        non-causal (proximity/method/class) relationship.
        """
        file_a = str(fa.get("file", "") or "")
        file_b = str(fb.get("file", "") or "")
        same_file = bool(file_a) and file_a == file_b

        kind_a = self._kind(fa)
        kind_b = self._kind(fb)

        # --- HIGH: same file + overlapping/adjacent lines -------------
        if same_file:
            if self._lines_close(fa, fb):
                return ("HIGH", None)
            # HIGH: same file + same method name in location
            method_a = self._method(fa)
            method_b = self._method(fb)
            if method_a and method_a == method_b:
                return ("HIGH", None)

        # --- causal pair? ---------------------------------------------
        causal_root = self._causal_root_marker(kind_a, kind_b)

        if same_file:
            # MEDIUM: same file + causal kind pair
            if causal_root is not None:
                return ("MEDIUM", causal_root)
            # MEDIUM: same file + same class/module in location
            cls_a = self._class_or_module(fa)
            cls_b = self._class_or_module(fb)
            if cls_a and cls_a == cls_b:
                return ("MEDIUM", None)
            return None

        # --- LOW: cross-file + causal relationship --------------------
        if causal_root is not None:
            return ("LOW", causal_root)
        return None

    def _causal_root_marker(self, kind_a: str, kind_b: str) -> int | None:
        """Return 0 if ``kind_a`` causes ``kind_b``, 1 if ``kind_b`` causes
        ``kind_a`` (per CAUSAL_PAIRS, either direction), else ``None``."""
        if kind_b in self.CAUSAL_PAIRS.get(kind_a, []):
            return 0
        if kind_a in self.CAUSAL_PAIRS.get(kind_b, []):
            return 1
        return None

    # ------------------------------------------------------------------
    # Bundle assembly
    # ------------------------------------------------------------------

    def _build_bundle(
        self,
        *,
        seq: int,
        member_indices: list[int],
        indexed: list[tuple[int, dict[str, Any]]],
        edge_confidence: dict[tuple[int, int], str],
        causal_dependents: dict[int, int],
        cross_file_edges: set[tuple[int, int]],
    ) -> dict[str, Any]:
        bundle_id = f"BUNDLE-{seq:03d}"
        members = [indexed[i][1] for i in member_indices]

        # Confidence: SINGLETON if one member, else the weakest edge that formed
        # the component (HIGH strongest, LOW weakest).
        if len(member_indices) == 1:
            confidence = "SINGLETON"
        else:
            confidence = self._component_confidence(member_indices, edge_confidence)

        # Root cause selection.
        root_idx = self._select_root(member_indices, indexed, causal_dependents)
        root = indexed[root_idx][1]

        # predicted_resolved per member.
        cross_file_member_indices = self._cross_file_member_indices(
            member_indices, cross_file_edges
        )
        member_findings = []
        acceptance_criteria = []
        for i in member_indices:
            f = indexed[i][1]
            fid = str(f.get("id", "") or "")
            is_root = i == root_idx
            if is_root:
                predicted = True
            elif confidence == "LOW" or i in cross_file_member_indices:
                # cross-file cascade members are predicted only, not co-fixed
                predicted = False
            else:
                # HIGH/MEDIUM cascade members: one fix resolves them
                predicted = True
            member_findings.append(
                {
                    "finding_id": fid,
                    "severity": str(f.get("severity", "") or ""),
                    "title": str(f.get("title", "") or ""),
                    "predicted_resolved": predicted,
                    "finding_kind": self._kind(f),
                }
            )
            acceptance_criteria.append(self._acceptance_criterion(f))

        affected_files = self._dedup_preserve(
            str(f.get("file", "") or "") for f in members if f.get("file")
        )

        return {
            "bundle_id": bundle_id,
            "confidence": confidence,
            "root_cause_finding_id": str(root.get("id", "") or ""),
            "root_cause_severity": str(root.get("severity", "") or ""),
            "root_cause_title": str(root.get("title", "") or ""),
            "affected_files": affected_files,
            "member_findings": member_findings,
            "bundle_status": BundleStatus.UNDECIDED,
            "acceptance_criteria": acceptance_criteria,
        }

    def _component_confidence(
        self,
        member_indices: list[int],
        edge_confidence: dict[tuple[int, int], str],
    ) -> str:
        """The weakest confidence among the edges internal to this component.

        A bundle held together by any LOW edge is LOW; if it has MEDIUM but no
        LOW it is MEDIUM; only all-HIGH edges make it HIGH.
        """
        member_set = set(member_indices)
        internal = [
            conf
            for (a, b), conf in edge_confidence.items()
            if a in member_set and b in member_set
        ]
        if not internal:
            # Should not happen for a multi-member component, but be safe.
            return "MEDIUM"
        # weakest = lowest rank
        return min(internal, key=self._confidence_rank)

    def _cross_file_member_indices(
        self,
        member_indices: list[int],
        cross_file_edges: set[tuple[int, int]],
    ) -> set[int]:
        """Indices that participate only via a cross-file (LOW) edge."""
        member_set = set(member_indices)
        result: set[int] = set()
        for a, b in cross_file_edges:
            if a in member_set and b in member_set:
                result.add(a)
                result.add(b)
        return result

    def _select_root(
        self,
        member_indices: list[int],
        indexed: list[tuple[int, dict[str, Any]]],
        causal_dependents: dict[int, int],
    ) -> int:
        """Root cause = highest severity; tie -> most causal dependents;
        still tie -> first in file (lowest start line, then original index)."""

        def key(i: int):
            f = indexed[i][1]
            sev = self._severity_rank(f.get("severity"))
            dependents = causal_dependents.get(i, 0)
            start = self._start_line(f)
            start_sort = start if start is not None else 10**9
            # Higher severity, more dependents first; lower start line, lower
            # original index last (all as ascending sort with negation).
            return (-sev, -dependents, start_sort, i)

        return min(member_indices, key=key)

    # ------------------------------------------------------------------
    # Finding field helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _kind(f: dict[str, Any]) -> str:
        return str(f.get("finding_kind", f.get("kind", "")) or "")

    def _severity_rank(self, severity: Any) -> int:
        return self._SEVERITY_RANK.get(str(severity or "").strip().upper(), 0)

    @staticmethod
    def _confidence_rank(confidence: str) -> int:
        return {"HIGH": 3, "MEDIUM": 2, "LOW": 1, "SINGLETON": 0}.get(confidence, 0)

    def _lines_close(self, fa: dict[str, Any], fb: dict[str, Any]) -> bool:
        """True when both findings have resolvable line windows in the same
        file and those windows overlap or sit within the HIGH threshold."""
        ra = self._line_window(fa)
        rb = self._line_window(fb)
        if ra is None or rb is None:
            return False
        a_start, a_end = ra
        b_start, b_end = rb
        # Overlap.
        if a_start <= b_end and b_start <= a_end:
            return True
        # Gap between the two windows.
        gap = b_start - a_end if b_start > a_end else a_start - b_end
        return gap <= self.HIGH_CONFIDENCE_LINE_THRESHOLD

    def _line_window(self, f: dict[str, Any]) -> tuple[int, int] | None:
        """Resolve a (start, end) line window: verified_location, then
        line_range, then None. Never raises."""
        vloc = f.get("verified_location")
        if isinstance(vloc, dict):
            start = vloc.get("start")
            end = vloc.get("end")
            if isinstance(start, int) and isinstance(end, int):
                return (start, end)
            if isinstance(start, int):
                return (start, start)
        rng = self._parse_line_range(f.get("line_range"))
        if rng is not None:
            return rng
        return None

    @staticmethod
    def _parse_line_range(value: Any) -> tuple[int, int] | None:
        if not value:
            return None
        text = str(value).strip()
        if not text:
            return None
        try:
            parts = text.split("-")
            start = int(parts[0].strip())
            end = int(parts[-1].strip())
            if start > end:
                start, end = end, start
            return (start, end)
        except (ValueError, IndexError):
            return None

    def _start_line(self, f: dict[str, Any]) -> int | None:
        window = self._line_window(f)
        return window[0] if window else None

    @staticmethod
    def _method(f: dict[str, Any]) -> str:
        """Method/function name from the ``location`` field.

        location is typically ``file:function`` or ``file:Class.method`` (with
        an optional trailing ``, lines N-M``). Returns the trailing symbol name.
        """
        location = str(f.get("location", "") or "")
        if ":" not in location:
            return ""
        tail = location.split(":")[-1].strip()
        tail = tail.split(",")[0].strip()
        tail = tail.split(" ")[0].strip()
        # For Class.method, the method is the last dotted segment.
        return tail.split(".")[-1] if tail else ""

    @staticmethod
    def _class_or_module(f: dict[str, Any]) -> str:
        """Class/module qualifier from ``location`` — the segment *before* the
        method when the location is ``file:Class.method``. Empty for bare
        ``file:function`` (no class qualifier)."""
        location = str(f.get("location", "") or "")
        if ":" not in location:
            return ""
        tail = location.split(":")[-1].strip()
        tail = tail.split(",")[0].strip()
        tail = tail.split(" ")[0].strip()
        if "." in tail:
            return tail.rsplit(".", 1)[0]
        return ""

    def _acceptance_criterion(self, f: dict[str, Any]) -> str:
        """One acceptance criterion derived from a member finding."""
        title = str(f.get("title", "") or "").strip()
        kind = self._kind(f)
        fid = str(f.get("id", "") or "")
        if title:
            return f"Resolve: {title} ({fid})"
        if kind:
            return f"Resolve {kind} finding {fid}"
        return f"Resolve finding {fid}"

    @staticmethod
    def _dedup_preserve(items) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for it in items:
            if it and it not in seen:
                seen.add(it)
                out.append(it)
        return out


# ---------------------------------------------------------------------------
# Plan-item stamping + member exclusion (module-level helpers)
# ---------------------------------------------------------------------------

_PLAN_TIERS = ("blocking", "required", "desired", "nice_to_have")


def stamp_plan_items_with_bundles(
    plan: dict[str, Any],
    bundles: list[dict[str, Any]],
) -> None:
    """Stamp ``bundle_id`` + ``bundle_status`` onto each plan item in place.

    A plan item takes the bundle of its ``source_findings``. When an item's
    findings span multiple bundles, the item takes the bundle whose root cause
    it contains; if it contains no root cause, the first matching bundle (by
    bundle id order) wins. Items whose findings match no bundle are left
    unstamped (backward compatible).
    """
    # finding_id -> bundle
    fid_to_bundle: dict[str, dict[str, Any]] = {}
    root_fids: set[str] = set()
    for b in bundles:
        rc = str(b.get("root_cause_finding_id", "") or "")
        if rc:
            root_fids.add(rc)
        for m in b.get("member_findings", []):
            fid = str(m.get("finding_id", "") or "")
            if fid:
                fid_to_bundle.setdefault(fid, b)

    for tier in _PLAN_TIERS:
        for item in plan.get(tier, []) or []:
            source_findings = [
                str(x) for x in (item.get("source_findings") or [])
            ]
            matched = [
                fid_to_bundle[fid] for fid in source_findings if fid in fid_to_bundle
            ]
            if not matched:
                continue
            # Prefer the bundle whose root cause this item contains. When the
            # item contains several root causes, take the lowest bundle_id
            # (deterministic); otherwise fall back to the first matching bundle
            # by bundle_id order.
            root_matches = [
                fid_to_bundle[fid]
                for fid in source_findings
                if fid in root_fids and fid in fid_to_bundle
            ]
            if root_matches:
                chosen = min(root_matches, key=lambda b: str(b.get("bundle_id", "")))
            else:
                chosen = min(matched, key=lambda b: str(b.get("bundle_id", "")))
            item["bundle_id"] = chosen.get("bundle_id")
            item["bundle_status"] = BundleStatus.UNDECIDED


def exclude_member(
    bundle: dict[str, Any], finding_id: str
) -> tuple[dict[str, Any], str]:
    """Exclude a member finding from a bundle (operator DEFER).

    Returns ``(updated_bundle, deferred_finding_id)``. The bundle is copied,
    not mutated. Excluding the root cause is rejected (the whole bundle would be
    rootless) — raise ``ValueError`` instead.
    """
    root = str(bundle.get("root_cause_finding_id", "") or "")
    if finding_id == root:
        raise ValueError(
            f"Cannot exclude {finding_id!r}: it is the bundle's root cause. "
            "Defer the whole bundle instead."
        )
    members = [
        m
        for m in bundle.get("member_findings", [])
        if str(m.get("finding_id", "")) != finding_id
    ]
    updated = dict(bundle)
    updated["member_findings"] = members
    return updated, finding_id


__all__ = [
    "BundlePlanner",
    "stamp_plan_items_with_bundles",
    "exclude_member",
]
