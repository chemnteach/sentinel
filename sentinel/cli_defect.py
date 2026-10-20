"""
sentinel/cli_defect.py

CLI command registration for Defect intake, Root Cause Analysis (RCA), and remediation briefs.
"""

from __future__ import annotations

import json as _json
import logging
import sys
from pathlib import Path
from typing import Callable

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from sentinel.config import load_config
from sentinel.defect.models import DefectReport, DefectSeverity, DefectStatus
from sentinel.defect.defect_manager import (
    DefectManager,
    DefectValidationError,
    DefectTransitionError,
)

log = logging.getLogger(__name__)


def register_defect_commands(
    main: click.Group,
    console: Console,
    *,
    is_safe_id: Callable[[str], bool],
    max_submit_json_bytes: int,
) -> None:
    """Register defect and rca commands on the root CLI group."""

    @main.command("defect")
    @click.option("--submit", "submit_path", default=None, metavar="JSON_FILE",
                  type=click.Path(exists=True), help="Submit a defect from a JSON file")
    @click.option("--list", "list_defects", is_flag=True, help="List all defects")
    @click.option("--show", "show_id", default=None, metavar="DEFECT_ID",
                  help="Show full detail for a defect")
    @click.option("--triage", "triage_id", default=None, metavar="DEFECT_ID",
                  help="Triage a defect (combine with --approve/--defer/--reject)")
    @click.option("--approve", is_flag=True, help="Approve the defect triage")
    @click.option("--defer", is_flag=True, help="Defer the defect")
    @click.option("--reject", is_flag=True, help="Reject the defect")
    @click.option("--reason", default="", help="Reason for the triage decision")
    def defect_cmd(
        submit_path: str | None,
        list_defects: bool,
        show_id: str | None,
        triage_id: str | None,
        approve: bool,
        defer: bool,
        reject: bool,
        reason: str,
    ) -> None:
        """Manage Defect / Bug reports."""
        from sentinel.orchestrator.state_store import StateStore

        cfg = load_config()
        store = StateStore(cfg.state_dir, cfg.repo_path)
        mgr = DefectManager(store)

        if submit_path:
            submit_file = Path(submit_path)
            if submit_file.suffix.lower() != ".json":
                console.print("[red]--submit requires a .json file.[/]")
                sys.exit(1)
            if submit_file.stat().st_size > max_submit_json_bytes:
                console.print(
                    f"[red]Submission file too large.[/] Maximum size is {max_submit_json_bytes} bytes."
                )
                sys.exit(1)
            try:
                data = _json.loads(submit_file.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    console.print("[red]Defect submission must be a JSON object.[/]")
                    sys.exit(1)
                defect = mgr.submit(data)
                console.print(Panel(
                    f"[green]Defect submitted successfully[/]\n\n"
                    f"ID:       {defect.defect_id}\n"
                    f"Title:    {defect.title}\n"
                    f"Severity: {defect.severity}\n"
                    f"Status:   {defect.status}\n\n"
                    f"Next: [dim]sentinel defect --triage {defect.defect_id} --approve[/]",
                    title="Defect Submitted",
                ))
            except DefectValidationError as exc:
                console.print(f"[red]Validation error:[/] {exc}")
                sys.exit(1)
            except (OSError, ValueError) as exc:
                console.print(f"[red]Invalid submission file:[/] {exc}")
                sys.exit(1)

        elif list_defects:
            defects = mgr.list_defects()
            if not defects:
                console.print("[dim]No defects found.[/]")
                return
            table = Table(title=f"Defects & Bug Reports ({len(defects)} total)")
            table.add_column("Defect ID", style="cyan")
            table.add_column("Severity")
            table.add_column("Status")
            table.add_column("Title")
            severity_colours = {
                "CRITICAL": "bold red", "HIGH": "red",
                "MEDIUM": "yellow", "LOW": "dim",
            }
            status_colours = {
                "COMPLETE": "green", "IN_PROGRESS": "yellow",
                "REJECTED": "red", "DEFERRED": "dim",
                "DIAGNOSED_REPORT_READY": "cyan", "SPEC_APPROVED": "cyan",
            }
            for d in defects:
                sc = severity_colours.get(d.severity, "white")
                stc = status_colours.get(d.status, "white")
                table.add_row(
                    d.defect_id,
                    f"[{sc}]{d.severity}[/]",
                    f"[{stc}]{d.status}[/]",
                    d.title,
                )
            console.print(table)

        elif show_id:
            if not is_safe_id(show_id):
                console.print("[bold red]Invalid DEFECT_ID format.[/]")
                sys.exit(1)
            try:
                defect = mgr.get(show_id)
                console.print(Panel(
                    f"[bold]{defect.title}[/]\n\n"
                    f"ID:          {defect.defect_id}\n"
                    f"Severity:    {defect.severity}\n"
                    f"Status:      {defect.status}\n"
                    f"Submitted:   {defect.submitted_at}\n\n"
                    f"[bold]Description:[/]\n{defect.description}\n\n"
                    f"[bold]Symptoms:[/]\n{defect.symptoms}\n\n"
                    f"[bold]Report Path:[/]\n{defect.report_path or 'None'}\n"
                    f"[bold]Patch Path:[/]\n{defect.patch_path or 'None'}",
                    title=f"Defect Detail: {defect.defect_id}",
                ))
            except DefectValidationError as exc:
                console.print(f"[red]{exc}[/]")
                sys.exit(1)

        elif triage_id:
            if not is_safe_id(triage_id):
                console.print("[bold red]Invalid DEFECT_ID format.[/]")
                sys.exit(1)
            decision = "APPROVED" if approve else ("DEFERRED" if defer else ("REJECTED" if reject else None))
            if not decision:
                console.print("[red]Specify --approve, --defer, or --reject with --triage.[/]")
                sys.exit(1)
            try:
                mgr.triage(triage_id, decision=decision, reason=reason)
                console.print(f"[green]Defect {triage_id} triaged:[/] {decision}")
                if decision == "APPROVED":
                    console.print(f"\nNext: [dim]sentinel rca --run {triage_id}[/]")
            except (DefectValidationError, DefectTransitionError) as exc:
                console.print(f"[red]{exc}[/]")
                sys.exit(1)

    @main.command("rca")
    @click.option("--run", "run_id", default=None, metavar="DEFECT_ID",
                  help="Run Layer 8a/8b Root Cause Analysis for a defect")
    @click.option("--show", "show_id", default=None, metavar="DEFECT_ID",
                  help="Show RCA findings and proposed diff")
    def rca_cmd(
        run_id: str | None,
        show_id: str | None,
    ) -> None:
        """Run and view Root Cause Analysis (RCA) and remediation briefs."""
        from sentinel.agents.architect import ArchitectAgent
        from sentinel.api.client import create_model_client
        from sentinel.defect.context_slicer import ContextSlicer
        from sentinel.defect.rca_orchestrator import RCAOrchestrator, HumanRCAGateRequired
        from sentinel.orchestrator.state_store import StateStore
        from sentinel.skills.loader import SkillLoader

        cfg = load_config()
        store = StateStore(cfg.state_dir, cfg.repo_path)
        mgr = DefectManager(store)
        slicer = ContextSlicer(cfg.repo_path, store)

        if run_id:
            if not is_safe_id(run_id):
                console.print("[bold red]Invalid DEFECT_ID format.[/]")
                sys.exit(1)

            client = create_model_client(cfg)
            loader = SkillLoader(cfg.skill_files_dir)
            architect = ArchitectAgent(cfg, client, loader)
            orch = RCAOrchestrator(mgr, architect, slicer, store)

            try:
                orch.run(run_id)
            except HumanRCAGateRequired as gate:
                p = gate.payload
                diff_preview = p.get("proposed_diff", "")
                if len(diff_preview) > 600:
                    diff_preview = diff_preview[:600] + "\n... [diff truncated for terminal display] ..."

                console.print(Panel(
                    f"[bold green]Root Cause Analysis Complete for {p['defect_id']}[/]\n\n"
                    f"[bold cyan]3-Tier Causal Decomposition:[/]\n"
                    f"• [bold]Tier 1 (Proximal):[/] {p.get('proximal_mechanism', '')}\n"
                    f"• [bold]Tier 2 (Systemic):[/] {p.get('systemic_factor', '')}\n"
                    f"• [bold]Tier 3 (Root Cause):[/] {p.get('root_cause', '')}\n\n"
                    f"[bold]Recommended Option:[/] {p.get('recommended_option', '')}\n\n"
                    f"[bold]Proposed Patch Preview:[/]\n```diff\n{diff_preview}\n```\n\n"
                    f"Advisory Decision Brief written to [cyan]reports/RCA_{p['defect_id']}_Report.md[/]\n"
                    f"Patch exported to [cyan]patches/{p['defect_id']}.patch[/]",
                    title=f"RCA Gate: {p['defect_id']}",
                ))
            except Exception as exc:
                console.print(f"[red]RCA Execution Error:[/] {exc}")
                sys.exit(1)

        elif show_id:
            if not is_safe_id(show_id):
                console.print("[bold red]Invalid DEFECT_ID format.[/]")
                sys.exit(1)
            rca_list = store.read("rca_reports")
            rca = next((r for r in rca_list if r.get("defect_id") == show_id), None)
            spec_list = store.read("defect_specs")
            spec = next((s for s in spec_list if s.get("defect_id") == show_id), None)

            if not rca:
                console.print(f"[red]No RCA report found for {show_id}.[/]")
                sys.exit(1)

            console.print(Panel(
                f"[bold cyan]Root Cause Analysis for {show_id}[/]\n\n"
                f"• [bold]Proximal Mechanism:[/] {rca.get('proximal_mechanism', '')}\n"
                f"• [bold]Systemic Factor:[/]    {rca.get('systemic_factor', '')}\n"
                f"• [bold]Root Cause:[/]         {rca.get('root_cause', '')}\n\n"
                f"[bold]Proposed Patch:[/]\n```diff\n{(spec or {}).get('proposed_diff', 'No diff available')}\n```",
                title=f"RCA Detail: {show_id}",
            ))
