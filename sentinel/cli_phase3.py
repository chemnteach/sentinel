"""Phase 3 CLI command registration for FER workflows."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

import logging

from sentinel.config import load_config

log = logging.getLogger(__name__)


def register_phase3_commands(
	main: click.Group,
	console: Console,
	*,
	is_safe_id: Callable[[str], bool],
	max_submit_json_bytes: int,
	max_feedback_chars: int,
) -> None:
	"""Register Phase 3 commands on the root CLI group."""

	@main.command("fer")
	@click.option("--submit", "submit_path", default=None, metavar="JSON_FILE",
				  type=click.Path(exists=True), help="Submit a FER from a JSON file")
	@click.option("--list", "list_fers", is_flag=True, help="List all FERs")
	@click.option("--show", "show_id", default=None, metavar="FER_ID",
				  help="Show full detail for a FER")
	@click.option("--triage", "triage_id", default=None, metavar="FER_ID",
				  help="Triage a FER (combine with --approve/--defer/--reject)")
	@click.option("--approve", is_flag=True, help="Approve the FER triage")
	@click.option("--defer", is_flag=True, help="Defer the FER")
	@click.option("--reject", is_flag=True, help="Reject the FER")
	@click.option("--reason", default="", help="Reason for the triage decision")
	def fer_cmd(
		submit_path: str | None,
		list_fers: bool,
		show_id: str | None,
		triage_id: str | None,
		approve: bool,
		defer: bool,
		reject: bool,
		reason: str,
	) -> None:
		"""Manage Feature Enhancement Requests (FERs)."""
		import json as _json
		import sys

		from sentinel.orchestrator.state_store import StateStore
		from sentinel.phase3.fer_manager import FERManager, FERValidationError, FERTransitionError

		cfg = load_config()
		store = StateStore(cfg.state_dir, cfg.repo_path)
		mgr = FERManager(store)

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
					console.print("[red]FER submission must be a JSON object.[/]")
					sys.exit(1)
				fer = mgr.submit(data)
				console.print(Panel(
					f"[green]FER submitted successfully[/]\n\n"
					f"ID:       {fer.fer_id}\n"
					f"Title:    {fer.title}\n"
					f"Priority: {fer.priority}\n"
					f"Status:   {fer.status}\n\n"
					f"Next: [dim]sentinel fer --triage {fer.fer_id} --approve[/]",
					title="FER Submitted",
				))
			except FERValidationError as exc:
				console.print(f"[red]Validation error:[/] {exc}")
				sys.exit(1)
			except (OSError, ValueError) as exc:
				console.print(f"[red]Invalid submission file:[/] {exc}")
				sys.exit(1)

		elif list_fers:
			fers = mgr.list_all()
			if not fers:
				console.print("[dim]No FERs found.[/]")
				return
			table = Table(title=f"Feature Enhancement Requests ({len(fers)} total)")
			table.add_column("FER ID", style="cyan")
			table.add_column("Priority")
			table.add_column("Status")
			table.add_column("Title")
			priority_colours = {
				"MUST_HAVE": "red", "SHOULD_HAVE": "yellow",
				"COULD_HAVE": "cyan", "WONT_HAVE": "dim",
			}
			status_colours = {
				"COMPLETE": "green", "IN_PROGRESS": "yellow",
				"REJECTED": "red", "DEFERRED": "dim",
				"SPEC_APPROVED": "cyan", "FEASIBILITY_APPROVED": "cyan",
			}
			for f in fers:
				pc = priority_colours.get(f.priority, "white")
				sc = status_colours.get(f.status, "white")
				table.add_row(
					f.fer_id,
					f"[{pc}]{f.priority}[/]",
					f"[{sc}]{f.status}[/]",
					f.title[:60],
				)
			console.print(table)

		elif show_id:
			if not is_safe_id(show_id):
				console.print(
					"[bold red]Invalid FER_ID format.[/] "
					"Use 1-64 chars from [A-Za-z0-9._-] and avoid '..'."
				)
				sys.exit(1)
			try:
				fer = mgr.get(show_id)
				console.print(Panel(
					f"[bold]{fer.title}[/]\n\n"
					f"Priority:   {fer.priority}\n"
					f"Status:     {fer.status}\n"
					f"Submitted:  {fer.submitted_at[:10]}\n"
					f"Approach:   {fer.selected_approach or '(not yet selected)'}\n"
					f"Tasks:      {', '.join(fer.task_ids) or '(none yet)'}\n\n"
					f"[bold]Business justification:[/]\n{fer.business_justification}\n\n"
					f"[bold]Affected systems:[/]\n"
					+ "\n".join(f"  • {s}" for s in fer.affected_systems),
					title=f"FER Detail — {show_id}",
				))
				if fer.history:
					console.print("\n[bold]History:[/]")
					for event in fer.history:
						console.print(
							f"  {event.get('timestamp', '')[:19]}  "
							f"[cyan]{event.get('event', '')}[/]"
						)
			except FERValidationError as exc:
				console.print(f"[red]{exc}[/]")
				sys.exit(1)

		elif triage_id:
			if not is_safe_id(triage_id):
				console.print(
					"[bold red]Invalid FER_ID format.[/] "
					"Use 1-64 chars from [A-Za-z0-9._-] and avoid '..'."
				)
				sys.exit(1)
			decision = "APPROVED" if approve else ("DEFERRED" if defer else ("REJECTED" if reject else None))
			if not decision:
				console.print("[red]Specify --approve, --defer, or --reject with --triage.[/]")
				sys.exit(1)
			try:
				mgr.triage(triage_id, decision=decision, reason=reason)
				console.print(f"[green]FER {triage_id} triaged:[/] {decision}")
				if decision == "APPROVED":
					console.print(
						f"\nNext: [dim]sentinel feasibility --run {triage_id}[/]"
					)
			except (FERValidationError, FERTransitionError) as exc:
				console.print(f"[red]{exc}[/]")
				sys.exit(1)

	@main.command("feasibility")
	@click.option("--run", "run_id", default=None, metavar="FER_ID",
				  help="Run Layer 7a feasibility assessment for a FER")
	@click.option("--show", "show_id", default=None, metavar="FER_ID",
				  help="Show the feasibility report for a FER")
	@click.option("--approve", "approve_id", default=None, metavar="FER_ID",
				  help="Approve the feasibility report and select an approach")
	@click.option("--approach", default=None, metavar="APPROACH_NAME",
				  help="Implementation approach name to select (use with --approve)")
	@click.option("--defer", "defer_id", default=None, metavar="FER_ID",
				  help="Defer a FER after reviewing the feasibility report")
	def feasibility_cmd(
		run_id: str | None,
		show_id: str | None,
		approve_id: str | None,
		approach: str | None,
		defer_id: str | None,
	) -> None:
		"""Run and manage Layer 7a feasibility assessments."""
		import sys

		from sentinel.api.client import create_model_client
		from sentinel.agents.architect import ArchitectAgent
		from sentinel.orchestrator.state_store import StateStore
		from sentinel.phase3.fer_manager import FERManager
		from sentinel.phase3.feasibility import FeasibilityOrchestrator, HumanFeasibilityGateRequired

		cfg = load_config()
		store = StateStore(cfg.state_dir, cfg.repo_path)
		fer_mgr = FERManager(store)

		if run_id:
			if not is_safe_id(run_id):
				console.print(
					"[bold red]Invalid FER_ID format.[/] "
					"Use 1-64 chars from [A-Za-z0-9._-] and avoid '..'."
				)
				sys.exit(1)
			from sentinel.skills.loader import SkillLoader
			client = create_model_client(cfg)
			loader = SkillLoader(cfg.skill_files_dir)
			architect = ArchitectAgent(cfg, client, loader)
			orch = FeasibilityOrchestrator(fer_mgr, architect, store)
			try:
				orch.run(run_id)
			except HumanFeasibilityGateRequired as gate:
				console.print(Panel(gate.payload["message"], title="Feasibility Gate"))
			except Exception as exc:
				console.print(f"[red]Feasibility error:[/] {exc}")
				sys.exit(1)

		elif show_id:
			if not is_safe_id(show_id):
				console.print(
					"[bold red]Invalid FER_ID format.[/] "
					"Use 1-64 chars from [A-Za-z0-9._-] and avoid '..'."
				)
				sys.exit(1)
			reports = store.read("feasibility")
			report = next((r for r in reports if r.get("fer_id") == show_id), None)
			if not report:
				console.print(f"[red]No feasibility report found for {show_id}.[/]")
				sys.exit(1)
			console.print(Panel(
				f"Verdict:    [bold]{report.get('feasibility_verdict')}[/]\n"
				f"Risk:       {report.get('risk_level')}\n"
				f"Effort:     {report.get('total_effort_estimate', '?')}\n"
				f"Recommended: {report.get('recommended_approach')}\n\n"
				+ "\n".join(
					f"  [{o.get('effort', '?')}] {o.get('approach_name', '')}: "
					f"{o.get('description', '')[:80]}"
					for o in report.get("options", [])
				),
				title=f"Feasibility Report — {show_id}",
			))

		elif approve_id:
			if not is_safe_id(approve_id):
				console.print(
					"[bold red]Invalid FER_ID format.[/] "
					"Use 1-64 chars from [A-Za-z0-9._-] and avoid '..'."
				)
				sys.exit(1)
			if not approach:
				console.print("[red]--approach is required with --approve.[/]")
				sys.exit(1)
			from sentinel.skills.loader import SkillLoader
			client = create_model_client(cfg)
			loader = SkillLoader(cfg.skill_files_dir)
			architect = ArchitectAgent(cfg, client, loader)
			orch = FeasibilityOrchestrator(fer_mgr, architect, store)
			try:
				orch.approve(approve_id, selected_approach=approach)
				console.print(
					f"[green]Feasibility approved for {approve_id}.[/] "
					f"Selected approach: {approach}\n\n"
					f"Next: [dim]sentinel spec --run {approve_id}[/]"
				)
			except Exception as exc:
				console.print(f"[red]{exc}[/]")
				sys.exit(1)

		elif defer_id:
			if not is_safe_id(defer_id):
				console.print(
					"[bold red]Invalid FER_ID format.[/] "
					"Use 1-64 chars from [A-Za-z0-9._-] and avoid '..'."
				)
				sys.exit(1)
			try:
				fer_mgr.triage(defer_id, decision="DEFERRED", reason="Deferred after feasibility review")
				console.print(f"[yellow]FER {defer_id} deferred.[/]")
			except Exception as exc:
				console.print(f"[red]{exc}[/]")
				sys.exit(1)

	@main.command("spec")
	@click.option("--run", "run_id", default=None, metavar="FER_ID",
				  help="Run Layer 7b spec generation for a FER")
	@click.option("--show", "show_id", default=None, metavar="FER_ID",
				  help="Show the enhancement spec for a FER")
	@click.option("--approve", "approve_id", default=None, metavar="FER_ID",
				  help="Approve the spec and generate implementation tasks")
	@click.option("--revise", "revise_id", default=None, metavar="FER_ID",
				  help="Request spec revision (returns to IN_SPEC)")
	@click.option("--feedback", default="", help="Feedback for revision request")
	def spec_cmd(
		run_id: str | None,
		show_id: str | None,
		approve_id: str | None,
		revise_id: str | None,
		feedback: str,
	) -> None:
		"""Run and manage Layer 7b enhancement specs."""
		import sys

		from sentinel.api.client import create_model_client
		from sentinel.agents.architect import ArchitectAgent
		from sentinel.orchestrator.state_store import StateStore
		from sentinel.orchestrator.task_manager import TaskManager
		from sentinel.phase3.fer_manager import FERManager
		from sentinel.phase3.spec_writer import SpecWriter, HumanSpecGateRequired

		cfg = load_config()
		store = StateStore(cfg.state_dir, cfg.repo_path)
		fer_mgr = FERManager(store)
		task_mgr = TaskManager(store)

		if run_id:
			if not is_safe_id(run_id):
				console.print(
					"[bold red]Invalid FER_ID format.[/] "
					"Use 1-64 chars from [A-Za-z0-9._-] and avoid '..'."
				)
				sys.exit(1)
			from sentinel.skills.loader import SkillLoader
			client = create_model_client(cfg)
			loader = SkillLoader(cfg.skill_files_dir)
			architect = ArchitectAgent(cfg, client, loader)
			writer = SpecWriter(fer_mgr, architect, task_mgr, store)
			try:
				writer.run(run_id)
			except HumanSpecGateRequired as gate:
				console.print(Panel(gate.payload["message"], title="Spec Approval Gate"))
			except Exception as exc:
				console.print(f"[red]Spec error:[/] {exc}")
				sys.exit(1)

		elif show_id:
			if not is_safe_id(show_id):
				console.print(
					"[bold red]Invalid FER_ID format.[/] "
					"Use 1-64 chars from [A-Za-z0-9._-] and avoid '..'."
				)
				sys.exit(1)
			specs = store.read("specs")
			spec = next((s for s in specs if s.get("fer_id") == show_id), None)
			if not spec:
				console.print(f"[red]No spec found for {show_id}.[/]")
				sys.exit(1)
			console.print(Panel(
				f"[bold]{spec.get('feature_summary', '')}[/]\n\n"
				f"Version:  {spec.get('spec_version', '1.0')}\n"
				f"Steps:    {len(spec.get('implementation_order', []))}\n"
				f"Stubs:    {len(spec.get('test_stubs', []))}\n\n"
				f"[bold]Implementation steps:[/]\n"
				+ "\n".join(
					f"  {i+1}. {step}"
					for i, step in enumerate(spec.get("implementation_order", []))
				),
				title=f"Enhancement Spec — {show_id}",
			))

		elif approve_id:
			if not is_safe_id(approve_id):
				console.print(
					"[bold red]Invalid FER_ID format.[/] "
					"Use 1-64 chars from [A-Za-z0-9._-] and avoid '..'."
				)
				sys.exit(1)
			from sentinel.skills.loader import SkillLoader
			client = create_model_client(cfg)
			loader = SkillLoader(cfg.skill_files_dir)
			architect = ArchitectAgent(cfg, client, loader)
			writer = SpecWriter(fer_mgr, architect, task_mgr, store)
			try:
				fer, tasks = writer.approve(approve_id)
				console.print(
					f"[green]Spec approved for {approve_id}.[/] "
					f"{len(tasks)} task(s) generated.\n"
				)
				for t in tasks:
					console.print(f"  [cyan]{t.task_id}[/]  {t.description[:60]}")
				console.print(
					f"\nNext: [dim]sentinel govern --start[/]  "
					f"(tasks are now in the PLANNED queue)"
				)
			except Exception as exc:
				console.print(f"[red]{exc}[/]")
				sys.exit(1)

		elif revise_id:
			if not is_safe_id(revise_id):
				console.print(
					"[bold red]Invalid FER_ID format.[/] "
					"Use 1-64 chars from [A-Za-z0-9._-] and avoid '..'."
				)
				sys.exit(1)
			if len(feedback) > max_feedback_chars:
				console.print(
					f"[red]--feedback too long.[/] Maximum length is {max_feedback_chars} characters."
				)
				sys.exit(1)
			try:
				fer_mgr.revise(revise_id, feedback)
			except Exception as exc:
				log.error("Spec revise failed for FER %s: %s", revise_id, exc)
				console.print(f"[red]Spec revise failed for {revise_id}:[/] {exc}")
				sys.exit(1)
			console.print(
				f"[yellow]Spec cleared for {revise_id}.[/] "
				f"Feedback recorded. Re-run: [dim]sentinel spec --run {revise_id}[/]"
			)

