"""
sentinel/assess/deterministic.py

Phase 1 deterministic lane scaffolding.

This lane produces deterministic findings without model calls and is gated
by SENTINEL_ENABLE_DETERMINISTIC_LANE.
"""

from __future__ import annotations

import ast
import re
from typing import Any, Protocol

from sentinel.assess.finding_kinds import (
    is_supported_report_contract_finding_kind,
)
from sentinel.assess.finding_schema import (
    _extract_file_path,
    materialize_emitter_finding,
)


class DeterministicAdapter(Protocol):
    """Adapter contract for deterministic lane analyzers."""

    def run(self, module: dict[str, Any], source_code: str) -> list[dict[str, Any]]:
        ...


class DeterministicFindingKindError(ValueError):
    """Raised when deterministic adapters emit a kind outside the active contract."""


class TodoMarkerAdapter:
    """Simple adapter that reports TODO markers in source."""

    def run(self, module: dict[str, Any], source_code: str) -> list[dict[str, Any]]:
        if "TODO" not in source_code:
            return []
        return [
            {
                "severity": "P3",
                "category": "maintainability",
                "description": "TODO markers found in source code.",
                "suggested_fix": "Resolve TODOs or convert them into tracked tasks.",
                "finding_kind": "structural_complexity",
            }
        ]


class FixmeMarkerAdapter:
    """Simple adapter that reports FIXME markers in source."""

    def run(self, module: dict[str, Any], source_code: str) -> list[dict[str, Any]]:
        if "FIXME" not in source_code:
            return []
        return [
            {
                "severity": "P2",
                "category": "correctness",
                "description": "FIXME markers found in source code.",
                "suggested_fix": "Address FIXME markers before promoting the module.",
                "finding_kind": "logic_bug",
            }
        ]


class PythonSyntaxAdapter:
    """Adapter that reports Python syntax errors for Python modules."""

    def run(self, module: dict[str, Any], source_code: str) -> list[dict[str, Any]]:
        module_path = str(module.get("path", "")).lower()
        if not module_path.endswith(".py"):
            return []

        try:
            ast.parse(source_code)
        except SyntaxError as exc:
            line = getattr(exc, "lineno", None)
            location = f"{module_path}:{line}" if line else module_path
            return [
                {
                    "severity": "P2",
                    "category": "correctness",
                    "location": location,
                    "description": f"Python syntax error: {exc.msg}",
                    "suggested_fix": "Fix syntax errors before further analysis.",
                    "finding_kind": "logic_bug",
                }
            ]

        return []


class DependencyRiskAdapter:
    """Adapter that flags pinned package versions below configured safe baselines."""

    _MANIFEST_FILES = {
        "requirements.txt",
        "requirements-dev.txt",
        "pyproject.toml",
        "pipfile",
        "pipfile.lock",
        "poetry.lock",
    }
    _PIN_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)\s*==\s*([0-9][A-Za-z0-9_.-]*)\s*$")
    _SAFE_MINIMUMS: dict[str, tuple[int, ...]] = {
        "django": (2, 2, 24),
        "flask": (2, 2, 5),
        "pyyaml": (5, 4),
    }

    def run(self, module: dict[str, Any], source_code: str) -> list[dict[str, Any]]:
        module_path = str(module.get("path", ""))
        manifest_name = module_path.rsplit("/", 1)[-1].rsplit("\\", 1)[-1].lower()
        if manifest_name not in self._MANIFEST_FILES:
            return []

        findings: list[dict[str, Any]] = []
        for raw_line in source_code.splitlines():
            match = self._PIN_RE.match(raw_line)
            if not match:
                continue
            package = match.group(1).lower()
            version = self._parse_version(match.group(2))
            minimum = self._SAFE_MINIMUMS.get(package)
            if not minimum or version >= minimum:
                continue

            findings.append(
                {
                    "severity": "P2",
                    "category": "security",
                    "location": module_path,
                    "description": (
                        f"Dependency risk: {package}=={match.group(2)} is below "
                        f"safe baseline {self._format_version(minimum)}."
                    ),
                    "suggested_fix": (
                        f"Upgrade {package} to >= {self._format_version(minimum)}."
                    ),
                    "finding_kind": "contract_violation",
                    "subject_key": f"{package}=={match.group(2)}",
                }
            )

        return findings

    def _parse_version(self, raw: str) -> tuple[int, ...]:
        parts = []
        for token in re.split(r"[^0-9]+", raw):
            if token:
                parts.append(int(token))
        return tuple(parts)

    def _format_version(self, version: tuple[int, ...]) -> str:
        return ".".join(str(p) for p in version)


class XmlConfigPolicyAdapter:
    """Adapter that flags insecure XML/config policy toggles."""

    _CONFIG_SUFFIXES = (".xml", ".config")
    _INSECURE_PATTERNS: tuple[tuple[str, str], ...] = (
        ("allowinsecure>true", "Disable AllowInsecure in configuration."),
        ("trustallcertificates>true", "Disable TrustAllCertificates in configuration."),
        ("sslverify>false", "Enable SSL verification in configuration."),
    )

    def run(self, module: dict[str, Any], source_code: str) -> list[dict[str, Any]]:
        module_path = str(module.get("path", "")).lower()
        if not module_path.endswith(self._CONFIG_SUFFIXES):
            return []

        lowered = source_code.lower().replace(" ", "")
        findings: list[dict[str, Any]] = []
        for pattern, fix in self._INSECURE_PATTERNS:
            if pattern in lowered:
                findings.append(
                    {
                        "severity": "P2",
                        "category": "security",
                        "location": module.get("path", module_path),
                        "description": f"Configuration policy risk detected: '{pattern}'.",
                        "suggested_fix": fix,
                        "finding_kind": "contract_violation",
                        "subject_key": pattern,
                    }
                )
        return findings


class DeterministicAnalyzer:
    """Run deterministic checks for a module and return normalized findings."""

    def __init__(self, adapters: list[DeterministicAdapter] | None = None) -> None:
        # Distinguish "no argument" (None -> default adapter set) from an
        # explicit empty list (caller wants zero adapters). ``adapters or [...]``
        # would wrongly substitute the defaults for an explicit ``[]``.
        self._adapters: list[DeterministicAdapter] = (
            adapters
            if adapters is not None
            else [
                TodoMarkerAdapter(),
                FixmeMarkerAdapter(),
                PythonSyntaxAdapter(),
                DependencyRiskAdapter(),
                XmlConfigPolicyAdapter(),
            ]
        )

    def review_module(
        self,
        module: dict[str, Any],
        source_code: str,
        *,
        repo_root_canonical: str = "",
    ) -> list[dict[str, Any]]:
        module_name = module.get("name", module.get("path", "unknown"))
        module_location = module.get("path", module_name)

        findings: list[dict[str, Any]] = []
        for adapter in self._adapters:
            raw_findings = adapter.run(module, source_code)
            for finding in raw_findings:
                if not isinstance(finding, dict):
                    continue
                finding_kind = str(
                    finding.get("finding_kind", "structural_complexity")
                    or "structural_complexity"
                ).strip()
                if not finding_kind:
                    finding_kind = "structural_complexity"

                if not is_supported_report_contract_finding_kind(finding_kind):
                    raise DeterministicFindingKindError(
                        "deterministic-lane emitted unsupported finding_kind "
                        f"'{finding_kind}' for module '{module_location}' from "
                        f"adapter '{adapter.__class__.__name__}'"
                    )

                location = finding.get("location", module_location)
                # Derive `file` from the location string so deterministic-lane
                # findings carry file attribution like every other emitter.
                # `_extract_file_path` is extension-anchored, so a location with
                # no path (e.g. a bare identifier) yields "" — no false
                # attribution. An adapter that already set `file` wins.
                file_value = str(finding.get("file", "") or "").strip()
                if not file_value:
                    file_value = _extract_file_path(str(location))

                normalised = {
                    "module": module_name,
                    # The deterministic lane is the deterministic half of the
                    # Layer-2a quick review; downstream dedup/grouping expect the
                    # quick-review skill name for correct consolidation. Emitter
                    # identity (report-contract gating) stays "deterministic-lane"
                    # via the emitter_name argument below.
                    "skill": "layer2a-quick-review",
                    "severity": finding.get("severity", "P3"),
                    "category": finding.get("category", "general"),
                    "location": location,
                    "file": file_value,
                    "description": finding.get("description", "deterministic finding"),
                    "suggested_fix": finding.get("suggested_fix", "Review finding details."),
                    "status": finding.get("status", "open"),
                    "finding_kind": finding_kind,
                }
                if "subject_key" in finding:
                    normalised["subject_key"] = finding["subject_key"]
                findings.append(
                    materialize_emitter_finding(
                        normalised,
                        emitter_name="deterministic-lane",
                        repo_root_canonical=repo_root_canonical,
                    )
                )

        return findings
