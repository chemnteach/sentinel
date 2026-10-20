"""Round B: integration validation of dedup wired through reviewer.py.

This file is the Round B counterpart to ``tests/test_dedup.py`` (Round A
unit suite). It exercises ``deduplicate()`` through the real
``ReviewOrchestrator.review_module()`` call path with a synthetic
``FakeArchitect`` stub so the registry-driven consolidation rules
(Table 2-A specialist-wins-over-quick, Table 2-B collapse-on-shared-
subject-key, the ``migration:`` token-collision disambiguation, the
mixed registry-and-legacy batch behavior, the ``review_module_with_
telemetry`` parity smoke, and the natural trigger-keyword path) are
verified end-to-end through the reviewer integration surface.

Scope guard: no changes to reviewer.py, dedup.py, the subject-key
registry, or the workflow models are made by this file. Synthetic
fixtures are the only test plumbing.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

import pytest

from sentinel.assess.reviewer import (
    ReviewOrchestrator,
    _FULL_SPECIALIST_SKILLS,
)
from sentinel.assess.scan_context import ScanContext
from sentinel.config import Config
from sentinel.orchestrator.state_store import StateStore
from sentinel.skills.loader import Skills


# ── Module-level helpers ─────────────────────────────────────────────


def _emitter_finding(
    *,
    subject_key: str,
    finding_kind: str,
    description: str,
    file: str = "app.py",
    function: str = "save_order",
    line_range: str = "42-50",
    severity: str = "P2",
    category: str = "data_integrity",
    suggested_fix: str = "Wrap call in transaction.",
) -> dict[str, Any]:
    """Build a finding dict in the shape emitters produce.

    Includes ``subject_key`` and ``finding_kind`` so that
    ``_extract_findings`` forwards them into the dedup input (the
    forwarding is conditional on key presence in the emitter response).
    """
    return {
        "severity": severity,
        "category": category,
        "file": file,
        "function": function,
        "line_range": line_range,
        "location": f"{file}:{line_range} {function}()",
        "description": description,
        "suggested_fix": suggested_fix,
        "subject_key": subject_key,
        "finding_kind": finding_kind,
    }


def fake_empty_response() -> dict[str, Any]:
    """Specialist response that emits no findings."""
    return {"findings": []}


def fake_layer2a_response(
    *,
    subject_key: str,
    finding_kind: str,
    description: str,
    **overrides: Any,
) -> dict[str, Any]:
    """Quick Review (layer2a) synthetic response."""
    return {
        "findings": [
            _emitter_finding(
                subject_key=subject_key,
                finding_kind=finding_kind,
                description=description,
                **overrides,
            )
        ]
    }


def fake_layer2d_response(
    *,
    subject_key: str,
    finding_kind: str,
    description: str,
    **overrides: Any,
) -> dict[str, Any]:
    """layer2d-data-integrity-specialist synthetic response."""
    return {
        "findings": [
            _emitter_finding(
                subject_key=subject_key,
                finding_kind=finding_kind,
                description=description,
                **overrides,
            )
        ]
    }


def fake_layer3d_response(
    *,
    subject_key: str,
    finding_kind: str,
    description: str,
    **overrides: Any,
) -> dict[str, Any]:
    """layer3d-error-handling-specialist synthetic response."""
    overrides.setdefault("category", "error_handling")
    return {
        "findings": [
            _emitter_finding(
                subject_key=subject_key,
                finding_kind=finding_kind,
                description=description,
                **overrides,
            )
        ]
    }


def fake_layer2i_response(
    *,
    subject_key: str,
    finding_kind: str,
    description: str,
    **overrides: Any,
) -> dict[str, Any]:
    """layer2i-change-impact-testability synthetic response."""
    overrides.setdefault("category", "change_impact")
    return {
        "findings": [
            _emitter_finding(
                subject_key=subject_key,
                finding_kind=finding_kind,
                description=description,
                **overrides,
            )
        ]
    }


def fake_legacy_response(
    *,
    description: str,
    file: str = "legacy.py",
    function: str = "legacy_func",
    line_range: str = "10-20",
    severity: str = "P2",
    category: str = "maintainability",
    suggested_fix: str = "Refactor.",
    finding_kind: str = "structural_complexity",
) -> dict[str, Any]:
    """Legacy-path finding: registered ``finding_kind`` with ``subject_required="n/a"``
    but no ``subject_key``.

    The finding clears the F2-mint gate (``validate_finding_kind_fields``
    is called indirectly by ``materialize_emitter_finding`` for every
    finding flowing through ``_extract_findings``) while still missing
    ``subject_key``, so ``dedup._registry_consolidation_plan`` returns
    ``None`` and the finding takes the legacy ``file|function|category``
    consolidation path.
    """
    return {
        "findings": [
            {
                "severity": severity,
                "category": category,
                "file": file,
                "function": function,
                "line_range": line_range,
                "location": f"{file}:{line_range} {function}()",
                "description": description,
                "suggested_fix": suggested_fix,
                "finding_kind": finding_kind,
            }
        ]
    }


# ── FakeArchitect stub ───────────────────────────────────────────────


class FakeArchitect:
    """Test double for ``ArchitectAgent`` driving the reviewer pipeline.

    ``specialist_review`` raises on any unregistered skill so that test
    configuration errors surface immediately instead of silently passing.
    """

    def __init__(
        self,
        *,
        quick_response: dict[str, Any],
        specialist_responses: dict[str, dict[str, Any]],
        priority_result: dict[str, Any] | None = None,
    ) -> None:
        self._quick = quick_response
        self._specialists = specialist_responses
        self._priority = priority_result or {
            "recommended_skills": [],
            "criticality": "medium",
        }
        self.quick_calls = 0
        self.specialist_calls: list[str] = []

    def prioritize_review(self, module_context: str) -> dict[str, Any]:
        return dict(self._priority)

    def quick_review(self, module_context: str) -> dict[str, Any]:
        self.quick_calls += 1
        return self._quick

    def specialist_review(
        self,
        skill_name: str,
        module_context: str,
    ) -> dict[str, Any]:
        self.specialist_calls.append(skill_name)
        if skill_name not in self._specialists:
            raise AssertionError(
                f"FakeArchitect: no synthetic response registered for "
                f"skill {skill_name!r}; either register one or use "
                f"enforce_full_specialist_coverage=False to skip "
                f"uninvoked specialists."
            )
        return self._specialists[skill_name]


# ── Constants ────────────────────────────────────────────────────────


_SHORT_SOURCE = (
    "def save_order(order):\n"
    "    db.write(order)\n"
    "    external.notify(order)\n"
)
_MODULE_NAME = "orders"
_MODULE = {
    "name": _MODULE_NAME,
    "path": "orders.py",
    "language": "python",
    "files": ["orders.py"],
}
_MANIFEST: dict[str, Any] = {
    "modules": [_MODULE],
    "languages": ["python"],
}


# ── Test class ───────────────────────────────────────────────────────


class TestDedupIntegrationThroughReviewer:
    """Round B integration validation for dedup via reviewer.review_module()."""

    # ── helpers ──────────────────────────────────────────────────────

    def _make_orchestrator(
        self,
        tmp_path: Path,
        *,
        architect: FakeArchitect,
        review_profile: str = "triggered",
        enforce_full_specialist_coverage: bool = True,
    ) -> tuple[ReviewOrchestrator, StateStore, Config]:
        """Construct a real ReviewOrchestrator wired to ``tmp_path``."""
        env = {
            "SENTINEL_REPO_PATH": str(tmp_path),
            "SENTINEL_STATE_DIR": "state",
        }
        config = Config(env=env, load_dotenv_file=False)
        config.review_profile = review_profile
        config.enforce_full_specialist_coverage = enforce_full_specialist_coverage
        state_dir = tmp_path / "state"
        store = StateStore(state_dir=state_dir, repo_path=tmp_path)
        scan_context = ScanContext.from_repo_root(tmp_path)
        orchestrator = ReviewOrchestrator(
            architect=architect,
            store=store,
            config=config,
            scan_context=scan_context,
        )
        return orchestrator, store, config

    def _default_specialist_responses(self) -> dict[str, dict[str, Any]]:
        """Empty response for every skill the coverage flag will invoke.

        Tests override specific entries for the specialists they want to
        emit findings; the rest stay empty so the orchestrator still
        completes its full sweep.
        """
        return {skill: fake_empty_response() for skill in _FULL_SPECIALIST_SKILLS}

    def _expected_root_cause_id(self, key: str) -> str:
        return hashlib.sha256(key.encode()).hexdigest()[:12]

    # ── Case A: collapse_on_shared_subject_key (Table 2-B) ───────────

    def test_case_a_collapse_on_shared_subject_key_external_call(
        self, tmp_path: Path
    ) -> None:
        """layer2d + layer3d on same external_call: subject_key collapse to 1."""
        subject_key = "external_call:payment-api"
        finding_kind = "missing_transaction_boundary"

        specialists = self._default_specialist_responses()
        specialists[Skills.DATA_INTEGRITY] = fake_layer2d_response(
            subject_key=subject_key,
            finding_kind=finding_kind,
            description=(
                "Outbound payment-api call lacks transactional boundary; "
                "durable write may diverge from downstream side-effect."
            ),
            severity="P1",
        )
        specialists[Skills.ERROR_HANDLING] = fake_layer3d_response(
            subject_key=subject_key,
            finding_kind=finding_kind,
            description="payment-api call missing error handling at boundary.",
            severity="P2",
        )

        architect = FakeArchitect(
            quick_response=fake_empty_response(),
            specialist_responses=specialists,
        )
        orchestrator, store, _ = self._make_orchestrator(
            tmp_path, architect=architect
        )

        returned = orchestrator.review_module(_MODULE, _SHORT_SOURCE, _MANIFEST)

        # Cross-consistency: return value == persisted state
        persisted = store.get_all_findings()
        assert len(returned) == len(persisted) == 1, (
            f"Expected collapse to 1 finding; got returned={len(returned)} "
            f"persisted={len(persisted)}"
        )
        survivor = returned[0]

        # Multi-layer attribution preserved
        assert set(survivor["layers"]) == {
            Skills.DATA_INTEGRITY,
            Skills.ERROR_HANDLING,
        }
        assert survivor["consolidated_from"] == 2

        # Severity escalation: P1 wins over P2
        assert survivor["severity"] == "P1"

        # Registry-derived root_cause_id (Table 2-B has no rule-sensitive
        # suffix on collapse_on_shared_subject_key for shared namespaces)
        expected_key = f"registry|external_call:|{subject_key}"
        assert survivor["root_cause_id"] == self._expected_root_cause_id(
            expected_key
        )

    # ── Case B: specialist_wins_over_quick (Table 2-A) ───────────────

    def test_case_b_specialist_wins_over_quick_transaction_boundary(
        self, tmp_path: Path
    ) -> None:
        """layer2a + layer2d on transaction_boundary: collapse to layer2d-primary."""
        subject_key = "transaction_boundary:order-create"
        finding_kind = "missing_transaction_boundary"

        quick = fake_layer2a_response(
            subject_key=subject_key,
            finding_kind=finding_kind,
            description="Short quick-review note about transaction.",
            severity="P2",
        )
        specialists = self._default_specialist_responses()
        specialists[Skills.DATA_INTEGRITY] = fake_layer2d_response(
            subject_key=subject_key,
            finding_kind=finding_kind,
            description=(
                "Detailed specialist analysis of order-create transaction "
                "boundary: durable write divergence risk under partial "
                "failure of downstream notify side-effect; full data_impact "
                "and failure_scenario captured."
            ),
            severity="P1",
        )

        architect = FakeArchitect(
            quick_response=quick,
            specialist_responses=specialists,
        )
        orchestrator, store, _ = self._make_orchestrator(
            tmp_path, architect=architect
        )

        returned = orchestrator.review_module(_MODULE, _SHORT_SOURCE, _MANIFEST)
        persisted = store.get_all_findings()
        assert len(returned) == len(persisted) == 1

        survivor = returned[0]
        # Canonical emitter survives as the primary description carrier
        assert survivor["skill"] == Skills.DATA_INTEGRITY
        assert "Detailed specialist analysis" in survivor["description"]
        assert set(survivor["layers"]) == {
            Skills.QUICK_REVIEW,
            Skills.DATA_INTEGRITY,
        }
        assert survivor["consolidated_from"] == 2
        assert survivor["severity"] == "P1"

        # transaction_boundary: is layer2d-exclusive (not shared between
        # multiple owners) so the key carries the finding_kind suffix.
        expected_key = (
            f"registry|transaction_boundary:|{subject_key}|{finding_kind}"
        )
        assert survivor["root_cause_id"] == self._expected_root_cause_id(
            expected_key
        )

    # ── Case C: migration: token collision disambiguation ────────────

    def test_case_c_migration_token_collision_disambiguated(
        self, tmp_path: Path
    ) -> None:
        """Same migration: subject_key, different finding_kind → 2 separate.

        Case C exercises dedup against a registry-projected emitter
        (layer2i) rather than a substep-4-validated one; the projection
        will become empirically grounded after layer2i's future
        validation cycle.
        """
        subject_key = "migration:orders-v3:add-status-column"

        specialists = self._default_specialist_responses()
        # layer2d carries migration_hazard semantics
        specialists[Skills.DATA_INTEGRITY] = fake_layer2d_response(
            subject_key=subject_key,
            finding_kind="migration_hazard",
            description="Schema migration step lacks rollback safety.",
            severity="P1",
        )
        # layer2i carries change_verification_gap semantics on the same token
        specialists[Skills.CHANGE_IMPACT] = fake_layer2i_response(
            subject_key=subject_key,
            finding_kind="change_verification_gap",
            description="Planned migration change lacks verification harness.",
            severity="P2",
        )

        architect = FakeArchitect(
            quick_response=fake_empty_response(),
            specialist_responses=specialists,
            # Seed layer2i into recommended_skills so _determine_specialists
            # includes it before the coverage union (layer2i is not part
            # of Skills.SPECIALISTS, so the coverage flag alone wouldn't
            # invoke it).
            priority_result={
                "recommended_skills": [Skills.CHANGE_IMPACT],
                "criticality": "high",
            },
        )
        orchestrator, store, _ = self._make_orchestrator(
            tmp_path, architect=architect
        )

        returned = orchestrator.review_module(_MODULE, _SHORT_SOURCE, _MANIFEST)
        persisted = store.get_all_findings()
        assert len(returned) == len(persisted) == 2, (
            f"Migration token collision must preserve both findings; got "
            f"returned={len(returned)} persisted={len(persisted)}"
        )

        # Distinct root_cause_ids — disambiguated by finding_kind
        ids = {f["root_cause_id"] for f in returned}
        assert len(ids) == 2

        # Value-level check on both keys
        expected_2d = self._expected_root_cause_id(
            f"registry|migration:|{subject_key}|migration_hazard"
        )
        expected_2i = self._expected_root_cause_id(
            f"registry|migration:|{subject_key}|change_verification_gap"
        )
        assert ids == {expected_2d, expected_2i}

        # Each finding remains attributed to exactly its own emitter
        kinds_by_id = {f["root_cause_id"]: f.get("finding_kind") for f in returned}
        assert kinds_by_id[expected_2d] == "migration_hazard"
        assert kinds_by_id[expected_2i] == "change_verification_gap"

    # ── Case D: mixed registry-and-legacy batch ──────────────────────

    def test_case_d_mixed_registry_and_legacy_batch(
        self, tmp_path: Path
    ) -> None:
        """Registry-backed and legacy findings coexist without cross-contamination."""
        subject_key = "external_call:billing-api"
        finding_kind = "missing_transaction_boundary"

        specialists = self._default_specialist_responses()
        # Registry-backed pair (Table 2-B collapse)
        specialists[Skills.DATA_INTEGRITY] = fake_layer2d_response(
            subject_key=subject_key,
            finding_kind=finding_kind,
            description="billing-api lacks transactional boundary.",
            severity="P1",
        )
        specialists[Skills.ERROR_HANDLING] = fake_layer3d_response(
            subject_key=subject_key,
            finding_kind=finding_kind,
            description="billing-api missing error handling.",
            severity="P2",
        )
        # Legacy-path findings from MAINTAINABILITY + TESTABILITY sharing
        # file|function|category so dedup consolidates them via the legacy
        # path and stamps root_cause_id. (Legacy singletons are intentionally
        # left unstamped by dedup — only registry-backed singletons and
        # multi-member groups receive root_cause_id, because the legacy key
        # carries a uuid-randomized fallback for findings with no file or
        # function.)
        specialists[Skills.MAINTAINABILITY] = fake_legacy_response(
            description="Function exceeds complexity threshold.",
            file="legacy_module.py",
            function="render_invoice",
            line_range="100-180",
            category="complexity",
        )
        specialists[Skills.TESTABILITY] = fake_legacy_response(
            description="Function complexity hinders unit testing.",
            file="legacy_module.py",
            function="render_invoice",
            line_range="100-180",
            category="complexity",
        )

        architect = FakeArchitect(
            quick_response=fake_empty_response(),
            specialist_responses=specialists,
        )
        orchestrator, store, _ = self._make_orchestrator(
            tmp_path, architect=architect
        )

        returned = orchestrator.review_module(_MODULE, _SHORT_SOURCE, _MANIFEST)
        persisted = store.get_all_findings()
        assert len(returned) == len(persisted) == 2, (
            f"Expected 1 collapsed registry finding + 1 legacy finding; "
            f"got returned={len(returned)} persisted={len(persisted)}"
        )

        # Find the registry-backed finding by subject_key presence
        registry_finding = next(
            f for f in returned if f.get("subject_key") == subject_key
        )
        legacy_finding = next(
            f for f in returned if not f.get("subject_key")
        )

        # Registry finding consolidated 2 layers
        assert registry_finding["consolidated_from"] == 2
        assert set(registry_finding["layers"]) == {
            Skills.DATA_INTEGRITY,
            Skills.ERROR_HANDLING,
        }
        expected_registry_key = f"registry|external_call:|{subject_key}"
        assert registry_finding["root_cause_id"] == self._expected_root_cause_id(
            expected_registry_key
        )

        # Legacy finding consolidated 2 layers via file|function|category key
        assert legacy_finding["consolidated_from"] == 2
        assert set(legacy_finding["layers"]) == {
            Skills.MAINTAINABILITY,
            Skills.TESTABILITY,
        }
        expected_legacy_key = "orders.py|render_invoice|complexity"
        assert legacy_finding["root_cause_id"] == self._expected_root_cause_id(
            expected_legacy_key
        )
        # No cross-contamination: distinct ids
        assert (
            registry_finding["root_cause_id"]
            != legacy_finding["root_cause_id"]
        )
        # Uniqueness of F2 ids (tmp_path-independent assertion shape)
        f2_ids = {f["id"] for f in returned}
        assert len(f2_ids) == 2

    # ── Case E: review_module_with_telemetry parity smoke ────────────

    def test_case_e_review_module_with_telemetry_parity(
        self, tmp_path: Path
    ) -> None:
        """Telemetry variant returns the same deduped findings as the plain call."""
        subject_key = "external_call:inventory-api"
        finding_kind = "missing_transaction_boundary"

        specialists = self._default_specialist_responses()
        specialists[Skills.DATA_INTEGRITY] = fake_layer2d_response(
            subject_key=subject_key,
            finding_kind=finding_kind,
            description="inventory-api transactional boundary missing.",
            severity="P1",
        )
        specialists[Skills.ERROR_HANDLING] = fake_layer3d_response(
            subject_key=subject_key,
            finding_kind=finding_kind,
            description="inventory-api error handling missing.",
            severity="P2",
        )

        architect = FakeArchitect(
            quick_response=fake_empty_response(),
            specialist_responses=specialists,
        )
        orchestrator, store, _ = self._make_orchestrator(
            tmp_path, architect=architect
        )

        findings, status, failed_chunks = (
            orchestrator.review_module_with_telemetry(
                _MODULE, _SHORT_SOURCE, _MANIFEST
            )
        )

        # Status count is computed post-dedup
        assert len(findings) == 1
        assert status["findings_count"] == 1
        # No chunks failed in a synthetic run
        assert failed_chunks == []

        # Cross-consistency vs persisted state
        persisted = store.get_all_findings()
        assert len(persisted) == 1

        # Registry-derived id
        expected_key = f"registry|external_call:|{subject_key}"
        assert findings[0]["root_cause_id"] == self._expected_root_cause_id(
            expected_key
        )
        assert set(findings[0]["layers"]) == {
            Skills.DATA_INTEGRITY,
            Skills.ERROR_HANDLING,
        }

    # ── Case F: natural trigger-keyword path ─────────────────────────

    def test_case_f_natural_trigger_keyword_path(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Quick Review keyword 'transaction' routes through _SPECIALIST_TRIGGERS.

        Uses review_profile='triggered' and
        enforce_full_specialist_coverage=False so the trigger heuristic is
        the only route that can put DATA_INTEGRITY into the specialist set.
        """
        subject_key = "transaction_boundary:checkout-flow"
        finding_kind = "missing_transaction_boundary"

        quick = fake_layer2a_response(
            subject_key=subject_key,
            finding_kind=finding_kind,
            # 'transaction' keyword triggers DATA_INTEGRITY specialist
            description="Checkout flow lacks transaction boundary.",
            severity="P2",
        )
        # In triggered mode without coverage flag, only the triggered
        # specialists actually run; register DATA_INTEGRITY only.
        specialists: dict[str, dict[str, Any]] = {
            Skills.DATA_INTEGRITY: fake_layer2d_response(
                subject_key=subject_key,
                finding_kind=finding_kind,
                description=(
                    "Specialist confirms checkout-flow transaction "
                    "boundary missing with durable write divergence risk."
                ),
                severity="P1",
            ),
        }

        architect = FakeArchitect(
            quick_response=quick,
            specialist_responses=specialists,
        )
        orchestrator, store, _ = self._make_orchestrator(
            tmp_path,
            architect=architect,
            review_profile="triggered",
            enforce_full_specialist_coverage=False,
        )

        caplog.set_level(logging.INFO, logger="sentinel.assess.reviewer")
        returned = orchestrator.review_module(_MODULE, _SHORT_SOURCE, _MANIFEST)

        # Trigger-path proof: the "running N specialist skill(s)" log
        # message must show DATA_INTEGRITY in the invoked set.
        trigger_log_records = [
            r for r in caplog.records
            if "running" in r.getMessage() and "specialist skill" in r.getMessage()
        ]
        assert trigger_log_records, (
            "Expected 'running N specialist skill(s)' log line to prove "
            "trigger path fired."
        )
        assert any(
            Skills.DATA_INTEGRITY in r.getMessage()
            for r in trigger_log_records
        ), (
            f"DATA_INTEGRITY missing from specialist log lines: "
            f"{[r.getMessage() for r in trigger_log_records]}"
        )

        # And dedup still collapses quick+specialist into 1 via Table 2-A
        persisted = store.get_all_findings()
        assert len(returned) == len(persisted) == 1
        survivor = returned[0]
        assert set(survivor["layers"]) == {
            Skills.QUICK_REVIEW,
            Skills.DATA_INTEGRITY,
        }
        assert survivor["skill"] == Skills.DATA_INTEGRITY
        assert survivor["severity"] == "P1"
