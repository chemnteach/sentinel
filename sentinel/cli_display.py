"""Display helpers for Sentinel CLI tables and gate messages."""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from sentinel.orchestrator.main import HumanGateRequired

# Warning glyph shown in the human-gate banner. Falls back to an ASCII marker
# when the output stream's encoding can't represent it (e.g. a legacy Windows
# cp1252 console), which otherwise raises UnicodeEncodeError mid-render.
_GATE_WARN_GLYPH = "\u26a0"  # ⚠
_GATE_WARN_ASCII = "[!]"


def _gate_marker(console: Console) -> str:
    """Return a warning marker safe for *console*'s output encoding."""
    encoding = (getattr(console, "encoding", None) or "utf-8")
    try:
        _GATE_WARN_GLYPH.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return _GATE_WARN_ASCII
    return _GATE_WARN_GLYPH


def display_gate(console: Console, gate: HumanGateRequired) -> None:
    """Display a human gate notice."""
    marker = _gate_marker(console)
    console.print()
    console.print(
        Panel(
            gate.payload.get("message", str(gate.payload)),
            title=f"[bold yellow]{marker}  Human Gate: {gate.gate_type}[/]",
            border_style="yellow",
        )
    )


def display_tech_debt_table(console: Console, items: list[dict]) -> None:
    table = Table(title="Tech Debt Register")
    table.add_column("ID", style="dim")
    table.add_column("Severity")
    table.add_column("Module")
    table.add_column("Description")
    table.add_column("Triage")

    for item in items[:50]:
        severity = item.get("severity", "")
        colour = {"Critical": "red", "P1": "yellow", "P2": "cyan", "P3": "green"}.get(severity, "white")
        triage = item.get("human_triage") or "[dim]pending[/]"
        table.add_row(
            item.get("id", ""),
            f"[{colour}]{severity}[/]",
            item.get("module", "")[:30],
            item.get("description", "")[:60],
            triage,
        )

    console.print(table)


def display_task_table(console: Console, tasks: list[dict]) -> None:
    table = Table(title="Tasks")
    table.add_column("Task ID", style="dim")
    table.add_column("State")
    table.add_column("Description")
    table.add_column("Agent")

    state_colours = {
        "COMPLETE": "green", "PR_CREATED": "cyan", "HUMAN_MERGED": "cyan",
        "IN_PROGRESS": "yellow", "SUBMITTED": "yellow",
        "REVIEW_FAIL": "red", "QA_FAIL": "red", "ESCALATED": "red",
        "BLOCKED": "red", "CANCELLED": "dim",
    }
    for task in tasks:
        state = task.get("state", "")
        colour = state_colours.get(state, "white")
        table.add_row(
            task.get("task_id", ""),
            f"[{colour}]{state}[/]",
            task.get("description", "")[:50],
            task.get("assigned_agent", "") or "",
        )

    console.print(table)
