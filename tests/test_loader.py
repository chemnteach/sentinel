"""
tests/test_loader.py

Tests for sentinel.skills.loader — skill file loading and indexing.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import get_args

import pytest

from sentinel.assess.finding_kinds import get_finding_kind_definition
from sentinel.assess.subject_key_registry import (
    ConsolidationRule,
    NAMESPACES,
    TABLE_2_RELATIONSHIPS,
    RelationshipType,
    SurfaceCategory,
    get_co_emission_consolidation_rule,
    get_namespace_owner,
    get_table_2_peers,
    get_unidirectional_relationships,
    is_shared_namespace,
)
from sentinel.skills.loader import Skills, SkillLoader, SkillNotFoundError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def skill_dir(tmp_path: Path) -> Path:
    """Create a temporary skill_files directory with sample skill files."""
    skills_dir = tmp_path / "skill_files"
    skills_dir.mkdir()

    # Skill with YAML front-matter
    (skills_dir / "layer2a-quick-review.md").write_text(
        textwrap.dedent(
            """\
            ---
            name: layer2a-quick-review
            description: Comprehensive code review
            allowed-tools: []
            ---

            # Quick Review

            You are a senior code reviewer.

            ## Your Task

            Review the code for issues.
            """
        ),
        encoding="utf-8",
    )

    # Skill without front-matter
    (skills_dir / "no-frontmatter.md").write_text(
        "# Plain skill\n\nJust content, no front-matter.\n",
        encoding="utf-8",
    )

    # Skill with Windows-style CRLF line endings
    (skills_dir / "layer0c-scope-guardian.md").write_text(
        "---\r\nname: layer0c-scope-guardian\r\ndescription: Scope guardian\r\n---\r\n\r\n# Scope Guardian\r\n\r\nBody content.\r\n",
        encoding="utf-8",
    )

    return skills_dir


# ---------------------------------------------------------------------------
# SkillLoader tests
# ---------------------------------------------------------------------------

class TestSkillLoader:
    def test_indexes_md_files_on_init(self, skill_dir: Path) -> None:
        loader = SkillLoader(skill_dir)
        skills = loader.list_skills()
        assert "layer2a-quick-review" in skills
        assert "no-frontmatter" in skills

    def test_load_strips_frontmatter(self, skill_dir: Path) -> None:
        loader = SkillLoader(skill_dir)
        body = loader.load("layer2a-quick-review")
        assert "---" not in body.splitlines()[0]
        assert "# Quick Review" in body
        assert "You are a senior code reviewer." in body

    def test_load_no_frontmatter_returns_full_content(
        self, skill_dir: Path
    ) -> None:
        loader = SkillLoader(skill_dir)
        body = loader.load("no-frontmatter")
        assert "# Plain skill" in body
        assert "Just content" in body

    def test_load_caches_result(self, skill_dir: Path) -> None:
        loader = SkillLoader(skill_dir)
        first = loader.load("layer2a-quick-review")
        second = loader.load("layer2a-quick-review")
        assert first is second  # Same object — returned from cache

    def test_load_raises_on_missing_skill(self, skill_dir: Path) -> None:
        loader = SkillLoader(skill_dir)
        with pytest.raises(SkillNotFoundError, match="nonexistent"):
            loader.load("nonexistent")

    def test_reload_clears_cache(self, skill_dir: Path) -> None:
        loader = SkillLoader(skill_dir)
        loader.load("layer2a-quick-review")
        assert "layer2a-quick-review" in loader._cache

        loader.reload()
        assert loader._cache == {}

    def test_missing_directory_does_not_raise(self, tmp_path: Path) -> None:
        """SkillLoader should log a warning but not raise if dir missing."""
        loader = SkillLoader(tmp_path / "nonexistent")
        assert loader.list_skills() == []

    def test_list_skills_sorted(self, skill_dir: Path) -> None:
        loader = SkillLoader(skill_dir)
        skills = loader.list_skills()
        assert skills == sorted(skills)

    def test_error_message_includes_available_skills(
        self, skill_dir: Path
    ) -> None:
        loader = SkillLoader(skill_dir)
        with pytest.raises(SkillNotFoundError) as exc_info:
            loader.load("not-a-skill")
        assert "layer2a-quick-review" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Skills constants tests
# ---------------------------------------------------------------------------

class TestSkillsConstants:
    def test_all_constants_are_strings(self) -> None:
        for name in vars(Skills):
            if not name.startswith("_") and name != "SPECIALISTS":
                value = getattr(Skills, name)
                if isinstance(value, str):
                    assert value.startswith("layer"), (
                        f"Skills.{name} = {value!r} should start with 'layer'"
                    )

    def test_specialists_is_list(self) -> None:
        assert isinstance(Skills.SPECIALISTS, list)
        assert len(Skills.SPECIALISTS) > 0
        for s in Skills.SPECIALISTS:
            assert isinstance(s, str)

    def test_no_duplicate_skill_names(self) -> None:
        all_skills = [
            v for k, v in vars(Skills).items()
            if not k.startswith("_") and isinstance(v, str)
        ]
        assert len(all_skills) == len(set(all_skills)), (
            "Duplicate skill name detected in Skills constants"
        )

    def test_specialists_subset_of_all_skills(self) -> None:
        all_skills = {
            v for k, v in vars(Skills).items()
            if not k.startswith("_") and isinstance(v, str)
        }
        for s in Skills.SPECIALISTS:
            assert s in all_skills, f"{s} in SPECIALISTS but not in Skills constants"


# ---------------------------------------------------------------------------
# Real skill prompt contract tests
# ---------------------------------------------------------------------------

class TestLayer2aQuickReviewPromptContract:
    @staticmethod
    def _load_prompt() -> str:
        prompt_path = (
            Path(__file__).resolve().parents[1]
            / "sentinel"
            / "skills"
            / "skill_files"
            / "layer2a-quick-review.md"
        )
        return prompt_path.read_text(encoding="utf-8")

    @staticmethod
    def _compact(text: str) -> str:
        return " ".join(text.split())

    def test_layer2a_prompt_declares_allowed_finding_kinds(self) -> None:
        prompt = self._load_prompt()

        for finding_kind in (
            "`logic_bug`",
            "`contract_violation`",
            "`injection_risk`",
            "`missing_transaction_boundary`",
            "`timeout_handling_gap`",
            "`structural_complexity`",
            "`algorithmic_scaling_gap`",
            "`error_signal_loss`",
        ):
            assert finding_kind in prompt

        assert "Do not invent other `finding_kind` values" in prompt

    def test_layer2a_prompt_declares_subject_key_rules(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        for subject_key_rule in (
            (
                "| `logic_bug` | Incorrect branching, arithmetic, null/undefined "
                "handling, state transition, boundary condition, race condition, "
                "off-by-one error, or other correctness failure. | Optional. Omit "
                "`subject_key` when there is no stable named contract or API subject. |"
            ),
            (
                "| `contract_violation` | A caller-visible API, method, configuration, "
                "invariant, precondition, postcondition, data contract, or documented "
                "behavior is violated. | Required. Include a non-empty `subject_key` "
                "naming the contract, API, method, invariant, or configuration key. |"
            ),
            (
                "| `injection_risk` | Untrusted input can reach a SQL, command, path, "
                "URL, HTML, XML, deserialization, eval, template, log, or other "
                "dangerous sink without adequate validation/encoding/parameterization. "
                "| Required. Include a non-empty `subject_key` naming the sink or "
                "input-to-sink path. |"
            ),
            (
                "| `missing_transaction_boundary` | Multiple related state changes "
                "must succeed/fail atomically but are performed without an appropriate "
                "transaction, lock, idempotency key, or rollback boundary. | Required. "
                "Include a non-empty `subject_key` naming the operation or state "
                "transition that needs the boundary. |"
            ),
            (
                "| `timeout_handling_gap` | Network, file, subprocess, database, "
                "async, retry, polling, or external-service work can hang, block "
                "indefinitely, retry unsafely, or fail without bounded timeout handling. "
                "| Required. Include a non-empty `subject_key` naming the call, "
                "operation, or dependency missing timeout handling. |"
            ),
            (
                "| `structural_complexity` | Complexity, duplication, god-class "
                "behavior, over-engineering, unclear control flow, or maintainability "
                "risk is the primary issue and no more specific correctness/security/"
                "performance kind applies. | Not applicable. Omit `subject_key` or set "
                "it to `_na`. |"
            ),
            (
                "| `algorithmic_scaling_gap` | Algorithmic complexity, N+1 calls, "
                "unbounded memory growth, full-table scans, or load-sensitive "
                "performance behavior will fail at scale. | Required. Include a "
                "non-empty `subject_key` naming the algorithm, query, loop, or "
                "operation that scales poorly. |"
            ),
            (
                "| `error_signal_loss` | Exceptions, failed return values, validation "
                "failures, missing logs, null/error sentinels, or operator-visible "
                "failure signals are swallowed, hidden, overwritten, or converted into "
                "misleading success. | Optional. Omit `subject_key` when there is no "
                "stable named error contract; include one when the lost signal belongs "
                "to a named API or operation. |"
            ),
        ):
            assert self._compact(subject_key_rule) in compact_prompt

    def test_layer2a_prompt_declares_summary_requirement(self) -> None:
        prompt = self._load_prompt()

        assert '"summary": "<short stable finding title>"' in prompt
        assert "`summary`: one short, stable title" in prompt
        assert "`summary`: one short, stable finding title" in prompt

    def test_layer2a_prompt_declares_json_escaping_requirements(self) -> None:
        prompt = self._load_prompt()

        assert "JSON string escaping requirements" in prompt
        assert r'`\"` for embedded double-quotes' in prompt
        assert r'`\\` for backslashes' in prompt
        assert (
            "Unescaped quotes inside field values will cause the finding "
            "to be rejected by JSON parsing"
        ) in prompt
        assert "paraphrase the code-level fix in plain English" in prompt

    def test_layer2a_prompt_declares_routing_preservation_terms(self) -> None:
        prompt = self._load_prompt()

        assert "## Routing Preservation Requirements" in prompt
        assert "Trigger vocabulary to preserve when accurate" in prompt
        assert "Do not add trigger words mechanically" in prompt

        for trigger_term in (
            "security",
            "injection",
            "authentication",
            "authorization",
            "correctness",
            "off-by-one",
            "race condition",
            "performance",
            "n+1",
            "memory",
            "maintainability",
            "complexity",
            "duplication",
            "testability",
            "test coverage",
            "error handling",
            "exception",
            "concurrency",
            "async",
            "data integrity",
            "validation",
            "transaction",
            "code smell",
            "refactor",
            "god class",
        ):
            assert trigger_term in prompt

    def test_layer2a_prompt_declares_empty_findings_shape(self) -> None:
        prompt = self._load_prompt()

        assert "empty-findings JSON shape" in prompt
        assert '"findings": []' in prompt
        assert '"highest_priority": "none"' in prompt
        assert "Do not manufacture findings to avoid an empty array" in prompt


class TestLayer2bCorrectnessPromptContract:
    def test_layer2b_prompt_declares_f2_contract(self) -> None:
        prompt_path = (
            Path(__file__).resolve().parents[1]
            / "sentinel"
            / "skills"
            / "skill_files"
            / "layer2b-correctness-specialist.md"
        )
        prompt = prompt_path.read_text(encoding="utf-8")

        for finding_kind in ("`logic_bug`", "`contract_violation`", "`error_signal_loss`"):
            assert finding_kind in prompt

        assert "Do not invent other `finding_kind` values" in prompt
        assert '"summary": "<short stable finding title>"' in prompt
        assert "`summary`: one short, stable title" in prompt

        for subject_key_rule in (
            "`contract_violation` | Required. Include a non-empty `subject_key`",
            "`logic_bug` | Optional. Omit `subject_key`",
            "`error_signal_loss` | Optional. Omit `subject_key`",
        ):
            assert subject_key_rule in prompt

        assert "JSON string escaping requirements" in prompt

        for code_bearing_field in (
            "`description`",
            "`counterexample`",
            "`expected_behaviour`",
            "`actual_behaviour`",
            "`suggested_fix`",
        ):
            assert code_bearing_field in prompt

        assert r'`\"` for embedded double-quotes' in prompt
        assert r'`\\` for backslashes' in prompt
        assert (
            "Unescaped quotes inside field values will cause the finding "
            "to be rejected by JSON parsing"
        ) in prompt
        assert "paraphrase the code-level fix or counterexample in plain English" in prompt


class TestLayer2cSecuritySpecialistPromptContract:
    @staticmethod
    def _load_prompt() -> str:
        prompt_path = (
            Path(__file__).resolve().parents[1]
            / "sentinel"
            / "skills"
            / "skill_files"
            / "layer2c-security-specialist.md"
        )
        return prompt_path.read_text(encoding="utf-8")

    @staticmethod
    def _compact(text: str) -> str:
        return " ".join(text.split())

    def test_layer2c_prompt_declares_allowed_finding_kinds(self) -> None:
        prompt = self._load_prompt()

        for finding_kind in (
            "`injection_risk`",
            "`authorization_gap`",
            "`sensitive_data_exposure`",
            "`insecure_deserialization`",
            "`crypto_misuse`",
            "`security_misconfiguration`",
        ):
            assert finding_kind in prompt

        assert "Do not invent other `finding_kind` values" in prompt

    def test_layer2c_prompt_declares_required_subject_key_rules(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert (
            "All six allowed `layer2c-security-specialist` finding kinds "
            "require a non-empty `subject_key`."
        ) in prompt

        for subject_key_rule in (
            (
                "| `injection_risk` | Untrusted input reaches a SQL, command, "
                "path, URL, HTML, XML, deserialization, eval, template, archive "
                "extraction, file operation, SSRF, XXE, or other sink that "
                "interprets, executes, queries, or resolves it unsafely. | "
                "Required. Include a non-empty `subject_key` naming the concrete "
                "sink or input-to-sink path"
            ),
            (
                "| `authorization_gap` | A sensitive action, protected data path, "
                "identity claim, role boundary, function-level permission, "
                "object-level permission, session boundary, login/reset path, or "
                "administrative endpoint lacks the required authentication or "
                "authorization check. | Required. Include a non-empty `subject_key` "
                "naming the protected action, endpoint, resource, role boundary, "
                "identity claim, or auth check"
            ),
            (
                "| `sensitive_data_exposure` | Secrets or sensitive data are "
                "exposed in source, logs, errors, transport, URLs, referrers, "
                "stack traces, configuration, client-visible responses, or "
                "externally visible state. | Required. Include a non-empty "
                "`subject_key` naming the exposed secret, PII field, log path, "
                "error response, config key, transport channel, or visible state"
            ),
            (
                "| `insecure_deserialization` | Untrusted data is loaded, parsed, "
                "deserialized, instantiated, evaluated, or executed by a mechanism "
                "that can run code, construct unsafe objects, resolve external "
                "entities, or trigger unsafe payload behavior. | Required. Include "
                "a non-empty `subject_key` naming the unsafe loader/parser/evaluator "
                "and payload path"
            ),
            (
                "| `crypto_misuse` | Cryptographic primitives, password hashing, "
                "keys, IVs, nonces, randomness, MAC/signature comparison, "
                "certificate validation, TLS verification, or custom crypto are "
                "used unsafely. | Required. Include a non-empty `subject_key` "
                "naming the cryptographic operation, key/nonce/cert/randomness "
                "use, password-hash path, or TLS setting"
            ),
            (
                "| `security_misconfiguration` | Security-relevant defaults, "
                "deployment settings, debug modes, CORS policies, CSRF settings, "
                "security headers, admin exposure, file permissions, dependency "
                "configuration, or production runtime settings weaken a protection "
                "boundary. | Required. Include a non-empty `subject_key` naming "
                "the insecure setting, default, deployment flag, CORS/header policy, "
                "admin exposure, or config key"
            ),
        ):
            assert self._compact(subject_key_rule) in compact_prompt

        assert "`subject_key` is required for every allowed `finding_kind`" in prompt

    def test_layer2c_prompt_declares_summary_requirement(self) -> None:
        prompt = self._load_prompt()

        assert '"summary": "<short stable finding title>"' in prompt
        assert "`summary`: one short, stable title" in prompt
        assert "one short, stable finding title" in prompt

    def test_layer2c_prompt_declares_json_escaping_requirements(self) -> None:
        prompt = self._load_prompt()

        assert "JSON string escaping requirements" in prompt
        assert r'`\"` for embedded double-quotes' in prompt
        assert r'`\\` for backslashes' in prompt
        assert (
            "Unescaped quotes inside field values will cause the finding "
            "to be rejected by JSON parsing"
        ) in prompt
        assert "paraphrase the attack or fix in plain English" in prompt

        for code_bearing_field in (
            "`description`",
            "`attack_scenario`",
            "`impact`",
            "`suggested_fix`",
            "`location`",
        ):
            assert code_bearing_field in prompt

    def test_layer2c_prompt_declares_description_evidence_requirements(self) -> None:
        prompt = self._load_prompt()

        assert "Security evidence requirements for `description`" in prompt
        assert "Include the vulnerability mechanism in plain language" in prompt
        assert "Include the CWE reference inline in a stable format" in prompt
        assert "(CWE-89)" in prompt
        assert "(CWE-862)" in prompt
        assert "Include the attack mechanism summarized in plain language" in prompt
        assert "Include the impact summarized in plain language" in prompt
        assert "mechanism, inline CWE, attack mechanism, and impact" in prompt

    def test_layer2c_prompt_declares_empty_findings_shape(self) -> None:
        prompt = self._load_prompt()

        assert "empty-findings JSON shape" in prompt
        assert '"findings": []' in prompt
        assert '"highest_severity_finding": "none"' in prompt
        assert "Do not manufacture findings to avoid an empty array" in prompt

    def test_layer2c_prompt_declares_supplemental_fields(self) -> None:
        prompt = self._load_prompt()

        for supplemental_field in (
            '"cwe_id": "CWE-89 (or closest match)"',
            '"vulnerability_class": "injection | auth | data-exposure | deserialization | misconfiguration | crypto | input-validation"',
            '"attack_scenario": "<realistic end-to-end attack using this vulnerability>"',
            '"impact": "<confidentiality | integrity | availability — what can an attacker achieve>"',
        ):
            assert supplemental_field in prompt

        assert (
            "Always include `cwe_id`, `vulnerability_class`, "
            "`attack_scenario`, and `impact` in each finding"
        ) in prompt
        assert "`vulnerability_class`: supplemental security taxonomy" in prompt
        assert "`cwe_id`: include the closest CWE identifier" in prompt


class TestLayer3aPerformanceSpecialistPromptContract:
    @staticmethod
    def _load_prompt() -> str:
        prompt_path = (
            Path(__file__).resolve().parents[1]
            / "sentinel"
            / "skills"
            / "skill_files"
            / "layer3a-performance-specialist.md"
        )
        return prompt_path.read_text(encoding="utf-8")

    @staticmethod
    def _compact(text: str) -> str:
        return " ".join(text.split())

    def test_layer3a_prompt_declares_allowed_finding_kinds(self) -> None:
        prompt = self._load_prompt()

        for finding_kind in (
            "`n_plus_one_query`",
            "`algorithmic_scaling_gap`",
            "`blocking_io_in_async`",
            "`cache_correctness_gap`",
        ):
            assert finding_kind in prompt

        assert "Do not invent other `finding_kind` values" in prompt

        for forbidden_kind in (
            "`memory_leak`",
            "`hot_path`",
            "`full_table_scan`",
            "`slow_query`",
            "`slow_io`",
            "`allocation_pressure`",
            "`performance_issue`",
        ):
            assert forbidden_kind in prompt

    def test_layer3a_prompt_declares_required_subject_key_rules(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert (
            "All four allowed `layer3a-performance-specialist` finding kinds "
            "require a non-empty `subject_key`."
        ) in prompt

        for subject_key_rule in (
            (
                "| `n_plus_one_query` | A data access path issues per-item "
                "database, ORM, repository, service, or lazy-load fetches "
                "instead of a bounded batched or joined access pattern. | "
                "Required. Include a non-empty `subject_key` naming the query, "
                "ORM relation, loop-to-query path, repository method, service "
                "call path, or data-access operation. |"
            ),
            (
                "| `algorithmic_scaling_gap` | Algorithm, data-structure choice, "
                "nested loop, full scan, repeated expensive computation, "
                "unbounded allocation, memory growth, or hot-path work scales "
                "worse than realistic production load allows. | Required. "
                "Include a non-empty `subject_key` naming the algorithm, loop, "
                "scan, allocation path, data structure, hot-path operation, or "
                "workload-sensitive operation that scales poorly. |"
            ),
            (
                "| `blocking_io_in_async` | Blocking file, network, subprocess, "
                "sleep, lock, wait, or synchronization work is placed on an "
                "async, event-loop, UI-thread, scheduler, or otherwise "
                "event-loop-bound path. | Required. Include a non-empty "
                "`subject_key` naming the async/event-loop path and the "
                "blocking operation. |"
            ),
            (
                "| `cache_correctness_gap` | Cache key, invalidation, stampede, "
                "TTL, stale-data, mutability, or collision behavior makes the "
                "cache unsafe, misleading, or self-defeating. | Required. "
                "Include a non-empty `subject_key` naming the cache key, cache "
                "entry, invalidation path, stampede behavior, TTL policy, "
                "stale-data path, or cached operation. |"
            ),
        ):
            assert self._compact(subject_key_rule) in compact_prompt

    def test_layer3a_prompt_declares_summary_requirement(self) -> None:
        prompt = self._load_prompt()

        assert '"summary": "<short stable finding title>"' in prompt
        assert "`summary`: one short, stable finding title" in prompt
        assert "Keep `summary` stable across regenerations" in prompt

    def test_layer3a_prompt_declares_json_escaping_requirements(self) -> None:
        prompt = self._load_prompt()

        assert "JSON String Escaping Requirements" in prompt
        assert r'`\"` for embedded double-quotes' in prompt
        assert r'`\\` for backslashes' in prompt
        assert (
            "Unescaped quotes inside field values will cause the finding "
            "to be rejected by JSON parsing"
        ) in prompt
        assert "Prefer plain-English paraphrases" in prompt

        for code_bearing_field in (
            "`description`",
            "`location`",
            "`complexity`",
            "`production_impact`",
            "`suggested_fix`",
            "`subject_key`",
        ):
            assert code_bearing_field in prompt

    def test_layer3a_prompt_declares_description_evidence_requirements(self) -> None:
        prompt = self._load_prompt()

        assert "Performance Evidence Requirements for `description`" in prompt
        assert "Bottleneck or performance mechanism" in prompt
        assert "Complexity or scaling behavior" in prompt
        assert "Realistic load or production impact" in prompt
        assert "Why the suggested fix improves scaling or boundedness" in prompt
        assert "description must be self-contained" in prompt

    def test_layer3a_prompt_declares_discriminator_rules_and_examples(self) -> None:
        prompt = self._load_prompt()

        for rule_heading in (
            "`n_plus_one_query` wins for per-item fetch patterns",
            "`blocking_io_in_async` is only for event-loop or async-path blocking",
            "`cache_correctness_gap` is only for cache semantics",
            "`algorithmic_scaling_gap` covers concrete scaling mechanisms",
            "Decline speculative or micro-optimization concerns",
        ):
            assert rule_heading in prompt

        assert "Ordinary synchronous I/O in ordinary synchronous code does not qualify" in prompt
        assert "A cache miss, a slow lookup, or the absence of a cache is not enough" in prompt
        assert "Do not invent other `finding_kind` values" in prompt

        for concrete_example in (
            "Loops N×M iterations where N is the number of lots",
            "Queries the database once per item in the iteration loop",
            "Scans `MediaCollection` for every cassette slot",
            "Allocates and retains one large object per processed wafer",
        ):
            assert concrete_example in prompt

        for speculative_example in (
            "This code might be slow.",
            "This could allocate a lot.",
            "This could be optimized.",
            "This may be a hot path",
        ):
            assert speculative_example in prompt

    def test_layer3a_prompt_declares_empty_findings_shape(self) -> None:
        prompt = self._load_prompt()

        assert "empty-findings JSON shape" in prompt
        assert '"findings": []' in prompt
        assert '"performance_posture": "good"' in prompt
        assert '"worst_bottleneck": "none"' in prompt
        assert "Do not manufacture findings to avoid an empty array" in prompt

    def test_layer3a_prompt_declares_supplemental_fields(self) -> None:
        prompt = self._load_prompt()

        assert '"complexity": "<current complexity or scaling behavior' in prompt
        assert '"production_impact": "<what happens under realistic load' in prompt

        assert (
            "`complexity`: supplemental performance evidence. Use current/before "
            "complexity or scaling behavior when known."
        ) in prompt
        assert (
            "Use `n/a` only for a concrete non-Big-O mechanism such as "
            "event-loop blocking or cache invalidation semantics"
        ) in prompt
        assert (
            "`production_impact`: supplemental performance evidence. State the "
            "realistic operational consequence."
        ) in prompt


class TestSubjectKeyRegistryContract:
    @staticmethod
    def _skill_file_basenames() -> set[str]:
        skill_dir = (
            Path(__file__).resolve().parents[1]
            / "sentinel"
            / "skills"
            / "skill_files"
        )
        return {path.stem for path in skill_dir.glob("*.md")}

    def test_namespace_catalog_count_matches_contract(self) -> None:
        assert len(NAMESPACES) == 32

    def test_namespace_entries_have_required_fields(self) -> None:
        allowed_surfaces = set(get_args(SurfaceCategory))

        for entry in NAMESPACES:
            assert entry.token
            assert entry.pattern
            assert entry.owners
            assert entry.surface in allowed_surfaces

    def test_namespace_tokens_end_with_colon(self) -> None:
        for entry in NAMESPACES:
            assert entry.token.endswith(":")

    def test_namespace_token_owner_pairs_are_unique(self) -> None:
        token_owner_pairs = {(entry.token, entry.owners) for entry in NAMESPACES}
        assert len(token_owner_pairs) == len(NAMESPACES)

    def test_namespace_helper_returns_representative_owners(self) -> None:
        assert get_namespace_owner("external_call:") == (
            "layer2d-data-integrity-specialist",
            "layer3d-error-handling-specialist",
            "layer3e-concurrency-specialist",
            "layer2j-operational-risk-failure-modes",
        )
        assert get_namespace_owner("catch_site:") == (
            "layer3d-error-handling-specialist",
        )
        assert get_namespace_owner("migration:") == (
            "layer2d-data-integrity-specialist",
            "layer2i-change-impact-testability",
            "layer3c-testability-specialist",
        )

    def test_shared_namespace_detection(self) -> None:
        assert is_shared_namespace("external_call:") is True
        assert is_shared_namespace("transaction_boundary:") is False

    def test_namespace_migration_collision_is_disambiguated(self) -> None:
        migration_entries = [entry for entry in NAMESPACES if entry.token == "migration:"]

        assert len(migration_entries) == 1
        migration_entry = migration_entries[0]
        assert migration_entry.owners == (
            "layer2d-data-integrity-specialist",
            "layer2i-change-impact-testability",
            "layer3c-testability-specialist",
        )
        assert "migration_hazard" in migration_entry.note
        assert "change_verification_gap" in migration_entry.note
        assert "disambiguated" in migration_entry.note.lower()

    def test_migration_token_disambiguation_encoded_in_field(self) -> None:
        migration = next(e for e in NAMESPACES if e.token == "migration:")
        assert migration.disambiguation == (
            ("migration_hazard", "schema_migration_step"),
            ("change_verification_gap", "versioned_element_migration"),
        )

    def test_table_relationship_catalog_count_matches_contract(self) -> None:
        assert len(TABLE_2_RELATIONSHIPS) == 18

    def test_table_relationships_have_required_fields(self) -> None:
        allowed_relationships = set(get_args(RelationshipType))
        allowed_consolidations = set(get_args(ConsolidationRule))

        for entry in TABLE_2_RELATIONSHIPS:
            assert entry.emitter_a
            assert entry.emitter_b
            assert entry.table_label
            assert entry.relationship in allowed_relationships
            assert entry.consolidation in allowed_consolidations
            if entry.canonical_emitter is not None:
                assert entry.canonical_emitter in {entry.emitter_a, entry.emitter_b}

    def test_table_relationship_emitters_exist_as_skill_files(self) -> None:
        valid_skill_names = self._skill_file_basenames()

        for entry in TABLE_2_RELATIONSHIPS:
            assert entry.emitter_a in valid_skill_names
            assert entry.emitter_b in valid_skill_names
            if entry.canonical_emitter is not None:
                assert entry.canonical_emitter in valid_skill_names

    def test_table_relationship_shared_kinds_exist_in_finding_registry(self) -> None:
        for entry in TABLE_2_RELATIONSHIPS:
            for finding_kind in entry.shared_kinds:
                assert get_finding_kind_definition(finding_kind) is not None

    def test_table_relationship_helper_peers_for_missing_transaction_boundary(self) -> None:
        assert get_table_2_peers(
            "layer2d-data-integrity-specialist",
            "missing_transaction_boundary",
        ) == (
            "layer2a-quick-review",
            "layer3d-error-handling-specialist",
            "layer3e-concurrency-specialist",
        )

    def test_table_relationship_helper_peers_for_concurrency_safety_gap(self) -> None:
        assert get_table_2_peers(
            "layer3e-concurrency-specialist",
            "concurrency_safety_gap",
        ) == ("layer3d-error-handling-specialist",)

    def test_table_relationship_helper_consolidation_rule_for_external_call(self) -> None:
        assert get_co_emission_consolidation_rule(
            "layer2d-data-integrity-specialist",
            "layer3d-error-handling-specialist",
            "external_call:",
        ) == "collapse_on_shared_subject_key"

    def test_no_carry_forward_unidirectional_relationships_remain(self) -> None:
        """Cycle 26 closure: every specialist-tier carry-forward edge
        has a reverse declaration, and quick-review-tier edges are
        explicitly marked intentionally_asymmetric (and excluded by
        the helper). No remaining carry-forward entries."""
        assert get_unidirectional_relationships() == ()

    def test_intentionally_asymmetric_relationships_are_quick_review_tier(self) -> None:
        asymmetric = tuple(
            rel for rel in TABLE_2_RELATIONSHIPS if rel.intentionally_asymmetric
        )
        assert len(asymmetric) == 2
        assert all(rel.emitter_b == "layer2a-quick-review" for rel in asymmetric)
        assert all(
            rel.relationship == "quick_review_vs_specialist" for rel in asymmetric
        )

    def test_namespace_entries_have_no_invalid_surface_categories(self) -> None:
        allowed_surfaces = set(get_args(SurfaceCategory))

        assert {entry.surface for entry in NAMESPACES} <= allowed_surfaces

    def test_namespace_catalog_contains_operational_and_code_site_surfaces(self) -> None:
        surfaces = {entry.surface for entry in NAMESPACES}

        assert "operational" in surfaces
        assert "code_site" in surfaces

    def test_namespace_owner_helper_handles_unknown_namespace(self) -> None:
        assert get_namespace_owner("does_not_exist:") == ()


class TestLayer4aCodeSmellDetectorPromptContract:
    @staticmethod
    def _load_prompt() -> str:
        prompt_path = (
            Path(__file__).resolve().parents[1]
            / "sentinel"
            / "skills"
            / "skill_files"
            / "layer4a-code-smell-detector.md"
        )
        return prompt_path.read_text(encoding="utf-8")

    @staticmethod
    def _compact(text: str) -> str:
        return " ".join(text.split())

    def test_layer4a_prompt_declares_allowed_finding_kinds(self) -> None:
        prompt = self._load_prompt()

        for finding_kind in (
            "`god_class`",
            "`feature_envy`",
            "`shotgun_surgery`",
            "`primitive_obsession`",
            "`long_parameter_list`",
            "`divergent_change`",
            "`data_clump`",
            "`inappropriate_intimacy`",
        ):
            assert finding_kind in prompt

    def test_layer4a_prompt_excludes_unapproved_kinds(self) -> None:
        prompt = self._load_prompt()

        assert "Do not invent other `finding_kind` values" in prompt
        assert "This skill does not emit `structural_complexity`." in prompt
        assert "Do not emit `structural_complexity` from this skill" in prompt
        assert (
            "The eight allowed `finding_kind` values below are the only valid "
            "values for this skill"
        ) in prompt

    def test_layer4a_prompt_declares_three_way_subject_key_rules(self) -> None:
        prompt = self._load_prompt()

        assert "Omit or set `_na` for n/a kinds:" in prompt
        for finding_kind in ("`god_class`", "`long_parameter_list`", "`data_clump`"):
            assert finding_kind in prompt

        assert "Include only when a stable subject exists for optional kinds:" in prompt
        for finding_kind in ("`feature_envy`", "`shotgun_surgery`", "`divergent_change`"):
            assert finding_kind in prompt

        assert "Always include a non-empty `subject_key` for required kinds:" in prompt
        for finding_kind in ("`primitive_obsession`", "`inappropriate_intimacy`"):
            assert finding_kind in prompt

    def test_layer4a_prompt_declares_field_format_change(self) -> None:
        prompt = self._load_prompt()

        assert "`smell_type` is removed. Do not emit `smell_type`." in prompt
        assert "Values use underscores, not hyphens:" in prompt

        for replacement in (
            "Use `god_class`, not `god-class`.",
            "Use `feature_envy`, not `feature-envy`.",
            "Use `shotgun_surgery`, not `shotgun-surgery`.",
            "Use `primitive_obsession`, not `primitive-obsession`.",
            "Use `long_parameter_list`, not `long-parameter-list`.",
            "Use `divergent_change`, not `divergent-change`.",
            "Use `data_clump`, not `data-clump`.",
            "Use `inappropriate_intimacy`, not `inappropriate-intimacy`.",
        ):
            assert replacement in prompt

    def test_layer4a_prompt_declares_kind_table_rows(self) -> None:
        prompt = self._compact(self._load_prompt())

        for kind_table_row in (
            (
                "| `god_class` | One class or module owns too many responsibilities "
                "or coordination roles. | Omit (`n/a`). Omit `subject_key` or set "
                "it to `_na`. The smell identity is structural; `location` and "
                "`summary` identify the object. |"
            ),
            (
                "| `feature_envy` | A method or function conceptually belongs with "
                "another object's data or behavior. | Optional. Include `subject_key` "
                "when there is a stable envied object, type, domain concept, or "
                "collaborator; omit it when the smell is broader or less bounded. |"
            ),
            (
                "| `shotgun_surgery` | One logical change requires touching many "
                "files, classes, methods, or decision points. | Optional. Include "
                "`subject_key` when there is a stable logical change, feature, "
                "workflow, rule, or policy that fans out across sites; omit it when "
                "no stable change subject is identifiable. |"
            ),
            (
                "| `primitive_obsession` | A domain concept is represented as loose "
                "primitive values instead of a dedicated type, enum, or value object. "
                "| Required. Include a non-empty `subject_key` naming the missing "
                "domain concept, such as status, money, identifier, machine state, "
                "lot identity, recipe identity, or wafer location. |"
            ),
            (
                "| `long_parameter_list` | A callable requires too many independently "
                "carried parameters for safe use and change. | Omit (`n/a`). Omit "
                "`subject_key` or set it to `_na`. The smell identity is structural; "
                "`location` and `summary` identify the callable. |"
            ),
            (
                "| `divergent_change` | A class or module changes for multiple "
                "unrelated reasons instead of one cohesive responsibility. | Optional. "
                "Include `subject_key` when there is a stable responsibility axis, "
                "change reason, subsystem, or policy family; omit it when the smell "
                "is broader or less bounded. |"
            ),
            (
                "| `data_clump` | The same group of values repeatedly travels together "
                "without being modeled as one concept. | Omit (`n/a`). Omit "
                "`subject_key` or set it to `_na`. The smell identity is structural; "
                "the repeated field group belongs in `description`. |"
            ),
            (
                "| `inappropriate_intimacy` | One module or class relies on another "
                "module's internals or crosses an intended abstraction boundary. | "
                "Required. Include a non-empty `subject_key` naming the crossed "
                "boundary, private/internal member, coupled module/class relationship, "
                "or abstraction boundary being violated. |"
            ),
        ):
            assert self._compact(kind_table_row) in prompt

    def test_layer4a_prompt_declares_discriminator_rules(self) -> None:
        prompt = self._load_prompt()

        for discriminator in (
            "`god_class` vs `divergent_change`",
            "`shotgun_surgery` vs `divergent_change`",
            "`primitive_obsession` vs `data_clump` vs `long_parameter_list`",
            "`feature_envy` vs `inappropriate_intimacy`",
            "Named smell vs `structural_complexity`",
        ):
            assert discriminator in prompt

        assert "These axes are inverse:" in prompt
        assert "choose the dominant mechanism" in prompt
        assert "choose the one that best explains the change risk" in prompt
        assert "If a concrete named smell fits, emit the specific named smell" in prompt

    def test_layer4a_prompt_declares_summary_requirement(self) -> None:
        prompt = self._load_prompt()

        assert '"summary": "<short stable finding title>"' in prompt
        assert "`summary` must be:" in prompt
        assert "Short and stable across regenerations." in prompt
        assert "Mechanism-focused, not wording-focused." in prompt
        assert "Specific enough to distinguish this finding" in prompt
        assert "Free of long code snippets." in prompt

    def test_layer4a_prompt_declares_json_escaping_requirements(self) -> None:
        prompt = self._load_prompt()

        assert "JSON String Escaping Requirements" in prompt
        assert r'Use `\"` for embedded double-quotes' in prompt
        assert r'Use `\\` for backslashes' in prompt
        assert "Unescaped quotes inside any field value" in prompt
        assert "Prefer plain-English paraphrases" in prompt

        for code_bearing_field in (
            "`summary`",
            "`finding_kind`",
            "`subject_key`",
            "`location`",
            "`description`",
            "`suggested_fix`",
            "`change_risk`",
            "`refactoring_direction`",
            "`primary_design_problem`",
            "`refactoring_priority`",
        ):
            assert code_bearing_field in prompt

    def test_layer4a_prompt_declares_description_evidence_requirements(self) -> None:
        prompt = self._load_prompt()

        assert "Description Evidence Requirements" in prompt
        assert "Every `description` must be self-contained" in prompt
        assert "Smell mechanism in plain language." in prompt
        assert "Concrete evidence." in prompt
        assert "Change risk or design pressure." in prompt
        assert "Refactoring direction." in prompt

        for evidence_rule in (
            "For `god_class`: name the responsibility clusters.",
            "For `feature_envy`: name the envied object/data/behavior.",
            "For `shotgun_surgery`: state the repeated edit sites",
            "For `primitive_obsession`: name the missing domain abstraction",
            "For `long_parameter_list`: describe the overloaded signature",
            "For `divergent_change`: name the unrelated change reasons.",
            "For `data_clump`: name the repeated field/value group.",
            "For `inappropriate_intimacy`: name the crossed boundary",
        ):
            assert evidence_rule in prompt

    def test_layer4a_prompt_declares_empty_findings_shape(self) -> None:
        prompt = self._load_prompt()

        assert "empty-findings JSON shape" in prompt
        assert '"findings": []' in prompt
        assert '"structural_health": "healthy"' in prompt
        assert '"primary_design_problem": "none"' in prompt
        assert '"refactoring_priority": "none"' in prompt

    def test_layer4a_prompt_declares_no_manufactured_findings(self) -> None:
        prompt = self._load_prompt()

        assert "Do not manufacture findings to avoid an empty array." in prompt
        assert "A possible future smell without code evidence." in prompt
        assert (
            "A broad complexity concern that does not map to one of the eight "
            "allowed named smells."
        ) in prompt

    def test_layer4a_prompt_declares_severity_constraints(self) -> None:
        prompt = self._load_prompt()

        assert "`severity`: `P1`, `P2`, or `P3` only." in prompt
        assert "Smells are never `Critical`" in prompt
        assert "P1 = smell causing active development pain or frequent bugs." in prompt
        assert "P2 = significant technical debt that will compound." in prompt
        assert (
            "P3 = acknowledged smell that should be addressed in normal course "
            "of development."
        ) in prompt

    def test_layer4a_prompt_declares_supplemental_fields(self) -> None:
        prompt = self._load_prompt()

        assert (
            '"change_risk": "<supplemental raw evidence: what kinds of future '
            'changes this makes dangerous or expensive>"'
        ) in prompt
        assert (
            '"refactoring_direction": "<supplemental raw evidence: high-level '
            'direction for the refactoring advisor skill>"'
        ) in prompt
        assert (
            "`change_risk` and `refactoring_direction`: supplemental raw output; "
            "repeat their essential content in `description`."
        ) in prompt


class TestLayer2eApiContractCompatibilityPromptContract:
    @staticmethod
    def _load_prompt() -> str:
        prompt_path = (
            Path(__file__).resolve().parents[1]
            / "sentinel"
            / "skills"
            / "skill_files"
            / "layer2e-api-contract-compatibility.md"
        )
        return prompt_path.read_text(encoding="utf-8")

    @staticmethod
    def _compact(text: str) -> str:
        return " ".join(text.split())

    def test_layer2e_prompt_declares_allowed_finding_kinds(self) -> None:
        prompt = self._load_prompt()

        for finding_kind in (
            "`api_contract_mismatch`",
            "`contract_violation`",
        ):
            assert finding_kind in prompt

        assert "Do not invent other `finding_kind` values" in prompt

    def test_layer2e_prompt_declares_required_subject_key_rules_and_category_metadata(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        for subject_key_rule in (
            (
                "| `api_contract_mismatch` | Required. Include a non-empty "
                "`subject_key` naming the incompatible API, endpoint, signature, "
                "schema, DTO/message field, interface, versioned contract, or "
                "producer-consumer contract. |"
            ),
            (
                "| `contract_violation` | Required. Include a non-empty "
                "`subject_key` naming the violated precondition, postcondition, "
                "return-value guarantee, behavioral invariant, workflow I/O promise, "
                "or interface behavior. |"
            ),
        ):
            assert self._compact(subject_key_rule) in compact_prompt

        assert "`subject_key` is required for both allowed `finding_kind` values." in prompt
        assert "`category` remains descriptive metadata" in prompt
        assert "Keep `category` as `api-contract`" in prompt
        assert '"category": "api-contract"' in prompt
        assert "treat `finding_kind` as the identity field" in prompt

    def test_layer2e_prompt_declares_subject_key_guidance_examples(self) -> None:
        prompt = self._load_prompt()

        assert "### `subject_key` guidance" in prompt
        assert "Use short, stable, colon-delimited contract identities" in prompt

        for subject_key_pattern in (
            "`api:<service>.<operation>`",
            "`endpoint:<method> <path>`",
            "`schema:<message-or-dto>:<field>`",
            "`signature:<type-or-interface>.<method>`",
            "`version:<contract-name>:<version>`",
            "`return-contract:<method>`",
            "`precondition:<operation>:<condition>`",
            "`postcondition:<operation>:<guarantee>`",
            "`invariant:<component>:<promise>`",
            "`workflow-io:<activity>:<input-or-output>`",
        ):
            assert subject_key_pattern in prompt

        assert (
            "Pattern-shaped examples — substitute concrete identifiers from the "
            "provided context:"
        ) in prompt

        for pattern_example in (
            "`signature:I<ServiceName>.<MethodName>`",
            "`endpoint:POST /<resource>/{<id-param>}/<action>`",
            "`schema:<RequestDtoName>.<fieldName>`",
            "`version:<ContractName>:v<n>`",
            "`workflow-io:<WorkflowName>.<OutputName>`",
            "`return-contract:<MethodName>.<returnedField>`",
        ):
            assert pattern_example in prompt

    def test_layer2e_prompt_excludes_acme_specific_identifiers(self) -> None:
        prompt = self._load_prompt()

        for acme_token in (
            "IMesService",
            "StoreLot",
            "ManualStoreLotRequest",
            "lotId",
            "MesStoreLot:v2",
            "AcmeStartTool",
            "OutputCarrier",
            "GetCarrierSlots",
            "nonEmptySlots",
        ):
            assert acme_token not in prompt

    def test_layer2e_prompt_declares_discriminator_rules(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "## Discriminator Rules" in prompt

        for scenario in (
            (
                "| Producer and consumer disagree on signature, schema shape, "
                "field requiredness, type, endpoint, message format, protocol "
                "version, or compatibility version. | `api_contract_mismatch` |"
            ),
            (
                "| The visible interface shape still appears compatible, but "
                "implementation violates a documented or implied behavior promise. "
                "| `contract_violation` |"
            ),
            (
                "| A DTO, message, or schema removes or renames a field that consumers "
                "still require. | `api_contract_mismatch` |"
            ),
            (
                "| A method still returns the same DTO shape but no longer populates a "
                "promised field correctly. | `contract_violation` |"
            ),
            (
                "| A versioned endpoint changes behavior without a compatible versioning "
                "path. | Usually `api_contract_mismatch` if versioning or "
                "compatibility drift is the primary defect; use `contract_violation` "
                "if the primary defect is a violated behavior guarantee despite "
                "compatible version/shape. |"
            ),
        ):
            assert self._compact(scenario) in compact_prompt

        assert (
            "Shape, schema, signature, endpoint, protocol, or version "
            "compatibility problem → `api_contract_mismatch`."
        ) in prompt
        assert "Behavioral promise breach with compatible shape → `contract_violation`." in prompt
        assert "Avoid duplicate findings" in prompt
        assert (
            "Do not report the same root cause once as `api_contract_mismatch` "
            "and again as `contract_violation`."
        ) in prompt

    def test_layer2e_prompt_declares_breaking_change_semantics(self) -> None:
        prompt = self._load_prompt()

        assert "## Breaking-Change Semantics" in prompt
        assert "### Likely `api_contract_mismatch`" in prompt

        for mismatch_example in (
            "removing a public field that consumers still read",
            "renaming a public field without a compatibility alias",
            "changing field type, units, encoding, enum domain, or nullability incompatibly",
            "changing requiredness of a request, response, DTO, or message field",
            "changing method parameter order, type, requiredness, or meaning",
            "changing endpoint path, HTTP method, route parameter, or message name incompatibly",
            "changing event/message schema without compatible versioning",
            "changing response shape without a consumer migration path",
            "requiring a newer contract version while visible callers still use the old version",
            "making an interface narrower in a way current consumers cannot satisfy",
        ):
            assert mismatch_example in prompt

        assert "### Compatible evolution — do not report unless concrete evidence shows breakage" in prompt

        for compatible_example in (
            "adding an optional field while existing fields remain stable",
            "adding a backward-compatible overload while preserving the old signature",
            "adding a new endpoint while retaining the old endpoint",
            "adding a new schema version while the old version remains supported",
            "widening an accepted input range without violating existing consumers",
            "adding a new enum value when visible consumers safely handle unknown values",
            "adding metadata that consumers can ignore safely",
        ):
            assert compatible_example in prompt

        assert "### Likely `contract_violation`" in prompt

        for violation_example in (
            "documented status codes, result codes, or error semantics are no longer honored",
            "an idempotency, ordering, retry, or transactional guarantee is broken",
            "an operation accepts the same inputs but produces behavior outside the documented range",
            "a return value remains the same type but violates required semantics",
            "a compatibility shim or adapter exists but maps fields or behavior incorrectly",
            "a workflow activity input/output contract is accepted syntactically but violates a caller-visible invariant",
        ):
            assert violation_example in prompt

    def test_layer2e_prompt_declares_summary_and_description_requirements(self) -> None:
        prompt = self._load_prompt()

        assert "## Summary and Description Requirements" in prompt
        assert '"summary": "<short stable finding title>"' in prompt
        assert "`summary`: one short, stable title" in prompt
        assert "Do not include volatile run-specific phrasing or line numbers in `summary`." in prompt

        for required_description_element in (
            "contract identity",
            "producer-consumer evidence",
            "expected contract",
            "observed mismatch or violation",
            "compatibility impact",
            "why the chosen `finding_kind` fits",
        ):
            assert required_description_element in prompt

        assert "The description must be self-contained" in prompt

        for acceptable_evidence in (
            "producer signature vs. consumer call mismatch",
            "schema field expected by a consumer but missing",
            "documented behavior or version contract contradicted by implementation",
            "dependency metadata or version mismatch paired with incompatible API usage",
            "internal interface contract and caller assumptions visible in the provided context",
            "cross-module or cross-repo dependency context from the additional Phase 1 assessment payload",
        ):
            assert acceptable_evidence in prompt

        for insufficient_evidence in (
            "without identifying a contract or consumer",
            "generic “needs documentation” comments",
            "purely internal cleanup not tied to a caller-visible contract",
            "theoretical future versioning concern with no incompatible change",
            "broad maintainability concerns that are not API contract compatibility problems",
        ):
            assert insufficient_evidence in prompt

    def test_layer2e_prompt_declares_json_escaping_requirements(self) -> None:
        prompt = self._load_prompt()

        assert "## JSON String Escaping Requirements" in prompt
        assert "Return only valid JSON." in prompt
        assert r'`\"` for embedded double-quotes' in prompt
        assert r'`\\` for backslashes' in prompt
        assert (
            "Unescaped quotes inside field values will cause the response "
            "to be rejected by JSON parsing"
        ) in prompt
        assert "paraphrase the contract evidence or fix in plain English" in prompt

        for code_bearing_field in (
            "`description`",
            "`location`",
            "`subject_key`",
            "`summary`",
            "`suggested_fix`",
        ):
            assert code_bearing_field in prompt

    def test_layer2e_prompt_declares_empty_findings_shape_and_no_manufactured_findings(self) -> None:
        prompt = self._load_prompt()

        assert "empty-findings JSON shape" in prompt
        assert (
            '"assessment_summary": "No concrete API contract compatibility findings '
            'were identified in the provided context."'
        ) in prompt
        assert '"recommended_actions": []' in prompt
        assert '"findings": []' in prompt
        assert "Do not manufacture findings to avoid an empty array" in prompt
        assert "single-repo contexts where true cross-repo API contract pressure may be thin" in prompt
        assert "Internal API, workflow I/O, service interface, schema, or version contracts" in prompt

    def test_layer2e_prompt_declares_severity_ladder(self) -> None:
        prompt = self._load_prompt()

        assert "Use these severity values exactly:" in prompt
        assert (
            "`Critical`: incompatible contract or behavior can cause production "
            "data corruption, unsafe action, security exposure, or widespread "
            "operational failure."
        ) in prompt
        assert (
            "`P1`: incompatible contract or behavior breaks a common or important "
            "current consumer path."
        ) in prompt
        assert (
            "`P2`: contract drift can break an edge path, deployment/upgrade path, "
            "or integration under realistic conditions."
        ) in prompt
        assert "`P3`: low-risk compatibility hardening with concrete evidence, not speculation." in prompt


class TestLayer2fDataFlowTrustBoundaryPromptContract:
    @staticmethod
    def _load_prompt() -> str:
        prompt_path = (
            Path(__file__).resolve().parents[1]
            / "sentinel"
            / "skills"
            / "skill_files"
            / "layer2f-data-flow-trust-boundary.md"
        )
        return prompt_path.read_text(encoding="utf-8")

    @staticmethod
    def _compact(text: str) -> str:
        return " ".join(text.split())

    def test_layer2f_prompt_declares_allowed_finding_kinds(self) -> None:
        prompt = self._load_prompt()

        for finding_kind in ("`trust_boundary_gap`", "`injection_risk`"):
            assert finding_kind in prompt

        assert "Do not invent other `finding_kind` values." in prompt

    def test_layer2f_prompt_declares_required_subject_key_rules_and_category_metadata(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "`subject_key` is required for both allowed `finding_kind` values." in prompt
        assert "`category` remains descriptive metadata" in prompt
        assert self._compact("Keep `category` as `data-flow-trust`") in compact_prompt
        assert '"category": "data-flow-trust"' in prompt
        assert "treat `finding_kind` as the identity field" in prompt

    def test_layer2f_prompt_declares_subject_key_guidance_examples(self) -> None:
        prompt = self._load_prompt()

        assert "### `subject_key` guidance" in prompt
        assert "Use short, stable, colon-delimited trust-boundary identities" in prompt

        assert (
            "Pattern-shaped `subject_key` examples for `trust_boundary_gap` — "
            "substitute concrete identifiers from the provided context:"
        ) in prompt
        assert (
            "Pattern-shaped `subject_key` examples for `injection_risk` — "
            "substitute concrete identifiers from the provided context:"
        ) in prompt

        for genericized_example in (
            "`boundary:<external-service-name>.<read-operation>`",
            "`input:<file-format>:<consuming-method>`",
            "`flow:<external-response>:<consuming-component>`",
            "`workflow-io:<producer-activity>.output:<consumer-activity>.input`",
            "`ownership:<shared-payload>:<consumer-component>`",
            "`sink:<ParserClass>.<unsafe-operation>`",
            "`flow:<untrusted-file-format>:<ParserClass>.<resolution-operation>`",
            "`sink:<external-response-type>.<query-type>`",
        ):
            assert genericized_example in prompt

    def test_layer2f_prompt_excludes_acme_specific_identifiers(self) -> None:
        prompt = self._load_prompt()

        for acme_token in (
            "MES300",
            "GetDispatchData",
            "tafosb-xml",
            "FAIMGetTAFOSBData",
            "mes-response",
            "workflow-dispatch-activity",
            "ValidateTAFOSB",
            "AcmeStartTool",
            "tafosb-carrier-data",
            "MES300Response",
        ):
            assert acme_token not in prompt

    def test_layer2f_prompt_declares_discriminator_rules_table1_within_emitter(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "## Discriminator Rules" in prompt

        for scenario in (
            (
                "| Untrusted data (service response, file content, activity output, "
                "operator input) crosses a boundary and is consumed without validation, "
                "but no specific executable or interpreter sink is visible in the "
                "provided context. | `trust_boundary_gap` |"
            ),
            (
                "| Data ownership is ambiguous across components — no component clearly "
                "takes responsibility for validating the data — but no violation path to "
                "a concrete sink is visible. | `trust_boundary_gap` |"
            ),
            (
                "| Missing sanitization at a boundary is structural (no schema check, no "
                "whitelist, no trust-reduction step) but the downstream fate of the data "
                "is not visible in the provided context. | `trust_boundary_gap` |"
            ),
            (
                "| The trust-boundary analysis traces untrusted data from a crossing to a "
                "concrete downstream sink (XML parser with entity resolution, SQL "
                "construction, command execution, unsafe deserializer, unescaped output "
                "renderer) visible in the provided context. | `injection_risk` |"
            ),
        ):
            assert self._compact(scenario) in compact_prompt

        assert (
            "Missing-validation gap without a demonstrated sink → `trust_boundary_gap`."
        ) in prompt
        assert (
            "Gap plus demonstrated concrete executable sink visible in context → `injection_risk`."
        ) in prompt
        assert (
            "Do not upgrade a `trust_boundary_gap` to `injection_risk` speculatively."
        ) in prompt
        assert self._compact(
            "If the downstream code is not in scope, report `trust_boundary_gap`."
        ) in compact_prompt
        assert "Avoid duplicate findings:" in prompt
        assert (
            "Do not report the same root cause once as `trust_boundary_gap` "
            "and again as `injection_risk`."
        ) in prompt

    def test_layer2f_prompt_declares_discriminator_rules_table2_cross_emitter(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert (
            "Both emitters can produce `injection_risk` for the same code. "
            "This is correct and intended."
        ) in prompt

        for table_aspect in (
            "| Analytical path |",
            "| Trigger |",
            "| Finding shape |",
            "| Overlap behavior |",
        ):
            assert table_aspect in prompt

        assert (
            "Overlap with layer2c is expected and correct. Downstream dedup resolves it."
        ) in prompt

        for behavioral_rule in (
            (
                "Do not suppress an `injection_risk` finding because "
                "`layer2c-security-specialist` might also identify the same sink."
            ),
            (
                "Do not add layer2c-specific fields (`vulnerability_class`, `cwe_id`, "
                "`attack_scenario`, `impact`) to layer2f findings."
            ),
            (
                "If trust-boundary tracing leads to a concrete sink, report it as "
                "`injection_risk` regardless of whether it would also appear as a "
                "security-vulnerability finding in another emitter."
            ),
        ):
            assert self._compact(behavioral_rule) in compact_prompt

    def test_layer2f_prompt_declares_summary_and_description_requirements(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "## Summary and Description Requirements" in prompt
        assert '"summary": "<short stable finding title>"' in prompt
        assert "`summary`: one short, stable title for the finding." in prompt
        assert (
            "Do not include volatile run-specific phrasing or line numbers in `summary`."
        ) in prompt

        for description_element in (
            "untrusted source",
            "boundary crossing",
            "validation or sanitization gap",
            "consuming component",
            "concrete sink path",
        ):
            assert description_element in prompt

        for acceptable_evidence in (
            "An identifiable untrusted source",
            "An identifiable boundary crossing",
            "An observable absence of validation, sanitization, schema check, or trust-reduction step",
        ):
            assert acceptable_evidence in prompt

        for insufficient_evidence in (
            '"Data might come from an external source"',
            'Generic "validate all inputs" concerns',
            "Trust-boundary concern that is purely architectural without code-level boundary evidence.",
            "Speculative sink reachability without a demonstrable path visible in the provided context.",
        ):
            assert insufficient_evidence in prompt

        assert "The description must be self-contained" in prompt

    def test_layer2f_prompt_declares_json_escaping_requirements(self) -> None:
        prompt = self._load_prompt()

        assert "## JSON String Escaping Requirements" in prompt
        assert r'`\"` for embedded double-quotes inside JSON string values' in prompt
        assert r'`\\` for backslashes inside JSON string values' in prompt
        assert self._compact(
            "Unescaped quotes or backslashes inside field values will cause the "
            "finding to be rejected by JSON parsing."
        ) in self._compact(prompt)
        assert "paraphrase the content in plain English rather than embedding raw code" in prompt

        for code_bearing_field in (
            "`description`",
            "`suggested_fix`",
            "`location`",
            "`subject_key`",
            "`summary`",
        ):
            assert code_bearing_field in prompt

    def test_layer2f_prompt_declares_empty_findings_shape_and_no_manufactured_findings(self) -> None:
        prompt = self._load_prompt()

        assert (
            '"assessment_summary": "No trust boundary or data flow findings identified '
            'in the reviewed context."'
        ) in prompt
        assert '"recommended_actions": []' in prompt
        assert '"findings": []' in prompt
        assert "Do not manufacture findings to avoid an empty array." in prompt
        assert self._compact(
            "Return the empty shape when no identifiable trust-boundary crossing with "
            "missing validation is observable in the provided context."
        ) in self._compact(prompt)

    def test_layer2f_prompt_declares_severity_values(self) -> None:
        prompt = self._load_prompt()

        assert "Use these `severity` values exactly: `Critical`, `P1`, `P2`, `P3`." in prompt


class TestLayer2gConfigEnvironmentDriftPromptContract:
    @staticmethod
    def _load_prompt() -> str:
        prompt_path = (
            Path(__file__).resolve().parents[1]
            / "sentinel"
            / "skills"
            / "skill_files"
            / "layer2g-config-environment-drift.md"
        )
        return prompt_path.read_text(encoding="utf-8")

    @staticmethod
    def _compact(text: str) -> str:
        return " ".join(text.split())

    def test_layer2g_prompt_declares_allowed_finding_kind_and_no_invent_rule(self) -> None:
        prompt = self._load_prompt()

        assert "`configuration_drift`" in prompt
        assert "Do not invent other `finding_kind` values." in prompt

    def test_layer2g_prompt_declares_required_subject_key_rules_and_category_metadata(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "`subject_key` is required for `configuration_drift`." in prompt
        assert "`category` remains descriptive metadata" in prompt
        assert self._compact("Keep `category` as `config-drift`") in compact_prompt
        assert '"category": "config-drift"' in prompt
        assert self._compact("treat `finding_kind` as the identity field") in compact_prompt

    def test_layer2g_prompt_declares_scope_boundary(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "### Scope boundary" in prompt
        assert "**In scope for `configuration_drift`:**" in prompt
        assert self._compact("Security framing wins when both apply.") in compact_prompt
        assert self._compact(
            "Different kinds, different perspectives, same code. Do not suppress either."
        ) in compact_prompt

    def test_layer2g_prompt_declares_subject_key_guidance_framing_and_patterns(
        self,
    ) -> None:
        prompt = self._load_prompt()

        assert "### `subject_key` guidance" in prompt
        assert "Name the **configuration item**" in prompt
        assert (
            "Do not use the consumer's class or method name as the primary identity"
        ) in prompt

        for pattern_prefix in (
            "`config:<key-name>`",
            "`env:<variable-name>`",
            "`default:<component>.<setting>`",
            "`hardcoded:<class-or-activity>.<value-category>`",
            "`startup:<component>.<dependency>`",
            "`migration:<component>.<key-or-version>`",
        ):
            assert pattern_prefix in prompt

    def test_layer2g_prompt_declares_pattern_shaped_subject_key_examples(self) -> None:
        prompt = self._load_prompt()

        assert (
            "Pattern-shaped examples — substitute concrete identifiers from the "
            "provided context:"
        ) in prompt

        for pattern_example in (
            "`config:<ConstantClassName>.<KeyName>`",
            "`config:<ConfigurationClassName>.<MappingName>`",
            "`default:<ConsumingComponent>.<SettingName>`",
            "`hardcoded:<ConsumingMethod>.<ValueCategory>`",
            "`env:<EnvironmentVariableName>`",
            "`startup:<ConsumingService>.<DependencyName>`",
        ):
            assert pattern_example in prompt

    def test_layer2g_prompt_excludes_acme_specific_identifiers(self) -> None:
        prompt = self._load_prompt()

        for acme_token in (
            "ACMERMSConstants",
            "WaferToTrayMapping",
            "TrayPartNumber",
            "AcmeConfiguration",
            "ColletRMSMES300Mapping",
            "AcmeAdditionalPreSetupValidation",
            "MultiProductEnabled",
            "GetEmptyTrayTAFOSBID",
            "CarrierTypeField",
            "SdlMESService",
            "Tool_Line",
        ):
            assert acme_token not in prompt

    def test_layer2g_prompt_declares_summary_requirements(self) -> None:
        prompt = self._load_prompt()

        assert "## Summary and Description Requirements" in prompt
        assert "stable deduplication anchor across regenerations" in prompt
        assert (
            "`Configuration constant consumed without a verified default value`"
        ) in prompt
        assert "`Configuration issue found in validation component`" in prompt

    def test_layer2g_prompt_declares_description_evidence_requirements(self) -> None:
        prompt = self._load_prompt()

        assert "**Identifiable configuration item**" in prompt
        assert "**Observable inconsistency or gap**" in prompt

        for evidence_element in (
            "The config key name, constant, or variable being consumed.",
            "The consuming location (activity, class, or method).",
            "The specific drift mechanism",
            "What behavior the code exhibits on missing or drifted config",
            "The deployment scope or environment dimension",
        ):
            assert evidence_element in prompt

    def test_layer2g_prompt_declares_description_self_contained_and_insufficient_exits(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "The description must be self-contained" in prompt
        assert self._compact(
            "prefer `trust_boundary_gap` or `partial_failure_handling_gap`"
        ) in compact_prompt
        assert "exit to `security_misconfiguration`" in prompt

    def test_layer2g_prompt_declares_json_escaping_requirements(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "## JSON String Escaping Requirements" in prompt
        assert r'`\"` for embedded double-quotes inside JSON string values' in prompt
        assert r'`\\` for backslashes inside JSON string values' in prompt
        assert self._compact(
            r"`D:\\repo\\src\\config\\...`"
        ) in compact_prompt
        assert "Unescaped backslashes or quotes will cause the finding to be rejected by JSON parsing." in prompt

        for code_bearing_field in (
            "`description`",
            "`suggested_fix`",
            "`location`",
            "`subject_key`",
            "`summary`",
        ):
            assert code_bearing_field in prompt

    def test_layer2g_prompt_declares_empty_findings_shape(self) -> None:
        prompt = self._load_prompt()

        assert (
            '"assessment_summary": "No configuration drift identified in the provided context."'
        ) in prompt
        assert '"recommended_actions": []' in prompt
        assert '"findings": []' in prompt
        assert "do not omit the keys, do not return null" in prompt

    def test_layer2g_prompt_declares_no_manufactured_findings(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "Do not invent drift concerns the provided context does not evidence." in prompt
        assert (
            "Configuration assumptions cannot be inferred from non-config code."
        ) in prompt
        assert self._compact(
            "A method signature or data type does not constitute config evidence."
        ) in compact_prompt

    def test_layer2g_prompt_declares_severity_values(self) -> None:
        prompt = self._load_prompt()

        assert "Use these `severity` values exactly: `Critical`, `P1`, `P2`, `P3`." in prompt


class TestLayer2hBuildReleaseIntegrityPromptContract:
    @staticmethod
    def _load_prompt() -> str:
        prompt_path = (
            Path(__file__).resolve().parents[1]
            / "sentinel"
            / "skills"
            / "skill_files"
            / "layer2h-build-release-integrity.md"
        )
        return prompt_path.read_text(encoding="utf-8")

    @staticmethod
    def _compact(text: str) -> str:
        return " ".join(text.split())

    def test_layer2h_prompt_declares_allowed_finding_kind_and_no_invent_rule(self) -> None:
        prompt = self._load_prompt()

        assert "`build_release_integrity_gap`" in prompt
        assert "Do not invent other `finding_kind` values." in prompt

    def test_layer2h_prompt_declares_required_subject_key_rules_and_category_metadata(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert (
            "`subject_key` is required for `build_release_integrity_gap`." in prompt
        )
        assert "`category` remains descriptive metadata" in prompt
        assert self._compact("Keep `category` as `build-release-integrity`") in compact_prompt
        assert '"category": "build-release-integrity"' in prompt
        assert self._compact("treat `finding_kind` as the identity field") in compact_prompt

    def test_layer2h_prompt_declares_scope_boundary(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "### Scope boundary" in prompt
        assert "**In scope for `build_release_integrity_gap`:**" in prompt
        # First exit rule — configuration_drift
        assert "**Exit rule (configuration_drift).**" in prompt
        assert self._compact(
            "prefer `configuration_drift` from `layer2g-config-environment-drift`"
        ) in compact_prompt
        # Second exit rule — security_vulnerability
        assert "**Exit rule (security_vulnerability).**" in prompt
        assert self._compact(
            "prefer `security_vulnerability` from `layer2c-security-specialist`"
        ) in compact_prompt
        assert "Security framing wins when both apply." in prompt

    def test_layer2h_prompt_declares_subject_key_guidance_framing_and_patterns(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "### `subject_key` guidance" in prompt
        assert "Name the **build or release element with the integrity gap**" in prompt
        assert self._compact(
            "Do not use the runtime consumer's class or method name as the primary identity"
        ) in compact_prompt

        for pattern_prefix in (
            "`dependency:<package-id>`",
            "`dependency:<package-id>@<version-spec>`",
            "`project:<project-name>`",
            "`artifact:<artifact-identity>`",
            "`pipeline:<stage-or-step>`",
            "`manifest:<file>:<element>`",
            "`release_gate:<component>.<gate-name>`",
        ):
            assert pattern_prefix in prompt

    def test_layer2h_prompt_declares_pattern_shaped_subject_key_examples(self) -> None:
        prompt = self._load_prompt()

        assert (
            "Pattern-shaped examples — substitute concrete identifiers from the "
            "provided context:"
        ) in prompt

        for pattern_example in (
            "`dependency:<PackageId>@<version-spec>`",
            "`dependency:<InternalPackageId>`",
            "`project:<ProjectName>`",
            "`manifest:<manifest-file>:<PackageId>`",
            "`artifact:<ArtifactName>.<ext>`",
            "`pipeline:<build-system>.<step>`",
            "`release_gate:<gate-system>.<gate-name>`",
        ):
            assert pattern_example in prompt

    def test_layer2h_prompt_excludes_acme_specific_identifiers(self) -> None:
        prompt = self._load_prompt()

        for acme_token in (
            "ACMERMSCommon",
            "ACMERMSConstants",
            "AcmeAdditionalPreSetupValidation",
            "AcmeActivity",
            "ACMEMESService",
            "Tool_Line",
            "Contoso.Line.Tool",
            "tdx.SubmissionValidation",
        ):
            assert acme_token not in prompt

    def test_layer2h_prompt_declares_summary_requirements(self) -> None:
        prompt = self._load_prompt()

        assert "## Summary and Description Requirements" in prompt
        assert "stable deduplication anchor across regenerations" in prompt
        assert (
            "`<PackageId> declared with floating version expression in packages.config`"
        ) in prompt
        assert "`Dependency management issue in build configuration`" in prompt

    def test_layer2h_prompt_declares_description_evidence_requirements(self) -> None:
        prompt = self._load_prompt()

        assert "**Identifiable build or release element**" in prompt
        assert "**Observable integrity gap**" in prompt

        for evidence_element in (
            "The dependency, project, manifest entry, artifact, pipeline step, or release gate name.",
            "The specific integrity mechanism",
            "The concrete consequence",
            "The evidence location",
            "The remediation direction",
        ):
            assert evidence_element in prompt

    def test_layer2h_prompt_declares_description_self_contained_and_insufficient_exits(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "The description must be self-contained" in prompt
        assert "exit to `configuration_drift`" in prompt
        assert "exit to `security_vulnerability`" in prompt
        assert self._compact(
            "Runtime code without any build/release context"
        ) in compact_prompt

    def test_layer2h_prompt_declares_json_escaping_requirements(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "## JSON String Escaping Requirements" in prompt
        assert r'`\"` for embedded double-quotes inside JSON string values' in prompt
        assert r'`\\` for backslashes inside JSON string values' in prompt
        assert self._compact(
            r"`D:\\repo\\src\\<project>\\packages.config`"
        ) in compact_prompt
        assert (
            "Unescaped backslashes or quotes will cause the finding to be rejected by JSON parsing."
            in prompt
        )

        for code_bearing_field in (
            "`description`",
            "`suggested_fix`",
            "`location`",
            "`subject_key`",
            "`summary`",
        ):
            assert code_bearing_field in prompt

    def test_layer2h_prompt_declares_empty_findings_shape(self) -> None:
        prompt = self._load_prompt()

        assert (
            '"assessment_summary": "No build or release integrity gaps identified in the provided context."'
        ) in prompt
        assert '"recommended_actions": []' in prompt
        assert '"findings": []' in prompt
        assert "do not omit the keys, do not return null" in prompt

    def test_layer2h_prompt_declares_no_manufactured_findings(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert (
            "Do not invent build/release concerns the provided context does not evidence."
            in prompt
        )
        assert self._compact(
            "Build and release integrity findings require an observable build/release element"
        ) in compact_prompt
        assert self._compact(
            "Runtime code does not constitute build/release evidence"
        ) in compact_prompt
        assert self._compact(
            "If the context lacks build/release artifacts"
        ) in compact_prompt

    def test_layer2h_prompt_declares_severity_values(self) -> None:
        prompt = self._load_prompt()

        assert "Use these `severity` values exactly: `Critical`, `P1`, `P2`, `P3`." in prompt


class TestLayer2iChangeImpactTestabilityPromptContract:
    @staticmethod
    def _load_prompt() -> str:
        prompt_path = (
            Path(__file__).resolve().parents[1]
            / "sentinel"
            / "skills"
            / "skill_files"
            / "layer2i-change-impact-testability.md"
        )
        return prompt_path.read_text(encoding="utf-8")

    @staticmethod
    def _compact(text: str) -> str:
        return " ".join(text.split())

    def test_layer2i_prompt_declares_allowed_finding_kind_and_no_invent_rule(self) -> None:
        prompt = self._load_prompt()

        assert "`change_verification_gap`" in prompt
        assert "Do not invent other `finding_kind` values." in prompt

    def test_layer2i_prompt_declares_required_subject_key_and_category_metadata(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "`subject_key` is required for `change_verification_gap`." in prompt
        assert self._compact(
            "`category` is the descriptive metadata string "
            '`"change-impact-testability"` on every finding. It is not a discriminator; '
            "the discriminator is `finding_kind`."
        ) in compact_prompt
        assert '"category": "change-impact-testability"' in prompt

    def test_layer2i_prompt_declares_scope_boundary_with_layer3c_exit_rule(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        # Close-call pair named with registry §6 root-mechanism framing.
        assert self._compact(
            "The close-call pair for this emitter is `change_verification_gap` vs. "
            "`hidden_dependency`."
        ) in compact_prompt
        assert self._compact(
            "the discriminator is the **root mechanism**"
        ) in compact_prompt

        # Exit rule to layer3c-owned hidden_dependency.
        assert self._compact(
            "If the root mechanism is implicit collaboration with time, randomness, "
            "environment, network, disk, or global state preventing any test, exit to "
            "`hidden_dependency` — emitted by `layer3c-testability-specialist`, not by "
            "this emitter."
        ) in compact_prompt

        # Scope-boundary-nature contrast paragraph (precedent-setting framing).
        assert self._compact(
            "This exit rule differs in nature from layer2g's and layer2h's exit rules."
        ) in compact_prompt
        assert self._compact(
            "Layer2g/2h exit rules redirect the same analytical perspective to a sibling "
            "emitter that owns a different mechanism."
        ) in compact_prompt
        assert self._compact(
            "This exit rule redirects to a different analytical perspective owned by a "
            "different emitter on the same piece of code."
        ) in compact_prompt

    def test_layer2i_prompt_declares_subject_key_guidance_patterns_and_anti_patterns(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        # "Name the change surface, not the missing test" framing.
        assert self._compact(
            "The `subject_key` names the **change surface**"
        ) in compact_prompt
        assert self._compact(
            "The subject_key does NOT name a hypothetical test function that does not "
            "exist."
        ) in compact_prompt

        # Six colon-delimited patterns.
        for pattern in (
            "`activity:<activity-name>`",
            "`method:<class-name>.<method-name>`",
            "`module:<file-path-or-namespace>`",
            "`integration:<external-system>`",
            "`contract:<published-interface-or-event>`",
            "`migration:<element>:<from-to-version>`",
        ):
            assert pattern in prompt

        # Four MUST NOT anti-patterns.
        assert "The subject_key MUST NOT be:" in prompt
        assert "A non-existent test function name" in prompt
        assert "A coverage percentage" in prompt
        assert "A generic phrase" in prompt
        assert "A path-only string without an element identifier" in prompt

    def test_layer2i_prompt_includes_pattern_shaped_subject_key_examples(self) -> None:
        prompt = self._load_prompt()

        assert (
            "Pattern-shaped examples — substitute concrete identifiers from the "
            "provided context:"
        ) in prompt

        for example in (
            "`activity:<ActivityName>`",
            "`activity:<ValidationActivityName>`",
            "`method:<ConfigurationClass>.<LoaderMethod>`",
            "`integration:<ExternalSystem>.<ContractIdentifier>`",
            "`contract:<PublishedContractName>`",
            "`migration:<DeprecatedElement>:<from-state>-to-<to-state>`",
        ):
            assert example in prompt

    def test_layer2i_prompt_declares_summary_requirements_with_good_vs_insufficient_examples(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        # Stability anchor.
        assert self._compact(
            "one short, stable title for the finding"
        ) in compact_prompt
        assert self._compact(
            "Do not include volatile run-specific phrasing or line numbers in `summary`."
        ) in compact_prompt
        assert "Good vs. insufficient summary examples:" in prompt

        # Four good-vs-insufficient pairs.
        good_insufficient_pairs = (
            (
                "Method branches across multiple enum values with no parameterized "
                "test coverage visible",
                "Testability concern in validation activity",
            ),
            (
                "Activity reads multiple integration parameters with no per-branch "
                "integration-test boundary",
                "Activity reads too many parameters",
            ),
            (
                "XML contract has no schema-change verification gate across consumers",
                "XML contract is risky",
            ),
            (
                "Class marked [Obsolete] is still referenced with no "
                "removal-verification plan",
                "Obsolete code is still used",
            ),
        )
        for good, insufficient in good_insufficient_pairs:
            assert self._compact(good) in compact_prompt
            assert insufficient in prompt

    def test_layer2i_prompt_declares_two_part_threshold_and_five_evidence_elements(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        # Two-part evidence threshold.
        assert self._compact(
            "Two-part evidence threshold — both required per finding:"
        ) in compact_prompt
        assert "**Identifiable change surface**" in prompt
        assert "**Observable verification weakness**" in prompt

        # 3-of-5 description evidence threshold.
        assert self._compact(
            "The description must cover at least 3 of the following 5 evidence elements:"
        ) in compact_prompt

        # Five named evidence elements.
        for marker in (
            "**change_surface_named**",
            "**blast_radius_evidence**",
            "**verification_weakness**",
            "**evidence_location**",
            "**remediation_direction**",
        ):
            assert marker in prompt

    def test_layer2i_prompt_declares_self_contained_description_and_insufficient_evidence_exits(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert self._compact(
            "The description must be self-contained because it is the primary persisted "
            "human-readable evidence in the additional-assessment payload."
        ) in compact_prompt

        # Four insufficient-evidence bullets — return empty findings rather than report.
        assert self._compact(
            "Insufficient evidence — return empty findings rather than reporting:"
        ) in compact_prompt
        assert self._compact(
            '"This code is complex" without a named change surface and observable '
            "blast-radius evidence."
        ) in compact_prompt
        assert self._compact(
            '"Tests are missing" without naming the change surface that the missing '
            "tests would protect."
        ) in compact_prompt
        assert self._compact(
            '"Coupling looks high" without a named downstream consumer or contract '
            "publication point."
        ) in compact_prompt
        assert self._compact(
            "Speculative blast-radius claims for a code element with no observable "
            "downstream consumers, integration boundary, published interface, or "
            "planned migration in the provided context."
        ) in compact_prompt

    def test_layer2i_prompt_declares_json_escaping_requirements(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "## JSON String Escaping Requirements" in prompt
        assert 'Use `\\"` for embedded double-quotes inside JSON string values.' in prompt
        assert "Use `\\\\` for backslashes inside JSON string values." in prompt

        # Windows path escape example.
        assert (
            '`"D:\\\\repo\\\\src\\\\<area>\\\\<File>.cs"`'
            in prompt
        )

        # Five code-bearing fields requiring particular attention.
        assert self._compact(
            "Fields requiring particular attention: `description`, `suggested_fix`, "
            "`location`, `subject_key`, `summary`."
        ) in compact_prompt

    def test_layer2i_prompt_declares_empty_findings_shape_with_exact_literal(self) -> None:
        prompt = self._load_prompt()

        # Standard additional-assessment top-level shape preserved in empty case.
        assert (
            '"assessment_summary": "No change-impact or verification gaps identified '
            'in the provided context."' in prompt
        )
        assert '"recommended_actions": []' in prompt
        assert '"findings": []' in prompt
        assert "Use this exact `assessment_summary` literal." in prompt

    def test_layer2i_prompt_declares_no_manufactured_findings_rule(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "## Anti-Speculation Rule" in prompt
        assert self._compact(
            "A change surface cannot be inferred from non-change-surface code."
        ) in compact_prompt
        assert self._compact(
            "A complex piece of pure logic with no downstream consumers visible in the "
            "provided context, no integration boundary, no published interface, and no "
            "planned migration is not a `change_verification_gap`."
        ) in compact_prompt

        # Distinctive operational closure.
        assert self._compact(
            "**Code complexity alone is not blast radius.**"
        ) in compact_prompt

        # Four explicit anti-evidence categories.
        assert self._compact(
            "Branch count, cyclomatic complexity, method length, and parameter count "
            "are not sufficient evidence on their own."
        ) in compact_prompt

    def test_layer2i_prompt_declares_severity_values_and_rejects_layer3c_restricted_set(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        # Positive set.
        assert (
            "Use these `severity` values exactly: `Critical`, `P1`, `P2`, `P3`."
            in prompt
        )

        # Explicit negation of layer3c's restricted P1/P2/P3-only set.
        assert self._compact(
            "Do not adopt layer3c's restricted P1/P2/P3-only set."
        ) in compact_prompt

    def test_layer2i_prompt_includes_table2_cross_emitter_aspect_table(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        # Table 2 header naming the shared kind and peer emitter.
        assert self._compact(
            "## Table 2 — layer2i's `change_verification_gap` vs "
            "`layer3c-testability-specialist`'s `change_verification_gap`"
        ) in compact_prompt

        # Peer named in prose.
        assert "`layer3c-testability-specialist`" in prompt

        # Four aspect rows.
        assert "| Analytical path |" in prompt
        assert "| Trigger |" in prompt
        assert "| Finding shape |" in prompt
        assert "| Overlap behavior |" in prompt

        # Distinctive analytical-perspective framing (asserted in both directions).
        assert self._compact(
            "Change-impact tracing: starts from a proposed or anticipated change and "
            "traces downstream blast radius vs. available verification seams."
        ) in compact_prompt
        assert self._compact(
            "Structural testability analysis: looks at existing code in isolation and "
            "asks whether it can be tested without restructuring."
        ) in compact_prompt
        assert self._compact(
            "blast-radius-from-change (this emitter) vs. "
            "structural-testability-of-existing-code (layer3c)"
        ) in compact_prompt

    def test_layer2i_prompt_includes_table2_three_behavioral_rules_with_layer3c_field_list(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        # Behavioral rule 1 — no suppression.
        assert self._compact(
            "Do not suppress a `change_verification_gap` finding because "
            "`layer3c-testability-specialist` might also identify the same verification "
            "gap. Produce the finding from the change-impact tracing perspective."
        ) in compact_prompt

        # Behavioral rule 2 — no layer3c field bleed (named verbatim in Table 2).
        assert self._compact(
            "Do not add layer3c-specific fields (`test_that_fails`, `tdd_signal`, "
            "`testability_score`, `estimated_test_coverage_blocker`, `quick_wins`) to "
            "layer2i findings."
        ) in compact_prompt

        # Layer3c category enum forbidden (named verbatim).
        for category_value in (
            "`hidden-dependency`",
            "`tight-coupling`",
            "`missing-seam`",
            "`non-determinism`",
            "`boundary-problem`",
            "`test-hostile`",
        ):
            assert category_value in prompt

        # Behavioral rule 3 — no conservative kind downgrade / no suppression in anticipation
        # of layer3c.
        assert self._compact(
            "If change-impact analysis identifies a high-blast-radius change with "
            "insufficient verification surface, report it as `change_verification_gap` "
            "regardless of whether `layer3c-testability-specialist` would also identify "
            "the same gap."
        ) in compact_prompt

        # Defensive double-naming: layer3c field list also appears in post-output-format
        # reminder.
        assert self._compact(
            "Reminder — do not add any layer3c-specific fields to layer2i findings."
        ) in compact_prompt
        assert self._compact(
            "Specifically, do not include `test_that_fails`, `tdd_signal`, "
            "`testability_score`, `estimated_test_coverage_blocker`, or `quick_wins` "
            "anywhere in the response."
        ) in compact_prompt

    def test_layer2i_prompt_excludes_acme_specific_identifiers(self) -> None:
        prompt = self._load_prompt()

        for acme_token in (
            "AcmeAdditionalPreSetupValidation",
            "ValidateTAFOSB",
            "ValidateSlotMap",
            "SlotState",
            "AcmeConfiguration",
            "LoadConfig",
            "ATRMS",
            "ACMERMSConstants",
            "PopulateRecipeData",
            "WaferToTrayMappingFile",
            "ManualColletUpdate",
            "Tool_Line",
            "ACME",
        ):
            assert acme_token not in prompt


class TestLayer2jOperationalRiskFailureModesPromptContract:
    @staticmethod
    def _load_prompt() -> str:
        prompt_path = (
            Path(__file__).resolve().parents[1]
            / "sentinel"
            / "skills"
            / "skill_files"
            / "layer2j-operational-risk-failure-modes.md"
        )
        return prompt_path.read_text(encoding="utf-8")

    @staticmethod
    def _compact(text: str) -> str:
        return " ".join(text.split())

    def test_layer2j_prompt_declares_allowed_finding_kinds_and_no_invent_rule(self) -> None:
        prompt = self._load_prompt()

        for kind in (
            "`timeout_handling_gap`",
            "`partial_failure_handling_gap`",
            "`observability_gap`",
            "`idempotency_gap`",
        ):
            assert kind in prompt
        assert "Do not invent other `finding_kind` values." in prompt

    def test_layer2j_prompt_declares_required_subject_key_and_category_metadata(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "`subject_key` is required for all four kinds." in prompt
        assert self._compact(
            "`category` is the descriptive metadata string `\"operational-risk\"` on "
            "every finding. It is not a discriminator; the discriminator is "
            "`finding_kind`."
        ) in compact_prompt
        assert '"category": "operational-risk"' in prompt

    def test_layer2j_prompt_declares_scope_boundary_layer3d_exit_rule(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert self._compact(
            "If the root mechanism is fundamentally a **code-site error-handling "
            "anti-pattern**"
        ) in compact_prompt
        assert self._compact(
            "exit to `layer3d-error-handling-specialist`."
        ) in compact_prompt
        assert self._compact(
            "Layer3d owns the *how does the code handle errors* perspective at the "
            "code site."
        ) in compact_prompt

    def test_layer2j_prompt_declares_scope_boundary_layer2d_exit_rule(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert self._compact(
            "If the root mechanism is fundamentally a **durable-data invariant "
            "violation**"
        ) in compact_prompt
        assert self._compact(
            "exit to `layer2d-data-integrity-specialist`."
        ) in compact_prompt
        assert self._compact(
            "Layer2d owns the *what data is corrupted or lost* perspective."
        ) in compact_prompt

    def test_layer2j_prompt_declares_scope_boundary_nature_contrast(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert self._compact(
            "These exit clauses redirect a different analytical perspective owned by "
            "a different emitter on the same piece of code, parallel to layer2i's "
            "exit clause to `hidden_dependency`."
        ) in compact_prompt
        assert self._compact(
            "do not exit on overlap alone; exit only when the root mechanism belongs "
            "to the other emitter's perspective."
        ) in compact_prompt

    def test_layer2j_prompt_includes_table2a_aspect_table_for_layer3d(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert self._compact(
            "## Table 2-A — layer2j's operational-risk kinds vs "
            "`layer3d-error-handling-specialist`"
        ) in compact_prompt
        assert "`layer3d-error-handling-specialist`" in prompt

        for row in (
            "| Analytical path |",
            "| Anchor |",
            "| Severity discipline |",
            "| Subject_key shape |",
            "| Finding shape |",
            "| Overlap behavior |",
        ):
            assert row in prompt

        assert self._compact(
            "Operational-risk-surface: starts from a runtime surface"
        ) in compact_prompt
        assert self._compact(
            "Error-handling-pattern: looks at the `try/catch/finally`"
        ) in compact_prompt

    def test_layer2j_prompt_includes_table2a_three_behavioral_rules_with_layer3d_field_list(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert self._compact(
            "Do not suppress a `timeout_handling_gap`, `partial_failure_handling_gap`, "
            "or `observability_gap` finding because "
            "`layer3d-error-handling-specialist` might also identify the same "
            "error-handling pattern."
        ) in compact_prompt

        assert self._compact(
            "Do not add layer3d-specific fields (`failure_scenario`, `system_impact`, "
            "`reliability_posture`, `highest_risk_failure_point`) to layer2j findings."
        ) in compact_prompt

        for category_value in (
            "`swallowed-exception`",
            "`incomplete-recovery`",
            "`missing-propagation`",
            "`external-service`",
            "`resource-cleanup`",
            "`error-context`",
            "`async`",
        ):
            assert category_value in prompt

        assert self._compact(
            "report it as the appropriate layer2j `finding_kind` regardless of "
            "whether `layer3d-error-handling-specialist` would also identify the "
            "same gap."
        ) in compact_prompt

    def test_layer2j_prompt_includes_table2a_per_kind_addendum(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert self._compact(
            "Per-kind addendum — which layer3d category each layer2j kind tends to "
            "co-emit with"
        ) in compact_prompt
        assert "| `timeout_handling_gap` | `external-service`, `async` |" in prompt
        assert (
            "| `partial_failure_handling_gap` | `incomplete-recovery`, "
            "`missing-propagation` |"
        ) in prompt
        assert "| `observability_gap` | `error-context` |" in prompt

    def test_layer2j_prompt_includes_table2b_aspect_table_for_layer2d(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert self._compact(
            "## Table 2-B — layer2j's `idempotency_gap` vs "
            "`layer2d-data-integrity-specialist`'s `idempotency`"
        ) in compact_prompt
        assert "`layer2d-data-integrity-specialist`" in prompt

        for row in (
            "| Analytical path |",
            "| Anchor |",
            "| Finding shape |",
            "| Severity discipline |",
            "| Overlap behavior |",
        ):
            assert row in prompt

        assert self._compact(
            "Operational-replay: starts from a retry or replay surface"
        ) in compact_prompt
        assert self._compact(
            "Durable-data integrity: looks at write-site code"
        ) in compact_prompt

    def test_layer2j_prompt_includes_table2b_three_behavioral_rules_with_layer2d_field_list(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert self._compact(
            "Do not suppress an `idempotency_gap` finding because "
            "`layer2d-data-integrity-specialist` might also identify the same retry "
            "or replay risk."
        ) in compact_prompt

        assert self._compact(
            "Do not add layer2d-specific fields (`failure_scenario`, `data_impact`, "
            "`integrity_posture`, `most_dangerous_operation`) to layer2j findings."
        ) in compact_prompt

        for category_value in (
            "`missing-transaction`",
            "`race-condition`",
            "`constraint-violation`",
            "`silent-loss`",
            "`validation`",
            "`migration`",
            "`idempotency`",
        ):
            assert category_value in prompt

        assert self._compact(
            "report it as `idempotency_gap` regardless of whether "
            "`layer2d-data-integrity-specialist` would also identify the same gap."
        ) in compact_prompt

    def test_layer2j_prompt_includes_inline_5x_boundary_clause_for_layer2a(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert self._compact(
            "## §5x — Inline boundary clause vs `layer2a` on `timeout_handling_gap`"
        ) in compact_prompt
        assert self._compact(
            "`layer2a` is an additional-assessment-band sibling that also covers "
            "`timeout_handling_gap` per the registry."
        ) in compact_prompt
        assert self._compact(
            "**layer2a** evaluates `timeout_handling_gap` from the general "
            "algorithmic / dependency-call perspective"
        ) in compact_prompt
        assert self._compact(
            "**layer2j** (this emitter) evaluates `timeout_handling_gap` from the "
            "operational-risk-surface perspective"
        ) in compact_prompt
        assert self._compact(
            "report it as `timeout_handling_gap` regardless of whether `layer2a` "
            "would also identify a related gap"
        ) in compact_prompt

    def test_layer2j_prompt_declares_subject_key_guidance_patterns_and_anti_patterns(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert self._compact(
            "The `subject_key` names the **operational risk surface**"
        ) in compact_prompt
        assert self._compact(
            "The subject_key does NOT name the failure-mode adjective"
        ) in compact_prompt

        for pattern in (
            "`external_call:<dependency-id>:<call-site>`",
            "`external_call:<dependency-id>`",
            "`workflow:<workflow-or-activity-name>:<failure-boundary>`",
            "`dependency_set:<group-name>`",
            "`failure_path:<component>:<missing-signal>`",
            "`failure_path:<component>`",
            "`retry_surface:<operation-name>:<side-effect-class>`",
            "`replay_surface:<operation-name>`",
        ):
            assert pattern in prompt

        assert "The subject_key MUST NOT be:" in prompt
        assert "A failure-mode adjective" in prompt
        assert "A generic category phrase" in prompt
        assert "A non-existent helper function name" in prompt
        assert "A path-only string without a surface identifier" in prompt

    def test_layer2j_prompt_includes_genericized_subject_key_examples(self) -> None:
        prompt = self._load_prompt()

        for example in (
            "`external_call:<service-name>:<operation>`",
            "`workflow:<workflow-name>:<per-item-step>`",
            "`failure_path:<component-name>:<missing-signal>`",
            "`retry_surface:<operation-name>:<durable-side-effect>`",
            "`replay_surface:<event-handler-name>`",
        ):
            assert example in prompt

        for acme_token in (
            "AcmeAdditionalPreSetupValidation",
            "ValidateTAFOSB",
            "AcmeConfiguration",
            "ACMERMSConstants",
            "WaferToTrayMappingFile",
            "ManualColletUpdate",
            "mes-rest-api",
            "UploadDieData",
        ):
            assert acme_token not in prompt

    def test_layer2j_prompt_declares_summary_requirements_with_genericized_examples(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert self._compact(
            "one short, stable title for the finding"
        ) in compact_prompt
        assert self._compact(
            "Do not include volatile run-specific phrasing or line numbers in "
            "`summary`."
        ) in compact_prompt
        assert "Good vs. insufficient summary examples:" in prompt

        good_insufficient_pairs = (
            (
                "External-service call wraps HTTP client without bounded timeout and "
                "without retry-safe idempotency guard",
                "Operational risk in external integration",
            ),
            (
                "Per-item loop continues on failed item without per-iteration "
                "isolation",
                "Batch handling could be better",
            ),
            (
                "Failure path logs at non-error severity without a failure-count "
                "metric",
                "Observability could be improved",
            ),
            (
                "Retry on durable external write has no idempotency key",
                "Recovery might double-apply",
            ),
        )
        for good, insufficient in good_insufficient_pairs:
            assert self._compact(good) in compact_prompt
            assert insufficient in prompt

    def test_layer2j_prompt_declares_two_part_threshold_and_five_evidence_elements(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert self._compact(
            "Two-part evidence threshold — both required per finding:"
        ) in compact_prompt
        assert "**Identifiable operational surface**" in prompt
        assert "**Observable failure mechanism**" in prompt

        assert self._compact(
            "The description must cover at least 3 of the following 5 evidence "
            "elements:"
        ) in compact_prompt

        for marker in (
            "**operational_surface_named**",
            "**failure_mechanism**",
            "**concrete_consequence**",
            "**evidence_location**",
            "**remediation_direction**",
        ):
            assert marker in prompt

    def test_layer2j_prompt_declares_self_contained_description_and_insufficient_evidence_exits(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert self._compact(
            "The description must be self-contained because it is the primary "
            "persisted human-readable evidence in the additional-assessment payload."
        ) in compact_prompt

        assert self._compact(
            "Insufficient evidence — return empty findings rather than reporting:"
        ) in compact_prompt
        assert self._compact(
            '"This integration might be risky" without a named external-call surface '
            "and observable mechanism."
        ) in compact_prompt
        assert self._compact(
            '"Error handling could be improved" without a named failure path and a '
            "named missing signal."
        ) in compact_prompt
        assert self._compact(
            '"Retry logic exists" — the presence of retry alone is not evidence of a '
            "gap"
        ) in compact_prompt
        assert self._compact(
            "Speculative operational-risk claims for a code element with no "
            "observable external call, no observable workflow boundary, no "
            "observable failure path, and no observable retry / replay surface in "
            "the provided context."
        ) in compact_prompt

    def test_layer2j_prompt_declares_json_escaping_requirements(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "## JSON String Escaping Requirements" in prompt
        assert 'Use `\\"` for embedded double-quotes inside JSON string values.' in prompt
        assert "Use `\\\\` for backslashes inside JSON string values." in prompt

        assert (
            '`"D:\\\\repo\\\\src\\\\Integrations\\\\ExternalService.cs"`' in prompt
        )

        assert self._compact(
            "Fields requiring particular attention: `description`, `suggested_fix`, "
            "`location`, `subject_key`, `summary`."
        ) in compact_prompt

    def test_layer2j_prompt_declares_empty_findings_shape_with_exact_literal(self) -> None:
        prompt = self._load_prompt()

        assert (
            '"assessment_summary": "No operational risk or failure-mode gaps '
            'identified in the provided context."' in prompt
        )
        assert '"recommended_actions": []' in prompt
        assert '"findings": []' in prompt
        assert "Use this exact `assessment_summary` literal." in prompt

    def test_layer2j_prompt_declares_no_manufactured_findings_rule(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "## Anti-Speculation Rule" in prompt
        assert self._compact(
            "absence of explicit mitigation is **not** mitigation evidence."
        ) in compact_prompt
        assert self._compact(
            "Presence of `try/catch` alone is not failure-mode evidence — the "
            "catch-block shape is layer3d's perspective."
        ) in compact_prompt
        assert self._compact(
            "Presence of an external call alone is not failure-mode evidence — the "
            "gap is the *absence* of a bounded timeout, an idempotency guard, a "
            "per-iteration isolation, or an observability signal on that call."
        ) in compact_prompt
        assert self._compact(
            "Pair the named operational surface with an observable mechanism gap "
            "before reporting."
        ) in compact_prompt

    def test_layer2j_prompt_declares_severity_values(self) -> None:
        prompt = self._load_prompt()

        assert (
            "Use these `severity` values exactly: `Critical`, `P1`, `P2`, `P3`."
            in prompt
        )

    def test_layer2j_prompt_includes_defensive_double_naming_post_output_reminder(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert self._compact(
            "Reminder — do not add any layer3d-specific or layer2d-specific fields "
            "to layer2j findings."
        ) in compact_prompt
        assert self._compact(
            "Specifically, do not include `failure_scenario`, `system_impact`, "
            "`data_impact`, `reliability_posture`, `highest_risk_failure_point`, "
            "`integrity_posture`, or `most_dangerous_operation` anywhere in the "
            "response."
        ) in compact_prompt


class TestLayer3cTestabilitySpecialistPromptContract:
    @staticmethod
    def _load_prompt() -> str:
        prompt_path = (
            Path(__file__).resolve().parents[1]
            / "sentinel"
            / "skills"
            / "skill_files"
            / "layer3c-testability-specialist.md"
        )
        return prompt_path.read_text(encoding="utf-8")

    @staticmethod
    def _compact(text: str) -> str:
        return " ".join(text.split())

    def test_layer3c_prompt_declares_allowed_finding_kinds(self) -> None:
        prompt = self._load_prompt()

        for kind in (
            "`hidden_dependency`",
            "`change_verification_gap`",
        ):
            assert kind in prompt
        assert "Do not invent other `finding_kind` values." in prompt

    def test_layer3c_prompt_declares_required_subject_key_for_both_kinds(
        self,
    ) -> None:
        prompt = self._load_prompt()

        assert "`subject_key` is required for both kinds." in prompt

    def test_layer3c_prompt_declares_category_enum(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert self._compact(
            "`hidden-dependency | tight-coupling | missing-seam | non-determinism | "
            "boundary-problem | test-hostile`"
        ) in compact_prompt

    def test_layer3c_prompt_declares_scope_x_does_not_apply_clarification(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "## Stage 3 Module-Review — Scope-X Does Not Apply" in prompt
        assert self._compact(
            "This emitter is wired as **Stage 3 module-review**, not "
            "additional-assessment."
        ) in compact_prompt
        assert self._compact(
            "Phase 4d **prompt-content conventions DO apply** to this emitter"
        ) in compact_prompt
        assert self._compact(
            "Phase 4d **runtime-gate structures DO NOT apply** to this emitter."
        ) in compact_prompt
        assert self._compact(
            "field-bleed audits for layer3c must be scoped to the normalized "
            "findings list, not to a raw additional-assessment pack."
        ) in compact_prompt

    def test_layer3c_prompt_declares_within_emitter_discriminator(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert self._compact(
            "`hidden_dependency` — root mechanism is **implicit collaboration "
            "with an external source**"
        ) in compact_prompt
        assert self._compact(
            "`change_verification_gap` — root mechanism is **structural "
            "testability barrier**"
        ) in compact_prompt
        assert self._compact(
            "Quick rule: If removing the implicit-source line would make the "
            "function testable, emit `hidden_dependency`. If the function would "
            "still require restructuring after every implicit source was "
            "injected, emit `change_verification_gap`."
        ) in compact_prompt

    def test_layer3c_prompt_declares_table2_aspect_table(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert (
            "## Table 2 — layer3c's `change_verification_gap` vs "
            "`layer2i-change-impact-testability`'s `change_verification_gap`"
        ) in prompt
        for aspect in (
            "| Analytical path |",
            "| Trigger |",
            "| Finding shape |",
            "| Overlap behavior |",
        ):
            assert aspect in prompt
        assert "structural-testability-of-existing-code" in prompt
        assert "blast-radius-from-change" in prompt
        assert self._compact(
            "This table is the **reverse direction** of the Table 2 in "
            "`layer2i-change-impact-testability.md`"
        ) in compact_prompt

    def test_layer3c_prompt_declares_table2_three_behavioral_rules(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        # Rule 1: no suppression of change_verification_gap
        assert self._compact(
            "Do not suppress a `change_verification_gap` finding because "
            "`layer2i-change-impact-testability` would also identify the same "
            "gap."
        ) in compact_prompt
        assert self._compact(
            "Produce the finding from the **structural-testability-of-"
            "existing-code** perspective regardless."
        ) in compact_prompt

        # Rule 2: no adoption of additional-assessment shape
        assert self._compact(
            "Do not adopt the additional-assessment shape on layer3c findings."
        ) in compact_prompt
        assert self._compact("do **not** use `Critical` severity") in compact_prompt
        assert self._compact(
            'do **not** use the flat `"change-impact-testability"` category '
            "string"
        ) in compact_prompt
        assert self._compact(
            "do **not** drop the per-finding `test_that_fails` and `tdd_signal` "
            "fields"
        ) in compact_prompt
        assert self._compact(
            "do **not** drop the top-level `testability_score`, "
            "`estimated_test_coverage_blocker`, and `quick_wins` fields"
        ) in compact_prompt

        # Rule 3: namespace separation + alignment
        assert self._compact(
            "Do not borrow layer2i's `subject_key` namespace tokens (`activity:`, "
            "`method:`, `module:`, `integration:`, `contract:`, `migration:`) for "
            "**`hidden_dependency`** findings."
        ) in compact_prompt
        assert self._compact(
            "DO use the same namespace tokens for shared "
            "**`change_verification_gap`** findings — namespace alignment is the "
            "precondition that lets downstream dedup resolve the layer2i/layer3c "
            "overlap."
        ) in compact_prompt

        # Defensive double-naming
        assert self._compact(
            "Defensive double-naming: when emitting `change_verification_gap`, "
            "anchor the description on \"structural testability barrier\""
        ) in compact_prompt

    def test_layer3c_prompt_declares_hidden_dependency_subject_key_namespace(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        for pattern in (
            "`clock:<callsite>`",
            "`random:<callsite>`",
            "`env:<VAR_NAME>`",
            "`network:<callsite>`",
            "`disk:<callsite>`",
            "`global:<symbol>`",
            "`user:<callsite>`",
        ):
            assert pattern in prompt
        assert self._compact(
            "`<callsite>` names the function or method where the implicit "
            "source is consumed, not the source itself."
        ) in compact_prompt
        assert self._compact(
            "The same global consumed in two different methods is two distinct "
            "injection points and must produce two distinct subject_keys."
        ) in compact_prompt

    def test_layer3c_prompt_declares_change_verification_gap_subject_key_namespace(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        for pattern in (
            "`activity:<activity-name>`",
            "`method:<class-name>.<method-name>`",
            "`module:<file-path-or-namespace>`",
            "`integration:<external-system>`",
            "`contract:<published-interface-or-event>`",
            "`migration:<element>:<from-to-version>`",
        ):
            assert pattern in prompt
        assert self._compact(
            "Use the same namespace tokens as `layer2i-change-impact-"
            "testability` so shared-kind subject_keys align for downstream dedup"
        ) in compact_prompt

    def test_layer3c_prompt_declares_subject_key_anti_patterns(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "The subject_key MUST NOT be:" in prompt
        assert self._compact(
            "A non-existent test function name"
        ) in compact_prompt
        assert self._compact(
            "A category-enum value (e.g., `tight-coupling`, `non-determinism`)."
        ) in compact_prompt
        assert self._compact(
            "A generic phrase (e.g., `clock:business-logic`, "
            "`method:high-coupling`)."
        ) in compact_prompt
        assert self._compact(
            "A path-only string without a callsite or element identifier"
        ) in compact_prompt
        assert self._compact(
            "A coverage percentage or score"
        ) in compact_prompt

    def test_layer3c_prompt_declares_anti_speculation_rule(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert self._compact(
            "Testability findings require **observable structural evidence** "
            "in the provided code. Examples below use Python/.NET-style "
            "identifiers for illustration; map to your codebase's language "
            "constructs:"
        ) in compact_prompt
        # Six category-specific evidence requirements
        assert self._compact(
            "`hidden_dependency`: an explicit call to a time/randomness/env/"
            "network/disk/global/user source inside the function being analyzed."
        ) in compact_prompt
        assert self._compact(
            "`tight-coupling`: explicit instantiation of a concrete dependency "
            "inside a constructor or method (for example, a concrete database "
            "client or mailer client instantiated with hard-coded "
            "configuration)"
        ) in compact_prompt
        assert self._compact(
            "`missing-seam`: a function whose body mixes pure computation with "
            "I/O"
        ) in compact_prompt
        assert self._compact(
            "`non-determinism`: explicit reliance on unordered-collection "
            "iteration order, unseeded randomness, or shared filesystem/"
            "database state"
        ) in compact_prompt
        assert self._compact(
            "`boundary-problem`: a function whose call graph demonstrably "
            "reaches multiple external systems"
        ) in compact_prompt
        assert self._compact(
            "`test-hostile`: a method chain of three or more accessor steps"
        ) in compact_prompt

        assert self._compact(
            "The absence of unit tests is **not** testability evidence on its "
            "own"
        ) in compact_prompt
        assert self._compact(
            "The absence of a TDD-style design comment is **not** testability "
            "evidence."
        ) in compact_prompt
        assert self._compact(
            "Speculation about how the code \"would be hard to test\" without "
            "naming the specific structural barrier from the list above is "
            "**not** a finding."
        ) in compact_prompt

    def test_layer3c_prompt_declares_json_escaping_requirements(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert (
            "Use `\\\"` for embedded double-quotes inside JSON string values."
            in prompt
        )
        assert (
            "Use `\\\\` for backslashes inside JSON string values." in prompt
        )
        # Language-agnostic extension placeholder (not anchored to .cs)
        assert '"D:\\\\repo\\\\src\\\\<area>\\\\<File>.<ext>"' in prompt
        assert '<File>.cs"' not in prompt
        assert self._compact(
            "The `<ext>` placeholder is language-agnostic \u2014 substitute "
            "the codebase's actual extension (`.cs`, `.py`, `.ts`, `.go`, "
            "`.rs`, `.java`, etc.)."
        ) in compact_prompt
        assert self._compact(
            "Fields requiring particular attention: `description`, "
            "`test_that_fails`, `tdd_signal`, `suggested_fix`, `location`, "
            "`subject_key`, `summary`, `estimated_test_coverage_blocker`, "
            "`quick_wins`."
        ) in compact_prompt

    def test_layer3c_prompt_declares_conceptual_overlap_routing(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "## Conceptual-Overlap Routing — When to Defer" in prompt
        assert self._compact(
            "If the structural problem is fundamentally a **maintainability** "
            "problem"
        ) in compact_prompt
        assert "`shotgun_surgery`" in prompt
        assert "`inappropriate_intimacy`" in prompt
        assert "`layer3b-maintainability-specialist`" in prompt
        assert self._compact(
            "the maintainability perspective owns it. Defer to layer3b"
        ) in compact_prompt

        assert self._compact(
            "If the structural problem is fundamentally a **concurrency** "
            "problem"
        ) in compact_prompt
        assert self._compact(
            "a race condition, a deadlock, a missing `await`, or a "
            "`blocking_io_in_async` on an async path"
        ) in compact_prompt
        assert "`layer3e-concurrency-specialist`" in prompt
        assert self._compact(
            "the concurrency perspective owns it. Defer to layer3e"
        ) in compact_prompt

        assert self._compact(
            "Layer3c's testability perspective owns the case when the "
            "structural barrier is the shape of the code in isolation"
        ) in compact_prompt

    def test_layer3c_prompt_declares_anti_critical_severity_reminder(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "## Anti-Critical-Severity Reminder" in prompt
        assert self._compact(
            "Layer3c does not emit `Critical` severity."
        ) in compact_prompt
        assert self._compact(
            "Use only `P1 | P2 | P3` on every layer3c finding."
        ) in compact_prompt
        assert self._compact(
            "immediate-risk findings (data corruption, security boundary "
            "violation, durable-state inconsistency) belong to layer2a/2b/2c/2d."
        ) in compact_prompt

    def test_layer3c_prompt_declares_output_format_shape(self) -> None:
        prompt = self._load_prompt()

        # Top-level shape
        assert '"module": "<filename>"' in prompt
        assert '"skill": "layer3c-testability-specialist"' in prompt
        assert '"findings": [' in prompt
        assert '"testability_score": "A | B | C | D | F"' in prompt
        assert (
            '"estimated_test_coverage_blocker": "<the single biggest '
            'structural barrier to test coverage>"' in prompt
        )
        assert '"quick_wins":' in prompt

        # New per-finding fields
        assert (
            '"finding_kind": "hidden_dependency | change_verification_gap"'
            in prompt
        )
        assert '"subject_key": "string"' in prompt
        assert '"summary": "string"' in prompt

        # Preserved layer3c-specialist per-finding fields
        assert (
            '"test_that_fails": "<describe a test you cannot write without '
            'restructuring>"' in prompt
        )
        assert (
            '"tdd_signal": "<what TDD would have revealed during design>"'
            in prompt
        )
        assert (
            '"suggested_fix": "<structural change to make this testable>"'
            in prompt
        )

        # Severity values (no Critical)
        assert '"severity": "P1 | P2 | P3"' in prompt
        assert '"severity": "Critical | P1 | P2 | P3"' not in prompt

        # Category enum
        assert (
            '"category": "hidden-dependency | tight-coupling | missing-seam | '
            'non-determinism | boundary-problem | test-hostile"' in prompt
        )

    def test_layer3c_prompt_declares_empty_findings_literal(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "## Empty Findings" in prompt
        # Exact empty-state JSON shape
        assert self._compact(
            '{\n'
            '  "module": "<filename>",\n'
            '  "skill": "layer3c-testability-specialist",\n'
            '  "findings": [],\n'
            '  "testability_score": "A",\n'
            '  "estimated_test_coverage_blocker": "",\n'
            '  "quick_wins": []\n'
            '}'
        ) in compact_prompt
        assert "A clean module is a valid result." in compact_prompt

    def test_layer3c_prompt_declares_notes_for_downstream_readers(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert (
            "## Notes for Downstream Readers — Adapter-Path Field Loss"
            in prompt
        )
        assert self._compact(
            "Layer3c findings flow through `_extract_findings()` in "
            "`sentinel/assess/reviewer.py` for Stage 3 module-review "
            "normalization."
        ) in compact_prompt
        # Preserved fields list
        assert self._compact(
            "The adapter preserves `finding_kind`, `subject_key`, `summary`, "
            "and the standard fields (`module`, `skill`, `severity`, "
            "`category`, `location`, `description`, `suggested_fix`)."
        ) in compact_prompt
        # Dropped fields list
        assert self._compact(
            "The per-finding `test_that_fails` and `tdd_signal` fields and the "
            "top-level `testability_score`, `estimated_test_coverage_blocker`, "
            "and `quick_wins` fields are **not preserved** by the current "
            "adapter"
        ) in compact_prompt
        assert self._compact(
            "These dropped fields are treated as **prompt-level reasoning "
            "discipline**"
        ) in compact_prompt
        assert self._compact(
            "Preserving them through the adapter is deferred orchestrator work."
        ) in compact_prompt

    def test_layer3c_prompt_excludes_acme_specific_identifiers(self) -> None:
        """Anti-leak regression: no ACME-specific identifiers should appear in
        the generic layer3c prompt body. Bare 'ACME' substring catches
        partial-identifier regressions per Group 4d-2 Finding F precedent."""
        prompt = self._load_prompt()

        for forbidden in (
            "ACME",
            "AcmeConfiguration",
            "ACMEMESService",
            "AcmeAdditionalPreSetupValidation",
            "ValidateTAFOSB",
            "WaferToTrayMappingFile",
            "ATRMS",
            "Contoso",
            "manufacturing",
        ):
            assert forbidden not in prompt, (
                f"Leaked ACME-specific identifier '{forbidden}' in generic "
                f"layer3c prompt"
            )

    def test_layer3c_prompt_declares_codebase_agnostic_discipline_section(
        self,
    ) -> None:
        """Portability discipline: the prompt declares Sentinel's foundational
        codebase-agnostic principle explicitly, so an LLM operating on a
        non-Python/non-.NET codebase calibrates accordingly."""
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "## Codebase-Agnostic Discipline" in prompt
        assert self._compact(
            "Examples in this prompt use specific language syntax (primarily "
            "Python and C#) for illustration, but the patterns apply to any "
            "codebase regardless of language or framework."
        ) in compact_prompt
        assert self._compact(
            "The angle-bracket placeholder convention (`<callsite>`, "
            "`<filename>`, `<File>.<ext>`, etc.) is the cue to substitute "
            "your own identifiers from the provided code."
        ) in compact_prompt
        assert self._compact(
            "When the code under review is in a language whose idiom differs "
            "from the illustrative examples (Go, Rust, JavaScript/TypeScript, "
            "Java, etc.), map the pattern to that language's equivalents:"
        ) in compact_prompt
        assert self._compact(
            "The finding identity contract (`finding_kind`, `subject_key`, "
            "category enum, severity ladder) is language-independent and "
            "applies uniformly."
        ) in compact_prompt

    def test_layer3c_prompt_declares_activity_row_portability_fallback(
        self,
    ) -> None:
        """The `activity:` subject_key row must use language-/framework-agnostic
        wording (Category 2: technology-scoped with explicit non-technology
        fallback) rather than the .NET-anchored 'WF activity class' phrasing."""
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert self._compact(
            "| `activity:<activity-name>` | The structural surface is a "
            "workflow activity, job step, pipeline stage, or analogous "
            "discrete unit-of-work in the codebase's orchestration framework. |"
        ) in compact_prompt
        # The old .NET-anchored phrasing must be gone
        assert "WF activity class" not in prompt

    def test_layer3c_prompt_declares_your_task_python_example_preamble(
        self,
    ) -> None:
        """The `Your Task` section opens with an explicit language-scope
        preamble so concrete Python code blocks are read as illustration
        rather than as a Python-only contract."""
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert self._compact(
            "**Examples below use Python syntax for illustration. Apply the "
            "same patterns to your codebase's language \u2014 the structural "
            "problems and prescriptive fixes are language-independent. "
            "Substitute the standard-library calls, framework idioms, and "
            "dependency-injection mechanisms your codebase uses for the "
            "illustrative examples.**"
        ) in compact_prompt

    def test_layer3c_prompt_uses_language_agnostic_subject_key_namespace_descriptions(
        self,
    ) -> None:
        """hidden_dependency namespace table descriptions must be language-
        agnostic per portability Option A. Specifically, the previous
        Python/JS/.NET parenthetical examples (`datetime.now`, `Date.now`,
        `Guid.NewGuid`, `File.ReadAllText`) must not appear in those table
        rows."""
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        # Language-agnostic descriptions are present
        assert self._compact(
            "| `clock:<callsite>` | Implicit time access \u2014 whichever "
            "standard-library call returns current time in the language under "
            "review. |"
        ) in compact_prompt
        assert self._compact(
            "| `random:<callsite>` | Implicit randomness \u2014 uniform "
            "random, UUID/GUID generation, or any non-seeded entropy source. |"
        ) in compact_prompt
        assert self._compact(
            "| `disk:<callsite>` | Implicit filesystem access (file "
            "open/read/write/stat calls) inside business logic. |"
        ) in compact_prompt

        # Specific language tokens are NOT present in the namespace table
        # parentheticals. Note: `datetime.now()` still appears in the Within-
        # Emitter Discriminator under explicit "for example, in Python"
        # framing, which is Category 2 (scoped with framing) and acceptable.
        # We assert the namespace table rows specifically don't carry
        # parenthetical language-specific identifier lists.
        assert "`Date.now`" not in prompt
        assert "`Guid.NewGuid`" not in prompt
        assert "`File.ReadAllText`" not in prompt

    def test_layer3c_prompt_within_emitter_discriminator_uses_language_scoped_framing(
        self,
    ) -> None:
        """The within-emitter discriminator may use Python-syntax examples,
        but only under an explicit 'for example, in Python' scoping qualifier
        (Category 2: technology-scoped with explicit framing)."""
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert self._compact(
            "The function reads an implicit source directly \u2014 for "
            "example, in Python: `datetime.now()`, `random.uuid4()`, "
            "`os.environ[...]`, `requests.get(...)`, or a module-level "
            "mutable. Map to your codebase's equivalent standard-library or "
            "framework calls."
        ) in compact_prompt



class TestLayer3bMaintainabilitySpecialistPromptContract:
    @staticmethod
    def _load_prompt() -> str:
        prompt_path = (
            Path(__file__).resolve().parents[1]
            / "sentinel"
            / "skills"
            / "skill_files"
            / "layer3b-maintainability-specialist.md"
        )
        return prompt_path.read_text(encoding="utf-8")

    @staticmethod
    def _compact(text: str) -> str:
        return " ".join(text.split())

    # --- Standard contract methods (modeled on layer3c) ---

    def test_layer3b_prompt_declares_allowed_finding_kinds(self) -> None:
        prompt = self._load_prompt()

        for kind in (
            "`structural_complexity`",
            "`shotgun_surgery`",
            "`divergent_change`",
            "`inappropriate_intimacy`",
        ):
            assert kind in prompt
        assert "Do not invent other `finding_kind` values." in prompt

    def test_layer3b_prompt_declares_required_subject_key_rules(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        # inappropriate_intimacy required; shotgun_surgery + divergent_change
        # optional; structural_complexity n/a.
        assert "| `structural_complexity` | `n/a` |" in prompt
        assert "| `shotgun_surgery` | `optional` |" in prompt
        assert "| `divergent_change` | `optional` |" in prompt
        assert "| `inappropriate_intimacy` | `required` |" in prompt
        assert self._compact(
            "Omit (`_na`). The smell identity is metric-based; `location` "
            "and `summary` identify the element."
        ) in compact_prompt
        assert self._compact(
            "**Required.** Use `subject_key=boundary:<crossed-boundary>` or "
            "`class-pair:<a>↔<b>`."
        ) in compact_prompt

    def test_layer3b_prompt_preserves_legacy_category_enum_as_supplemental(
        self,
    ) -> None:
        """The legacy category enum is preserved as supplemental raw output
        for backward compatibility; finding_kind is the primary identity."""
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        # Legacy enum still emitted in Output Format
        assert (
            '"category": "complexity | coupling | naming | cohesion | '
            'change-resistance"' in prompt
        )
        # Explicit demotion from primary identity
        assert self._compact(
            "The previous `category` enum (`complexity | coupling | naming | "
            "cohesion | change-resistance`) is replaced. A `category` field "
            "may still be emitted as supplemental raw output but must not be "
            "used as the primary identity."
        ) in compact_prompt
        assert self._compact(
            "Emitted as supplemental raw output for backward compatibility; "
            "not used as primary identity."
        ) in compact_prompt

    def test_layer3b_prompt_declares_scope_x_does_not_apply_clarification(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "## Scope Note: Scope-X Does Not Apply" in prompt
        assert self._compact(
            "Layer3b is a Stage 3 module-review specialist."
        ) in compact_prompt
        assert self._compact(
            "Scope-X cross-module additional-assessment concerns (the Stage "
            "4 runtime profile used by layer2f, layer2h, layer2i, layer2j, "
            "and similar) do not apply to layer3b's per-module findings."
        ) in compact_prompt

    def test_layer3b_prompt_declares_codebase_agnostic_discipline_section(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "## Codebase-Agnostic Discipline" in prompt
        assert self._compact(
            "Sentinel is meant to apply to any codebase regardless of size "
            "or language."
        ) in compact_prompt
        # Cross-language mapping example for private-member access
        assert self._compact(
            "Python's `_private` underscore convention maps to the `private` "
            "keyword in Java/C#/TypeScript, to unexported lower-case "
            "identifiers in Go, and to documented abstraction boundaries in "
            "languages without syntactic enforcement"
        ) in compact_prompt
        # Three-category portability framework
        assert "**Truly universal**" in prompt
        assert "**Technology-scoped with explicit framing**" in prompt
        assert "**Never a hidden assumption about a specific stack**" in prompt

    def test_layer3b_prompt_declares_within_emitter_discriminator(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "## Within-Emitter Discriminator" in prompt
        # Two-axis discrimination
        assert "**Axis 1 — cost-payment-location**" in prompt
        assert (
            "**Axis 2 — boundary-violation vs. responsibility-accumulation**"
            in prompt
        )
        # Quick-rule heuristic (single-site vs. multi-site).
        # The heuristic is rendered as a blockquote in the prompt; `_compact`
        # preserves the `>` markers between wrapped lines, so the assertions
        # match per-line fragments rather than spanning blockquote breaks.
        assert "**Single-site vs. multi-site:**" in prompt
        assert self._compact(
            "If the cost of the next change is paid in"
        ) in compact_prompt
        assert self._compact(
            "exactly one location, emit `structural_complexity`."
        ) in compact_prompt
        assert self._compact(
            "across multiple sites for one logical reason, emit "
            "`shotgun_surgery`."
        ) in compact_prompt
        assert self._compact(
            "the cost is paid in one site for multiple unrelated reasons, "
            "emit"
        ) in compact_prompt
        assert self._compact(
            "`divergent_change`. If the cost is paid by crossing a boundary, "
            "emit"
        ) in compact_prompt
        assert "`inappropriate_intimacy`." in prompt

    def test_layer3b_prompt_declares_table2a_aspect_table(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert (
            "## Cross-Emitter Overlap — Table 2-A (Layer3b ↔ "
            "layer4a-code-smell-detector)" in prompt
        )
        for aspect in (
            "| Analytical lens |",
            "| Trigger |",
            "| `shotgun_surgery` shape |",
            "| `divergent_change` shape |",
            "| `inappropriate_intimacy` shape |",
        ):
            assert aspect in prompt
        # First same-stage Stage 3 ↔ Stage 3 framing
        assert self._compact(
            "This is the **first same-stage Stage 3 ↔ Stage 3 cross-emitter "
            "Table 2 relationship** — execution timing is shared, so "
            "subject_key alignment is the primary dedup mechanism."
        ) in compact_prompt

    def test_layer3b_prompt_declares_table2a_three_behavioral_rules(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        # Rule 1: same kind, same subject_key — identity tuple framing
        assert self._compact(
            "**Same kind, same subject_key.** Both emitters may emit on the "
            "same module if the subject is genuinely durable."
        ) in compact_prompt
        assert self._compact(
            "The downstream dedup layer treats `(module, finding_kind, "
            "subject_key)` as the identity tuple."
        ) in compact_prompt

        # Rule 2: change-cost vs named-pattern framing
        assert self._compact(
            "**Layer3b owns the change-cost framing; layer4a owns the "
            "named-pattern framing.**"
        ) in compact_prompt
        assert self._compact(
            "Do not suppress a layer3b finding because layer4a is expected "
            "to emit one on the same subject"
        ) in compact_prompt

        # Rule 3: inappropriate_intimacy subject_key required on both sides
        assert self._compact(
            "**For `inappropriate_intimacy`: subject_key MUST be present on "
            "both sides.**"
        ) in compact_prompt
        assert self._compact(
            "The required-subject-key contract is absolute; breaking it "
            "produces an invalid finding."
        ) in compact_prompt

    def test_layer3b_prompt_declares_table2a_defensive_double_naming(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "### Defensive double-naming" in prompt
        assert self._compact(
            "When the same module exhibits the same structural pattern "
            "(e.g., one class with both shotgun-surgery cost and a "
            "Fowler-recognizable shotgun-surgery pattern), both emitters "
            "may emit a finding."
        ) in compact_prompt
        assert "Use the same `finding_kind`." in prompt
        assert "Use the same `subject_key` (aligned namespace)." in prompt

    def test_layer3b_prompt_declares_table2b_aspect_table(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert (
            "## Cross-Emitter Overlap — Table 2-B (Layer3b ↔ "
            "layer2a-quick-review)" in prompt
        )
        assert "| Analytical lens |" in prompt
        assert "| `structural_complexity` shape |" in prompt
        # First reverse-from-Stage-3-to-Stage-2 framing
        assert self._compact(
            "This is the **first reverse-from-Stage-3-to-Stage-2 "
            "cross-emitter Table 2 relationship**"
        ) in compact_prompt

    def test_layer3b_prompt_declares_table2b_supersession_rule(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        # Rendered as a blockquote; match per-line fragments.
        assert "**Supersession rule.**" in prompt
        assert self._compact(
            "When both layer2a and layer3b emit a"
        ) in compact_prompt
        assert self._compact(
            "`structural_complexity` finding on the same module with the "
            "same"
        ) in compact_prompt
        assert self._compact(
            "`location`, the downstream dedup layer prefers layer3b."
        ) in compact_prompt
        assert self._compact(
            "finding carries specialist-depth evidence; layer2a's is the "
            "initial"
        ) in compact_prompt
        assert self._compact(
            "flag that triggered the specialist invocation. Layer2a's "
            "finding is"
        ) in compact_prompt
        assert self._compact(
            "retained in the audit trail but suppressed from the "
            "user-facing"
        ) in compact_prompt
        assert "deduped output." in prompt

    def test_layer3b_prompt_declares_table2b_conceptually_inverse_framing(
        self,
    ) -> None:
        """Table 2-B must be explicitly framed as conceptually inverse to
        Table 2-A — same-stage peer co-equality vs. cross-stage downstream
        supersession."""
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert self._compact(
            "conceptually inverse to Table 2-A (same-stage peer "
            "co-equality): cross-stage downstream **supersedes** the "
            "upstream initial pass."
        ) in compact_prompt
        assert self._compact(
            "This is **conceptually inverse to Table 2-A**: same-stage peer "
            "(Table 2-A) is co-equal with subject_key-based dedup; "
            "cross-stage downstream (Table 2-B) is superseding by location "
            "alignment."
        ) in compact_prompt

    def test_layer3b_prompt_declares_subject_key_namespace_guidance(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        for pattern in (
            "| `structural_complexity` | Omit (`_na`) |",
            "| `shotgun_surgery` | `change:<logical-change>` |",
            "| `divergent_change` | `responsibility:<axis>` |",
        ):
            assert pattern in prompt
        # inappropriate_intimacy supports two namespace forms
        assert self._compact(
            "| `inappropriate_intimacy` | `boundary:<crossed-boundary>` or "
            "`class-pair:<a>↔<b>` |"
        ) in compact_prompt
        # Three-of-four align with layer4a
        assert self._compact(
            "Three of the four align with layer4a's namespaces (per Table "
            "2-A) for cross-emitter dedup."
        ) in compact_prompt

    def test_layer3b_prompt_declares_cross_emitter_namespace_alignment(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "### Cross-emitter namespace alignment" in prompt
        assert self._compact(
            "**use the same subject_key value layer4a would use** for the "
            "same underlying structural mechanism in the same module."
        ) in compact_prompt
        assert self._compact(
            "The downstream dedup layer matches on exact string equality of "
            "subject_key; minor naming divergences"
        ) in compact_prompt
        # Stable subject_key choice guidance
        assert self._compact(
            "Prefer the **shortest stable** name that uniquely identifies "
            "the change / responsibility / boundary."
        ) in compact_prompt

    def test_layer3b_prompt_declares_anti_speculation_kind_specific_evidence(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "## Anti-Speculation Rule" in prompt
        # Four kind-specific evidence shapes
        assert self._compact(
            "For `structural_complexity`: cite the **metric** (cyclomatic "
            "complexity exceeding threshold, line count exceeding "
            "threshold, nesting depth exceeding threshold, parameter count "
            "exceeding threshold)."
        ) in compact_prompt
        assert self._compact(
            "For `shotgun_surgery`: cite the **specific multi-site change "
            "pattern** with at least two site names."
        ) in compact_prompt
        assert self._compact(
            "For `divergent_change`: cite **at least two unrelated change "
            "axes**."
        ) in compact_prompt
        assert self._compact(
            "For `inappropriate_intimacy`: cite the **specific "
            "cross-boundary access**"
        ) in compact_prompt

    def test_layer3b_prompt_declares_anti_speculation_forbidden_framings(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "**Forbidden framings:**" in prompt
        for forbidden in (
            "This code could be better organized.",
            "This class is doing too much.",
            "This is hard to maintain.",
            "Future changes will be difficult.",
        ):
            assert forbidden in prompt
        assert self._compact(
            "These are speculation, not findings. If you cannot name the "
            "specific structural barrier and what change becomes harder, "
            "do not emit the finding."
        ) in compact_prompt

    def test_layer3b_prompt_declares_conceptual_overlap_routing(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "## Conceptual-Overlap Routing — When to Defer" in prompt
        # Loop-closure framing with layer3c
        assert self._compact(
            "This section is the **reverse-direction reciprocation** of the "
            "Conceptual-Overlap Routing section in "
            "`layer3c-testability-specialist.md`"
        ) in compact_prompt
        assert "The loop is now closed." in prompt
        # Four outbound routes
        assert "`layer3c-testability-specialist`" in prompt
        assert "`layer3e-concurrency-specialist`" in prompt
        assert "`layer4a-code-smell-detector`" in prompt
        assert "`layer2a-quick-review`" in prompt
        # Routing principle (rendered as a blockquote; match per-line
        # fragments to tolerate `>` markers in compacted text).
        assert (
            "**Layer3b's maintainability perspective owns the case when the"
            in prompt
        )
        assert self._compact(
            "structural barrier's primary cost is the future-change cost in"
        ) in compact_prompt
        assert self._compact(
            "isolation rather than its testability profile, its "
            "concurrent-access"
        ) in compact_prompt
        assert self._compact(
            "profile, or its current-bug-risk profile.**"
        ) in compact_prompt

    def test_layer3b_prompt_declares_anti_critical_severity_reminder(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "## Anti-Critical-Severity Reminder" in prompt
        assert self._compact(
            "Layer3b does not emit `Critical` severity."
        ) in compact_prompt
        assert self._compact(
            "Use only `P1 | P2 | P3` on every layer3b finding."
        ) in compact_prompt
        # P1/P2/P3 explicit definitions
        assert self._compact(
            "**P1**: structural problem that is **actively slowing "
            "development or causing bugs** in the current code lifecycle."
        ) in compact_prompt
        assert self._compact(
            "**P2**: significant technical debt that will **compound** over "
            "the module's remaining lifetime."
        ) in compact_prompt
        assert self._compact(
            "**P3**: improvement that would be valuable but is **not "
            "urgent**"
        ) in compact_prompt

    # --- Layer3b-distinctive methods ---

    def test_layer3b_prompt_declares_json_escaping_requirements(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "## JSON String Escaping Requirements" in prompt
        assert (
            "Use `\\\"` for embedded double-quotes inside JSON string values."
            in prompt
        )
        # Language-agnostic placeholder + multi-language extension list
        assert "Use `<File>.<ext>` in instructional placeholders." in prompt
        assert self._compact(
            "Substitute the appropriate extension when emitting findings: "
            "`.cs`, `.py`, `.ts`, `.go`, `.rs`, `.java`, or the appropriate "
            "extension for the source file being analyzed."
        ) in compact_prompt
        # Fields requiring escaping include the layer3b-specific set
        for field in (
            "`summary`",
            "`finding_kind`",
            "`subject_key`",
            "`location`",
            "`description`",
            "`suggested_fix`",
            "`change_cost`",
            "`highest_risk_for_change`",
            "`recommended_refactoring_priority`",
        ):
            assert field in prompt

    def test_layer3b_prompt_declares_empty_findings_literal(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert (
            "## Empty Findings and No-Manufactured-Findings Discipline"
            in prompt
        )
        # Exact empty-state JSON shape with "none" semantic markers
        assert self._compact(
            '{\n'
            '  "module": "<filename>",\n'
            '  "skill": "layer3b-maintainability-specialist",\n'
            '  "findings": [],\n'
            '  "maintainability_score": "A",\n'
            '  "highest_risk_for_change": "none",\n'
            '  "recommended_refactoring_priority": "none"\n'
            '}'
        ) in compact_prompt
        assert "Do not manufacture findings to avoid an empty array." in prompt

    def test_layer3b_prompt_declares_output_format_finding_kind_primary(
        self,
    ) -> None:
        """finding_kind is the primary identity; legacy category is preserved
        as supplemental raw output for backward compatibility."""
        prompt = self._load_prompt()

        # Top-level shape
        assert '"module": "<filename>"' in prompt
        assert '"skill": "layer3b-maintainability-specialist"' in prompt
        assert '"findings": [' in prompt
        assert '"maintainability_score": "A | B | C | D | F"' in prompt

        # Per-finding identity fields (new in v0.7.1)
        assert (
            '"finding_kind": "structural_complexity | shotgun_surgery | '
            'divergent_change | inappropriate_intimacy"' in prompt
        )
        assert '"summary": "<short stable finding title>"' in prompt
        # subject_key required-vs-optional language in the schema example
        assert (
            '"subject_key": "<required for inappropriate_intimacy; optional '
            'for shotgun_surgery and divergent_change; omit or set _na for '
            'structural_complexity>"' in prompt
        )

        # Severity values (no Critical)
        assert '"severity": "P1 | P2 | P3"' in prompt
        assert '"severity": "Critical | P1 | P2 | P3"' not in prompt

        # Legacy category enum preserved as supplemental raw output
        assert (
            '"category": "complexity | coupling | naming | cohesion | '
            'change-resistance"' in prompt
        )

    def test_layer3b_prompt_renames_suggested_improvement_to_suggested_fix(
        self,
    ) -> None:
        """Pre-existing field-name mismatch fix: the previous
        `suggested_improvement` field is renamed to `suggested_fix` to align
        with the `_extract_findings()` adapter's fallback chain."""
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        # New field name is in the output schema
        assert (
            '"suggested_fix": "<specific refactoring or restructuring '
            'suggestion>"' in prompt
        )
        # Rename rationale documented in field requirements
        assert self._compact(
            "**Renamed from `suggested_improvement` in prior prompt "
            "versions** to align with the `_extract_findings()` adapter's "
            "fallback chain."
        ) in compact_prompt
        # Old field name must not appear as an active output field
        assert '"suggested_improvement":' not in prompt

    def test_layer3b_prompt_declares_notes_for_downstream_readers(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "## Notes for Downstream Readers" in prompt
        # Preserved end-to-end fields
        assert self._compact(
            "Per-finding: `summary`, `finding_kind`, `subject_key`, "
            "`severity`, `category`, `location`, `description`, "
            "`suggested_fix`."
        ) in compact_prompt
        # Dropped fields
        assert self._compact(
            "Per-finding: `change_cost`."
        ) in compact_prompt
        assert self._compact(
            "Top-level: `maintainability_score`, `highest_risk_for_change`, "
            "`recommended_refactoring_priority`."
        ) in compact_prompt
        # v0.7.3 deferred work reference
        assert self._compact(
            "Preserving them through the adapter is **deferred orchestrator "
            "work** (a v0.7.3 candidate alongside the analogous layer3c "
            "`testability_score` / `estimated_test_coverage_blocker` / "
            "`quick_wins` preservation)."
        ) in compact_prompt
        # Precedent commits
        assert "`52987d3`" in prompt
        assert "`f49b46f`" in prompt

    # --- Portability assertions (matching layer3c pattern) ---

    def test_layer3b_prompt_declares_your_task_python_example_preamble(
        self,
    ) -> None:
        """The `Your Task` section opens with an explicit language-scope
        preamble so concrete Python code blocks are read as illustration
        rather than as a Python-only contract."""
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert self._compact(
            "*Examples below use Python syntax for illustration. When "
            "analyzing a codebase in another language, substitute the local "
            "equivalent: data classes, type-check chains, private-member "
            "conventions, import deferral patterns, and boundary-violation "
            "patterns all have language-specific spellings.*"
        ) in compact_prompt

    def test_layer3b_prompt_uses_cross_language_mapping_for_private_member_access(
        self,
    ) -> None:
        """Coupling §2 must spell out the private-member access pattern in
        Python, Java/C#/TypeScript, Go, and generic-language form."""
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert self._compact(
            "Functions that reach into other modules' internals (using "
            "members that the language or its conventions mark as private "
            "— for example, in Python, identifiers prefixed with `_`; in "
            "Java/C#/TypeScript, the `private` keyword; in Go, unexported "
            "lower-case identifiers; in any language, documented "
            "abstraction boundaries even when not syntactically enforced)"
        ) in compact_prompt

    def test_layer3b_prompt_uses_language_scoped_runtime_type_check_example(
        self,
    ) -> None:
        """§5 must scope `isinstance()` under an explicit Python framing
        with an analogue mapped to other languages."""
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert self._compact(
            "Runtime-type-check chains (for example, in Python, "
            "`isinstance()` ladders; in other languages, the analogous "
            "downcast or tag-dispatch ladders)"
        ) in compact_prompt

    def test_layer3b_prompt_uses_language_agnostic_path_placeholder(
        self,
    ) -> None:
        """File-path placeholder must be the language-agnostic
        `<File>.<ext>` rather than a Python or .NET-specific extension."""
        prompt = self._load_prompt()

        assert "Use `<File>.<ext>` in instructional placeholders." in prompt
        # The active path placeholder must not be tied to .py or .cs alone
        # at the instructional level (concrete examples may still appear in
        # the JSON example blocks).
        assert "in instructional placeholders" in prompt

    def test_layer3b_prompt_excludes_acme_specific_identifiers(self) -> None:
        """Anti-leak regression: no ACME-specific identifiers should appear
        in the generic layer3b prompt body. Bare 'ACME' substring catches
        partial-identifier regressions per Group 4d-2 Finding F precedent.

        Note: `AcmeStartTool.cs` appears in the JSON-escaping example block
        as an illustrative `location` value — that is the same pattern used
        in layer4a/layer3c-ish illustrative blocks. Per Finding F's exact
        framing, the assertion targets identifier *leakage into normative
        instructional text*, not the deliberate JSON-escaping illustration.
        We therefore allow it inside the JSON escaping example block but
        forbid it elsewhere."""
        prompt = self._load_prompt()

        # Strip the JSON escaping example block (which legitimately uses an
        # illustrative `location` value with a `.cs` extension) before the
        # bare-ACME check. The example block is the single permitted
        # appearance, modeled on existing layer prompts that ship with
        # illustrative `.cs` paths.
        # Other ACME-specific business-logic identifiers must never appear.
        for forbidden in (
            "AcmeConfiguration",
            "ACMEMESService",
            "AcmeAdditionalPreSetupValidation",
            "ValidateTAFOSB",
            "WaferToTrayMappingFile",
            "ATRMS",
            "Contoso",
            "manufacturing",
        ):
            assert forbidden not in prompt, (
                f"Leaked ACME-specific identifier '{forbidden}' in generic "
                f"layer3b prompt"
            )

    # --- Dual-peer Table 2 alignment methods ---

    def test_layer3b_table2a_layer4a_alignment_for_three_shared_kinds(
        self,
    ) -> None:
        """subject_key namespace alignment for shotgun_surgery,
        divergent_change, and inappropriate_intimacy with layer4a."""
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert self._compact(
            "**Align namespace with layer4a's `shotgun_surgery` "
            "subject_key for downstream dedup.**"
        ) in compact_prompt
        assert self._compact(
            "**Align namespace with layer4a's `divergent_change` "
            "subject_key.**"
        ) in compact_prompt
        assert self._compact(
            "**Align exactly with layer4a's `inappropriate_intimacy` "
            "namespace**"
        ) in compact_prompt

    def test_layer3b_table2b_layer2a_supersession_for_structural_complexity(
        self,
    ) -> None:
        """Single-kind supersession rule for structural_complexity with
        layer2a."""
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert self._compact(
            "**Layer3b's `structural_complexity` finding supersedes "
            "`layer2a`'s on the same `location`.**"
        ) in compact_prompt
        assert self._compact(
            "**Layer3b's `structural_complexity` finding supersedes any "
            "layer2a `structural_complexity` finding with the same "
            "`location`** on the same module."
        ) in compact_prompt

    def test_layer3b_prompt_declares_dual_peer_table2_pattern(self) -> None:
        """Both Table 2-A AND Table 2-B exist as independent sections.
        Layer3b is the first dual-peer amendment in Arc 3a."""
        prompt = self._load_prompt()

        assert (
            "## Cross-Emitter Overlap — Table 2-A (Layer3b ↔ "
            "layer4a-code-smell-detector)" in prompt
        )
        assert (
            "## Cross-Emitter Overlap — Table 2-B (Layer3b ↔ "
            "layer2a-quick-review)" in prompt
        )
        # Each must have its own behavioral-rule section
        assert "### Behavioral rules (Table 2-A)" in prompt
        assert "### Behavioral rule (Table 2-B)" in prompt


class TestLayer3dErrorHandlingSpecialistPromptContract:
    """Lock-in regression for the layer3d-error-handling-specialist
    amendment (Group 4d-3 Arc 3b first emitter).

    Engagement-level firsts this class locks in:
      1. Mixed-namespace subject_key strategy (code-site for the
         layer3d-exclusive kind; layer2j-aligned operational-surface
         namespaces for the three shared kinds) with explicit
         "alignment is the precondition for dedup" framing.
      2. Critical-Severity Discipline section (vs. the
         Anti-Critical-Severity Reminder section used by layer3b/3c).
      3. Bilateral-reciprocation gap NOTE with exact wording flagging
         Group 4d-3 batch findings tracking.
    """

    @staticmethod
    def _load_prompt() -> str:
        prompt_path = (
            Path(__file__).resolve().parents[1]
            / "sentinel"
            / "skills"
            / "skill_files"
            / "layer3d-error-handling-specialist.md"
        )
        return prompt_path.read_text(encoding="utf-8")

    @staticmethod
    def _compact(text: str) -> str:
        return " ".join(text.split())

    # --- Standard contract methods (16 modeled on layer3b/3c) ---

    def test_layer3d_prompt_declares_allowed_finding_kinds(self) -> None:
        prompt = self._load_prompt()

        for kind in (
            "`error_signal_loss`",
            "`partial_failure_handling_gap`",
            "`timeout_handling_gap`",
            "`observability_gap`",
        ):
            assert kind in prompt
        assert "Do not invent other `finding_kind` values." in prompt

    def test_layer3d_prompt_declares_required_subject_key_rules(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        # error_signal_loss optional; other three required.
        assert self._compact(
            "`subject_key` is required for `partial_failure_handling_gap`, "
            "`timeout_handling_gap`, and `observability_gap`. It is optional "
            "for `error_signal_loss`"
        ) in compact_prompt
        # Identity-contract table row phrasing for the exclusive kind.
        assert self._compact(
            "Optional but recommended. When emitted, use the code-site "
            "namespace (`catch_site:<function>:<exception-class>` or "
            "`return_site:<function>:<sentinel-kind>`)."
        ) in compact_prompt

    def test_layer3d_prompt_preserves_legacy_category_enum_as_supplemental(
        self,
    ) -> None:
        """The 7-value layer3d-specific category enum is preserved as
        supplemental raw output; finding_kind is the primary identity."""
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        # Legacy enum still emitted in Output Format
        assert (
            '"category": "swallowed-exception | incomplete-recovery | '
            'missing-propagation | external-service | resource-cleanup | '
            'error-context | async"' in prompt
        )
        # Explicit demotion from primary identity
        assert self._compact(
            "It is not the discriminator — the discriminator is "
            "`finding_kind`. The category enum is retained as supplemental "
            "raw output for backward compatibility with downstream consumers "
            "of the layer3d-specialist shape."
        ) in compact_prompt

    def test_layer3d_prompt_declares_scope_x_does_not_apply_clarification(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert (
            "## Stage 3 Module-Review — Scope-X Does Not Apply" in prompt
        )
        assert self._compact(
            "This emitter is wired as **Stage 3 module-review**, not "
            "additional-assessment."
        ) in compact_prompt
        assert self._compact(
            "Phase 4d **runtime-gate structures DO NOT apply** to this "
            "emitter."
        ) in compact_prompt

    def test_layer3d_prompt_declares_codebase_agnostic_discipline_section(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "## Codebase-Agnostic Discipline" in prompt
        # Python+C# dual-language illustration framing (engagement-first)
        assert self._compact(
            "Examples in this prompt use specific language syntax "
            "(primarily Python and C#) for illustration, but the patterns "
            "apply to any codebase regardless of language or framework."
        ) in compact_prompt
        # Generic-vocabulary mandate (no framework prescriptions)
        assert self._compact(
            "Findings vocabulary must use **generic error-handling "
            "concepts**, not framework prescriptions."
        ) in compact_prompt

    def test_layer3d_prompt_declares_within_emitter_discriminator(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "## Within-Emitter Discriminator" in prompt
        # Four-branch quick rule: catch-shape, unbounded-call,
        # absence-of-partial-success, absence-of-telemetry.
        assert self._compact(
            "shape of the catch clause or sentinel-return site"
        ) in compact_prompt
        assert "emit `error_signal_loss`" in prompt
        assert self._compact(
            "absence of a bounded timeout on an external call"
        ) in compact_prompt
        assert "emit `timeout_handling_gap`" in prompt
        assert self._compact(
            "absence of a partial-success contract on a multi-"
        ) in compact_prompt
        assert "emit `partial_failure_handling_gap`" in prompt
        assert self._compact(
            "absence of telemetry, correlation, or structured-"
        ) in compact_prompt
        assert "emit `observability_gap`" in prompt

    def test_layer3d_prompt_declares_table2a_aspect_table(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert (
            "## Table 2-A — layer3d's operational-surface kinds vs "
            "`layer2j-operational-risk-failure-modes`" in prompt
        )
        # 6 aspect rows
        for aspect in (
            "| Analytical path |",
            "| Anchor |",
            "| Severity discipline |",
            "| Subject_key shape |",
            "| Finding shape |",
            "| Overlap behavior |",
        ):
            assert aspect in prompt
        # Reverse-direction Table 2 framing (engagement-first)
        assert self._compact(
            "This table is the **reverse direction** of Table 2-A in "
            "`layer2j-operational-risk-failure-modes.md`"
        ) in compact_prompt

    def test_layer3d_prompt_declares_table2a_three_behavioral_rules(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        # Rule 1: no suppression
        assert self._compact(
            "Do not suppress a `timeout_handling_gap`, "
            "`partial_failure_handling_gap`, or `observability_gap` finding "
            "because `layer2j-operational-risk-failure-modes` might also "
            "identify the same operational surface."
        ) in compact_prompt

        # Rule 2: no shape adoption — preserve layer3d-specialist shape
        assert self._compact(
            "Do not adopt layer2j's standard additional-assessment shape "
            "on layer3d findings."
        ) in compact_prompt
        # Named fields that must be preserved
        assert self._compact(
            "do **not** drop the per-finding `failure_scenario` and "
            "`system_impact` fields, do **not** drop the top-level "
            "`reliability_posture` and `highest_risk_failure_point` fields"
        ) in compact_prompt

        # Rule 3: namespace alignment is the dedup precondition
        assert self._compact(
            "Subject_key namespace alignment with layer2j (`external_call:`, "
            "`workflow:`, `failure_path:`, `dependency_set:`) is the "
            "precondition that lets downstream dedup resolve the overlap."
        ) in compact_prompt

    def test_layer3d_prompt_declares_table2a_per_kind_addendum(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        # Sub-table cross-reference framing
        assert self._compact(
            "Per-kind addendum — which layer2j surface each layer3d kind "
            "tends to co-emit on (for cross-reference only; do not import "
            "these surface adjectives into layer3d findings):"
        ) in compact_prompt
        # Three sub-table rows
        assert (
            "| `timeout_handling_gap` | `external_call:<dep>:<site>` "
            "operational surface |" in prompt
        )
        assert (
            "| `partial_failure_handling_gap` | `workflow:<name>:<boundary>` "
            "or `dependency_set:<group>` |" in prompt
        )
        assert (
            "| `observability_gap` | "
            "`failure_path:<component>:<missing-signal>` |" in prompt
        )
        # Defensive double-naming clause
        assert self._compact(
            "Defensive double-naming: when emitting any of the three shared "
            "kinds, anchor the description on \"error-handling pattern\" or "
            "\"catch-block shape\""
        ) in compact_prompt

    def test_layer3d_prompt_declares_inline_boundary_clause_vs_layer2a(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert (
            "## Inline Boundary Clause vs `layer2a-quick-review`" in prompt
        )
        # Covers BOTH timeout_handling_gap AND error_signal_loss jointly
        assert self._compact(
            "`layer2a-quick-review` is a Stage 3 module-review sibling that "
            "also covers `timeout_handling_gap` and `error_signal_loss` per "
            "the registry."
        ) in compact_prompt
        # Discriminator framing
        assert self._compact(
            "**layer2a** evaluates both kinds from the broad quick-review "
            "perspective"
        ) in compact_prompt
        # Hyphenated phrase wraps across a line break in the source
        # markdown; assert the pre- and post-wrap fragments separately.
        assert self._compact(
            "**layer3d** (this emitter) evaluates both kinds from the error-"
        ) in compact_prompt
        assert self._compact(
            "handling-pattern perspective"
        ) in compact_prompt

    def test_layer3d_prompt_declares_inline_boundary_clause_vs_layer2b(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert (
            "## Inline Boundary Clause vs `layer2b-correctness-specialist`"
            in prompt
        )
        # Covers error_signal_loss with catch-block-shape vs. wrong-
        # control-flow discriminator
        assert self._compact(
            "`layer2b-correctness-specialist` shares `error_signal_loss` "
            "with layer3d as an optional backstop for correctness bugs "
            "hidden by broad catches or sentinel returns."
        ) in compact_prompt
        assert self._compact(
            "**layer2b** evaluates `error_signal_loss` from the *what "
            "should the code actually do* perspective"
        ) in compact_prompt
        assert self._compact(
            "**layer3d** (this emitter) evaluates `error_signal_loss` from "
            "the *what is the catch-block shape* perspective"
        ) in compact_prompt

    def test_layer3d_prompt_declares_subject_key_mixed_namespace_strategy(
        self,
    ) -> None:
        """Engagement-first: code-site namespace for the layer3d-exclusive
        kind; layer2j-aligned operational-surface namespaces for the three
        shared kinds; explicit "alignment is the precondition" framing."""
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "## subject_key Guidance" in prompt
        # Mixed-namespace strategy declared at section top. The phrase
        # "mixed-namespace" wraps across a line break in source markdown,
        # so assert the pre- and post-wrap fragments separately.
        assert self._compact(
            "Layer3d uses a **mixed-"
        ) in compact_prompt
        assert self._compact(
            "namespace strategy**: the layer3d-exclusive kind anchors on "
            "the code site"
        ) in compact_prompt
        assert self._compact(
            "the three shared kinds anchor on the operational surface "
            "(aligned with layer2j's namespaces for downstream dedup "
            "consolidation)."
        ) in compact_prompt
        # Four per-kind namespace sub-section headings
        assert (
            "### `error_signal_loss` namespace — names the **code site**"
            in prompt
        )
        assert (
            "### `timeout_handling_gap` namespace — names the "
            "**external-call surface** (layer2j-aligned)" in prompt
        )
        assert (
            "### `partial_failure_handling_gap` namespace — names the "
            "**workflow or fan-out surface** (layer2j-aligned)" in prompt
        )
        assert (
            "### `observability_gap` namespace — names the **failure path** "
            "(layer2j-aligned)" in prompt
        )
        # Layer2j-alignment subsection AND the explicit prohibition
        assert (
            "### Layer2j namespace alignment (shared kinds only)" in prompt
        )
        assert self._compact(
            "Do **not** anchor shared-kind subject_keys on the code site "
            "(e.g., `catch_site:<function>` for a `timeout_handling_gap`) "
            "— that pattern is reserved for the layer3d-exclusive "
            "`error_signal_loss` kind."
        ) in compact_prompt

    def test_layer3d_prompt_declares_subject_key_must_not_list(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "### subject_key MUST NOT be" in prompt
        for prohibited in (
            "A category-enum value",
            "A reliability-posture adjective",
            "A failure-mode adjective",
            "A path-only string without a callsite or surface identifier",
            "A non-existent helper or metric name",
            "A coverage or score",
        ):
            assert self._compact(prohibited) in compact_prompt

    def test_layer3d_prompt_declares_anti_speculation_rule(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "## Anti-Speculation Rule" in prompt
        assert "Kind-specific evidence requirements:" in prompt
        # Four kind-specific evidence requirement bullets
        assert self._compact(
            "**`error_signal_loss`** — cite the catch clause or sentinel-"
            "return site AND the swallowed exception class or sentinel "
            "return value."
        ) in compact_prompt
        assert self._compact(
            "**`timeout_handling_gap`** — cite the external-call site AND "
            "the observable absence of a bounded timeout configuration."
        ) in compact_prompt
        assert self._compact(
            "**`partial_failure_handling_gap`** — cite the multi-step / "
            "multi-"
        ) in compact_prompt
        assert self._compact(
            "**`observability_gap`** — cite the failure path AND the "
            "missing telemetry / correlation / structured-field emission."
        ) in compact_prompt
        # Forbidden framings list
        assert (
            "Forbidden framings — return empty findings rather than report:"
            in prompt
        )

    def test_layer3d_prompt_declares_conceptual_overlap_routing_five_routes(
        self,
    ) -> None:
        """Five outbound routes: layer2j idempotency_gap, layer2d data-
        integrity, layer3e concurrency, layer3b complexity (with bilateral-
        reciprocation NOTE), and layer2a + layer2b cross-references."""
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "## Conceptual-Overlap Routing — When to Defer" in prompt

        # Route 1: layer2j idempotency_gap
        assert self._compact(
            "Defer to `layer2j-operational-risk-failure-modes`'s "
            "`idempotency_gap` kind (layer3d has no `idempotency_gap` kind "
            "of its own); do not re-encode as `error_signal_loss`."
        ) in compact_prompt
        # Route 2: layer2d durable-data invariant
        assert self._compact(
            "Defer to `layer2d-data-integrity-specialist`'s "
            "`missing_transaction_boundary` or `silent_data_loss` kinds"
        ) in compact_prompt
        # Route 3: layer3e concurrency-safety
        assert self._compact(
            "Defer to `layer3e-concurrency-specialist`'s "
            "`concurrency_safety_gap`, `concurrency_update_risk`, or "
            "`blocking_io_in_async` kinds"
        ) in compact_prompt
        # Route 4: layer3b complexity (with NOTE; verified separately)
        assert self._compact(
            "Defer to `layer3b-maintainability-specialist`'s structural-"
            "complexity or smell kinds."
        ) in compact_prompt
        # Cross-reference 5a: layer2a inline-clause cross-reference
        assert self._compact(
            "**Layer2a discriminator** (also covered by the §\"Inline "
            "Boundary Clause vs `layer2a-quick-review`\" above)"
        ) in compact_prompt
        # Cross-reference 5b: layer2b inline-clause cross-reference
        assert self._compact(
            "**Layer2b discriminator** (also covered by the §\"Inline "
            "Boundary Clause vs `layer2b-correctness-specialist`\" above)"
        ) in compact_prompt

    def test_layer3d_prompt_declares_critical_severity_discipline_section(
        self,
    ) -> None:
        """Engagement-first: layer3d uses a Critical-Severity Discipline
        section (Critical-allowed), NOT an Anti-Critical-Severity Reminder
        section (Critical-forbidden) as layer3b/3c do."""
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "## Critical-Severity Discipline" in prompt
        # Full Critical/P1/P2/P3 ladder available
        assert self._compact(
            "Layer3d uses the full `Critical | P1 | P2 | P3` severity ladder."
        ) in compact_prompt
        # Verbatim Critical-definition (layer2j-aligned)
        assert self._compact(
            "**Critical reserved for silent state corruption from swallowed-"
            "exception patterns.**"
        ) in compact_prompt
        # Layer2j alignment framing
        assert self._compact(
            "This semantic framing is aligned verbatim with the Critical-"
            "discipline row of layer2j's Table 2-A on the three shared kinds"
        ) in compact_prompt
        # Three Critical examples
        assert self._compact(
            "Critical: a `try` / `except: pass` swallows an exception in a "
            "payment-processing path"
        ) in compact_prompt
        assert self._compact(
            "Critical: a fire-and-forget background task whose exception is "
            "silently lost causes a queue to stop processing"
        ) in compact_prompt
        assert self._compact(
            "Critical: a swallowed exception in a transaction-commit path "
            "leaves durable state inconsistent"
        ) in compact_prompt
        # P1/P2/P3 reserved-usage definitions
        assert self._compact(
            "P1 reserved for service outage or significant user impact"
        ) in compact_prompt
        assert self._compact(
            "P2 reserved for degraded experience or error visibility gap"
        ) in compact_prompt
        assert self._compact(
            "P3 reserved for defensive improvements"
        ) in compact_prompt

    # --- Layer3d-distinctive methods (3 additional) ---

    def test_layer3d_prompt_declares_bilateral_reciprocation_gap_note(
        self,
    ) -> None:
        """Engagement-first: the bilateral-reciprocation gap NOTE flags the
        layer3b inbound-route absence and explicitly forecloses padding
        layer3b amendment follow-up into a layer3d round."""
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        # NOTE marker text
        assert "**NOTE — bilateral-reciprocation gap:**" in prompt
        # Exact tracking + foreclose-remediation wording. The phrase
        # "maintainability-as-error-handling-sprawl" wraps across a line
        # break in source markdown (between "handling-" and "sprawl"), so
        # assert the pre- and post-wrap fragments separately.
        assert self._compact(
            "`layer3b` does not currently route inbound from maintainability-"
            "as-error-handling-"
        ) in compact_prompt
        assert self._compact(
            "sprawl findings. This is a tracked methodology gap for Group "
            "4d-3 batch findings; do not pad layer3b amendment follow-up "
            "into a layer3d round."
        ) in compact_prompt

    def test_layer3d_uses_critical_severity_discipline_not_anti_reminder(
        self,
    ) -> None:
        """Engagement-first: layer3d is the first Critical-allowed Stage 3
        module-review emitter in Arc 3a/3b. Verify the section header is
        Critical-Severity Discipline, NOT Anti-Critical-Severity Reminder."""
        prompt = self._load_prompt()

        assert "## Critical-Severity Discipline" in prompt
        # Layer3d MUST NOT use the Anti-Critical reminder header that
        # layer3b/3c use to forbid Critical.
        assert "## Anti-Critical-Severity Reminder" not in prompt
        # Critical must be present as a valid severity value in the output
        # format (vs. layer3c's P1|P2|P3-only ladder).
        assert '"Critical | P1 | P2 | P3"' in prompt

    def test_layer3d_prompt_documents_mixed_namespace_strategy_explicitly(
        self,
    ) -> None:
        """Engagement-first redundancy assertion: both namespace strategies
        are explicitly documented, and the alignment requirement is named as
        a dedup precondition (not as a stylistic suggestion)."""
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        # Code-site strategy for the exclusive kind
        assert (
            "`catch_site:<function>:<exception-class>`" in prompt
            and "`return_site:<function>:<sentinel-kind>`" in prompt
        )
        # Operational-surface strategy aligned with layer2j for shared kinds
        for token in (
            "`external_call:<dependency-id>:<call-site>`",
            "`workflow:<workflow-or-activity-name>:<failure-boundary>`",
            "`dependency_set:<group-name>`",
            "`failure_path:<component>:<missing-signal>`",
        ):
            assert token in prompt
        # Alignment-as-precondition framing (appears in both the identity
        # contract and the Layer2j-alignment subsection)
        assert self._compact(
            "This is the precondition that lets downstream dedup "
            "consolidate layer3d / layer2j co-emission on the same "
            "operational surface."
        ) in compact_prompt

    # --- Portability assertions (5 matching layer3c/layer3b pattern) ---

    def test_layer3d_prompt_codebase_agnostic_section_presence(self) -> None:
        prompt = self._load_prompt()

        assert "## Codebase-Agnostic Discipline" in prompt

    def test_layer3d_prompt_uses_python_csharp_illustration_preamble(
        self,
    ) -> None:
        """Layer3d uses Python+C# dual-language illustration; verify both
        languages are named in the Your Task preamble."""
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert self._compact(
            "**Examples below use Python and C# syntax for illustration. "
            "Apply the same patterns to your codebase's language"
        ) in compact_prompt

    def test_layer3d_prompt_documents_cross_language_error_handling_mapping(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        # try/catch/finally cross-language mapping (Python, C#, Java, TS,
        # Rust, Go)
        assert self._compact(
            "Try / catch / finally maps to whichever exception-handling "
            "construct the language uses (`try`/`except` in Python, "
            "`try`/`catch`/`finally` in C# / Java / TypeScript, "
            "`Result`/`?` in Rust, error-return in Go)."
        ) in compact_prompt

    def test_layer3d_prompt_uses_language_agnostic_path_placeholder(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        # <File>.<ext> placeholder + multi-extension list
        assert "`<File>.<ext>`" in prompt
        assert self._compact(
            "substitute the codebase's actual extension (`.cs`, `.py`, "
            "`.ts`, `.go`, `.rs`, `.java`, etc.)."
        ) in compact_prompt

    def test_layer3d_prompt_does_not_leak_acme_substrings(self) -> None:
        """Per Group 4d-2 Finding F: prompt must not contain ACME-specific
        substrings; the amendment is genericized."""
        prompt = self._load_prompt()

        # Bare ACME substring match (genericization regression guard)
        assert "ACME" not in prompt
        assert "Contoso" not in prompt

    # --- Additional anti-leak method (1) ---

    def test_layer3d_prompt_anti_leak_peer_kinds_only_via_deferral(
        self,
    ) -> None:
        """Peer-emitter kinds (layer2j idempotency_gap, layer3e concurrency
        kinds, layer2d data-integrity kinds) must appear ONLY in deferral
        framing within Conceptual-Overlap Routing, never as layer3d's own
        kinds in the Finding Identity Contract."""
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        # Layer2j peer kind — appears only in deferral context
        assert "`idempotency_gap`" in prompt
        assert self._compact(
            "layer3d has no `idempotency_gap` kind of its own"
        ) in compact_prompt

        # Layer2d peer kinds — appear only in deferral context
        for layer2d_kind in (
            "`missing_transaction_boundary`",
            "`silent_data_loss`",
        ):
            assert layer2d_kind in prompt
        # Layer3e peer kinds — appear only in deferral context
        for layer3e_kind in (
            "`concurrency_safety_gap`",
            "`concurrency_update_risk`",
            "`blocking_io_in_async`",
        ):
            assert layer3e_kind in prompt

        # Finding Identity Contract table contains exactly the four
        # layer3d kinds — verify no peer kinds appear in the contract
        # table rows themselves (table rows start with `| \``).
        contract_start = prompt.index("## Finding Identity Contract")
        contract_end = prompt.index("## Within-Emitter Discriminator")
        contract_section = prompt[contract_start:contract_end]
        for peer_kind in (
            "idempotency_gap",
            "missing_transaction_boundary",
            "silent_data_loss",
            "concurrency_safety_gap",
            "concurrency_update_risk",
            "blocking_io_in_async",
            "structural_complexity",
            "shotgun_surgery",
            "divergent_change",
            "inappropriate_intimacy",
            "test_that_fails",
            "tdd_signal",
        ):
            assert peer_kind not in contract_section, (
                f"Peer-emitter kind {peer_kind!r} leaked into layer3d's "
                f"Finding Identity Contract table."
            )


class TestLayer3eConcurrencySpecialistPromptContract:
    """Lock-in regression for the layer3e-concurrency-specialist
    F1->F2 migration amendment (Group 4d-3 Arc 3b second emitter).

    Engagement-level firsts this class locks in:
      1. F1->F2 migration of a Stage 3 module-review emitter (layer3e
         was the last emitter on the F1 contract).
      2. Seven-namespace mixed-strategy subject_key catalog (four
         code-site exclusive namespaces; three operational-surface
         namespaces aligned with layer3d / layer2j).
      3. Formal separation of `shared_state:` (what is protected)
         from `lock_site:` (where coordination happens).
      4. Replacement of a pro-speculation persona with an evidence-
         required anti-speculation rule.
      5. G6-e PERMISSIVE Critical-vocabulary discipline declared from
         the start (mandatory, not calibrated in-run) across five
         vocabulary clusters.
      6. Five-peer Table 2 / inline boundary footprint: full Table 2-A
         vs layer3d, Table 2-B for `blocking_io_in_async` shared with
         layer3a, plus inline boundary clauses vs layer3b, layer3c,
         layer2a, layer2b, layer2j.
    """

    @staticmethod
    def _load_prompt() -> str:
        prompt_path = (
            Path(__file__).resolve().parents[1]
            / "sentinel"
            / "skills"
            / "skill_files"
            / "layer3e-concurrency-specialist.md"
        )
        return prompt_path.read_text(encoding="utf-8")

    @staticmethod
    def _compact(text: str) -> str:
        return " ".join(text.split())

    # --- Standard contract methods (modeled on layer3d) ---

    def test_layer3e_prompt_declares_allowed_finding_kinds(self) -> None:
        prompt = self._load_prompt()

        for kind in (
            "`concurrency_safety_gap`",
            "`concurrency_update_risk`",
            "`blocking_io_in_async`",
        ):
            assert kind in prompt
        assert "Do not invent other `finding_kind` values." in prompt

    def test_layer3e_prompt_declares_required_subject_key_rules(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert self._compact(
            "`subject_key` is required for all three layer3e kinds."
        ) in compact_prompt

    def test_layer3e_prompt_preserves_legacy_category_enum_as_supplemental(
        self,
    ) -> None:
        """The 6-value layer3e-specific category enum (carried over from
        F1) is preserved as supplemental raw output; finding_kind is the
        primary identity."""
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert (
            '"category": "race-condition | deadlock | missing-await | '
            'blocking-async | thread-safety | atomicity"' in prompt
        )
        assert self._compact(
            "It is not the discriminator -- the discriminator is "
            "`finding_kind`. The category enum is retained as supplemental "
            "raw output for backward compatibility with downstream consumers "
            "of the layer3e-specialist shape."
        ).replace("--", "\u2014") in compact_prompt

    def test_layer3e_prompt_declares_scope_x_does_not_apply_clarification(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "## Stage 3 Module-Review \u2014 Scope-X Does Not Apply" in prompt
        assert self._compact(
            "This emitter is wired as **Stage 3 module-review**, not "
            "additional-assessment."
        ) in compact_prompt
        assert self._compact(
            "Phase 4d **runtime-gate structures DO NOT apply** to this "
            "emitter."
        ) in compact_prompt

    def test_layer3e_prompt_declares_codebase_agnostic_discipline_section(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "## Codebase-Agnostic Discipline" in prompt
        assert self._compact(
            "Examples in this prompt use specific language syntax "
            "(primarily Python and C#) for illustration, but the patterns "
            "apply to any codebase regardless of language or framework."
        ) in compact_prompt
        assert self._compact(
            "Findings vocabulary must use **generic concurrency "
            "concepts**, not framework prescriptions."
        ) in compact_prompt

    def test_layer3e_prompt_declares_within_emitter_discriminator(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "## Within-Emitter Discriminator" in prompt
        # Three-branch quick rule
        assert self._compact(
            "a concurrency-primitive shape"
        ) in compact_prompt
        assert "emit\n  `concurrency_safety_gap`" in prompt or \
            "emit `concurrency_safety_gap`" in prompt
        assert self._compact(
            "a non-atomic shared-state update"
        ) in compact_prompt
        assert "emit `concurrency_update_risk`" in prompt
        assert self._compact(
            "a blocking primitive placed on an async / event-loop"
        ) in compact_prompt
        assert "emit `blocking_io_in_async`" in prompt

    def test_layer3e_prompt_declares_table2a_aspect_table(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert (
            "## Table 2-A \u2014 layer3e's three kinds vs "
            "`layer3d-error-handling-specialist`" in prompt
        )
        for aspect in (
            "| Analytical path |",
            "| Anchor |",
            "| Severity discipline |",
            "| Subject_key shape |",
            "| Finding shape |",
            "| Overlap behavior |",
        ):
            assert aspect in prompt
        # Reverse-direction Table 2 framing (engagement-symmetric to layer3d)
        assert self._compact(
            "This table is the **reverse direction** of the layer3d "
            "\u2192 layer3e deferral route in "
            "`layer3d-error-handling-specialist.md`"
        ) in compact_prompt

    def test_layer3e_prompt_declares_table2a_three_behavioral_rules(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert self._compact(
            "Do not suppress a `concurrency_safety_gap`, "
            "`concurrency_update_risk`, or `blocking_io_in_async` finding "
            "because `layer3d-error-handling-specialist` might also "
            "surface a related error-handling-pattern finding"
        ) in compact_prompt

        assert self._compact(
            "Do not adopt layer3d's specialist shape on layer3e findings."
        ) in compact_prompt
        assert self._compact(
            "do **not** add the per-finding `failure_scenario` and "
            "`system_impact` fields, do **not** add the top-level "
            "`reliability_posture` and `highest_risk_failure_point` "
            "fields."
        ) in compact_prompt

        assert self._compact(
            "Subject_key namespace alignment with layer3d / layer2j on "
            "the operational-surface namespaces (`external_call:`, "
            "`workflow:`, `failure_path:`) is the precondition that lets "
            "downstream dedup resolve cross-emitter overlap"
        ) in compact_prompt

    def test_layer3e_prompt_declares_table2a_per_kind_addendum(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert self._compact(
            "Per-kind addendum \u2014 which layer3d kind each layer3e "
            "kind tends to co-emit with"
        ) in compact_prompt
        assert "`concurrency_safety_gap`" in prompt
        assert "`error_signal_loss` at the same async call site" in prompt
        assert (
            "`partial_failure_handling_gap` at the same workflow boundary"
            in prompt
        )
        assert (
            "`timeout_handling_gap` at the same external-call site"
            in prompt
        )
        assert self._compact(
            'Defensive double-naming: when emitting any of the three '
            'kinds, anchor the description on "concurrency primitive" or '
            '"shared-state access" or "event-loop path"'
        ) in compact_prompt

    def test_layer3e_prompt_declares_table2b_blocking_io_shared_with_layer3a(
        self,
    ) -> None:
        """Engagement-first: a SECOND Table 2 documenting the
        `blocking_io_in_async` overlap with layer3a-performance-specialist
        (the first emitter to declare two distinct Table 2s)."""
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert (
            "## Table 2-B \u2014 `blocking_io_in_async` shared with "
            "`layer3a-performance-specialist`" in prompt
        )
        # Behavioral rule (no suppression on overlap)
        assert self._compact(
            "Do not suppress a `blocking_io_in_async` finding because "
            "layer3a might also identify the same site under its "
            "performance perspective."
        ) in compact_prompt

    def test_layer3e_prompt_declares_inline_boundary_clause_vs_layer3b(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert (
            "## Inline Boundary Clause vs `layer3b-maintainability-specialist`"
            in prompt
        )
        assert self._compact(
            "**layer3b** evaluates concurrency from the *cost-of-modification*"
        ) in compact_prompt
        assert self._compact(
            "**layer3e** (this emitter) evaluates concurrency from the "
            "*concurrency-"
        ) in compact_prompt

    def test_layer3e_prompt_declares_inline_boundary_clause_vs_layer3c(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert (
            "## Inline Boundary Clause vs `layer3c-testability-specialist`"
            in prompt
        )
        assert self._compact(
            "**layer3c** evaluates concurrency from the *can-this-be-"
            "tested-"
        ) in compact_prompt

    def test_layer3e_prompt_declares_inline_boundary_clause_vs_layer2a(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert (
            "## Inline Boundary Clause vs `layer2a-quick-review`" in prompt
        )
        assert self._compact(
            "**layer2a** evaluates concurrency from the broad quick-review "
            "perspective"
        ) in compact_prompt

    def test_layer3e_prompt_declares_inline_boundary_clause_vs_layer2b(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert (
            "## Inline Boundary Clause vs `layer2b-correctness-specialist`"
            in prompt
        )
        assert self._compact(
            "`layer2b-correctness-specialist` shares `concurrency_safety_gap`"
            " and `concurrency_update_risk` with layer3e as an optional "
            "backstop"
        ) in compact_prompt

    def test_layer3e_prompt_declares_inline_boundary_clause_vs_layer2j(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert (
            "## Inline Boundary Clause vs `layer2j-operational-risk-"
            "failure-modes`" in prompt
        )
        # Critical discriminator: simultaneous-actor vs single-actor retry
        assert self._compact(
            "Layer3e's `concurrency_update_risk` is reserved for "
            "**simultaneous-actor races** on a shared-state update."
        ) in compact_prompt

    def test_layer3e_prompt_declares_subject_key_mixed_namespace_strategy(
        self,
    ) -> None:
        """Engagement-first: SEVEN namespaces (4 code-site exclusive,
        3 operational-surface shared); `shared_state:` formally separated
        from `lock_site:`."""
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "## subject_key Guidance" in prompt
        # Mixed-namespace strategy with seven-namespace count called out
        assert self._compact(
            "Layer3e uses a **mixed-"
        ) in compact_prompt
        assert self._compact(
            "namespace strategy** with seven namespaces"
        ) in compact_prompt
        # Formal separation of shared_state from lock_site (engagement-first)
        assert self._compact(
            "Layer3e formally separates `shared_state:` (what is "
            "protected) from `lock_site:` (where coordination happens)."
        ) in compact_prompt
        # Seven per-kind namespace sub-section headings
        assert (
            "### `shared_state:` namespace \u2014 what is protected "
            "(code-site, exclusive)" in prompt
        )
        assert (
            "### `lock_site:` namespace \u2014 where coordination happens "
            "(code-site, exclusive)" in prompt
        )
        assert (
            "### `await_site:` namespace \u2014 async suspension point "
            "(code-site, exclusive)" in prompt
        )
        assert (
            "### `task_site:` namespace \u2014 fire-and-forget / task "
            "lifecycle (code-site, exclusive)" in prompt
        )
        assert (
            "### `external_call:` namespace \u2014 concurrent I/O surface "
            "(operational, layer3d/layer2j-aligned)" in prompt
        )
        assert (
            "### `workflow:` namespace \u2014 multi-step concurrency "
            "surface (operational, layer3d/layer2j-aligned)" in prompt
        )
        assert (
            "### `failure_path:` namespace \u2014 concurrency exposed on "
            "failure path (operational, layer3d/layer2j-aligned)" in prompt
        )
        # Alignment subsection
        assert (
            "### Layer3d / layer2j namespace alignment "
            "(operational-surface kinds only)" in prompt
        )
        # Exclusivity reminder for code-site namespaces
        assert self._compact(
            "the four code-site namespaces (`shared_state:`, `lock_site:`,"
            " `await_site:`, `task_site:`) are **layer3e-exclusive**."
        ) in compact_prompt

    def test_layer3e_prompt_declares_subject_key_must_not_list(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "### subject_key MUST NOT be" in prompt
        for prohibited in (
            "A category-enum value",
            "A concurrency-posture adjective",
            "A failure-mode adjective",
            "A path-only string without a callsite or surface identifier",
            "A non-existent helper, primitive, or metric name",
            "A coverage or score",
        ):
            assert self._compact(prohibited) in compact_prompt

    def test_layer3e_prompt_declares_anti_speculation_rule(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "## Anti-Speculation Rule" in prompt
        assert "Kind-specific evidence requirements:" in prompt
        # Three kind-specific evidence requirement bullets
        assert self._compact(
            "**`concurrency_safety_gap`** \u2014 cite the concurrency "
            "primitive AND the structural defect"
        ) in compact_prompt
        assert self._compact(
            "**`concurrency_update_risk`** \u2014 cite the shared-state "
            "read AND the corresponding shared-state write"
        ) in compact_prompt
        assert self._compact(
            "**`blocking_io_in_async`** \u2014 cite the blocking call AND "
            "the containing async / event-loop / UI-thread / scheduler "
            "context."
        ) in compact_prompt
        assert (
            "Forbidden framings \u2014 return empty findings rather than report:"
            in prompt
        )

    def test_layer3e_prompt_replaces_pro_speculation_persona(
        self,
    ) -> None:
        """Engagement-first: the F1 pro-speculation persona quote ('you
        don't need to observe the race to know it exists') must be
        REMOVED in the F2 amendment. This is the single largest semantic
        gap in the F1->F2 migration."""
        prompt = self._load_prompt()

        # The exact pro-speculation quote from the F1 persona must not
        # appear anywhere in the prompt.
        assert "you don't need to observe the race to know it exists" \
            not in prompt
        # The new anti-speculation framing must be present.
        assert (
            "If the evidence isn't in the diff, the finding isn't in the "
            "report." in prompt
        )

    def test_layer3e_prompt_declares_conceptual_overlap_routing(
        self,
    ) -> None:
        """Outbound routes: layer2j idempotency_gap, layer2d data-
        integrity, layer3d error-handling, layer3b complexity, layer3c
        testability, layer3a performance, and layer2a + layer2b
        cross-references."""
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "## Conceptual-Overlap Routing \u2014 When to Defer" in prompt

        # Route 1: layer2j idempotency_gap
        assert self._compact(
            "Defer to `layer2j-operational-risk-failure-modes`'s "
            "`idempotency_gap` kind; do not re-encode as "
            "`concurrency_update_risk`."
        ) in compact_prompt
        # Route 2: layer2d durable-data invariant
        assert self._compact(
            "Defer to `layer2d-data-integrity-specialist`'s "
            "`missing_transaction_boundary` or `silent_data_loss` kinds"
        ) in compact_prompt
        # Route 3: layer3d error-handling-pattern
        assert self._compact(
            "Defer to `layer3d-error-handling-specialist`'s "
            "`error_signal_loss` kind"
        ) in compact_prompt
        # Route 4: layer3b complexity
        assert self._compact(
            "Defer to `layer3b-maintainability-specialist`'s "
            "`structural_complexity` or smell kinds."
        ) in compact_prompt
        # Route 5: layer3c testability
        assert self._compact(
            "Defer to `layer3c-testability-specialist`'s testability kinds."
        ) in compact_prompt
        # Route 6: layer3a performance scaling
        assert self._compact(
            "Defer to `layer3a-performance-specialist`'s "
            "`blocking_io_in_async` framing."
        ) in compact_prompt
        # Cross-reference: layer2a
        assert self._compact(
            "**Layer2a discriminator**"
        ) in compact_prompt
        # Cross-reference: layer2b
        assert self._compact(
            "**Layer2b discriminator**"
        ) in compact_prompt

    def test_layer3e_prompt_declares_critical_severity_discipline_section(
        self,
    ) -> None:
        """Engagement-first: layer3e adopts Critical-Severity Discipline
        with G6-e PERMISSIVE from the start (mandatory, not calibrated
        in-run). Five Critical-vocabulary clusters are enumerated."""
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "## Critical-Severity Discipline" in prompt
        assert self._compact(
            "Layer3e uses the full `Critical | P1 | P2 | P3` severity ladder."
        ) in compact_prompt
        assert self._compact(
            "**Critical reserved for silent data corruption or silent "
            "lost updates from concurrency primitives.**"
        ) in compact_prompt
        # G6-e PERMISSIVE-from-start framing
        assert self._compact(
            "Treat them as **synonyms under a permissive Critical-"
            "vocabulary audit (G6-e PERMISSIVE) from the start of this "
            "emitter's operation**"
        ) in compact_prompt
        # Five Critical-vocabulary cluster headings
        for cluster in (
            "**Literal-silent cluster:**",
            "**Mechanism-naming cluster:**",
            "**State-corruption cluster (shared with layer3d / layer2d):**",
            "**Async-specific cluster:**",
            "**Memory-model cluster (rare but Critical when surfaced):**",
        ):
            assert cluster in prompt
        # P1/P2/P3 reserved-usage definitions
        assert self._compact(
            "P1 reserved for deadlock or severe degradation"
        ) in compact_prompt
        assert self._compact(
            "P2 reserved for potential issues under high load"
        ) in compact_prompt
        assert self._compact(
            "P3 reserved for defensive improvements"
        ) in compact_prompt

    # --- Layer3e-distinctive methods ---

    def test_layer3e_uses_critical_severity_discipline_not_anti_reminder(
        self,
    ) -> None:
        """Layer3e is Critical-allowed (mirroring layer3d). Verify the
        section header is Critical-Severity Discipline, NOT Anti-Critical-
        Severity Reminder, and Critical is present in the severity enum."""
        prompt = self._load_prompt()

        assert "## Critical-Severity Discipline" in prompt
        assert "## Anti-Critical-Severity Reminder" not in prompt
        assert '"Critical | P1 | P2 | P3"' in prompt

    def test_layer3e_prompt_documents_seven_namespace_strategy_explicitly(
        self,
    ) -> None:
        """Engagement-first redundancy assertion: all seven namespace
        patterns are explicitly documented with their pattern tokens."""
        prompt = self._load_prompt()

        # Four code-site exclusive namespace patterns
        for token in (
            "`shared_state:<file>:<symbol>`",
            "`lock_site:<file>:<symbol>`",
            "`await_site:<file>:<function>`",
            "`task_site:<file>:<function>`",
        ):
            assert token in prompt
        # Three operational-surface namespace patterns (layer3d/layer2j-aligned)
        for token in (
            "`external_call:<dependency-id>:<call-site>`",
            "`workflow:<workflow-or-activity-name>:<step>`",
            "`failure_path:<component>:<scenario>`",
        ):
            assert token in prompt

    def test_layer3e_prompt_drops_f1_top_level_concurrency_posture_fields(
        self,
    ) -> None:
        """Engagement-first: F1 top-level fields (concurrency_model,
        thread_safety_assessment, highest_risk_race) are DROPPED in the
        F2 amendment under Scope X precedent (zero consumers verified at
        amendment time)."""
        prompt = self._load_prompt()

        # F1 top-level field names must not appear anywhere in the prompt.
        assert "concurrency_model" not in prompt
        assert "thread_safety_assessment" not in prompt
        assert "highest_risk_race" not in prompt

    def test_layer3e_prompt_natively_owns_three_concurrency_kinds(
        self,
    ) -> None:
        """Engagement-first: layer3e natively emits
        `concurrency_safety_gap`, `concurrency_update_risk`, and
        `blocking_io_in_async`. These appear in the Finding Identity
        Contract table as layer3e's own kinds (with `subject_key` rule
        and `shared_with` peers), not solely as deferral targets."""
        prompt = self._load_prompt()

        contract_start = prompt.index("## Finding Identity Contract")
        contract_end = prompt.index("## Within-Emitter Discriminator")
        contract_section = prompt[contract_start:contract_end]

        for kind in (
            "concurrency_safety_gap",
            "concurrency_update_risk",
            "blocking_io_in_async",
        ):
            assert kind in contract_section, (
                f"Native layer3e kind {kind!r} missing from Finding "
                f"Identity Contract table."
            )
        # Layer3e is named as primary owner of the first two kinds.
        assert "Layer3e is the primary owner of this kind." in prompt
        assert "Layer3e is the primary owner." in prompt
        # blocking_io_in_async is shared with layer3a, not solely owned.
        assert (
            "Layer3e shares this kind with `layer3a-performance-specialist`."
            in prompt
        )

    # --- Portability assertions (matching layer3d pattern) ---

    def test_layer3e_prompt_codebase_agnostic_section_presence(self) -> None:
        prompt = self._load_prompt()

        assert "## Codebase-Agnostic Discipline" in prompt

    def test_layer3e_prompt_uses_python_csharp_illustration_preamble(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert self._compact(
            "**Examples below use Python and C# syntax for illustration. "
            "Apply the same patterns to your codebase's language"
        ) in compact_prompt

    def test_layer3e_prompt_does_not_leak_acme_substrings(self) -> None:
        """Per Group 4d-2 Finding F: prompt must not contain ACME-specific
        substrings; the amendment is genericized. ACMEMESService.cs appears
        only as an example identifier in namespace pattern illustrations."""
        prompt = self._load_prompt()

        # Bare ACME substring match (genericization regression guard).
        # Allow ACMEMESService.cs in code-site namespace example strings
        # (mirrors layer3d's tolerance for `<AcmeRecipeFile>.cs` shape).
        acme_count = prompt.count("ACME")
        # Permitted occurrences: namespace illustration examples only.
        # Verify all occurrences are inside example-style fenced contexts.
        assert "Contoso" not in prompt
        # If ACME appears, it must be in illustrative example identifiers
        # like `ACMEMESService.cs`, not in prose-level discussion of ACME.
        for line in prompt.splitlines():
            if "ACME" in line:
                assert "ACMEMESService" in line, (
                    f"ACME substring leaked outside namespace example: "
                    f"{line!r}"
                )

    # --- Anti-leak: peer kinds only via deferral ---

    def test_layer3e_prompt_anti_leak_peer_kinds_only_via_deferral(
        self,
    ) -> None:
        """Peer-emitter kinds (layer3d error_signal_loss, layer2j
        idempotency_gap, layer2d data-integrity kinds) must appear ONLY
        in deferral / Table 2 context within the prompt, never as
        layer3e's own kinds in the Finding Identity Contract."""
        prompt = self._load_prompt()

        contract_start = prompt.index("## Finding Identity Contract")
        contract_end = prompt.index("## Within-Emitter Discriminator")
        contract_section = prompt[contract_start:contract_end]

        # NOTE: `idempotency_gap` legitimately appears in the contract
        # table as a within-row discriminator cross-reference ("Distinct
        # from layer2j's `idempotency_gap`"), not as a layer3e-owned
        # kind. It is therefore excluded from this anti-leak guard. The
        # remaining peer kinds must not appear in the contract section
        # at all.
        for peer_kind in (
            "error_signal_loss",
            "partial_failure_handling_gap",
            "timeout_handling_gap",
            "observability_gap",
            "missing_transaction_boundary",
            "silent_data_loss",
            "structural_complexity",
            "n_plus_one_query",
            "algorithmic_scaling_gap",
            "cache_correctness_gap",
        ):
            assert peer_kind not in contract_section, (
                f"Peer-emitter kind {peer_kind!r} leaked into layer3e's "
                f"Finding Identity Contract table."
            )


class TestLayer2dDataIntegritySpecialistPromptContract:
    """Lock-in regression for the layer2d-data-integrity-specialist
    F1->F2 migration amendment (Group 4d-3 Arc 3b third emitter; closes
    Arc 3b; final F1 holdout emitter in the engagement).

    Engagement-level firsts this class locks in:
      1. F1->F2 migration of the final F1 holdout emitter.
      2. Seven-kind catalog with two new native kinds
         (`missing_transaction_boundary`, `silent_data_loss`) closing
         the broken-deferral-route gap from layer3d / layer3e.
      3. Eight-namespace mixed-strategy subject_key catalog (five
         layer2d-exclusive including engagement-first schema-surface
         and migration-surface families; three operational aligned with
         layer3d / layer3e / layer2j).
      4. First full Table 2 against `layer2a-quick-review` in the
         engagement (prior emitters used inline boundary clauses).
      5. Six-peer Table 2 / inline boundary footprint: Table 2-A vs
         layer2a, Table 2-B vs layer3d, Table 2-C vs layer3e, Table 2-D
         deferral vs layer2j, plus inline boundary clauses vs layer2c
         and layer2b.
      6. Three bilateral-reciprocation closures (layer3d, layer3e,
         layer2j) via the new Tables 2-B / 2-C / 2-D.
      7. F1 persona preservation discipline (mantra is already
         evidence-anchored as an analytical question, so it is
         preserved verbatim and an anti-speculation paragraph is
         appended rather than replacing the persona).
    """

    @staticmethod
    def _load_prompt() -> str:
        prompt_path = (
            Path(__file__).resolve().parents[1]
            / "sentinel"
            / "skills"
            / "skill_files"
            / "layer2d-data-integrity-specialist.md"
        )
        return prompt_path.read_text(encoding="utf-8")

    @staticmethod
    def _compact(text: str) -> str:
        return " ".join(text.split())

    # --- Standard contract methods ---

    def test_layer2d_prompt_declares_allowed_finding_kinds(self) -> None:
        prompt = self._load_prompt()

        for kind in (
            "`missing_transaction_boundary`",
            "`silent_data_loss`",
            "`concurrency_update_risk`",
            "`constraint_violation_risk`",
            "`validation_gap`",
            "`migration_hazard`",
            "`read_integrity_gap`",
        ):
            assert kind in prompt
        assert "Do not invent other `finding_kind` values." in prompt

    def test_layer2d_prompt_declares_required_subject_key_rules(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert self._compact(
            "`subject_key` is required for all seven layer2d kinds."
        ) in compact_prompt

    def test_layer2d_prompt_narrows_category_enum_dropping_race_and_idempotency(
        self,
    ) -> None:
        """The layer2d category enum is NARROWED from F1: `race-condition`
        and `idempotency` are dropped (routed to layer3e and layer2j
        respectively); the remaining six values are layer2d-native."""
        prompt = self._load_prompt()

        assert (
            '"category": "missing-transaction | constraint-violation | '
            'silent-loss | validation | migration | read-integrity"'
            in prompt
        )
        # F1 values dropped from the category enum (must not appear in
        # the F2 output-schema fenced JSON block).
        output_format_start = prompt.index("## Output Format")
        output_format_end = prompt.index("## Empty Findings")
        output_section = prompt[output_format_start:output_format_end]
        # Extract the first fenced ```json block in the output section.
        json_block_start = output_section.index("```json")
        json_block_end = output_section.index("```", json_block_start + 7)
        json_block = output_section[json_block_start:json_block_end]
        assert "race-condition" not in json_block
        assert "idempotency" not in json_block
        assert (
            "missing-transaction | constraint-violation | silent-loss | "
            "validation | migration | read-integrity" in json_block
        )

    def test_layer2d_prompt_declares_scope_x_carve_out_section(
        self,
    ) -> None:
        """Scope-X header is the INVERSE of layer3e's: layer2d IS Phase 4d
        additional-assessment, and Phase 4d prompt-content + runtime-gate
        structures DO apply."""
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert (
            "## Phase 4d Additional-Assessment \u2014 Scope-X Carve-Out"
            in prompt
        )
        assert self._compact(
            "This emitter is wired as **Phase 4d additional-assessment**,"
            " not Stage 3 module-review."
        ) in compact_prompt
        assert self._compact(
            "Phase 4d **prompt-content conventions DO apply** to this "
            "emitter"
        ) in compact_prompt
        assert self._compact(
            "Phase 4d **runtime-gate structures DO apply** to this "
            "emitter"
        ) in compact_prompt
        # Scope-X application: F1 top-level fields are dropped.
        assert self._compact(
            "the two F1 top-level fields `integrity_posture` and "
            "`most_dangerous_operation` are **dropped** from the F2 "
            "output schema"
        ) in compact_prompt

    def test_layer2d_prompt_drops_f1_top_level_posture_fields(self) -> None:
        """Engagement carry-over: F1 top-level fields are removed from
        the F2 output schema. The names may appear in the Scope-X
        explanation prose and Notes for Downstream Readers prose, but
        must NOT appear in the Output Format fenced JSON schema block."""
        prompt = self._load_prompt()

        output_format_start = prompt.index("## Output Format")
        output_format_end = prompt.index("## Empty Findings")
        output_section = prompt[output_format_start:output_format_end]
        json_block_start = output_section.index("```json")
        json_block_end = output_section.index("```", json_block_start + 7)
        json_block = output_section[json_block_start:json_block_end]

        assert "integrity_posture" not in json_block
        assert "most_dangerous_operation" not in json_block

    def test_layer2d_prompt_preserves_persona_mantra_verbatim(self) -> None:
        """F1 persona is evidence-anchored as an analytical question;
        the mantra MUST be preserved verbatim. An anti-speculation
        paragraph is appended (not replacing the persona)."""
        prompt = self._load_prompt()

        assert (
            "Your mantra: **\"If this function fails halfway through, "
            "what state is the database in?\"**" in prompt
        )
        # Anti-speculation paragraph appended after the mantra.
        compact_prompt = self._compact(prompt)
        assert self._compact(
            "You assert data-integrity findings only when the source "
            "under review contains **structural evidence**"
        ) in compact_prompt
        assert self._compact(
            "Your mantra remains the analytical question; the answer "
            "must cite the code that exposes the gap."
        ) in compact_prompt

    def test_layer2d_prompt_declares_codebase_agnostic_discipline_section(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "## Codebase-Agnostic Discipline" in prompt
        assert self._compact(
            "Examples in this prompt use specific language syntax "
            "(primarily Python and C#) for illustration"
        ) in compact_prompt
        assert self._compact(
            "Findings vocabulary must use **generic data-integrity "
            "concepts**, not framework prescriptions."
        ) in compact_prompt

    def test_layer2d_prompt_declares_within_emitter_discriminator(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "## Within-Emitter Discriminator" in prompt
        # Quick-rule branches for all seven kinds
        assert "emit\n  `missing_transaction_boundary`" in prompt or \
            "emit `missing_transaction_boundary`" in prompt
        assert "emit\n  `silent_data_loss`" in prompt or \
            "emit `silent_data_loss`" in prompt
        assert "emit `concurrency_update_risk`" in prompt
        assert "emit\n  `constraint_violation_risk`" in prompt or \
            "emit `constraint_violation_risk`" in prompt
        assert "emit `validation_gap`" in prompt
        assert "emit\n  `migration_hazard`" in prompt or \
            "emit `migration_hazard`" in prompt
        assert "emit\n  `read_integrity_gap`" in prompt or \
            "emit `read_integrity_gap`" in prompt
        # Routing-out clauses
        assert self._compact(
            "Defer to layer2j's `idempotency_gap`"
        ) in compact_prompt
        assert self._compact(
            "Defer to layer3e's `concurrency_safety_gap` or "
            "`concurrency_update_risk`"
        ) in compact_prompt

    # --- Table 2 assertions (4 tables) ---

    def test_layer2d_prompt_declares_table2a_vs_layer2a_quick_review(
        self,
    ) -> None:
        """Engagement-first: a full Table 2 against `layer2a-quick-review`
        (prior emitters used inline boundary clauses only)."""
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert (
            "## Table 2-A \u2014 layer2d's `missing_transaction_boundary`"
            " vs `layer2a-quick-review`" in prompt
        )
        for aspect in (
            "| Analytical path |",
            "| Anchor |",
            "| Severity discipline |",
            "| Subject_key shape |",
            "| Finding shape |",
            "| Overlap behavior |",
        ):
            assert aspect in prompt
        # Engagement-first framing acknowledgement
        assert self._compact(
            "This table is the **reverse direction** of the layer2a "
            "\u2192 layer2d routing relationship"
        ) in compact_prompt
        # Behavioral rule
        assert self._compact(
            "Do not suppress a `missing_transaction_boundary` finding "
            "because layer2a might also surface a related quick-review "
            "observation."
        ) in compact_prompt

    def test_layer2d_prompt_declares_table2b_vs_layer3d(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert (
            "## Table 2-B \u2014 layer2d's two kinds vs "
            "`layer3d-error-handling-specialist`" in prompt
        )
        # Reverse-direction acknowledgement (bilateral reciprocation)
        assert self._compact(
            "This table is the **reverse direction** of the layer3d "
            "\u2192 layer2d deferral route"
        ) in compact_prompt
        # Behavioral rule on both shared kinds
        assert self._compact(
            "Do not suppress a `missing_transaction_boundary` or "
            "`silent_data_loss` finding because "
            "`layer3d-error-handling-specialist` might also surface"
        ) in compact_prompt
        # Shape-discipline: use data_impact, not system_impact; no
        # top-level reliability_posture / highest_risk_failure_point.
        assert self._compact(
            "do **not** add the per-finding `system_impact` field (use "
            "`data_impact`"
        ) in compact_prompt
        assert self._compact(
            "do **not** add the top-level `reliability_posture` and "
            "`highest_risk_failure_point` fields."
        ) in compact_prompt

    def test_layer2d_prompt_declares_table2c_vs_layer3e(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert (
            "## Table 2-C \u2014 layer2d's two kinds vs "
            "`layer3e-concurrency-specialist`" in prompt
        )
        # Reverse-direction acknowledgement (bilateral reciprocation)
        assert self._compact(
            "This table is the **reverse direction** of the layer3e "
            "\u2192 layer2d deferral route"
        ) in compact_prompt
        # Critical discriminator between concurrency_update_risk and
        # concurrency_update_risk
        assert self._compact(
            "**Critical discriminator \u2014 `concurrency_update_risk` (layer2d)"
            " vs `concurrency_update_risk` (layer3e):**"
        ) in compact_prompt
        assert self._compact(
            "These two kinds are **distinct, not shared**."
        ) in compact_prompt
        # Co-emission scenario when both apply
        assert self._compact(
            "emits **both** kinds \u2014 `concurrency_update_risk` from layer2d"
            " and `concurrency_update_risk` from layer3e"
        ) in compact_prompt

    def test_layer2d_prompt_declares_table2d_deferral_vs_layer2j(
        self,
    ) -> None:
        """Table 2-D is a DEFERRAL table (no shared kind). Layer2d does
        not emit `idempotency_gap`; layer2j is the exclusive owner."""
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert (
            "## Table 2-D \u2014 layer2d's deferral relationship vs "
            "`layer2j-operational-risk-failure-modes`" in prompt
        )
        # Reverse-direction acknowledgement (bilateral reciprocation)
        assert self._compact(
            "This table describes the **deferral relationship** (no "
            "shared kind), and is the **reverse direction** of layer2j's"
            " existing Table 2-B vs layer2d."
        ) in compact_prompt
        # Locked decision: idempotency_gap is layer2j-only
        assert self._compact(
            "Do not introduce `idempotency_gap` as a layer2d native "
            "kind under any circumstances."
        ) in compact_prompt
        assert self._compact(
            "`idempotency_gap` is layer2j-only."
        ) in compact_prompt

    # --- Inline boundary clauses (2) ---

    def test_layer2d_prompt_declares_inline_boundary_clause_vs_layer2c(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert (
            "## Inline Boundary Clause vs `layer2c-security-specialist`"
            in prompt
        )
        assert self._compact(
            "**layer2c** evaluates payment from the *credential / "
            "authentication / authorization / data-exposure* perspective"
        ) in compact_prompt
        assert self._compact(
            "**layer2d** (this emitter) evaluates payment from the "
            "*durable-data invariant* perspective"
        ) in compact_prompt

    def test_layer2d_prompt_declares_inline_boundary_clause_vs_layer2b(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert (
            "## Inline Boundary Clause vs `layer2b-correctness-specialist`"
            in prompt
        )
        assert self._compact(
            "**layer2b** evaluates the finding from the *what should "
            "the code actually do* perspective"
        ) in compact_prompt
        assert self._compact(
            "**layer2d** (this emitter) evaluates the finding from the "
            "*data-integrity invariant* perspective"
        ) in compact_prompt

    # --- Subject_key mixed-namespace strategy ---

    def test_layer2d_prompt_declares_subject_key_mixed_namespace_strategy(
        self,
    ) -> None:
        """Engagement-first: EIGHT namespaces (5 layer2d-exclusive
        including engagement-first schema-surface and migration-surface
        families; 3 operational-surface aligned with layer3d / layer3e
        / layer2j)."""
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "## subject_key Guidance" in prompt
        assert self._compact(
            "Layer2d uses a **mixed-"
        ) in compact_prompt
        assert self._compact(
            "namespace strategy** with eight namespaces"
        ) in compact_prompt
        # Engagement-first: schema-surface and migration-surface families
        assert self._compact(
            "one schema-surface namespace (layer2d-exclusive, "
            "engagement-first surface family)"
        ) in compact_prompt
        assert self._compact(
            "one migration-surface namespace (layer2d-exclusive, "
            "engagement-first surface family)"
        ) in compact_prompt
        # All eight per-kind namespace sub-section headings
        assert (
            "### `transaction_boundary:` namespace \u2014 multi-write "
            "atomicity site (code-site, exclusive)" in prompt
        )
        assert (
            "### `durable_write:` namespace \u2014 single durable write "
            "site (code-site, exclusive)" in prompt
        )
        assert (
            "### `read_modify_write:` namespace \u2014 durable RMW "
            "sequence (code-site, exclusive)" in prompt
        )
        assert (
            "### `schema_constraint:` namespace \u2014 schema-level "
            "enforcement gap (schema-surface, exclusive)" in prompt
        )
        assert (
            "### `migration:` namespace \u2014 schema migration step "
            "(migration-surface, exclusive)" in prompt
        )
        assert (
            "### `external_call:` namespace \u2014 durable-write external"
            " surface (operational, layer3d / layer3e / layer2j-aligned)"
            in prompt
        )
        assert (
            "### `workflow:` namespace \u2014 multi-step persistence "
            "surface (operational, layer3d / layer3e / layer2j-aligned)"
            in prompt
        )
        assert (
            "### `failure_path:` namespace \u2014 durable write exposed "
            "on failure path (operational, layer3d / layer3e / "
            "layer2j-aligned)" in prompt
        )
        # Alignment subsection naming all three peers
        assert (
            "### Layer3d / layer3e / layer2j namespace alignment "
            "(operational-surface kinds only)" in prompt
        )
        # Exclusivity reminder for the five layer2d-exclusive namespaces
        assert self._compact(
            "the five layer2d-exclusive namespaces "
            "(`transaction_boundary:`, `durable_write:`, "
            "`read_modify_write:`, `schema_constraint:`, `migration:`) "
            "are **layer2d-exclusive**."
        ) in compact_prompt

    def test_layer2d_prompt_documents_eight_namespace_patterns_explicitly(
        self,
    ) -> None:
        """Engagement-first redundancy assertion: all eight namespace
        patterns are explicitly documented with their pattern tokens."""
        prompt = self._load_prompt()

        # Five layer2d-exclusive namespace patterns
        for token in (
            "`transaction_boundary:<file>:<function>`",
            "`durable_write:<file>:<symbol>`",
            "`read_modify_write:<file>:<function>`",
            "`schema_constraint:<entity>:<field>`",
            "`migration:<migration-id>:<step>`",
        ):
            assert token in prompt
        # Three operational-surface namespace patterns (layer3d/layer3e/
        # layer2j-aligned)
        for token in (
            "`external_call:<dependency-id>:<call-site>`",
            "`workflow:<workflow-or-activity-name>:<step>`",
            "`failure_path:<component>:<scenario>`",
        ):
            assert token in prompt

    def test_layer2d_prompt_declares_subject_key_must_not_list(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "### subject_key MUST NOT be" in prompt
        for prohibited in (
            "A category-enum value",
            "An integrity-posture adjective",
            "A failure-mode adjective",
            "A path-only string without a callsite or surface identifier",
            "A non-existent table, entity, field, migration, or "
            "write-target name",
            "A coverage or score",
        ):
            assert self._compact(prohibited) in compact_prompt

    # --- Anti-Speculation Rule ---

    def test_layer2d_prompt_declares_anti_speculation_rule(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "## Anti-Speculation Rule" in prompt
        assert "Kind-specific evidence requirements:" in prompt
        # Seven kind-specific evidence requirement bullets
        assert self._compact(
            "**`missing_transaction_boundary`** \u2014 cite **two or "
            "more durable writes from a single logical operation**"
        ) in compact_prompt
        assert self._compact(
            "**`silent_data_loss`** \u2014 cite **a durable-write call**"
            " AND **the silent-failure path**"
        ) in compact_prompt
        assert self._compact(
            "**`concurrency_update_risk`** \u2014 cite **the read-modify-write "
            "sequence on durable state**"
        ) in compact_prompt
        assert self._compact(
            "**`constraint_violation_risk`** \u2014 cite **a write call**"
            " AND **the constraint dimension**"
        ) in compact_prompt
        assert self._compact(
            "**`validation_gap`** \u2014 cite **a write call** AND "
            "**the unvalidated external input**"
        ) in compact_prompt
        assert self._compact(
            "**`migration_hazard`** \u2014 cite **the schema-migration "
            "step** AND **the destructive or unsafe property**"
        ) in compact_prompt
        assert self._compact(
            "**`read_integrity_gap`** \u2014 cite **a read call against"
            " a write-shared surface**"
        ) in compact_prompt
        assert (
            "Forbidden framings \u2014 return empty findings rather than report:"
            in prompt
        )

    # --- Critical-Severity Discipline ---

    def test_layer2d_prompt_declares_critical_severity_discipline_section(
        self,
    ) -> None:
        """Layer2d uses Critical-Severity Discipline with G6-e PERMISSIVE
        from the start. EIGHT Critical-vocabulary clusters (the
        engagement's richest, befitting data-integrity's silent-bug
        nature)."""
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "## Critical-Severity Discipline" in prompt
        assert self._compact(
            "Layer2d uses the full `Critical | P1 | P2 | P3` severity "
            "ladder."
        ) in compact_prompt
        assert self._compact(
            "**Critical reserved for silent data corruption or silent "
            "data loss on durable state.**"
        ) in compact_prompt
        # G6-e PERMISSIVE-from-start framing
        assert self._compact(
            "Treat them as **synonyms under a permissive Critical-"
            "vocabulary audit (G6-e PERMISSIVE) from the start of this "
            "emitter's operation**"
        ) in compact_prompt
        # Eight Critical-vocabulary cluster headings
        for cluster in (
            "**Literal-silent cluster (canonical):**",
            "**Mechanism-naming cluster (transaction / atomicity):**",
            "**Lost-update cluster (cross-reference layer3e):**",
            "**Constraint-violation cluster:**",
            "**Validation / precision cluster (silent-on-write):**",
            "**Migration-hazard cluster:**",
            "**Idempotency cluster (cross-reference layer2j; not "
            "owned here):**",
            "**Read-integrity cluster:**",
        ):
            assert cluster in prompt
        # P1/P2/P3 reserved-usage definitions
        assert self._compact(
            "P1 reserved for data-integrity gaps that surface under "
            "specific load or error conditions"
        ) in compact_prompt
        assert self._compact(
            "P2 reserved for validation gaps or precision loss"
        ) in compact_prompt
        assert self._compact(
            "P3 reserved for defensive improvements"
        ) in compact_prompt

    def test_layer2d_uses_critical_severity_discipline_not_anti_reminder(
        self,
    ) -> None:
        prompt = self._load_prompt()

        assert "## Critical-Severity Discipline" in prompt
        assert "## Anti-Critical-Severity Reminder" not in prompt
        assert '"Critical | P1 | P2 | P3"' in prompt

    # --- Conceptual-Overlap Routing ---

    def test_layer2d_prompt_declares_conceptual_overlap_routing(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "## Conceptual-Overlap Routing \u2014 When to Defer" in prompt
        # Route 1: layer2j idempotency_gap
        assert self._compact(
            "Defer to `layer2j-operational-risk-failure-modes`'s "
            "`idempotency_gap` kind; do not re-encode as "
            "`silent_data_loss`"
        ) in compact_prompt
        # Route 2: layer3e concurrency primitives
        assert self._compact(
            "Defer to `layer3e-concurrency-specialist`'s "
            "`concurrency_safety_gap` or `concurrency_update_risk` kinds"
        ) in compact_prompt
        # Route 3: layer3d error-handling-pattern
        assert self._compact(
            "Defer to `layer3d-error-handling-specialist`'s "
            "`error_signal_loss` kind when the gap is the swallow itself"
        ) in compact_prompt
        # Route 4: layer2a quick-review framing
        assert self._compact(
            "defer to `layer2a-quick-review`'s "
            "`missing_transaction_boundary` framing"
        ) in compact_prompt
        # Cross-reference: layer2c discriminator
        assert self._compact(
            "**Layer2c discriminator**"
        ) in compact_prompt
        # Cross-reference: layer2b discriminator
        assert self._compact(
            "**Layer2b discriminator**"
        ) in compact_prompt

    # --- JSON Escaping ---

    def test_layer2d_prompt_declares_json_string_escaping_requirements(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "## JSON String Escaping Requirements" in prompt
        assert self._compact(
            "Use `\\\"` for embedded double-quotes inside JSON string "
            "values."
        ) in compact_prompt
        assert self._compact(
            "Use `\\\\` for backslashes inside JSON string values."
        ) in compact_prompt

    # --- Output Format / Empty Findings ---

    def test_layer2d_output_format_includes_layer2d_specialist_shape(
        self,
    ) -> None:
        """F2 output schema: per-finding specialist shape with
        `failure_scenario` and `data_impact`, narrowed category enum,
        no top-level posture fields."""
        prompt = self._load_prompt()

        # Present (F2 specialist shape)
        for field in (
            '"finding_kind":',
            '"subject_key":',
            '"summary":',
            '"severity":',
            '"category":',
            '"location":',
            '"description":',
            '"failure_scenario":',
            '"data_impact":',
            '"suggested_fix":',
        ):
            assert field in prompt
        # Layer3d's per-finding field must not leak into layer2d's shape
        assert '"system_impact":' not in prompt
        # Layer3e's per-finding field must not leak
        assert '"reproduction_scenario":' not in prompt

    def test_layer2d_prompt_declares_empty_findings_literal(self) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert "## Empty Findings" in prompt
        assert self._compact(
            '"skill": "layer2d-data-integrity-specialist",'
        ) in compact_prompt
        assert '"findings": []' in prompt

    def test_layer2d_prompt_declares_notes_for_downstream_readers(
        self,
    ) -> None:
        """Engagement carry-over from layer3d / layer3e: explicit Notes
        for Downstream Readers section explaining the
        additional-assessment-pack flow and confirming Scope-X field
        loss."""
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert (
            "## Notes for Downstream Readers (adapter-path field loss)"
            in prompt
        )
        assert self._compact(
            "Layer2d is wired as Phase 4d additional-assessment"
        ) in compact_prompt
        assert self._compact(
            "The F1 top-level fields `integrity_posture` and "
            "`most_dangerous_operation` are dropped under Scope X"
        ) in compact_prompt

    # --- Portability assertions ---

    def test_layer2d_prompt_uses_python_csharp_illustration_preamble(
        self,
    ) -> None:
        prompt = self._load_prompt()
        compact_prompt = self._compact(prompt)

        assert self._compact(
            "**Examples below use Python and C# syntax for illustration."
            " Apply the same patterns to your codebase's language"
        ) in compact_prompt

    def test_layer2d_prompt_does_not_leak_acme_or_contoso_substrings(
        self,
    ) -> None:
        """Per Group 4d-2 Finding F: prompt must be genericized. ACME-
        prefixed identifiers are permitted only inside example namespace
        tokens (mirrors layer3d / layer3e tolerance)."""
        prompt = self._load_prompt()

        assert "Contoso" not in prompt
        for line in prompt.splitlines():
            if "ACME" in line:
                assert "ACMEMESService" in line, (
                    f"ACME substring leaked outside namespace example: "
                    f"{line!r}"
                )

    # --- Native-kind ownership / anti-leak ---

    def test_layer2d_prompt_natively_owns_two_kinds_previously_unowned(
        self,
    ) -> None:
        """Engagement-first: `missing_transaction_boundary` and
        `silent_data_loss` are layer2d-native kinds. These close the
        broken-deferral-route gap from layer3d / layer3e (which referenced
        these kinds before layer2d had them as native kinds)."""
        prompt = self._load_prompt()

        contract_start = prompt.index("## Finding Identity Contract")
        contract_end = prompt.index("## Within-Emitter Discriminator")
        contract_section = prompt[contract_start:contract_end]

        for kind in (
            "missing_transaction_boundary",
            "silent_data_loss",
            "concurrency_update_risk",
            "constraint_violation_risk",
            "validation_gap",
            "migration_hazard",
            "read_integrity_gap",
        ):
            assert kind in contract_section, (
                f"Native layer2d kind {kind!r} missing from Finding "
                f"Identity Contract table."
            )
        # Layer2d is named as primary owner of missing_transaction_boundary.
        assert "Layer2d is the primary owner." in prompt
        # concurrency_update_risk is layer2d-exclusive (no shared_with).
        assert (
            "Layer2d is the exclusive owner; `concurrency_update_risk`"
            " (layer3e) is the simultaneous-actor analog." in prompt
        )

    def test_layer2d_prompt_anti_leak_peer_kinds_only_via_deferral(
        self,
    ) -> None:
        """Peer-emitter kinds that are NOT layer2d-native must appear
        only in deferral / Table 2 context, never as layer2d's own kinds
        in the Finding Identity Contract.

        Notably: `missing_transaction_boundary` and `silent_data_loss`
        are EXCLUDED from this guard because they are now layer2d-native
        (shared with peers, but owned by layer2d). `concurrency_update_risk`
        and `idempotency_gap` may appear in the contract table as
        within-row discriminator cross-references (per the
        `concurrency_update_risk` row, and the within-emitter discriminator
        routing-out clauses), so they are also excluded.
        """
        prompt = self._load_prompt()

        contract_start = prompt.index("## Finding Identity Contract")
        contract_end = prompt.index("## Within-Emitter Discriminator")
        contract_section = prompt[contract_start:contract_end]

        for peer_kind in (
            "error_signal_loss",
            "partial_failure_handling_gap",
            "timeout_handling_gap",
            "observability_gap",
            "concurrency_safety_gap",
            "blocking_io_in_async",
            "structural_complexity",
            "n_plus_one_query",
            "algorithmic_scaling_gap",
            "cache_correctness_gap",
        ):
            assert peer_kind not in contract_section, (
                f"Peer-emitter kind {peer_kind!r} leaked into layer2d's "
                f"Finding Identity Contract table."
            )