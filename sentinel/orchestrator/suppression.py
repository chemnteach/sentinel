"""
sentinel/orchestrator/suppression.py

S3-5 — Suppression rules (learning pass).

A ``SuppressionManager`` reads operator-authored suppression rules from
``suppression_rules.json`` in the run state directory and filters accumulated
findings before tech-debt triage (Stage 4d). Suppressed findings are stamped
with the matching ``rule_id`` for traceability and diverted to a
``suppressed_findings.json`` sidecar rather than being deleted, so the
suppression decision remains auditable.

A rule matches a finding when ALL of its set fields match:
  * ``file``           — exact match OR glob (``fnmatch``) against ``finding.file``
  * ``finding_kind``   — exact match against ``finding.finding_kind``
  * ``location_pattern`` (optional) — substring of ``finding.location``

A finding matching ANY rule is suppressed.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_RULES_FILENAME = "suppression_rules.json"
_SUPPRESSED_FILENAME = "suppressed_findings.json"
_DEFAULT_CREATED_BY = "operator"


class SuppressionManager:
    """Load, apply, and persist finding-suppression rules for a run."""

    def __init__(self, state_dir: Path) -> None:
        self._state_dir = Path(state_dir)
        self._rules_path = self._state_dir / _RULES_FILENAME
        self._suppressed_path = self._state_dir / _SUPPRESSED_FILENAME

    # ------------------------------------------------------------------
    # Rule loading / listing
    # ------------------------------------------------------------------

    def load_rules(self) -> list[dict]:
        """Return the persisted rules, or ``[]`` if missing/unreadable.

        A missing file or malformed JSON is treated as "no rules" rather
        than an error — suppression is an optional learning pass and must
        never abort the assessment.
        """
        try:
            text = self._rules_path.read_text(encoding="utf-8")
        except OSError:
            return []
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            log.warning(
                "suppression_rules.json is not valid JSON — ignoring all rules"
            )
            return []
        if not isinstance(data, list):
            log.warning(
                "suppression_rules.json is not a JSON array — ignoring all rules"
            )
            return []
        return [r for r in data if isinstance(r, dict)]

    def list_rules(self) -> list[dict]:
        """Return all persisted rules."""
        return self.load_rules()

    # ------------------------------------------------------------------
    # Application
    # ------------------------------------------------------------------

    def apply(self, findings: list[dict]) -> tuple[list[dict], list[dict]]:
        """Split ``findings`` into ``(active, suppressed)`` by the loaded rules.

        A finding matching ANY rule is suppressed and stamped with the
        matching rule's ``rule_id`` under ``suppressed_by``. Findings are
        never mutated in place — suppressed entries are shallow copies.
        """
        rules = self.load_rules()
        if not rules:
            return list(findings), []

        active: list[dict] = []
        suppressed: list[dict] = []
        for finding in findings:
            rule_id = self._first_matching_rule_id(finding, rules)
            if rule_id is None:
                active.append(finding)
            else:
                stamped = dict(finding)
                stamped["suppressed_by"] = rule_id
                suppressed.append(stamped)
        return active, suppressed

    @staticmethod
    def _first_matching_rule_id(
        finding: dict, rules: list[dict]
    ) -> str | None:
        """Return the ``rule_id`` of the first rule that matches, else None."""
        for rule in rules:
            if _rule_matches(rule, finding):
                return rule.get("rule_id")
        return None

    # ------------------------------------------------------------------
    # Rule mutation
    # ------------------------------------------------------------------

    def add_rule(self, rule: dict) -> None:
        """Append a rule, assigning ``rule_id``/``created``/``created_by``.

        A supplied ``rule_id`` is preserved; otherwise a uuid4 hex id is
        assigned. ``created`` and ``created_by`` default when absent. The
        rules file is persisted atomically (temp + replace).
        """
        new_rule = dict(rule)
        new_rule.setdefault("rule_id", uuid.uuid4().hex)
        new_rule.setdefault("created", datetime.now(timezone.utc).isoformat())
        new_rule.setdefault("created_by", _DEFAULT_CREATED_BY)

        rules = self.load_rules()
        rules.append(new_rule)
        self._write_rules(rules)

    def remove_rule(self, rule_id: str) -> None:
        """Drop the rule with ``rule_id`` and persist the remainder."""
        rules = [r for r in self.load_rules() if r.get("rule_id") != rule_id]
        self._write_rules(rules)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _write_rules(self, rules: list[dict]) -> None:
        """Persist rules atomically (write temp, then replace)."""
        self._state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._rules_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rules, indent=2), encoding="utf-8")
        os.replace(tmp, self._rules_path)


def _rule_matches(rule: dict, finding: dict) -> bool:
    """Return True when every set field of ``rule`` matches ``finding``.

    A rule with no matchable fields (no ``file`` and no ``finding_kind``)
    never matches, so an empty/degenerate rule cannot suppress everything.
    """
    rule_file = str(rule.get("file", "") or "")
    rule_kind = str(rule.get("finding_kind", "") or "")
    location_pattern = str(rule.get("location_pattern", "") or "")

    if not rule_file and not rule_kind:
        return False

    if rule_file:
        finding_file = str(finding.get("file", "") or "")
        if finding_file != rule_file and not fnmatch(finding_file, rule_file):
            return False

    if rule_kind:
        if str(finding.get("finding_kind", "") or "") != rule_kind:
            return False

    if location_pattern:
        if location_pattern not in str(finding.get("location", "") or ""):
            return False

    return True
