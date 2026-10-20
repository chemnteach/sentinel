"""Repo-level workflow configuration with profile-based overrides.

Loads ``/.sentinel/config.toml`` (TOML, not YAML) per spec section 10.1
field schema. The spec example uses YAML; this implementation reads the
same field names from TOML so the workflow track can use the Python
3.11+ stdlib ``tomllib`` without taking a PyYAML dependency. PyYAML is
not currently in the Sentinel dep stack (verified against
``pyproject.toml`` and a workspace grep), and the cycle 7 design scope
locked the no-new-dependency constraint. Future cycles can add PyYAML
support alongside TOML if profiles materialize that require it.

The ``skills`` section described in spec section 10.1 is intentionally
deferred in cycle 7: cycle 7 reads only the ``github``, ``themes``,
``remediation``, and ``profile`` sections. Skills configuration is a
cross-track integration (assess + workflow) and belongs to a future
cycle.
"""

from __future__ import annotations

import tomllib
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# Profile identifiers reserved in spec section 1.5. Cycle 7 ships only
# the ``owner_gate`` profile defaults; the other names are recognized
# (no validation error on selection) but their profile contracts are
# defined by future cycles.
_KNOWN_PROFILES: frozenset[str] = frozenset({
	"owner_gate",
	"solo_local_advisory",
	"solo_local_remediating",
	"artifact_pipeline",
})


# Spec section 1.6.1: concrete state label set for the owner_gate profile.
_OWNER_GATE_STATE_LABELS: frozenset[str] = frozenset({
	"triage",
	"owner-assessed",
	"owner-approved",
	"remediating",
	"pr-open",
	"wont-fix",
	"deferred",
	"closed",
})


# Public re-export of ``_OWNER_GATE_STATE_LABELS`` for cross-module
# consumers (e.g. the dashboard builder). The underscored name remains
# the canonical definition; this alias avoids forcing importers to bend
# the leading-underscore convention while preserving the single source
# of truth.
OWNER_GATE_STATE_LABELS = _OWNER_GATE_STATE_LABELS


class WorkflowConfigError(ValueError):
	"""Raised when a workflow configuration file is missing required keys
	or names an unknown profile.

	Also raised by :func:`_resolve_transitions` when a caller threads a
	``WorkflowConfig`` whose profile declares no workflow transitions
	(``profile.transitions is None``) into a code path that requires
	them. Per spec section 1.6.2, advisory profiles publish no
	persistent workflow states; Phase 4 handlers and item-16-wired
	reconcile/PR-feedback paths cannot operate on them. Placement is
	co-located with config so the canonical error type travels with the
	canonical config surface; no separate ``errors.py`` module is
	introduced this cycle.
	"""


@dataclass(frozen=True, slots=True)
class TransitionStateConfig:
	"""Profile-portable workflow transition vocabulary per spec section 7.1.

	Maps the seven foundation transition concepts (triage_state,
	accepted_states, rejected_states, approval_state, remediation_state,
	review_state, closed_state) onto a profile's concrete state labels.
	The owner_gate profile populates this with its eight-label vocabulary
	(spec section 1.6.1); other profiles substitute their own.

	Naming convention: singular ``_state`` for fields that name a single
	canonical label (the cardinality is one) and plural ``_states`` for
	fields that name a set of labels (cardinality is many). The
	suffix-mixed naming is intentional and signals cardinality at the
	read site.

	A ``None`` value on ``ProfileConfig.transitions`` indicates an
	advisory profile per spec section 1.6.2 ("No persistent workflow
	states are claimed"). Callers reaching :func:`_resolve_transitions`
	with an advisory profile receive a :class:`WorkflowConfigError`
	rather than silent fall-through, because the foundation
	transitions are not meaningful in advisory mode.

	Foundation mapping:
	- ``triage_state``: initial entry state for new findings.
	- ``accepted_states``: states reachable along the accepted lane
	  (post-triage progression through review/remediation/closure).
	- ``rejected_states``: terminal states on the rejected lane
	  (wont-fix / deferred / equivalent).
	- ``approval_state``: the canonical "owner-or-approver accepted"
	  state; the destination for full-PR rejections per spec
	  section 8.2.
	- ``remediation_state``: the canonical "work in progress" state;
	  the destination for per-commit rejections.
	- ``review_state``: the canonical "code under review" state; the
	  source state for PR-feedback transitions per spec section 8.2.
	- ``closed_state``: the canonical terminal-accepted state; the
	  destination for successful PR merges.
	- ``rejection_label`` / ``deferral_label`` (cycle 11 Commit 6,
	  Q1 lock extension): disposition discriminators for finding-
	  level rejection (Phase 4). ``rejected_states`` is a set with
	  no intrinsic ordering; these two fields name *which* member
	  carries the "rejection" semantic vs. the "deferral" semantic.
	  Under ``owner_gate`` these are ``"wont-fix"`` and ``"deferred"``;
	  under ``solo_local_remediating`` they are ``"rejected"`` and
	  ``"deferred"``. The ``__post_init__`` invariant
	  ``{rejection_label, deferral_label} <= rejected_states`` is
	  enforced at construction time so handler code never sees an
	  inconsistent configuration.

	Q1 lock framing (cycle 11 Commit 6): Commit 1 locked the seven
	foundation fields above as the required shape. Commit 6
	reconnaissance for ``handle_reject_finding`` surfaced the
	disposition discriminator need (Phase 4's reject-finding /
	wont-fix / defer flow is a single foundation transition
	parameterized by which ``rejected_states`` member applies). The
	Q1 lock extends from "the locked shape" to "the locked starting
	shape; implementation-grounded need may extend with surfaced
	rationale." Recorded as a methodology candidate for cycle 11
	closure (Family-2 framing-extension instance).
	"""

	triage_state: str
	accepted_states: frozenset[str]
	rejected_states: frozenset[str]
	rejection_label: str
	deferral_label: str
	approval_state: str
	remediation_state: str
	review_state: str
	closed_state: str

	def __post_init__(self) -> None:
		# Disposition-discriminator invariant: both labels must be
		# valid members of ``rejected_states`` so the handler-layer
		# mapping always lands on a real state. Fails fast at
		# construction time; the handler never sees inconsistent
		# config.
		missing = {self.rejection_label, self.deferral_label} - self.rejected_states
		if missing:
			raise WorkflowConfigError(
				"rejection_label and deferral_label must be members of "
				f"rejected_states; missing: {sorted(missing)} "
				f"(rejected_states={sorted(self.rejected_states)})"
			)

	def to_dict(self) -> dict[str, Any]:
		return {
			"triage_state": self.triage_state,
			"accepted_states": sorted(self.accepted_states),
			"rejected_states": sorted(self.rejected_states),
			"rejection_label": self.rejection_label,
			"deferral_label": self.deferral_label,
			"approval_state": self.approval_state,
			"remediation_state": self.remediation_state,
			"review_state": self.review_state,
			"closed_state": self.closed_state,
		}


# Format-compatibility pin: these field values must match the hardcoded
# state strings in ``sentinel/workflow/reconcile_issues.py`` and
# ``sentinel/workflow/pr_feedback_handler.py``. Commits 3-4 of cycle 11
# rewire those modules to read from this constant; the test
# ``test_owner_gate_transitions_default`` is the contract anchor.
_OWNER_GATE_TRANSITIONS: TransitionStateConfig = TransitionStateConfig(
	triage_state="triage",
	accepted_states=frozenset({
		"owner-assessed",
		"owner-approved",
		"remediating",
		"pr-open",
		"closed",
	}),
	rejected_states=frozenset({"wont-fix", "deferred"}),
	rejection_label="wont-fix",
	deferral_label="deferred",
	approval_state="owner-approved",
	remediation_state="remediating",
	review_state="pr-open",
	closed_state="closed",
)


# Spec section 1.6.3: concrete state label set for the
# ``solo_local_remediating`` profile. The core labels are ``triage``,
# ``accepted``, ``rejected``, ``deferred``, ``approved``, ``remediating``,
# ``review``, and ``closed``. Per §1.6.3, GitHub-based runs may use
# display labels such as ``wont-fix`` for the foundation ``rejected``
# concept; that is a presentation variant and is not in the core set.
_SOLO_LOCAL_REMEDIATING_STATE_LABELS: frozenset[str] = frozenset({
	"triage",
	"accepted",
	"rejected",
	"deferred",
	"approved",
	"remediating",
	"review",
	"closed",
})


# Canonical-approved synthesis (cycle 11, Q2 design lock):
# Spec §1.6.3 lists the approval state as "approved or authorized"
# without an explicit canonical designation. Cycle 11 chose ``approved``
# as the canonical label for three reasons:
#   1. "approved" aligns with the spec's own ordering ("approved or
#      authorized") and with the foundation §7.1 concept naming.
#   2. "approved" is the more profile-neutral term; "authorized" carries
#      a stronger gate-bearing connotation that overlaps the §2 Approval
#      Gates language and risks confusion in dual-lens reporting.
#   3. The owner_gate parallel uses ``owner-approved``; keeping the
#      ``approved`` stem in the solo_local_remediating profile maintains
#      the cross-profile naming coherence at the foundation-concept level.
# This is recorded as a Family-1 spec-divergence sub-instance per the
# cycle 11 methodology candidates list (same family as cycle 5 yaml->toml
# and cycle 6 band-label gap-fills). Future cycles or profile authors who
# need ``authorized`` semantics can introduce a profile variant or a
# display-label translation; the foundation transition contract uses
# ``approved`` as canonical.
#
# Format-compatibility pin parallel to _OWNER_GATE_TRANSITIONS:
# ``test_solo_local_remediating_transitions_default`` is the §1.6.3
# contract anchor. Commits 5-7 (Phase 4 handlers) parametrize over both
# profiles; commit 8 demonstrates both end-to-end.
_SOLO_LOCAL_REMEDIATING_TRANSITIONS: TransitionStateConfig = TransitionStateConfig(
	triage_state="triage",
	accepted_states=frozenset({
		"accepted",
		"approved",
		"remediating",
		"review",
		"closed",
	}),
	rejected_states=frozenset({"rejected", "deferred"}),
	rejection_label="rejected",
	deferral_label="deferred",
	approval_state="approved",
	remediation_state="remediating",
	review_state="review",
	closed_state="closed",
)


@dataclass(frozen=True, slots=True)
class GitHubConfig:
	"""GitHub workflow store coordinates per spec section 10.1 ``github``."""

	owner: str
	repo: str
	default_assignee: str = ""
	approver_team: str = ""

	def to_dict(self) -> dict[str, Any]:
		return {
			"owner": self.owner,
			"repo": self.repo,
			"default_assignee": self.default_assignee,
			"approver_team": self.approver_team,
		}


@dataclass(frozen=True, slots=True)
class HdbscanConfig:
	"""HDBSCAN clustering parameters per spec section 10.1
	``themes.conceptual_lens.hdbscan``."""

	min_cluster_size: int = 3
	min_samples: int = 2
	cluster_selection_epsilon: float = 0.15

	def to_dict(self) -> dict[str, Any]:
		return {
			"min_cluster_size": self.min_cluster_size,
			"min_samples": self.min_samples,
			"cluster_selection_epsilon": self.cluster_selection_epsilon,
		}


@dataclass(frozen=True, slots=True)
class ConceptualLensConfig:
	"""Conceptual lens parameters per spec section 10.1
	``themes.conceptual_lens``."""

	enabled: bool = True
	embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
	embedding_seed: int = 42
	hdbscan: HdbscanConfig = field(default_factory=HdbscanConfig)
	stability_jaccard_threshold: float = 0.7
	title_strategy: str = "synthesized"

	def to_dict(self) -> dict[str, Any]:
		return {
			"enabled": self.enabled,
			"embedding_model": self.embedding_model,
			"embedding_seed": self.embedding_seed,
			"hdbscan": self.hdbscan.to_dict(),
			"stability_jaccard_threshold": self.stability_jaccard_threshold,
			"title_strategy": self.title_strategy,
		}


@dataclass(frozen=True, slots=True)
class StructuralLensConfig:
	"""Structural lens parameters per spec section 10.1
	``themes.structural_lens``."""

	source: str = "root_cause_id"
	title_strategy: str = "synthesized"

	def to_dict(self) -> dict[str, Any]:
		return {
			"source": self.source,
			"title_strategy": self.title_strategy,
		}


@dataclass(frozen=True, slots=True)
class ThemesConfig:
	"""Themes section per spec section 10.1 ``themes`` (cycle 18: full spec §10.1 schema)."""

	primary_lens: str = "conceptual"
	structural_lens: StructuralLensConfig = field(default_factory=StructuralLensConfig)
	conceptual_lens: ConceptualLensConfig = field(default_factory=ConceptualLensConfig)

	def to_dict(self) -> dict[str, Any]:
		return {
			"primary_lens": self.primary_lens,
			"structural_lens": self.structural_lens.to_dict(),
			"conceptual_lens": self.conceptual_lens.to_dict(),
		}


@dataclass(frozen=True, slots=True)
class RemediationConfig:
	"""Remediation section per spec section 10.1 ``remediation``."""

	max_open_prs: int = 5
	pr_max_files: int = 15
	pr_max_loc: int = 500
	pilot_gate_enabled: bool = True
	pilot_auto_relax_after_n_clean_merges: int = 3
	require_tests_green: bool = True

	def to_dict(self) -> dict[str, Any]:
		return {
			"max_open_prs": self.max_open_prs,
			"pr_max_files": self.pr_max_files,
			"pr_max_loc": self.pr_max_loc,
			"pilot_gate_enabled": self.pilot_gate_enabled,
			"pilot_auto_relax_after_n_clean_merges": self.pilot_auto_relax_after_n_clean_merges,
			"require_tests_green": self.require_tests_green,
		}


@dataclass(frozen=True, slots=True)
class DashboardConfig:
	"""Dashboard / artifact-publishing surface per cycle-16 Item 22.

	Captures the routing and presentation defaults for the Sentinel
	assessment dashboard. ``primary_host`` and ``mirror_host`` name the
	primary and secondary publication targets; ``output_root`` is the
	in-repo path for dashboard artifacts; the remaining fields supply
	SharePoint/Power-Automate wiring and the audience identifier used
	by downstream renderers. All fields default to the spec-example
	values so unconfigured runs match the documented behavior.
	"""

	primary_host: str = "sharepoint"
	mirror_host: str = "github_branch"
	branch: str = "dashboard"
	output_root: str = "dashboard/"
	sharepoint_page_url: str = ""
	power_automate_flow: str = ""
	latest_scan_issue: str = "Latest Sentinel scan"
	audience: str = "owner_approver"

	def to_dict(self) -> dict[str, Any]:
		return {
			"primary_host": self.primary_host,
			"mirror_host": self.mirror_host,
			"branch": self.branch,
			"output_root": self.output_root,
			"sharepoint_page_url": self.sharepoint_page_url,
			"power_automate_flow": self.power_automate_flow,
			"latest_scan_issue": self.latest_scan_issue,
			"audience": self.audience,
		}


@dataclass(frozen=True, slots=True)
class ProfileConfig:
	"""Active profile contract per spec section 1.6.

	``override_label`` is the Gap B coupling site: ``reconcile_issues``
	and ``emit_to_github`` planners default to ``human-override`` but
	read this value when a ``WorkflowConfig`` is threaded in.
	``state_labels`` and ``auto_label`` carry the cycle 7 owner_gate
	defaults; future profiles will substitute their own.

	``transitions`` (cycle 11, Q1 lock) carries the profile-portable
	mapping of foundation transition concepts to concrete state
	labels per :class:`TransitionStateConfig`. The field defaults to
	``None`` so existing call sites that construct ``ProfileConfig``
	without it remain valid; ``None`` is also the advisory-profile
	sentinel per spec section 1.6.2 and is treated as such by
	:func:`_resolve_transitions`.
	"""

	name: str
	version: str
	override_label: str = "human-override"
	state_labels: frozenset[str] = field(default_factory=frozenset)
	auto_label: str = "sentinel-auto"
	transitions: TransitionStateConfig | None = None

	def to_dict(self) -> dict[str, Any]:
		return {
			"name": self.name,
			"version": self.version,
			"override_label": self.override_label,
			"state_labels": sorted(self.state_labels),
			"auto_label": self.auto_label,
			"transitions": (
				self.transitions.to_dict() if self.transitions is not None else None
			),
		}


@dataclass(frozen=True, slots=True)
class WorkflowConfig:
	"""Top-level workflow configuration carrying all section dataclasses."""

	github: GitHubConfig
	themes: ThemesConfig
	remediation: RemediationConfig
	profile: ProfileConfig
	dashboard: DashboardConfig = field(default_factory=DashboardConfig)

	def to_dict(self) -> dict[str, Any]:
		return {
			"github": self.github.to_dict(),
			"themes": self.themes.to_dict(),
			"remediation": self.remediation.to_dict(),
			"dashboard": self.dashboard.to_dict(),
			"profile": self.profile.to_dict(),
		}

	@classmethod
	def default(cls, profile_name: str = "owner_gate") -> "WorkflowConfig":
		"""Return a complete config using only profile defaults.

		Used for unconfigured runs per spec section 1.4 backward-compat:
		an unconfigured v0.8.0 run defaults to ``owner_gate`` for one
		release cycle. Tests that do not supply a YAML/TOML file use
		this constructor.
		"""
		if profile_name not in _KNOWN_PROFILES:
			raise WorkflowConfigError(
				f"unknown profile {profile_name!r}; known: {sorted(_KNOWN_PROFILES)}"
			)
		profile = _default_profile(profile_name)
		return cls(
			github=GitHubConfig(owner="", repo=""),
			themes=ThemesConfig(),
			remediation=RemediationConfig(),
			dashboard=DashboardConfig(),
			profile=profile,
		)


def load_workflow_config(path: Path) -> WorkflowConfig:
	"""Load a ``WorkflowConfig`` from a TOML file at ``path``.

	The TOML schema mirrors spec section 10.1's YAML schema field for
	field; the ``skills`` table is silently ignored per cycle 7 scope.
	Raises ``WorkflowConfigError`` on missing required keys or on an
	unknown profile name.
	"""
	with path.open("rb") as handle:
		data = tomllib.load(handle)
	return _build_config_from_dict(data)


def load_workflow_config_yaml(path: Path) -> WorkflowConfig:
	"""Load a ``WorkflowConfig`` from a YAML file at ``path``.

	The YAML schema mirrors the TOML schema field for field per spec
	section 10.1. Requires PyYAML (install via the ``[workflow]``
	extras group). Raises ``WorkflowConfigError`` on missing required
	keys, unknown profiles, or when PyYAML is not installed.
	"""
	try:
		import yaml
	except ImportError:
		raise WorkflowConfigError(
			"PyYAML is required to load YAML workflow configs; "
			"install with 'pip install -e .[workflow]' or pass "
			"a TOML path to load_workflow_config instead"
		) from None
	with path.open("r", encoding="utf-8") as handle:
		try:
			data = yaml.safe_load(handle)
		except yaml.YAMLError as exc:
			# Surface malformed YAML as a WorkflowConfigError with the config
			# path and parse detail, rather than leaking a raw parser
			# exception. (FIX-DESIRED-004)
			raise WorkflowConfigError(
				f"malformed YAML in workflow config '{path}': {exc}"
			) from exc
	if data is None:
		data = {}
	return _build_config_from_dict(data)


def _build_config_from_dict(data: dict[str, Any]) -> WorkflowConfig:
	"""Build a ``WorkflowConfig`` from an already-parsed mapping.

	Shared post-parse builder for both the TOML loader
	(:func:`load_workflow_config`) and the YAML loader
	(:func:`load_workflow_config_yaml`). Extracted in cycle 14 to
	support the additive YAML-frontend shape (Item 9): both loaders
	parse their respective source format into a ``dict[str, Any]``
	and delegate construction here. Private (leading underscore,
	not in ``__all__``); callers should use one of the public
	format-specific loaders.
	"""
	github_section = _require_section(data, "github")
	github = GitHubConfig(
		owner=_require_string(github_section, "owner", "github"),
		repo=_require_string(github_section, "repo", "github"),
		default_assignee=str(github_section.get("default_assignee", "")),
		approver_team=str(github_section.get("approver_team", "")),
	)
	themes_section = data.get("themes", {})
	structural_section = themes_section.get("structural_lens", {})
	conceptual_section = themes_section.get("conceptual_lens", {})
	hdbscan_section = conceptual_section.get("hdbscan", {})
	structural_lens = StructuralLensConfig(
		source=str(structural_section.get("source", "root_cause_id")),
		title_strategy=str(structural_section.get("title_strategy", "synthesized")),
	)
	hdbscan = HdbscanConfig(
		min_cluster_size=int(hdbscan_section.get("min_cluster_size", 3)),
		min_samples=int(hdbscan_section.get("min_samples", 2)),
		cluster_selection_epsilon=float(
			hdbscan_section.get("cluster_selection_epsilon", 0.15)
		),
	)
	conceptual_lens = ConceptualLensConfig(
		enabled=bool(conceptual_section.get("enabled", True)),
		embedding_model=str(
			conceptual_section.get(
				"embedding_model", "sentence-transformers/all-MiniLM-L6-v2"
			)
		),
		embedding_seed=int(conceptual_section.get("embedding_seed", 42)),
		hdbscan=hdbscan,
		stability_jaccard_threshold=float(
			conceptual_section.get("stability_jaccard_threshold", 0.7)
		),
		title_strategy=str(conceptual_section.get("title_strategy", "synthesized")),
	)
	themes = ThemesConfig(
		primary_lens=str(themes_section.get("primary_lens", "conceptual")),
		structural_lens=structural_lens,
		conceptual_lens=conceptual_lens,
	)
	remediation_section = data.get("remediation", {})
	remediation = RemediationConfig(
		max_open_prs=int(remediation_section.get("max_open_prs", 5)),
		pr_max_files=int(remediation_section.get("pr_max_files", 15)),
		pr_max_loc=int(remediation_section.get("pr_max_loc", 500)),
		pilot_gate_enabled=bool(remediation_section.get("pilot_gate_enabled", True)),
		pilot_auto_relax_after_n_clean_merges=int(
			remediation_section.get("pilot_auto_relax_after_n_clean_merges", 3)
		),
		require_tests_green=bool(remediation_section.get("require_tests_green", True)),
	)
	dashboard_section = data.get("dashboard", {})
	dashboard = DashboardConfig(
		primary_host=str(dashboard_section.get("primary_host", "sharepoint")),
		mirror_host=str(dashboard_section.get("mirror_host", "github_branch")),
		branch=str(dashboard_section.get("branch", "dashboard")),
		output_root=str(dashboard_section.get("output_root", "dashboard/")),
		sharepoint_page_url=str(dashboard_section.get("sharepoint_page_url", "")),
		power_automate_flow=str(dashboard_section.get("power_automate_flow", "")),
		latest_scan_issue=str(
			dashboard_section.get("latest_scan_issue", "Latest Sentinel scan")
		),
		audience=str(dashboard_section.get("audience", "owner_approver")),
	)
	profile_section = _require_section(data, "profile")
	profile_name = _require_string(profile_section, "name", "profile")
	if profile_name not in _KNOWN_PROFILES:
		raise WorkflowConfigError(
			f"unknown profile {profile_name!r}; known: {sorted(_KNOWN_PROFILES)}"
		)
	default_profile = _default_profile(profile_name)
	profile = ProfileConfig(
		name=profile_name,
		version=str(profile_section.get("version", default_profile.version)),
		override_label=str(
			profile_section.get("override_label", default_profile.override_label)
		),
		state_labels=_coerce_state_labels(
			profile_section.get("state_labels"), default_profile.state_labels
		),
		auto_label=str(profile_section.get("auto_label", default_profile.auto_label)),
		transitions=default_profile.transitions,
	)
	return WorkflowConfig(
		github=github,
		themes=themes,
		remediation=remediation,
		dashboard=dashboard,
		profile=profile,
	)


def _default_profile(profile_name: str) -> ProfileConfig:
	if profile_name == "owner_gate":
		return ProfileConfig(
			name="owner_gate",
			version="0.8.0",
			override_label="human-override",
			state_labels=_OWNER_GATE_STATE_LABELS,
			auto_label="sentinel-auto",
			transitions=_OWNER_GATE_TRANSITIONS,
		)
	if profile_name == "solo_local_remediating":
		# Per spec §1.6.3: version 0.8.0; override/auto markers use the
		# foundation defaults ("human-override", "sentinel-auto") per the
		# §1.6.3 Label vocabulary row ("`sentinel-auto`, `human-override`,
		# and `needs-human-review` or equivalents").
		return ProfileConfig(
			name="solo_local_remediating",
			version="0.8.0",
			override_label="human-override",
			state_labels=_SOLO_LOCAL_REMEDIATING_STATE_LABELS,
			auto_label="sentinel-auto",
			transitions=_SOLO_LOCAL_REMEDIATING_TRANSITIONS,
		)
	# Other known profiles use generic foundation defaults; their
	# concrete state-label sets and transition mappings land when their
	# profile cycles run. ``transitions=None`` is the advisory-profile
	# sentinel per spec section 1.6.2.
	return ProfileConfig(
		name=profile_name,
		version="0.8.0",
		override_label="human-override",
		state_labels=frozenset(),
		auto_label="sentinel-auto",
		transitions=None,
	)


def _resolve_transitions(
	config: "WorkflowConfig | None",
) -> TransitionStateConfig:
	"""Resolve the active transition vocabulary for a workflow caller.

	Three-state behavior (cycle 11, Q3 lock):

	1. ``config is None``: emit a one-time :class:`DeprecationWarning`
	   and fall back to :data:`_OWNER_GATE_TRANSITIONS`. Preserves the
	   cycle-8 ``build_workflow_state_summary`` precedent of silent
	   ``owner_gate`` fallback for backward compat, while making the
	   end-goal portability gap visible. Future cycles may flip this
	   branch to raise once all call sites pass an explicit config.
	2. ``config.profile.transitions is None``: raise
	   :class:`WorkflowConfigError`. The profile is advisory per spec
	   section 1.6.2 and cannot drive workflow transitions.
	3. otherwise: return ``config.profile.transitions`` as supplied.

	``stacklevel=2`` on the warning surfaces it at the caller's frame
	rather than this helper's frame, matching Python convention.
	"""
	if config is None:
		warnings.warn(
			"WorkflowConfig not supplied; falling back to owner_gate transitions. "
			"Pass an explicit WorkflowConfig for profile-portable behavior.",
			DeprecationWarning,
			stacklevel=2,
		)
		return _OWNER_GATE_TRANSITIONS
	if config.profile.transitions is None:
		raise WorkflowConfigError(
			f"profile {config.profile.name!r} declares no workflow transitions; "
			"advisory profiles cannot use workflow transition handlers "
			"(spec sections 1.6.2, 7)"
		)
	return config.profile.transitions


def _coerce_state_labels(
	value: Any, default: frozenset[str]
) -> frozenset[str]:
	"""Coerce a config ``state_labels`` value into a ``frozenset[str]``.

	Returns ``default`` when the key is absent. A bare string is rejected
	rather than silently exploded into single characters (``frozenset("triage")``
	would yield ``{'t', 'r', 'i', 'a', 'g', 'e'}``); ``state_labels`` must be a
	list of label strings.
	"""
	if value is None:
		return default
	if isinstance(value, str) or not isinstance(value, (list, tuple, set, frozenset)):
		raise WorkflowConfigError(
			"'state_labels' in section [profile] must be a list of strings, "
			f"got {type(value).__name__}"
		)
	labels = list(value)
	if any(not isinstance(label, str) for label in labels):
		raise WorkflowConfigError(
			"'state_labels' in section [profile] must contain only strings"
		)
	return frozenset(labels)


def _require_section(data: dict[str, Any], key: str) -> dict[str, Any]:
	section = data.get(key)
	if not isinstance(section, dict):
		raise WorkflowConfigError(f"missing required section [{key}]")
	return section


def _require_string(section: dict[str, Any], key: str, section_name: str) -> str:
	value = section.get(key)
	if not isinstance(value, str) or not value:
		raise WorkflowConfigError(
			f"missing required key {key!r} in section [{section_name}]"
		)
	return value


__all__ = [
	"ConceptualLensConfig",
	"DashboardConfig",
	"GitHubConfig",
	"OWNER_GATE_STATE_LABELS",
	"HdbscanConfig",
	"ProfileConfig",
	"RemediationConfig",
	"StructuralLensConfig",
	"ThemesConfig",
	"TransitionStateConfig",
	"WorkflowConfig",
	"WorkflowConfigError",
	"load_workflow_config",
	"load_workflow_config_yaml",
]
