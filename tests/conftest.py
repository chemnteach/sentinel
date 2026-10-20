"""Shared test fixtures and helpers for the Sentinel test suite.

Cycle 16 (Item 21) consolidation: ``advisory_config()`` was previously
duplicated across five workflow-track test files (per the cycle-12 D4
"duplicated, not imported" convention). The fixture is now centralized
here as a plain function (not a pytest ``@fixture``) because all call
sites invoke it as a bare callable rather than as a fixture parameter;
keeping it a function minimizes call-site churn relative to a fixture
conversion.

Imported in test files as::

	from tests.conftest import advisory_config
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from sentinel.workflow.config import (
	GitHubConfig,
	ProfileConfig,
	RemediationConfig,
	ThemesConfig,
	WorkflowConfig,
)


def advisory_config() -> WorkflowConfig:
	"""Return a ``solo_local_advisory`` config with ``transitions=None``.

	Used by tests that exercise the advisory-profile error path in
	workflow handlers: any handler that threads this config in must
	raise :class:`sentinel.workflow.config.WorkflowConfigError` because
	advisory profiles declare no workflow transitions per spec §1.6.2.
	"""
	return WorkflowConfig(
		github=GitHubConfig(owner="o", repo="r"),
		themes=ThemesConfig(),
		remediation=RemediationConfig(),
		profile=ProfileConfig(
			name="solo_local_advisory",
			version="0.8.0",
			transitions=None,
		),
	)


def write_semantic_groups(
	state_dir: Path,
	groups: Iterable,
) -> None:
	"""Write semantic groups to StateStore for test fixture setup.

	Canonical replacement for the deprecated ``write_groups`` function
	(removed in cycle 21 sub-step 3). Uses ``StateStore.write(
	"semantic_groups", ..., sort_keys=True)`` to match the production
	write path established in cycle 20 (Stage 4b orchestrator).

	The ``groups`` parameter is typed ``Iterable`` (not
	``Iterable[SemanticGroup]``) to avoid forcing every conftest-importing
	test file to also import ``SemanticGroup`` from the assess track;
	the runtime call to ``serialize_groups`` performs the shape check.
	"""
	from sentinel.assess.grouper import serialize_groups
	from sentinel.orchestrator.state_store import StateStore

	StateStore(state_dir).write(
		"semantic_groups",
		serialize_groups(groups),
		sort_keys=True,
	)



import pytest as _pytest


@_pytest.fixture(autouse=True)
def _default_public_provider(monkeypatch: _pytest.MonkeyPatch) -> None:
    """Give every test a credential-free provider (copilot-cli) so code paths
    that build a model client behave as they would with a configured provider.
    Tests that pass an explicit ``env`` mapping to ``Config`` are unaffected."""
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "SENTINEL_ALLOW_GITHUB_MODELS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SENTINEL_PROVIDER", "copilot-cli")
