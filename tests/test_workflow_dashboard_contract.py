"""
Tests for dashboard data contract and bundle writer (Cycle 8 Commit 1).
"""
import json
from pathlib import Path
from sentinel.workflow.dashboard.contract import (
    SCHEMA_VERSION,
    DashboardBundle,
    DashboardMetadata,
    SeverityRiskRow,
    ThemeSummaryRow,
    ModuleHotspotRow,
    WorkflowStateRow,
    LaneConflictRow,
    HumanOverrideRow,
    RunConfidence,
    AuditFreshnessSummary,
    LensDivergence,
)
from sentinel.workflow.dashboard.build_dashboard import BundleInput, build_dashboard_bundle, write_bundle_json

def test_schema_version_constant():
    assert SCHEMA_VERSION == "1.0.0"

def test_dashboard_metadata_to_dict():
    meta = DashboardMetadata(scan_id="s1", profile="p", profile_version="v1", artifact_scope="scope", artifact_status="status", review_policy="policy", generation_timestamp="now", freshness_evidence="fresh")
    d = meta.to_dict()
    assert d["scan_id"] == "s1"
    assert d["profile"] == "p"
    assert d["profile_version"] == "v1"
    assert d["artifact_scope"] == "scope"
    assert d["artifact_status"] == "status"
    assert d["review_policy"] == "policy"
    assert d["generation_timestamp"] == "now"
    assert d["freshness_evidence"] == "fresh"

def test_empty_bundle_to_dict_keys():
    bundle = DashboardBundle()
    d = bundle.to_dict()
    # All top-level keys present
    expected_keys = [
        "schema_version", "metadata", "severity_risk_summary", "theme_summary", "module_hotspot_summary",
        "workflow_state_summary", "lane_conflict_summary", "human_override_summary", "run_confidence",
        "audit_freshness_summary", "lens_divergence"
    ]
    for k in expected_keys:
        assert k in d
    # All lists/dicts are empty or default
    assert d["severity_risk_summary"] == []
    assert d["theme_summary"] == []
    assert d["module_hotspot_summary"] == []
    assert d["workflow_state_summary"] == []
    assert d["lane_conflict_summary"] == []
    assert d["human_override_summary"] == []
    assert isinstance(d["run_confidence"], dict)
    assert isinstance(d["audit_freshness_summary"], dict)
    assert isinstance(d["lens_divergence"], dict)

def test_write_bundle_json(tmp_path):
    bundle = build_dashboard_bundle(BundleInput(scan_id="scanX", profile="owner_gate", profile_version="0.8.0", artifact_scope="test", artifact_status="ok", review_policy="strict", generation_timestamp="2026-05-30T00:00:00Z", freshness_evidence="synced"))
    out_path = tmp_path / "bundle.json"
    write_bundle_json(bundle, out_path)
    data = json.loads(out_path.read_text(encoding="utf-8"))
    assert data["schema_version"] == "1.0.0"
    assert data["metadata"]["profile"] == "owner_gate"
    assert data["metadata"]["scan_id"] == "scanX"
    # All section keys present
    for k in [
        "severity_risk_summary", "theme_summary", "module_hotspot_summary", "workflow_state_summary",
        "lane_conflict_summary", "human_override_summary", "run_confidence", "audit_freshness_summary", "lens_divergence"
    ]:
        assert k in data
    # All lists/dicts are empty or default
    assert data["severity_risk_summary"] == []
    assert data["theme_summary"] == []
    assert data["module_hotspot_summary"] == []
    assert data["workflow_state_summary"] == []
    assert data["lane_conflict_summary"] == []
    assert data["human_override_summary"] == []
    assert isinstance(data["run_confidence"], dict)
    assert isinstance(data["audit_freshness_summary"], dict)
    assert isinstance(data["lens_divergence"], dict)
