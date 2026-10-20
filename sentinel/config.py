"""
sentinel/config.py

Central configuration for the Sentinel system.
All settings are loaded from environment variables with sensible defaults.
Call load_config() once at startup; pass the Config object everywhere.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path

from dotenv import load_dotenv

log = logging.getLogger(__name__)

_PUBLIC_PROVIDERS = frozenset({"anthropic", "openai", "github-models", "copilot-cli"})

# Specialist-selection profiles accepted via SENTINEL_REVIEW_PROFILE.
_VALID_REVIEW_PROFILES: frozenset[str] = frozenset({"triggered", "reliability-first"})

# S4-11 — `.sentinel/config.yaml` project defaults. Maps a SENTINEL_* env name
# to the corresponding yaml key. When the env var is unset, ``_get_env`` falls
# back to the yaml value before the built-in default, so precedence is:
#   explicit env var  >  .sentinel/config.yaml  >  built-in default.
_SENTINEL_YAML_FILENAME = ".sentinel/config.yaml"
_ENV_TO_YAML_KEY: dict[str, str] = {
    "SENTINEL_PROVIDER": "provider",
    "SENTINEL_MODEL": "model",
    "SENTINEL_STATE_DIR_BASE": "state_dir_base",
    "SENTINEL_OPERATOR": "operator",
    "SENTINEL_OPERATOR_EMAIL": "operator_email",
    "SENTINEL_GITHUB_URL": "github_url",
    "SENTINEL_AUTO_SYNTHESIZE": "auto_synthesize",
}


def _load_sentinel_yaml_defaults() -> dict[str, str]:
    """Load ``.sentinel/config.yaml`` (from CWD) as a flat string mapping.

    Returns an empty dict when the file is absent, unreadable, malformed, or
    not a top-level mapping. Malformed content is logged and ignored rather
    than raised so a broken project file never blocks startup. Values are
    stringified to match the env-mapping contract consumed by ``_get_env``.
    """
    path = Path.cwd() / _SENTINEL_YAML_FILENAME
    if not path.is_file():
        return {}
    try:
        import yaml  # local import: keep config import light when yaml unused

        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — never let a bad file crash startup
        log.warning(
            "Ignoring malformed %s: %s", _SENTINEL_YAML_FILENAME, exc
        )
        return {}
    if not isinstance(raw, Mapping):
        if raw is not None:
            log.warning(
                "Ignoring %s: top-level content is not a mapping.",
                _SENTINEL_YAML_FILENAME,
            )
        return {}
    defaults: dict[str, str] = {}
    for key, value in raw.items():
        if value is None:
            continue
        defaults[str(key)] = str(value)
    return defaults


class Config:
    """Runtime configuration for Sentinel."""

    def __init__(
        self,
        env: Mapping[str, str] | None = None,
        *,
        load_dotenv_file: bool | None = None,
    ) -> None:
        self._env = env if env is not None else os.environ

        # Preserve existing behavior for runtime usage, but allow deterministic
        # test construction with injected environment mappings.
        if load_dotenv_file is None:
            load_dotenv_file = env is None
        if load_dotenv_file:
            load_dotenv()

        # S4-11 — project defaults from .sentinel/config.yaml. Consulted by
        # _get_env when an env var is unset, but always below an explicit env
        # value (env > yaml > built-in default).
        self._yaml_defaults: dict[str, str] = _load_sentinel_yaml_defaults()

        self.local_only_mode: bool = self._get_env(
            "SENTINEL_LOCAL_ONLY_MODE",
            "false",
        ).strip().lower() in {"1", "true", "yes", "on"}
        self.chat_native_mode: bool = self._get_env(
            "SENTINEL_CHAT_NATIVE_MODE",
            "false",
        ).strip().lower() in {"1", "true", "yes", "on"}

        # -- LLM Provider ----------------------------------------------------
        # Select with SENTINEL_PROVIDER = anthropic | openai | github-models |
        # copilot-cli, or let Sentinel auto-detect from the available API keys.
        # Credentials come from the environment (optionally via a local .env).
        # GitHub Models is disabled by default and requires explicit opt-in via
        # SENTINEL_ALLOW_GITHUB_MODELS=true.
        self.github_token: str = self._get_env("GITHUB_TOKEN", "")
        self.anthropic_api_key: str = self._get_env("ANTHROPIC_API_KEY", "")
        self.openai_api_key: str = self._get_env("OPENAI_API_KEY", "")
        self.allow_github_models: bool = self._get_env(
            "SENTINEL_ALLOW_GITHUB_MODELS",
            "false",
        ).strip().lower() in {"1", "true", "yes", "on"}
        self.models_endpoint: str = self._get_env(
            "GITHUB_MODELS_ENDPOINT",
            "https://models.inference.ai.azure.com",
        )
        self.openai_base_url: str = self._get_env(
            "OPENAI_BASE_URL",
            "https://api.openai.com/v1",
        )
        self.github_api_token: str = self._get_env("GITHUB_API_TOKEN", self.github_token)
        self.github_api_base: str = self._get_env("GITHUB_API_BASE", "https://api.github.com")
        # cycle 58 — base branch for real PR creation (gh pr create --base)
        self.base_branch: str = self._get_env("SENTINEL_BASE_BRANCH", "main")
        # S2-3 — operator identity stamped into the completion certificate
        # (sentinel govern --verify-complete). Defaults to "unknown".
        self.operator: str = self._get_env("SENTINEL_OPERATOR", "unknown")
        # S4-10 — operator contact address printed on the assessment handoff
        # document (where the owner emails their decisions.json). Empty default.
        # (S4-11 also surfaces this + github_url/state_dir_base via
        # .sentinel/config.yaml; env vars still override.)
        self.operator_email: str = self._get_env("SENTINEL_OPERATOR_EMAIL", "")
        self.github_url: str = self._get_env("SENTINEL_GITHUB_URL", "")
        # Base directory under which `sentinel run` derives a per-run state dir
        # (<state_dir_base>/<repo>-<timestamp>). Empty ⇒ the run command falls
        # back to a repo-relative default.
        self.state_dir_base: str = self._get_env("SENTINEL_STATE_DIR_BASE", "")
        # NOTE: auto_synthesize is defined below (S4-9, default "true"); do not
        # duplicate it here — the .sentinel/config.yaml value still flows through
        # _get_env's yaml fallback for that later assignment.
        # Delay (seconds) between verifiability-gate chunk calls and Stage-5b
        # pattern-proposal calls — throttles large batches away from provider
        # per-window rate limits.
        self.verifiability_gate_chunk_delay: float = self._get_float(
            "SENTINEL_VERIFIABILITY_CHUNK_DELAY", 0.5
        )
        # Proactive cap on the number of findings sent to the verifiability gate
        # in a single model call. 0 = unbounded (one call for all findings —
        # preserves large-context behavior). Set > 0 for smaller/slower
        # providers so chunks start small enough to fit the context
        # window and finish before the gateway timeout, instead of relying on
        # the reactive split of an oversized first call.
        self.verifiability_max_chunk: int = self._get_int(
            "SENTINEL_VERIFIABILITY_MAX_FINDINGS_PER_CHUNK", 0
        )

        self._provider_override: str = self._get_env("SENTINEL_PROVIDER", "").strip().lower()
        provider_for_default = self._provider_override
        if provider_for_default not in _PUBLIC_PROVIDERS:
            if self.anthropic_api_key:
                provider_for_default = "anthropic"
            elif self.openai_api_key:
                provider_for_default = "openai"
            elif self.allow_github_models and self.github_token:
                provider_for_default = "github-models"
        model_defaults = {"anthropic": "claude-sonnet-4-6", "github-models": "gpt-4o-mini"}
        model_default = model_defaults.get(provider_for_default, "gpt-5.1")
        self.model: str = self._get_env("SENTINEL_MODEL", model_default)
        self.self_approve_mode: bool = self._get_env(
            "SENTINEL_SELF_APPROVE",
            "false",
        ).strip().lower() in {"1", "true", "yes", "on"}
        self.verification_gate: bool = self._get_env(
            "SENTINEL_VERIFICATION_GATE",
            "false",
        ).strip().lower() in {"1", "true", "yes", "on"}
        # Stage 5c — automated finding verification. Runs by default (unlike
        # the Phase-2 verification_gate above) so hallucinated/stale findings
        # are filtered before tech-debt triage.
        self.verification_stage: bool = self._get_env(
            "SENTINEL_VERIFICATION_STAGE",
            "true",
        ).strip().lower() in {"1", "true", "yes", "on"}
        self.verification_timeout_seconds: float = float(
            self._get_env("SENTINEL_VERIFICATION_TIMEOUT_SECONDS", "60") or "60"
        )
        self.enforce_single_pr_approver: bool = self._get_env(
            "SENTINEL_ENFORCE_SINGLE_PR_APPROVER",
            "false",
        ).strip().lower() in {"1", "true", "yes", "on"}
        self.required_pr_approver: str = self._get_env(
            "SENTINEL_REQUIRED_PR_APPROVER",
            "",
        ).strip()
        self.auto_approve_without_review: bool = self._get_env(
            "SENTINEL_AUTO_APPROVE_WITHOUT_REVIEW",
            "false",
        ).strip().lower() in {"1", "true", "yes", "on"}

        # ── Paths ─────────────────────────────────────────────────────
        self.repo_path: Path = Path(
            self._get_env("SENTINEL_REPO_PATH", "."),
        ).resolve()
        self.state_dir: Path = self.repo_path / self._get_env(
            "SENTINEL_STATE_DIR", "sentinel/state"
        )
        # Skill prompt files live in sentinel/skills/skill_files/*.md
        self.skill_files_dir: Path = Path(__file__).parent / "skills" / "skill_files"

        # ── Logging ───────────────────────────────────────────────────
        self.log_level: str = self._get_env("SENTINEL_LOG_LEVEL", "INFO").upper()
        self.allow_debug_logging: bool = self._get_env(
            "SENTINEL_ALLOW_DEBUG_LOGGING",
            "false",
        ).strip().lower() in {"1", "true", "yes", "on"}
        if self.log_level == "DEBUG" and not self.allow_debug_logging:
            log.warning(
                "SENTINEL_LOG_LEVEL=DEBUG requested but blocked by policy. "
                "Set SENTINEL_ALLOW_DEBUG_LOGGING=true to allow DEBUG logging. "
                "Falling back to INFO."
            )
            self.log_level = "INFO"

        # ── Dependency scanning ───────────────────────────────────────
        # Maximum number of Python files to sample for import snippet context.
        # Strict quality mode ignores this limit and scans everything.
        # Use a stratified sample across directories rather than first-N.
        self.dep_scan_limit: int = self._get_int("SENTINEL_DEP_SCAN_LIMIT", 500)

        # ── Chunking ─────────────────────────────────────────────────
        # Maximum lines per code chunk sent to an agent
        self.max_tokens: int = self._get_int("SENTINEL_MAX_TOKENS", 8192)
        self.chunk_max_lines: int = self._get_int("SENTINEL_CHUNK_MAX_LINES", 300)
        # Overlap lines between consecutive chunks
        self.chunk_overlap_lines: int = self._get_int("SENTINEL_CHUNK_OVERLAP_LINES", 80)
        # Overlap must be strictly smaller than the chunk size, otherwise
        # consecutive chunks fail to advance (a window that never moves forward
        # yields degenerate, fully-overlapping chunks or a non-terminating
        # chunker). Fail fast on invalid configuration.
        if self.chunk_overlap_lines >= self.chunk_max_lines:
            raise ValueError(
                "SENTINEL_CHUNK_OVERLAP_LINES "
                f"({self.chunk_overlap_lines}) must be strictly less than "
                f"SENTINEL_CHUNK_MAX_LINES ({self.chunk_max_lines})."
            )
        # Maximum chunks per module before capping (prevents API call explosion)
        self.max_chunks_per_module: int = self._get_int("SENTINEL_MAX_CHUNKS", 200)
        # Optional Stage 3 gate that halts Phase 1 when review coverage is incomplete.
        self.enforce_review_completeness_gate: bool = self._get_env(
            "SENTINEL_ENFORCE_REVIEW_COMPLETENESS_GATE",
            "false",
        ).strip().lower() in {"1", "true", "yes", "on"}
        self.review_module_completeness_threshold: float = self._get_float(
            "SENTINEL_REVIEW_MODULE_COMPLETENESS_THRESHOLD",
            100.0,
        )
        self.review_quick_success_threshold: float = self._get_float(
            "SENTINEL_REVIEW_QUICK_SUCCESS_THRESHOLD",
            100.0,
        )
        self.enforce_full_coverage_gate: bool = self._get_env(
            "SENTINEL_ENFORCE_FULL_COVERAGE_GATE",
            "false",
        ).strip().lower() in {"1", "true", "yes", "on"}
        self.full_coverage_threshold_pct: float = self._get_float(
            "SENTINEL_FULL_COVERAGE_THRESHOLD_PCT",
            100.0,
        )

        # ── Phase 0 Quality Foundations ─────────────────────────────
        # SENTINEL_PROFILE=full-coverage is the one-switch handoff preset: it
        # forces strict mode, all quality lanes, full specialist coverage, and
        # (via the defaults above) operational scope + alarm classification, so
        # an operator gets 100% line-by-line, all-skills coverage without
        # remembering a dozen env vars.
        self.profile: str = self._get_env("SENTINEL_PROFILE", "").strip().lower()
        _full = self.profile == "full-coverage"
        self.enable_deterministic_lane: bool = _full or self._get_env(
            "SENTINEL_ENABLE_DETERMINISTIC_LANE",
            "false",
        ).strip().lower() in {"1", "true", "yes", "on"}
        self.enable_retrieval_reasoning_lane: bool = _full or self._get_env(
            "SENTINEL_ENABLE_RETRIEVAL_REASONING_LANE",
            "false",
        ).strip().lower() in {"1", "true", "yes", "on"}
        self.enable_reconciliation: bool = _full or self._get_env(
            "SENTINEL_ENABLE_RECONCILIATION",
            "false",
        ).strip().lower() in {"1", "true", "yes", "on"}
        self.enable_quality_scorecard: bool = _full or self._get_env(
            "SENTINEL_ENABLE_QUALITY_SCORECARD",
            "false",
        ).strip().lower() in {"1", "true", "yes", "on"}
        self.strict_quality_mode: bool = _full or self._get_env(
            "SENTINEL_STRICT_QUALITY_MODE",
            "false",
        ).strip().lower() in {"1", "true", "yes", "on"}
        self.shadow_compare_mode: bool = self._get_env(
            "SENTINEL_SHADOW_COMPARE_MODE",
            "false",
        ).strip().lower() in {"1", "true", "yes", "on"}
        # When a run's state dir has a domain_context.json (written by
        # `sentinel scope`), `sentinel report` auto-enables --synthesize and
        # adopts the recorded domain — unless the operator passed --synthesize
        # explicitly. Set SENTINEL_AUTO_SYNTHESIZE=false to disable.
        self.auto_synthesize: bool = self._get_env(
            "SENTINEL_AUTO_SYNTHESIZE", "true"
        ).strip().lower() in {"1", "true", "yes", "on"}
        # Specialist-selection profile. "triggered" lets Quick Review findings
        # drive the full specialist suite; "reliability-first" forces a fixed
        # reliability subset always-on. Invalid values warn and fall back.
        _raw_profile = self._get_env(
            "SENTINEL_REVIEW_PROFILE", "reliability-first"
        ).strip().lower()
        if _raw_profile not in _VALID_REVIEW_PROFILES:
            log.warning(
                "SENTINEL_REVIEW_PROFILE=%r is not recognized; falling back to "
                "'reliability-first'. Valid values: %s",
                _raw_profile,
                ", ".join(sorted(_VALID_REVIEW_PROFILES)),
            )
            _raw_profile = "reliability-first"
        self.review_profile: str = _raw_profile
        self.enforce_full_specialist_coverage: bool = _full or self._get_env(
            "SENTINEL_ENFORCE_FULL_SPECIALIST_COVERAGE",
            "false",
        ).strip().lower() in {"1", "true", "yes", "on"}
        self.retrieval_max_snippets_per_skill: int = self._get_int(
            "SENTINEL_RETRIEVAL_MAX_SNIPPETS_PER_SKILL",
            4,
        )
        self.retrieval_max_chars_per_snippet: int = self._get_int(
            "SENTINEL_RETRIEVAL_MAX_CHARS_PER_SNIPPET",
            1200,
        )
        # Stage 4c (concept themes) is the optional conceptual-lens grouping
        # (description embedding + HDBSCAN clustering). Unlike the Phase-0
        # lanes above it defaults ON, preserving the historic always-run
        # behavior; set SENTINEL_ENABLE_CONCEPT_THEMES=false to skip the
        # (expensive) clustering when only the structural lens / decision
        # brief is needed. It is an alternate lens, not a coverage gate, so
        # strict mode does NOT force it on (see the strict block below).
        self.enable_concept_themes: bool = self._get_env(
            "SENTINEL_ENABLE_CONCEPT_THEMES",
            "true",
        ).strip().lower() in {"1", "true", "yes", "on"}

        # Alarm-relevance classification (Stage 5c-alarm) labels each finding
        # direct/adjacent/none so the Decision Brief can focus on alarm and
        # alarm-adjacent defects. Defaults ON; deterministic keyword seeding is
        # the sole path in local-only mode, otherwise the Architect refines it.
        self.enable_alarm_classification: bool = False  # alarm skills not shipped

        # Alarm / WIP-stall specialist (layer2k). Defaults ON; set false (or pass
        # --no-alarms) to skip it entirely for codebases with no alarms, so the
        # trigger never schedules it even under full specialist coverage.
        self.enable_alarm_specialist: bool = False  # alarm skills not shipped

        # Assessment scope: which files Stage 1 admits as review targets.
        #   "operational" (default) — production/runtime source only (the
        #     language-agnostic operational-code set); config/data, docs,
        #     helper scripts, project/build files, and tests are excluded so a
        #     run focuses on the code that does the work.
        #   "all" — every non-excluded file (legacy behavior).
        _raw_scope = self._get_env(
            "SENTINEL_ASSESSMENT_SCOPE", "operational"
        ).strip().lower()
        self.assessment_scope: str = (
            _raw_scope if _raw_scope in {"operational", "all"} else "operational"
        )

        if self.strict_quality_mode:
            # Strict mode enforces full-lane execution and 100% review gates.
            # Specialist breadth is controlled independently by the review profile.
            self.enable_deterministic_lane = True
            self.enable_retrieval_reasoning_lane = True
            self.enable_reconciliation = True
            self.enable_quality_scorecard = True
            self.enforce_review_completeness_gate = True
            self.enforce_full_coverage_gate = True
            self.review_module_completeness_threshold = 100.0
            self.review_quick_success_threshold = 100.0
            self.full_coverage_threshold_pct = 100.0

        # ── Learning Feed ──────────────────────────────────────────
        self.auto_extract_learnings: bool = self._get_env(
            "SENTINEL_AUTO_EXTRACT_LEARNINGS",
            "true",
        ).strip().lower() in {"1", "true", "yes", "on"}
        self.authored_by_foundry: bool = self._get_env(
            "SENTINEL_AUTHORED_BY_FOUNDRY",
            "false",
        ).strip().lower() in {"1", "true", "yes", "on"}
        self.learnings_path: str = self._get_env("SENTINEL_LEARNINGS_PATH", "")

        # ── Workflow Config (spec §10.1) ─────────────────────────────
        # Optional YAML path consumed by the Orchestrator's lazy
        # ``_workflow_config`` property (sub-step 8-E). Empty string
        # ⇒ ``WorkflowConfig.default()`` is used.
        self.workflow_config_yaml_path: str = self._get_env(
            "SENTINEL_WORKFLOW_CONFIG_YAML", ""
        )

        # ── Retry ────────────────────────────────────────────────────
        self.api_max_retries: int = self._get_int("SENTINEL_API_MAX_RETRIES", 3)
        self.api_retry_delay: float = self._get_float("SENTINEL_API_RETRY_DELAY", 2.0)

        # ── Coder execution layer (cycle 56) ─────────────────────────
        # Command the Coder runs to verify its edits. Empty ⇒ the test runner
        # defaults to ``<current interpreter> -m pytest`` in the repo root.
        self.test_command: str = self._get_env("SENTINEL_TEST_COMMAND", "")
        self.test_timeout: int = self._get_int("SENTINEL_TEST_TIMEOUT", 300)

    # ------------------------------------------------------------------

    def _get_env(self, key: str, default: str = "") -> str:
        # Precedence: explicit env var > .sentinel/config.yaml > built-in default.
        if key in self._env:
            value = self._env.get(key)
            return "" if value is None else str(value)
        yaml_key = _ENV_TO_YAML_KEY.get(key)
        if yaml_key is not None:
            yaml_value = getattr(self, "_yaml_defaults", {}).get(yaml_key)
            if yaml_value is not None:
                return str(yaml_value)
        return "" if default is None else str(default)

    def _get_int(self, key: str, default: int) -> int:
        raw = self._get_env(key, str(default)).strip()
        try:
            return int(raw)
        except ValueError as exc:
            raise EnvironmentError(
                f"Environment variable '{key}' must be an integer. Got: {raw!r}"
            ) from exc

    def _get_float(self, key: str, default: float) -> float:
        raw = self._get_env(key, str(default)).strip()
        try:
            return float(raw)
        except ValueError as exc:
            raise EnvironmentError(
                f"Environment variable '{key}' must be a float. Got: {raw!r}"
            ) from exc

    def _require(self, key: str) -> str:
        value = self._get_env(key)
        if not value:
            raise EnvironmentError(
                f"Required environment variable '{key}' is not set. "
                f"Copy .env.example to .env and fill in your values."
            )
        return value

    @property
    def provider(self) -> str:
        """Return the selected LLM provider ("" when none can be resolved).

        Resolution order:
        1. Explicit ``SENTINEL_PROVIDER`` (anthropic, openai, github-models, copilot-cli).
        2. Auto-detect from ANTHROPIC_API_KEY, OPENAI_API_KEY, or
           GITHUB_TOKEN + SENTINEL_ALLOW_GITHUB_MODELS=true.
        """
        if self._provider_override:
            if self._provider_override not in _PUBLIC_PROVIDERS:
                log.warning(
                    "SENTINEL_PROVIDER=%r is not a recognised provider. "
                    "Valid values: %s. Falling back to auto-detection.",
                    self._provider_override,
                    ", ".join(sorted(_PUBLIC_PROVIDERS)),
                )
            elif self._provider_override == "github-models" and not self.allow_github_models:
                log.warning(
                    "SENTINEL_PROVIDER='github-models' is blocked because "
                    "SENTINEL_ALLOW_GITHUB_MODELS is not enabled."
                )
                return ""
            else:
                return self._provider_override

        if self.anthropic_api_key:
            return "anthropic"
        if self.openai_api_key:
            return "openai"
        if self.allow_github_models and self.github_token:
            return "github-models"
        if not self.local_only_mode:
            log.warning(
                "No model provider credentials found. Set ANTHROPIC_API_KEY, "
                "OPENAI_API_KEY, or GITHUB_TOKEN + SENTINEL_ALLOW_GITHUB_MODELS=true, "
                "or set SENTINEL_PROVIDER=copilot-cli."
            )
        return ""

    def ensure_state_dir(self) -> None:
        """Create the state directory if it does not exist."""
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def configure_logging(self) -> None:
        logging.basicConfig(
            level=getattr(logging, self.log_level, logging.INFO),
            format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
            datefmt="%H:%M:%S",
        )

    def __repr__(self) -> str:
        return (
            f"Config(model={self.model!r}, "
            f"repo={self.repo_path}, "
            f"state_dir={self.state_dir})"
        )


# Module-level singleton — call load_config() once, import cfg everywhere.
_config: Config | None = None


def load_config() -> Config:
    """Load and return the global Config singleton."""
    global _config
    if _config is None:
        _config = Config()
        _config.configure_logging()
        _config.ensure_state_dir()
        log.debug("Config loaded: %s", _config)
    return _config


def get_config() -> Config:
    """Return the already-loaded Config. Raises if load_config() not yet called."""
    if _config is None:
        raise RuntimeError("Config not loaded. Call load_config() first.")
    return _config
