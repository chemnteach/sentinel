"""Tests for the Orchestrator Stage 4c wiring (``_run_concept_themes``).

Three tests cover the three branches of the Stage 4c method body:

* ``test_run_concept_themes_writes_themes_when_findings_present`` — the
  success path: findings present → producer returns themes → themes
  persisted via ``StateStore`` + ``concept_themes_written`` audit
  event.
* ``test_run_concept_themes_writes_empty_when_findings_absent`` — the
  absent-findings short-circuit: no ``findings.json`` →
  ``load_findings`` returns ``()`` → producer returns ``[]`` (its own
  empty-input guard at ``build_concept_themes.py`` L80) → empty list
  persisted + ``concept_themes_written`` audit event with zeroed
  counts.
* ``test_run_concept_themes_warn_and_skip_on_missing_extras`` — the
  warn-and-skip path: producer raises ``RuntimeError`` (simulating
  ``_load_embedder`` / ``_load_clusterer`` re-raise on absent
  ``sentence-transformers`` / ``hdbscan``) → empty list persisted via
  distinct skip-commit message + distinct ``concept_themes_skipped``
  audit event + warning log captured.

Fixture pattern mirrors ``tests/test_architect_reentry.py``'s
``_stub_orchestrator`` helper, pared down to the attributes Stage 4c
actually touches (``_store`` + ``_config.state_dir``).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from sentinel.config import Config
from sentinel.orchestrator.main import Orchestrator
from sentinel.orchestrator.state_store import StateStore
from sentinel.workflow.models import ConceptTheme


def _stub_orchestrator(tmp_path: Path) -> Orchestrator:
    """Construct an Orchestrator via ``__new__`` with only the
    attributes Stage 4c touches (``_store`` + ``_config.state_dir``).
    Mirrors ``tests/test_architect_reentry.py::_stub_orchestrator``
    without the Stage 5b-only ``_architect`` / ``_loader`` / ``_client``
    plumbing.
    """
    orch = Orchestrator.__new__(Orchestrator)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    orch._store = StateStore(state_dir=state_dir, repo_path=tmp_path)
    cfg = MagicMock(spec=Config)
    cfg.state_dir = state_dir
    # Sub-step 8-E (cycle 31): the lazy ``_workflow_config`` property
    # reads ``workflow_config_yaml_path`` from Config; empty string ⇒
    # ``WorkflowConfig.default()``. Cache primed to ``None`` so the
    # first property access populates it.
    cfg.workflow_config_yaml_path = ""
    # Master switch (cycle 40): default ON so the existing success-path
    # tests exercise the producer. Individual tests override to False to
    # cover the disabled-skip branch.
    cfg.enable_concept_themes = True
    orch._config = cfg
    orch._workflow_config_cache = None
    return orch


def _write_findings(state_dir: Path, count: int) -> None:
    """Pre-write a small ``findings.json`` with ``count`` minimal
    findings whose shape matches the producer-side schema consumed by
    ``finding_loader._deserialize_finding`` (raw ``"id"`` key, plus the
    fields with tolerant-default reads).
    """
    findings = [
        {
            "id": f"f-{i:03d}",
            "summary": f"Finding {i}",
            "severity": "P2",
            "skill": "layer2c-security-specialist",
            "module": "mod-a",
            "category": "security",
            "location": "src/file.py:10",
            "description": (
                f"Description of finding {i} explaining the issue in "
                "enough words for a clustering embedder to see signal."
            ),
        }
        for i in range(count)
    ]
    (state_dir / "findings.json").write_text(
        json.dumps(findings), encoding="utf-8",
    )


def test_run_concept_themes_writes_themes_when_findings_present(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orch = _stub_orchestrator(tmp_path)
    _write_findings(orch._config.state_dir, count=3)

    theme = ConceptTheme(
        concept_theme_id="ct-test-001",
        title="Test Concept Theme",
        root_cause_summary="Stub root cause.",
        remediation_strategy="Stub remediation.",
        member_finding_ids=["f-000", "f-001", "f-002"],
    )

    def _stub_builder(findings, *, previous_themes=None, **_kwargs):
        # Sanity-check the bridge handed us the loaded findings.
        assert len(findings) == 3
        assert previous_themes == []
        return [theme]

    monkeypatch.setattr(
        "sentinel.orchestrator.main.build_concept_themes_from_descriptions",
        _stub_builder,
    )

    orch._run_concept_themes()

    persisted_path = orch._config.state_dir / "concept_themes.json"
    assert persisted_path.exists()
    persisted = json.loads(persisted_path.read_text(encoding="utf-8"))
    assert isinstance(persisted, list)
    assert len(persisted) == 1
    assert persisted[0]["concept_theme_id"] == "ct-test-001"
    assert persisted[0]["title"] == "Test Concept Theme"

    audit_events = orch._store.read("audit_log")
    written = [
        e for e in audit_events
        if e.get("action") == "concept_themes_written"
    ]
    assert len(written) == 1
    assert written[0]["details"]["finding_count"] == 3
    assert written[0]["details"]["theme_count"] == 1


def test_run_concept_themes_writes_empty_when_findings_absent(
    tmp_path: Path,
) -> None:
    """Absent ``findings.json`` → ``load_findings`` returns ``()`` →
    count-based short-circuit triggers (0 < 3) → empty
    ``concept_themes.json`` + ``concept_themes_skipped`` audit event
    with ``reason="insufficient_findings"`` and ``finding_count=0``.
    Stage 4c never reaches the builder; no monkeypatch needed.
    """
    orch = _stub_orchestrator(tmp_path)
    # No findings.json pre-write → load_findings returns ().

    orch._run_concept_themes()

    persisted_path = orch._config.state_dir / "concept_themes.json"
    assert persisted_path.exists()
    persisted = json.loads(persisted_path.read_text(encoding="utf-8"))
    assert persisted == []

    audit_events = orch._store.read("audit_log")
    skipped = [
        e for e in audit_events
        if e.get("action") == "concept_themes_skipped"
    ]
    assert len(skipped) == 1
    assert skipped[0]["details"]["reason"] == "insufficient_findings"
    assert skipped[0]["details"]["finding_count"] == 0
    assert skipped[0]["details"]["min_cluster_size"] == 3


def test_run_concept_themes_warn_and_skip_on_missing_extras(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    orch = _stub_orchestrator(tmp_path)
    # Seed at or above the count-based clustering threshold so the
    # short-circuit guard does not preempt the builder invocation;
    # the RuntimeError path is the surface under test here.
    _write_findings(orch._config.state_dir, count=3)

    def _raising_builder(*_args, **_kwargs):
        # Mirror the real producer message (_load_embedder / _load_clusterer),
        # which names the optional 'workflow' dependencies + pip remedy. Only a
        # message with that missing-dependency signature is treated as a skip;
        # other RuntimeErrors now propagate.
        raise RuntimeError(
            "Concept theme embedding requires the optional 'workflow' "
            "dependencies. Install with pip install -e \".[workflow]\"."
        )

    monkeypatch.setattr(
        "sentinel.orchestrator.main.build_concept_themes_from_descriptions",
        _raising_builder,
    )

    with caplog.at_level(logging.WARNING, logger="sentinel.orchestrator.main"):
        orch._run_concept_themes()  # must not raise

    persisted_path = orch._config.state_dir / "concept_themes.json"
    assert persisted_path.exists()
    persisted = json.loads(persisted_path.read_text(encoding="utf-8"))
    assert persisted == []

    audit_events = orch._store.read("audit_log")
    skipped = [
        e for e in audit_events
        if e.get("action") == "concept_themes_skipped"
    ]
    assert len(skipped) == 1
    assert skipped[0]["details"]["reason"] == "workflow_extras_missing"
    assert skipped[0]["details"]["finding_count"] == 3

    # No success-path audit event must have been emitted.
    written = [
        e for e in audit_events
        if e.get("action") == "concept_themes_written"
    ]
    assert written == []

    warning_messages = [r.getMessage() for r in caplog.records]
    assert any("Stage 4c" in msg for msg in warning_messages), warning_messages


def test_run_concept_themes_skips_when_below_threshold(
    tmp_path: Path,
) -> None:
    """Count-based short-circuit: with fewer than
    ``MIN_FINDINGS_FOR_CLUSTERING`` (3) findings present, Stage 4c must
    bypass the producer entirely (HDBSCAN raises ``ValueError`` on
    undersized inputs), write an empty ``concept_themes.json``, and
    record a ``concept_themes_skipped`` audit event with
    ``reason="insufficient_findings"``. No monkeypatching of the
    builder — the guard must protect against the real producer being
    reached.
    """
    orch = _stub_orchestrator(tmp_path)
    _write_findings(orch._config.state_dir, count=2)

    orch._run_concept_themes()  # must not raise

    persisted_path = orch._config.state_dir / "concept_themes.json"
    assert persisted_path.exists()
    persisted = json.loads(persisted_path.read_text(encoding="utf-8"))
    assert persisted == []

    audit_events = orch._store.read("audit_log")
    skipped = [
        e for e in audit_events
        if e.get("action") == "concept_themes_skipped"
    ]
    assert len(skipped) == 1
    assert skipped[0]["details"]["reason"] == "insufficient_findings"
    assert skipped[0]["details"]["finding_count"] == 2
    assert skipped[0]["details"]["min_cluster_size"] == 3


def test_run_concept_themes_skips_when_disabled_by_env(
    tmp_path: Path,
) -> None:
    """Master switch (cycle 40): ``enable_concept_themes=False`` (env
    ``SENTINEL_ENABLE_CONCEPT_THEMES=false``) → Stage 4c writes an empty
    ``concept_themes.json``, records a ``concept_themes_skipped`` audit
    event with ``reason="disabled_by_config"``, and never reaches the
    builder. The env switch short-circuits before the workflow config is
    consulted, so no findings pre-write and no monkeypatch are needed.
    """
    orch = _stub_orchestrator(tmp_path)
    orch._config.enable_concept_themes = False

    orch._run_concept_themes()  # must not raise

    persisted_path = orch._config.state_dir / "concept_themes.json"
    assert persisted_path.exists()
    persisted = json.loads(persisted_path.read_text(encoding="utf-8"))
    assert persisted == []

    audit_events = orch._store.read("audit_log")
    skipped = [
        e for e in audit_events
        if e.get("action") == "concept_themes_skipped"
    ]
    assert len(skipped) == 1
    assert skipped[0]["details"]["reason"] == "disabled_by_config"

    written = [
        e for e in audit_events
        if e.get("action") == "concept_themes_written"
    ]
    assert written == []


def test_run_concept_themes_skips_when_lens_disabled_in_workflow_config(
    tmp_path: Path,
) -> None:
    """Master switch (cycle 40), workflow-config arm: with the env toggle
    ON but ``conceptual_lens.enabled=False`` in ``WorkflowConfig``, Stage
    4c must still skip with ``reason="disabled_by_config"``. This proves
    the previously-inert ``ConceptualLensConfig.enabled`` field is now
    honored. Findings are seeded above the clustering floor to show the
    skip is driven by the flag, not the count guard.
    """
    from sentinel.workflow.config import (
        ConceptualLensConfig,
        HdbscanConfig,
        ThemesConfig,
        WorkflowConfig,
    )

    orch = _stub_orchestrator(tmp_path)
    orch._config.enable_concept_themes = True
    _write_findings(orch._config.state_dir, count=3)

    base = WorkflowConfig.default()
    orch._workflow_config_cache = WorkflowConfig(
        github=base.github,
        themes=ThemesConfig(
            primary_lens=base.themes.primary_lens,
            structural_lens=base.themes.structural_lens,
            conceptual_lens=ConceptualLensConfig(
                enabled=False,
                embedding_model=base.themes.conceptual_lens.embedding_model,
                embedding_seed=base.themes.conceptual_lens.embedding_seed,
                hdbscan=base.themes.conceptual_lens.hdbscan,
                stability_jaccard_threshold=(
                    base.themes.conceptual_lens.stability_jaccard_threshold
                ),
                title_strategy=base.themes.conceptual_lens.title_strategy,
            ),
        ),
        remediation=base.remediation,
        profile=base.profile,
        dashboard=base.dashboard,
    )

    orch._run_concept_themes()  # must not raise

    persisted_path = orch._config.state_dir / "concept_themes.json"
    assert persisted_path.exists()
    assert json.loads(persisted_path.read_text(encoding="utf-8")) == []

    audit_events = orch._store.read("audit_log")
    skipped = [
        e for e in audit_events
        if e.get("action") == "concept_themes_skipped"
    ]
    assert len(skipped) == 1
    assert skipped[0]["details"]["reason"] == "disabled_by_config"


# ----------------------------------------------------------------------
# Sub-step 8-E (cycle 31): WorkflowConfig threading into Stage 4c.
# ----------------------------------------------------------------------


def test_run_concept_themes_uses_default_config_when_no_yaml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty ``workflow_config_yaml_path`` ⇒ ``WorkflowConfig.default()``
    is used, and the 5 producer parameters are threaded from its
    ``themes.conceptual_lens`` defaults (sub-step 8-E).
    """
    orch = _stub_orchestrator(tmp_path)
    _write_findings(orch._config.state_dir, count=3)

    captured: dict[str, object] = {}

    def _capturing_builder(findings, *, previous_themes=None, **kwargs):
        captured["findings_count"] = len(findings)
        captured.update(kwargs)
        return []

    monkeypatch.setattr(
        "sentinel.orchestrator.main.build_concept_themes_from_descriptions",
        _capturing_builder,
    )

    orch._run_concept_themes()

    # Defaults sourced from ConceptualLensConfig + HdbscanConfig.
    assert captured["embedding_model"] == (
        "sentence-transformers/all-MiniLM-L6-v2"
    )
    assert captured["embedding_seed"] == 42
    assert captured["min_cluster_size"] == 3
    assert captured["min_samples"] == 2
    assert captured["cluster_selection_epsilon"] == 0.15
    assert captured["findings_count"] == 3


def test_workflow_config_threads_min_cluster_size_to_guard(
    tmp_path: Path,
) -> None:
    """``min_cluster_size`` from ``WorkflowConfig`` drives the count-based
    short-circuit (not the old hardcoded ``3``). With ``min_cluster_size=5``
    and 4 findings, Stage 4c must short-circuit with
    ``min_cluster_size=5`` in the audit payload (sub-step 8-E).
    """
    from sentinel.workflow.config import (
        ConceptualLensConfig,
        HdbscanConfig,
        ThemesConfig,
        WorkflowConfig,
    )

    orch = _stub_orchestrator(tmp_path)
    _write_findings(orch._config.state_dir, count=4)

    base = WorkflowConfig.default()
    orch._workflow_config_cache = WorkflowConfig(
        github=base.github,
        themes=ThemesConfig(
            primary_lens=base.themes.primary_lens,
            structural_lens=base.themes.structural_lens,
            conceptual_lens=ConceptualLensConfig(
                enabled=base.themes.conceptual_lens.enabled,
                embedding_model=base.themes.conceptual_lens.embedding_model,
                embedding_seed=base.themes.conceptual_lens.embedding_seed,
                hdbscan=HdbscanConfig(
                    min_cluster_size=5,
                    min_samples=base.themes.conceptual_lens.hdbscan.min_samples,
                    cluster_selection_epsilon=(
                        base.themes.conceptual_lens.hdbscan.cluster_selection_epsilon
                    ),
                ),
                stability_jaccard_threshold=(
                    base.themes.conceptual_lens.stability_jaccard_threshold
                ),
                title_strategy=base.themes.conceptual_lens.title_strategy,
            ),
        ),
        remediation=base.remediation,
        profile=base.profile,
        dashboard=base.dashboard,
    )

    orch._run_concept_themes()  # must not raise

    audit_events = orch._store.read("audit_log")
    skipped = [
        e for e in audit_events
        if e.get("action") == "concept_themes_skipped"
    ]
    assert len(skipped) == 1
    assert skipped[0]["details"]["reason"] == "insufficient_findings"
    assert skipped[0]["details"]["finding_count"] == 4
    assert skipped[0]["details"]["min_cluster_size"] == 5


def test_workflow_config_yaml_path_loads_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-empty ``workflow_config_yaml_path`` ⇒ ``load_workflow_config_yaml``
    is invoked and the loaded values are threaded to the producer
    (sub-step 8-E). PyYAML may be absent in the test environment; we
    monkeypatch the loader instead of writing a real YAML so the test
    is hermetic.
    """
    from sentinel.workflow.config import (
        ConceptualLensConfig,
        HdbscanConfig,
        ThemesConfig,
        WorkflowConfig,
    )

    orch = _stub_orchestrator(tmp_path)
    _write_findings(orch._config.state_dir, count=3)
    orch._config.workflow_config_yaml_path = str(
        tmp_path / "workflow_config.yaml"
    )

    base = WorkflowConfig.default()
    custom = WorkflowConfig(
        github=base.github,
        themes=ThemesConfig(
            primary_lens=base.themes.primary_lens,
            structural_lens=base.themes.structural_lens,
            conceptual_lens=ConceptualLensConfig(
                enabled=True,
                embedding_model="custom/embedder",
                embedding_seed=99,
                hdbscan=HdbscanConfig(
                    min_cluster_size=3,
                    min_samples=4,
                    cluster_selection_epsilon=0.25,
                ),
                stability_jaccard_threshold=(
                    base.themes.conceptual_lens.stability_jaccard_threshold
                ),
                title_strategy=base.themes.conceptual_lens.title_strategy,
            ),
        ),
        remediation=base.remediation,
        profile=base.profile,
        dashboard=base.dashboard,
    )

    monkeypatch.setattr(
        "sentinel.orchestrator.main.load_workflow_config_yaml",
        lambda _path: custom,
    )

    captured: dict[str, object] = {}

    def _capturing_builder(findings, *, previous_themes=None, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr(
        "sentinel.orchestrator.main.build_concept_themes_from_descriptions",
        _capturing_builder,
    )

    orch._run_concept_themes()

    assert captured["embedding_model"] == "custom/embedder"
    assert captured["embedding_seed"] == 99
    assert captured["min_cluster_size"] == 3
    assert captured["min_samples"] == 4
    assert captured["cluster_selection_epsilon"] == 0.25
