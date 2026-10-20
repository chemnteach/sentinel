from types import SimpleNamespace

from sentinel.assess.retrieval import RetrievalPackager


class _Chunk:
    def __init__(self, content: str, start_line: int, end_line: int) -> None:
        self.content = content
        self.start_line = start_line
        self.end_line = end_line


def _make_packager(limit: int = 2, max_chars: int = 200) -> RetrievalPackager:
    config = SimpleNamespace(
        chunk_max_lines=50,
        chunk_overlap_lines=5,
        retrieval_max_snippets_per_skill=limit,
        retrieval_max_chars_per_snippet=max_chars,
    )
    return RetrievalPackager(config)


def test_select_snippets_returns_later_positive_score_chunks_within_limit_when_zeros_appear_first():
    """
    Acceptance criterion: When ranked results contain positive-score chunks beyond an initial block of zero-score chunks, `_select_snippets` returns the highest-ranked positive-score chunks up to `retrieval_max_snippets_per_skill`.

    Scenario: Ranked chunks for one skill begin with zero-score entries, followed by positive-score entries later in the ranked list.
    Given: A ranked chunk list for a skill where early zero-score chunks would occupy the old pre-truncation slice and later chunks have positive scores.
    When: `_select_snippets` is called with a per-skill limit smaller than the total number of ranked chunks.
    Then: The returned snippets contain the highest-ranked positive-score chunks up to the limit and exclude the earlier zero-score chunks.
    """
    packager = _make_packager(limit=2)
    chunks = [
        _Chunk("plain text", 1, 1),
        _Chunk("more plain text", 2, 2),
        _Chunk("contains todo", 3, 3),
        _Chunk("todo and fixme", 4, 4),
    ]

    snippets = packager._select_snippets("module.py", chunks, ("todo", "fixme"))

    assert [snippet["start_line"] for snippet in snippets] == [4, 3]
    assert [snippet["content"] for snippet in snippets] == ["todo and fixme", "contains todo"]



def test_select_snippets_does_not_return_zero_score_snippets_ahead_of_available_positive_scores():
    """
    Acceptance criterion: Zero-score snippets are not returned ahead of available positive-score snippets for the same skill.

    Scenario: A skill has both zero-score and positive-score ranked chunks available.
    Given: A ranked chunk list containing zero-score chunks before and/or alongside positive-score chunks for the same skill.
    When: `_select_snippets` selects snippets for that skill.
    Then: Only positive-score snippets are returned while any positive-score chunks are available within the ranked results.
    """
    packager = _make_packager(limit=3)
    chunks = [
        _Chunk("no matches here", 1, 1),
        _Chunk("todo item", 2, 2),
        _Chunk("still no matches", 3, 3),
        _Chunk("fixme noted", 4, 4),
    ]

    snippets = packager._select_snippets("module.py", chunks, ("todo", "fixme"))

    assert [snippet["start_line"] for snippet in snippets] == [2, 4]
    assert all(snippet["content"] in {"todo item", "fixme noted"} for snippet in snippets)



def test_select_snippets_returns_zero_score_fallback_when_no_positive_scores_exist():
    """
    Acceptance criterion: If no positive-score chunks exist for a skill, `_select_snippets` may return zero-score fallback snippets according to the existing retrieval limit.

    Scenario: All ranked chunks for a skill have zero scores.
    Given: A ranked chunk list for a skill containing only zero-score chunks and a configured per-skill retrieval limit.
    When: `_select_snippets` is called.
    Then: The returned snippets are zero-score fallback chunks up to the configured retrieval limit.
    """
    packager = _make_packager(limit=2)
    chunks = [
        _Chunk("plain text", 10, 10),
        _Chunk("still plain", 11, 11),
        _Chunk("also plain", 12, 12),
    ]

    snippets = packager._select_snippets("module.py", chunks, ("todo",))

    assert [snippet["start_line"] for snippet in snippets] == [10, 11]
    assert [snippet["content"] for snippet in snippets] == ["plain text", "still plain"]



def test_select_snippets_does_not_discard_later_positive_scores_due_to_zero_score_pre_truncation():
    """
    Acceptance criterion: Selection logic does not discard eligible later-ranked positive-score chunks solely because earlier zero-score chunks occupied the pre-truncation slice.

    Scenario: Eligible positive-score chunks fall outside the old truncated prefix because earlier zero-score chunks consumed the slice.
    Given: A ranked chunk list where the first N entries are zero-score chunks and later entries include positive-score chunks that should be eligible under the per-skill limit.
    When: `_select_snippets` processes the full ranked list for the skill.
    Then: The returned snippets include the later positive-score chunks instead of being limited to the earlier zero-score prefix.
    """
    packager = _make_packager(limit=2)
    chunks = [
        _Chunk("zero one", 1, 1),
        _Chunk("zero two", 2, 2),
        _Chunk("todo appears", 3, 3),
        _Chunk("fixme appears", 4, 4),
    ]

    snippets = packager._select_snippets("module.py", chunks, ("todo", "fixme"))

    assert [snippet["start_line"] for snippet in snippets] == [3, 4]
    assert all(snippet["start_line"] not in {1, 2} for snippet in snippets)



def test_select_snippets_truncation_boundary_prefers_positive_matches_over_earlier_zero_scores():
    """
    Acceptance criterion: Automated tests cover a truncation-boundary scenario where zero-score chunks appear before later positive-score chunks and verify the returned snippets include the positive-score matches.

    Scenario: The per-skill limit creates a truncation boundary that would previously have excluded later positive-score chunks.
    Given: A ranked chunk list with zero-score chunks before the boundary and positive-score chunks immediately after it for the same skill.
    When: `_select_snippets` is called with that retrieval limit.
    Then: The returned snippets include the positive-score matches from beyond the old boundary.
    """
    packager = _make_packager(limit=1)
    chunks = [
        _Chunk("zero score", 1, 1),
        _Chunk("todo match", 2, 2),
    ]

    snippets = packager._select_snippets("module.py", chunks, ("todo",))

    assert len(snippets) == 1
    assert snippets[0]["start_line"] == 2
    assert snippets[0]["content"] == "todo match"
