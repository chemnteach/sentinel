"""Tests for the S4-7 live bundle status UI (Screen 6 /triage + /api/bundles).

Exercises the /api/bundles read endpoint, the POST decision endpoint (which
atomically updates bundles.json + tech_debt.json), and the /triage screen with
its severity sections and resume indicator. Uses a Flask test client over a
small fixture state dir (bundles.json + tech_debt.json).
"""

from __future__ import annotations

import json

import pytest

from sentinel.assess.finding_status import BundleStatus
from sentinel.reports import server as server_mod
from sentinel.reports.server import create_app


class _FakeClient:
    def call_model(self, system_prompt, user_message, temperature=0.2, max_tokens=None):
        class _R:
            content = "stub"
        return _R()


@pytest.fixture
def state_dir(tmp_path):
    d = tmp_path / "self-assessment-020"
    d.mkdir()
    return d


@pytest.fixture
def app(state_dir, monkeypatch):
    monkeypatch.setattr(server_mod, "_get_llm_client", lambda: _FakeClient())
    application = create_app(str(state_dir))
    application.config.update(TESTING=True)
    return application


@pytest.fixture
def client(app):
    return app.test_client()


def _bundle(bundle_id, severity, status, root_fid, members):
    """members: list of (finding_id, severity, title, predicted_resolved)."""
    return {
        "bundle_id": bundle_id,
        "confidence": "HIGH",
        "root_cause_finding_id": root_fid,
        "root_cause_severity": severity,
        "root_cause_title": f"Root cause for {bundle_id}",
        "affected_files": ["src/foo.py"],
        "member_findings": [
            {
                "finding_id": fid,
                "severity": sev,
                "title": title,
                "predicted_resolved": pr,
                "finding_kind": "logic_bug",
            }
            for (fid, sev, title, pr) in members
        ],
        "bundle_status": status,
        "acceptance_criteria": ["Resolve it"],
        "root_cause_file": "src/foo.py",
        "root_cause_line": 42,
    }


def _write_bundles(state_dir, bundles):
    (state_dir / "bundles.json").write_text(json.dumps(bundles), encoding="utf-8")


def _write_tech_debt(state_dir, items):
    (state_dir / "tech_debt.json").write_text(json.dumps(items), encoding="utf-8")


def _default_state(state_dir):
    bundles = [
        _bundle("BUNDLE-001", "Critical", BundleStatus.COMPLETE, "F1",
                [("F1", "Critical", "Crit root", True),
                 ("F2", "P2", "Cascade member", True)]),
        _bundle("BUNDLE-002", "P1", BundleStatus.UNDECIDED, "F3",
                [("F3", "P1", "P1 root", True)]),
        _bundle("BUNDLE-003", "P3", BundleStatus.UNDECIDED, "F4",
                [("F4", "P3", "P3 root", True)]),
    ]
    _write_bundles(state_dir, bundles)
    _write_tech_debt(state_dir, [
        {"finding_id": "F1", "human_triage": "APPROVE"},
        {"finding_id": "F2", "human_triage": "APPROVE"},
        {"finding_id": "F3", "human_triage": None},
        {"finding_id": "F4", "human_triage": None},
    ])


# -- GET /api/bundles ---------------------------------------------------

def test_api_bundles_returns_bundles_and_summary(client, state_dir):
    _default_state(state_dir)
    r = client.get("/api/bundles")
    assert r.status_code == 200
    data = r.get_json()
    assert len(data["bundles"]) == 3
    s = data["summary"]
    assert s["total"] == 3
    assert s["complete"] == 1
    assert s["undecided"] == 2
    # every status key present
    for key in ("approved", "in_progress", "in_review", "reverted"):
        assert key in s


def test_api_bundles_empty_when_no_file(client):
    data = client.get("/api/bundles").get_json()
    assert data["bundles"] == []
    assert data["summary"]["total"] == 0
    assert data["completion_ready"] is False


def test_api_bundles_completion_ready_false_when_p1_undecided(client, state_dir):
    _default_state(state_dir)
    # BUNDLE-002 (P1) is UNDECIDED -> not ready.
    assert client.get("/api/bundles").get_json()["completion_ready"] is False


def test_api_bundles_completion_ready_true_when_all_crit_p1_complete(client, state_dir):
    bundles = [
        _bundle("BUNDLE-001", "Critical", BundleStatus.COMPLETE, "F1",
                [("F1", "Critical", "Crit root", True)]),
        _bundle("BUNDLE-002", "P1", BundleStatus.COMPLETE, "F3",
                [("F3", "P1", "P1 root", True)]),
        # A P3 bundle left undecided must NOT block completion.
        _bundle("BUNDLE-003", "P3", BundleStatus.UNDECIDED, "F4",
                [("F4", "P3", "P3 root", True)]),
    ]
    _write_bundles(state_dir, bundles)
    assert client.get("/api/bundles").get_json()["completion_ready"] is True


# -- POST /api/bundles/<id>/decision ------------------------------------

def test_decision_approve_updates_bundle_status(client, state_dir):
    _default_state(state_dir)
    r = client.post("/api/bundles/BUNDLE-002/decision", json={"decision": "APPROVED"})
    assert r.status_code == 200
    assert r.get_json()["bundle_status"] == BundleStatus.APPROVED
    # persisted
    saved = json.loads((state_dir / "bundles.json").read_text(encoding="utf-8"))
    b2 = next(b for b in saved if b["bundle_id"] == "BUNDLE-002")
    assert b2["bundle_status"] == BundleStatus.APPROVED


def test_decision_defer_sets_undecided(client, state_dir):
    _default_state(state_dir)
    # First approve, then defer it back.
    client.post("/api/bundles/BUNDLE-002/decision", json={"decision": "APPROVED"})
    r = client.post("/api/bundles/BUNDLE-002/decision", json={"decision": "DEFERRED"})
    assert r.status_code == 200
    saved = json.loads((state_dir / "bundles.json").read_text(encoding="utf-8"))
    b2 = next(b for b in saved if b["bundle_id"] == "BUNDLE-002")
    assert b2["bundle_status"] == BundleStatus.UNDECIDED


def test_decision_excluded_findings_update_tech_debt(client, state_dir):
    _default_state(state_dir)
    r = client.post(
        "/api/bundles/BUNDLE-001/decision",
        json={"decision": "APPROVED", "excluded_finding_ids": ["F2"]},
    )
    assert r.status_code == 200
    td = json.loads((state_dir / "tech_debt.json").read_text(encoding="utf-8"))
    by_fid = {item["finding_id"]: item for item in td}
    assert by_fid["F2"]["human_triage"] == "DEFER"
    # Non-excluded member of an approved bundle is set to APPROVE.
    assert by_fid["F1"]["human_triage"] == "APPROVE"


def test_decision_unknown_bundle_returns_404(client, state_dir):
    _default_state(state_dir)
    r = client.post("/api/bundles/BUNDLE-999/decision", json={"decision": "APPROVED"})
    assert r.status_code == 404


# -- GET /triage --------------------------------------------------------

def test_triage_screen_renders_severity_sections(client, state_dir):
    _default_state(state_dir)
    r = client.get("/triage")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Critical" in body
    assert "P1" in body
    assert "Improvement opportunities" in body   # P2/P3 collapsed section
    # root cause title + also-resolves line rendered
    assert "Root cause for BUNDLE-001" in body
    assert "Also resolves" in body


def test_triage_header_shows_counts(client, state_dir):
    _default_state(state_dir)
    body = client.get("/triage").get_data(as_text=True)
    # "N bundles · N complete · N undecided"
    assert "3 bundles" in body
    assert "1 complete" in body
    assert "2 undecided" in body


def test_triage_resume_indicator_n_of_n(client, state_dir):
    _default_state(state_dir)
    body = client.get("/triage").get_data(as_text=True)
    # 1 of 3 bundles decided (BUNDLE-001 COMPLETE counts as decided).
    assert "Resume: 1 of 3 decided" in body


def test_triage_in_nav(client, state_dir):
    _default_state(state_dir)
    body = client.get("/triage").get_data(as_text=True)
    assert "/triage" in body
