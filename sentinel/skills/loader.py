"""
sentinel/skills/loader.py

Skill file loader for Sentinel.

Skill files are Markdown documents in sentinel/skill_files/*.md.
Each file has a YAML front-matter block with name and description,
followed by the skill body which becomes the agent's system prompt.

The loader strips the front-matter and returns the body as a clean
string ready to inject as a system prompt.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from pathlib import Path

log = logging.getLogger(__name__)

# Front-matter is delimited by --- on its own line
_FRONTMATTER_RE = re.compile(r"^---\s*\n.*?\n---\s*\n", re.DOTALL)


class SkillNotFoundError(KeyError):
    """Raised when a requested skill file does not exist."""


class SkillLoader:
    """
    Loads skill files from the skill_files directory.

    Skills are identified by their filename stem (without .md extension).
    E.g., 'layer2a-quick-review' loads layer2a-quick-review.md.
    """

    def __init__(self, skill_files_dir: Path) -> None:
        self._dir = skill_files_dir
        self._cache: dict[str, str] = {}
        self._index: dict[str, Path] = {}
        self._build_index()

    def _build_index(self) -> None:
        """Scan the skill files directory and index all .md files."""
        if not self._dir.exists():
            log.warning("Skill files directory not found: %s", self._dir)
            return
        for path in self._dir.glob("*.md"):
            self._index[path.stem] = path
        log.debug("Indexed %d skill file(s) from %s", len(self._index), self._dir)

    def load(self, skill_name: str) -> str:
        """
        Load a skill by name and return its body as a system prompt string.

        The YAML front-matter block is stripped. The remaining Markdown
        content is returned verbatim.

        Args:
            skill_name: The skill file stem, e.g. 'layer2a-quick-review'.

        Returns:
            The skill body as a string.

        Raises:
            SkillNotFoundError: If no matching skill file exists.
        """
        if skill_name in self._cache:
            return self._cache[skill_name]

        path = self._index.get(skill_name)
        if path is None:
            available = ", ".join(sorted(self._index.keys()))
            raise SkillNotFoundError(
                f"Skill '{skill_name}' not found. "
                f"Available skills: {available or '(none)'}"
            )

        raw = path.read_text(encoding="utf-8")
        body = _FRONTMATTER_RE.sub("", raw).strip()

        self._cache[skill_name] = body
        log.debug("Loaded skill '%s' (%d chars)", skill_name, len(body))
        return body

    def list_skills(self) -> list[str]:
        """Return sorted list of all available skill names."""
        return sorted(self._index.keys())

    def exists(self, skill_name: str) -> bool:
        """Return whether a skill file exists for ``skill_name``."""
        return skill_name in self._index

    def reload(self) -> None:
        """Clear cache and re-index the skill files directory."""
        self._cache.clear()
        self._index.clear()
        self._build_index()


# ---------------------------------------------------------------------------
# Skill name constants — used throughout the codebase
# ---------------------------------------------------------------------------

class Skills:
    """Named constants for all skill file stems."""

    # Layer 0 — Discovery
    CARTOGRAPHER        = "layer0a-codebase-cartographer"
    DEPENDENCY_ANALYZER = "layer0b-dependency-analyzer"
    SCOPE_GUARDIAN      = "layer0c-scope-guardian"
    ARCH_CHECKLIST      = "layer0d-architectural-checklist"

    # Layer 1 — Planning
    REVIEW_PRIORITIZER  = "layer1-review-prioritizer"

    # Layer 2 — Core Review
    QUICK_REVIEW        = "layer2a-quick-review"
    CORRECTNESS         = "layer2b-correctness-specialist"
    SECURITY            = "layer2c-security-specialist"
    DATA_INTEGRITY      = "layer2d-data-integrity-specialist"
    API_CONTRACT        = "layer2e-api-contract-compatibility"
    DATA_FLOW_TRUST     = "layer2f-data-flow-trust-boundary"
    CONFIG_DRIFT        = "layer2g-config-environment-drift"
    BUILD_RELEASE       = "layer2h-build-release-integrity"
    CHANGE_IMPACT       = "layer2i-change-impact-testability"
    OPERATIONAL_RISK    = "layer2j-operational-risk-failure-modes"
    ALARM_INTEGRITY     = "layer2k-alarm-integrity-specialist"

    # Layer 3 — Specialist Review
    PERFORMANCE         = "layer3a-performance-specialist"
    MAINTAINABILITY     = "layer3b-maintainability-specialist"
    TESTABILITY         = "layer3c-testability-specialist"
    ERROR_HANDLING      = "layer3d-error-handling-specialist"
    CONCURRENCY         = "layer3e-concurrency-specialist"

    # Layer 4 — Structural
    CODE_SMELL          = "layer4a-code-smell-detector"
    REFACTORING_ADVISOR = "layer4b-refactoring-advisor"

    # Layer 5 — Planning
    VERIFIABILITY_GATE  = "layer5a-verifiability-gate"
    ACCEPTANCE_CRITERIA_GATE = "layer5b-acceptance-criteria-gate"
    FIX_PLANNER         = "layer5-fix-planner"
    REMEDIATION_PATTERN_PROPOSER = "layer5b-remediation-pattern-proposer"
    FINDING_VERIFIER    = "layer5c-finding-verifier"
    ALARM_CLASSIFIER    = "layer5d-alarm-relevance-classifier"

    # Layer 6 — Verification
    VERIFICATION        = "layer6a-verification-reviewer"
    CROSS_REPO_VERIFY   = "layer6b-cross-repo-verification"
    TRANSITION_MANIFEST_GENERATOR = "layer6c-transition-manifest-generator"

    # Layer 7 — Feature Enhancement (Phase 3)
    FEASIBILITY_ANALYZER  = "layer7a-feature-feasibility-analyzer"
    SPEC_WRITER           = "layer7b-enhancement-spec-writer"
    COMPAT_CHECKER        = "layer7c-backwards-compatibility-checker"

    # Layer 8 — Defect RCA & Bug Remediation
    ROOT_CAUSE_ANALYZER   = "layer8a-root-cause-analyzer"
    BUG_SPEC_WRITER       = "layer8b-bug-remediation-spec-writer"

    # All specialist skills triggered by Quick Review findings
    SPECIALISTS: list[str] = [
        CORRECTNESS, SECURITY, DATA_INTEGRITY,
        PERFORMANCE, MAINTAINABILITY, TESTABILITY,
        ERROR_HANDLING, CONCURRENCY, ALARM_INTEGRITY,
    ]
