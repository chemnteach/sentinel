"""Git-tracked learning store.

Persists learnings as individual JSON files under a store directory,
with an index.json manifest for quick lookups.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from sentinel.learnings.schema import Learning

log = logging.getLogger(__name__)

_INDEX_LOCK_TIMEOUT_SECONDS = 10.0
_INDEX_LOCK_POLL_SECONDS = 0.01

# Severity rank for min_severity filtering (higher = more severe). Learnings
# carry a ``severity`` field on the Critical/P1/P2/P3 scale used across Sentinel.
_SEVERITY_RANK = {"Critical": 4, "P1": 3, "P2": 2, "P3": 1}


class LearningStore:
    """Persist and query learnings as individual JSON files.

    Args:
        store_path: Directory to store learning files. Created if needed.
    """

    def __init__(self, store_path: Path) -> None:
        self._path = store_path
        self._path.mkdir(parents=True, exist_ok=True)

    def save(self, learnings: list[Learning]) -> list[Path]:
        """Save learnings as individual JSON files. Update index.json.

        Returns:
            List of paths to the saved JSON files.
        """
        saved_paths: list[Path] = []
        for learning in learnings:
            file_path = self._path / f"{learning.id}.json"
            file_path.write_text(
                json.dumps(learning.to_dict(), indent=2),
                encoding="utf-8",
            )
            saved_paths.append(file_path)

        self._update_index(learnings)
        return saved_paths

    def load_all(self) -> list[Learning]:
        """Load all learnings from store."""
        learnings: list[Learning] = []
        for file_path in sorted(self._path.glob("LRN-*.json")):
            try:
                data = json.loads(file_path.read_text(encoding="utf-8"))
                learnings.append(Learning.from_dict(data))
            except (json.JSONDecodeError, OSError, TypeError, KeyError) as e:
                # A single corrupt/unreadable learning file must not abort
                # loading of all the others — skip it with a diagnostic.
                log.warning("Skipping corrupt learning file %s: %s", file_path.name, e)
                continue
        return learnings

    def load_index(self) -> dict:
        """Load index.json -- manifest of all learnings."""
        index_path = self._path / "index.json"
        if not index_path.exists():
            return {"learnings": []}
        return json.loads(index_path.read_text(encoding="utf-8"))

    def _update_index(self, new_learnings: list[Learning]) -> None:
        """Update index.json with new learnings (append, don't overwrite).

        The whole read-modify-write is serialized under an interprocess lock so
        two concurrent ``save()`` calls cannot each read the same index image
        and clobber the other's appended entries. (FIX-CRITICAL run-019:
        unguarded index RMW)
        """
        index_path = self._path / "index.json"
        lock_path = self._path / "index.json.lock"
        lock_fd = self._acquire_index_lock(lock_path)
        try:
            index = self.load_index()
            existing_ids = {entry["id"] for entry in index["learnings"]}

            for learning in new_learnings:
                if learning.id not in existing_ids:
                    index["learnings"].append({
                        "id": learning.id,
                        "category": learning.category.value if hasattr(learning.category, "value") else learning.category,
                        "severity": learning.severity,
                        "applicable_to_generation": learning.applicable_to_generation,
                        "extracted_at": learning.extracted_at,
                    })
                    existing_ids.add(learning.id)

            index_path.write_text(
                json.dumps(index, indent=2),
                encoding="utf-8",
            )
        finally:
            self._release_index_lock(lock_path, lock_fd)

    @staticmethod
    def _acquire_index_lock(lock_path: Path) -> int:
        """Acquire the interprocess lock guarding the index.json RMW."""
        deadline = time.monotonic() + _INDEX_LOCK_TIMEOUT_SECONDS
        while True:
            try:
                return os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            except (FileExistsError, PermissionError):
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "Timed out waiting for learnings index lock; another "
                        "writer may hold a stale lock."
                    )
                time.sleep(_INDEX_LOCK_POLL_SECONDS)

    @staticmethod
    def _release_index_lock(lock_path: Path, lock_fd: int) -> None:
        """Release the learnings index lock acquired above."""
        try:
            os.close(lock_fd)
        finally:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                return
            except OSError:
                return

    def query(
        self,
        category: str | None = None,
        applicable_to_generation: bool | None = None,
        min_severity: str | None = None,
    ) -> list[Learning]:
        """Query learnings with filters.

        Args:
            category: Filter by LearningCategory value string.
            applicable_to_generation: Filter by applicability flag.
            min_severity: Minimum severity to include, on the Critical/P1/P2/P3
                scale. Learnings whose severity ranks below this floor are
                excluded. An unknown value (or None) applies no severity filter.

        Returns:
            Filtered list of Learning objects.
        """
        all_learnings = self.load_all()
        result: list[Learning] = []

        # Resolve the minimum severity rank once. Unknown/None min_severity
        # values impose no floor (rank 0 admits everything).
        min_rank = _SEVERITY_RANK.get(min_severity, 0) if min_severity else 0

        for learning in all_learnings:
            if category is not None:
                cat_value = learning.category.value if hasattr(learning.category, "value") else learning.category
                if cat_value != category:
                    continue
            if applicable_to_generation is not None:
                if learning.applicable_to_generation != applicable_to_generation:
                    continue
            if min_rank:
                learning_rank = _SEVERITY_RANK.get(learning.severity, 0)
                if learning_rank < min_rank:
                    continue
            result.append(learning)

        return result
