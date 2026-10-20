"""Quick scan: lightweight assessment of specific files or directories.

Unlike full ``sentinel assess``, this skips repo-wide cartography and dependency
mapping. It runs Quick Review + triggered specialists on the specified targets only.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sentinel.assess.file_filter import EXCLUDE_DIRS, should_exclude_path
from sentinel.assess.finding_kinds import (
    ACTIVE_SUPPORTED_REPORT_FINDING_KINDS,
    get_finding_kind_definition,
)
from sentinel.assess.reviewer import _SPECIALIST_TRIGGERS
from sentinel.assess.scan_context import ScanContext
from sentinel.config import Config


# File extension -> language name (lowercase)
_LANG_MAP: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".cs": "csharp",
    ".cpp": "cpp",
    ".c": "c",
    ".kt": "kotlin",
    ".swift": "swift",
    ".php": "php",
    ".sh": "shell",
    ".sql": "sql",
}

# Extensions considered scannable (known source code)
_SCANNABLE_EXTENSIONS: frozenset[str] = frozenset(_LANG_MAP.keys())

_SCAN_CATEGORY_TO_FINDING_KIND: dict[str, str] = {
    "security": "injection_risk",
    "error_handling": "error_signal_loss",
    "correctness": "logic_bug",
    "contract": "contract_violation",
    "contract_violation": "contract_violation",
}


@dataclass
class ScanResult:
    """Result of a quick scan."""

    target: str
    findings: list[dict]
    findings_by_severity: dict[str, int]
    specialists_triggered: list[str]
    scan_duration_ms: int

    def has_blockers(self) -> bool:
        """True if any Critical or P1 findings."""
        return (
            self.findings_by_severity.get("Critical", 0) > 0
            or self.findings_by_severity.get("P1", 0) > 0
        )

    def summary_text(self) -> str:
        """Human-readable summary."""
        total = len(self.findings)
        lines = [
            f"Scan: {self.target}",
            f"  Findings: {total}",
        ]
        for sev in ("Critical", "P1", "P2", "P3"):
            count = self.findings_by_severity.get(sev, 0)
            if count > 0:
                lines.append(f"    {sev}: {count}")
        if self.specialists_triggered:
            lines.append(f"  Specialists: {', '.join(self.specialists_triggered)}")
        lines.append(f"  Duration: {self.scan_duration_ms}ms")
        if self.has_blockers():
            lines.append("  ** BLOCKERS FOUND **")
        return "\n".join(lines)


class Scanner:
    """Quick file/directory scanner."""

    def __init__(self, config: Config | None = None) -> None:
        """Initialize with optional config. Uses default config if none provided."""
        self._config = config

    def scan_file(
        self,
        file_path: Path,
        scan_context: ScanContext | None = None,
    ) -> ScanResult:
        """Scan a single file.

        1. Read file content
        2. Determine language from extension
        3. Run quick review (deterministic pattern matching)
        4. Detect specialist triggers from content/findings
        5. Return ScanResult
        """
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        start_ms = _now_ms()
        content = file_path.read_text(encoding="utf-8", errors="replace")
        language = self._detect_language(file_path)
        if scan_context is None:
            repo_root = (
                self._config.repo_path
                if self._config and self._config.repo_path
                else file_path.parent
            )
            scan_context = ScanContext.from_repo_root(repo_root)

        # Deterministic quick review (pattern-based, no LLM)
        findings = self._quick_review_patterns(
            content,
            str(file_path),
            language,
            scan_context.repo_root_canonical,
        )

        # Detect triggered specialists
        triggered = self._detect_specialist_triggers(content, findings)

        # Run triggered specialist patterns
        for specialist in triggered:
            specialist_findings = self._specialist_patterns(
                specialist, content, str(file_path), language,
            )
            findings.extend(specialist_findings)

        # Deduplicate by description+location
        findings = self._dedup(findings)

        # Build severity counts
        severity_counts = self._count_severities(findings)

        elapsed = _now_ms() - start_ms
        return ScanResult(
            target=str(file_path),
            findings=findings,
            findings_by_severity=severity_counts,
            specialists_triggered=triggered,
            scan_duration_ms=elapsed,
        )

    def scan_directory(
        self,
        dir_path: Path,
        extensions: list[str] | None = None,
    ) -> list[ScanResult]:
        """Scan all matching files in a directory.

        1. Find files (filtered by extensions, respecting file_filter.py exclusions)
        2. Scan each file
        3. Return list of ScanResults
        """
        results: list[ScanResult] = []
        allowed_exts = frozenset(extensions) if extensions else _SCANNABLE_EXTENSIONS
        repo_root = Path(
            self._config.repo_path
            if self._config and self._config.repo_path
            else dir_path
        ).resolve()
        scan_context = ScanContext.from_repo_root(repo_root)

        for path in sorted(dir_path.rglob("*")):
            if not path.is_file():
                continue
            if should_exclude_path(path.relative_to(dir_path)):
                continue
            if path.suffix.lower() not in allowed_exts:
                continue
            results.append(self.scan_file(path, scan_context=scan_context))

        return results

    def _detect_language(self, file_path: Path) -> str:
        """Map file extension to language."""
        return _LANG_MAP.get(file_path.suffix.lower(), "unknown")

    def _detect_specialist_triggers(
        self,
        content: str,
        findings: list[dict],
    ) -> list[str]:
        """Determine which specialists should run based on content and initial findings.

        Uses same trigger keywords as reviewer.py's _SPECIALIST_TRIGGERS.
        """
        finding_text = " ".join(
            str(f.get("description", "")).lower() for f in findings
        )
        combined_text = (content.lower() + " " + finding_text)

        triggered_skills: set[str] = set()
        for keyword, skills in _SPECIALIST_TRIGGERS.items():
            if keyword in combined_text:
                for skill in skills:
                    # Extract the specialist name from the skill ID
                    # e.g. "layer2c-security-specialist" -> "security"
                    triggered_skills.add(_skill_to_name(skill))

        return sorted(triggered_skills)

    # ------------------------------------------------------------------
    # Pattern-based review (deterministic, no LLM)
    # ------------------------------------------------------------------

    def _quick_review_patterns(
        self,
        content: str,
        file_path: str,
        language: str,
        repo_root_canonical: str,
    ) -> list[dict]:
        """Run deterministic pattern checks (no LLM)."""
        findings: list[dict] = []
        lines = content.splitlines()

        for i, line in enumerate(lines, 1):
            lower_line = line.lower().strip()

            # SQL injection patterns
            if language == "python" and "f'" in line and "select" in lower_line:
                findings.append(self._make_finding(
                    file_path, i, "Critical", "security",
                    "Possible SQL injection via f-string query construction.",
                    "Use parameterized queries instead.",
                    repo_root_canonical,
                ))

            # Bare except
            if language == "python" and lower_line == "except:":
                findings.append(self._make_finding(
                    file_path, i, "P2", "error_handling",
                    "Bare except clause catches all exceptions including SystemExit.",
                    "Catch specific exception types.",
                    repo_root_canonical,
                ))

            # eval/exec usage
            if language == "python" and ("eval(" in line or "exec(" in line):
                findings.append(self._make_finding(
                    file_path, i, "P1", "security",
                    "Use of eval() or exec() may allow code injection.",
                    "Avoid eval/exec; use ast.literal_eval if needed.",
                    repo_root_canonical,
                ))

            # os.system usage
            if language == "python" and "os.system(" in line:
                findings.append(self._make_finding(
                    file_path, i, "P1", "security",
                    "os.system() is vulnerable to shell injection.",
                    "Use subprocess.run() with shell=False.",
                    repo_root_canonical,
                ))

            # Hardcoded passwords/secrets
            if any(kw in lower_line for kw in ("password =", "secret =", "api_key =")):
                if "'" in line or '"' in line:
                    findings.append(self._make_finding(
                        file_path, i, "P1", "security",
                        "Possible hardcoded secret or password.",
                        "Use environment variables or a secrets manager.",
                        repo_root_canonical,
                    ))

        return findings

    def _specialist_patterns(
        self,
        specialist: str,
        content: str,
        file_path: str,
        language: str,
    ) -> list[dict]:
        """Run additional specialist-specific patterns."""
        # Placeholder for future specialist-specific patterns.
        # The quick review already covers the main patterns.
        return []

    @staticmethod
    def _make_finding(
        file_path: str,
        line: int,
        severity: str,
        category: str,
        description: str,
        suggested_fix: str,
        repo_root_canonical: str,
    ) -> dict:
        """Create a finding dict in the standard schema."""
        from sentinel.assess.finding_schema import materialize_emitter_finding

        location = f"{file_path}:{line}"
        normalized_category = category.strip().lower()
        candidate_kind = _SCAN_CATEGORY_TO_FINDING_KIND.get(
            normalized_category,
            "logic_bug",
        )
        finding_kind = (
            candidate_kind
            if candidate_kind in ACTIVE_SUPPORTED_REPORT_FINDING_KINDS
            else "logic_bug"
        )

        finding_payload: dict[str, object] = {
            "module": "scan",
            "skill": f"quick-scan-{category}",
            "severity": severity,
            "category": category,
            "location": location,
            "description": description,
            "suggested_fix": suggested_fix,
            "status": "open",
            "finding_kind": finding_kind,
        }
        kind_definition = get_finding_kind_definition(finding_kind)
        if kind_definition and kind_definition.subject_required == "required":
            finding_payload["subject_key"] = normalized_category or "general"

        return materialize_emitter_finding(
            finding_payload,
            emitter_name=f"quick-scan-{category}",
            repo_root_canonical=repo_root_canonical,
        )

    @staticmethod
    def _dedup(findings: list[dict]) -> list[dict]:
        """Remove duplicate findings by description+location."""
        seen: set[str] = set()
        result: list[dict] = []
        for f in findings:
            key = f"{f.get('description', '')}|{f.get('location', '')}"
            if key not in seen:
                seen.add(key)
                result.append(f)
        return result

    @staticmethod
    def _count_severities(findings: list[dict]) -> dict[str, int]:
        """Count findings by severity level."""
        counts = {"Critical": 0, "P1": 0, "P2": 0, "P3": 0}
        for f in findings:
            sev = f.get("severity", "P3")
            if sev in counts:
                counts[sev] += 1
        return counts


def _skill_to_name(skill_id: str) -> str:
    """Extract a human-friendly specialist name from a skill ID.

    E.g. "layer2c-security-specialist" -> "security"
         "layer3e-concurrency-specialist" -> "concurrency"
         "layer3a-performance-specialist" -> "performance"
    """
    # Strip layer prefix (layerXx-)
    parts = skill_id.split("-")
    if len(parts) >= 2:
        # Skip the layer prefix, take the main name
        name_parts = parts[1:]
        # Remove trailing "specialist", "detector", "advisor"
        cleaned = [
            p for p in name_parts
            if p not in ("specialist", "detector", "advisor", "reviewer")
        ]
        if cleaned:
            return cleaned[0]
    return skill_id


def _now_ms() -> int:
    """Current time in milliseconds."""
    return int(time.monotonic() * 1000)
