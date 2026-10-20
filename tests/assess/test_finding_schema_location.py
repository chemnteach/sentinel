from sentinel.assess.finding_schema import (
    unpack_structured_location,
    generate_finding_id_v2,
)

BASE = {
    "module": "sentinel:assess",
    "skill": "layer2a-quick-review",
    "category": "correctness",
    "finding_kind": "logic_bug",
    "subject_key": "file:pkg/module.py",
    "severity": "P1",
    "description": "test finding",
    "summary": "test",
}

def _struct(file=None, line=None, function=None):
    loc = {}
    if file: loc["file"] = file
    if line: loc["line"] = line
    if function: loc["function"] = function
    return {**BASE, "location": loc}

def _string(loc):
    return {**BASE, "location": loc,
            "file": "pkg/module.py",
            "function": "run_check",
            "line_range": "17"}

def test_file_line_function_retains_line():
    r = unpack_structured_location(
        _struct("pkg/module.py", "17", "run_check"))
    assert r["location"] == "pkg/module.py:17 in run_check"

def test_file_and_line_only():
    r = unpack_structured_location(
        _struct("pkg/module.py", "17"))
    assert r["location"] == "pkg/module.py:17"

def test_file_and_function_only():
    r = unpack_structured_location(
        _struct("pkg/module.py", function="run_check"))
    assert r["location"] == "pkg/module.py in run_check"

def test_different_lines_produce_different_locations():
    r1 = unpack_structured_location(
        _struct("pkg/module.py", "17", "run_check"))
    r2 = unpack_structured_location(
        _struct("pkg/module.py", "18", "run_check"))
    assert r1["location"] != r2["location"]
    id1 = generate_finding_id_v2(r1, "test", "")
    id2 = generate_finding_id_v2(r2, "test", "")
    assert id1 != id2

def test_structured_and_string_same_identity():
    structured = unpack_structured_location(
        _struct("pkg/module.py", "17", "run_check"))
    string_finding = _string(
        "pkg/module.py:17 in run_check")
    id1 = generate_finding_id_v2(structured, "test", "")
    id2 = generate_finding_id_v2(string_finding, "test", "")
    assert id1 == id2

def test_file_only_no_trailing_separator():
    r = unpack_structured_location(
        _struct("pkg/module.py"))
    assert r["location"] == "pkg/module.py"
