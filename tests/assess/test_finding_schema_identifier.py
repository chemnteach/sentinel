from sentinel.assess.finding_schema import _extract_identifier_context


def test_extract_identifier_context_rejects_line_location_text_in_brackets():
    """
    Acceptance criterion: Inputs containing location text such as `foo.py (line 42)` or `src/app.py (method)` do not yield `line` or `method` as extracted identifiers.

    Scenario: A bracketed token contains structural line-location text rather than an identifier.
    Given: The input location string is `foo.py (line 42)`.
    When: _extract_identifier_context is called with that location string.
    Then: The extracted identifier is None or empty, and the returned remainder is exactly `foo.py (line 42)`.
    """
    location = "foo.py (line 42)"

    identifier = _extract_identifier_context(location)
    remainder = location if not identifier else None

    assert identifier == ""
    assert remainder == "foo.py (line 42)"


def test_extract_identifier_context_rejects_method_location_text_in_brackets():
    """
    Acceptance criterion: Inputs containing location text such as `foo.py (line 42)` or `src/app.py (method)` do not yield `line` or `method` as extracted identifiers.

    Scenario: A bracketed token contains the structural word 'method' rather than an identifier.
    Given: The input location string is `src/app.py (method)`.
    When: _extract_identifier_context is called with that location string.
    Then: The extracted identifier is None or empty, and the returned remainder is exactly `src/app.py (method)`.
    """
    location = "src/app.py (method)"

    identifier = _extract_identifier_context(location)
    remainder = location if not identifier else None

    assert identifier == ""
    assert remainder == "src/app.py (method)"


def test_extract_identifier_context_extracts_valid_bracketed_function_identifier_with_exact_remainder():
    """
    Acceptance criterion: Inputs containing genuine bracketed identifiers still extract the expected function or class name.

    Scenario: A bracketed token contains a valid function-like identifier that should still be extracted.
    Given: The input location string is `src/app.py (process_event)`.
    When: _extract_identifier_context is called with that location string.
    Then: The extracted identifier is exactly `process_event`, and the returned remainder is exactly `src/app.py`.
    """
    location = "src/app.py (process_event)"

    identifier = _extract_identifier_context(location)
    remainder = location.replace(f" ({identifier})", "") if identifier else location

    assert identifier == "process_event"
    assert remainder == "src/app.py"


def test_extract_identifier_context_extracts_valid_bracketed_class_identifier_with_exact_remainder():
    """
    Acceptance criterion: Inputs containing genuine bracketed identifiers still extract the expected function or class name.

    Scenario: A bracketed token contains a valid class-like identifier that should still be extracted.
    Given: The input location string is `src/app.py (OrderProcessor)`.
    When: _extract_identifier_context is called with that location string.
    Then: The extracted identifier is exactly `OrderProcessor`, and the returned remainder is exactly `src/app.py`.
    """
    location = "src/app.py (OrderProcessor)"

    identifier = _extract_identifier_context(location)
    remainder = location.replace(f" ({identifier})", "") if identifier else location

    assert identifier == "OrderProcessor"
    assert remainder == "src/app.py"


def test_extract_identifier_context_preserves_structural_words_in_remainder_when_no_identifier_found():
    """
    Acceptance criterion: Normalization no longer removes structural location words from the remainder when no valid identifier was present.

    Scenario: A structural bracketed token is rejected and must remain intact in the returned remainder.
    Given: The input location string is `foo.py (line 42)`.
    When: _extract_identifier_context is called with that location string.
    Then: The returned remainder is exactly `foo.py (line 42)`, with `line 42` preserved rather than removed or normalized away.
    """
    location = "foo.py (line 42)"

    identifier = _extract_identifier_context(location)
    remainder = location if not identifier else None

    assert identifier == ""
    assert remainder == "foo.py (line 42)"


def test_extract_identifier_context_returns_exact_original_remainder_for_structural_word_variants():
    """
    Acceptance criterion: When no valid identifier is found in a bracketed token, _extract_identifier_context returns the full original location string as the remainder, unchanged — the parsed output is deterministic for the same input regardless of casing or whitespace in structural words like 'line' or 'method'.

    Scenario: Structural bracketed tokens vary by casing and whitespace, including a mixed-case 'method' example, and each variant must preserve its own original text exactly.
    Given: The input location strings are `foo.py (line 42)`, `foo.py ( LINE 42 )`, `src/app.py (method)`, and `src/app.py ( MeThOd )`.
    When: _extract_identifier_context is called separately with each location string.
    Then: For each input variant, the extracted identifier is None or empty and the returned remainder is exactly the original input string for that variant, unchanged.
    """
    locations = [
        "foo.py (line 42)",
        "foo.py ( LINE 42 )",
        "src/app.py (method)",
        "src/app.py ( MeThOd )",
    ]

    for location in locations:
        identifier = _extract_identifier_context(location)
        remainder = location if not identifier else None

        assert identifier == ""
        assert remainder == location
