"""
sentinel/defect/models.py

Data models for Defect/Bug intake, Root Cause Analysis (RCA), and Defect Specs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class DefectSeverity:
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"

    ALL = frozenset({CRITICAL, HIGH, MEDIUM, LOW})


class DefectStatus:
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    DEFERRED = "DEFERRED"
    REJECTED = "REJECTED"
    IN_RCA = "IN_RCA"
    RCA_COMPLETE = "RCA_COMPLETE"
    DIAGNOSED_REPORT_READY = "DIAGNOSED_REPORT_READY"
    IN_SPEC = "IN_SPEC"
    SPEC_APPROVED = "SPEC_APPROVED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETE = "COMPLETE"

    ALL = frozenset({
        PENDING,
        APPROVED,
        DEFERRED,
        REJECTED,
        IN_RCA,
        RCA_COMPLETE,
        DIAGNOSED_REPORT_READY,
        IN_SPEC,
        SPEC_APPROVED,
        IN_PROGRESS,
        COMPLETE,
    })


@dataclass
class DefectReport:
    """
    Defect / Bug Report model representing an ingested issue.
    """
    defect_id: str
    title: str
    description: str
    symptoms: str
    stack_trace: str = ""
    reproduction_steps: str = ""
    suspected_files: list[str] = field(default_factory=list)
    severity: str = DefectSeverity.HIGH
    submitted_by: str = "human"
    submitted_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    status: str = DefectStatus.PENDING
    selected_remediation: str = ""
    report_path: str = ""
    patch_path: str = ""
    task_ids: list[str] = field(default_factory=list)
    history: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DefectReport":
        return cls(
            defect_id=data["defect_id"],
            title=data["title"],
            description=data["description"],
            symptoms=data.get("symptoms", ""),
            stack_trace=data.get("stack_trace", ""),
            reproduction_steps=data.get("reproduction_steps", ""),
            suspected_files=data.get("suspected_files", []),
            severity=data.get("severity", DefectSeverity.HIGH),
            submitted_by=data.get("submitted_by", "human"),
            submitted_at=data.get("submitted_at", ""),
            status=data.get("status", DefectStatus.PENDING),
            selected_remediation=data.get("selected_remediation", ""),
            report_path=data.get("report_path", ""),
            patch_path=data.get("patch_path", ""),
            task_ids=data.get("task_ids", []),
            history=data.get("history", []),
        )


@dataclass
class RCAReport:
    """
    Root Cause Analysis output following 3-tier causal decomposition.
    """
    defect_id: str
    symptom_analysis: str
    proximal_mechanism: str  # Tier 1: immediate crash / fault site
    systemic_factor: str     # Tier 2: architectural / invariant gap
    root_cause: str          # Tier 3: underlying design root cause
    blast_radius: list[str] = field(default_factory=list)
    external_dependency: bool = False
    out_of_scope_reasons: str = ""
    remediation_options: list[dict[str, Any]] = field(default_factory=list)
    recommended_option: str = ""
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RCAReport":
        return cls(
            defect_id=data["defect_id"],
            symptom_analysis=data["symptom_analysis"],
            proximal_mechanism=data["proximal_mechanism"],
            systemic_factor=data["systemic_factor"],
            root_cause=data["root_cause"],
            blast_radius=data.get("blast_radius", []),
            external_dependency=data.get("external_dependency", False),
            out_of_scope_reasons=data.get("out_of_scope_reasons", ""),
            remediation_options=data.get("remediation_options", []),
            recommended_option=data.get("recommended_option", ""),
            created_at=data.get("created_at", ""),
        )


@dataclass
class DefectSpec:
    """
    Concrete remediation spec containing unified diff / patch and TDD reproduction test stubs.
    """
    defect_id: str
    selected_option: str
    affected_symbols: list[str] = field(default_factory=list)
    reproduction_test_stubs: dict[str, Any] = field(default_factory=dict)
    proposed_diff: str = ""
    acceptance_criteria: list[str] = field(default_factory=list)
    task_definitions: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DefectSpec":
        return cls(
            defect_id=data["defect_id"],
            selected_option=data["selected_option"],
            affected_symbols=data.get("affected_symbols", []),
            reproduction_test_stubs=data.get("reproduction_test_stubs", {}),
            proposed_diff=data.get("proposed_diff", ""),
            acceptance_criteria=data.get("acceptance_criteria", []),
            task_definitions=data.get("task_definitions", []),
            created_at=data.get("created_at", ""),
        )
