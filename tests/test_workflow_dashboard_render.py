import pytest
from sentinel.workflow.dashboard.contract import (
	DashboardBundle, DashboardMetadata, SeverityRiskRow, ThemeSummaryRow,
	ModuleHotspotRow, WorkflowStateRow, LaneConflictRow, HumanOverrideRow,
	RunConfidence, AuditFreshnessSummary, LensDivergence,
)
from sentinel.workflow.dashboard.render_markdown import (
	render_bundle_markdown, write_bundle_markdown,
	_band_label_for_ratio, _band_label_for_span,
)


def _populated_bundle(lens_divergence=None):
	return DashboardBundle(
		metadata=DashboardMetadata(
			scan_id="scanX", profile="owner_gate", profile_version="0.8.0",
			generation_timestamp="2026-05-31T00:00:00Z",
		),
		severity_risk_summary=[SeverityRiskRow("P0", 2, {"Open": 1, "Closed": 1})],
		theme_summary=[ThemeSummaryRow(
			"struct", "S-001", "Struct one", 2, "P0",
			["security"], ["injection"], ["mod_a"], "triage",
		)],
		module_hotspot_summary=[ModuleHotspotRow("mod_a", 3, {"P0": 2, "P1": 1})],
		workflow_state_summary=[WorkflowStateRow("triage", 2)],
		lane_conflict_summary=[LaneConflictRow("F1", "S-001", True)],
		human_override_summary=[HumanOverrideRow("F1", 101, "", (), True)],
		run_confidence=RunConfidence(
			"high",
			{"metrics": {"lane_agreement_pct": 98.0}, "gates": {"confidence_floor": "pass"}},
		),
		audit_freshness_summary=AuditFreshnessSummary(
			scan_id="scanX", generation_timestamp="2026-05-31T00:00:00Z",
			profile="owner_gate", profile_version="0.8.0",
			workflow_store_sync_status="synced", artifact_status="ok",
			freshness_evidence="synced",
		),
		lens_divergence=lens_divergence or LensDivergence(2.0, 2.6),
	)


def test_render_bundle_markdown_basic():
	md = render_bundle_markdown(_populated_bundle())
	# H1 with scan_id.
	assert md.startswith("# Sentinel Dashboard -- scanX")
	# Foundation-question H2s in the locked order, then lens divergence.
	order = ["## Scope", "## Risk", "## Trend", "## Confidence", "## Lens Divergence"]
	idx = [md.index(h) for h in order]
	assert idx == sorted(idx)
	# H3s under the correct foundation question.
	scope, risk, trend, conf, lens = idx
	assert scope < md.index("### Theme Summary") < risk
	assert scope < md.index("### Workflow State Summary") < risk
	assert risk < md.index("### Severity / Risk Summary") < trend
	assert risk < md.index("### Module / Hotspot Summary") < trend
	assert conf < md.index("### Run Confidence") < lens
	assert conf < md.index("### Human-Override Summary") < lens
	assert conf < md.index("### Lane-Conflict Summary") < lens
	assert conf < md.index("### Audit / Freshness Summary") < lens
	# A table renders for severity/risk.
	assert "| Severity | Count | States |" in md


def test_render_bundle_markdown_empty_sections():
	md = render_bundle_markdown(DashboardBundle())
	assert "(no themes in this scan)" in md
	assert "(no workflow states in this scan)" in md
	assert "(no severity/risk rows in this scan)" in md
	assert "(no module hotspots in this scan)" in md
	assert "(no lane conflicts in this scan)" in md
	assert "(no human overrides in this scan)" in md
	# Empty run confidence label -> placeholder.
	assert "no confidence label available" in md
	# Audit/freshness has no placeholder (always populated by construction).
	assert "### Audit / Freshness Summary" in md


def test_render_bundle_markdown_trend_deferred():
	md = render_bundle_markdown(_populated_bundle())
	assert "## Trend" in md
	assert "Trend reporting requires multi-scan history" in md
	assert "see section 9.4-7" in md


def test_render_bundle_markdown_lens_divergence_bands():
	md = render_bundle_markdown(_populated_bundle(LensDivergence(2.0, 2.6)))
	assert "**Consolidation ratio:** 2.0 (noticeable consolidation)" in md
	assert "**Mean conceptual span:** 2.6 (typical)" in md


def test_render_bundle_markdown_lens_divergence_suppressed():
	# Item 13 (cycle 10 Commit 1): explicit signal_bearing=False suppression
	# replaces the cycle 8 Commit 7 both-metrics-zero heuristic. Suppression is
	# now driven by the flag, not by a 0.0/0.0 proxy.
	ld = LensDivergence(0.0, 0.0, signal_bearing=False, suppression_reason="non-signal-bearing")
	md = render_bundle_markdown(_populated_bundle(ld))
	assert "lens divergence not meaningful for this scan: conceptual lens not signal-bearing; see section 6.8" in md
	# The numeric band lines must NOT render when suppressed.
	assert "Consolidation ratio:" not in md


@pytest.mark.parametrize("reason,detail", [
	("non-signal-bearing", "conceptual lens not signal-bearing"),
	("low-signal", "low signal in conceptual lens"),
	("effectively-singleton", "conceptual themes effectively singleton"),
	("concept-unstable", "conceptual themes unstable"),
])
def test_render_lens_divergence_suppression_reasons(reason, detail):
	# Section 6.8.4 "disclose the degraded mode": each reason renders distinctly.
	ld = LensDivergence(2.0, 2.6, signal_bearing=False, suppression_reason=reason)
	md = render_bundle_markdown(_populated_bundle(ld))
	assert f"lens divergence not meaningful for this scan: {detail}; see section 6.8" in md
	assert "Consolidation ratio:" not in md


def test_render_lens_divergence_signal_bearing_renders_bands():
	# signal_bearing=True (default) renders the band-label flow, even at 0.0/0.0
	# (no longer a suppression proxy after item 13).
	ld = LensDivergence(2.0, 2.6, signal_bearing=True)
	md = render_bundle_markdown(_populated_bundle(ld))
	assert "**Consolidation ratio:** 2.0 (noticeable consolidation)" in md
	assert "**Mean conceptual span:** 2.6 (typical)" in md


def test_render_bundle_markdown_ascii_clean():
	# Populated and empty bundles both render ASCII-only (engagement convention).
	assert render_bundle_markdown(_populated_bundle()).isascii()
	assert render_bundle_markdown(DashboardBundle()).isascii()


def test_write_bundle_markdown(tmp_path):
	bundle = _populated_bundle()
	out_path = tmp_path / "dashboard.md"
	write_bundle_markdown(bundle, out_path)
	assert out_path.read_text(encoding="utf-8") == render_bundle_markdown(bundle)


@pytest.mark.parametrize("value,label", [
	(0.5, "conceptual lens splits more"),
	(1.0, "lenses agree"),
	(1.2, "mild consolidation"),
	(1.5, "mild consolidation"),
	(2.0, "noticeable consolidation"),
	(3.0, "noticeable consolidation"),
	(4.0, "strong consolidation"),
])
def test_band_label_for_ratio(value, label):
	assert _band_label_for_ratio(value) == label


@pytest.mark.parametrize("value,label", [
	(0.5, "below 1:1 (some conceptual themes span no structural theme)"),
	(1.0, "1:1 mapping"),
	(2.0, "typical"),
	(3.0, "typical"),
	(4.0, "above typical"),
	(5.0, "above typical"),
	(7.0, "heavy consolidation"),
])
def test_band_label_for_span(value, label):
	assert _band_label_for_span(value) == label
