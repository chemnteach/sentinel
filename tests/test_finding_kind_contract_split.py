from __future__ import annotations

from sentinel.assess.finding_kinds import (
    FINDING_KIND_REGISTRY,
    SUPPORTED_QUICK_REVIEW_FINDING_KINDS,
    SUPPORTED_REPORT_FINDING_KINDS,
)
from sentinel.assess.finding_schema import (
    get_declared_finding_kind_definition,
    get_supported_report_finding_kind_definition,
    iter_supported_report_finding_kind_definitions,
)


EXPECTED_QUICK_REVIEW_REPORT_KINDS = frozenset(
    {
        "logic_bug",
        "contract_violation",
        "injection_risk",
        "missing_transaction_boundary",
        "timeout_handling_gap",
        "structural_complexity",
        "algorithmic_scaling_gap",
        "error_signal_loss",
    }
)


def test_supported_report_kinds_match_quick_review_contract() -> None:
    """Quick Review contract kinds must remain an explicit 8-kind subset."""

    declared_kinds = frozenset(entry.kind_name for entry in FINDING_KIND_REGISTRY)

    assert SUPPORTED_QUICK_REVIEW_FINDING_KINDS == EXPECTED_QUICK_REVIEW_REPORT_KINDS
    assert SUPPORTED_QUICK_REVIEW_FINDING_KINDS < declared_kinds
    assert SUPPORTED_REPORT_FINDING_KINDS == declared_kinds


def test_schema_exposes_declared_and_supported_boundaries() -> None:
    """Schema helpers should preserve broad catalog access and report-safe access."""

    assert get_declared_finding_kind_definition("security_misconfiguration") is not None
    assert get_supported_report_finding_kind_definition("security_misconfiguration") is None

    supported_names = {
        entry.kind_name for entry in iter_supported_report_finding_kind_definitions()
    }
    assert supported_names == EXPECTED_QUICK_REVIEW_REPORT_KINDS
