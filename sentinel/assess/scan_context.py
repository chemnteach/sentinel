"""Scan-scoped repository identity context for stable finding IDs.

``ScanContext`` captures the canonical repository root used by F2 identity
derivation. Full assessments persist this metadata once per scan so resumed
work can recover the same root without consulting mutable configuration.
Quick scans may construct an ephemeral context for exploratory-only IDs.

Construction from a repository root uses ``Path.resolve()``, so symlinked
checkout paths canonicalize to their resolved targets. This makes the scan
identity depend on the real filesystem root rather than on a caller's symlink
spelling. Metadata rehydration deliberately does not check whether the stored
path still exists; persisted identity can be useful for ID reconstruction even
when resumption code must separately validate filesystem availability.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Mapping


_WINDOWS_DRIVE_PREFIX_RE = re.compile(r"^(?P<drive>[A-Za-z]):(?=/)")


@dataclass(frozen=True, slots=True)
class ScanContext:
    """Repository-root context for one Sentinel scan."""

    repo_root_path: Path
    repo_root_canonical: str

    @classmethod
    def from_repo_root(cls, repo_root: Path) -> "ScanContext":
        """Create a scan context from an assessed repository root path.

        ``Path.resolve()`` intentionally follows symlinks so the canonical
        scan identity uses the resolved filesystem root.
        """

        resolved_root = Path(repo_root).resolve()
        return cls(
            repo_root_path=resolved_root,
            repo_root_canonical=_canonicalize_repo_root(resolved_root),
        )

    @classmethod
    def from_metadata(cls, payload: Mapping[str, object]) -> "ScanContext":
        """Rehydrate a scan context from persisted scan metadata.

        This validates that identity fields are present, but it does not check
        path existence. Resumption paths that need to read source files should
        perform their own filesystem validation in context.
        """

        repo_root_path = str(payload.get("repo_root_path", "") or "").strip()
        repo_root_canonical = str(
            payload.get("repo_root_canonical", "") or ""
        ).strip()
        if not repo_root_path:
            raise ValueError("scan metadata missing repo_root_path")
        if not repo_root_canonical:
            raise ValueError("scan metadata missing repo_root_canonical")

        return cls(
            repo_root_path=Path(repo_root_path),
            repo_root_canonical=repo_root_canonical,
        )

    def to_metadata(self) -> dict[str, str]:
        """Serialize this context for state-store persistence."""

        return {
            "repo_root_path": self.repo_root_path.as_posix(),
            "repo_root_canonical": self.repo_root_canonical,
        }


def _canonicalize_repo_root(repo_root: Path) -> str:
    """Return the canonical root string used for F2 location stripping."""

    canonical = repo_root.as_posix().replace("\\", "/")
    canonical = _WINDOWS_DRIVE_PREFIX_RE.sub(
        lambda match: match.group("drive").lower() + ":",
        canonical,
    )
    canonical = re.sub(r"/{2,}", "/", canonical)
    if canonical == "/" or re.fullmatch(r"[a-z]:/", canonical):
        return canonical
    return canonical.rstrip("/")


__all__ = ["ScanContext"]