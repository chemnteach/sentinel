"""
Dashboard bundle data contract. Cycle 8 Commit 1 (Phase 3 MVP).

Defines the data schema for the dashboard bundle artifact, including all top-level
sections, metadata, and lens divergence. All dataclasses are frozen+slots and provide
to_dict() for serialization. The top-level schema_version is semver ('1.0.0') because
this bundle is the first external-consumer surface in the engagement (SharePoint bridge,
dashboard branch readers, analytics). Semver semantics serve external consumers in a way
the engagement's internal '1.0' convention does not. This departure is documented here so
future cycles asking 'why semver here when other schemas use major-version-only' find the
rationale at the source.
"""

from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Tuple
from pathlib import Path
from sentinel.workflow.models import FieldDrift

# Schema version for dashboard bundle contract (semver, see module docstring)
SCHEMA_VERSION = "1.0.0"

@dataclass(frozen=True, slots=True)
class DashboardMetadata:
    """Artifact-level metadata for the dashboard bundle."""
    scan_id: str = ""
    profile: str = ""
    profile_version: str = ""
    artifact_scope: str = ""
    artifact_status: str = ""
    review_policy: str = ""
    generation_timestamp: str = ""
    freshness_evidence: str = ""
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

@dataclass(frozen=True, slots=True)
class SeverityRiskRow:
    severity: str
    count: int
    states: Dict[str, int]
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

@dataclass(frozen=True, slots=True)
class ThemeSummaryRow:
    lens: str
    theme_id: str
    title: str
    member_count: int
    aggregate_severity: str
    skills_involved: List[str]
    categories: List[str]
    modules_affected: List[str]
    state: str
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

@dataclass(frozen=True, slots=True)
class ModuleHotspotRow:
    module: str
    open_finding_count: int
    severity_mix: Dict[str, int]
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

@dataclass(frozen=True, slots=True)
class WorkflowStateRow:
    state: str
    count: int
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

@dataclass(frozen=True, slots=True)
class LaneConflictRow:
    finding_id: str
    theme_id: str
    needs_human_review: bool
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

@dataclass(frozen=True, slots=True)
class HumanOverrideRow:
    finding_id: str
    issue_number: int
    drift_kind: str
    drift_details: Tuple[FieldDrift, ...]
    override_label_present: bool
    def to_dict(self) -> dict[str, Any]:
        # Convert drift_details tuple to list of dicts
        d = asdict(self)
        d["drift_details"] = [fd.to_dict() for fd in self.drift_details]
        return d

@dataclass(frozen=True, slots=True)
class RunConfidence:
    confidence_label: str
    supporting_evidence: Any
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

@dataclass(frozen=True, slots=True)
class AuditFreshnessSummary:
    scan_id: str = ""
    generation_timestamp: str = ""
    profile: str = ""
    profile_version: str = ""
    workflow_store_sync_status: str = ""
    artifact_status: str = ""
    freshness_evidence: str = ""
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

@dataclass(frozen=True, slots=True)
class LensDivergence:
    """Lens divergence metrics for dashboard bundle.

    consolidation_ratio and mean_conceptual_span are the two implemented
    section 9.5 sub-metrics; cross_lens_entropy is deferred (see cycle 8 Q3/Q4).

    signal_bearing / suppression_reason implement the section 6.8 signal-bearing
    suppression (engagement item 13). Per section 6.8.4 "Presentation Rules,"
    lens divergence must be suppressed or marked not meaningful when the
    conceptual lens is singleton, low-signal, or unstable, and the report must
    disclose the degraded mode. suppression_reason names which mode, so the
    renderer can disclose it. Convention: signal_bearing=True implies
    suppression_reason="" (the metrics are meaningful); signal_bearing=False
    pairs with one of the section-6.8.2 reasons. The convention is documented
    here and pinned by tests, not enforced via __post_init__ (matching the
    cycle 9 WorkflowSyncState string-enum precedent).

    suppression_reason values (section 6.8.2 triggers):
      ""                       -- signal-bearing; metrics meaningful.
      "non-signal-bearing"     -- fewer than 2 non-singleton clusters (6.8.2-1).
      "low-signal"             -- >80% noise/singleton (6.8.2-2).
      "effectively-singleton"  -- concept count within +/-10% of finding count (6.8.2-3).
      "concept-unstable"       -- stability reconciliation failed (6.8.2-4).
    """
    consolidation_ratio: float = 0.0
    mean_conceptual_span: float = 0.0
    signal_bearing: bool = True
    suppression_reason: str = ""
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

@dataclass(frozen=True, slots=True)
class DashboardBundle:
    """
    Top-level dashboard bundle contract for Phase 3 MVP.
    schema_version is semver ('1.0.0') because this bundle is the first external-consumer surface in the engagement (SharePoint bridge, dashboard branch readers, analytics). Semver semantics serve external consumers in a way the engagement's internal '1.0' convention does not. This departure is documented here so future cycles asking 'why semver here when other schemas use major-version-only' find the rationale at the source.
    """
    schema_version: str = SCHEMA_VERSION
    metadata: DashboardMetadata = field(default_factory=DashboardMetadata)
    severity_risk_summary: List[SeverityRiskRow] = field(default_factory=list)
    theme_summary: List[ThemeSummaryRow] = field(default_factory=list)
    module_hotspot_summary: List[ModuleHotspotRow] = field(default_factory=list)
    workflow_state_summary: List[WorkflowStateRow] = field(default_factory=list)
    lane_conflict_summary: List[LaneConflictRow] = field(default_factory=list)
    human_override_summary: List[HumanOverrideRow] = field(default_factory=list)
    run_confidence: RunConfidence = field(default_factory=lambda: RunConfidence(confidence_label="", supporting_evidence=None))
    audit_freshness_summary: AuditFreshnessSummary = field(default_factory=AuditFreshnessSummary)
    lens_divergence: LensDivergence = field(default_factory=LensDivergence)

    def to_dict(self) -> dict[str, Any]:
        # Serialize all fields, using to_dict for nested dataclasses
        return {
            "schema_version": self.schema_version,
            "metadata": self.metadata.to_dict(),
            "severity_risk_summary": [row.to_dict() for row in self.severity_risk_summary],
            "theme_summary": [row.to_dict() for row in self.theme_summary],
            "module_hotspot_summary": [row.to_dict() for row in self.module_hotspot_summary],
            "workflow_state_summary": [row.to_dict() for row in self.workflow_state_summary],
            "lane_conflict_summary": [row.to_dict() for row in self.lane_conflict_summary],
            "human_override_summary": [row.to_dict() for row in self.human_override_summary],
            "run_confidence": self.run_confidence.to_dict(),
            "audit_freshness_summary": self.audit_freshness_summary.to_dict(),
            "lens_divergence": self.lens_divergence.to_dict(),
        }
