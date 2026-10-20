from types import SimpleNamespace

from sentinel.assess.dependency import DependencyAnalyzer


class _DummyStore:
    pass


class _DummyArchitect:
    pass


def _make_analyzer() -> DependencyAnalyzer:
    return DependencyAnalyzer(
        architect=_DummyArchitect(),
        store=_DummyStore(),
        config=SimpleNamespace(),
    )


def test_merge_dependency_edges_duplicate_group_uses_order_independent_is_external_reconciliation():
    """
    Acceptance criterion: Merging the same set of dependency edges in different input orders produces the same output edge set and metadata values.

    Scenario: The same duplicate edge group with mixed is_external values is merged in two different input orders.
    Given: A set of dependency edges containing more than two duplicates with the same source, target, and kind, where is_external differs across duplicates.
    When: The merge routine is called on the edges in one order and then in a different order.
    Then: Both merged results contain the same single reconciled edge with identical metadata, proving order independence for the duplicate group.
    """
    analyzer = _make_analyzer()
    edges_order_one = [
        {"source": "a.py", "target": "requests", "kind": "python_import", "is_external": False},
        {"source": "a.py", "target": "requests", "kind": "python_import", "is_external": True},
        {"source": "a.py", "target": "requests", "kind": "python_import", "is_external": False},
    ]
    edges_order_two = list(reversed(edges_order_one))

    merged_one = analyzer._merge_dependency_edges([], edges_order_one)
    merged_two = analyzer._merge_dependency_edges([], edges_order_two)

    assert merged_one == [
        {"source": "a.py", "target": "requests", "kind": "python_import", "is_external": True}
    ]
    assert merged_two == merged_one



def test_merge_dependency_edges_duplicate_group_prefers_external_when_any_duplicate_is_external():
    """
    Acceptance criterion: When two edges have the same source, target, and kind but different is_external values, the merged result applies a defined reconciliation rule rather than preserving whichever edge appeared first.

    Scenario: A duplicate edge group contains conflicting is_external values and the merge must apply an explicit boolean rule.
    Given: Two or more edges with identical source, target, and kind, where at least one duplicate has is_external=True and another has is_external=False.
    When: The merge routine combines the duplicate group.
    Then: The merged edge has is_external=True, demonstrating the explicit reconciliation rule is not first-write-wins.
    """
    analyzer = _make_analyzer()

    merged = analyzer._merge_dependency_edges(
        [],
        [
            {"source": "a.py", "target": "requests", "kind": "python_import", "is_external": False},
            {"source": "a.py", "target": "requests", "kind": "python_import", "is_external": True},
        ],
    )

    assert merged == [
        {"source": "a.py", "target": "requests", "kind": "python_import", "is_external": True}
    ]



def test_merge_dependency_edges_duplicate_group_with_identical_metadata_preserves_is_external_value():
    """
    Acceptance criterion: The implementation of assess/dependency.py:_merge_dependency_edges contains an explicit, readable merge path for correctness-relevant metadata fields on duplicate edges.

    Scenario: A duplicate edge group agrees on is_external, so reconciliation should preserve the agreed semantic value rather than alter it.
    Given: Multiple duplicate edges with the same source, target, and kind, all with is_external=False.
    When: The merge routine combines the duplicate group.
    Then: The merged edge keeps is_external=False, showing duplicate metadata is merged semantically rather than arbitrarily overwritten.
    """
    analyzer = _make_analyzer()

    merged = analyzer._merge_dependency_edges(
        [],
        [
            {"source": "a.py", "target": "pkg", "kind": "python_import", "is_external": False},
            {"source": "a.py", "target": "pkg", "kind": "python_import", "is_external": False},
            {"source": "a.py", "target": "pkg", "kind": "python_import", "is_external": False},
        ],
    )

    assert merged == [
        {"source": "a.py", "target": "pkg", "kind": "python_import", "is_external": False}
    ]



def test_merge_dependency_edges_regression_fails_under_first_write_wins_for_conflicting_duplicates():
    """
    Acceptance criterion: Regression tests fail if duplicate edges with conflicting metadata are merged using first-write-wins behavior.

    Scenario: The first duplicate says internal and a later duplicate says external, which would be misclassified by first-write-wins.
    Given: A duplicate edge group ordered so the first edge has is_external=False and a later duplicate has is_external=True.
    When: The merge routine combines the duplicate group.
    Then: The merged edge has is_external=True, so a first-write-wins implementation would fail this test.
    """
    analyzer = _make_analyzer()

    merged = analyzer._merge_dependency_edges(
        [],
        [
            {"source": "service.py", "target": "vendor", "kind": "python_import", "is_external": False},
            {"source": "service.py", "target": "vendor", "kind": "python_import", "is_external": True},
        ],
    )

    assert merged[0]["is_external"] is True



def test_merge_dependency_edges_non_duplicate_edges_remain_unchanged():
    """
    Acceptance criterion: Existing dependency analysis behavior for non-duplicate edges remains unchanged.

    Scenario: The merge routine receives only non-duplicate edges.
    Given: A list of dependency edges where each edge has a unique source, target, and kind combination.
    When: The merge routine is called.
    Then: The merged result contains the same edge set and metadata values as the input, preserving existing non-duplicate behavior.
    """
    analyzer = _make_analyzer()
    edges = [
        {"source": "a.py", "target": "pkg_a", "kind": "python_import", "is_external": False},
        {"source": "b.py", "target": "pkg_b", "kind": "python_import", "is_external": True},
        {"source": "proj.csproj", "target": "Other.csproj", "kind": "project_reference", "is_external": False},
    ]

    merged = analyzer._merge_dependency_edges([], edges)

    assert merged == edges


def test_merge_dependency_edges_drops_edges_with_one_empty_endpoint():
    """An edge with only source OR only target populated is malformed and must
    be dropped (previously it was retained unless BOTH were empty)."""
    analyzer = _make_analyzer()
    edges = [
        {"source": "a.py", "target": "", "kind": "python_import"},
        {"source": "", "target": "requests", "kind": "python_import"},
        {"source": "", "target": "", "kind": "python_import"},
        {"source": "b.py", "target": "pkg_b", "kind": "python_import"},
    ]

    merged = analyzer._merge_dependency_edges([], edges)

    assert merged == [
        {"source": "b.py", "target": "pkg_b", "kind": "python_import"},
    ]


def test_collect_import_snippets_truncates_by_characters_not_files(tmp_path):
    """The snippet cap must bound total characters, not the number of files.
    Many small files must not produce an unbounded joined string."""
    repo = tmp_path / "repo"
    repo.mkdir()
    # Create many files, each with a small but non-trivial import line.
    for i in range(200):
        (repo / f"mod_{i}.py").write_text(
            "import some_package_with_a_reasonably_long_name\n",
            encoding="utf-8",
        )

    analyzer = DependencyAnalyzer(
        architect=_DummyArchitect(),
        store=_DummyStore(),
        config=SimpleNamespace(strict_quality_mode=False, dep_scan_limit=1000),
    )
    result = analyzer._collect_import_snippets(
        {"repositories": [{"path": str(repo)}]}
    )

    assert len(result) <= 4000
