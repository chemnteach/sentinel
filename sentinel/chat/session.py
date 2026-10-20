from __future__ import annotations

import copy
from typing import Any

from sentinel.config import Config, load_config
from sentinel.orchestrator.main import HumanGateRequired, Orchestrator


class ChatSession:
    """Programmatic chat-first interface for single-task remediation workflows."""

    def __init__(self, config: Config | None = None) -> None:
        # Work on a private shallow copy so chat-specific overrides never leak
        # back onto the caller's shared Config object. The caller may reuse the
        # same Config for other workflows that must NOT run in chat/local-only
        # mode. (A fresh load_config() is already private and needs no copy.)
        self._config = copy.copy(config) if config is not None else load_config()
        # Chat sessions should remain runnable without external model credentials.
        self._config.chat_native_mode = True
        self._config.local_only_mode = True
        # Self-approval is never enabled by default for chat task execution.
        # Capture the policy-authorized baseline once, then require explicit
        # opt-in per call via run_single_task(..., self_approve=True).
        self._self_approve_authorized = bool(
            getattr(self._config, "self_approve_mode", False)
        )

    def run_single_task(self, task_id: str, self_approve: bool = False) -> dict[str, Any]:
        """Run one task and return either completion or human gate payload."""
        self._config.self_approve_mode = bool(
            self_approve and self._self_approve_authorized
        )
        with Orchestrator(self._config) as orch:
            try:
                orch.run_phase2(task_id=task_id, limit=1)
                return {"status": "completed", "task_id": task_id}
            except HumanGateRequired as gate:
                return {
                    "status": "human_gate",
                    "task_id": task_id,
                    "gate_type": gate.gate_type,
                    "payload": gate.payload,
                }

    def show_diff(self, task_id: str) -> str:
        """Return the latest diff summary for a task."""
        with Orchestrator(self._config) as orch:
            return orch.get_task_diff(task_id)

    def request_changes(self, task_id: str, comment: str) -> None:
        """Record local review changes request and reopen the task."""
        with Orchestrator(self._config) as orch:
            orch.request_local_changes(task_id, comment)

    def approve_local(self, task_id: str, comment: str = "") -> None:
        """Approve a locally reviewed task and execute finalization flow."""
        with Orchestrator(self._config) as orch:
            orch.approve_local_and_push(task_id, comment=comment)
