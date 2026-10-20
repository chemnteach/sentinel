from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from sentinel.assess.dependency import DependencyAnalyzer
from sentinel.config import Config
from sentinel.orchestrator.state_store import StateStore


def _cfg(tmp_path: Path) -> Config:
    env = {
        "SENTINEL_REPO_PATH": str(tmp_path),
        "SENTINEL_LOCAL_ONLY_MODE": "true",
    }
    return Config(env=env, load_dotenv_file=False)


def _cfg_live(tmp_path: Path) -> Config:
    """Config with local_only_mode off so the model call path is exercised."""
    env = {
        "SENTINEL_REPO_PATH": str(tmp_path),
        "SENTINEL_LOCAL_ONLY_MODE": "false",
    }
    return Config(env=env, load_dotenv_file=False)


def test_collect_local_dependency_edges_extracts_dotnet_references(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    proj_dir = repo / "src"
    proj_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / "App.csproj").write_text(
        "\n".join(
            [
                "<Project Sdk=\"Microsoft.NET.Sdk\">",
                "  <ItemGroup>",
                "    <ProjectReference Include=\"..\\Shared\\Shared.csproj\" />",
                "    <PackageReference Include=\"Newtonsoft.Json\" Version=\"13.0.1\" />",
                "    <Reference Include=\"Contoso.LineFw\" />",
                "  </ItemGroup>",
                "</Project>",
            ]
        ),
        encoding="utf-8",
    )

    store = MagicMock(spec=StateStore)
    architect = MagicMock()
    analyzer = DependencyAnalyzer(architect=architect, store=store, config=_cfg(tmp_path))

    manifest = {"repositories": [{"path": str(repo)}], "modules": []}
    edges = analyzer._collect_local_dependency_edges(manifest)

    kinds = {edge["kind"] for edge in edges}
    assert "project_reference" in kinds
    assert "package_reference" in kinds
    assert "assembly_reference" in kinds

    project_targets = [edge["target"] for edge in edges if edge["kind"] == "project_reference"]
    assert "../Shared/Shared.csproj" in project_targets

    external_assemblies = [edge for edge in edges if edge["kind"] == "assembly_reference"]
    assert any(edge.get("is_external") for edge in external_assemblies)


def test_run_merges_model_and_local_edges(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "App.csproj").write_text(
        "<Project><ItemGroup><Reference Include=\"Contoso.LineFw\" /></ItemGroup></Project>",
        encoding="utf-8",
    )

    store = MagicMock(spec=StateStore)
    store.read.side_effect = lambda name: {
        "repositories": [{"path": str(repo), "name": "repo"}],
        "modules": [],
    }[name]

    architect = MagicMock()
    architect.analyze_dependencies.return_value = {
        "dependencies": [
            {"source": "model", "target": "edge", "kind": "model_edge"}
        ]
    }

    analyzer = DependencyAnalyzer(architect=architect, store=store, config=_cfg_live(tmp_path))
    analyzer.run()

    write_call = store.write.call_args
    assert write_call.args[0] == "dependencies"
    written_edges = write_call.args[1]
    assert any(edge.get("kind") == "model_edge" for edge in written_edges)
    assert any(edge.get("kind") == "assembly_reference" for edge in written_edges)


def test_run_falls_back_to_local_edges_when_model_fails(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "App.csproj").write_text(
        "<Project><ItemGroup><Reference Include=\"Contoso.LineFw\" /></ItemGroup></Project>",
        encoding="utf-8",
    )

    store = MagicMock(spec=StateStore)
    store.read.side_effect = lambda name: {
        "repositories": [{"path": str(repo), "name": "repo"}],
        "modules": [],
    }[name]

    architect = MagicMock()
    architect.analyze_dependencies.side_effect = RuntimeError("upstream unavailable")

    analyzer = DependencyAnalyzer(architect=architect, store=store, config=_cfg(tmp_path))
    analysis = analyzer.run()

    assert analysis.get("dependencies")
    write_call = store.write.call_args
    written_edges = write_call.args[1]
    assert any(edge.get("kind") == "assembly_reference" for edge in written_edges)
