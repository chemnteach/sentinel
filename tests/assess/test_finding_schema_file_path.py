from sentinel.assess.finding_schema import (
    _extract_file_path,
)

def test_posix_path_extracted():
    assert _extract_file_path(
        "/repo/app/main.py:42"
    ) == "/repo/app/main.py"

def test_backslash_path_extracted():
    assert _extract_file_path(
        r"repo\app\main.py:42"
    ) == r"repo\app\main.py"

def test_drive_prefixed_windows_path_extracted():
    assert _extract_file_path(
        r"C:\repo\app\main.py:42"
    ) == r"C:\repo\app\main.py"

def test_structured_location_backslash_nonempty():
    # _extract_file_path on a Windows-style location
    # string must return a non-empty file component
    result = _extract_file_path(r"repo\app\main.py:42")
    assert result != ""

def test_structured_location_drive_nonempty():
    result = _extract_file_path(r"C:\repo\app\main.py:42")
    assert result != ""

def test_no_extension_returns_empty():
    assert _extract_file_path("note:42") == ""

def test_drive_letter_alone_returns_empty():
    assert _extract_file_path("C: not a path") == ""
