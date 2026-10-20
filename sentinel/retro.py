"""Retrospective analysis of Sentinel assessments.

Analyzes findings, learnings, and assessment patterns to produce
a retrospective report. Can analyze a single assessment or trends
across multiple assessments.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class RetroReport:
    """Retrospective report for an assessment."""

    repo_name: str
    total_findings: int
    findings_by_severity: dict[str, int]
    findings_by_category: dict[str, int]
    top_categories: list[tuple[str, int]]
    learnings_extracted: int
    dedup_ratio: float
    specialists_used: list[str]

    # Quality signals
    false_positive_rate: float | None
    noise_categories: list[str]

    # Recommendations
    trigger_gaps: list[str]
    persona_improvements: list[str]

    def to_markdown(self) -> str:
        """Format as markdown report."""
        lines: list[str] = []
        lines.append(f"# Retrospective: {self.repo_name}")
        lines.append("")

        lines.append("## Summary")
        lines.append(f"- **Total findings:** {self.total_findings}")
        if self.total_findings == 0:
            lines.append("- No findings to analyze.")
            lines.append("")
            return "\n".join(lines)

        lines.append(f"- **Learnings extracted:** {self.learnings_extracted}")
        lines.append(f"- **Dedup ratio:** {self.dedup_ratio:.1%}")
        if self.false_positive_rate is not None:
            lines.append(f"- **False positive rate:** {self.false_positive_rate:.1%}")
        lines.append("")

        lines.append("## Findings by Severity")
        for sev in ["Critical", "P1", "P2", "P3"]:
            count = self.findings_by_severity.get(sev, 0)
            if count:
                lines.append(f"- **{sev}:** {count}")
        lines.append("")

        lines.append("## Top Categories")
        for cat, count in self.top_categories:
            lines.append(f"- **{cat}:** {count}")
        lines.append("")

        if self.specialists_used:
            lines.append("## Specialists Used")
            for spec in self.specialists_used:
                lines.append(f"- {spec}")
            lines.append("")

        if self.trigger_gaps:
            lines.append("## Trigger Gaps")
            lines.append("Categories with findings but no specialist triggered:")
            for gap in self.trigger_gaps:
                lines.append(f"- {gap}")
            lines.append("")

        if self.noise_categories:
            lines.append("## Noise Categories")
            for cat in self.noise_categories:
                lines.append(f"- {cat}")
            lines.append("")

        if self.persona_improvements:
            lines.append("## Persona Improvements")
            for imp in self.persona_improvements:
                lines.append(f"- {imp}")
            lines.append("")

        return "\n".join(lines)

    def to_dict(self) -> dict:
        """Serialize to dict."""
        return {
            "repo_name": self.repo_name,
            "total_findings": self.total_findings,
            "findings_by_severity": self.findings_by_severity,
            "findings_by_category": self.findings_by_category,
            "top_categories": self.top_categories,
            "learnings_extracted": self.learnings_extracted,
            "dedup_ratio": self.dedup_ratio,
            "specialists_used": self.specialists_used,
            "false_positive_rate": self.false_positive_rate,
            "noise_categories": self.noise_categories,
            "trigger_gaps": self.trigger_gaps,
            "persona_improvements": self.persona_improvements,
        }


class RetroAnalyzer:
    """Generates retrospective reports from assessment data."""

    def __init__(
        self,
        state_store_path: Path,
        learnings_path: Path | None = None,
    ) -> None:
        """Initialize with paths to state and learnings.

        Args:
            state_store_path: Directory containing state JSON files.
            learnings_path: Directory containing learning files. Defaults
                to state_store_path / "learnings".
        """
        self._state_path = state_store_path
        self._learnings_path = learnings_path or state_store_path / "learnings"

    def analyze(self) -> RetroReport:
        """Analyze current assessment data and produce retrospective."""
        findings = self._load_json_list("findings.json")
        tech_debt = self._load_json_list("tech_debt.json")
        learnings_count = self._count_learnings()

        if not findings:
            return RetroReport(
                repo_name=self._state_path.parent.name if self._state_path.exists() else "unknown",
                total_findings=0,
                findings_by_severity={},
                findings_by_category={},
                top_categories=[],
                learnings_extracted=learnings_count,
                dedup_ratio=0.0,
                specialists_used=[],
                false_positive_rate=None,
                noise_categories=[],
                trigger_gaps=[],
                persona_improvements=[],
            )

        severity_counts = dict(Counter(f.get("severity", "P3") for f in findings))
        category_counts = dict(Counter(f.get("category", "unknown") for f in findings))
        top_categories = self._identify_top_categories(findings)
        specialists_used = list({f.get("skill", "") for f in findings if f.get("skill")})
        dedup_ratio = self._calculate_dedup_ratio(findings)
        trigger_gaps = self._identify_trigger_gaps(findings, specialists_used)
        persona_improvements = self._generate_persona_improvements(findings, [])

        # Calculate false positive rate from triage data
        false_positive_rate = None
        noise_categories: list[str] = []
        if tech_debt:
            excluded = [t for t in tech_debt if t.get("human_triage") == "EXCLUDE"]
            triaged = [t for t in tech_debt if t.get("human_triage") is not None]
            if triaged:
                false_positive_rate = len(excluded) / len(triaged)
                # Identify categories with high false positive rates
                excluded_cats = Counter(
                    t.get("category", t.get("skill", "unknown")) for t in excluded
                )
                for cat, count in excluded_cats.items():
                    cat_total = sum(
                        1 for t in triaged
                        if (t.get("category", t.get("skill", "unknown")) == cat)
                    )
                    if cat_total > 0 and count / cat_total > 0.5:
                        noise_categories.append(cat)

        repo_name = "unknown"
        repos_data = self._load_json_list("repositories.json")
        if repos_data and isinstance(repos_data, list) and repos_data:
            repo_name = repos_data[0].get("name", "unknown")
        elif self._state_path.exists():
            repo_name = self._state_path.parent.name

        return RetroReport(
            repo_name=repo_name,
            total_findings=len(findings),
            findings_by_severity=severity_counts,
            findings_by_category=category_counts,
            top_categories=top_categories,
            learnings_extracted=learnings_count,
            dedup_ratio=dedup_ratio,
            specialists_used=specialists_used,
            false_positive_rate=false_positive_rate,
            noise_categories=noise_categories,
            trigger_gaps=trigger_gaps,
            persona_improvements=persona_improvements,
        )

    def _load_json_list(self, filename: str) -> list[dict]:
        """Load a JSON file from the state directory, returning [] on failure."""
        path = self._state_path / filename
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            # A corrupt/unreadable state file must not silently vanish into an
            # empty list — the retro would understate findings with no signal.
            log.warning("Could not load %s (treating as empty): %s", filename, exc)
            return []
        if not isinstance(data, list):
            log.warning(
                "%s did not contain a JSON list (got %s); treating as empty.",
                filename,
                type(data).__name__,
            )
            return []
        return data

    def _count_learnings(self) -> int:
        """Count learnings in the learnings store."""
        if not self._learnings_path.exists():
            return 0
        try:
            from sentinel.learnings.store import LearningStore
            store = LearningStore(self._learnings_path)
            return len(store.load_all())
        except Exception as exc:
            # A corrupt/unreadable store must not masquerade as a legitimate
            # empty store (count 0) with no signal — emit a diagnostic so the
            # load failure is visible while preserving the safe 0 fallback.
            # (FIX-DESIRED-014)
            log.warning(
                "Learnings store unreadable at %s; counting as 0 learnings: %s",
                self._learnings_path, exc,
            )
            return 0

    def _calculate_dedup_ratio(self, findings: list[dict]) -> float:
        """How much dedup reduced findings (0.0 = no dupes, 1.0 = all dupes).

        Measures the fraction of findings that share an identical description
        with at least one other finding.
        """
        if not findings:
            return 0.0
        descriptions = [f.get("description", "") for f in findings]
        counts = Counter(descriptions)
        duplicated = sum(count - 1 for count in counts.values() if count > 1)
        return duplicated / len(findings)

    def _identify_top_categories(
        self, findings: list[dict], n: int = 3,
    ) -> list[tuple[str, int]]:
        """Top N finding categories by count."""
        counts = Counter(f.get("category", "unknown") for f in findings)
        return counts.most_common(n)

    def _identify_trigger_gaps(
        self,
        findings: list[dict],
        specialists_used: list[str],
    ) -> list[str]:
        """Find categories with findings where the specialist wasn't triggered.

        A "gap" is a category that produced findings but whose associated
        specialist skill is not in the specialists_used list.
        """
        specialists_set = set(specialists_used)
        # Map each finding's skill to its category
        skill_to_category: dict[str, str] = {}
        categories_with_findings: set[str] = set()
        categories_covered: set[str] = set()

        for finding in findings:
            cat = finding.get("category", "unknown")
            skill = finding.get("skill", "")
            categories_with_findings.add(cat)
            if skill:
                skill_to_category[skill] = cat
                if skill in specialists_set:
                    categories_covered.add(cat)

        return sorted(categories_with_findings - categories_covered)

    def _generate_persona_improvements(
        self,
        findings: list[dict],
        learnings: list[dict],
    ) -> list[str]:
        """Suggest improvements based on patterns."""
        improvements: list[str] = []

        # Check for high-severity concentration
        severity_counts = Counter(f.get("severity", "P3") for f in findings)
        critical = severity_counts.get("Critical", 0)
        if critical >= 3:
            improvements.append(
                f"High critical finding count ({critical}). "
                "Consider adding pre-review security scan."
            )

        # Check for category concentration
        cat_counts = Counter(f.get("category", "unknown") for f in findings)
        for cat, count in cat_counts.most_common(1):
            if count >= len(findings) * 0.5 and len(findings) >= 4:
                improvements.append(
                    f"Category '{cat}' accounts for {count}/{len(findings)} findings. "
                    "Consider a dedicated specialist pass."
                )

        return improvements
