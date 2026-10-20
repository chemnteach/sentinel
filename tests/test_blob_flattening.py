"""
tests/test_blob_flattening.py

TDD tests for Phase 2: Blob Flattening.

Blob findings are Quick Review outputs where the description field contains
a raw JSON/Python dict with severity arrays like {"critical": [...], "p1": [...]}.
These must be detected and flattened into individual structured findings.
"""

from __future__ import annotations

import json

from sentinel.assess.finding_schema import (
    flatten_blob,
    is_blob_finding,
)
from sentinel.assess.reviewer import _extract_findings


# ── Detection: is_blob_finding ───────────────────────────────────────


class TestIsBlobFinding:
    def test_is_blob_finding_with_severity_dict(self):
        """Description containing severity keys should be detected as blob."""
        blob = json.dumps({
            "critical": ["Missing input validation on user data"],
            "p1": ["No error handling in API endpoint", "Hardcoded secret"],
        })
        assert is_blob_finding(blob) is True

    def test_is_blob_finding_with_normal_text(self):
        """Plain text description should NOT be detected as blob."""
        assert is_blob_finding("Missing input validation on user data") is False

    def test_is_blob_finding_with_json_but_no_severity(self):
        """JSON dict without severity keys should NOT be detected."""
        desc = json.dumps({"file": "main.py", "issue": "bad code"})
        assert is_blob_finding(desc) is False

    def test_is_blob_finding_with_python_dict_syntax(self):
        """Python dict syntax (single quotes) should also be detected."""
        blob = "{'critical': ['issue one'], 'p2': ['issue two']}"
        assert is_blob_finding(blob) is True

    def test_is_blob_finding_with_empty_string(self):
        """Empty string should not be detected."""
        assert is_blob_finding("") is False

    def test_is_blob_finding_with_only_p3(self):
        """Dict with only p3 key should be detected."""
        blob = json.dumps({"p3": ["minor style issue"]})
        assert is_blob_finding(blob) is True


# ── Flattening: flatten_blob ─────────────────────────────────────────


class TestFlattenBlob:
    SAMPLE_BLOB = json.dumps({
        "critical": [
            {
                "description": "SQL injection in query builder",
                "finding_kind": "logic_bug",
                "subject_key": "blob:sql-injection",
            },
            {
                "description": "Hardcoded database password",
                "finding_kind": "logic_bug",
                "subject_key": "blob:hardcoded-password",
            },
        ],
        "p1": [
            {
                "description": "No input validation on user endpoint",
                "finding_kind": "logic_bug",
                "subject_key": "blob:input-validation",
            },
            {
                "description": "Missing rate limiting",
                "finding_kind": "logic_bug",
                "subject_key": "blob:rate-limiting",
            },
            {
                "description": "Unencrypted PII storage",
                "finding_kind": "logic_bug",
                "subject_key": "blob:pii-storage",
            },
        ],
    })

    def test_flatten_blob_produces_individual_findings(self):
        """Blob with 2 critical + 3 p1 items = 5 findings."""
        results = flatten_blob(self.SAMPLE_BLOB, "api/routes.py", "quick_review")
        assert len(results) == 5

    def test_flatten_blob_severity_mapping(self):
        """Severity keys map to proper case: critical→Critical, p1→P1."""
        results = flatten_blob(self.SAMPLE_BLOB, "api/routes.py", "quick_review")
        severities = [r["severity"] for r in results]
        assert severities.count("Critical") == 2
        assert severities.count("P1") == 3

    def test_flatten_blob_preserves_description(self):
        """Each flattened finding has the inner item's description text."""
        results = flatten_blob(self.SAMPLE_BLOB, "api/routes.py", "quick_review")
        descriptions = [r["description"] for r in results]
        assert "SQL injection in query builder" in descriptions
        assert "Hardcoded database password" in descriptions
        assert "No input validation on user endpoint" in descriptions

    def test_flatten_blob_uses_schema_v2_fields(self):
        """Flattened findings have id, category, file, function, line_range."""
        results = flatten_blob(self.SAMPLE_BLOB, "api/routes.py", "quick_review")
        for finding in results:
            assert "id" in finding
            assert finding["id"].startswith("F2-")
            assert "legacy_finding_id" in finding
            assert finding["legacy_finding_id"].startswith("F-")
            assert "finding_kind" in finding
            assert "subject_key" in finding
            assert "category" in finding
            assert "file" in finding
            assert "function" in finding
            assert "line_range" in finding
            assert finding["module"] == "api/routes.py"
            assert finding["skill"] == "quick_review"
            assert finding["status"] == "open"

    def test_flatten_blob_preserves_distinct_ids_without_locations(self):
        """Missing locations should not collapse distinct blob entries onto one F2 ID."""
        results = flatten_blob(self.SAMPLE_BLOB, "api/routes.py", "quick_review")
        assert len({finding["id"] for finding in results}) == len(results)

    def test_flatten_blob_with_python_dict_syntax(self):
        """Python dict syntax (single quotes) should also be parsed."""
        blob = "{'critical': [{'description': 'issue one', 'finding_kind': 'logic_bug', 'subject_key': 'blob:issue-one'}], 'p2': [{'description': 'issue two', 'finding_kind': 'logic_bug', 'subject_key': 'blob:issue-two'}]}"
        results = flatten_blob(blob, "mod.py", "quick_review")
        assert len(results) == 2
        severities = {r["severity"] for r in results}
        assert severities == {"Critical", "P2"}

    def test_flatten_blob_isolates_string_items_missing_finding_kind(self):
        """String blob items lack finding_kind, so F2 generation fails for them.

        Per the per-item isolation contract (P1 wave-3), such a malformed item
        is logged and skipped rather than aborting the whole call — so a blob of
        only string items yields no findings instead of raising.
        """
        blob = json.dumps({"p1": ["simple string finding"]})
        assert flatten_blob(blob, "mod.py", "quick_review") == []

    def test_flatten_blob_string_item_skipped_but_valid_item_survives(self):
        """A malformed string item must not drop a sibling valid dict item."""
        blob = json.dumps(
            {
                "p1": [
                    "simple string finding",  # missing finding_kind -> skipped
                    {
                        "description": "real finding",
                        "finding_kind": "logic_bug",
                        "subject_key": "blob:real",
                    },
                ]
            }
        )
        results = flatten_blob(blob, "mod.py", "quick_review")
        assert len(results) == 1
        assert results[0]["description"] == "real finding"

    def test_flatten_blob_skips_unknown_severity_keys(self):
        """Keys not in the severity map are ignored."""
        blob = json.dumps({
            "critical": [
                {
                    "description": "real issue",
                    "finding_kind": "logic_bug",
                    "subject_key": "blob:real-issue",
                },
            ],
            "notes": "some metadata",
            "summary": "overview text",
        })
        results = flatten_blob(blob, "mod.py", "quick_review")
        assert len(results) == 1
        assert results[0]["severity"] == "Critical"

    def test_flatten_blob_with_dict_items(self):
        """Items that are dicts should use their 'description' or str repr."""
        blob = json.dumps({
            "p1": [
                {
                    "description": "detailed finding",
                    "location": "foo.py:10",
                    "finding_kind": "logic_bug",
                    "subject_key": "blob:detailed-finding",
                },
            ],
        })
        results = flatten_blob(blob, "mod.py", "quick_review")
        assert len(results) == 1
        assert results[0]["description"] == "detailed finding"


# ── Integration: _extract_findings with blob ─────────────────────────


class TestExtractFindingsFlattenBlobs:
    def test_extract_findings_flattens_blobs(self):
        """_extract_findings with a blob finding returns flattened results."""
        blob_desc = json.dumps({
            "critical": [
                {
                    "description": "SQL injection vulnerability",
                    "finding_kind": "logic_bug",
                    "subject_key": "blob:sql-injection",
                },
            ],
            "p1": [
                {
                    "description": "Missing auth check",
                    "finding_kind": "logic_bug",
                    "subject_key": "blob:auth-check",
                },
                {
                    "description": "No rate limiting",
                    "finding_kind": "logic_bug",
                    "subject_key": "blob:rate-limiting",
                },
            ],
        })
        result = {
            "findings": [
                {
                    "severity": "P2",  # outer severity is meaningless
                    "description": blob_desc,
                    "location": "api/routes.py",
                },
            ],
        }
        findings = _extract_findings(result, "api_module", "quick_review")
        # Should produce 3 individual findings, not 1 blob
        assert len(findings) == 3
        severities = [f["severity"] for f in findings]
        assert "Critical" in severities
        assert "P1" in severities
        # The outer "P2" should NOT appear
        assert "P2" not in severities

    def test_extract_findings_passes_through_normal(self):
        """Normal findings pass through unchanged."""
        result = {
            "findings": [
                {
                    "severity": "P1",
                    "description": "Missing error handling in main()",
                    "location": "app.py:42",
                    "finding_kind": "error_signal_loss",
                },
            ],
        }
        findings = _extract_findings(result, "app_module", "quick_review")
        assert len(findings) == 1
        assert findings[0]["severity"] == "P1"
        assert findings[0]["description"] == "Missing error handling in main()"

    def test_extract_findings_preserves_summary(self):
        """Producer summary is preserved through reviewer normalization."""
        result = {
            "findings": [
                {
                    "severity": "P1",
                    "finding_kind": "contract_violation",
                    "subject_key": "return-contract:get_wafer_state",
                    "summary": "Unchecked Optional return from get_wafer_state",
                    "description": "get_wafer_state() can return None, but callers treat the result as a valid wafer state.",
                    "location": "wafer_state.py:42 get_wafer_state()",
                    "suggested_fix": "Return an explicit error or require callers to handle None.",
                },
            ],
        }

        findings = _extract_findings(
            result,
            "wafer_state",
            "layer2b-correctness-specialist",
        )

        assert len(findings) == 1
        assert "summary" in findings[0]
        assert findings[0]["summary"] == "Unchecked Optional return from get_wafer_state"
