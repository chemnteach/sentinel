from sentinel.govern.conditions import ConditionItem, format_constraints_for_architect


def test_format_constraints_for_architect_accepts_conditionitem_entries():
    """
    Acceptance criterion: format_constraints_for_architect completes successfully when conditions contains ConditionItem instances.

    Scenario: All condition entries are model-backed ConditionItem instances.
    Given: A conditions list containing one or more valid ConditionItem objects.
    When: format_constraints_for_architect is called with that list.
    Then: It returns a constraints block successfully without raising AttributeError.
    """
    conditions = [
        ConditionItem(type="REQUIRE_CHANGE", target="change_0", note="Update validation"),
        ConditionItem(type="APPROACH_DIRECTIVE", note="Keep changes minimal"),
    ]

    result = format_constraints_for_architect(conditions)

    assert result.startswith("REMEDIATION CONSTRAINTS")
    assert "- REQUIRED: Include this change in the plan: Update validation" in result
    assert "- DIRECTIVE: Keep changes minimal" in result


def test_format_constraints_for_architect_accepts_dict_entries():
    """
    Acceptance criterion: format_constraints_for_architect also completes successfully when conditions contains plain dict entries.

    Scenario: All condition entries are plain dictionaries.
    Given: A conditions list containing valid dict-backed condition entries.
    When: format_constraints_for_architect is called with that list.
    Then: It returns a constraints block successfully and preserves existing dict-backed behavior.
    """
    conditions = [
        {"type": "EXCLUDE_CHANGE", "target": "change_1", "note": "Do not alter API"},
        {"type": "SCOPE_LIMIT", "target": "sentinel/govern/conditions.py", "note": None},
    ]

    result = format_constraints_for_architect(conditions)

    assert result.startswith("REMEDIATION CONSTRAINTS")
    assert "- EXCLUDED: Do NOT implement this change: Do not alter API" in result
    assert "- SCOPE LIMIT: Edits must be restricted to: sentinel/govern/conditions.py" in result


def test_format_constraints_for_architect_accepts_mixed_condition_entry_types():
    """
    Acceptance criterion: A mixed list containing both dict entries and ConditionItem instances produces a constraints block without raising AttributeError or other runtime type errors.

    Scenario: The conditions list mixes dict-backed and model-backed entries.
    Given: A conditions list containing both plain dicts and ConditionItem instances.
    When: format_constraints_for_architect is called with the mixed list.
    Then: It produces a constraints block without raising AttributeError or other runtime type errors.
    """
    conditions = [
        {"type": "REQUIRE_CRITERION", "note": "Verify output formatting", "target": None},
        ConditionItem(type="ADD_CRITERION", note="Support mixed condition inputs"),
    ]

    result = format_constraints_for_architect(conditions)

    assert "- REQUIRED CRITERION: The QA gate must verify: Verify output formatting" in result
    assert "- ADDITIONAL CRITERION: Add this to the acceptance criteria: Support mixed condition inputs" in result


def test_format_constraints_for_architect_degrades_predictably_when_fields_are_missing():
    """
    Acceptance criterion: Field access inside format_constraints_for_architect does not unconditionally call .get(...) on non-dict condition entries.

    Scenario: A model-backed condition entry lacks one or more optional fields used by the formatter.
    Given: A ConditionItem instance or equivalent model-backed entry with missing optional data.
    When: format_constraints_for_architect is called.
    Then: It does not crash from unconditional .get(...) access and degrades predictably in the output.
    """
    conditions = [ConditionItem(type="SCOPE_LIMIT")]

    result = format_constraints_for_architect(conditions)

    assert result.startswith("REMEDIATION CONSTRAINTS")
    assert "- SCOPE LIMIT: Edits must be restricted to: " in result


def test_review_context_generation_path_handles_model_backed_conditions():
    """
    Acceptance criterion: Review-context generation paths that call format_constraints_for_architect no longer fail for model-backed condition inputs.

    Scenario: A review-context generation path invokes the formatter with model-backed conditions.
    Given: A review-context input structure containing ConditionItem entries that reaches format_constraints_for_architect.
    When: The review-context generation path is executed.
    Then: It completes successfully without failing on model-backed condition inputs.
    """
    review_context = {
        "conditions_structured": [
            ConditionItem(type="REQUIRE_CHANGE", note="Preserve architect context generation")
        ]
    }

    result = format_constraints_for_architect(review_context["conditions_structured"])

    assert "- REQUIRED: Include this change in the plan: Preserve architect context generation" in result
