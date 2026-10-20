"""
sentinel/assess/cartographer.py

Stage 1 — Repository Discovery (Layer 0a).

Scans the target repository/repositories and produces the structured
manifest (codebase_manifest) consumed by all subsequent stages.

The Cartographer does the *local* scanning work (file tree, LOC counting,
language detection, etc.) and passes a pre-built context to the
Architect Agent which applies the Layer 0a skill prompt for structured
JSON output.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path, PurePath
from typing import Any

from sentinel.agents.architect import ArchitectAgent
from sentinel.assess.chunker import Chunker
from sentinel.assess.file_filter import EXCLUDE_DIRS, should_exclude_path
from sentinel.config import Config
from sentinel.orchestrator.context import build_cartographer_context
from sentinel.orchestrator.state_store import StateStore

log = logging.getLogger(__name__)

# File extensions → language names
_LANG_MAP: dict[str, str] = {
    ".py": "Python",
    ".js": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".jsx": "JavaScript",
    ".java": "Java",
    ".go": "Go",
    ".rs": "Rust",
    ".rb": "Ruby",
    ".cs": "C#",
    ".cpp": "C++",
    ".c": "C",
    ".kt": "Kotlin",
    ".swift": "Swift",
    ".php": "PHP",
    ".vb": "VB.NET",
    ".vbs": "VBScript",
    ".csproj": "MSBuild",
    ".sln": "Solution",
    ".wxs": "WiX",
    ".wixproj": "WiX",
    ".config": "XML",
    ".dart": "Dart",
    ".ex": "Elixir",
    ".exs": "Elixir",
    ".fs": "F#",
    ".fsx": "F#",
    ".hs": "Haskell",
    ".jl": "Julia",
    ".lua": "Lua",
    ".ml": "OCaml",
    ".pl": "Perl",
    ".pm": "Perl",
    ".r": "R",
    ".R": "R",
    ".scala": "Scala",
    ".sol": "Solidity",
    ".zig": "Zig",
    ".astro": "Astro",
    ".razor": "Razor",
    ".cshtml": "Razor",
    ".sass": "Sass",
    ".scss": "Sass",
    ".svelte": "Svelte",
    ".vue": "Vue",
    ".gradle": "Gradle",
    ".kts": "Gradle",
    ".graphql": "GraphQL",
    ".gql": "GraphQL",
    ".proto": "Protobuf",
    ".nix": "Nix",
    ".sh": "Shell",
    ".sql": "SQL",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".xml": "XML",
    ".json": "JSON",
    ".md": "Markdown",
    ".toml": "TOML",
    ".tf": "Terraform",
    ".html": "HTML",
    ".css": "CSS",
}

# Re-export shared exclusion set for backward compatibility.
# The canonical set lives in sentinel.assess.file_filter.
_EXCLUDE_DIRS = EXCLUDE_DIRS

# Files to include as samples in the cartographer context
_SAMPLE_EXTENSIONS = frozenset({
    ".py", ".js", ".ts", ".java", ".go", ".rs", ".cs", ".xml", ".vb", ".vbs",
    ".csproj", ".sln", ".wxs", ".wixproj", ".config"
})
_MAX_SAMPLE_FILES = 8
_MAX_SAMPLE_LINES = 60
_MAX_TREE_LINES = 800
_MAX_CONTEXT_CHARS = 120_000


class Cartographer:
    """
    Repository scanner for Phase 1 Stage 1.

    Performs local file system scanning and delegates structured analysis
    to the Architect Agent with the Layer 0a Codebase Cartographer skill.
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
        self._chunker = Chunker(
            max_lines=config.chunk_max_lines,
            overlap_lines=config.chunk_overlap_lines,
        )

    def run(
        self,
        repo_paths: list[Path],
        restricted_files: list[str] | None = None,
    ) -> dict[str, Any]:
        """
        Execute Stage 1: scan all repos and produce the codebase manifest.

        Args:
            repo_paths: List of repository root paths.
            restricted_files: Optional list of repo-relative source paths. When
                provided (``sentinel assess --changed-since``), the manifest is
                restricted to only these files so the assessment becomes a
                targeted verification pass over changed files. ``None`` (the
                default) assesses the full codebase unchanged.

        Returns:
            The codebase manifest dict, already written to
            ``sentinel/state/repositories.json`` and ``modules.json``.
        """
        log.info("Stage 1 — Codebase Cartographer: scanning %d repo(s)", len(repo_paths))

        manifest: dict[str, Any]
        if self._config.local_only_mode or self._config.strict_quality_mode:
            # Strict/local-only runs never call the model, so the context
            # assembly in the else-branch would be discarded unused. Skipping
            # it is load-bearing: in strict mode the sample collector reads
            # every file in the tree in full (no cap, no extension filter),
            # which exhausts memory on repos with large vendored/data trees.
            log.info("Using deterministic local cartographer manifest")
            manifest = self._build_local_manifest(repo_paths)
        else:
            # Local scan
            file_tree = self._build_file_tree(repo_paths)
            sample_files = self._collect_samples(repo_paths)
            loc_summary = self._count_loc(repo_paths)

            # Build context and call Architect with Layer 0a skill
            context = build_cartographer_context(
                repo_paths=[str(p) for p in repo_paths],
                file_tree=file_tree,
                sample_files=sample_files,
                strict_mode=self._config.strict_quality_mode,
            )

            # Append LOC summary to the context
            context += f"\n\n### LOC Summary (local scan)\n```json\n"
            import json
            context += json.dumps(loc_summary, indent=2)
            context += "\n```"

            # Keep request payload under endpoint limits.
            if len(context) > _MAX_CONTEXT_CHARS:
                log.warning(
                    "Cartographer context too large (%d chars); truncating to %d chars",
                    len(context),
                    _MAX_CONTEXT_CHARS,
                )
                context = (
                    context[:_MAX_CONTEXT_CHARS]
                    + "\n\n[truncated by cartographer: context exceeded payload cap]"
                )

            log.info("Calling Architect with Cartographer skill...")
            try:
                manifest = self._architect.map_codebase(context)
            except Exception as exc:
                log.warning(
                    "Cartographer model call failed (%s); using deterministic local fallback",
                    exc,
                )
                manifest = {
                    "repositories": [],
                    "modules": [],
                    "local_only": True,
                }

        # Some model responses place modules under each repository entry
        # instead of top-level `modules`. Recover that shape to keep
        # downstream stages robust.
        modules = manifest.get("modules", [])
        if not modules:
            recovered: list[dict[str, Any]] = []
            for repo in manifest.get("repositories", []):
                repo_modules = repo.get("modules", [])
                if isinstance(repo_modules, list):
                    for m in repo_modules:
                        if isinstance(m, dict):
                            recovered.append(m)
            modules = recovered
            if modules:
                log.warning(
                    "Recovered %d module(s) from repository-scoped manifest entries",
                    len(modules),
                )

        if not manifest.get("repositories") and not modules:
            log.warning(
                "Cartographer returned empty manifest; using deterministic local fallback"
            )
            manifest = self._build_local_manifest(repo_paths)
            modules = manifest.get("modules", [])

        # Keep the returned manifest's top-level modules consistent with the
        # recovered/persisted modules (the recovery block above updates the
        # local `modules` but not manifest["modules"]).
        manifest["modules"] = modules

        # Targeted verification pass (`--changed-since`): restrict the manifest
        # to only the changed source files before persisting so all downstream
        # stages assess just those files.
        if restricted_files is not None:
            modules = self._restrict_modules_to_files(modules, restricted_files)
            manifest["modules"] = modules

        # Operational scope: keep only production/runtime source (excludes
        # config/data, docs, scripts, project/build files, and tests) so a run
        # focuses on the code that does the work rather than grinding through
        # config trees. "all" preserves legacy behavior.
        if self._config.assessment_scope == "operational":
            modules = self._restrict_modules_to_operational(modules)
            manifest["modules"] = modules

        # Persist to state files
        self._store.write(
            "repositories",
            manifest.get("repositories", []),
            commit=True,
            commit_message="sentinel: Stage 1 — repository manifest",
        )
        self._store.write(
            "modules",
            modules,
            commit=True,
            commit_message="sentinel: Stage 1 — module inventory",
        )

        log.info(
            "Stage 1 complete: %d repo(s), %d module(s)",
            len(manifest.get("repositories", [])),
            len(modules),
        )
        return manifest

    def _restrict_modules_to_operational(
        self,
        modules: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Restrict each module's ``files`` to operational (production) code.

        Uses the language-agnostic ``is_operational_code`` classifier: config/
        data, docs, helper scripts, project/build files, and tests are dropped.
        Modules with no operational files remaining are removed so downstream
        stages only review the code that does the work.
        """
        from sentinel.assess.file_filter import is_operational_code

        restricted: list[dict[str, Any]] = []
        for module in modules:
            kept = [f for f in module.get("files", []) if is_operational_code(f)]
            if kept:
                restricted_module = dict(module)
                restricted_module["files"] = kept
                restricted.append(restricted_module)
        log.info(
            "Operational scope: %d module(s), %d file(s) after filtering non-operational paths",
            len(restricted),
            sum(len(m["files"]) for m in restricted),
        )
        return restricted

    def _restrict_modules_to_files(
        self,
        modules: list[dict[str, Any]],
        restricted_files: list[str],
    ) -> list[dict[str, Any]]:
        """Restrict each module's ``files`` list to *restricted_files*.

        Paths are compared with forward-slash normalization. Modules with no
        remaining files after the restriction are dropped so downstream stages
        only see the changed source. Used by the ``--changed-since`` targeted
        verification pass.
        """
        allowed = {f.replace("\\", "/") for f in restricted_files}
        restricted: list[dict[str, Any]] = []
        for module in modules:
            kept = [
                f for f in module.get("files", [])
                if f.replace("\\", "/") in allowed
            ]
            if kept:
                restricted_module = dict(module)
                restricted_module["files"] = kept
                restricted.append(restricted_module)
        log.info(
            "Restricted assessment (--changed-since): %d module(s), %d file(s)",
            len(restricted),
            sum(len(m["files"]) for m in restricted),
        )
        return restricted

    def _build_local_manifest(self, repo_paths: list[Path]) -> dict[str, Any]:
        """Build a deterministic manifest from local filesystem data.

        This fallback keeps Phase 1 usable in local-only mode and when model
        responses fail to return repository/module inventory.
        """
        repositories: list[dict[str, Any]] = []
        modules: list[dict[str, Any]] = []

        for repo in repo_paths:
            module_map: dict[str, list[str]] = {}
            language_counts: dict[str, int] = {}

            for path in repo.rglob("*"):
                if not path.is_file():
                    continue
                rel = path.relative_to(repo)
                if should_exclude_path(rel) or any(part.startswith(".") for part in rel.parts[:-1]):
                    continue

                file_ext = path.suffix.lower()
                language = _LANG_MAP.get(file_ext, "Other")
                language_counts[language] = language_counts.get(language, 0) + 1

                if language == "Other":
                    continue

                module_key = self._module_key_for(repo, rel)
                module_map.setdefault(module_key, []).append(str(rel).replace("\\", "/"))

            repositories.append({
                "name": repo.name,
                "path": str(repo),
                "languages": sorted([k for k, v in language_counts.items() if v > 0]),
                "module_count": len(module_map),
                "local_only": True,
            })

            for module_name, files in sorted(module_map.items()):
                modules.append({
                    "name": f"{repo.name}:{module_name}" if module_name != "." else f"{repo.name}:root",
                    "path": module_name,
                    "criticality": "medium",
                    "files": sorted(files),
                    "has_tests": any("test" in p.lower() for p in files),
                    "has_docs": any(p.lower().endswith(".md") for p in files),
                    "local_only": True,
                })

        return {
            "repositories": repositories,
            "modules": modules,
            "local_only": True,
        }

    def _module_key_for(self, repo: Path, rel: PurePath) -> str:
        """
        Decide the module key for a file path relative to *repo*.

        Splits one level deeper than the top-level directory when that
        top-level directory is a Python package (contains ``__init__.py``).
        Files that live directly inside the package keep the top-level
        key; files inside ``<pkg>/<subpkg>/...`` are bucketed by
        ``<pkg>/<subpkg>``. Non-package top-level directories and root
        files keep the previous behaviour.
        """
        parts = rel.parts
        if len(parts) <= 1:
            return "."
        top = parts[0]
        if (repo / top / "__init__.py").is_file():
            if len(parts) == 2:
                return top
            return f"{top}/{parts[1]}"
        return top

    # ------------------------------------------------------------------
    # Local scanning helpers
    # ------------------------------------------------------------------

    def _build_file_tree(self, repo_paths: list[Path]) -> str:
        """Build a combined file tree string for all repos."""
        lines = []
        max_depth = None if self._config.strict_quality_mode else 4
        for repo in repo_paths:
            lines.append(f"=== {repo} ===")
            lines.extend(self._tree(repo, indent=0, max_depth=max_depth))
        if self._config.strict_quality_mode:
            return "\n".join(lines)
        return "\n".join(lines[:_MAX_TREE_LINES])

    def _tree(self, path: Path, indent: int, max_depth: int | None) -> list[str]:
        """Recursive directory tree builder."""
        if max_depth is not None and indent > max_depth:
            return []
        lines = []
        prefix = "  " * indent

        try:
            entries = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name))
        except PermissionError:
            return [f"{prefix}[permission denied]"]

        for entry in entries:
            if entry.name in _EXCLUDE_DIRS or entry.name.startswith("."):
                continue
            if entry.is_dir():
                lines.append(f"{prefix}{entry.name}/")
                lines.extend(self._tree(entry, indent + 1, max_depth))
            else:
                lines.append(f"{prefix}{entry.name}")

        return lines

    def _collect_samples(self, repo_paths: list[Path]) -> dict[str, str]:
        """Collect representative source file samples."""
        samples: dict[str, str] = {}

        for repo in repo_paths:
            for path in repo.rglob("*"):
                if not self._config.strict_quality_mode and len(samples) >= _MAX_SAMPLE_FILES:
                    break
                if (
                    not self._config.strict_quality_mode
                    and path.suffix.lower() not in _SAMPLE_EXTENSIONS
                ):
                    continue
                if any(ex in path.parts for ex in _EXCLUDE_DIRS):
                    continue
                if not path.is_file():
                    continue
                try:
                    content_lines = path.read_text(
                        encoding="utf-8", errors="replace"
                    ).splitlines()
                    if not self._config.strict_quality_mode:
                        content_lines = content_lines[:_MAX_SAMPLE_LINES]
                    samples[str(path)] = "\n".join(content_lines)
                except OSError:
                    continue

        return samples

    def _count_loc(self, repo_paths: list[Path]) -> dict[str, Any]:
        """Count lines of code by language across all repos."""
        by_lang: dict[str, int] = {}
        file_counts: dict[str, int] = {}
        total_files = 0
        total_loc = 0

        for repo in repo_paths:
            for path in repo.rglob("*"):
                if not path.is_file():
                    continue
                if any(ex in path.parts for ex in _EXCLUDE_DIRS):
                    continue
                lang = _LANG_MAP.get(path.suffix.lower(), "Other")
                try:
                    loc = path.read_text(encoding="utf-8", errors="replace").count("\n")
                except OSError:
                    continue
                by_lang[lang] = by_lang.get(lang, 0) + loc
                file_counts[lang] = file_counts.get(lang, 0) + 1
                total_files += 1
                total_loc += loc

        return {
            "total_files": total_files,
            "total_loc": total_loc,
            "by_language": by_lang,
            "file_counts_by_language": file_counts,
        }
