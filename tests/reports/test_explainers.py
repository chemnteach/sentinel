import pytest

from sentinel.assess.finding_kinds import SUPPORTED_REPORT_FINDING_KINDS
from sentinel.reports import explainers
from sentinel.reports.explainers import _merge_explainers_with_clusters


def test_validation_fails_when_supported_finding_kind_lacks_cluster_mapping():
    """
    Acceptance criterion: Initialization fails fast or tests fail when a supported finding kind lacks a corresponding explainer cluster mapping.

    Scenario: A supported finding kind used by report generation is missing from the explainer registry cluster metadata.
    Given: An authoritative supported finding kind set containing a kind not present in the cluster mapping.
    When: The explainer coverage validation or registry initialization runs.
    Then: An exception is raised identifying the missing supported finding kind.
    """
    supported_kinds = frozenset({"logic_bug", "missing_kind"})
    explainers_map = {"logic_bug": {"what": "desc"}}
    clusters = {"logic_bug": "cluster text"}

    with pytest.raises(ValueError, match="missing_kind"):
        _merge_explainers_with_clusters(explainers_map, clusters, supported_kinds)


def test_supported_finding_kinds_all_have_cluster_metadata_through_registry():
    """
    Acceptance criterion: Every supported finding kind used by report generation has cluster metadata available through the explainer registry.

    Scenario: The supported finding kind set is fully covered by the explainer registry cluster metadata.
    Given: A complete mapping for every supported finding kind used by report generation.
    When: The explainer registry is built or validated.
    Then: Validation succeeds and each supported finding kind resolves to cluster metadata through the registry.
    """
    for kind in SUPPORTED_REPORT_FINDING_KINDS:
        assert kind in explainers.EXPLAINERS
        assert explainers.EXPLAINERS[kind]["cluster"]


def test_registry_merge_detects_uncovered_supported_finding_kinds_instead_of_skipping_them():
    """
    Acceptance criterion: The registry merge logic no longer silently skips uncovered finding kinds without detection.

    Scenario: Registry merge encounters a supported finding kind with no explainer cluster coverage.
    Given: Merge inputs where one supported finding kind is uncovered.
    When: The registry merge logic executes.
    Then: The merge path fails explicitly rather than returning a partially covered registry.
    """
    supported_kinds = frozenset({"logic_bug", "api_contract_mismatch"})
    explainers_map = {
        "logic_bug": {"what": "desc"},
        "api_contract_mismatch": {"what": "desc"},
    }
    clusters = {"logic_bug": "cluster text"}

    with pytest.raises(ValueError, match="api_contract_mismatch"):
        _merge_explainers_with_clusters(explainers_map, clusters, supported_kinds)


def test_validation_does_not_fail_when_non_supported_kind_lacks_cluster_mapping():
    """
    Acceptance criterion: Every supported finding kind used by report generation has cluster metadata available through the explainer registry.

    Scenario: A kind outside the supported report-generation set has no cluster mapping.
    Given: A complete mapping for all supported finding kinds and an uncovered non-supported kind.
    When: The explainer coverage validation runs.
    Then: Validation succeeds because only supported finding kinds are required to have cluster metadata.
    """
    supported_kinds = frozenset({"logic_bug"})
    explainers_map = {
        "logic_bug": {"what": "desc"},
        "non_supported_kind": {"what": "desc"},
    }
    clusters = {"logic_bug": "cluster text"}

    merged = _merge_explainers_with_clusters(explainers_map, clusters, supported_kinds)

    assert merged["logic_bug"]["cluster"] == "cluster text"
    assert "cluster" not in merged["non_supported_kind"]


def test_explainers_module_documents_enforced_coverage_contract():
    """
    Acceptance criterion: Documentation in reports/explainers.py accurately describes the enforced coverage contract.

    Scenario: The explainers module documentation is inspected after the invariant is added.
    Given: The reports/explainers.py module docstring or adjacent contract documentation.
    When: The documentation text is read in tests.
    Then: It states that every supported finding kind used by report generation must have explainer cluster metadata and that missing coverage is treated as an error.
    """
    module_doc = explainers.__doc__

    assert module_doc is not None
    assert "Every supported finding kind used by report generation must have" in module_doc
    assert "missing coverage is treated as an error" in module_doc
