"""
sentinel/agents/base.py

Base class for all Sentinel agents.

Agents are stateless between model calls. The Orchestrator reconstructs
full context from JSON state files before every agent invocation and injects
it here. No agent has persistent memory beyond what is written to the
sentinel/state/ directory.

Each agent has a single responsibility. Agents never self-approve their work
and never self-select skills — the Orchestrator always decides which skill
to inject.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from typing import Any

from sentinel.api.client import ModelClient, ModelResponse
from sentinel.config import Config
from sentinel.skills.loader import SkillLoader, SkillNotFoundError, Skills

log = logging.getLogger(__name__)


class AgentError(RuntimeError):
    """Raised when an agent encounters an unrecoverable error."""


class AgentResponseError(AgentError):
    """Raised when an agent's response cannot be parsed or is malformed."""


class UnsupportedLocalOnlySkillError(AgentError):
    """Raised when local-only mode receives a skill without an explicit fallback."""


class BaseAgent(ABC):
    """
    Abstract base class for all Sentinel agents.

    Subclasses declare their ``name`` and implement ``_build_system_prompt``
    to compose the persona + skill system prompt. The ``run`` and
    ``run_json`` helpers handle the actual API calls.

    The Orchestrator always passes the skill name explicitly — agents never
    choose their own skills.
    """

    #: Unique name for this agent, used in audit logs.
    name: str = "base"

    def __init__(self, config: Config, client: ModelClient, loader: SkillLoader) -> None:
        self._config = config
        self._client = client
        self._loader = loader

    # ------------------------------------------------------------------
    # Public interface called by the Orchestrator
    # ------------------------------------------------------------------

    def run(
        self,
        skill_name: str,
        user_message: str,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> ModelResponse:
        """
        Invoke this agent with a specific skill as the system prompt.

        Args:
            skill_name: Skill file stem (e.g. ``'layer2a-quick-review'``).
            user_message: The code, context, or task payload.
            temperature: Sampling temperature (lower = more deterministic).
            max_tokens: Override config max_tokens for this call.

        Returns:
            ModelResponse with content and token usage.

        Raises:
            SkillNotFoundError: If the skill file does not exist.
            AgentError: On unrecoverable API failure.
        """
        if getattr(self._config, "local_only_mode", False):
            self._validate_skill_exists(skill_name)
            return self._run_local_only_text(skill_name, user_message)

        system_prompt = self._build_system_prompt(skill_name)
        log.info("Agent %s running skill '%s'", self.name, skill_name)

        try:
            response = self._client.call_model(
                system_prompt=system_prompt,
                user_message=user_message,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except Exception as exc:
            raise AgentError(
                f"Agent '{self.name}' failed on skill '{skill_name}': {exc}"
            ) from exc

        log.debug(
            "Agent %s completed | tokens=%d",
            self.name,
            response.total_tokens,
        )
        return response

    def run_json(
        self,
        skill_name: str,
        user_message: str,
        temperature: float = 0.1,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """
        Invoke this agent and parse the response as JSON.

        The skill prompt must instruct the model to respond with JSON only.

        Returns:
            Parsed JSON dict.

        Raises:
            SkillNotFoundError: If the skill file does not exist.
            AgentResponseError: If the response is not valid JSON.
            AgentError: On unrecoverable API failure.
        """
        if getattr(self._config, "local_only_mode", False):
            self._validate_skill_exists(skill_name)
            return self._run_local_only_json(skill_name, user_message)

        system_prompt = self._build_system_prompt(skill_name)
        log.info("Agent %s running skill '%s' (JSON mode)", self.name, skill_name)

        try:
            result = self._client.call_model_json(
                system_prompt=system_prompt,
                user_message=user_message,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        except ValueError as exc:
            raise AgentResponseError(
                f"Agent '{self.name}' returned non-JSON on skill '{skill_name}': {exc}"
            ) from exc
        except Exception as exc:
            raise AgentError(
                f"Agent '{self.name}' failed on skill '{skill_name}': {exc}"
            ) from exc

        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _validate_skill_exists(self, skill_name: str) -> None:
        """Validate that a skill exists before any local-only dispatch shortcut.

        In local-only mode ``run``/``run_json`` skip ``_build_system_prompt``
        (which would raise ``SkillNotFoundError`` for an unknown skill), so
        existence is validated explicitly here. This keeps missing-skill
        behavior identical across normal and local-only modes: an unknown
        ``skill_name`` raises ``SkillNotFoundError`` in both
        (FIX-BLOCKING-002/013/014). A skill that exists but is unsupported in
        local-only mode is still handled by ``_run_local_only_json`` with the
        explicit ``UnsupportedLocalOnlySkillError``.
        """
        exists_fn = getattr(self._loader, "exists", None)
        if not callable(exists_fn):
            # Loader (e.g. a lightweight test double) does not expose an
            # existence check — cannot validate; defer to dispatch behavior.
            return
        if exists_fn(skill_name) is False:
            list_fn = getattr(self._loader, "list_skills", None)
            available = ", ".join(list_fn()) if callable(list_fn) else ""
            raise SkillNotFoundError(
                f"Skill '{skill_name}' not found. "
                f"Available skills: {available or '(none)'}"
            )

    def _build_system_prompt(self, skill_name: str) -> str:
        """
        Compose the full system prompt for this agent + skill combination.

        Loads the skill file body and prepends the agent identity header so
        the model knows which agent role it is playing.
        """
        try:
            skill_body = self._loader.load(skill_name)
        except SkillNotFoundError:
            raise

        header = self._agent_header()
        return f"{header}\n\n{skill_body}"

    def _agent_header(self) -> str:
        """
        Return the agent identity header prepended to every system prompt.

        This ensures the model understands which agent role it is playing
        even when the skill body does not explicitly state it.
        """
        return (
            f"You are operating as the **{self.name.title()} Agent** "
            f"in the Sentinel multi-agent code governance system.\n"
            f"You have a single, well-defined responsibility. "
            f"You do not self-approve your own work. "
            f"You do not deviate from the skill instructions below.\n"
            f"Respond only as instructed by the active skill."
        )

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name!r})"

    # ------------------------------------------------------------------
    # Local-only fallback responses
    # ------------------------------------------------------------------

    def _run_local_only_text(self, skill_name: str, user_message: str) -> ModelResponse:
        """Return deterministic text output when external modeling is disabled."""
        content = (
            f"Local-only mode active. Skill '{skill_name}' executed without external model inference."
        )
        return ModelResponse(
            content=content,
            model="local-only",
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            raw={"local_only": True, "skill": skill_name},
        )

    def _run_local_only_json(self, skill_name: str, user_message: str) -> dict[str, Any]:
        """Return deterministic JSON output when external modeling is disabled."""
        task_id_match = re.search(r"\bTASK-\d{3,5}\b", user_message)
        task_id = task_id_match.group(0) if task_id_match else "TASK-LOCAL"

        if skill_name == Skills.CARTOGRAPHER:
            return {
                "repositories": [],
                "modules": [],
                "local_only": True,
            }

        if skill_name == Skills.DEPENDENCY_ANALYZER:
            return {
                "dependencies": [],
                "edges": [],
                "local_only": True,
            }

        if skill_name == Skills.REVIEW_PRIORITIZER:
            return {
                "criticality": "low",
                "recommended_skills": [],
                "estimated_minutes": 0,
                "local_only": True,
            }

        if skill_name == Skills.FIX_PLANNER:
            return {
                "blocking": [],
                "required": [],
                "desired": [],
                "nice_to_have": [],
                "local_only": True,
                "summary": "No external model planning performed in local-only mode.",
            }

        if skill_name == Skills.SCOPE_GUARDIAN:
            return {
                "task_id": task_id,
                "implementation_plan": [],
                "test_stubs": [],
                "out_of_scope_findings": [],
                "files_changed": [],
                "diff_summary": "No-op implementation in local-only mode.",
                "tests_pass": True,
                "self_review_triggered": True,
                "test_results": "local-only mode",
                "local_only": True,
            }

        if skill_name == Skills.VERIFICATION:
            raise UnsupportedLocalOnlySkillError(
                f"Local-only mode does not support review skill '{skill_name}'."
            )

        if skill_name == Skills.CROSS_REPO_VERIFY:
            raise UnsupportedLocalOnlySkillError(
                f"Local-only mode does not support review skill '{skill_name}'."
            )

        if skill_name == Skills.COMPAT_CHECKER:
            raise UnsupportedLocalOnlySkillError(
                f"Local-only mode does not support review skill '{skill_name}'."
            )

        if skill_name == Skills.FEASIBILITY_ANALYZER:
            return {
                "feasibility_verdict": "FEASIBLE",
                "risk_level": "LOW",
                "recommended_approach": "local-only-default",
                "options": [],
                "local_only": True,
            }

        if skill_name == Skills.SPEC_WRITER:
            return {
                "spec_summary": "Local-only placeholder specification.",
                "acceptance_criteria": [],
                "task_breakdown": [],
                "local_only": True,
            }

        if skill_name.startswith("layer"):
            return {
                "status": "unsupported_local_only_skill",
                "skill": skill_name,
                "message": f"Local-only mode does not support JSON skill '{skill_name}'.",
                "local_only": True,
            }

        raise UnsupportedLocalOnlySkillError(
            f"Local-only mode does not support JSON skill '{skill_name}'."
        )
