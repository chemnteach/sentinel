"""Tests for illustrative diff previews (sentinel/reports/diff_preview.py, Part A′)."""
from __future__ import annotations

import json

from sentinel.reports.diff_preview import (
    _input_hash,
    _parse_response,
    _sanitize_source,
    generate_all,
    generate_diff,
    load_cache,
    save_cache,
)

# A minimal valid unified diff that references OrderService.cs.
_VALID_DIFF = (
    "--- a/OrderService.cs\n"
    "+++ b/OrderService.cs\n"
    "@@ -40,2 +40,3 @@\n"
    "-conn.Execute(sql)\n"
    "+using var tx = conn.BeginTransaction()\n"
    "+conn.Execute(sql, tx)"
)


class _FakeClient:
    """Matches ModelClient.call_model(system_prompt, user_message) -> obj with .content."""

    def __init__(self, content=_VALID_DIFF, raise_exc=False):
        self.content = content
        self.raise_exc = raise_exc
        self.calls = 0
        self.last_prompt = ""

    def call_model(self, system_prompt="", user_message="", **kw):
        self.calls += 1
        self.last_prompt = user_message
        if self.raise_exc:
            raise RuntimeError("boom")
        return type("R", (), {"content": self.content})()


def _unit(uid="B-1", source="int Save(){\n    conn.Execute(sql)\n    return 0;\n}", changes=None, files=None):
    return {
        "id": uid,
        "title": "root cause",
        "affected_files": files if files is not None else ["OrderService.cs"],
        "proposed_changes": changes if changes is not None else ["wrap in transaction"],
        "source": source,
    }


# --- cache --------------------------------------------------------------------

def test_load_cache_missing_file(tmp_path):
    assert load_cache(str(tmp_path)) == {}


def test_save_and_reload_cache(tmp_path):
    cache = {"B-1": {"input_hash": "abc", "diff": _VALID_DIFF}}
    assert save_cache(str(tmp_path), cache) is True
    assert load_cache(str(tmp_path)) == cache


# --- input hash ---------------------------------------------------------------

def test_input_hash_stable():
    assert _input_hash("src", ["a", "b"]) == _input_hash("src", ["a", "b"])


def test_input_hash_changes_on_source():
    assert _input_hash("src", ["a"]) != _input_hash("src2", ["a"])


def test_input_hash_changes_on_prose():
    # Part C sharpens the prose; the diff must invalidate when the prose changes.
    assert _input_hash("src", ["a"]) != _input_hash("src", ["a", "b"])


# --- source neutralization (hard requirement) ---------------------------------

def test_sanitize_strips_invisible_and_fences():
    dirty = "﻿code with a ``` fence and ​ zero-width"
    clean = _sanitize_source(dirty)
    assert "﻿" not in clean
    assert "​" not in clean
    assert "```" not in clean  # the triple-backtick run is broken up


def test_generate_diff_neutralizes_source_before_prompt():
    client = _FakeClient(content=_VALID_DIFF)
    unit = _unit(source="﻿bad ``` chars", files=["OrderService.cs"])
    generate_diff(unit, client)
    assert "﻿" not in client.last_prompt
    assert "```" not in client.last_prompt


# --- response parsing ---------------------------------------------------------

def test_parse_response_valid():
    assert _parse_response(_VALID_DIFF, ["OrderService.cs"]) is not None


def test_parse_response_strips_fences():
    out = _parse_response("```diff\n" + _VALID_DIFF + "\n```", ["OrderService.cs"])
    assert out is not None and "@@" in out


def test_parse_response_no_diff_sentinel():
    assert _parse_response("NO_DIFF", ["OrderService.cs"]) is None


def test_parse_response_empty():
    assert _parse_response("   ", ["OrderService.cs"]) is None


def test_parse_response_rejects_prose_only():
    assert _parse_response("Just change the boundary check.", ["OrderService.cs"]) is None


def test_parse_response_rejects_unrelated_file():
    # Guard against hallucinated edits: diff must touch an affected file.
    assert _parse_response(_VALID_DIFF, ["SomethingElse.cs"]) is None


# --- grounding: the removed ("current code") lines must exist in the source ---
# Fixes the misleading case where the illustrative "current code" is a real line
# lifted from a DIFFERENT method (wide source window) that isn't at the finding's
# cited location — so it contradicts the code shown on GitHub.
_SRC_WITH_LINE = "public void Save(){\n    conn.Execute(sql)\n    return;\n}"


def test_parse_response_rejects_ungrounded_removed_line():
    # The '-' line is nowhere in the cited source -> fabricated / wrong-method
    # "current code" -> reject rather than mislead.
    assert _parse_response(
        _VALID_DIFF, ["OrderService.cs"],
        source="totally unrelated source with no matching line",
    ) is None


def test_parse_response_accepts_grounded_removed_line():
    assert _parse_response(
        _VALID_DIFF, ["OrderService.cs"], source=_SRC_WITH_LINE,
    ) is not None


def test_parse_response_grounding_normalizes_whitespace():
    # Same code, different indentation / inner spacing -> still grounded.
    src = "public void Save(){\n        conn.Execute( sql )\n}"
    assert _parse_response(
        _VALID_DIFF, ["OrderService.cs"], source=src,
    ) is not None


def test_parse_response_allows_pure_addition_diff():
    # A diff that only ADDS lines has no "current code" to mislead about.
    add_only = (
        "--- a/OrderService.cs\n"
        "+++ b/OrderService.cs\n"
        "@@ -40,0 +41,1 @@\n"
        "+conn.Close()"
    )
    assert _parse_response(
        add_only, ["OrderService.cs"], source="unrelated",
    ) is not None


def test_parse_response_grounding_ignores_file_header():
    # The '--- a/OrderService.cs' header must not be treated as a removed code
    # line that needs grounding.
    assert _parse_response(
        _VALID_DIFF, ["OrderService.cs"], source=_SRC_WITH_LINE,
    ) is not None


def test_parse_response_no_source_skips_grounding():
    # Backward compatible: with no source provided, grounding is not enforced.
    assert _parse_response(_VALID_DIFF, ["OrderService.cs"]) is not None


def test_generate_diff_rejects_ungrounded_current_code():
    # End-to-end: the model returns a diff whose '-' line isn't in the unit
    # source -> generate_diff declines (no misleading preview).
    unit = _unit(source="public void Other(){ /* nothing removable here */ }")
    assert generate_diff(unit, _FakeClient(content=_VALID_DIFF)) is None


# --- generate_diff ------------------------------------------------------------

def test_generate_diff_client_none():
    assert generate_diff(_unit(), None) is None


def test_generate_diff_client_raises():
    assert generate_diff(_unit(), _FakeClient(raise_exc=True)) is None


# --- generate_all -------------------------------------------------------------

def test_generate_all_uses_cache(tmp_path):
    unit = _unit()
    ihash = _input_hash(unit["source"], unit["proposed_changes"])
    save_cache(str(tmp_path), {"B-1": {"input_hash": ihash, "diff": _VALID_DIFF}})
    client = _FakeClient()
    out = generate_all([unit], str(tmp_path), client)
    assert client.calls == 0  # cached -> no LLM call
    assert out["B-1"] == _VALID_DIFF


def test_generate_all_calls_uncached(tmp_path):
    client = _FakeClient()
    out = generate_all([_unit()], str(tmp_path), client)
    assert client.calls == 1
    assert "B-1" in out


def test_generate_all_stale_cache_regenerates(tmp_path):
    # Cached under an OLD prose hash -> must regenerate when prose changed.
    unit = _unit(changes=["new sharpened prose at File.cs:12"])
    save_cache(str(tmp_path), {"B-1": {"input_hash": "OLDHASH", "diff": "stale"}})
    client = _FakeClient()
    out = generate_all([unit], str(tmp_path), client)
    assert client.calls == 1
    assert out["B-1"] == _VALID_DIFF


def test_generate_all_failure_omits_unit(tmp_path):
    client = _FakeClient(raise_exc=True)
    out = generate_all([_unit()], str(tmp_path), client)
    assert out == {}


def test_generate_all_saves_cache(tmp_path):
    generate_all([_unit()], str(tmp_path), _FakeClient())
    cache_file = tmp_path / "diff_preview_cache.json"
    assert cache_file.exists()
    data = json.loads(cache_file.read_text(encoding="utf-8"))
    assert "B-1" in data["units"]
