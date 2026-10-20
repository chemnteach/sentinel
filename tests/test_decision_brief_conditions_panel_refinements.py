"""Conditions-panel state-machine refinements for decision_brief.py.

Covers the five conditions-panel JS refinements (items 231/234/245/284/295):
  - 231: importDecisions performs structured validation and surfaces a
         user-facing error instead of throwing / failing silently.
  - 234: panel rehydration keys condition text by a stable criterion id
         (data-cid) rather than array position.
  - 245: exportDecisions aggregates conditions once per group.
  - 284: clearGroupDecision also resets the group's conditions-panel state.
  - 295: openConditionsPanel keeps an already-open panel open (no rebuild
         flicker / focus loss) when the same group is re-selected.

Tests assert on the generated JS strings (no browser engine available), which
is the observable contract of the report renderer — mirroring the existing
tests/test_decision_brief_conditions_cluster.py style.
"""
from __future__ import annotations

from sentinel.reports import decision_brief as db


def _fn_body(js: str, name: str) -> str:
    """Return the source of a top-level JS function by name (up to the next one)."""
    start = js.index("function " + name)
    nxt = js.find("\nfunction ", start + 1)
    return js[start: nxt if nxt != -1 else len(js)]


def _all_js() -> str:
    """The full client script as emitted: both JS blocks concatenated."""
    return db._JS + "\n" + db._JS_STATE


# ── 231 — import validation ───────────────────────────────────────────────
def test_import_defines_structured_validator():
    js = _all_js()
    assert "function _validateImportedDecisions" in js
    # A whitelist of valid decision values is used, not a blind accept.
    assert "_VALID_DECISIONS" in js
    assert "APPROVE_WITH_CONDITIONS" in js


def test_import_handles_read_errors_and_bad_json():
    body = _fn_body(_all_js(), "importDecisions")
    # A FileReader read failure is surfaced, not swallowed.
    assert "reader.onerror" in body
    # Malformed JSON shows a user-facing error rather than throwing uncaught.
    assert "Import failed" in body
    assert "msg warn" in body
    # The structured validator is invoked.
    assert "_validateImportedDecisions" in body


def test_import_uses_validator_result_not_blind_apply():
    body = _fn_body(_all_js(), "importDecisions")
    # It must branch on the validator's outcome before applying decisions.
    assert ".valid" in body
    assert "_applyAllDecided" in body


# ── 234 — rehydration by stable criterion id ──────────────────────────────
def test_criterion_id_helper_defined():
    assert "function _critId" in _all_js()


def test_open_panel_tags_criteria_with_stable_cid():
    body = _fn_body(_all_js(), "openConditionsPanel")
    assert "data-cid" in body
    assert "_critId(" in body


def test_update_criterion_stores_cid_not_only_index():
    body = _fn_body(_all_js(), "updateCriterion")
    assert "cid" in body


def test_rehydrate_matches_criteria_by_cid():
    body = _fn_body(_all_js(), "_rehydrateConditions")
    # Restoration queries by the stable data-cid, not solely by positional index.
    assert 'data-cid="' in body


# ── cycle 65 — per-finding conditions (supersedes 245 group aggregation) ──────
def test_conditions_state_is_keyed_per_finding():
    js = _all_js()
    assert "window._findingConditions" in js
    # The old per-group aggregation is gone.
    assert "_aggregateGroupConditions" not in js
    assert "_groupConditions" not in js


def test_export_builds_conditions_per_finding():
    body = _fn_body(_all_js(), "exportDecisions")
    # Each finding entry gets its OWN conditions, not a shared group aggregate.
    assert "buildConditionsStructured(en.fid)" in body


def test_single_finding_awc_scopes_panel_to_that_finding():
    # cycle 65 — single-finding "Approve with conditions" now reveals that
    # finding's own inline conditions dropdown (superseding the earlier
    # shared-panel _panelTargets scoping).
    dui = _fn_body(_all_js(), "_applyDecidedUI")
    assert "revealInlineConditions(fid)" in dui


def test_group_awc_scopes_panel_to_all_members():
    body = _fn_body(_all_js(), "setGroupDecision")
    # The group button still writes conditions to every member finding.
    assert "window._panelTargets[gid]=_groupFids(gid)" in body


def test_single_finding_panel_filters_changes_to_that_finding():
    """cycle 65 — when scoped to one finding, the conditions panel filters the
    change list to that finding's file, keeping each change's ORIGINAL index (so
    change_N targets stay shared for Phase-2 merge_conditions), with a fallback
    to all changes when nothing matches."""
    body = _fn_body(_all_js(), "openConditionsPanel")
    # scoped to a single finding -> read that finding's file and filter changes
    assert "_panelFids(gid)" in body
    assert ".dataset.file" in body
    # original index preserved for the radio target (not re-indexed)
    assert "'chg-'+gid+'-'+i" in body
    # fallback: keep all when no change matches the finding's file
    assert "if(_m.length>0) _visible=_m;" in body


def test_panel_rebuilds_when_scoped_target_changes():
    """The 'keep panel open' guard must rebuild when the scoped finding changes,
    so switching findings re-applies the per-finding change filter."""
    body = _fn_body(_all_js(), "openConditionsPanel")
    assert "dataset.scopedTo===_panelFids(gid).join(',')" in body


def test_inline_conditions_functions_present():
    """cycle 65 — inline per-finding 'Approve with conditions' dropdown: a
    dedicated per-fid writer, a lazy builder with line-proximity scoping, and
    reveal on the decided-UI path."""
    js = _all_js()
    assert "function updateFindingChange" in js
    assert "function buildInlineConditions" in js
    # scoped by line proximity using the row's data-line-range, keeping origin idx
    assert ".dataset.lineRange" in js
    assert "_fcNear" in js
    assert "'chg-'+fid+'-'+i" in js
    # fallback chain proximity -> file -> all, and a mandatory "show all"
    assert "byProx.length?byProx:(byFile.length?byFile:all)" in js
    assert "fc-showall" in js
    # inline writer is fid-scoped, NOT via _panelTargets (avoids group aliasing)
    body = _fn_body(js, "updateFindingChange")
    assert "_panelFids" not in body and "_panelTargets" not in body


def test_awc_reveals_inline_not_shared_panel():
    """Selecting a finding's AWC reveals its inline dropdown (via _applyDecidedUI),
    and no longer opens the shared group panel per-finding."""
    js = _all_js()
    dui = _fn_body(js, "_applyDecidedUI")
    assert "revealInlineConditions(fid)" in dui
    assert "hideInlineConditions(fid)" in dui
    # setFindingDecision must not open the shared panel for a single finding anymore
    sfd = _fn_body(js, "setFindingDecision")
    assert "openConditionsPanel" not in sfd


def test_restore_state_migrates_legacy_group_conditions():
    # Back-compat: an in-progress brief saved under the old group-keyed shape
    # must still restore (migrated onto each member finding).
    body = _fn_body(_all_js(), "_restoreState")
    assert "s.findings" in body and "s.groups" in body


# ── 284 — clear-conditions reconciliation ─────────────────────────────────
def test_clear_group_decision_clears_conditions_state():
    body = _fn_body(_all_js(), "clearGroupDecision")
    assert "clearConditions(gid)" in body


# ── 295 — panel stays open on re-select ───────────────────────────────────
def test_open_panel_keeps_already_open_panel():
    body = _fn_body(_all_js(), "openConditionsPanel")
    # Early-return guard when the panel is already open for this same group.
    assert "_openEl" in body
    assert "!_openEl.hidden" in body
