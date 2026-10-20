from __future__ import annotations

from pathlib import Path
import json
from unittest.mock import patch

from sentinel.config import Config
from sentinel.chat.session import ChatSession
from sentinel.orchestrator.main import HumanGateRequired, Orchestrator, _SOURCE_EXTS


def test_phase1_runs_without_github_token_in_local_only_mode(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "sample.py").write_text("print('ok')\n", encoding="utf-8")

    env = {
        "SENTINEL_REPO_PATH": str(repo),
        "SENTINEL_LOCAL_ONLY_MODE": "true",
    }
    cfg = Config(env=env, load_dotenv_file=False)
    cfg.ensure_state_dir()

    with Orchestrator(cfg) as orch:
        try:
            orch.run_phase1([repo])
            assert False, "Expected Phase 1 triage gate"
        except HumanGateRequired as gate:
            assert gate.gate_type == "tech_debt_triage"

    tech_debt_file = cfg.state_dir / "tech_debt.json"
    assert tech_debt_file.exists()


def test_strict_mode_rejects_local_only_orchestrator(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)

    env = {
        "SENTINEL_REPO_PATH": str(repo),
        "SENTINEL_LOCAL_ONLY_MODE": "true",
        "SENTINEL_STRICT_QUALITY_MODE": "true",
    }
    cfg = Config(env=env, load_dotenv_file=False)

    try:
        Orchestrator(cfg)
        assert False, "Expected EnvironmentError in strict local-only mode"
    except EnvironmentError as exc:
        assert "Strict quality mode requires external model inference" in str(exc)


def test_strict_mode_allows_local_only_when_chat_native(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)

    env = {
        "SENTINEL_REPO_PATH": str(repo),
        "SENTINEL_LOCAL_ONLY_MODE": "true",
        "SENTINEL_STRICT_QUALITY_MODE": "true",
        "SENTINEL_CHAT_NATIVE_MODE": "true",
    }
    cfg = Config(env=env, load_dotenv_file=False)

    with Orchestrator(cfg):
        pass


def test_chat_session_enables_chat_native_mode() -> None:
    cfg = Config(
        env={
            "SENTINEL_LOCAL_ONLY_MODE": "false",
            "SENTINEL_CHAT_NATIVE_MODE": "false",
        },
        load_dotenv_file=False,
    )

    session = ChatSession(cfg)

    # The session enables chat-native + local-only on its OWN private copy,
    # without mutating the caller's Config (which may be reused elsewhere).
    assert session._config.chat_native_mode is True
    assert session._config.local_only_mode is True
    assert cfg.chat_native_mode is False
    assert cfg.local_only_mode is False


def test_phase1_local_only_discovers_csharp_and_xml_modules(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "config").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "Program.cs").write_text(
        "namespace Demo; class Program { static void Main() {} }\n",
        encoding="utf-8",
    )
    (repo / "config" / "settings.xml").write_text(
        "<settings><flag>true</flag></settings>\n",
        encoding="utf-8",
    )
    (repo / "config" / "bootstrap.vbs").write_text(
        "WScript.Echo \"ok\"\n",
        encoding="utf-8",
    )
    (repo / "Installer" / "Installer.wxs").parent.mkdir(parents=True, exist_ok=True)
    (repo / "Installer" / "Installer.wxs").write_text(
        "<Wix></Wix>\n",
        encoding="utf-8",
    )
    (repo / "Installer" / "Installer.wixproj").write_text(
        "<Project></Project>\n",
        encoding="utf-8",
    )
    (repo / "src" / "App.csproj").write_text(
        "<Project Sdk=\"Microsoft.NET.Sdk\"></Project>\n",
        encoding="utf-8",
    )
    (repo / "Contoso.sln").write_text(
        "Microsoft Visual Studio Solution File, Format Version 12.00\n",
        encoding="utf-8",
    )
    (repo / "Config" / "Settings.Config").parent.mkdir(parents=True, exist_ok=True)
    (repo / "Config" / "Settings.Config").write_text(
        "<configuration></configuration>\n",
        encoding="utf-8",
    )
    (repo / "src" / "Legacy.CS").write_text(
        "class Legacy {}\n",
        encoding="utf-8",
    )

    env = {
        "SENTINEL_REPO_PATH": str(repo),
        "SENTINEL_LOCAL_ONLY_MODE": "true",
        # This test validates discovery breadth (all file types the local
        # cartographer can enumerate), so opt out of operational scoping.
        "SENTINEL_ASSESSMENT_SCOPE": "all",
    }
    cfg = Config(env=env, load_dotenv_file=False)

    with Orchestrator(cfg) as orch:
        try:
            orch.run_phase1([repo])
            assert False, "Expected Phase 1 triage gate"
        except HumanGateRequired as gate:
            assert gate.gate_type == "tech_debt_triage"

    repos = json.loads((cfg.state_dir / "repositories.json").read_text(encoding="utf-8"))
    modules = json.loads((cfg.state_dir / "modules.json").read_text(encoding="utf-8"))

    assert repos
    assert modules
    repo_languages = repos[0].get("languages", [])
    assert "C#" in repo_languages

    all_files = [f for module in modules for f in module.get("files", [])]
    assert any(str(path).endswith(".cs") for path in all_files)
    assert any(str(path).endswith(".xml") for path in all_files)
    assert any(str(path).endswith(".vbs") for path in all_files)
    assert any(str(path).endswith(".csproj") for path in all_files)
    assert any(str(path).endswith(".sln") for path in all_files)
    assert any(str(path).endswith(".wxs") for path in all_files)
    assert any(str(path).endswith(".wixproj") for path in all_files)
    assert any(str(path).endswith(".Config") for path in all_files)


def test_phase1_operational_scope_excludes_non_operational(tmp_path: Path) -> None:
    """Default operational scope keeps source (.cs) and drops config/data,
    scripts, project/build files, and other non-operational paths."""
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "config").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "Program.cs").write_text(
        "namespace Demo; class Program { static void Main() {} }\n",
        encoding="utf-8",
    )
    (repo / "config" / "settings.xml").write_text(
        "<settings><flag>true</flag></settings>\n", encoding="utf-8",
    )
    (repo / "config" / "bootstrap.vbs").write_text(
        "WScript.Echo \"ok\"\n", encoding="utf-8",
    )
    (repo / "src" / "App.csproj").write_text(
        "<Project Sdk=\"Microsoft.NET.Sdk\"></Project>\n", encoding="utf-8",
    )
    (repo / "Contoso.sln").write_text("Format Version 12.00\n", encoding="utf-8")

    # Default scope is "operational" — no env override.
    cfg = Config(
        env={"SENTINEL_REPO_PATH": str(repo), "SENTINEL_LOCAL_ONLY_MODE": "true"},
        load_dotenv_file=False,
    )
    assert cfg.assessment_scope == "operational"

    with Orchestrator(cfg) as orch:
        try:
            orch.run_phase1([repo])
            assert False, "Expected Phase 1 triage gate"
        except HumanGateRequired as gate:
            assert gate.gate_type == "tech_debt_triage"

    modules = json.loads((cfg.state_dir / "modules.json").read_text(encoding="utf-8"))
    all_files = [str(f) for module in modules for f in module.get("files", [])]

    assert any(f.endswith(".cs") for f in all_files)
    for ext in (".xml", ".vbs", ".csproj", ".sln"):
        assert not any(f.endswith(ext) for f in all_files), (
            f"operational scope should exclude {ext} files"
        )

    # The real enforcement point: Stage 3 must not REVIEW non-operational files
    # even though it re-walks the module directory. module_review_status.json
    # records the units actually reviewed.
    status_path = cfg.state_dir / "module_review_status.json"
    if status_path.exists():
        reviewed = json.loads(status_path.read_text(encoding="utf-8"))
        reviewed_names = " ".join(
            str(s.get("module", "")) for s in reviewed
        ).lower()
        for ext in (".xml", ".vbs", ".csproj", ".sln", ".config", ".wxs"):
            assert ext not in reviewed_names, (
                f"Stage 3 reviewed a {ext} file under operational scope"
            )


def test_source_extensions_include_csharp_and_xml() -> None:
    assert ".cs" in _SOURCE_EXTS
    assert ".xml" in _SOURCE_EXTS
    assert ".vbs" in _SOURCE_EXTS
    assert ".csproj" in _SOURCE_EXTS
    assert ".sln" in _SOURCE_EXTS
    assert ".wxs" in _SOURCE_EXTS
    assert ".wixproj" in _SOURCE_EXTS
    assert ".config" in _SOURCE_EXTS


def test_phase1_writes_review_coverage_artifacts(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "Program.cs").write_text(
        "namespace Demo; class Program { static void Main() {} }\n",
        encoding="utf-8",
    )
    (repo / "src" / "settings.xml").write_text(
        "<settings><flag>true</flag></settings>\n",
        encoding="utf-8",
    )

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

    coverage_report = json.loads((cfg.state_dir / "coverage_report.json").read_text(encoding="utf-8"))
    module_status = json.loads((cfg.state_dir / "module_review_status.json").read_text(encoding="utf-8"))
    failed_chunks = json.loads((cfg.state_dir / "failed_chunk_registry.json").read_text(encoding="utf-8"))

    assert coverage_report["total_modules"] >= 1
    assert coverage_report["failed_chunk_entries"] == len(failed_chunks)
    assert isinstance(module_status, list)
    assert module_status
    assert "review_complete" in module_status[0]


def test_phase1_writes_consolidated_assessment_report(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "Program.cs").write_text(
        "namespace Demo; class Program { static void Main() {} }\n",
        encoding="utf-8",
    )
    (repo / "src" / "App.csproj").write_text(
        "\n".join(
            [
                "<Project Sdk=\"Microsoft.NET.Sdk\">",
                "  <ItemGroup>",
                "    <ProjectReference Include=\"..\\Shared\\Shared.csproj\" />",
                "  </ItemGroup>",
                "</Project>",
            ]
        ),
        encoding="utf-8",
    )

    env = {
        "SENTINEL_REPO_PATH": str(repo),
        "SENTINEL_LOCAL_ONLY_MODE": "true",
        "SENTINEL_ENABLE_DETERMINISTIC_LANE": "true",
    }
    cfg = Config(env=env, load_dotenv_file=False)

    with Orchestrator(cfg) as orch:
        try:
            orch.run_phase1([repo])
            assert False, "Expected Phase 1 triage gate"
        except HumanGateRequired as gate:
            assert gate.gate_type == "tech_debt_triage"
            assert "phase1_assessment_file" in gate.payload

    report = json.loads((cfg.state_dir / "phase1_assessment.json").read_text(encoding="utf-8"))
    markdown = (cfg.state_dir / "phase1_assessment.md").read_text(encoding="utf-8")

    assert report["title"] == "Phase 1 Assessment"
    assert "summary" in report
    assert "findings" in report
    assert "dependencies" in report
    assert "additional_assessments" in report
    assert "cross_repo_candidates" in report["dependencies"]
    assert "cross_repo_groups" in report["dependencies"]
    assert report["roadmap"]["mode"] == "iterative_phase1"
    assert "iteration_policy" in report["roadmap"]
    assert "review_scope_queue" in report

    dep_items = report["dependencies"]["items"]
    assert dep_items
    project_refs = [item for item in dep_items if item.get("kind") == "project_reference"]
    assert project_refs
    assert project_refs[0]["dependency_id"].startswith("DEP-")
    assert project_refs[0]["trace_id"].startswith("TR-D-")
    assert project_refs[0]["severity"] in {"Critical", "P1", "P2", "P3"}
    assert isinstance(project_refs[0]["risk_score"], int)
    assert "repository_chain" in report
    assert "untriaged_external_dependencies" in report["summary"]
    assert "additional_assessment_findings" in report["summary"]
    assert "src/App.csproj" in project_refs[0]["review_targets"]
    assert "../Shared/Shared.csproj" in project_refs[0]["review_targets"]

    groups = report["dependencies"]["cross_repo_groups"]
    assert groups
    shared_group = next((item for item in groups if item.get("repo") == "Shared"), None)
    assert shared_group is not None
    assert shared_group["count"] == 1
    assert project_refs[0]["dependency_id"] in shared_group["dependency_ids"]

    assert "# Phase 1 Assessment" in markdown
    assert "## External Dependency Groups (By Repo)" in markdown
    assert "## External Dependency Candidates" in markdown
    assert "## Additional Architect Assessments" in markdown

    assessments = report["additional_assessments"]
    assert isinstance(assessments, list)
    assert len(assessments) == 6


def test_phase2_blocks_when_external_dependencies_untriaged(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)

    env = {
        "SENTINEL_REPO_PATH": str(repo),
        "SENTINEL_LOCAL_ONLY_MODE": "true",
    }
    cfg = Config(env=env, load_dotenv_file=False)
    cfg.ensure_state_dir()

    (cfg.state_dir / "plan.json").write_text(
        json.dumps({"items": []}, indent=2),
        encoding="utf-8",
    )
    (cfg.state_dir / "phase1_assessment.json").write_text(
        json.dumps(
            {
                "dependencies": {
                    "cross_repo_candidates": [
                        {
                            "dependency_id": "DEP-1234567890ab",
                            "source": "src/App.csproj",
                            "target": "../Shared/Shared.csproj",
                            "triage_decision": None,
                        }
                    ]
                }
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    with Orchestrator(cfg) as orch:
        try:
            orch.run_phase2()
            assert False, "Expected dependency scope triage gate"
        except HumanGateRequired as gate:
            assert gate.gate_type == "dependency_scope_triage"


def test_phase1_report_sorts_cross_repo_candidates_and_groups(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)

    env = {
        "SENTINEL_REPO_PATH": str(repo),
        "SENTINEL_LOCAL_ONLY_MODE": "true",
    }
    cfg = Config(env=env, load_dotenv_file=False)

    with Orchestrator(cfg) as orch:
        orch._store.write("findings", [])
        orch._store.write("analysis_pack", [])
        orch._store.write("tech_debt", [])
        orch._store.write(
            "repositories",
            [{"name": "demo", "path": str(repo)}],
        )
        orch._store.write(
            "dependencies",
            [
                {
                    "source": "src/Z.csproj",
                    "target": "../RepoB/Component.csproj",
                    "kind": "project_reference",
                    "is_external": True,
                },
                {
                    "source": "src/C.csproj",
                    "target": "../RepoA/Component2.csproj",
                    "kind": "project_reference",
                    "is_external": True,
                },
                {
                    "source": "src/A.csproj",
                    "target": "../RepoA/Component1.csproj",
                    "kind": "project_reference",
                    "is_external": True,
                },
            ],
        )

        report = orch._build_phase1_assessment_report({"systemic_patterns": []})

    candidates = report["dependencies"]["cross_repo_candidates"]
    assert [item["target"] for item in candidates] == [
        "../RepoA/Component1.csproj",
        "../RepoA/Component2.csproj",
        "../RepoB/Component.csproj",
    ]

    groups = report["dependencies"]["cross_repo_groups"]
    assert [item["repo"] for item in groups] == ["RepoA", "RepoB"]
    assert [item["count"] for item in groups] == [2, 1]


def test_strict_mode_additional_assessment_context_uses_full_payload(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)

    env = {
        "SENTINEL_REPO_PATH": str(repo),
        "SENTINEL_LOCAL_ONLY_MODE": "true",
        "SENTINEL_CHAT_NATIVE_MODE": "true",
        "SENTINEL_STRICT_QUALITY_MODE": "true",
    }
    cfg = Config(env=env, load_dotenv_file=False)

    findings = [
        {
            "id": f"F-{i:04d}",
            "severity": "P2",
            "module": "m",
            "description": f"finding {i}",
        }
        for i in range(250)
    ]
    dependencies = [
        {
            "source": f"src/{i}.py",
            "target": f"dep{i}",
            "kind": "python_import",
        }
        for i in range(250)
    ]
    modules = [{"name": f"module-{i}", "path": f"m/{i}", "files": []} for i in range(250)]

    captured_contexts: list[str] = []

    def _capture_context(_skill: str, context: str) -> dict:
        captured_contexts.append(context)
        return {"findings": [], "assessment_summary": "ok", "recommended_actions": []}

    with Orchestrator(cfg) as orch:
        orch._store.write("findings", findings)
        orch._store.write("dependencies", dependencies)
        orch._store.write("modules", modules)
        orch._architect.specialist_review = _capture_context

        orch._run_additional_phase1_assessments({"systemic_patterns": []})

    assert captured_contexts
    context = captured_contexts[0]
    assert "F-0249" in context
    assert '"dep249"' in context
    assert '"module-249"' in context
    assert '"findings_sample"' not in context


def test_phase1_enforced_review_completeness_gate_blocks_on_incomplete_coverage(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "Program.cs").write_text(
        "namespace Demo; class Program { static void Main() {} }\n",
        encoding="utf-8",
    )

    env = {
        "SENTINEL_REPO_PATH": str(repo),
        "SENTINEL_LOCAL_ONLY_MODE": "true",
        "SENTINEL_ENFORCE_REVIEW_COMPLETENESS_GATE": "true",
    }
    cfg = Config(env=env, load_dotenv_file=False)

    forced_status = {
        "module": "src",
        "module_path": "src",
        "chunks_total": 1,
        "chunks_capped": 0,
        "quick_review_failed_chunks": 1,
        "specialist_failed_chunks": 0,
        "findings_count": 0,
        "skills_run": ["layer2a-quick-review"],
        "specialist_attempted_chunks": {},
        "specialist_failed_chunks_by_skill": {},
        "review_complete": False,
    }
    forced_failure = {
        "module": "src",
        "module_path": "src",
        "skill": "layer2a-quick-review",
        "chunk_index": 0,
        "total_chunks": 1,
        "start_line": 1,
        "end_line": 10,
        "error_type": "RuntimeError",
        "error": "forced failure",
    }

    with Orchestrator(cfg) as orch:
        with patch(
            "sentinel.assess.reviewer.ReviewOrchestrator.review_module_with_telemetry",
            return_value=([], forced_status, [forced_failure]),
        ):
            try:
                orch.run_phase1([repo])
                assert False, "Expected review completeness gate"
            except HumanGateRequired as gate:
                assert gate.gate_type == "review_completeness"
                assert "coverage_report_file" in gate.payload
