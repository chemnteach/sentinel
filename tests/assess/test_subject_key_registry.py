from sentinel.assess.subject_key_registry import get_table_2_peers


def test_get_table_2_peers_returns_only_peers_declared_from_emitter_a_for_matching_finding_kind():
    """
    Acceptance criterion: get_table_2_peers(emitter, finding_kind) returns peers only from registry entries where emitter_a == emitter and finding_kind matches.

    Scenario: Query peers for an emitter that has a forward declaration for the requested finding kind and unrelated entries for other finding kinds.
    Given: A registry fixture containing an entry with emitter_a equal to the queried emitter and matching finding_kind, plus additional entries with non-matching finding_kind.
    When: get_table_2_peers is called with the queried emitter and matching finding_kind.
    Then: The result contains only the peer emitter identifiers declared from emitter_a for that finding_kind.
    """
    peers = get_table_2_peers(
        "layer2d-data-integrity-specialist",
        "missing_transaction_boundary",
    )

    assert peers == (
        "layer2a-quick-review",
        "layer3d-error-handling-specialist",
        "layer3e-concurrency-specialist",
    )


def test_get_table_2_peers_excludes_entries_where_queried_emitter_is_only_emitter_b():
    """
    Acceptance criterion: A registry entry where emitter_b == emitter but emitter_a != emitter is not returned as a peer for the queried emitter.

    Scenario: Query peers for an emitter that appears only on the emitter_b side of a matching registry entry.
    Given: A registry fixture containing a matching finding_kind entry where emitter_b is the queried emitter and emitter_a is a different emitter.
    When: get_table_2_peers is called with the queried emitter and that finding_kind.
    Then: The result does not include the opposite-side emitter from that entry.
    """
    peers = get_table_2_peers(
        "layer2d-data-integrity-specialist",
        "concurrency_safety_gap",
    )

    assert peers == ()


def test_get_table_2_peers_returns_different_results_for_asymmetric_forward_and_reverse_declarations():
    """
    Acceptance criterion: A test fixture with asymmetric forward and reverse declarations produces different results for each emitter when the declarations differ.

    Scenario: Query each emitter in a fixture where forward and reverse declarations are both present but intentionally asymmetric.
    Given: A registry fixture containing one declaration from emitter_x to emitter_y for a finding kind and a separate reverse-side declaration from emitter_y to emitter_z for the same finding kind.
    When: get_table_2_peers is called once for emitter_x and once for emitter_y with the same finding_kind.
    Then: Each call returns a different peer collection reflecting only that emitter's own declarations.
    """
    emitter_x_peers = get_table_2_peers(
        "layer2d-data-integrity-specialist",
        "missing_transaction_boundary",
    )
    emitter_y_peers = get_table_2_peers(
        "layer3d-error-handling-specialist",
        "missing_transaction_boundary",
    )

    assert emitter_x_peers == (
        "layer2a-quick-review",
        "layer3d-error-handling-specialist",
        "layer3e-concurrency-specialist",
    )
    assert emitter_y_peers == ("layer2d-data-integrity-specialist",)
    assert emitter_x_peers != emitter_y_peers


def test_get_table_2_peers_preserves_collection_of_peer_emitter_identifiers_api_shape():
    """
    Acceptance criterion: Existing callers in assess continue to receive a collection of peer emitter identifiers without API shape changes.

    Scenario: Query peers from a valid forward declaration and inspect the observable return shape.
    Given: A registry fixture containing at least one matching forward declaration for the queried emitter.
    When: get_table_2_peers is called with that emitter and finding_kind.
    Then: The return value is a collection containing peer emitter identifiers, preserving the existing API shape expected by callers.
    """
    peers = get_table_2_peers(
        "layer3e-concurrency-specialist",
        "blocking_io_in_async",
    )

    assert isinstance(peers, tuple)
    assert peers == (
        "layer3d-error-handling-specialist",
        "layer3a-performance-specialist",
    )
    assert all(isinstance(peer, str) for peer in peers)
