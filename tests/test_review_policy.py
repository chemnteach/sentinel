from __future__ import annotations

from sentinel.govern.review_policy import classify_paths, parse_status_porcelain


def test_classify_paths_routes_code_to_review_required() -> None:
    buckets = classify_paths(["sentinel/cli.py", "tests/test_cli.py"])

    assert "sentinel/cli.py" in buckets.review_required
    assert "tests/test_cli.py" in buckets.review_required
    assert buckets.auto_accept == []


def test_classify_paths_routes_state_and_docs_to_auto_accept() -> None:
    buckets = classify_paths(
        [
            "sentinel/state/tasks.json",
            "README.md",
            "docs/runbook.md",
            ".env.example",
        ]
    )

    assert buckets.review_required == []
    assert "sentinel/state/tasks.json" in buckets.auto_accept
    assert "README.md" in buckets.auto_accept
    assert "docs/runbook.md" in buckets.auto_accept
    assert ".env.example" in buckets.auto_accept


def test_classify_paths_defaults_unknown_to_review_required() -> None:
    buckets = classify_paths(["pyproject.toml"])
    assert buckets.review_required == ["pyproject.toml"]
    assert buckets.auto_accept == []


def test_parse_status_porcelain_handles_modified_untracked_and_rename() -> None:
    stdout = " M sentinel/cli.py\n?? tests/test_new.py\nR  old/name.py -> sentinel/new_name.py\n"
    parsed = parse_status_porcelain(stdout)

    assert parsed == [
        "sentinel/cli.py",
        "tests/test_new.py",
        "sentinel/new_name.py",
    ]
