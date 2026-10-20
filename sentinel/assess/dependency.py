"""
sentinel/assess/dependency.py

Stage 2 — Dependency Mapping (Layer 0b).

Invokes the Architect Agent with the Dependency Analyzer skill to produce
the internal dependency graph, external dependency risk assessment,
data dependency map, change impact matrix, and cross-repo coupling scores.
"""

from __future__ import annotations

import logging
import re
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any

from sentinel.agents.architect import ArchitectAgent
from sentinel.config import Config
from sentinel.orchestrator.context import build_dependency_context
from sentinel.orchestrator.state_store import StateStore

log = logging.getLogger(__name__)

# Simple import extraction for Python (to bootstrap context for the model)
_IMPORT_RE = re.compile(
    r"^(?:from\s+(\S+)\s+import|import\s+(\S+))",
    re.MULTILINE,
)

_PROJECT_REF_RE = re.compile(
    r"<ProjectReference\s+Include=\"([^\"]+)\"",
    re.IGNORECASE,
)
_PACKAGE_REF_RE = re.compile(
    r"<PackageReference\s+Include=\"([^\"]+)\"",
    re.IGNORECASE,
)
_ASSEMBLY_REF_RE = re.compile(
    r"<Reference\s+Include=\"([^\"]+)\"",
    re.IGNORECASE,
)

# Names that indicate an entry point or structural root — always sampled first.
_PRIORITY_STEMS = frozenset(
    {"__init__", "main", "app", "setup", "manage", "wsgi", "asgi", "cli", "entrypoint"}
)


def _stratified_sample(files: list[Path], repo_root: Path, limit: int) -> list[Path]:
    """
    Return up to ``limit`` files using a stratified sample across directories.

    Strategy:
    1. Always include files whose stem is a known entry point / structural root.
    2. Bucket remaining files by their immediate parent directory.
    3. Round-robin across buckets until the limit is reached, so coverage
       is spread across the whole tree rather than pulled from one corner.
    """
    priority: list[Path] = []
    rest: list[Path] = []
    for f in files:
        if f.stem.lower() in _PRIORITY_STEMS:
            priority.append(f)
        else:
            rest.append(f)

    selected: list[Path] = priority[:limit]
    remaining = limit - len(selected)

    if remaining <= 0:
        return selected

    # Bucket by relative top-level directory so we sample across the tree
    buckets: dict[str, list[Path]] = defaultdict(list)
    for f in rest:
        try:
            parts = f.relative_to(repo_root).parts
            bucket_key = parts[0] if len(parts) > 1 else "__root__"
        except ValueError:
            bucket_key = "__root__"
        buckets[bucket_key].append(f)

    bucket_lists = list(buckets.values())
    idx = 0
    while remaining > 0 and any(bucket_lists):
        bucket = bucket_lists[idx % len(bucket_lists)]
        if bucket:
            selected.append(bucket.pop(0))
            remaining -= 1
        # Remove exhausted buckets
        bucket_lists = [b for b in bucket_lists if b]
        idx += 1

    return selected


class DependencyAnalyzer:
    """
    Stage 2: build the dependency graph using Layer 0b.

    Performs local import scanning to build a bootstrap context, then
    delegates structured analysis to the Architect Agent.
    """

    def __init__(
        self,
        architect: ArchitectAgent,
        store: StateStore,
        config: Config,
    ) -> None:
        self._architect = architect
        self._store = store
        self._config = config

    def run(self) -> dict[str, Any]:
        """
        Execute Stage 2: produce the dependency analysis.

        Reads the manifest from Stage 1, scans imports locally, then
        calls the Architect with the Dependency Analyzer skill.

        Returns:
            Dependency analysis dict, written to ``dependencies.json``.
        """
        manifest = {
            "repositories": self._store.read("repositories"),
            "modules": self._store.read("modules"),
        }

        log.info("Stage 2 — Dependency Analyzer: scanning imports")
        source_snippets = self._collect_import_snippets(manifest)

        context = build_dependency_context(
            manifest=manifest,
            source_snippets=source_snippets,
            strict_mode=self._config.strict_quality_mode,
        )

        analysis: dict[str, Any]
        if self._config.local_only_mode or self._config.strict_quality_mode:
            analysis = {"dependencies": []}
        else:
            log.info("Calling Architect with Dependency Analyzer skill...")
            try:
                analysis = self._architect.analyze_dependencies(context)
            except Exception as exc:
                log.warning(
                    "Dependency model analysis failed (%s); using local dependency enrichment",
                    exc,
                )
                analysis = {"dependencies": []}

        model_edges = analysis.get("dependencies", analysis.get("edges", []))
        model_edges_list = model_edges if isinstance(model_edges, list) else []
        local_edges = self._collect_local_dependency_edges(manifest)
        dependencies = self._merge_dependency_edges(model_edges_list, local_edges)
        analysis["dependencies"] = dependencies

        # Persist
        self._store.write(
            "dependencies",
            dependencies,
            commit=True,
            commit_message="sentinel: Stage 2 — dependency graph",
        )

        log.info(
            "Stage 2 complete: %d dependency edge(s)",
            len(dependencies),
        )
        return analysis

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _collect_import_snippets(self, manifest: dict[str, Any]) -> str:
        """
        Extract import statements from source files for model context.

        Uses a stratified sample across directories, always prioritising
        entry points and __init__.py files so the model sees meaningful
        structural coverage rather than an alphabetical slice.
        """
        snippets: list[str] = []
        repo_list = manifest.get("repositories", [])

        for repo in repo_list:
            repo_path = Path(repo.get("path", "."))
            if not repo_path.exists():
                continue

            all_py_files = list(repo_path.rglob("*.py"))
            total = len(all_py_files)

            if self._config.strict_quality_mode:
                py_files = all_py_files
            else:
                limit = self._config.dep_scan_limit
                if total <= limit:
                    py_files = all_py_files
                else:
                    py_files = _stratified_sample(all_py_files, repo_path, limit)
                    log.warning(
                        "Dependency scan sampled %d of %d Python files "
                        "(stratified by directory; set SENTINEL_DEP_SCAN_LIMIT to raise, "
                        "or SENTINEL_STRICT_QUALITY_MODE=true to scan all)",
                        len(py_files),
                        total,
                    )

            for py_file in py_files:
                try:
                    content = py_file.read_text(encoding="utf-8", errors="replace")
                    imports = _IMPORT_RE.findall(content)
                    if imports:
                        flat = [m[0] or m[1] for m in imports]
                        if not self._config.strict_quality_mode:
                            flat = flat[:30]
                        relative_path = py_file.relative_to(repo_path)
                        snippets.append(
                            f"## {relative_path}\n"
                            + "\n".join(f"  {imp}" for imp in flat)
                        )
                except OSError:
                    continue

        if self._config.strict_quality_mode:
            return "\n\n".join(snippets)
        return "\n\n".join(snippets)[:4000]  # cap total size (chars)

    def _collect_local_dependency_edges(self, manifest: dict[str, Any]) -> list[dict[str, Any]]:
        """Collect local dependency edges from Python and .NET project metadata."""
        edges: list[dict[str, Any]] = []
        repo_list = manifest.get("repositories", [])

        for repo in repo_list:
            repo_path = Path(repo.get("path", "."))
            if not repo_path.exists():
                continue

            # Python import edges
            for py_file in repo_path.rglob("*.py"):
                try:
                    content = py_file.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                imports = _IMPORT_RE.findall(content)
                for match in imports:
                    module_name = (match[0] or match[1]).split(".")[0].strip()
                    if not module_name:
                        continue
                    edges.append(
                        {
                            "source": str(py_file.relative_to(repo_path)).replace("\\", "/"),
                            "target": module_name,
                            "kind": "python_import",
                        }
                    )

            # .NET and installer project edges
            project_files = list(repo_path.rglob("*.csproj")) + list(repo_path.rglob("*.wixproj"))
            for proj in project_files:
                try:
                    content = proj.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue

                source = str(proj.relative_to(repo_path)).replace("\\", "/")
                for match in _PROJECT_REF_RE.findall(content):
                    target = match.replace("\\", "/")
                    edges.append(
                        {
                            "source": source,
                            "target": target,
                            "kind": "project_reference",
                            "is_external": ".." in Path(target).parts,
                        }
                    )

                for match in _PACKAGE_REF_RE.findall(content):
                    edges.append(
                        {
                            "source": source,
                            "target": match.strip(),
                            "kind": "package_reference",
                            "is_external": True,
                        }
                    )

                for match in _ASSEMBLY_REF_RE.findall(content):
                    assembly = match.split(",")[0].strip()
                    if not assembly:
                        continue
                    is_framework = assembly.startswith(("System", "Microsoft", "Windows")) or assembly == "mscorlib"
                    edges.append(
                        {
                            "source": source,
                            "target": assembly,
                            "kind": "assembly_reference",
                            "is_external": not is_framework,
                        }
                    )

        return self._merge_dependency_edges([], edges)

    def _merge_dependency_edges(
        self,
        model_edges: list[dict[str, Any]],
        local_edges: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Merge and deduplicate model and local dependency edges."""
        merged: list[dict[str, Any]] = []
        merged_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}

        for raw in [*model_edges, *local_edges]:
            if not isinstance(raw, dict):
                continue
            source = str(raw.get("source", "")).strip()
            target = str(raw.get("target", "")).strip()
            kind = str(raw.get("kind", "unknown")).strip() or "unknown"
            if not source or not target:
                continue
            key = (source, target, kind)
            if key in merged_by_key:
                existing = merged_by_key[key]
                if "is_external" in raw or "is_external" in existing:
                    existing["is_external"] = bool(existing.get("is_external", False)) or bool(
                        raw.get("is_external", False)
                    )
                continue
            edge = dict(raw)
            edge["source"] = source
            edge["target"] = target
            edge["kind"] = kind
            merged_by_key[key] = edge
            merged.append(edge)

        return merged
