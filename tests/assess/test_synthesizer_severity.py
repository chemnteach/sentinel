from sentinel.assess.synthesizer import Synthesizer

def _make_synthesizer():
    from unittest.mock import MagicMock
    s = Synthesizer.__new__(Synthesizer)
    return s

def _findings(*severities):
    return [{"severity": s} for s in severities]

def test_rank_modules_orders_p1_above_p2_above_p3():
    s = _make_synthesizer()
    result = s._rank_modules({
        "mod_p3": _findings("P3"),
        "mod_p1": _findings("P1"),
        "mod_p2": _findings("P2"),
    })
    names = [r["module"] for r in result]
    assert names.index("mod_p1") < names.index("mod_p2")
    assert names.index("mod_p2") < names.index("mod_p3")

def test_rank_modules_unknown_severity_uses_highest_risk():
    s = _make_synthesizer()
    result = s._rank_modules({
        "mod_unknown": _findings("P0"),
        "mod_p3": _findings("P3"),
    })
    names = [r["module"] for r in result]
    assert names[0] == "mod_unknown"

def test_rank_modules_unknown_not_equivalent_to_p3():
    s = _make_synthesizer()
    result = s._rank_modules({
        "mod_unknown": _findings("INVALID"),
        "mod_p3": _findings("P3"),
    })
    scores = {r["module"]: r["risk_score"] for r in result}
    assert scores["mod_unknown"] != scores["mod_p3"]

def test_rank_modules_invalid_severity_explicit_not_silent():
    s = _make_synthesizer()
    result = s._rank_modules({
        "mod_bad": _findings("garbage"),
        "mod_p1": _findings("P1"),
    })
    scores = {r["module"]: r["risk_score"] for r in result}
    assert scores["mod_bad"] >= scores["mod_p1"]

def test_rank_modules_docstring_documents_policy():
    from sentinel.assess.synthesizer import Synthesizer
    doc = Synthesizer._rank_modules.__doc__ or ""
    assert "Critical" in doc
    assert "unrecognized" in doc.lower() or \
        "unknown" in doc.lower()
