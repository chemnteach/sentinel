from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from sentinel.assess.deterministic import DeterministicAnalyzer
from sentinel.assess.finding_kinds import (
    iter_supported_report_finding_kind_definitions,
)
from sentinel.config import Config
from sentinel.orchestrator.main import HumanGateRequired, Orchestrator


def test_phase1_writes_deterministic_findings_when_flag_enabled(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")

    env = {
        "SENTINEL_REPO_PATH": str(repo),
        "SENTINEL_LOCAL_ONLY_MODE": "true",
        "SENTINEL_ENABLE_DETERMINISTIC_LANE": "true",
    }
    cfg = Config(env=env, load_dotenv_file=False)

    deterministic_findings = [
        {
            "module": "src",
            "skill": "deterministic-lane",
            "severity": "P3",
            "category": "maintainability",
            "location": "src/app.py:1",
            "description": "sample deterministic finding",
            "suggested_fix": "sample fix",
            "status": "open",
        }
    ]

    with Orchestrator(cfg) as orch:
        with patch.object(
            orch._deterministic_analyzer,
            "review_module",
            return_value=deterministic_findings,
        ):
            try:
                orch.run_phase1([repo])
                assert False, "Expected Phase 1 triage gate"
            except HumanGateRequired as gate:
                assert gate.gate_type == "tech_debt_triage"

    findings_path = cfg.state_dir / "deterministic_findings.json"
    assert findings_path.exists()

    stored = json.loads(findings_path.read_text(encoding="utf-8"))
    assert len(stored) >= 1
    assert stored[0]["skill"] == "deterministic-lane"


def test_phase1_skips_deterministic_findings_file_when_flag_disabled(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")

    env = {
        "SENTINEL_REPO_PATH": str(repo),
        "SENTINEL_LOCAL_ONLY_MODE": "true",
    }
    cfg = Config(env=env, load_dotenv_file=False)

    with Orchestrator(cfg) as orch:
        try:
            orch.run_phase1([repo])
            assert False, "Expected Phase 1 triage gate"
        except HumanGateRequired as gate:
            assert gate.gate_type == "tech_debt_triage"

    findings_path = cfg.state_dir / "deterministic_findings.json"
    assert not findings_path.exists()


def test_deterministic_analyzer_runs_all_adapters_and_normalizes() -> None:
    class AdapterA:
        def run(self, module: dict, source_code: str) -> list[dict]:
            return [{"description": "A"}]

    class AdapterB:
        def run(self, module: dict, source_code: str) -> list[dict]:
            return [{"description": "B", "severity": "P2", "category": "correctness"}]

    analyzer = DeterministicAnalyzer(adapters=[AdapterA(), AdapterB()])
    findings = analyzer.review_module({"name": "m", "path": "src/m.py"}, "print('x')")

    assert len(findings) == 2
    assert findings[0]["description"] == "A"
    assert findings[1]["description"] == "B"
    for finding in findings:
        assert finding["module"] == "m"
        # The deterministic lane is the deterministic half of Layer-2a quick
        # review; findings carry the quick-review skill name for downstream
        # dedup/grouping consolidation.
        assert finding["skill"] == "layer2a-quick-review"
        assert finding["status"] == "open"


def test_deterministic_analyzer_ignores_non_dict_findings() -> None:
    class BadAdapter:
        def run(self, module: dict, source_code: str):
            return ["bad", {"description": "ok"}]

    analyzer = DeterministicAnalyzer(adapters=[BadAdapter()])
    findings = analyzer.review_module({"name": "m", "path": "src/m.py"}, "print('x')")

    assert len(findings) == 1
    assert findings[0]["description"] == "ok"


class _LocationAdapter:
    """Adapter that emits a single finding with a caller-supplied location."""

    def __init__(self, location: str) -> None:
        self._location = location

    def run(self, module: dict, source_code: str) -> list[dict]:
        return [
            {
                "description": "loc finding",
                "location": self._location,
                "finding_kind": "structural_complexity",
            }
        ]


def test_deterministic_finding_derives_file_from_location_with_line_and_context() -> None:
    analyzer = DeterministicAnalyzer(
        adapters=[_LocationAdapter("sentinel/foo.py:10 in bar")]
    )
    findings = analyzer.review_module(
        {"name": "m", "path": "sentinel/foo.py"}, "print('x')"
    )

    assert findings[0]["file"] == "sentinel/foo.py"


def test_deterministic_finding_derives_file_from_bare_path_location() -> None:
    analyzer = DeterministicAnalyzer(adapters=[_LocationAdapter("sentinel/foo.py")])
    findings = analyzer.review_module(
        {"name": "m", "path": "sentinel/foo.py"}, "print('x')"
    )

    assert findings[0]["file"] == "sentinel/foo.py"


def test_deterministic_finding_no_false_file_attribution_without_path() -> None:
    analyzer = DeterministicAnalyzer(adapters=[_LocationAdapter("some_function")])
    findings = analyzer.review_module(
        {"name": "m", "path": "sentinel/foo.py"}, "print('x')"
    )

    assert findings[0].get("file", "") == ""


def test_ai_lane_finding_file_not_derived_by_deterministic_change() -> None:
    from sentinel.assess.finding_schema import materialize_emitter_finding

    finding = {
        "module": "m",
        "skill": "layer2a-quick-review",
        "category": "general",
        "location": "sentinel/foo.py",
        "description": "ai finding",
        "finding_kind": "structural_complexity",
    }
    out = materialize_emitter_finding(
        finding,
        emitter_name="layer2a-quick-review",
        repo_root_canonical="",
    )

    assert out.get("file", "") == ""


def test_default_adapters_emit_ordered_findings() -> None:
    analyzer = DeterministicAnalyzer()
    findings = analyzer.review_module(
        {"name": "m", "path": "src/m.py"},
        "# TODO: cleanup\n# FIXME: unstable behavior\n",
    )

    assert [f["category"] for f in findings] == ["maintainability", "correctness"]
    assert [f["description"] for f in findings] == [
        "TODO markers found in source code.",
        "FIXME markers found in source code.",
    ]


def test_deterministic_normalization_golden_defaults() -> None:
    class MinimalAdapter:
        def run(self, module: dict, source_code: str) -> list[dict]:
            return [{"description": "minimal"}]

    analyzer = DeterministicAnalyzer(adapters=[MinimalAdapter()])
    findings = analyzer.review_module({"name": "mod", "path": "src/mod.py"}, "print('x')")

    assert len(findings) == 1
    finding = findings[0]
    assert finding["module"] == "mod"
    assert finding["skill"] == "layer2a-quick-review"
    assert finding["severity"] == "P3"
    assert finding["category"] == "general"
    assert finding["location"] == "src/mod.py"
    assert finding["description"] == "minimal"
    assert finding["suggested_fix"] == "Review finding details."
    assert finding["status"] == "open"
    assert finding["finding_kind"] == "structural_complexity"
    assert finding["id"].startswith("F2-")
    assert finding["legacy_finding_id"].startswith("F-")


def test_phase1_writes_reconciliation_report_with_deterministic_resolution(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")

    env = {
        "SENTINEL_REPO_PATH": str(repo),
        "SENTINEL_LOCAL_ONLY_MODE": "true",
        "SENTINEL_ENABLE_DETERMINISTIC_LANE": "true",
        "SENTINEL_ENABLE_RECONCILIATION": "true",
    }
    cfg = Config(env=env, load_dotenv_file=False)

    deterministic_findings = [
        {
            "module": "src",
            "skill": "deterministic-lane",
            "severity": "P3",
            "category": "maintainability",
            "location": "src/app.py:1",
            "description": "sample deterministic finding",
            "suggested_fix": "sample fix",
            "status": "open",
        }
    ]

    # Stage 4c invokes the workflow concept-theme builder; stub it to
    # decouple this Phase-1 reconciliation test from the embedding +
    # HDBSCAN clustering dependency. Defense-in-depth on top of the
    # count-based short-circuit in _run_concept_themes.
    monkeypatch.setattr(
        "sentinel.orchestrator.main.build_concept_themes_from_descriptions",
        lambda *_args, **_kwargs: [],
    )

    with Orchestrator(cfg) as orch:
        with patch.object(
            orch._deterministic_analyzer,
            "review_module",
            return_value=deterministic_findings,
        ):
            try:
                orch.run_phase1([repo])
                assert False, "Expected Phase 1 triage gate"
            except HumanGateRequired as gate:
                assert gate.gate_type == "tech_debt_triage"

    report_path = cfg.state_dir / "reconciliation_report.json"
    assert report_path.exists()

    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["summary"]["resolved_count"] == 1
    assert report["summary"]["kept_count"] == 1
    assert report["summary"]["unresolved_count"] == 0
    assert report["summary"]["dropped_count"] == 0
    assert report["decisions"][0]["action"] == "keep"
    assert report["decisions"][0]["resolution"] == "keep_deterministic"


def test_default_adapters_include_python_syntax_check() -> None:
    analyzer = DeterministicAnalyzer()
    findings = analyzer.review_module(
        {"name": "bad", "path": "src/bad.py"},
        "def broken(:\n    pass\n",
    )

    assert any(f["category"] == "correctness" for f in findings)
    assert any("syntax" in f["description"].lower() for f in findings)


def test_python_syntax_check_skips_non_python_modules() -> None:
    analyzer = DeterministicAnalyzer()
    findings = analyzer.review_module(
        {"name": "config", "path": "config/settings.xml"},
        "<settings><flag>true</flag></settings>",
    )

    assert all("syntax" not in f["description"].lower() for f in findings)


def test_default_adapters_include_dependency_risk_check() -> None:
    analyzer = DeterministicAnalyzer()
    findings = analyzer.review_module(
        {"name": "deps", "path": "requirements.txt"},
        "Django==1.11.0\nrequests==2.31.0\n",
    )

    assert any(f["category"] == "security" for f in findings)
    assert any("dependency risk" in f["description"].lower() for f in findings)


def test_dependency_risk_check_skips_non_dependency_manifests() -> None:
    analyzer = DeterministicAnalyzer()
    findings = analyzer.review_module(
        {"name": "module", "path": "src/app.py"},
        "Django==1.11.0\n",
    )

    assert all("dependency risk" not in f["description"].lower() for f in findings)


def test_default_adapters_include_xml_policy_check() -> None:
    analyzer = DeterministicAnalyzer()
    findings = analyzer.review_module(
        {"name": "cfg", "path": "config/settings.xml"},
        "<settings><AllowInsecure>true</AllowInsecure></settings>",
    )

    assert any(f["category"] == "security" for f in findings)
    assert any("policy" in f["description"].lower() for f in findings)


def test_xml_policy_check_skips_non_xml_config_files() -> None:
    analyzer = DeterministicAnalyzer()
    findings = analyzer.review_module(
        {"name": "code", "path": "src/main.py"},
        "AllowInsecure=true",
    )

    assert all("policy" not in f["description"].lower() for f in findings)


def test_deterministic_security_adapters_emit_supported_contract_kinds() -> None:
    supported_kinds = {
        definition.kind_name
        for definition in iter_supported_report_finding_kind_definitions()
    }
    analyzer = DeterministicAnalyzer()

    dependency_findings = analyzer.review_module(
        {"name": "deps", "path": "requirements.txt"},
        "Django==1.11.0\n",
    )
    xml_findings = analyzer.review_module(
        {"name": "cfg", "path": "config/settings.xml"},
        "<settings><AllowInsecure>true</AllowInsecure></settings>",
    )

    dependency_kinds = {finding["finding_kind"] for finding in dependency_findings}
    xml_kinds = {finding["finding_kind"] for finding in xml_findings}

    assert dependency_kinds
    assert xml_kinds
    assert dependency_kinds <= supported_kinds
    assert xml_kinds <= supported_kinds
    assert "build_release_integrity_gap" not in dependency_kinds
    assert "security_misconfiguration" not in xml_kinds


def test_deterministic_analyzer_rejects_unsupported_contract_finding_kind() -> None:
    class UnsupportedKindAdapter:
        def run(self, module: dict, source_code: str) -> list[dict]:
            return [
                {
                    "severity": "P2",
                    "category": "security",
                    "description": "unsupported contract kind",
                    "suggested_fix": "map this kind",
                    "finding_kind": "security_misconfiguration",
                    "subject_key": "allowinsecure>true",
                }
            ]

    analyzer = DeterministicAnalyzer(adapters=[UnsupportedKindAdapter()])

    with pytest.raises(ValueError, match="unsupported finding_kind"):
        analyzer.review_module(
            {"name": "cfg", "path": "config/settings.xml"},
            "<settings><AllowInsecure>true</AllowInsecure></settings>",
        )


def test_all_hard_coded_deterministic_finding_kinds_are_supported() -> None:
    supported_kinds = {
        definition.kind_name
        for definition in iter_supported_report_finding_kind_definitions()
    }

    source = Path("sentinel/assess/deterministic.py").read_text(encoding="utf-8")
    hard_coded_kinds = set(re.findall(r'"finding_kind"\s*:\s*"([a-z0-9_]+)"', source))

    assert hard_coded_kinds
    assert hard_coded_kinds <= supported_kinds


def test_default_deterministic_adapters_emit_only_supported_contract_kinds() -> None:
    supported_kinds = {
        definition.kind_name
        for definition in iter_supported_report_finding_kind_definitions()
    }
    analyzer = DeterministicAnalyzer()

    samples = [
        ({"name": "todo", "path": "src/todo.py"}, "# TODO: resolve\n"),
        ({"name": "fixme", "path": "src/fixme.py"}, "# FIXME: patch\n"),
        ({"name": "syntax", "path": "src/bad.py"}, "def broken(:\n    pass\n"),
        ({"name": "deps", "path": "requirements.txt"}, "Django==1.11.0\n"),
        (
            {"name": "cfg", "path": "config/settings.xml"},
            "<settings><AllowInsecure>true</AllowInsecure></settings>",
        ),
    ]

    emitted_kinds: set[str] = set()
    for module, source in samples:
        for finding in analyzer.review_module(module, source):
            emitted_kinds.add(finding["finding_kind"])

    assert emitted_kinds
    assert emitted_kinds <= supported_kinds
