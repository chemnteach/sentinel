"""Immutable registries for finding_kind values.

Two registries are maintained and must be kept distinct (FIX-BLOCKING-003):

* **Contract-facing Quick Review registry** — the eight approved Quick Review
  ``finding_kind`` values (``_QUICK_REVIEW_FINDING_KIND_REGISTRY`` /
  ``SUPPORTED_QUICK_REVIEW_FINDING_KINDS`` / ``ACTIVE_SUPPORTED_REPORT_FINDING_KINDS``).
  Quick Review validation and report-contract callers use *only* this set via
  ``get_quick_review_finding_kind_definition`` /
  ``iter_quick_review_finding_kind_definitions`` /
  ``is_supported_report_contract_finding_kind``.

* **Broader internal taxonomy** — ``FINDING_KIND_REGISTRY`` holds the full
  finding_kind vocabulary used by specialist layers, normalization, and
  documentation generation, exposed via ``get_finding_kind_definition`` /
  ``iter_finding_kind_definitions``. This is deliberately a *distinct* name and
  is not the Quick Review contract surface.

Both are defined once at import time and are immutable.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Iterator, Literal, Mapping

SubjectRequirement = Literal["required", "optional", "n/a"]


@dataclass(frozen=True, slots=True)
class FindingKindDefinition:
    """One immutable finding_kind registry entry."""

    kind_name: str
    subject_required: SubjectRequirement
    description_of_mechanism: str


FINDING_KIND_REGISTRY: Final[tuple[FindingKindDefinition, ...]] = (
    FindingKindDefinition(
        "logic_bug",
        "optional",
        "Incorrect control flow, boundary reasoning, assumption, or value handling causes wrong behavior. Use when the wrong behavior cannot be tied to an identifiable contract; otherwise prefer contract_violation.",
    ),
    FindingKindDefinition(
        "contract_violation",
        "required",
        "Code violates a documented or implied precondition, postcondition, return-value promise, or interface contract.",
    ),
    FindingKindDefinition(
        "error_signal_loss",
        "optional",
        "A real failure is swallowed, flattened, or converted into a misleading success-like result.",
    ),
    FindingKindDefinition(
        "injection_risk",
        "required",
        "Untrusted input reaches a sink that interprets, executes, queries, or resolves it unsafely.",
    ),
    FindingKindDefinition(
        "missing_transaction_boundary",
        "required",
        "A multi-step durable operation lacks all-or-nothing transactional protection.",
    ),
    FindingKindDefinition(
        "timeout_handling_gap",
        "required",
        "A dependency or long-running operation is not bounded or handled correctly on timeout.",
    ),
    FindingKindDefinition(
        "structural_complexity",
        "n/a",
        "Broad code complexity makes safe understanding and modification difficult even when no named smell fits cleanly. Prefer a named smell kind when one applies; use this only when no named smell fits cleanly.",
    ),
    FindingKindDefinition(
        "algorithmic_scaling_gap",
        "required",
        "Algorithm or data-structure choice scales worse than realistic load or data size allows.",
    ),
    FindingKindDefinition(
        "api_contract_mismatch",
        "required",
        "A producer-consumer interface changed incompatibly in signature, schema, version, or behavior.",
    ),
    FindingKindDefinition(
        "trust_boundary_gap",
        "required",
        "Data crosses a component, repo, or trust boundary without explicit validation, ownership, or trust reduction. Use when no concrete reachable sink is demonstrated; if a sink path exists, prefer injection_risk.",
    ),
    FindingKindDefinition(
        "configuration_drift",
        "required",
        "Configuration keys, defaults, or environment assumptions diverge across repos or deployment targets. If the divergent setting weakens a security control, prefer security_misconfiguration.",
    ),
    FindingKindDefinition(
        "build_release_integrity_gap",
        "required",
        "The dependency, build, artifact, or release-validation chain is inconsistently or unsafely defined.",
    ),
    FindingKindDefinition(
        "change_verification_gap",
        "required",
        "Blast radius is large enough that the available seams or tests are insufficient for safe verification.",
    ),
    FindingKindDefinition(
        "partial_failure_handling_gap",
        "required",
        "Partial dependency or workflow failure is not recovered, isolated, rolled back, or degraded safely. If the primary impact is durable-data correctness, prefer silent_data_loss.",
    ),
    FindingKindDefinition(
        "observability_gap",
        "required",
        "Failure paths lack the logging, metrics, tracing, or alerts needed for detection and diagnosis.",
    ),
    FindingKindDefinition(
        "authorization_gap",
        "required",
        "A sensitive action or data path lacks the required authentication or authorization check.",
    ),
    FindingKindDefinition(
        "sensitive_data_exposure",
        "required",
        "Secrets or sensitive data are exposed in source, logs, errors, transport, or externally visible state.",
    ),
    FindingKindDefinition(
        "insecure_deserialization",
        "required",
        "Untrusted data is loaded or evaluated with a mechanism that can execute or instantiate unsafe payloads.",
    ),
    FindingKindDefinition(
        "crypto_misuse",
        "required",
        "Cryptographic primitives, keys, nonces, randomness, or certificate validation are used unsafely.",
    ),
    FindingKindDefinition(
        "security_misconfiguration",
        "required",
        "Security-relevant defaults or settings weaken a protection boundary in deployment or runtime behavior.",
    ),
    FindingKindDefinition(
        "concurrency_update_risk",
        "required",
        "A shared state transition or update is non-atomic and can duplicate, lose, or corrupt state under concurrency. Use for simultaneous updates from different actors. For repeated updates from a single logical event, prefer idempotency_gap.",
    ),
    FindingKindDefinition(
        "data_constraint_mismatch",
        "required",
        "Persistence constraints or field semantics do not match the data being written or read.",
    ),
    FindingKindDefinition(
        "silent_data_loss",
        "required",
        "A durable write path can partially succeed or silently discard/corrupt data without surfacing failure.",
    ),
    FindingKindDefinition(
        "idempotency_gap",
        "required",
        "A retry, replay, or duplicate event can repeat durable side effects because no idempotency guard exists. Use for repeated updates from a single logical event (retry, replay, duplicate delivery). For simultaneous-actor races, prefer concurrency_update_risk.",
    ),
    FindingKindDefinition(
        "migration_hazard",
        "required",
        "A schema or data migration step is unsafe, incompatible, or missing a required transitional stage.",
    ),
    FindingKindDefinition(
        "n_plus_one_query",
        "required",
        "A data access path issues per-item queries instead of a bounded batched or joined query plan.",
    ),
    FindingKindDefinition(
        "blocking_io_in_async",
        "required",
        "Blocking I/O or blocking synchronization is used on an async or event-loop-bound path.",
    ),
    FindingKindDefinition(
        "cache_correctness_gap",
        "required",
        "Cache keys, invalidation, stampede behavior, or stale data semantics make the cache unsafe or misleading.",
    ),
    FindingKindDefinition(
        "hidden_dependency",
        "required",
        "Time, randomness, environment, network, disk, or global state is implicit instead of injected or parameterized.",
    ),
    FindingKindDefinition(
        "concurrency_safety_gap",
        "required",
        "A concurrency-specific race, deadlock, missing-await, or thread-safety failure remains after update-specific issues are factored out.",
    ),
    FindingKindDefinition(
        "god_class",
        "n/a",
        "One class or module has accumulated too many responsibilities and coordination roles.",
    ),
    FindingKindDefinition(
        "feature_envy",
        "optional",
        "A method belongs conceptually with another object's data or behavior.",
    ),
    FindingKindDefinition(
        "shotgun_surgery",
        "optional",
        "A single logical change requires touching many separate files, classes, or decision points.",
    ),
    FindingKindDefinition(
        "primitive_obsession",
        "required",
        "A domain concept is represented as loose primitive values instead of a dedicated type or value object.",
    ),
    FindingKindDefinition(
        "long_parameter_list",
        "n/a",
        "A callable requires too many independently-carried parameters for safe use and change.",
    ),
    FindingKindDefinition(
        "divergent_change",
        "optional",
        "A class or module changes for multiple unrelated reasons instead of one cohesive responsibility.",
    ),
    FindingKindDefinition(
        "data_clump",
        "n/a",
        "The same set of values repeatedly travels together without being modeled as one concept.",
    ),
    FindingKindDefinition(
        "inappropriate_intimacy",
        "required",
        "One module or class relies on another module's internals or crosses an intended abstraction boundary.",
    ),
)

_FINDING_KIND_BY_NAME: Final[Mapping[str, FindingKindDefinition]] = MappingProxyType(
    {entry.kind_name: entry for entry in FINDING_KIND_REGISTRY}
)

_QUICK_REVIEW_KIND_ORDER: Final[tuple[str, ...]] = (
    "logic_bug",
    "contract_violation",
    "injection_risk",
    "missing_transaction_boundary",
    "timeout_handling_gap",
    "structural_complexity",
    "algorithmic_scaling_gap",
    "error_signal_loss",
)

_QUICK_REVIEW_FINDING_KIND_REGISTRY: Final[tuple[FindingKindDefinition, ...]] = tuple(
    _FINDING_KIND_BY_NAME[kind_name] for kind_name in _QUICK_REVIEW_KIND_ORDER
)

_QUICK_REVIEW_FINDING_KIND_BY_NAME: Final[Mapping[str, FindingKindDefinition]] = (
    MappingProxyType(
        {
            entry.kind_name: entry
            for entry in _QUICK_REVIEW_FINDING_KIND_REGISTRY
        }
    )
)

SUPPORTED_QUICK_REVIEW_FINDING_KINDS: Final[frozenset[str]] = frozenset(
    entry.kind_name for entry in _QUICK_REVIEW_FINDING_KIND_REGISTRY
)

ACTIVE_SUPPORTED_REPORT_FINDING_KINDS: Final[frozenset[str]] = frozenset(
    entry.kind_name for entry in _QUICK_REVIEW_FINDING_KIND_REGISTRY
)

SUPPORTED_REPORT_FINDING_KINDS: Final[frozenset[str]] = frozenset(
    entry.kind_name for entry in FINDING_KIND_REGISTRY
)


def get_finding_kind_definition(kind_name: str) -> FindingKindDefinition | None:
    """Lookup one finding_kind definition by its registry name."""

    return _FINDING_KIND_BY_NAME.get(kind_name)


def iter_finding_kind_definitions() -> Iterator[FindingKindDefinition]:
    """Iterate over finding_kind definitions in stable declaration order."""

    return iter(FINDING_KIND_REGISTRY)


def get_quick_review_finding_kind_definition(
    kind_name: str,
) -> FindingKindDefinition | None:
    """Lookup one Quick Review contract finding_kind definition by name."""

    return _QUICK_REVIEW_FINDING_KIND_BY_NAME.get(kind_name)


def iter_quick_review_finding_kind_definitions() -> Iterator[FindingKindDefinition]:
    """Iterate Quick Review contract finding kinds in stable contract order."""

    return iter(_QUICK_REVIEW_FINDING_KIND_REGISTRY)


def get_supported_report_finding_kind_definition(
    kind_name: str,
) -> FindingKindDefinition | None:
    """Backward-compatible alias for Quick Review report-contract lookup."""

    return get_quick_review_finding_kind_definition(kind_name)


def iter_supported_report_finding_kind_definitions() -> Iterator[FindingKindDefinition]:
    """Backward-compatible alias for Quick Review report-contract iteration."""

    return iter_quick_review_finding_kind_definitions()


def is_supported_report_contract_finding_kind(kind_name: str) -> bool:
    """Return True when *kind_name* is in the active report contract subset."""

    return kind_name in ACTIVE_SUPPORTED_REPORT_FINDING_KINDS


__all__ = [
    "FINDING_KIND_REGISTRY",
    "FindingKindDefinition",
    "SUPPORTED_QUICK_REVIEW_FINDING_KINDS",
    "SUPPORTED_REPORT_FINDING_KINDS",
    "ACTIVE_SUPPORTED_REPORT_FINDING_KINDS",
    "SubjectRequirement",
    "get_finding_kind_definition",
    "get_quick_review_finding_kind_definition",
    "get_supported_report_finding_kind_definition",
    "is_supported_report_contract_finding_kind",
    "iter_finding_kind_definitions",
    "iter_quick_review_finding_kind_definitions",
    "iter_supported_report_finding_kind_definitions",
]