from __future__ import annotations

import os
from unittest.mock import patch

from sentinel.config import Config


def test_config_accepts_custom_environment_mapping() -> None:
    env = {
        "GITHUB_TOKEN": "token-from-mapping",
        "SENTINEL_REPO_PATH": "/tmp/custom-repo",
        "SENTINEL_MODEL": "openai/gpt-4.1",
    }
    cfg = Config(env=env, load_dotenv_file=False)

    assert cfg.github_token == "token-from-mapping"
    assert cfg.model == "openai/gpt-4.1"


def test_config_defaults_to_os_environ_when_no_mapping_provided() -> None:
    with patch.dict(
        "os.environ",
        {
            "GITHUB_TOKEN": "token-from-os",
            "SENTINEL_REPO_PATH": "/tmp/os-repo",
        },
        clear=False,
    ):
        cfg = Config(load_dotenv_file=False)

    assert cfg.github_token == "token-from-os"


def test_config_isolated_from_global_state_with_custom_mapping() -> None:
    with patch.dict("os.environ", {"GITHUB_TOKEN": "token-in-os"}, clear=False):
        env = {
            "GITHUB_TOKEN": "token-in-mapping",
            "SENTINEL_REPO_PATH": "/tmp/custom-repo",
        }
        before = dict(os.environ)
        cfg = Config(env=env, load_dotenv_file=False)
        after = dict(os.environ)

    assert cfg.github_token == "token-in-mapping"
    assert before == after


def test_no_test_requires_os_environ_mutation() -> None:
    env = {
        "GITHUB_TOKEN": "token",
        "SENTINEL_REPO_PATH": "/tmp/repo",
        "SENTINEL_MAX_TOKENS": "4096",
    }
    cfg = Config(env=env, load_dotenv_file=False)
    assert cfg.max_tokens == 4096


def test_config_reads_auto_approve_without_review_flag() -> None:
    env = {
        "GITHUB_TOKEN": "token",
        "SENTINEL_REPO_PATH": "/tmp/repo",
        "SENTINEL_AUTO_APPROVE_WITHOUT_REVIEW": "true",
    }
    cfg = Config(env=env, load_dotenv_file=False)
    assert cfg.auto_approve_without_review is True


def test_config_reads_single_approver_policy_flags() -> None:
    env = {
        "SENTINEL_REPO_PATH": "/tmp/repo",
        "SENTINEL_ENFORCE_SINGLE_PR_APPROVER": "true",
        "SENTINEL_REQUIRED_PR_APPROVER": "project-owner",
    }
    cfg = Config(env=env, load_dotenv_file=False)
    assert cfg.enforce_single_pr_approver is True
    assert cfg.required_pr_approver == "project-owner"


def test_config_rejects_invalid_integer_env_value() -> None:
    env = {
        "GITHUB_TOKEN": "token",
        "SENTINEL_REPO_PATH": "/tmp/repo",
        "SENTINEL_MAX_TOKENS": "not-an-int",
    }

    try:
        Config(env=env, load_dotenv_file=False)
        assert False, "Expected EnvironmentError for invalid integer env value"
    except EnvironmentError as exc:
        assert "SENTINEL_MAX_TOKENS" in str(exc)


def test_config_blocks_debug_logging_unless_explicitly_allowed() -> None:
    env = {
        "GITHUB_TOKEN": "token",
        "SENTINEL_REPO_PATH": "/tmp/repo",
        "SENTINEL_LOG_LEVEL": "DEBUG",
    }
    cfg = Config(env=env, load_dotenv_file=False)
    assert cfg.log_level == "INFO"


def test_config_local_only_mode_is_disabled_by_default() -> None:
    env = {
        "SENTINEL_REPO_PATH": "/tmp/repo",
    }
    cfg = Config(env=env, load_dotenv_file=False)
    assert cfg.local_only_mode is False


def test_provider_override_openai_is_honoured() -> None:
    env = {
        "SENTINEL_PROVIDER": "openai",
        "OPENAI_API_KEY": "sk-test",
        "SENTINEL_REPO_PATH": "/tmp/repo",
    }
    cfg = Config(env=env, load_dotenv_file=False)
    assert cfg.provider == "openai"


def test_unknown_provider_override_falls_back_to_auto_detection() -> None:
    env = {
        "SENTINEL_PROVIDER": "not-a-provider",
        "ANTHROPIC_API_KEY": "sk-ant-test",
        "SENTINEL_REPO_PATH": "/tmp/repo",
    }
    cfg = Config(env=env, load_dotenv_file=False)
    assert cfg.provider == "anthropic"


def test_provider_auto_detects_openai_key() -> None:
    env = {
        "OPENAI_API_KEY": "sk-test",
        "SENTINEL_REPO_PATH": "/tmp/repo",
    }
    cfg = Config(env=env, load_dotenv_file=False)
    assert cfg.provider == "openai"


def test_provider_prefers_anthropic_key_over_openai_key() -> None:
    env = {
        "ANTHROPIC_API_KEY": "sk-ant-test",
        "OPENAI_API_KEY": "sk-test",
        "SENTINEL_REPO_PATH": "/tmp/repo",
    }
    cfg = Config(env=env, load_dotenv_file=False)
    assert cfg.provider == "anthropic"
    assert cfg.model == "claude-sonnet-4-6"


def test_provider_is_empty_when_no_api_keys_present() -> None:
    env = {
        "SENTINEL_REPO_PATH": "/tmp/repo",
    }
    cfg = Config(env=env, load_dotenv_file=False)
    assert cfg.provider == ""


def test_provider_override_copilot_cli_is_honoured() -> None:
    env = {
        "SENTINEL_PROVIDER": "copilot-cli",
        "SENTINEL_REPO_PATH": "/tmp/repo",
    }
    cfg = Config(env=env, load_dotenv_file=False)
    assert cfg.provider == "copilot-cli"
    assert cfg.model == "gpt-5.1"


def test_provider_github_models_without_opt_in_is_blocked() -> None:
    env = {
        "SENTINEL_PROVIDER": "github-models",
        "GITHUB_TOKEN": "ghp_test",
        "SENTINEL_REPO_PATH": "/tmp/repo",
    }
    cfg = Config(env=env, load_dotenv_file=False)
    assert cfg.provider == ""


def test_provider_github_models_with_opt_in_and_token() -> None:
    env = {
        "SENTINEL_PROVIDER": "github-models",
        "SENTINEL_ALLOW_GITHUB_MODELS": "true",
        "GITHUB_TOKEN": "ghp_test",
        "SENTINEL_REPO_PATH": "/tmp/repo",
    }
    cfg = Config(env=env, load_dotenv_file=False)
    assert cfg.provider == "github-models"
    assert cfg.model == "gpt-4o-mini"


def test_default_model_for_openai_override_is_gpt_5_1() -> None:
    env = {
        "SENTINEL_PROVIDER": "openai",
        "SENTINEL_REPO_PATH": "/tmp/repo",
    }
    cfg = Config(env=env, load_dotenv_file=False)
    assert cfg.model == "gpt-5.1"


def test_sentinel_model_overrides_provider_default() -> None:
    env = {
        "SENTINEL_PROVIDER": "anthropic",
        "SENTINEL_MODEL": "claude-opus-4-1",
        "SENTINEL_REPO_PATH": "/tmp/repo",
    }
    cfg = Config(env=env, load_dotenv_file=False)
    assert cfg.model == "claude-opus-4-1"


def test_config_reads_review_completeness_gate_flag() -> None:
    env = {
        "SENTINEL_PROVIDER": "openai",
        "SENTINEL_REPO_PATH": "/tmp/repo",
        "SENTINEL_ENFORCE_REVIEW_COMPLETENESS_GATE": "true",
    }
    cfg = Config(env=env, load_dotenv_file=False)
    assert cfg.enforce_review_completeness_gate is True


def test_config_reads_full_coverage_gate_settings() -> None:
    env = {
        "SENTINEL_PROVIDER": "openai",
        "SENTINEL_REPO_PATH": "/tmp/repo",
        "SENTINEL_ENFORCE_FULL_COVERAGE_GATE": "true",
        "SENTINEL_FULL_COVERAGE_THRESHOLD_PCT": "99.5",
    }
    cfg = Config(env=env, load_dotenv_file=False)
    assert cfg.enforce_full_coverage_gate is True
    assert cfg.full_coverage_threshold_pct == 99.5


def test_config_reads_retrieval_packaging_policy_env_vars() -> None:
    env = {
        "SENTINEL_REPO_PATH": "/tmp/repo",
        "SENTINEL_RETRIEVAL_MAX_SNIPPETS_PER_SKILL": "3",
        "SENTINEL_RETRIEVAL_MAX_CHARS_PER_SNIPPET": "800",
    }
    cfg = Config(env=env, load_dotenv_file=False)
    assert cfg.retrieval_max_snippets_per_skill == 3
    assert cfg.retrieval_max_chars_per_snippet == 800


def test_strict_quality_mode_forces_full_quality_flags() -> None:
    env = {
        "SENTINEL_REPO_PATH": "/tmp/repo",
        "SENTINEL_STRICT_QUALITY_MODE": "true",
    }
    cfg = Config(env=env, load_dotenv_file=False)

    assert cfg.strict_quality_mode is True
    assert cfg.enable_deterministic_lane is True
    assert cfg.enable_retrieval_reasoning_lane is True
    assert cfg.enable_reconciliation is True
    assert cfg.enable_quality_scorecard is True
    assert cfg.enforce_review_completeness_gate is True
    assert cfg.enforce_full_coverage_gate is True
    assert cfg.enforce_full_specialist_coverage is False
    assert cfg.review_profile == "reliability-first"
    assert cfg.review_module_completeness_threshold == 100.0
    assert cfg.review_quick_success_threshold == 100.0
    assert cfg.full_coverage_threshold_pct == 100.0


def test_config_reads_enable_concept_themes_env() -> None:
    base = {"SENTINEL_REPO_PATH": "/tmp/repo"}

    # Default (unset) is ON, preserving the historic always-run behavior.
    assert Config(env=base, load_dotenv_file=False).enable_concept_themes is True

    # Explicit disable, case-insensitive / whitespace-trimmed.
    assert (
        Config(
            env={**base, "SENTINEL_ENABLE_CONCEPT_THEMES": "false"},
            load_dotenv_file=False,
        ).enable_concept_themes
        is False
    )
    assert (
        Config(
            env={**base, "SENTINEL_ENABLE_CONCEPT_THEMES": "  FALSE  "},
            load_dotenv_file=False,
        ).enable_concept_themes
        is False
    )
    assert (
        Config(
            env={**base, "SENTINEL_ENABLE_CONCEPT_THEMES": "true"},
            load_dotenv_file=False,
        ).enable_concept_themes
        is True
    )


def test_strict_quality_mode_does_not_force_concept_themes() -> None:
    # Concept themes is an alternate lens, not a coverage gate: strict mode
    # must NOT force it on, and an explicit disable must survive strict mode.
    env = {
        "SENTINEL_REPO_PATH": "/tmp/repo",
        "SENTINEL_STRICT_QUALITY_MODE": "true",
        "SENTINEL_ENABLE_CONCEPT_THEMES": "false",
    }
    cfg = Config(env=env, load_dotenv_file=False)
    assert cfg.strict_quality_mode is True
    assert cfg.enable_concept_themes is False


def test_config_reads_review_profile_env() -> None:
    base = {"SENTINEL_REPO_PATH": "/tmp/repo"}

    # Default (unset) stays the pilot default.
    assert Config(env=base, load_dotenv_file=False).review_profile == "reliability-first"

    # Explicit selection, case-insensitive / whitespace-trimmed.
    assert Config(
        env={**base, "SENTINEL_REVIEW_PROFILE": "triggered"}, load_dotenv_file=False
    ).review_profile == "triggered"
    assert Config(
        env={**base, "SENTINEL_REVIEW_PROFILE": "  Triggered  "}, load_dotenv_file=False
    ).review_profile == "triggered"

    # Unrecognized value warns and falls back to the default.
    assert Config(
        env={**base, "SENTINEL_REVIEW_PROFILE": "exhaustive"}, load_dotenv_file=False
    ).review_profile == "reliability-first"
