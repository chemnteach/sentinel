import pytest

from sentinel.assess.chunker import Chunker


def _numbered_lines(count: int) -> str:
    return "\n".join(f"line_{i}" for i in range(count))


def test_chunker_raises_value_error_when_overlap_lines_is_negative():
    """
    Acceptance criterion: The chunker rejects or safely normalizes configurations where `overlap_lines` is less than 0 or greater than or equal to `max_lines` before `_split_at_points` begins iterating.

    Scenario: Negative overlap is rejected as invalid configuration.
    Given: A chunker configuration with `max_lines` set to a positive value and `overlap_lines=-1`.
    When: The chunker is invoked through its public chunk-splitting entry point.
    Then: A `ValueError` is raised before any iteration or range emission occurs.
    """
    with pytest.raises(ValueError, match="overlap_lines must be non-negative"):
        Chunker(max_lines=4, overlap_lines=-1).chunk_string(_numbered_lines(10))



def test_chunker_raises_value_error_when_overlap_lines_equals_max_lines():
    """
    Acceptance criterion: The chunker rejects or safely normalizes configurations where `overlap_lines` is less than 0 or greater than or equal to `max_lines` before `_split_at_points` begins iterating.

    Scenario: Boundary overlap equal to max_lines is rejected.
    Given: A chunker configuration with `max_lines=4` and `overlap_lines=4`.
    When: The chunker is invoked through its public chunk-splitting entry point.
    Then: A `ValueError` is raised before any iteration or range emission occurs.
    """
    with pytest.raises(ValueError, match="overlap_lines must be less than max_lines"):
        Chunker(max_lines=4, overlap_lines=4).chunk_string(_numbered_lines(10))



def test_chunker_raises_value_error_when_overlap_lines_exceeds_max_lines():
    """
    Acceptance criterion: The chunker rejects or safely normalizes configurations where `overlap_lines` is less than 0 or greater than or equal to `max_lines` before `_split_at_points` begins iterating.

    Scenario: Overlap greater than max_lines is rejected.
    Given: A chunker configuration with `max_lines=4` and `overlap_lines=5`.
    When: The chunker is invoked through its public chunk-splitting entry point.
    Then: A `ValueError` is raised before any iteration or range emission occurs.
    """
    with pytest.raises(ValueError, match="overlap_lines must be less than max_lines"):
        Chunker(max_lines=4, overlap_lines=5).chunk_string(_numbered_lines(10))



def test_split_at_points_never_emits_empty_or_reversed_ranges_for_valid_overlap():
    """
    Acceptance criterion: `_split_at_points` never yields a range where `end <= start` for any accepted configuration.

    Scenario: All emitted ranges are non-empty under a valid overlapping configuration.
    Given: Input lines long enough to require multiple chunks, valid `max_lines`, valid `overlap_lines`, and preferred split points near chunk boundaries.
    When: The chunker produces ranges for the input.
    Then: Every emitted `(start, end)` pair satisfies `end > start`.
    """
    chunker = Chunker(max_lines=6, overlap_lines=5)
    lines = [f"line_{i}" for i in range(20)]
    split_points = [6, 11, 16]

    ranges = list(chunker._split_at_points(lines, split_points))

    assert ranges
    assert all(end > start for start, end in ranges)



def test_split_at_points_advances_start_strictly_on_every_iteration():
    """
    Acceptance criterion: For every iteration of `_split_at_points`, the next `start` value is strictly greater than the previous `start` value.

    Scenario: Successive chunk starts always move forward for accepted configurations.
    Given: A valid chunker configuration with overlap and enough input to produce several chunks.
    When: The chunker emits all ranges for the input.
    Then: The sequence of emitted `start` values is strictly increasing.
    """
    chunker = Chunker(max_lines=6, overlap_lines=5)
    lines = [f"line_{i}" for i in range(20)]
    split_points = [6, 11, 16]

    ranges = list(chunker._split_at_points(lines, split_points))
    starts = [start for start, _ in ranges]

    assert len(starts) > 1
    assert all(curr > prev for prev, curr in zip(starts, starts[1:]))



def test_split_at_points_ignores_preferred_split_points_that_would_eliminate_forward_progress():
    """
    Acceptance criterion: Preferred split points that would create a chunk whose length is less than or equal to the configured overlap are ignored or otherwise handled so forward progress is preserved.

    Scenario: A nearby preferred split point that would create a chunk of length equal to or less than overlap does not get selected.
    Given: A valid chunker configuration and preferred split points positioned so one candidate end would produce a chunk length less than or equal to `overlap_lines` while a later valid end is available.
    When: The chunker emits ranges.
    Then: The emitted next range starts after the previous start, and the chosen chunk end is not the too-short preferred split point.
    """
    chunker = Chunker(max_lines=6, overlap_lines=5)
    lines = [f"line_{i}" for i in range(20)]
    split_points = [5, 6, 11, 16]

    ranges = list(chunker._split_at_points(lines, split_points))

    assert len(ranges) > 1
    assert ranges[0] == (0, 6)
    assert ranges[1][0] > ranges[0][0]



def test_chunking_with_large_valid_overlap_and_nearby_split_points_terminates_without_duplicate_consecutive_ranges():
    """
    Acceptance criterion: A chunking run with large overlap and nearby preferred split points terminates and does not emit duplicate consecutive ranges.

    Scenario: A stress case with large valid overlap and dense nearby preferred split points completes with bounded unique progress.
    Given: A valid chunker configuration where `overlap_lines` is large but still less than `max_lines`, plus input and preferred split points that previously could repeat the same window.
    When: All emitted ranges are collected for a fixed input.
    Then: Collection completes, the number of emitted ranges is bounded by the input length, emitted starts are strictly increasing, and no two consecutive emitted ranges are identical.
    """
    chunker = Chunker(max_lines=6, overlap_lines=5)
    content = _numbered_lines(20)
    preferred_split_hints = [5, 6, 7, 11, 12, 16, 17]

    chunks = chunker.chunk_string(content, preferred_split_hints=preferred_split_hints)
    ranges = [(chunk.start_line - 1, chunk.end_line) for chunk in chunks]
    starts = [start for start, _ in ranges]

    assert len(ranges) <= 20
    assert all(curr > prev for prev, curr in zip(starts, starts[1:]))
    assert all(curr != prev for prev, curr in zip(ranges, ranges[1:]))



def test_valid_overlap_configuration_preserves_intended_overlap_between_consecutive_chunks():
    """
    Acceptance criterion: The fix should establish explicit invariants for accepted overlap values and for per-iteration progress, while preserving intended overlapping chunk behavior for valid inputs.

    Scenario: Regression case proving valid overlap still produces actual overlap rather than disabling overlap entirely.
    Given: A valid chunker configuration with `0 < overlap_lines < max_lines`, no pathological preferred split point forcing, and enough input to produce at least two chunks.
    When: The chunker emits consecutive ranges.
    Then: At least one pair of consecutive ranges overlaps by the configured number of lines in the observable emitted ranges.
    """
    chunker = Chunker(max_lines=6, overlap_lines=2)
    content = _numbered_lines(20)

    chunks = chunker.chunk_string(content)
    ranges = [(chunk.start_line - 1, chunk.end_line) for chunk in chunks]

    assert len(ranges) >= 2
    overlaps = [prev_end - curr_start for (_, prev_end), (curr_start, _) in zip(ranges, ranges[1:])]
    assert 2 in overlaps
