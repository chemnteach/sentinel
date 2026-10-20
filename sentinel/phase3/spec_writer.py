"""
sentinel/phase3/spec_writer.py

Enhancement specification orchestrator for Sentinel Phase 3.

Drives the Layer 7b (Enhancement Specification Writer) pipeline for a
given FER. Converts the approved FER and feasibility report into a
    detailed implementation specification, then generates atomic implementation
    tasks from the spec's acceptance criteria.

Like the feasibility pipeline, spec generation is resumable: if a model
call fails after the spec is written to state, the existing spec can be
used without re-invoking the model.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sentinel.agents.architect import ArchitectAgent
from sentinel.orchestrator.state_store import StateStore
from sentinel.orchestrator.task_manager import Task, TaskManager
from sentinel.phase3.fer_manager import FER, FERManager

log = logging.getLogger(__name__)

# Required top-level keys in a valid Layer 7b response
_REQUIRED_KEYS = frozenset({
    "fer_id",
    "acceptance_criteria",
    "scope_boundary",
    "test_stubs",
    "implementation_order",
})


class SpecError(RuntimeError):
    """Raised when the spec generation pipeline encounters an unrecoverable state."""


class HumanSpecGateRequired(Exception):
    """
    Raised when the enhancement spec is complete and requires human
    approval before tasks are generated.

    The CLI displays payload to the operator and waits for
    ``sentinel spec --approve`` or ``sentinel spec --revise``.
    """

    def __init__(self, fer_id: str, payload: dict[str, Any]) -> None:
        self.fer_id = fer_id
        self.payload = payload
        super().__init__(f"Human spec approval gate required for {fer_id}")


class SpecWriter:
    """
    Orchestrates Layer 7b spec generation for a single FER.

    Workflow:
      1. Advance FER to IN_SPEC
      2. Load the feasibility report from state
      3. Build context from FER + feasibility + selected approach
      4. Invoke Architect with Layer 7b skill
      5. Validate and persist the spec
      6. Raise HumanSpecGateRequired for operator review
    """

    def __init__(
        self,
        fer_manager: FERManager,
        architect: ArchitectAgent,
        task_manager: TaskManager,
        store: StateStore,
    ) -> None:
        self._fer_mgr = fer_manager
        self._architect = architect
        self._task_mgr = task_manager
        self._store = store

    def run(self, fer_id: str) -> None:
        """
        Run the full spec generation pipeline for a FER.

        Accepts FERs entering spec generation from FEASIBILITY_APPROVED
        (fresh run) or IN_SPEC (explicit resume), runs the Layer 7b spec
        writer, persists the spec, then raises HumanSpecGateRequired.

        Args:
            fer_id: The FER to spec (must be in FEASIBILITY_APPROVED or IN_SPEC status).

        Raises:
            HumanSpecGateRequired: Always, on successful completion.
            SpecError: On unrecoverable pipeline failure or disallowed entry state.
        """
        fer = self._fer_mgr.get(fer_id)
        log.info("SpecWriter: starting spec generation for %s", fer_id)

        if fer.status == "FEASIBILITY_APPROVED":
            fer = self._fer_mgr.start_spec(fer_id)
        elif fer.status != "IN_SPEC":
            raise SpecError(
                f"FER {fer_id} must be in FEASIBILITY_APPROVED or IN_SPEC status "
                f"to enter spec generation; got {fer.status}."
            )

        # Load feasibility report (required)
        feasibility = self._load_feasibility(fer_id)
        if not feasibility:
            raise SpecError(
                f"No feasibility report found for {fer_id}. "
                "The feasibility pipeline must complete before spec generation."
            )

        # Check if spec already exists (resumability)
        existing_spec = self._load_existing_spec(fer_id)
        if existing_spec:
            log.info("SpecWriter: existing spec found for %s — skipping model call", fer_id)
            spec = existing_spec
        else:
            context = self._fer_mgr.build_spec_context(fer, feasibility)
            log.info("SpecWriter: invoking Layer 7b for %s", fer_id)
            spec = self._architect.write_enhancement_spec(context)

            self._validate_spec(fer_id, spec)

            self._store.upsert_in_list(
                "specs",
                {"fer_id": fer_id, "spec_version": "1.0", **spec},
                key_field="fer_id",
            )
            self._store.commit_state(f"sentinel: FER {fer_id} — enhancement spec generated")
            log.info(
                "SpecWriter: %s spec generated — %d acceptance criteria, %d test stubs",
                fer_id,
                self._count_criteria(spec),
                len(spec.get("test_stubs", [])),
            )

        criteria_count = self._count_criteria(spec)
        stubs_count = len(spec.get("test_stubs", []))

        raise HumanSpecGateRequired(
            fer_id=fer_id,
            payload={
                "fer_id": fer_id,
                "feature_summary": spec.get("feature_summary", ""),
                "acceptance_criteria_count": criteria_count,
                "test_stubs_count": stubs_count,
                "implementation_steps": spec.get("implementation_order", []),
                "scope_boundary": spec.get("scope_boundary", {}),
                "non_goals": spec.get("non_goals", []),
                "message": (
                    f"\nEnhancement spec generated for {fer_id}.\n\n"
                    f"Summary: {spec.get('feature_summary', '')}\n\n"
                    f"Acceptance criteria:  {criteria_count}\n"
                    f"Test stubs defined:   {stubs_count}\n\n"
                    f"Implementation steps:\n"
                    + "\n".join(
                        f"  {i+1}. {step}"
                        for i, step in enumerate(spec.get("implementation_order", []))
                    )
                    + "\n\nFull spec: sentinel/state/specs.json\n\n"
                    "To approve and generate tasks:\n"
                    f"  sentinel spec --approve {fer_id}\n\n"
                    "To request revision:\n"
                    f"  sentinel spec --revise {fer_id} --feedback \"...\""
                ),
            },
        )

    def approve(
        self,
        fer_id: str,
        actor: str = "human",
    ) -> tuple[FER, list[Task]]:
        """
        Record human approval of the spec and generate implementation tasks.

        Each item in the spec's implementation_order becomes an atomic
        Phase 2 task. Every generated task carries the original flattened
        acceptance criteria from the approved spec so the contract remains
        stable for tasks 1..N and no step falls back to synthetic criteria.

        Args:
            fer_id: The FER whose spec is being approved.
            actor: Who approved (default: "human").

        Returns:
            Tuple of (updated FER, list of generated Tasks).

        Atomicity / idempotency (FIX-BLOCKING-016/021): there is no cross-store
        transaction spanning task generation (tasks store) and FER approval
        (fers store). Instead this method is made **idempotent**: generated task
        ids are deterministic (``fer-{fer_id}-task-{n}``) and ``create_task``
        upserts by id, so re-running ``approve`` after an interrupted attempt
        reconciles the same task set rather than creating duplicates. If the FER
        is already ``SPEC_APPROVED``, the approval transition is not re-recorded
        (it would be a duplicate) — the existing task set is returned. This
        guarantees exactly one approval transition and one authoritative task
        set across retries.
        """
        spec = self._load_existing_spec(fer_id)
        if not spec:
            raise SpecError(
                f"No spec found for {fer_id}. Run spec generation first."
            )

        fer = self._fer_mgr.get(fer_id)

        # Idempotency guard: a prior approve() already recorded the SPEC_APPROVED
        # transition and generated the tasks. Re-run task generation (upsert on
        # deterministic ids → no duplicates) and return without re-recording the
        # approval, so a retried/duplicated approval is a safe no-op rather than
        # a FERTransitionError or a second task set.
        if getattr(fer, "status", None) == "SPEC_APPROVED":
            tasks = self._generate_tasks(fer, spec)
            log.info(
                "SpecWriter: %s already approved — reconciled %d task(s), "
                "no re-approval",
                fer_id,
                len(tasks),
            )
            return fer, tasks

        # Authorization gate: only a FER that has actually reached the spec
        # human-review stage (IN_SPEC) may be approved. Verify this BEFORE
        # generating any tasks — otherwise _generate_tasks would durably write
        # task records to the store and only then would record_spec_approved
        # raise FERTransitionError, leaving orphan tasks behind for a FER that
        # was never validly approvable.
        if getattr(fer, "status", None) != "IN_SPEC":
            raise SpecError(
                f"FER {fer_id} must be in IN_SPEC status to approve its spec; "
                f"got {getattr(fer, 'status', None)}. Run spec generation first."
            )

        tasks = self._generate_tasks(fer, spec)
        task_ids = [t.task_id for t in tasks]

        fer = self._fer_mgr.record_spec_approved(
            fer_id=fer_id,
            spec_version=spec.get("spec_version", "1.0"),
            task_ids=task_ids,
            actor=actor,
        )

        self._store.commit_state(
            f"sentinel: FER {fer_id} — spec approved, {len(tasks)} task(s) created"
        )
        log.info(
            "SpecWriter: %s approved — %d task(s) created",
            fer_id,
            len(tasks),
        )
        return fer, tasks

    def get_spec(self, fer_id: str) -> dict[str, Any] | None:
        """Return the spec for a FER, or None if not yet generated."""
        return self._load_existing_spec(fer_id)

    # ------------------------------------------------------------------
    # Task generation from spec
    # ------------------------------------------------------------------

    def _generate_tasks(self, fer: FER, spec: dict[str, Any]) -> list[Task]:
        """
        Generate atomic Phase 2 tasks from the spec's implementation_order.

        Each implementation step becomes one task. Every task receives the
        original flattened acceptance criteria from the approved spec; the
        criteria are not replaced with step-specific placeholders for later
        tasks. Each task also carries the full scope boundary so the Scope
        Guardian can enforce it on every task.
        """
        steps = spec.get("implementation_order", [])
        if not steps:
            raise SpecError(
                f"Spec for {fer.fer_id} contains no implementation_order steps. "
                "Cannot generate tasks."
            )

        scope = spec.get("scope_boundary", {})
        scope_boundary = (
            "IN SCOPE: "
            + ", ".join(scope.get("in_scope", []))
            + " | OUT OF SCOPE: "
            + ", ".join(scope.get("out_of_scope", []))
        )

        # Flatten all acceptance criteria into a list of strings
        all_criteria = self._flatten_criteria(spec.get("acceptance_criteria", {}))

        tasks: list[Task] = []
        for i, step in enumerate(steps):
            task_id = f"fer-{fer.fer_id.lower()}-task-{i+1:03d}"

            # Distribute test stubs to each task proportionally
            stubs = spec.get("test_stubs", [])
            step_stubs = _distribute_stubs(stubs, i, len(steps))

            task = self._task_mgr.create_task(
                task_id=task_id,
                description=f"[{fer.fer_id}] Step {i+1}/{len(steps)}: {step}",
                acceptance_criteria=all_criteria,
                affected_files=scope.get("in_scope", []),
                scope_boundary=scope_boundary,
                source_fer_id=fer.fer_id,
                test_stubs=step_stubs,
                is_phase3=True,
            )
            tasks.append(task)
            log.debug("Generated task %s for FER %s step %d", task_id, fer.fer_id, i + 1)

        return tasks

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_feasibility(self, fer_id: str) -> dict[str, Any] | None:
        """Load feasibility report for a FER."""
        reports = self._store.read("feasibility")
        return next((r for r in reports if r.get("fer_id") == fer_id), None)

    def _load_existing_spec(self, fer_id: str) -> dict[str, Any] | None:
        """Load existing spec for a FER."""
        specs = self._store.read("specs")
        return next((s for s in specs if s.get("fer_id") == fer_id), None)

    def _validate_spec(self, fer_id: str, spec: dict[str, Any]) -> None:
        """Validate the Layer 7b response structure."""
        missing = _REQUIRED_KEYS - set(spec.keys())
        if missing:
            raise SpecError(
                f"Layer 7b response for {fer_id} missing required keys: "
                f"{sorted(missing)}. Got: {sorted(spec.keys())}"
            )

        if not spec.get("test_stubs"):
            raise SpecError(
                f"Layer 7b spec for {fer_id} contains no test_stubs. "
                "Test stubs are required for the TDD gate."
            )

        if not spec.get("implementation_order"):
            raise SpecError(
                f"Layer 7b spec for {fer_id} contains no implementation_order. "
                "At least one implementation step is required."
            )

    @staticmethod
    def _count_criteria(spec: dict[str, Any]) -> int:
        """Count total acceptance criteria across all categories."""
        criteria = spec.get("acceptance_criteria", {})
        if isinstance(criteria, dict):
            return sum(len(v) for v in criteria.values() if isinstance(v, list))
        if isinstance(criteria, list):
            return len(criteria)
        return 0

    @staticmethod
    def _flatten_criteria(criteria: dict[str, list] | list) -> list[str]:
        """Flatten categorised criteria into a flat list of strings."""
        if isinstance(criteria, list):
            return [c.get("then", str(c)) if isinstance(c, dict) else str(c) for c in criteria]
        flat = []
        for category_items in criteria.values():
            # A model may emit a category whose value is null or a scalar rather
            # than a list. Skip non-list values instead of crashing task
            # generation with a TypeError (parallels the isinstance(v, list)
            # guard already present in _count_criteria).
            if not isinstance(category_items, list):
                continue
            for item in category_items:
                if isinstance(item, dict):
                    flat.append(
                        f"[{item.get('id', '')}] "
                        f"Given {item.get('given', '')} "
                        f"When {item.get('when', '')} "
                        f"Then {item.get('then', '')}"
                    )
                else:
                    flat.append(str(item))
        return flat


def _distribute_stubs(
    stubs: list[dict[str, Any]],
    step_index: int,
    total_steps: int,
) -> list[dict[str, Any]]:
    """
    Distribute test stubs across steps approximately evenly.

    Step 0 gets the first batch; the last step gets any remainder.
    """
    if not stubs or total_steps == 0:
        return []
    per_step = max(1, len(stubs) // total_steps)
    start = step_index * per_step
    end = start + per_step if step_index < total_steps - 1 else len(stubs)
    return stubs[start:end]
