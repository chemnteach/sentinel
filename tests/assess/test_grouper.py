from sentinel.assess.grouper import _bucket_by_file, _safe_key, SemanticGrouper


def test_bucket_by_file_separates_file_less_findings_by_id():
    """Findings with missing/falsy file must not all collapse into one shared
    "" bucket; each falls back to its own id so they stay separate candidates."""
    findings = [
        {"id": "f1", "description": "one"},
        {"id": "f2", "file": "", "description": "two"},
    ]
    buckets = _bucket_by_file(findings)

    # Two separate buckets keyed by id, not one shared "" bucket.
    assert set(buckets.keys()) == {"f1", "f2"}
    assert all(len(v) == 1 for v in buckets.values())


def test_bucket_by_file_groups_shared_real_file():
    """Findings that share a real file still bucket together."""
    findings = [
        {"id": "f1", "file": "a.py"},
        {"id": "f2", "file": "a.py"},
    ]
    buckets = _bucket_by_file(findings)
    assert list(buckets.keys()) == ["a.py"]
    assert len(buckets["a.py"]) == 2


def test_safe_key_escapes_pipe_in_component():
    # A | inside a value must not split the key
    assert _safe_key("half", "a|b", "c", "") != \
           _safe_key("half", "a", "b|c", "")


def test_safe_key_preserves_delimiter_free_inputs():
    # Inputs without | produce the same key as before
    # (deterministic, no escaping noise)
    assert _safe_key("legacy", "file.py", "fn", "cat") == \
           "legacy|file.py|fn|cat"


def test_half_registry_path_distinct_when_value_contains_pipe():
    # Two findings that would collide under raw | join must land in
    # distinct buckets via the half-registry path. Construct a real
    # grouper with form_empty_group so the unknown finding_kind routes
    # to half-registry (keyed by subject_key) instead of raising.
    g = SemanticGrouper(unknown_finding_kind_policy="form_empty_group")
    f1 = {"file": "src/a.py", "function": "fn",
          "finding_kind": "x|y", "subject_key": "x|y",
          "category": "cat", "severity": "P1",
          "location": "src/a.py:fn", "description": "d"}
    f2 = {"file": "src/a.py", "function": "fn|x",
          "finding_kind": "y", "subject_key": "y",
          "category": "cat", "severity": "P1",
          "location": "src/a.py:fn", "description": "d"}
    _, k1, _ = g._classify_and_key(f1)
    _, k2, _ = g._classify_and_key(f2)
    assert k1 != k2


def test_legacy_path_distinct_when_value_contains_pipe():
    # category contains | — legacy path (no finding_kind/subject_key).
    g = SemanticGrouper(unknown_finding_kind_policy="form_empty_group")
    f1 = {"file": "f.py", "function": "fn",
          "category": "a|b", "severity": "P1",
          "finding_kind": "", "subject_key": "",
          "location": "f.py:fn", "description": "d"}
    f2 = {"file": "f.py", "function": "fn|a",
          "category": "b", "severity": "P1",
          "finding_kind": "", "subject_key": "",
          "location": "f.py:fn", "description": "d"}
    _, k1, _ = g._classify_and_key(f1)
    _, k2, _ = g._classify_and_key(f2)
    assert k1 != k2


def test_grouping_unchanged_for_delimiter_free_inputs():
    assert _safe_key("half", "f.py", "func", "kind") == \
           "half|f.py|func|kind"


# ── FileAuditLog append-safety (FIX-REQUIRED-018) ─────────────────────
import json as _json
import threading as _threading
from pathlib import Path as _Path
from unittest.mock import MagicMock, patch

from sentinel.assess.grouper import FileAuditLog


def test_file_audit_log_append_writes_single_complete_jsonl_record_per_event(tmp_path):
    # The complete line is built in memory and emitted with exactly ONE
    # append write (was two writes: json then newline).
    log = FileAuditLog(tmp_path)
    fake_fh = MagicMock()
    cm = MagicMock()
    cm.__enter__.return_value = fake_fh
    with patch.object(_Path, "open", return_value=cm):
        log._append({"record_type": "half_registry", "finding_id": "F1"})
    assert fake_fh.write.call_count == 1
    written = fake_fh.write.call_args[0][0]
    assert written.endswith("\n")
    assert _json.loads(written.rstrip("\n")) == {
        "record_type": "half_registry", "finding_id": "F1"
    }


def test_file_audit_log_concurrent_appends_produce_parseable_jsonl_records(tmp_path):
    # Concurrent in-process writers must not interleave bytes; every record
    # lands as one complete, parseable JSON object.
    log = FileAuditLog(tmp_path)
    workers, per_worker = 8, 25

    def work(w):
        for k in range(per_worker):
            log._append({"record_type": "half_registry", "w": w, "k": k})

    threads = [_threading.Thread(target=work, args=(w,)) for w in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = log.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == workers * per_worker
    seen = set()
    for ln in lines:
        obj = _json.loads(ln)  # raises on any interleaved/partial record
        assert obj["record_type"] == "half_registry"
        seen.add((obj["w"], obj["k"]))
    assert len(seen) == workers * per_worker  # all records intact, none corrupted


def test_file_audit_log_documents_supported_concurrency_contract():
    # Guarded: a process-wide lock exists at the implementation boundary.
    assert hasattr(FileAuditLog, "_write_lock")
    lock = FileAuditLog._write_lock
    assert hasattr(lock, "acquire") and hasattr(lock, "release")
    # Documented: the class docstring states the concurrency contract.
    assert FileAuditLog.__doc__ is not None
    assert "concurren" in FileAuditLog.__doc__.lower()


def test_file_audit_log_sequential_appends_remain_one_json_object_per_line(tmp_path):
    log = FileAuditLog(tmp_path)
    n = 5
    for i in range(n):
        log._append({"record_type": "malformed_subject_key", "i": i})
    lines = log.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == n
    for i, ln in enumerate(lines):
        obj = _json.loads(ln)
        assert obj["i"] == i
