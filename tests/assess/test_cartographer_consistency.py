"""Regression coverage for Cartographer.run keeping the returned manifest's
top-level `modules` consistent with what is persisted to state (FIX-REQUIRED-006).
"""
from unittest.mock import MagicMock

from sentinel.assess.cartographer import Cartographer


def _make_config():
    config = MagicMock()
    config.strict_quality_mode = False
    config.local_only_mode = False
    config.chunk_max_lines = 300
    config.chunk_overlap_lines = 80
    return config


def _make_cartographer(manifest):
    """Build a Cartographer whose model call returns ``manifest`` and whose
    local-scan helpers are stubbed out so run() exercises only the
    recovery/persist/return path."""
    architect = MagicMock()
    architect.map_codebase.return_value = manifest
    store = MagicMock()
    carto = Cartographer(architect, store, _make_config())
    carto._build_file_tree = MagicMock(return_value="")
    carto._collect_samples = MagicMock(return_value={})
    carto._count_loc = MagicMock(return_value={})
    return carto, store


def _persisted_modules(store):
    """The last value passed to store.write('modules', <value>, ...)."""
    writes = [
        c for c in store.write.call_args_list
        if c.args and c.args[0] == "modules"
    ]
    assert writes, "expected a store.write('modules', ...) call"
    return writes[-1].args[1]


def test_run_returns_recovered_modules_and_persists_identical_modules():
    recovered = [{"name": "mod_a", "path": "a.py"}, {"name": "mod_b", "path": "b.py"}]
    manifest = {
        "repositories": [{"name": "repo1", "modules": recovered}],
        "modules": [],
    }
    carto, store = _make_cartographer(manifest)

    returned = carto.run([])

    assert returned["modules"] == recovered
    assert _persisted_modules(store) == recovered
    assert returned["modules"] == _persisted_modules(store)


def test_run_replaces_empty_or_stale_top_level_modules_with_recovered():
    recovered = [{"name": "real_mod", "path": "r.py"}]
    manifest = {
        "repositories": [{"name": "repo1", "modules": recovered}],
        "modules": [],  # empty/stale top-level
    }
    carto, store = _make_cartographer(manifest)

    returned = carto.run([])

    assert returned["modules"] == recovered
    assert _persisted_modules(store) == recovered


def test_run_preserves_original_modules_when_recovery_finds_nothing():
    existing = [{"name": "top_mod", "path": "t.py"}]
    manifest = {
        "repositories": [{"name": "repo1", "modules": []}],
        "modules": existing,  # already populated → no recovery
    }
    carto, store = _make_cartographer(manifest)

    returned = carto.run([])

    assert returned["modules"] == existing
    assert _persisted_modules(store) == existing


def test_run_returned_and_persisted_modules_do_not_diverge_regression():
    recovered = [{"name": "m1"}, {"name": "m2"}]
    manifest = {
        "repositories": [{"name": "repo1", "modules": recovered}],
        "modules": [],
    }
    carto, store = _make_cartographer(manifest)

    returned = carto.run([])

    # Any divergence between returned and persisted module views fails.
    assert returned["modules"] == _persisted_modules(store)
