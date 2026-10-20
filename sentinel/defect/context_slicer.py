"""
sentinel/defect/context_slicer.py

Targeted context extraction and slicing engine for Defect RCA.

Extracts stack trace frames, source snippets surrounding fault sites,
suspected files, and dependency graph neighborhoods while strictly
enforcing token / character budgets.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sentinel.defect.models import DefectReport
from sentinel.orchestrator.state_store import StateStore

log = logging.getLogger(__name__)

# Regex patterns for various language stack traces
_PYTHON_TRACE_RE = re.compile(
    r'File\s+["\'](?P<file>[^"\']+)["\'],\s+line\s+(?P<line>\d+)(?:,\s+in\s+(?P<func>[^\n\r]+))?'
)
_DOTNET_TRACE_RE = re.compile(
    r'in\s+(?P<file>[A-Za-z]:\\[^:\n\r]+|\/[^:\n\r]+):line\s+(?P<line>\d+)'
)
_GENERIC_COLON_RE = re.compile(
    r'(?P<file>[A-Za-z0-9_\-./\\]+\.[a-zA-Z0-9]+):(?P<line>\d+)(?::(?P<col>\d+))?'
)
_JS_TRACE_RE = re.compile(
    r'at\s+(?:(?P<func>[^\s(]+)\s+\()?(?P<file>[A-Za-z0-9_\-./\\]+\.[a-zA-Z0-9]+):(?P<line>\d+):(?P<col>\d+)\)?'
)


@dataclass
class ParsedFrame:
    file_path: str
    line_number: int
    function_name: str = ""
    raw_frame: str = ""


@dataclass
class FileSlice:
    file_path: str
    start_line: int
    end_line: int
    content: str
    is_crash_site: bool = False


@dataclass
class ContextSlice:
    defect_id: str
    parsed_frames: list[ParsedFrame] = field(default_factory=list)
    file_slices: list[FileSlice] = field(default_factory=list)
    dependency_context: list[str] = field(default_factory=list)
    formatted_context: str = ""


class ContextSlicer:
    """
    Builds targeted context slices for Defect Root Cause Analysis.
    """

    def __init__(
        self,
        repo_path: Path | str,
        store: StateStore | None = None,
        max_chars: int = 60_000,
        window_lines: int = 30,
    ) -> None:
        self.repo_path = Path(repo_path)
        self.store = store
        self.max_chars = max_chars
        self.window_lines = window_lines

    def parse_stack_trace(self, trace_text: str) -> list[ParsedFrame]:
        """
        Extract structured frames from raw stack trace text.
        """
        if not trace_text:
            return []

        frames: list[ParsedFrame] = []
        seen: set[tuple[str, int]] = set()

        # 1. Try Python style
        for m in _PYTHON_TRACE_RE.finditer(trace_text):
            f_path = m.group("file").strip()
            line_no = int(m.group("line"))
            func = (m.group("func") or "").strip()
            key = (f_path, line_no)
            if key not in seen:
                seen.add(key)
                frames.append(ParsedFrame(file_path=f_path, line_number=line_no, function_name=func))

        # 2. Try .NET style
        for m in _DOTNET_TRACE_RE.finditer(trace_text):
            f_path = m.group("file").strip()
            line_no = int(m.group("line"))
            key = (f_path, line_no)
            if key not in seen:
                seen.add(key)
                frames.append(ParsedFrame(file_path=f_path, line_number=line_no))

        # 3. Try JS/TS style
        for m in _JS_TRACE_RE.finditer(trace_text):
            f_path = m.group("file").strip()
            line_no = int(m.group("line"))
            func = (m.group("func") or "").strip()
            key = (f_path, line_no)
            if key not in seen:
                seen.add(key)
                frames.append(ParsedFrame(file_path=f_path, line_number=line_no, function_name=func))

        return frames

    def build_slice(self, defect: DefectReport) -> ContextSlice:
        """
        Construct a targeted context slice from a defect report.
        """
        frames = self.parse_stack_trace(defect.stack_trace)
        file_slices: list[FileSlice] = []
        budget_remaining = self.max_chars

        # Track processed files to avoid duplicate slices
        processed_files: set[str] = set()

        # 1. Process crash sites / parsed frames first (Highest priority)
        for frame in frames:
            resolved = self._resolve_file_path(frame.file_path)
            if not resolved or not resolved.is_file():
                continue

            rel_str = self._rel_path(resolved)
            f_slice = self._slice_file(resolved, target_line=frame.line_number, is_crash_site=True)
            if f_slice:
                file_slices.append(f_slice)
                processed_files.add(rel_str)
                budget_remaining -= len(f_slice.content)
                if budget_remaining <= 0:
                    break

        # 2. Process suspected files if budget permits
        if budget_remaining > 500:
            for suspected in defect.suspected_files:
                resolved = self._resolve_file_path(suspected)
                if not resolved or not resolved.is_file():
                    continue

                rel_str = self._rel_path(resolved)
                if rel_str in processed_files:
                    continue

                f_slice = self._slice_file(resolved, target_line=None, is_crash_site=False)
                if f_slice:
                    file_slices.append(f_slice)
                    processed_files.add(rel_str)
                    budget_remaining -= len(f_slice.content)
                    if budget_remaining <= 0:
                        break

        # 3. Lookup dependency neighborhood if available
        dep_context = self._get_dependency_context(processed_files)

        # 4. Format prompt-ready string
        formatted = self._format_context(defect, frames, file_slices, dep_context)

        # Truncate if total formatted exceeds max_chars
        if len(formatted) > self.max_chars:
            formatted = formatted[:self.max_chars] + "\n\n... [Context truncated to budget limit] ..."

        return ContextSlice(
            defect_id=defect.defect_id,
            parsed_frames=frames,
            file_slices=file_slices,
            dependency_context=dep_context,
            formatted_context=formatted,
        )

    def _resolve_file_path(self, raw_path: str) -> Path | None:
        p = Path(raw_path)
        if p.is_absolute() and p.exists():
            return p

        # Check relative to repo_path
        cand = self.repo_path / raw_path
        if cand.exists():
            return cand

        # Try matching filename under repo_path
        name = p.name
        matches = list(self.repo_path.rglob(name))
        if matches:
            return matches[0]

        return None

    def _rel_path(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.repo_path)).replace("\\", "/")
        except ValueError:
            return str(path).replace("\\", "/")

    def _slice_file(self, file_path: Path, target_line: int | None, is_crash_site: bool) -> FileSlice | None:
        try:
            lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            log.warning("Could not read file %s: %exc", file_path, exc)
            return None

        total_lines = len(lines)
        rel_name = self._rel_path(file_path)

        if target_line is not None and total_lines > 120:
            start = max(1, target_line - self.window_lines)
            end = min(total_lines, target_line + self.window_lines)
            selected_lines = lines[start - 1 : end]
            numbered = [f"{start + i:4d} | {line}" for i, line in enumerate(selected_lines)]
            content = "\n".join(numbered)
            return FileSlice(file_path=rel_name, start_line=start, end_line=end, content=content, is_crash_site=is_crash_site)
        else:
            # Whole file
            numbered = [f"{i + 1:4d} | {line}" for i, line in enumerate(lines)]
            content = "\n".join(numbered)
            return FileSlice(file_path=rel_name, start_line=1, end_line=total_lines, content=content, is_crash_site=is_crash_site)

    def _get_dependency_context(self, files: set[str]) -> list[str]:
        if not self.store:
            return []
        try:
            deps_raw = self.store.read("dependencies")
            if not isinstance(deps_raw, list):
                return []
            info: list[str] = []
            for item in deps_raw:
                mod = item.get("module", "")
                if any(f in mod for f in files):
                    imports = item.get("imports", [])
                    imported_by = item.get("imported_by", [])
                    info.append(f"- Module `{mod}`: imports {imports}, imported by {imported_by}")
            return info
        except Exception:
            return []

    def _format_context(
        self,
        defect: DefectReport,
        frames: list[ParsedFrame],
        file_slices: list[FileSlice],
        dep_context: list[str],
    ) -> str:
        parts: list[str] = []
        parts.append(f"### Defect Ingestion Context: {defect.defect_id}")
        parts.append(f"**Title**: {defect.title}")
        parts.append(f"**Severity**: {defect.severity}")
        parts.append(f"**Description**: {defect.description}")
        parts.append(f"**Symptoms**: {defect.symptoms}")

        if defect.reproduction_steps:
            parts.append(f"\n**Reproduction Steps**:\n{defect.reproduction_steps}")

        if defect.stack_trace:
            parts.append(f"\n**Raw Stack Trace / Logs**:\n```\n{defect.stack_trace}\n```")

        if frames:
            parts.append("\n**Parsed Stack Frames**:")
            for fr in frames:
                func_str = f" in `{fr.function_name}`" if fr.function_name else ""
                parts.append(f"- `{fr.file_path}:{fr.line_number}`{func_str}")

        if file_slices:
            parts.append("\n### Relevant Source Code Slices:")
            for sl in file_slices:
                badge = " (Crash Site)" if sl.is_crash_site else ""
                parts.append(f"\n#### File: `{sl.file_path}` (Lines {sl.start_line}-{sl.end_line}){badge}\n```\n{sl.content}\n```")

        if dep_context:
            parts.append("\n### Dependency Graph Neighborhood:")
            parts.extend(dep_context)

        return "\n".join(parts)
