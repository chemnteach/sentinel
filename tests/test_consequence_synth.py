"""Tests for LLM consequence personalization (sentinel/reports/consequence_synth.py)."""
from __future__ import annotations

import json

from sentinel.reports.consequence_synth import (
    _parse_response,
    load_cache,
    personalize_consequence,
    save_cache,
    synthesize_all,
)

_TEMPLATE = {"factory": {"best_case": "tb", "likely": "tl", "worst_case": "tw"}}
_VALID_JSON = '{"best_case": "b", "likely": "l", "worst_case": "w"}'


class _FakeClient:
    """Matches ModelClient.call_model(system_prompt, user_message) -> obj with .content."""

    def __init__(self, content=_VALID_JSON, raise_exc=False):
        self.content = content
        self.raise_exc = raise_exc
        self.calls = 0

    def call_model(self, system_prompt="", user_message="", **kw):
        self.calls += 1
        if self.raise_exc:
            raise RuntimeError("boom")
        return type("R", (), {"content": self.content})()


# --- cache --------------------------------------------------------------------

def test_load_cache_missing_file(tmp_path):
    assert load_cache(str(tmp_path), "factory") == {}


def test_load_cache_domain_mismatch(tmp_path):
    save_cache(str(tmp_path), "factory", {"F1": {"best_case": "b", "likely": "l", "worst_case": "w"}})
    assert load_cache(str(tmp_path), "general") == {}


def test_load_cache_domain_match(tmp_path):
    entry = {"best_case": "b", "likely": "l", "worst_case": "w"}
    save_cache(str(tmp_path), "factory", {"F1": entry})
    assert load_cache(str(tmp_path), "factory") == {"F1": entry}


def test_save_and_reload_cache(tmp_path):
    cache = {"F1": {"best_case": "x", "likely": "y", "worst_case": "z"}}
    save_cache(str(tmp_path), "factory", cache)
    assert load_cache(str(tmp_path), "factory") == cache


# --- response parsing ---------------------------------------------------------

def test_parse_response_valid():
    out = _parse_response(_VALID_JSON)
    assert out == {"best_case": "b", "likely": "l", "worst_case": "w"}


def test_parse_response_strips_fences():
    out = _parse_response("```json\n" + _VALID_JSON + "\n```")
    assert out is not None and out["worst_case"] == "w"


def test_parse_response_missing_key():
    assert _parse_response('{"best_case": "b", "likely": "l"}') is None


def test_parse_response_empty_value():
    assert _parse_response('{"best_case": "", "likely": "l", "worst_case": "w"}') is None


# --- personalize --------------------------------------------------------------

def test_personalize_consequence_client_none():
    assert personalize_consequence({"id": "F1"}, _TEMPLATE, "factory", None, None) is None


def test_personalize_consequence_client_raises():
    client = _FakeClient(raise_exc=True)
    assert personalize_consequence({"id": "F1"}, _TEMPLATE, "factory", "code", client) is None


# --- synthesize_all -----------------------------------------------------------

def test_synthesize_all_uses_cache(tmp_path):
    save_cache(str(tmp_path), "factory", {"F1": {"best_case": "b", "likely": "l", "worst_case": "w"}})
    client = _FakeClient()
    findings = [{"id": "F1", "finding_kind": "logic_bug"}]
    synthesize_all(findings, {"logic_bug": _TEMPLATE}, "factory", {}, str(tmp_path), client)
    assert client.calls == 0  # cached -> no LLM call


def test_synthesize_all_calls_uncached(tmp_path):
    client = _FakeClient()
    findings = [{"id": "F1", "finding_kind": "logic_bug"}]
    out = synthesize_all(findings, {"logic_bug": _TEMPLATE}, "factory", {"F1": "code"}, str(tmp_path), client)
    assert client.calls == 1
    assert out["F1"] == {"best_case": "b", "likely": "l", "worst_case": "w"}


def test_synthesize_all_saves_cache(tmp_path):
    client = _FakeClient()
    findings = [{"id": "F1", "finding_kind": "logic_bug"}]
    synthesize_all(findings, {"logic_bug": _TEMPLATE}, "factory", {}, str(tmp_path), client)
    cache_file = tmp_path / "consequence_cache.json"
    assert cache_file.exists()
    data = json.loads(cache_file.read_text(encoding="utf-8"))
    assert data["domain"] == "factory" and "F1" in data["findings"]
