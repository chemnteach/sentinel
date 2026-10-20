from __future__ import annotations

import json

from sentinel.assess.finding_schema import (
    _parse_blob_dict,
    is_blob_finding,
)


def test_is_blob_finding_matches_uppercase_severity_keys():
    """is_blob_finding must recognise severity keys case-insensitively, matching
    flatten_blob's key.lower() handling. A blob like {"P1": [...]} that
    flatten_blob would recognise as a severity bucket must also be detected here
    rather than being classified as a non-blob finding."""
    blob = json.dumps({"P1": [{"description": "boom"}]})

    assert is_blob_finding(blob) is True
    # And _parse_blob_dict agrees the uppercase key is a recognised severity
    # bucket (flatten_blob maps it via key.lower()).
    parsed = _parse_blob_dict(blob)
    assert parsed is not None
    assert {k.lower() for k in parsed} & {"critical", "p1", "p2", "p3"}


def test_is_blob_finding_still_matches_lowercase_keys():
    """Existing lowercase behavior is preserved."""
    blob = json.dumps({"critical": [{"description": "boom"}]})
    assert is_blob_finding(blob) is True


def test_is_blob_finding_rejects_non_severity_dict():
    blob = json.dumps({"note": "not a severity blob"})
    assert is_blob_finding(blob) is False


def test_parse_blob_dict_skips_literal_eval_for_oversized_text():
    """ast.literal_eval must not run on very large untrusted text (CWE-502).
    A single-quoted (non-JSON) dict over the size guard returns None instead of
    being parsed via literal_eval."""
    # Single-quoted so JSON parsing fails and only literal_eval could succeed.
    oversized = "{'a': '" + ("x" * 60_000) + "'}"
    assert len(oversized) > 50_000

    assert _parse_blob_dict(oversized) is None


def test_parse_blob_dict_still_uses_literal_eval_under_size_guard():
    """Small single-quoted dicts are still parsed via literal_eval."""
    small = "{'critical': [{'description': 'boom'}]}"
    parsed = _parse_blob_dict(small)
    assert parsed == {"critical": [{"description": "boom"}]}


def test_parse_blob_dict_parses_large_valid_json_over_guard():
    """The size guard only gates literal_eval; a large but valid JSON blob is
    still parsed by json.loads before the guard is reached."""
    payload = {"critical": [{"description": "x" * 60_000}]}
    text = json.dumps(payload)
    assert len(text) > 50_000

    assert _parse_blob_dict(text) == payload
