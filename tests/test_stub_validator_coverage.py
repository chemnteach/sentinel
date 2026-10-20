"""Coverage matcher for the TDD stub validator.

`_validate_acceptance_criteria_coverage` originally required each task
acceptance criterion to appear as a verbatim case-insensitive substring of
some stub's ``acceptance_criterion`` field. That is brittle: the Coder
paraphrases long/backticked criteria and the substring test fails, exhausting
its TODO attempts. A token-overlap fallback now also accepts a stub when
≥0.6 of the criterion's significant tokens appear in the stub field, while
preserving the original substring rule for full backward compatibility.
"""
from __future__ import annotations

from sentinel.orchestrator.todo_schema import (
    TestStub as _TestStub,
    _validate_acceptance_criteria_coverage,
)


def _stub(acceptance_criterion: str, name: str = "test_behaviour") -> _TestStub:
    return _TestStub(
        name=name,
        acceptance_criterion=acceptance_criterion,
        scenario="scenario",
        given="given",
        when="when",
        then="then",
        stub_code="def test_behaviour():\n    raise NotImplementedError()",
    )


# 1. verbatim substring still passes (backward compat)
def test_verbatim_substring_still_covers():
    criterion = "The function returns True for an excluded directory name"
    stub = _stub("Acceptance criterion: " + criterion + " in all cases.")
    assert _validate_acceptance_criteria_coverage([stub], [criterion]) == []


# 2. paraphrase with >=0.6 token overlap passes (not a substring)
def test_paraphrase_with_token_overlap_covers():
    criterion = "the routing corpus includes summary description finding_kind category fields"
    # reordered + extra words, not a substring; tokens overlap well above 0.6
    stub = _stub(
        "corpus draws routing signal from category, finding_kind, summary and "
        "description fields plus others"
    )
    assert _validate_acceptance_criteria_coverage([stub], [criterion]) == []


# 3. completely unrelated stub fails
def test_unrelated_stub_is_uncovered():
    criterion = "the system validates user input against the schema before saving"
    stub = _stub("performance regression observed in the hot rendering loop path")
    errors = _validate_acceptance_criteria_coverage([stub], [criterion])
    assert len(errors) == 1
    assert "not covered by any test stub" in errors[0]


# 4. criterion whose tokens are all filtered out -> no ZeroDivisionError
def test_all_filtered_tokens_does_not_divide_by_zero():
    criterion = "it is on"  # every word is len<=2 -> crit_tokens empty
    stub = _stub("an entirely different sentence about saving records")
    # must not raise; criterion is uncovered (no substring, overlap skipped)
    errors = _validate_acceptance_criteria_coverage([stub], [criterion])
    assert len(errors) == 1


# 5. empty acceptance_criteria returns no errors
def test_empty_criteria_returns_no_errors():
    stub = _stub("anything at all")
    assert _validate_acceptance_criteria_coverage([stub], []) == []


# 6. real batch-style failure: long backticked criterion the Coder paraphrases
def test_backticked_criterion_paraphrase_covers():
    criterion = (
        "`_determine_specialists` constructs its routing corpus from at least "
        "`summary`, `description`, `finding_kind`, `category`, and `subject_key` "
        "when those fields are present."
    )
    # paraphrase without backticks, reordered — this is exactly the shape that
    # broke -014 attempt 3 under the substring-only rule.
    stub = _stub(
        "builds the routing corpus from summary, description, finding_kind, "
        "category and subject_key whenever those fields are present"
    )
    assert _validate_acceptance_criteria_coverage([stub], [criterion]) == []


# 6b. paraphrase that is too thin (<0.6 overlap) still fails — guards the threshold
def test_low_overlap_paraphrase_still_fails():
    criterion = "the routing corpus includes summary description finding_kind category subject_key"
    stub = _stub("the corpus includes summary only")  # ~2 of 5 significant tokens
    errors = _validate_acceptance_criteria_coverage([stub], [criterion])
    assert len(errors) == 1
