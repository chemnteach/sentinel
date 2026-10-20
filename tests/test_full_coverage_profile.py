"""SENTINEL_PROFILE=full-coverage preset bundles the strict handoff settings."""
from __future__ import annotations

from sentinel.config import Config


def _cfg(env):
    return Config(env=env, load_dotenv_file=False)


def test_full_coverage_profile_forces_strict_bundle():
    cfg = _cfg({"SENTINEL_PROFILE": "full-coverage"})
    assert cfg.strict_quality_mode is True
    assert cfg.enable_deterministic_lane is True
    assert cfg.enable_retrieval_reasoning_lane is True
    assert cfg.enable_reconciliation is True
    assert cfg.enable_quality_scorecard is True
    assert cfg.enforce_full_specialist_coverage is True
    # Defaults that make the run correct for handoff.
    assert cfg.assessment_scope == "operational"
    assert cfg.enable_alarm_classification is False  # not shipped publicly
    # Strict block then forces the completeness/coverage gates.
    assert cfg.enforce_review_completeness_gate is True
    assert cfg.enforce_full_coverage_gate is True


def test_no_profile_preserves_legacy_defaults():
    cfg = _cfg({})
    assert cfg.strict_quality_mode is False
    assert cfg.enable_deterministic_lane is False
    assert cfg.enforce_full_specialist_coverage is False


def test_full_coverage_profile_still_allows_scope_override():
    cfg = _cfg({"SENTINEL_PROFILE": "full-coverage", "SENTINEL_ASSESSMENT_SCOPE": "all"})
    assert cfg.strict_quality_mode is True
    assert cfg.assessment_scope == "all"


def test_alarm_specialist_cannot_be_enabled_in_public_build():
    assert _cfg({"SENTINEL_ENABLE_ALARM_SPECIALIST": "true"}).enable_alarm_specialist is False


def test_enable_alarm_specialist_env_false_disables():
    assert _cfg({"SENTINEL_ENABLE_ALARM_SPECIALIST": "false"}).enable_alarm_specialist is False
