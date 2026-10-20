import pytest

from sentinel.assess.reviewer import _split_by_chars


def test_split_by_chars_raises_value_error_when_max_parts_is_zero():
    """
    Acceptance criterion: _split_by_chars raises ValueError when max_parts is 0

    Scenario: Calling `_split_by_chars` with non-empty input and `max_parts=0`
    Given: A non-empty string input, a positive `max_chars`, and `max_parts` set to 0
    When: `_split_by_chars` is invoked
    Then: A `ValueError` is raised instead of returning an empty list
    """
    with pytest.raises(ValueError, match="max_parts must be positive"):
        _split_by_chars("abcdef", max_chars=3, overlap_chars=1, max_parts=0)


def test_split_by_chars_raises_value_error_when_max_parts_is_negative():
    """
    Acceptance criterion: _split_by_chars raises ValueError when max_parts is negative

    Scenario: Calling `_split_by_chars` with non-empty input and a negative `max_parts`
    Given: A non-empty string input, a positive `max_chars`, and `max_parts` set to a negative integer
    When: `_split_by_chars` is invoked
    Then: A `ValueError` is raised instead of returning an empty list
    """
    with pytest.raises(ValueError, match="max_parts must be positive"):
        _split_by_chars("abcdef", max_chars=3, overlap_chars=1, max_parts=-2)


def test_split_by_chars_non_empty_input_with_non_positive_max_parts_never_returns_empty_list():
    """
    Acceptance criterion: non-empty input with max_parts <= 0 never returns empty list

    Scenario: Invalid non-positive `max_parts` values are rejected explicitly for non-empty input
    Given: A non-empty string input and `max_parts` values of 0 and a negative integer
    When: `_split_by_chars` is invoked for each invalid value
    Then: The function raises `ValueError`, ensuring it does not silently return an empty list
    """
    for invalid_max_parts in (0, -1):
        with pytest.raises(ValueError, match="max_parts must be positive"):
            _split_by_chars("abcdef", max_chars=3, overlap_chars=1, max_parts=invalid_max_parts)


def test_split_by_chars_valid_input_produces_chunks_bounded_by_max_chars():
    """
    Acceptance criterion: valid inputs still produce chunks bounded by max_chars

    Scenario: Splitting valid non-empty input with a positive `max_parts` and a small `max_chars` limit
    Given: An input string long enough to require multiple chunks, a positive `max_chars`, and a positive `max_parts`
    When: `_split_by_chars` is invoked
    Then: All returned chunks are non-empty and each chunk length is less than or equal to `max_chars`
    """
    parts = _split_by_chars(
        "abcdefghijklmnopqrstuvwxyz",
        max_chars=5,
        overlap_chars=1,
        max_parts=10,
    )

    assert parts
    assert all(parts)
    assert all(len(part) <= 5 for part in parts)


def test_split_by_chars_overlap_near_boundary_terminates_without_infinite_loop():
    """
    Acceptance criterion: overlap near max_chars boundary does not produce infinite loop

    Scenario: Input content near the `max_chars` boundary exercises overlap/advance logic
    Given: An input string and parameters that force repeated boundary-adjacent chunking with positive `max_parts`
    When: `_split_by_chars` is invoked
    Then: The function returns a finite result promptly and all chunks remain bounded by `max_chars`
    """
    parts = _split_by_chars(
        "abcdefghij" * 5,
        max_chars=10,
        overlap_chars=9,
        max_parts=50,
    )

    assert parts
    assert len(parts) <= 50
    assert all(len(part) <= 10 for part in parts)


def test_split_by_chars_appends_remainder_when_part_cap_reached():
    """When max_parts is exhausted before the whole text is windowed, the
    uncovered suffix must be appended as a final part rather than dropped."""
    text = "abcdefghijklmnopqrstuvwxyz0123456789"
    parts = _split_by_chars(text, max_chars=5, overlap_chars=0, max_parts=2)

    # Two windows of 5 chars each cover only the first 10 chars; the remainder
    # must be appended so no content is lost.
    assert len(parts) == 3
    assert "".join(parts) == text
    # The trailing part carries the whole uncovered suffix (exceeds max_chars).
    assert parts[-1] == text[10:]


def test_split_by_chars_no_remainder_when_fully_covered_within_cap():
    """When the text fits within the part cap, no extra remainder part is added
    and the final window is not duplicated."""
    text = "abcdefghij"
    parts = _split_by_chars(text, max_chars=5, overlap_chars=0, max_parts=10)

    assert parts == ["abcde", "fghij"]
