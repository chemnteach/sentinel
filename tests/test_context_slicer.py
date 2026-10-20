"""
tests/test_context_slicer.py

Unit tests for Targeted Context Slicer (stack trace parsing, source slicing,
budget preservation, and dependency neighborhood assembly).
"""

from __future__ import annotations

import tempfile
from pathlib import Path
import pytest

from sentinel.defect.context_slicer import ContextSlicer, ParsedFrame, FileSlice
from sentinel.defect.models import DefectReport
from sentinel.orchestrator.state_store import StateStore


@pytest.fixture
def sample_repo():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        src = root / "src"
        src.mkdir(parents=True, exist_ok=True)

        # Create a sample python file
        app_py = src / "app.py"
        app_code = "\n".join([f"# Line {i}" for i in range(1, 40)]) + "\n" + """
def process_data(payload):
    if payload is None:
        raise ValueError("Payload cannot be None")
    return payload["id"] * 2
""" + "\n" + "\n".join([f"# Line {i}" for i in range(45, 100)])
        app_py.write_text(app_code, encoding="utf-8")

        # Create another file
        utils_py = src / "utils.py"
        utils_py.write_text("def helper():\n    return 42\n", encoding="utf-8")

        # Setup state store with dependency metadata
        store = StateStore(root / "state", root)
        store.write("dependencies", [
            {
                "module": "src/app.py",
                "imports": ["src/utils.py"],
                "imported_by": [],
            }
        ])

        yield root, store


def test_parse_python_stack_trace(sample_repo):
    root, store = sample_repo
    slicer = ContextSlicer(root, store)

    trace = """Traceback (most recent call last):
  File "src/app.py", line 42, in process_data
    return payload["id"] * 2
TypeError: 'NoneType' object is not subscriptable"""

    frames = slicer.parse_stack_trace(trace)
    assert len(frames) == 1
    assert frames[0].file_path.replace("\\", "/") == "src/app.py"
    assert frames[0].line_number == 42
    assert frames[0].function_name == "process_data"


def test_parse_dotnet_stack_trace(sample_repo):
    root, store = sample_repo
    slicer = ContextSlicer(root, store)

    trace = """Unhandled exception. System.NullReferenceException: Object reference not set to an instance of an object.
   at MyApp.Services.DataService.Process(Item item) in C:\\repo\\src\\app.py:line 42
   at MyApp.Program.Main(String[] args) in C:\\repo\\src\\utils.py:line 2"""

    frames = slicer.parse_stack_trace(trace)
    assert len(frames) == 2
    assert "app.py" in frames[0].file_path
    assert frames[0].line_number == 42
    assert "utils.py" in frames[1].file_path
    assert frames[1].line_number == 2


def test_slice_defect_context(sample_repo):
    root, store = sample_repo
    slicer = ContextSlicer(root, store)

    defect = DefectReport(
        defect_id="DEF-001",
        title="Null pointer in process_data",
        description="Payload is null on empty requests",
        symptoms="TypeError raised",
        stack_trace='File "src/app.py", line 42, in process_data',
        suspected_files=["src/utils.py"],
    )

    ctx_slice = slicer.build_slice(defect)
    assert len(ctx_slice.parsed_frames) == 1
    assert len(ctx_slice.file_slices) >= 1

    formatted = ctx_slice.formatted_context
    assert "src/app.py" in formatted
    assert "process_data" in formatted
    assert "src/utils.py" in formatted


def test_context_budget_enforcement(sample_repo):
    root, store = sample_repo
    # Small budget to test truncation
    slicer = ContextSlicer(root, store, max_chars=300)

    defect = DefectReport(
        defect_id="DEF-001",
        title="Budget test",
        description="Testing max char truncation",
        symptoms="None",
        stack_trace='File "src/app.py", line 42, in process_data',
    )

    ctx_slice = slicer.build_slice(defect)
    assert len(ctx_slice.formatted_context) <= 450  # includes small formatting overhead
