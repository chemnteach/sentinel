"""Consolidated subject_key namespace and Table 2 relationship registry.

Derivative artifact of the substep-4 emitter contract validation.
The authoritative source remains the per-emitter skill files in
``sentinel/skills/skill_files/``; this module is a faithful static
projection of the namespace declarations and Table 2 boundary tables
those skill files declare, in a shape consumable by dedup, semantic
grouping, and the Architect re-entry point.

This module is data-only. It encodes no policy. Downstream consumers
read ``NAMESPACES`` and ``TABLE_2_RELATIONSHIPS`` and apply their own
behavior; the registry tells them *what* the emitter contract says,
not what to *do* about it.

Faithful asymmetric projection (decision Q-A from build proposal):
relationships are recorded only as declared in skill files. Carry-
forward unidirectional edges from substep-4 closure (per
``docs/substep_4d/group_4d_3/batch_findings_2026-05-28.md`` pattern
#1) have been closed in cycle 26 by appending the reverse Table 2
entry for each specialist-tier pair. Edges that are *intentionally*
asymmetric (the quick-review-vs-specialist tier, where the peer
``layer2a-quick-review`` does not maintain Table 2 declarations by
convention) are marked with ``intentionally_asymmetric=True`` and
excluded from ``get_unidirectional_relationships()``.

Mirror of the existing ``finding_kinds.py`` pattern: hand-derived
static immutable data with lookup helpers, no runtime extraction.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
from types import MappingProxyType
from typing import Final, Iterator, Literal, Mapping

# ── Surface categories ────────────────────────────────────────────────
#
# What kind of program structure the namespace anchors on. Used by
# semantic grouping to bucket findings by anchor family before
# proposing remediation patterns.
SurfaceCategory = Literal[
    "code_site",          # named function / call / catch / lock site
    "operational",        # cross-module surface: external call, workflow, failure path
    "schema_surface",     # database entity + field constraint
    "migration_surface",  # versioned migration script step
    "change_surface",     # change-impact / testability surface
    "injection_point",    # implicit collaborator (clock, random, env, …)
    "structural",         # cross-cutting structural-smell axis
]

# ── Relationship taxonomy ─────────────────────────────────────────────
#
# How two emitters relate when both can fire on the same surface.
RelationshipType = Literal[
    "boundary_discipline",          # one defers to the other on root-mechanism match
    "anchor_framing_routing",       # shared kind, anchor discriminator splits findings
    "no_suppression",               # both legitimately emit; downstream dedup consolidates
    "quick_review_vs_specialist",   # depth-of-analysis boundary
]

# What dedup should do when both emitters co-emit on a shared surface.
ConsolidationRule = Literal[
    "collapse_on_shared_subject_key",   # same kind + shared namespace → single finding
    "preserve_both_with_attribution",   # both layers preserved, attributed in `layers`
    "anchor_framing_split",             # different anchors → separate findings
    "specialist_wins_over_quick",       # quick-review yields to specialist
]


@dataclass(frozen=True, slots=True)
class NamespaceDefinition:
    """One subject_key namespace declared by one or more emitters."""

    token: str                          # "external_call:"
    pattern: str                        # "<dependency-id>:<call-site>"
    surface: SurfaceCategory
    owners: tuple[str, ...]             # canonical emitter names; >1 = shared
    note: str = ""                      # disambiguation / token-collision note
    # Structured disambiguation for collision-prone tokens shared
    # across emitters with different semantics. Each entry is
    # (finding_kind, semantic_label). Default () = no collision.
    disambiguation: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class Table2Relationship:
    """One pairwise Table 2 boundary between two emitters."""

    emitter_a: str                      # canonical name (skill-file basename, no .md)
    emitter_b: str
    table_label: str                    # "2-A", "2-B", … (from emitter_a's POV)
    shared_kinds: tuple[str, ...]       # finding_kinds in scope
    shared_namespaces: tuple[str, ...]  # subject_key namespaces in scope
    relationship: RelationshipType
    consolidation: ConsolidationRule
    canonical_emitter: str | None       # whose finding shape wins; None = preserve both
    note: str = ""                      # short prose from skill-file amendment
    intentionally_asymmetric: bool = False  # True = peer does not maintain Table 2 (quick-review tier)


@dataclass(frozen=True, slots=True)
class PairRule:
    """One Table 2 pair rule, indexed from the perspective of one
    emitter.

    Derived from ``Table2Relationship`` and consumed by the pair-aware
    kind index. ``other_emitter`` is the peer in the pair;
    ``canonical_emitter`` carries the registry's ``None`` value as-is
    (``None`` = preserve-both).
    """

    other_emitter: str
    canonical_emitter: str | None
    consolidation: str
    table_label: str


# ── Namespace registry ────────────────────────────────────────────────
#
# Every subject_key namespace declared across the 16 review-tier emitters.
# Layer-exclusive (owners has length 1) vs shared (length >= 2) is
# computed by ``is_shared_namespace`` rather than stored as a flag.
NAMESPACES: Final[tuple[NamespaceDefinition, ...]] = (
    # ── layer2d-exclusive code-site / schema / migration surfaces ────
    NamespaceDefinition(
        token="transaction_boundary:",
        pattern="<file>:<function>",
        surface="code_site",
        owners=("layer2d-data-integrity-specialist",),
    ),
    NamespaceDefinition(
        token="durable_write:",
        pattern="<file>:<symbol>",
        surface="code_site",
        owners=("layer2d-data-integrity-specialist",),
    ),
    NamespaceDefinition(
        token="read_modify_write:",
        pattern="<file>:<function>",
        surface="code_site",
        owners=("layer2d-data-integrity-specialist",),
    ),
    NamespaceDefinition(
        token="schema_constraint:",
        pattern="<entity>:<field>",
        surface="schema_surface",
        owners=("layer2d-data-integrity-specialist",),
        note="Engagement-first schema-surface namespace (layer2d Round A).",
    ),
    # ── layer3d-exclusive error-handling code-site surfaces ──────────
    NamespaceDefinition(
        token="catch_site:",
        pattern="<function>:<exception-class>",
        surface="code_site",
        owners=("layer3d-error-handling-specialist",),
    ),
    NamespaceDefinition(
        token="return_site:",
        pattern="<function>:<sentinel-kind>",
        surface="code_site",
        owners=("layer3d-error-handling-specialist",),
    ),
    # ── layer3e-exclusive concurrency-primitive code-site surfaces ───
    NamespaceDefinition(
        token="shared_state:",
        pattern="<file>:<symbol>",
        surface="code_site",
        owners=("layer3e-concurrency-specialist",),
    ),
    NamespaceDefinition(
        token="lock_site:",
        pattern="<file>:<symbol>",
        surface="code_site",
        owners=("layer3e-concurrency-specialist",),
    ),
    NamespaceDefinition(
        token="await_site:",
        pattern="<file>:<function>",
        surface="code_site",
        owners=("layer3e-concurrency-specialist",),
    ),
    NamespaceDefinition(
        token="task_site:",
        pattern="<file>:<function>",
        surface="code_site",
        owners=("layer3e-concurrency-specialist",),
    ),
    # ── 4-way operational-surface namespaces (dedup-aligned) ─────────
    NamespaceDefinition(
        token="external_call:",
        pattern="<dependency-id>:<call-site>",
        surface="operational",
        owners=(
            "layer2d-data-integrity-specialist",
            "layer3d-error-handling-specialist",
            "layer3e-concurrency-specialist",
            "layer2j-operational-risk-failure-modes",
        ),
        note="Operational surface aligned across four emitters for cross-emitter dedup.",
    ),
    NamespaceDefinition(
        token="workflow:",
        pattern="<workflow-or-activity-name>:<boundary>",
        surface="operational",
        owners=(
            "layer2d-data-integrity-specialist",
            "layer3d-error-handling-specialist",
            "layer3e-concurrency-specialist",
            "layer2j-operational-risk-failure-modes",
        ),
    ),
    NamespaceDefinition(
        token="failure_path:",
        pattern="<component>:<missing-signal>",
        surface="operational",
        owners=(
            "layer2d-data-integrity-specialist",
            "layer3d-error-handling-specialist",
            "layer3e-concurrency-specialist",
            "layer2j-operational-risk-failure-modes",
        ),
    ),
    # ── layer3d ↔ layer2j shared operational namespace ───────────────
    NamespaceDefinition(
        token="dependency_set:",
        pattern="<group-name>",
        surface="operational",
        owners=(
            "layer3d-error-handling-specialist",
            "layer2j-operational-risk-failure-modes",
        ),
    ),
    # ── layer2j-exclusive retry / replay surfaces ────────────────────
    NamespaceDefinition(
        token="retry_surface:",
        pattern="<operation-name>:<side-effect-class>",
        surface="operational",
        owners=("layer2j-operational-risk-failure-modes",),
    ),
    NamespaceDefinition(
        token="replay_surface:",
        pattern="<operation-name>",
        surface="operational",
        owners=("layer2j-operational-risk-failure-modes",),
    ),
    # ── Token-collision: `migration:` used by three emitters with ────
    # different semantic meanings. Layer2d uses it for actual schema-
    # migration steps (migration_hazard); layer2i / layer3c use it for
    # change_verification_gap on a planned change to a versioned
    # element. Same token surface, different finding_kind scope. The
    # G7 normalization work referenced in batch findings #4 will need
    # to either split the token (e.g., `migration_step:` vs
    # `migration_change:`) or scope-qualify it by finding_kind.
    NamespaceDefinition(
        token="migration:",
        pattern="<migration-id>:<step>  (layer2d) | <element>:<from-to-version>  (layer2i/3c)",
        surface="migration_surface",
        owners=(
            "layer2d-data-integrity-specialist",
            "layer2i-change-impact-testability",
            "layer3c-testability-specialist",
        ),
        note=(
            "TOKEN COLLISION: same `migration:` token; different semantics. "
            "Layer2d = migration_hazard at schema-migration step. "
            "Layer2i / layer3c = change_verification_gap at planned migration "
            "of a versioned element. Disambiguated by co-emitted finding_kind."
        ),
        disambiguation=(
            ("migration_hazard", "schema_migration_step"),
            ("change_verification_gap", "versioned_element_migration"),
        ),
    ),
    # ── layer2i ↔ layer3c shared change-surface namespaces ───────────
    NamespaceDefinition(
        token="activity:",
        pattern="<activity-name>",
        surface="change_surface",
        owners=("layer2i-change-impact-testability", "layer3c-testability-specialist"),
    ),
    NamespaceDefinition(
        token="method:",
        pattern="<class-name>.<method-name>",
        surface="change_surface",
        owners=("layer2i-change-impact-testability", "layer3c-testability-specialist"),
    ),
    NamespaceDefinition(
        token="module:",
        pattern="<file-path-or-namespace>",
        surface="change_surface",
        owners=("layer2i-change-impact-testability", "layer3c-testability-specialist"),
    ),
    NamespaceDefinition(
        token="integration:",
        pattern="<external-system>",
        surface="change_surface",
        owners=("layer2i-change-impact-testability", "layer3c-testability-specialist"),
    ),
    NamespaceDefinition(
        token="contract:",
        pattern="<published-interface-or-event>",
        surface="change_surface",
        owners=("layer2i-change-impact-testability", "layer3c-testability-specialist"),
    ),
    # ── layer3c-exclusive hidden_dependency injection points ─────────
    NamespaceDefinition(
        token="clock:",
        pattern="<injection-point>",
        surface="injection_point",
        owners=("layer3c-testability-specialist",),
    ),
    NamespaceDefinition(
        token="random:",
        pattern="<injection-point>",
        surface="injection_point",
        owners=("layer3c-testability-specialist",),
    ),
    NamespaceDefinition(
        token="env:",
        pattern="<injection-point>",
        surface="injection_point",
        owners=("layer3c-testability-specialist",),
    ),
    NamespaceDefinition(
        token="network:",
        pattern="<injection-point>",
        surface="injection_point",
        owners=("layer3c-testability-specialist",),
    ),
    NamespaceDefinition(
        token="disk:",
        pattern="<injection-point>",
        surface="injection_point",
        owners=("layer3c-testability-specialist",),
    ),
    NamespaceDefinition(
        token="global:",
        pattern="<injection-point>",
        surface="injection_point",
        owners=("layer3c-testability-specialist",),
    ),
    NamespaceDefinition(
        token="user:",
        pattern="<injection-point>",
        surface="injection_point",
        owners=("layer3c-testability-specialist",),
    ),
    # ── layer3b ↔ layer4a shared structural-smell namespaces ─────────
    NamespaceDefinition(
        token="change:",
        pattern="<logical-change>",
        surface="structural",
        owners=("layer3b-maintainability-specialist", "layer4a-code-smell-detector"),
        note="Used for shotgun_surgery; namespace alignment per layer3b Table 2-A.",
    ),
    NamespaceDefinition(
        token="responsibility:",
        pattern="<axis>",
        surface="structural",
        owners=("layer3b-maintainability-specialist", "layer4a-code-smell-detector"),
        note="Used for divergent_change; namespace alignment per layer3b Table 2-A.",
    ),
    NamespaceDefinition(
        token="boundary:",
        pattern="<crossed-boundary>",
        surface="structural",
        owners=("layer3b-maintainability-specialist", "layer4a-code-smell-detector"),
        note="Used for inappropriate_intimacy; alternative form `class-pair:<a>↔<b>`.",
    ),
)


# ── Table 2 relationship registry ─────────────────────────────────────
#
# One entry per Table 2 declaration in a skill file. Pairwise; multi-way
# emitter relationships compose via repeated entries (layer2d's four
# Table 2-A/B/C/D entries are four independent pairwise records).
TABLE_2_RELATIONSHIPS: Final[tuple[Table2Relationship, ...]] = (
    # ── layer2d's four tables ────────────────────────────────────────
    Table2Relationship(
        emitter_a="layer2d-data-integrity-specialist",
        emitter_b="layer2a-quick-review",
        table_label="2-A",
        shared_kinds=("missing_transaction_boundary",),
        shared_namespaces=("transaction_boundary:", "workflow:"),
        relationship="quick_review_vs_specialist",
        consolidation="specialist_wins_over_quick",
        canonical_emitter="layer2d-data-integrity-specialist",
        note=(
            "First quick-review-vs-specialist Table 2 formalization "
            "(batch findings #9). Depth-of-analysis boundary; layer2d "
            "carries the durable-write perspective."
        ),
        intentionally_asymmetric=True,
    ),
    Table2Relationship(
        emitter_a="layer2d-data-integrity-specialist",
        emitter_b="layer3d-error-handling-specialist",
        table_label="2-B",
        shared_kinds=("missing_transaction_boundary", "silent_data_loss"),
        shared_namespaces=("external_call:", "workflow:", "failure_path:"),
        relationship="boundary_discipline",
        consolidation="collapse_on_shared_subject_key",
        canonical_emitter="layer2d-data-integrity-specialist",
        note=(
            "Layer3d defers to layer2d on durable-data-invariant root "
            "mechanism. Layer2d-specialist shape (`data_impact`, "
            "`failure_scenario`) canonical."
        ),
    ),
    Table2Relationship(
        emitter_a="layer2d-data-integrity-specialist",
        emitter_b="layer3e-concurrency-specialist",
        table_label="2-C",
        shared_kinds=("missing_transaction_boundary", "silent_data_loss"),
        shared_namespaces=("external_call:", "workflow:", "failure_path:"),
        relationship="boundary_discipline",
        consolidation="collapse_on_shared_subject_key",
        canonical_emitter="layer2d-data-integrity-specialist",
        note=(
            "Layer3e defers to layer2d on durable-data-invariant root "
            "mechanism. `lost_update_risk` (layer2d) vs `concurrency_"
            "update_risk` (layer3e) are distinct kinds, not shared — "
            "anchor-framing split (batch findings #3)."
        ),
    ),
    Table2Relationship(
        emitter_a="layer2d-data-integrity-specialist",
        emitter_b="layer2j-operational-risk-failure-modes",
        table_label="2-D",
        shared_kinds=(),  # deferral relationship; no shared kind
        shared_namespaces=("external_call:", "workflow:", "failure_path:"),
        relationship="boundary_discipline",
        consolidation="preserve_both_with_attribution",
        canonical_emitter="layer2j-operational-risk-failure-modes",
        note=(
            "Deferral: layer2d does not emit `idempotency_gap`. Retry / "
            "replay surface is layer2j-only. Bidirectional with layer2j 2-B."
        ),
    ),
    # ── layer3d's table ──────────────────────────────────────────────
    Table2Relationship(
        emitter_a="layer3d-error-handling-specialist",
        emitter_b="layer2j-operational-risk-failure-modes",
        table_label="2-A",
        shared_kinds=(
            "timeout_handling_gap",
            "partial_failure_handling_gap",
            "observability_gap",
        ),
        shared_namespaces=(
            "external_call:",
            "workflow:",
            "failure_path:",
            "dependency_set:",
        ),
        relationship="no_suppression",
        consolidation="preserve_both_with_attribution",
        canonical_emitter=None,
        note=(
            "Both emit from different perspectives (error-handling-pattern "
            "vs operational-risk-surface). Bidirectional with layer2j 2-A."
        ),
    ),
    # ── layer3e's two tables ─────────────────────────────────────────
    Table2Relationship(
        emitter_a="layer3e-concurrency-specialist",
        emitter_b="layer3d-error-handling-specialist",
        table_label="2-A",
        shared_kinds=(
            "concurrency_safety_gap",
            "concurrency_update_risk",
            "blocking_io_in_async",
        ),
        shared_namespaces=("external_call:", "workflow:", "failure_path:"),
        relationship="boundary_discipline",
        consolidation="collapse_on_shared_subject_key",
        canonical_emitter="layer3e-concurrency-specialist",
        note=(
            "Layer3d defers to layer3e on concurrency-primitive root "
            "mechanism. Layer3e-specialist shape (`reproduction_scenario`, "
            "`data_impact`) canonical."
        ),
    ),
    Table2Relationship(
        emitter_a="layer3e-concurrency-specialist",
        emitter_b="layer3a-performance-specialist",
        table_label="2-B",
        shared_kinds=("blocking_io_in_async",),
        shared_namespaces=("await_site:", "external_call:"),
        relationship="no_suppression",
        consolidation="preserve_both_with_attribution",
        canonical_emitter=None,
        note=(
            "Both emit `blocking_io_in_async`; layer3e from event-loop "
            "safety, layer3a from performance scaling. Both perspectives "
            "valid."
        ),
    ),
    # ── layer2j's two tables ─────────────────────────────────────────
    Table2Relationship(
        emitter_a="layer2j-operational-risk-failure-modes",
        emitter_b="layer3d-error-handling-specialist",
        table_label="2-A",
        shared_kinds=(
            "timeout_handling_gap",
            "partial_failure_handling_gap",
            "observability_gap",
        ),
        shared_namespaces=(
            "external_call:",
            "workflow:",
            "failure_path:",
            "dependency_set:",
        ),
        relationship="no_suppression",
        consolidation="preserve_both_with_attribution",
        canonical_emitter=None,
        note="Bidirectional with layer3d 2-A; mirror perspectives.",
    ),
    Table2Relationship(
        emitter_a="layer2j-operational-risk-failure-modes",
        emitter_b="layer2d-data-integrity-specialist",
        table_label="2-B",
        shared_kinds=(),  # deferral; layer2j owns idempotency_gap
        shared_namespaces=("retry_surface:", "replay_surface:"),
        relationship="boundary_discipline",
        consolidation="preserve_both_with_attribution",
        canonical_emitter="layer2j-operational-risk-failure-modes",
        note="Bidirectional with layer2d 2-D; deferral discipline.",
    ),
    # ── layer3c's table ──────────────────────────────────────────────
    Table2Relationship(
        emitter_a="layer3c-testability-specialist",
        emitter_b="layer2i-change-impact-testability",
        table_label="2",
        shared_kinds=("change_verification_gap",),
        shared_namespaces=(
            "activity:",
            "method:",
            "module:",
            "integration:",
            "contract:",
            "migration:",
        ),
        relationship="no_suppression",
        consolidation="collapse_on_shared_subject_key",
        canonical_emitter=None,
        note=(
            "Both emit from different perspectives: layer3c structural-"
            "testability-of-existing-code, layer2i pending-change blast "
            "radius. Bidirectional with layer2i Table 2."
        ),
    ),
    # ── layer2i's table ──────────────────────────────────────────────
    Table2Relationship(
        emitter_a="layer2i-change-impact-testability",
        emitter_b="layer3c-testability-specialist",
        table_label="2",
        shared_kinds=("change_verification_gap",),
        shared_namespaces=(
            "activity:",
            "method:",
            "module:",
            "integration:",
            "contract:",
            "migration:",
        ),
        relationship="no_suppression",
        consolidation="collapse_on_shared_subject_key",
        canonical_emitter=None,
        note="Bidirectional with layer3c Table 2; mirror perspectives.",
    ),
    # ── layer3b's two tables ─────────────────────────────────────────
    Table2Relationship(
        emitter_a="layer3b-maintainability-specialist",
        emitter_b="layer4a-code-smell-detector",
        table_label="2-A",
        shared_kinds=(
            "shotgun_surgery",
            "divergent_change",
            "inappropriate_intimacy",
        ),
        shared_namespaces=("change:", "responsibility:", "boundary:"),
        relationship="no_suppression",
        consolidation="collapse_on_shared_subject_key",
        canonical_emitter=None,
        note=(
            "Namespace alignment for cross-emitter dedup; both emit from "
            "structural perspectives (maintainability cost vs code-smell "
            "shape)."
        ),
    ),
    Table2Relationship(
        emitter_a="layer3b-maintainability-specialist",
        emitter_b="layer2a-quick-review",
        table_label="2-B",
        shared_kinds=("structural_complexity",),
        shared_namespaces=(),
        relationship="quick_review_vs_specialist",
        consolidation="specialist_wins_over_quick",
        canonical_emitter="layer3b-maintainability-specialist",
        note="Depth-of-analysis boundary on structural_complexity.",
        intentionally_asymmetric=True,
    ),
    # ── Cycle 26 reverse edges: close 5 specialist-tier carry-forward ─
    # unidirectional edges by adding the peer-side Table 2 declaration.
    # Substantive fields mirror the forward entry; only table_label
    # (peer-side numbering) and note (reciprocation prose) change.
    Table2Relationship(
        emitter_a="layer3d-error-handling-specialist",
        emitter_b="layer2d-data-integrity-specialist",
        table_label="2-B",
        shared_kinds=("missing_transaction_boundary", "silent_data_loss"),
        shared_namespaces=("external_call:", "workflow:", "failure_path:"),
        relationship="boundary_discipline",
        consolidation="collapse_on_shared_subject_key",
        canonical_emitter="layer2d-data-integrity-specialist",
        note=(
            "Bidirectional with layer2d 2-B; layer3d defers to layer2d on "
            "durable-data-invariant root mechanism."
        ),
    ),
    Table2Relationship(
        emitter_a="layer3e-concurrency-specialist",
        emitter_b="layer2d-data-integrity-specialist",
        table_label="2-C",
        shared_kinds=("missing_transaction_boundary", "silent_data_loss"),
        shared_namespaces=("external_call:", "workflow:", "failure_path:"),
        relationship="boundary_discipline",
        consolidation="collapse_on_shared_subject_key",
        canonical_emitter="layer2d-data-integrity-specialist",
        note=(
            "Bidirectional with layer2d 2-C; layer3e defers to layer2d on "
            "durable-data-invariant root mechanism. Anchor-framing split "
            "on lost_update_risk vs concurrency_update_risk preserved per "
            "forward entry."
        ),
    ),
    Table2Relationship(
        emitter_a="layer3d-error-handling-specialist",
        emitter_b="layer3e-concurrency-specialist",
        table_label="2-C",
        shared_kinds=(
            "concurrency_safety_gap",
            "concurrency_update_risk",
            "blocking_io_in_async",
        ),
        shared_namespaces=("external_call:", "workflow:", "failure_path:"),
        relationship="boundary_discipline",
        consolidation="collapse_on_shared_subject_key",
        canonical_emitter="layer3e-concurrency-specialist",
        note=(
            "Bidirectional with layer3e 2-A; layer3d defers to layer3e on "
            "concurrency-primitive root mechanism."
        ),
    ),
    Table2Relationship(
        emitter_a="layer3a-performance-specialist",
        emitter_b="layer3e-concurrency-specialist",
        table_label="2-A",
        shared_kinds=("blocking_io_in_async",),
        shared_namespaces=("await_site:", "external_call:"),
        relationship="no_suppression",
        consolidation="preserve_both_with_attribution",
        canonical_emitter=None,
        note=(
            "Bidirectional with layer3e 2-B; both emit blocking_io_in_async—"
            "layer3a from performance scaling, layer3e from event-loop "
            "safety. Both perspectives valid."
        ),
    ),
    Table2Relationship(
        emitter_a="layer4a-code-smell-detector",
        emitter_b="layer3b-maintainability-specialist",
        table_label="2-A",
        shared_kinds=(
            "shotgun_surgery",
            "divergent_change",
            "inappropriate_intimacy",
        ),
        shared_namespaces=("change:", "responsibility:", "boundary:"),
        relationship="no_suppression",
        consolidation="collapse_on_shared_subject_key",
        canonical_emitter=None,
        note=(
            "Bidirectional with layer3b 2-A; namespace alignment for "
            "cross-emitter dedup. Layer4a emits structural code-smell "
            "shape; layer3b emits maintainability cost. Both perspectives "
            "valid."
        ),
    ),
)


# ── Internal lookups ─────────────────────────────────────────────────
_NAMESPACE_BY_TOKEN: Final[Mapping[str, NamespaceDefinition]] = MappingProxyType(
    {entry.token: entry for entry in NAMESPACES}
)


# ── Helpers ──────────────────────────────────────────────────────────
def get_namespace_owner(namespace_token: str) -> tuple[str, ...]:
    """Return the emitter(s) that own a namespace token.

    Empty tuple if the token is unknown. Single-element tuple means
    layer-exclusive; multi-element means shared across emitters.
    """
    entry = _NAMESPACE_BY_TOKEN.get(namespace_token)
    return entry.owners if entry is not None else ()


def is_shared_namespace(namespace_token: str) -> bool:
    """True if the namespace is declared by more than one emitter."""
    return len(get_namespace_owner(namespace_token)) >= 2


def get_table_2_peers(emitter: str, finding_kind: str) -> tuple[str, ...]:
    """Return peer emitters declared by `emitter` for `finding_kind` per Table 2.

    Looks only at Table 2 declarations where `emitter` is the declaring
    side (``emitter_a``) and `finding_kind` is in `shared_kinds`.
    Returns ``emitter_b`` for each matching declaration, de-duplicated
    in declaration order.
    """
    peers: list[str] = []
    for rel in TABLE_2_RELATIONSHIPS:
        if rel.emitter_a != emitter:
            continue
        if finding_kind not in rel.shared_kinds:
            continue
        if rel.emitter_b not in peers:
            peers.append(rel.emitter_b)
    return tuple(peers)


def get_co_emission_consolidation_rule(
    emitter_a: str, emitter_b: str, namespace_token: str
) -> ConsolidationRule | None:
    """Return the consolidation rule for co-emission between two emitters.

    Looks for a Table 2 entry pairing the two emitters (in either
    direction) where `namespace_token` is in the relationship's
    `shared_namespaces`. Returns None if no such relationship is
    declared (consumers may treat unknown pairings as
    `preserve_both_with_attribution` by default — that policy is the
    consumer's, not the registry's).
    """
    for rel in TABLE_2_RELATIONSHIPS:
        pair = {rel.emitter_a, rel.emitter_b}
        if pair != {emitter_a, emitter_b}:
            continue
        if namespace_token in rel.shared_namespaces:
            return rel.consolidation
    return None


def get_unidirectional_relationships() -> tuple[Table2Relationship, ...]:
    """Return Table 2 entries with no declared reverse edge.

    A relationship is unidirectional when the peer emitter has no
    Table 2 declaration pairing it back to the first emitter. Per
    batch findings #1 (bilateral Table-2 reciprocation), these are
    open carry-forward items requiring peer-side amendment cycles.

    Entries marked ``intentionally_asymmetric=True`` (the quick-review-
    vs-specialist tier, where ``layer2a-quick-review`` does not
    maintain Table 2 declarations by convention) are excluded — they
    are asymmetric by design, not by carry-forward.

    Bidirectional pairs (e.g., layer2d 2-D ↔ layer2j 2-B) are excluded.
    """
    # Build set of declared (emitter_a, emitter_b) ordered pairs.
    declared_pairs: set[tuple[str, str]] = {
        (rel.emitter_a, rel.emitter_b) for rel in TABLE_2_RELATIONSHIPS
    }
    unidirectional: list[Table2Relationship] = []
    for rel in TABLE_2_RELATIONSHIPS:
        if rel.intentionally_asymmetric:
            continue
        reverse = (rel.emitter_b, rel.emitter_a)
        if reverse not in declared_pairs:
            unidirectional.append(rel)
    return tuple(unidirectional)


def iter_namespaces() -> Iterator[NamespaceDefinition]:
    """Iterate namespace definitions in declaration order."""
    return iter(NAMESPACES)


def iter_table_2_relationships() -> Iterator[Table2Relationship]:
    """Iterate Table 2 relationships in declaration order."""
    return iter(TABLE_2_RELATIONSHIPS)


@cache
def build_pair_aware_kind_index() -> Mapping[str, Mapping[str, tuple[PairRule, ...]]]:
    """Walk Table 2 relationships once and return a pair-aware index:
    ``kind -> emitter -> tuple[PairRule, ...]``.

    Each pair rule preserves the registry's ``canonical_emitter`` value
    verbatim (including ``None`` = preserve-both). No conflict raise
    here — conflict detection is the consumer's responsibility at
    group-resolution time, and only fires when a group's own
    contributing emitters span pairs with disagreeing canonicals.

    Memoised via ``functools.cache``; safe because ``TABLE_2_RELATIONSHIPS``
    is immutable at runtime. The returned mapping is wrapped in
    ``MappingProxyType`` (outer + inner) to discourage mutation by
    callers; the per-kind tuple values are already immutable.
    """
    index: dict[str, dict[str, list[PairRule]]] = {}
    for rel in iter_table_2_relationships():
        for kind in rel.shared_kinds:
            by_emitter = index.setdefault(kind, {})
            for emitter, other in (
                (rel.emitter_a, rel.emitter_b),
                (rel.emitter_b, rel.emitter_a),
            ):
                by_emitter.setdefault(emitter, []).append(
                    PairRule(
                        other_emitter=other,
                        canonical_emitter=rel.canonical_emitter,
                        consolidation=str(rel.consolidation),
                        table_label=rel.table_label,
                    )
                )
    return MappingProxyType({
        kind: MappingProxyType({
            e: tuple(rules) for e, rules in by_emitter.items()
        })
        for kind, by_emitter in index.items()
    })


__all__ = [
    "NAMESPACES",
    "TABLE_2_RELATIONSHIPS",
    "NamespaceDefinition",
    "Table2Relationship",
    "PairRule",
    "SurfaceCategory",
    "RelationshipType",
    "ConsolidationRule",
    "get_namespace_owner",
    "is_shared_namespace",
    "get_table_2_peers",
    "get_co_emission_consolidation_rule",
    "get_unidirectional_relationships",
    "iter_namespaces",
    "iter_table_2_relationships",
    "build_pair_aware_kind_index",
]
