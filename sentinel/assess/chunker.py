"""
sentinel/assess/chunker.py

Code chunker for Sentinel.

Splits large source files into context-window-sized pieces with configurable
overlap. Chunk boundaries respect logical structure — they prefer to split
at class and function definitions rather than mid-expression.

Used by the assessment engine before passing code to review agents.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

# Patterns that represent good chunk split points (Python-centric)
_SPLIT_PATTERNS = [
    re.compile(r"^class\s+\w+", re.MULTILINE),
    re.compile(r"^def\s+\w+", re.MULTILINE),
    re.compile(r"^async\s+def\s+\w+", re.MULTILINE),
    re.compile(r"^# -{4,}", re.MULTILINE),  # Section dividers
]


@dataclass(frozen=True)
class Chunk:
    """A single chunk of source code with metadata."""

    file_path: str
    """Path to the source file this chunk came from."""

    chunk_index: int
    """Zero-based index of this chunk within the file."""

    total_chunks: int
    """Total number of chunks for this file."""

    start_line: int
    """1-based line number of the first line in this chunk."""

    end_line: int
    """1-based line number of the last line in this chunk."""

    content: str
    """The source code content of this chunk."""

    @property
    def header(self) -> str:
        """Formatted header describing the chunk's position in the file."""
        return (
            f"# File: {self.file_path} "
            f"(chunk {self.chunk_index + 1}/{self.total_chunks}, "
            f"lines {self.start_line}–{self.end_line})"
        )

    @property
    def content_with_header(self) -> str:
        """Source content prefixed with the chunk header."""
        return f"{self.header}\n\n{self.content}"

    def __len__(self) -> int:
        return len(self.content)


class Chunker:
    """
    Splits source files into context-window-sized chunks with overlap.

    Chunk size is measured in lines. The overlap ensures that constructs
    spanning a chunk boundary are visible in both chunks.
    """

    def __init__(
        self,
        max_lines: int = 400,
        overlap_lines: int = 40,
    ) -> None:
        """
        Args:
            max_lines: Maximum lines per chunk.
            overlap_lines: Number of overlap lines between consecutive chunks.
        """
        if max_lines <= 0:
            raise ValueError("max_lines must be positive")
        if overlap_lines < 0:
            raise ValueError("overlap_lines must be non-negative")
        if overlap_lines >= max_lines:
            raise ValueError("overlap_lines must be less than max_lines")

        self._max_lines = max_lines
        self._overlap_lines = overlap_lines

    def chunk_file(self, file_path: str | Path) -> list[Chunk]:
        """
        Read a file and split it into chunks.

        Args:
            file_path: Path to the source file.

        Returns:
            List of Chunk objects. Returns a single chunk if the file is
            small enough to fit without splitting.

        Raises:
            OSError: If the file cannot be read.
        """
        path = Path(file_path)
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            log.error("Cannot read file %s: %s", path, exc)
            raise

        return self.chunk_string(content, file_path=str(path))

    def chunk_string(
        self,
        content: str,
        file_path: str = "<string>",
        preferred_split_hints: list[int] | None = None,
    ) -> list[Chunk]:
        """
        Split a string of source code into chunks.

        Args:
            content: Source code as a string.
            file_path: Path label used in chunk headers.
            preferred_split_hints: Optional line indices to prefer as split
                points. These are merged with discovered split points.

        Returns:
            List of Chunk objects.
        """
        if self._overlap_lines < 0:
            raise ValueError("overlap_lines must be non-negative")
        if self._overlap_lines >= self._max_lines:
            raise ValueError("overlap_lines must be less than max_lines")

        lines = content.splitlines()

        if len(lines) <= self._max_lines:
            return [
                Chunk(
                    file_path=file_path,
                    chunk_index=0,
                    total_chunks=1,
                    start_line=1,
                    end_line=len(lines),
                    content=content,
                )
            ]

        ast_split_points = self._find_ast_split_points(lines)
        if ast_split_points:
            split_points = ast_split_points
        else:
            split_points = self._find_split_points(lines)
        if preferred_split_hints:
            split_points = sorted(
                set(split_points).union(
                    pt for pt in preferred_split_hints if 0 < pt < len(lines)
                )
            )
        raw_chunks = list(self._split_at_points(lines, split_points))

        # Build Chunk objects with correct metadata
        total = len(raw_chunks)
        result = []
        for i, (start_idx, end_idx) in enumerate(raw_chunks):
            chunk_lines = lines[start_idx:end_idx]
            result.append(
                Chunk(
                    file_path=file_path,
                    chunk_index=i,
                    total_chunks=total,
                    start_line=start_idx + 1,
                    end_line=end_idx,
                    content="\n".join(chunk_lines),
                )
            )

        log.debug(
            "Chunked %s into %d chunk(s) (%d lines, max=%d, overlap=%d)",
            file_path,
            total,
            len(lines),
            self._max_lines,
            self._overlap_lines,
        )
        return result

    def chunk_content(
        self,
        file_path: str | Path,
        content: str,
    ) -> list[Chunk]:
        """
        Split a string of source code into chunks.

        Alias for ``chunk_string`` with argument order matching the test
        convention: ``chunk_content(file_path, content)``.

        Returns an empty list for empty or whitespace-only content.
        """
        if not content or not content.strip():
            return []
        return self.chunk_string(content, file_path=str(file_path))

    def iter_chunks(self, content: str, file_path: str = "<string>") -> Iterator[Chunk]:
        """Iterate over chunks without materialising the full list."""
        yield from self.chunk_string(content, file_path=file_path)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _find_ast_split_points(self, lines: list[str]) -> list[int]:
        """Return line indices of top-level function and class definition
        boundaries for Python source. Returns empty list if source is not
        valid Python or ast parsing fails."""
        import ast

        source = "\n".join(lines)
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return []
        points = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.col_offset == 0:
                    points.append(node.lineno - 1)
        return sorted(set(points))

    def _find_split_points(self, lines: list[str]) -> list[int]:
        """
        Find preferred split points in the source lines.

        Prefers class/function definitions and section dividers. Falls back
        to blank lines, then hard line-count splits.
        """
        preferred: set[int] = set()
        joined = "\n".join(lines)

        for pattern in _SPLIT_PATTERNS:
            for match in pattern.finditer(joined):
                # Map character offset → line index
                line_idx = joined[:match.start()].count("\n")
                preferred.add(line_idx)

        return sorted(preferred)

    def _split_at_points(
        self,
        lines: list[str],
        split_points: list[int],
    ) -> Iterator[tuple[int, int]]:
        """
        Yield (start_idx, end_idx) pairs for each chunk.

        Respects max_lines and overlap_lines constraints.
        """
        n = len(lines)
        start = 0

        while start < n:
            hard_end = min(start + self._max_lines, n)
            end = hard_end

            # Try to find a good split point before the hard limit
            if hard_end < n:
                best = hard_end
                # Only accept split points in the latter half of the window.
                # A split point near `start` must not be chosen: if the next
                # split point lies beyond the window (e.g. a large class body
                # with no top-level defs), picking the nearby one would pin
                # `end` and advance `start` one line at a time, exploding the
                # chunk count. The floor guarantees each chunk is at least
                # ~max_lines//2 lines, so the cursor always makes progress.
                floor = start + self._max_lines // 2
                min_end_for_progress = start + self._overlap_lines + 1
                min_acceptable_end = max(floor + 1, min_end_for_progress)
                for pt in reversed(split_points):
                    if min_acceptable_end <= pt <= hard_end:
                        best = pt
                        break
                else:
                    # No preferred split in the latter half — try blank lines
                    for i in range(hard_end - 1, floor, -1):
                        candidate = i + 1
                        if candidate < min_acceptable_end:
                            continue
                        if not lines[i].strip():
                            best = candidate
                            break
                end = best

            if end <= start:
                end = min(start + 1, n)

            yield (start, end)

            if end >= n:
                break

            # Next chunk starts with overlap
            next_start = end - self._overlap_lines
            if next_start <= start:
                next_start = start + 1
            start = next_start
