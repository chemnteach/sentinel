from __future__ import annotations

import pytest

from sentinel.assess.finding_schema import (
    FindingSchemaValidationError,
    generate_finding_id_v2,
    validate_finding_kind_fields,
)
from sentinel.assess.finding_kinds import iter_finding_kind_definitions



def _build_valid_finding(kind_name: str) -> dict[str, object]:
    return {
        "finding_kind": kind_name,
        "module": "sentinel.assess",
        "category": "validation",
        "skill": "unit-test-emitter",
        "location": "sentinel/assess/finding_schema.py:10 in validate_finding_kind_fields",
        "file": "sentinel/assess/finding_schema.py",
        "function": "validate_finding_kind_fields",
        "line_range": "10",
        "description": "Test finding payload",
        "severity": "P2",
        "status": "open",
    }



def _find_kind(*, subject_required: str) -> str:
    for definition in iter_finding_kind_definitions():
        if definition.subject_required == subject_required:
            return definition.kind_name
    raise AssertionError(f"No finding kind found for subject_required={subject_required!r}")



def test_validate_finding_kind_fields_rejects_non_string_subject_key_values():
    """
    Acceptance criterion: Validation fails when `subject_key` is a list, dict, boolean, integer, or other non-string value.

    Scenario: A finding includes subject_key as several malformed non-string values that were previously coerced to strings.
    Given: A valid finding payload template for a finding kind that accepts a subject_key field when it is a string.
    When: Validation is invoked with subject_key set to a list, dict, boolean, integer, and another non-string value.
    Then: Schema validation fails for each malformed payload instead of accepting the coerced string form.
    """
    kind_name = _find_kind(subject_required="optional")
    malformed_values = [[], {}, True, 7, 3.14]

    for malformed_value in malformed_values:
        payload = _build_valid_finding(kind_name)
        payload["subject_key"] = malformed_value

        with pytest.raises(FindingSchemaValidationError, match="subject_key must be a string"):
            validate_finding_kind_fields(payload, emitter_name="unit-test-emitter")



def test_validate_finding_kind_fields_allows_absent_or_null_subject_key_when_permitted():
    """
    Acceptance criterion: Validation still accepts `subject_key` when it is absent or null if that is allowed for the finding kind.

    Scenario: A finding kind that permits missing subject_key is validated with the field omitted and with the field set to null.
    Given: A valid finding payload for a finding kind whose schema allows subject_key to be absent or null.
    When: Validation is invoked once without subject_key and once with subject_key set to None.
    Then: Both payloads pass validation.
    """
    kind_name = _find_kind(subject_required="optional")

    payload_without_subject_key = _build_valid_finding(kind_name)
    payload_with_null_subject_key = _build_valid_finding(kind_name)
    payload_with_null_subject_key["subject_key"] = None

    assert (
        validate_finding_kind_fields(
            payload_without_subject_key,
            emitter_name="unit-test-emitter",
        ).kind_name
        == kind_name
    )
    assert (
        validate_finding_kind_fields(
            payload_with_null_subject_key,
            emitter_name="unit-test-emitter",
        ).kind_name
        == kind_name
    )



def test_validate_finding_kind_fields_trims_actual_string_subject_key_before_validation():
    """
    Acceptance criterion: Validation applies whitespace trimming and required-field checks only to actual string inputs.

    Scenario: A finding provides subject_key as a real string with surrounding whitespace.
    Given: A valid finding payload for a finding kind where a non-empty string subject_key is valid.
    When: Validation is invoked with subject_key containing leading and trailing whitespace around a valid identifier.
    Then: Validation succeeds using the trimmed string semantics rather than rejecting the value because of surrounding whitespace.
    """
    kind_name = _find_kind(subject_required="required")
    payload = _build_valid_finding(kind_name)
    payload["subject_key"] = "  package.module:Symbol  "

    definition = validate_finding_kind_fields(payload, emitter_name="unit-test-emitter")

    assert definition.kind_name == kind_name



def test_validate_finding_kind_fields_applies_required_checks_to_trimmed_string_only():
    """
    Acceptance criterion: Validation applies whitespace trimming and required-field checks only to actual string inputs.

    Scenario: A required subject_key is provided as a whitespace-only string, which becomes empty after trimming.
    Given: A valid finding payload for a finding kind where subject_key is required.
    When: Validation is invoked with subject_key set to a whitespace-only string.
    Then: Validation fails the required-field check after trimming.
    """
    kind_name = _find_kind(subject_required="required")
    payload = _build_valid_finding(kind_name)
    payload["subject_key"] = "   \t   "

    with pytest.raises(FindingSchemaValidationError, match="subject_key is required"):
        validate_finding_kind_fields(payload, emitter_name="unit-test-emitter")



def test_schema_validation_no_longer_accepts_malformed_non_string_subject_key():
    """
    Acceptance criterion: Tests demonstrate that malformed non-string `subject_key` values no longer pass schema validation.

    Scenario: An end-to-end schema validation call is made with a payload that previously passed because subject_key was coerced to a string.
    Given: A finding payload that is otherwise valid except that subject_key is a malformed non-string value.
    When: The public schema validation entry point is called.
    Then: The payload is rejected by schema validation.
    """
    kind_name = _find_kind(subject_required="optional")
    payload = _build_valid_finding(kind_name)
    payload["subject_key"] = {"malformed": "value"}

    with pytest.raises(FindingSchemaValidationError, match="subject_key must be a string"):
        generate_finding_id_v2(
            payload,
            emitter_name="unit-test-emitter",
            repo_root_canonical="",
        )
