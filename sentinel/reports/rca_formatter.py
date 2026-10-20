"""
sentinel/reports/rca_formatter.py

Formatter for Root Cause Analysis (RCA) Decision Briefs and unified git patches.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sentinel.defect.models import DefectReport, RCAReport, DefectSpec

log = logging.getLogger(__name__)


class RCAReportFormatter:
    """
    Renders human-readable RCA decision briefs (Markdown) and standalone patch files.
    """

    def __init__(self, repo_path: Path | str = ".") -> None:
        self.repo_path = Path(repo_path)

    def format_markdown(
        self,
        defect: DefectReport,
        rca: RCAReport,
        spec: DefectSpec,
    ) -> str:
        """
        Produce a comprehensive, structured Markdown Decision Brief.
        """
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        parts: list[str] = []
        parts.append(f"# Root Cause Analysis & Remediation Brief: {defect.defect_id}")
        parts.append(f"**Title**: {defect.title}")
        parts.append(f"**Severity**: `{defect.severity}` | **Status**: `{defect.status}` | **Generated**: `{now}`")
        parts.append(f"**Operating Mode**: `Advisory / Diagnostic (Read-Only)`\n")

        parts.append("## 1. Executive Summary & Symptoms")
        parts.append(f"**Description**: {defect.description}\n")
        parts.append(f"**Observed Symptoms**:\n{defect.symptoms}\n")
        if defect.reproduction_steps:
            parts.append(f"**Reproduction Steps**:\n{defect.reproduction_steps}\n")

        parts.append("## 2. 3-Tier Causal Decomposition")
        parts.append(f"### Tier 1 — Proximal Mechanism (Immediate Failure Site)\n{rca.proximal_mechanism}\n")
        parts.append(f"### Tier 2 — Intermediate / Systemic Factor\n{rca.systemic_factor}\n")
        parts.append(f"### Tier 3 — Root Architectural Cause\n{rca.root_cause}\n")

        parts.append("## 3. Blast Radius & Scope Boundaries")
        if rca.blast_radius:
            parts.append("**Affected Modules / Files**:")
            for b in rca.blast_radius:
                parts.append(f"- `{b}`")
            parts.append("")
        else:
            parts.append("Blast radius contained to localized fault site.\n")

        if rca.external_dependency:
            parts.append(f"⚠️ **External Dependency Flag**: Yes ({rca.out_of_scope_reasons or 'External service or DB dependency'})\n")

        parts.append("## 4. Remediation Strategy & Options")
        for opt in rca.remediation_options:
            opt_id = opt.get("id", "OPT")
            title = opt.get("title", "Remediation Option")
            strat = opt.get("strategy", "SURGICAL")
            effort = opt.get("effort", "LOW")
            risk = opt.get("risk", "LOW")
            desc = opt.get("description", "")
            tradeoffs = opt.get("tradeoffs", "")

            is_rec = " ⭐ **(Recommended)**" if opt_id == rca.recommended_option or strat == spec.selected_option else ""
            parts.append(f"### Option `{opt_id}`: {title}{is_rec}")
            parts.append(f"- **Strategy**: `{strat}` | **Effort**: `{effort}` | **Risk**: `{risk}`")
            if desc:
                parts.append(f"- **Description**: {desc}")
            if tradeoffs:
                parts.append(f"- **Trade-offs**: {tradeoffs}")
            parts.append("")

        parts.append("## 5. Proposed Unified Diff / Patch")
        parts.append("Below is the ready-to-apply patch for this defect:\n")
        if spec.proposed_diff:
            parts.append(f"```diff\n{spec.proposed_diff}\n```\n")
        else:
            parts.append("*No diff generated.*\n")

        parts.append("## 6. Reproduction Test Stubs & Verification")
        if spec.reproduction_test_stubs:
            t_file = spec.reproduction_test_stubs.get("test_file", "tests/test_repro.py")
            t_code = spec.reproduction_test_stubs.get("test_code", "")
            framework = spec.reproduction_test_stubs.get("framework", "pytest")
            parts.append(f"**Target Test File**: `{t_file}` ({framework})\n")
            if t_code:
                parts.append(f"```python\n{t_code}\n```\n")

        if spec.acceptance_criteria:
            parts.append("**Acceptance Criteria**:")
            for ac in spec.acceptance_criteria:
                parts.append(f"- [ ] {ac}")
            parts.append("")

        return "\n".join(parts)

    def write_artifacts(
        self,
        defect: DefectReport,
        rca: RCAReport,
        spec: DefectSpec,
        reports_dir: Path | str | None = None,
        patches_dir: Path | str | None = None,
    ) -> tuple[Path, Path]:
        """
        Write the Markdown report and patch file to disk.
        """
        rep_dir = Path(reports_dir) if reports_dir else self.repo_path / "reports"
        pat_dir = Path(patches_dir) if patches_dir else self.repo_path / "patches"

        rep_dir.mkdir(parents=True, exist_ok=True)
        pat_dir.mkdir(parents=True, exist_ok=True)

        report_file = rep_dir / f"RCA_{defect.defect_id}_Report.md"
        patch_file = pat_dir / f"{defect.defect_id}.patch"

        md_content = self.format_markdown(defect, rca, spec)
        report_file.write_text(md_content, encoding="utf-8")

        patch_content = spec.proposed_diff or "# No diff available\n"
        patch_file.write_text(patch_content, encoding="utf-8")

        log.info("Wrote RCA Decision Brief to %s and patch to %s", report_file, patch_file)
        return report_file, patch_file
