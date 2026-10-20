import json

import pytest

# Test-only cross-track import: the production loader stays decoupled from the
# assess track, but the test synthesizes format-correct JSON by calling the
# grouper's own serializer on a real SemanticGroup. This is the most rigorous
# compatibility pin -- if grouper's on-disk shape changes, this test breaks.
from sentinel.assess.grouper import (
	Provenance,
	SemanticGroup,
	_serialize_group,
	GROUPS_FILENAME,
)

from sentinel.workflow.struct_theme_loader import (
	SemanticGroupRecord,
	GroupProvenanceRecord,
	load_semantic_groups,
	load_struct_themes,
	_deserialize_semantic_group,
)
from sentinel.workflow.models import ThemeIssueRecord, FindingIssueRecord, ThemeReference

from tests.conftest import write_semantic_groups


def _make_grouper_group(group_id="g0001", members=("F1", "F2")):
	return SemanticGroup(
		group_id=group_id,
		grouping_key="security|sql-injection|db.py",
		mode="registry",
		severity="P1",
		member_count=len(members),
		member_finding_ids=tuple(members),
		contributing_emitters=("layer2c",),
		canonical_emitter="layer2c",
		peer_emitters=("layer2c", "layer3d"),
		subject_key="db.query",
		finding_kind="sql-injection",
		file="db.py",
		function="query",
		category="security",
		provenance=Provenance(
			mode="registry",
			grouping_key="security|sql-injection|db.py",
			subject_key="db.query",
			finding_kind="sql-injection",
			file="db.py",
			function="query",
			category="security",
		),
	)


def test_loads_grouper_shape(tmp_path):
	# write_semantic_groups produces the canonical on-disk shape; the loader must read it.
	groups = [_make_grouper_group("g0001", ("F1", "F2")), _make_grouper_group("g0002", ("F3",))]
	write_semantic_groups(tmp_path, groups)
	loaded = load_semantic_groups(tmp_path)
	assert len(loaded) == 2
	assert all(isinstance(r, SemanticGroupRecord) for r in loaded)
	by_id = {r.group_id: r for r in loaded}
	g1 = by_id["g0001"]
	assert g1.grouping_key == "security|sql-injection|db.py"
	assert g1.mode == "registry"
	assert g1.severity == "P1"
	assert g1.member_count == 2
	assert g1.member_finding_ids == ("F1", "F2")
	assert g1.contributing_emitters == ("layer2c",)
	assert g1.canonical_emitter == "layer2c"
	assert g1.peer_emitters == ("layer2c", "layer3d")
	assert g1.subject_key == "db.query"
	assert g1.finding_kind == "sql-injection"
	assert g1.file == "db.py"
	assert g1.function == "query"
	assert g1.category == "security"
	assert isinstance(g1.provenance, GroupProvenanceRecord)
	assert g1.provenance.mode == "registry"
	assert g1.provenance.subject_key == "db.query"


def test_deserialize_semantic_group_roundtrip():
	# Serialize via grouper's authority, deserialize via the workflow side.
	group = _make_grouper_group("g0009", ("F7", "F8"))
	payload = _serialize_group(group)
	record = _deserialize_semantic_group(payload)
	assert record.group_id == "g0009"
	assert record.member_finding_ids == ("F7", "F8")
	assert record.peer_emitters == ("layer2c", "layer3d")
	assert record.provenance.finding_kind == "sql-injection"
	assert record.provenance.category == "security"


def test_returns_empty_tuple_on_missing_file(tmp_path):
	# No semantic_groups.json in this state_dir.
	assert load_semantic_groups(tmp_path) == ()


def test_returns_empty_tuple_on_malformed_json(tmp_path):
	(tmp_path / GROUPS_FILENAME).write_text("{ not valid json", encoding="utf-8")
	assert load_semantic_groups(tmp_path) == ()


def test_returns_empty_tuple_on_unexpected_shape(tmp_path):
	# Valid JSON but a dict at top level instead of a list.
	(tmp_path / GROUPS_FILENAME).write_text(json.dumps({"group_id": "g1"}), encoding="utf-8")
	assert load_semantic_groups(tmp_path) == ()


def test_path_accepts_str_and_path(tmp_path):
	write_semantic_groups(tmp_path, [_make_grouper_group()])
	assert len(load_semantic_groups(tmp_path)) == 1
	assert len(load_semantic_groups(str(tmp_path))) == 1


def _make_record(group_id="g0001", grouping_key="security|sql-injection|db.py", members=("F1", "F2")):
	return SemanticGroupRecord(
		group_id=group_id,
		grouping_key=grouping_key,
		mode="registry",
		severity="P1",
		member_count=len(members),
		member_finding_ids=tuple(members),
		contributing_emitters=("layer2c", "layer3d"),
		canonical_emitter="layer2c",
		peer_emitters=("layer2c", "layer3d"),
		subject_key="db.query",
		finding_kind="sql-injection",
		file="db.py",
		function="query",
		category="security",
		provenance=GroupProvenanceRecord(
			mode="registry", grouping_key=grouping_key, subject_key="db.query",
			finding_kind="sql-injection", file="db.py", function="query", category="security",
		),
	)


def _make_finding(finding_id, summary, module="db.py"):
	return FindingIssueRecord(
		triage_item_id=f"TD-{finding_id}",
		finding_id=finding_id,
		summary=summary,
		severity="P1",
		skill="layer2c",
		module=module,
		category="security",
		location=f"{module}:1",
		description=f"desc {finding_id}",
		suggested_fix="fix",
		status="triage",
		human_triage=None,
		effort_estimate="medium",
		risk_of_deferral="high",
		primary_theme=ThemeReference(lens="struct", theme_id="T-001", title="t", member_count=1),
		struct_theme_id="T-001",
		concept_theme_id="C-001",
	)


def test_load_struct_themes_with_findings(tmp_path):
	# Item 14: load_struct_themes now passes findings to build_struct_themes,
	# producing section-6.4-faithful titles (not the cycle-9 placeholder).
	write_semantic_groups(tmp_path, [_make_grouper_group("g0001", ("F1", "F2"))])
	findings = [
		_make_finding("F1", "Unchecked Optional return in alpha"),
		_make_finding("F2", "Unchecked Optional return in beta"),
	]
	themes = load_struct_themes(tmp_path, findings)
	assert len(themes) == 1
	assert themes[0].lens == "struct"
	# Common-prefix section-6.4 title, NOT "Structural group: <grouping_key>".
	assert themes[0].title == "Unchecked Optional return in"
	assert themes[0].member_finding_ids == ["F1", "F2"]


def test_load_struct_themes_empty_findings_falls_back(tmp_path):
	# Default-empty findings -> producer empty-member fallback (SemanticGroup-only
	# values), preserving the cycle-9 adapter's behavior as the fallback path.
	write_semantic_groups(tmp_path, [_make_grouper_group("g0001", ("F1", "F2"))])
	themes = load_struct_themes(tmp_path)
	assert len(themes) == 1
	assert themes[0].title == "Structural group: security|sql-injection|db.py"
	assert themes[0].lens == "struct"


def test_load_struct_themes_empty_when_no_file(tmp_path):
	assert load_struct_themes(tmp_path) == ()
	assert load_struct_themes(tmp_path, [_make_finding("F1", "x")]) == ()


def test_lens_constant(tmp_path):
	write_semantic_groups(tmp_path, [_make_grouper_group("g0001"), _make_grouper_group("g0002")])
	themes = load_struct_themes(tmp_path)
	assert themes
	assert all(t.lens == "struct" for t in themes)
