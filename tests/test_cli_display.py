from __future__ import annotations

from rich.console import Console

from sentinel.cli_display import display_gate, display_task_table, display_tech_debt_table
from sentinel.orchestrator.main import HumanGateRequired


def test_display_gate_renders_message() -> None:
    console = Console(record=True)
    gate = HumanGateRequired("gate", {"message": "hello"})
    display_gate(console, gate)
    out = console.export_text()
    assert "hello" in out


def test_display_gate_no_unicode_error_on_cp1252_stream() -> None:
    """The gate banner must render without a UnicodeEncodeError when the output
    stream can't encode the warning emoji (legacy Windows cp1252 console)."""
    import io

    raw = io.BytesIO()
    buf = io.TextIOWrapper(raw, encoding="cp1252", errors="strict", newline="")
    console = Console(file=buf, width=80)
    gate = HumanGateRequired("plan_approval", {"message": "Approve the plan to start Phase 2."})

    display_gate(console, gate)  # must not raise UnicodeEncodeError
    buf.flush()

    output = raw.getvalue().decode("cp1252")
    assert "Human Gate: plan_approval" in output
    assert "[!]" in output           # ASCII fallback marker
    assert "\u26a0" not in output     # raw warning emoji never emitted


def test_display_gate_uses_emoji_on_utf8_stream() -> None:
    """On a UTF-8 stream the banner keeps the warning emoji (no regression)."""
    import io

    raw = io.BytesIO()
    buf = io.TextIOWrapper(raw, encoding="utf-8", errors="strict", newline="")
    console = Console(file=buf, width=80)
    gate = HumanGateRequired("plan_approval", {"message": "ok"})

    display_gate(console, gate)
    buf.flush()

    output = raw.getvalue().decode("utf-8")
    assert "\u26a0" in output
    assert "[!]" not in output


def test_display_tables_render_rows() -> None:
    console = Console(record=True)
    display_tech_debt_table(
        console,
        [{"id": "TD-1", "severity": "P1", "module": "cli", "description": "desc", "human_triage": "APPROVE"}],
    )
    display_task_table(
        console,
        [{"task_id": "task-1", "state": "COMPLETE", "description": "done", "assigned_agent": "coder"}],
    )
    out = console.export_text()
    assert "TD-1" in out
    assert "task-1" in out
