from sentinel.assess.dedup import (
    _collapse_intra_specialist_overlaps,
    _location_fields,
    deduplicate,
)


def test_consolidation_uses_highest_severity_source_for_mixed_severity_group():
    """
    Acceptance criterion: When a deduplication group contains findings with different severities, the consolidated result selects its primary record from the highest-severity subset or applies equivalent merge logic that keeps severity-coupled fields aligned with the final severity.

    Scenario: A lower-severity finding would otherwise be selected by existing primary heuristics, but a higher-severity sibling is present.
    Given: A deduplication group containing at least one lower-severity finding with a longer description and one higher-severity finding with distinct severity-coupled metadata.
    When: The group is consolidated.
    Then: The consolidated finding reports the higher severity and keeps severity-coupled fields aligned to the same higher-severity source.
    """
    findings = [
        {
            "finding_kind": "injection_risk",
            "category": "security",
            "file": "app.py",
            "function": "run_query",
            "summary": "Potential SQL injection",
            "description": "lower severity description that is intentionally much longer than the other finding",
            "severity": "P2",
            "remediation": "sanitize later",
            "skill": "layer1",
        },
        {
            "finding_kind": "injection_risk",
            "category": "security",
            "file": "app.py",
            "function": "run_query",
            "summary": "Potential SQL injection",
            "description": "critical description",
            "severity": "Critical",
            "remediation": "parameterize now",
            "skill": "layer2",
        },
    ]

    consolidated = deduplicate(findings)

    assert len(consolidated) == 1
    result = consolidated[0]
    assert result["severity"] == "Critical"
    assert result["description"] == "critical description"
    assert result["remediation"] == "parameterize now"


def test_consolidation_does_not_mix_high_severity_with_lower_severity_description():
    """
    Acceptance criterion: A consolidated finding never reports a severity taken from one group member while retaining description text from a different lower-severity member unless an explicit merge rule defines that field as severity-independent.

    Scenario: A mixed-severity group contains distinct descriptions so a mismatch between severity and description would be observable.
    Given: A deduplication group with a higher-severity finding and a lower-severity finding whose description is different and otherwise favored by prior primary selection logic.
    When: The group is consolidated.
    Then: The consolidated finding does not combine the higher severity with the lower-severity description.
    """
    findings = [
        {
            "finding_kind": "sensitive_data_exposure",
            "category": "security",
            "file": "settings.py",
            "function": "load_config",
            "summary": "Hardcoded secret",
            "description": "this lower severity description is much longer and would previously win primary selection",
            "severity": "P2",
            "skill": "layer1",
        },
        {
            "finding_kind": "sensitive_data_exposure",
            "category": "security",
            "file": "settings.py",
            "function": "load_config",
            "summary": "Hardcoded secret",
            "description": "critical source description",
            "severity": "Critical",
            "skill": "layer2",
        },
    ]

    consolidated = deduplicate(findings)

    assert len(consolidated) == 1
    result = consolidated[0]
    assert result["severity"] == "Critical"
    assert result["description"] != findings[0]["description"]
    assert result["description"] == findings[1]["description"]


def test_consolidation_keeps_description_and_remediation_metadata_with_higher_severity_source():
    """
    Acceptance criterion: For a group where the longest-description finding is lower severity than a sibling, the consolidated output's severity, description, and remediation-related metadata all correspond to the same higher-severity finding or documented severity-consistent merge source.

    Scenario: The longest-description finding is lower severity, and the higher-severity sibling has different remediation metadata.
    Given: A deduplication group where the lower-severity finding has the longest description and the higher-severity finding has distinct remediation-related fields.
    When: The group is consolidated.
    Then: The consolidated severity, description, and remediation-related metadata all match the same higher-severity source or documented severity-consistent merge source.
    """
    findings = [
        {
            "finding_kind": "crypto_misuse",
            "category": "security",
            "file": "crypto.py",
            "function": "encrypt",
            "summary": "Weak crypto",
            "description": "lower severity but longest description by far for previous tie breaker behavior",
            "severity": "P1",
            "remediation": "rotate keys eventually",
            "remediation_effort": "medium",
            "skill": "layer1",
        },
        {
            "finding_kind": "crypto_misuse",
            "category": "security",
            "file": "crypto.py",
            "function": "encrypt",
            "summary": "Weak crypto",
            "description": "critical crypto issue",
            "severity": "Critical",
            "remediation": "replace algorithm immediately",
            "remediation_effort": "high",
            "skill": "layer2",
        },
    ]

    consolidated = deduplicate(findings)

    assert len(consolidated) == 1
    result = consolidated[0]
    assert result["severity"] == "Critical"
    assert result["description"] == findings[1]["description"]
    assert result["remediation"] == findings[1]["remediation"]
    assert result["remediation_effort"] == findings[1]["remediation_effort"]


def test_consolidation_preserves_existing_behavior_when_all_severities_match():
    """
    Acceptance criterion: Existing deduplication behavior for groups where all findings have the same severity remains unchanged.

    Scenario: A same-severity group still relies on existing tie-breakers such as description length or canonical emitter.
    Given: A deduplication group where all findings share the same severity but differ in other fields relevant to current primary selection.
    When: The group is consolidated.
    Then: The consolidated finding matches the pre-existing same-severity selection behavior.
    """
    findings = [
        {
            "finding_kind": "trust_boundary_gap",
            "category": "quality",
            "file": "forms.py",
            "function": "validate",
            "summary": "Missing validation",
            "description": "short description",
            "severity": "P2",
            "remediation": "short remediation",
            "skill": "layer1",
        },
        {
            "finding_kind": "trust_boundary_gap",
            "category": "quality",
            "file": "forms.py",
            "function": "validate",
            "summary": "Missing validation",
            "description": "this is the longest same-severity description and should still win",
            "severity": "P2",
            "remediation": "long remediation",
            "skill": "layer2",
        },
    ]

    consolidated = deduplicate(findings)

    assert len(consolidated) == 1
    result = consolidated[0]
    assert result["severity"] == "P2"
    assert result["description"] == findings[1]["description"]
    assert result["remediation"] == findings[1]["remediation"]


def test_nested_location_file_difference_prevents_false_merge():
    """Two findings identical except for nested ``location.file`` must not merge.

    The active finding contract stores file/line/function under a nested
    ``location`` object. When only nested location data is present, dedup must
    read ``location.file`` rather than the (absent) top-level ``file`` key.
    Without that, both findings hash to an empty file and collapse into one —
    a false merge across different source files.
    """
    findings = [
        {
            "finding_kind": "logic_bug",
            "category": "correctness",
            "summary": "Off-by-one in loop bound",
            "description": "loop iterates one element past the end",
            "severity": "P2",
            "skill": "layer2b-correctness-specialist",
            "location": {"file": "a.py", "line": "10", "function": "scan"},
        },
        {
            "finding_kind": "logic_bug",
            "category": "correctness",
            "summary": "Off-by-one in loop bound",
            "description": "loop iterates one element past the end",
            "severity": "P2",
            "skill": "layer2b-correctness-specialist",
            "location": {"file": "b.py", "line": "10", "function": "scan"},
        },
    ]

    consolidated = deduplicate(findings)

    assert len(consolidated) == 2


def test_nested_location_equivalents_merge_across_separate_inputs():
    """Equivalent findings sharing nested ``location.file``/``function`` merge."""
    findings = [
        {
            "finding_kind": "logic_bug",
            "category": "correctness",
            "summary": "Off-by-one in loop bound",
            "description": "loop iterates one element past the end",
            "severity": "P2",
            "skill": "layer2b-correctness-specialist",
            "location": {"file": "a.py", "line": "10", "function": "scan"},
        },
        {
            "finding_kind": "logic_bug",
            "category": "correctness",
            "summary": "Off-by-one in loop bound",
            "description": "loop iterates one element past the end",
            "severity": "P2",
            "skill": "layer3a-performance-specialist",
            "location": {"file": "a.py", "line": "10", "function": "scan"},
        },
    ]

    consolidated = deduplicate(findings)

    assert len(consolidated) == 1


def test_nested_location_file_precedence_ignores_top_level_file_noise_for_separation():
    """Distinct nested location.file values must stay distinct even when
    conflicting top-level file fields are identical.

    This locks the nested-location precedence contract in dedup identity.
    """
    findings = [
        {
            "finding_kind": "logic_bug",
            "category": "correctness",
            "summary": "same summary",
            "description": "same description",
            "severity": "P2",
            "skill": "layer2b-correctness-specialist",
            "file": "shared_noise.py",
            "location": {"file": "a.py", "line": "10", "function": "scan"},
        },
        {
            "finding_kind": "logic_bug",
            "category": "correctness",
            "summary": "same summary",
            "description": "same description",
            "severity": "P2",
            "skill": "layer2b-correctness-specialist",
            "file": "shared_noise.py",
            "location": {"file": "b.py", "line": "10", "function": "scan"},
        },
    ]

    consolidated = deduplicate(findings)

    assert len(consolidated) == 2


def test_nested_location_file_precedence_preserves_same_location_dedup():
    """Equivalent nested locations should dedup even when top-level file
    fields disagree.

    This ensures same-location dedup remains stable under schema-noise.
    """
    findings = [
        {
            "finding_kind": "logic_bug",
            "category": "correctness",
            "summary": "same summary",
            "description": "same description",
            "severity": "P2",
            "skill": "layer2b-correctness-specialist",
            "file": "noise_a.py",
            "location": {"file": "service.py", "line": "10", "function": "scan"},
        },
        {
            "finding_kind": "logic_bug",
            "category": "correctness",
            "summary": "same summary",
            "description": "same description",
            "severity": "P2",
            "skill": "layer3a-performance-specialist",
            "file": "noise_b.py",
            "location": {"file": "service.py", "line": "10", "function": "scan"},
        },
    ]

    consolidated = deduplicate(findings)

    assert len(consolidated) == 1


def test_legacy_top_level_location_is_stable_without_nested_object():
    """A legacy-shaped finding (top-level keys, no nested location) stays stable."""
    finding = {
        "finding_kind": "logic_bug",
        "category": "correctness",
        "summary": "Off-by-one in loop bound",
        "description": "loop iterates one element past the end",
        "severity": "P2",
        "skill": "layer2b-correctness-specialist",
        "file": "a.py",
        "line_range": "10",
        "function": "scan",
    }

    # Two equal legacy-shaped findings share file+function+category, so they
    # consolidate into a single stable record without raising.
    consolidated = deduplicate([dict(finding), dict(finding)])

    assert len(consolidated) == 1


# ── Pass 3: intra-specialist overlapping-line duplicate collapse ──────────
#
# Fixtures give each finding a DISTINCT `function` so Pass 1 (fingerprint
# includes function) and Pass 2 (legacy key = file|function|category) never
# merge them — isolating Pass 3's (file, skill, finding_kind) + line overlap.

def _f3(fid, *, skill="layerA", file="x.py", function="fn", kind="logic_bug",
        line_range="10-20", severity="P2", desc=None, category="c",
        subject_key=None):
    finding = {
        "id": fid, "finding_kind": kind, "category": category, "file": file,
        "function": function, "summary": "s", "description": desc or f"desc-{fid}",
        "severity": severity, "skill": skill, "line_range": line_range,
    }
    if subject_key is not None:
        finding["subject_key"] = subject_key
    return finding


def test_pass3_collapses_same_skill_overlapping_same_kind():
    findings = [
        _f3("a", function="fa", line_range="10-20", severity="P2"),
        _f3("b", function="fb", line_range="15-25", severity="Critical"),  # overlaps a
    ]
    result = deduplicate(findings)
    assert len(result) == 1
    assert result[0]["severity"] == "Critical"   # higher severity survives
    assert result[0]["id"] == "b"


def test_pass3_does_not_collapse_non_overlapping():
    findings = [
        _f3("a", function="fa", line_range="10-20"),
        _f3("b", function="fb", line_range="30-40"),  # no overlap
    ]
    assert len(deduplicate(findings)) == 2


def test_pass3_does_not_collapse_cross_specialist():
    # Different skill, overlapping lines -> left for the grouper.
    findings = [
        _f3("a", function="fa", skill="layerA", line_range="10-20"),
        _f3("b", function="fb", skill="layerB", line_range="15-25"),
    ]
    assert len(deduplicate(findings)) == 2


def test_pass3_does_not_collapse_different_finding_kind():
    findings = [
        _f3("a", function="fa", kind="logic_bug", line_range="10-20"),
        _f3("b", function="fb", kind="error_signal_loss", line_range="15-25"),
    ]
    assert len(deduplicate(findings)) == 2


def test_pass3_empty_line_range_passes_through_and_does_not_block():
    findings = [
        _f3("a", function="fa", line_range="10-20", severity="P2"),
        _f3("b", function="fb", line_range="15-25", severity="Critical"),  # overlaps a
        _f3("c", function="fc", line_range=""),                            # empty -> passthrough
    ]
    result = deduplicate(findings)
    ids = {r["id"] for r in result}
    assert len(result) == 2
    assert "c" in ids                 # empty line_range survives untouched
    assert "b" in ids and "a" not in ids  # a+b collapsed to the Critical one


def test_pass3_transitive_three_way_overlap_collapses_to_one():
    # a-b overlap (18<=20), b-c overlap (28<=30), a-c do NOT (28>20).
    findings = [
        _f3("a", function="fa", line_range="10-20", severity="P2"),
        _f3("b", function="fb", line_range="18-30", severity="Critical"),
        _f3("c", function="fc", line_range="28-40", severity="P1"),
    ]
    result = deduplicate(findings)
    assert len(result) == 1
    assert result[0]["id"] == "b"     # Critical survives the transitive cluster


def test_pass3_survivor_layers_is_union_of_cluster_members():
    # f1+f2 share file/function/category -> Pass 2 merges them into one finding
    # carrying layers [layerA, layerB]; f3 (same skill, diff function) overlaps
    # -> Pass 3 merges, and the survivor's layers must union all members'.
    findings = [
        _f3("f1", skill="layerA", function="g", kind="k", line_range="10-20",
            desc="AAAAAAAA the long primary description", severity="P1"),
        _f3("f2", skill="layerB", function="g", kind="k", line_range="10-20",
            desc="short", severity="P1"),
        _f3("f3", skill="layerA", function="g2", kind="k", line_range="15-25",
            desc="f3 description", severity="P1"),
    ]
    result = deduplicate(findings)
    assert len(result) == 1
    assert set(result[0]["layers"]) == {"layerA", "layerB"}


def test_location_fields_falls_back_to_top_level_line():
    """Legacy producers emit a top-level ``line`` (not ``line_range``); it must
    feed the location so distinct/duplicate findings fingerprint correctly."""
    finding = {"file": "app.py", "line": "42", "function": "run"}
    file, line, function = _location_fields(finding)
    assert file == "app.py"
    assert line == "42"
    assert function == "run"


def test_location_fields_prefers_line_range_over_top_level_line():
    """When both are present, existing line_range precedence is preserved and
    the new top-level ``line`` fallback only applies when line_range is absent."""
    finding = {"file": "app.py", "line_range": "10-20", "line": "99"}
    _file, line, _function = _location_fields(finding)
    assert line == "10-20"


def test_pass3_does_not_collapse_distinct_subject_keys_on_overlap():
    """Two distinct bugs from the same specialist on overlapping lines but with
    different subject_keys must NOT be merged."""
    findings = [
        _f3("a", function="fa", line_range="10-20", subject_key="config:x.py:A"),
        _f3("b", function="fb", line_range="15-25", subject_key="config:x.py:B"),
    ]
    result = _collapse_intra_specialist_overlaps(findings)
    assert len(result) == 2


def test_pass3_collapses_matching_subject_keys_on_overlap():
    """Same (file, skill, kind, subject_key) with overlapping lines still
    collapses, preserving the existing merge behavior."""
    findings = [
        _f3("a", function="fa", line_range="10-20", severity="P2",
            subject_key="config:x.py:A"),
        _f3("b", function="fb", line_range="15-25", severity="Critical",
            subject_key="config:x.py:A"),
    ]
    result = _collapse_intra_specialist_overlaps(findings)
    assert len(result) == 1
    assert result[0]["severity"] == "Critical"
