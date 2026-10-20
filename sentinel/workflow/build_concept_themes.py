"""Conceptual theme clustering and stability reconciliation."""

from __future__ import annotations

import json
import random
from itertools import count
from pathlib import Path
from typing import Iterable

from sentinel.workflow.models import ConceptTheme, FindingIssueRecord

_SEVERITY_RANK = {"Critical": 0, "P1": 1, "P2": 2, "P3": 3}
_EFFORT_RANK = {"high": 0, "medium-high": 1, "medium": 2, "low": 3}
_RISK_RANK = {
	"unacceptable — must fix before next release": 0,
	"high — likely to cause production issues": 1,
	"medium — technical debt accumulates": 2,
	"low — cosmetic or nice-to-have": 3,
}


def build_concept_themes(
	findings: list[FindingIssueRecord],
	cluster_assignments: dict[str, int],
	previous_themes: list[ConceptTheme] | None = None,
) -> list[ConceptTheme]:
	"""Build deterministic conceptual themes from precomputed cluster assignments."""
	previous_themes = previous_themes or []
	reused_theme_ids: set[str] = set()
	next_theme_number = count(_next_theme_number(previous_themes))
	concept_themes: list[ConceptTheme] = []

	for group in _group_findings(findings, cluster_assignments):
		prior_theme = _best_matching_prior_theme(group, previous_themes, reused_theme_ids)
		if prior_theme is not None:
			concept_theme_id = prior_theme.concept_theme_id
			reused_theme_ids.add(concept_theme_id)
			title = prior_theme.title
		else:
			concept_theme_id = f"C-{next(next_theme_number):03d}"
			title = _synthesize_title(group)

		concept_themes.append(ConceptTheme(
			concept_theme_id=concept_theme_id,
			title=title,
			root_cause_summary=_synthesize_root_cause_summary(group),
			remediation_strategy=_synthesize_remediation_strategy(group),
			member_finding_ids=[finding.finding_id for finding in group],
			skills_involved={finding.skill for finding in group},
			categories={finding.category for finding in group},
			files_affected={finding.file or finding.module for finding in group},
			aggregate_severity=_max_ranked([finding.severity for finding in group], _SEVERITY_RANK, "P3"),
			aggregate_effort=_max_ranked([finding.effort_estimate for finding in group], _EFFORT_RANK, "low"),
			aggregate_risk_of_deferral=_max_ranked(
				[finding.risk_of_deferral for finding in group],
				_RISK_RANK,
				"low — cosmetic or nice-to-have",
			),
		))

	return concept_themes


def build_concept_themes_from_descriptions(
	findings: list[FindingIssueRecord],
	previous_themes: list[ConceptTheme] | None = None,
	*,
	embedder: object | None = None,
	clusterer: object | None = None,
	embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2",
	embedding_seed: int = 42,
	min_cluster_size: int = 3,
	min_samples: int = 2,
	cluster_selection_epsilon: float = 0.15,
) -> list[ConceptTheme]:
	"""Build concept themes from finding descriptions using embeddings and HDBSCAN labels."""
	if not findings:
		return []

	if embedder is None:
		embedder = _load_embedder(embedding_model, embedding_seed)
	if clusterer is None:
		clusterer = _load_clusterer(
			min_cluster_size=min_cluster_size,
			min_samples=min_samples,
			cluster_selection_epsilon=cluster_selection_epsilon,
		)

	descriptions = [finding.description for finding in findings]
	embeddings = embedder.encode(
		descriptions,
		convert_to_numpy=True,
		normalize_embeddings=True,
		show_progress_bar=False,
	)
	cluster_labels = clusterer.fit_predict(embeddings)
	cluster_assignments = {
		finding.finding_id: int(cluster_label)
		for finding, cluster_label in zip(findings, cluster_labels, strict=True)
	}
	return build_concept_themes(
		findings,
		cluster_assignments=cluster_assignments,
		previous_themes=previous_themes,
	)


def load_concept_themes(file_path: str | Path) -> list[ConceptTheme]:
	path = Path(file_path)
	if not path.exists():
		return []
	payload = json.loads(path.read_text(encoding="utf-8"))
	return [ConceptTheme.from_dict(item) for item in payload]


def write_concept_themes(file_path: str | Path, themes: list[ConceptTheme]) -> None:
	path = Path(file_path)
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(
		json.dumps([theme.to_dict() for theme in themes], indent=2) + "\n",
		encoding="utf-8",
	)


def _load_embedder(model_name: str, embedding_seed: int) -> object:
	_set_embedding_seed(embedding_seed)
	try:
		from sentence_transformers import SentenceTransformer
	except ImportError as exc:  # pragma: no cover
		raise RuntimeError(
			"Concept theme embedding requires the optional 'workflow' dependencies. "
			"Install with pip install -e \".[workflow]\"."
		) from exc
	return SentenceTransformer(model_name)


def _load_clusterer(
	*,
	min_cluster_size: int,
	min_samples: int,
	cluster_selection_epsilon: float,
) -> object:
	try:
		import hdbscan
	except ImportError as exc:  # pragma: no cover
		raise RuntimeError(
			"Concept theme clustering requires the optional 'workflow' dependencies. "
			"Install with pip install -e \".[workflow]\"."
		) from exc
	return hdbscan.HDBSCAN(
		min_cluster_size=min_cluster_size,
		min_samples=min_samples,
		cluster_selection_epsilon=cluster_selection_epsilon,
	)


def _set_embedding_seed(seed: int) -> None:
	random.seed(seed)
	try:
		import numpy as np
	except ImportError:  # pragma: no cover
		np = None
	if np is not None:
		np.random.seed(seed)
	try:
		import torch
	except ImportError:  # pragma: no cover
		torch = None
	if torch is not None:
		torch.manual_seed(seed)
		if torch.cuda.is_available():  # pragma: no cover
			torch.cuda.manual_seed_all(seed)


def _group_findings(
	findings: list[FindingIssueRecord],
	cluster_assignments: dict[str, int],
) -> list[list[FindingIssueRecord]]:
	grouped: dict[str, list[FindingIssueRecord]] = {}
	for finding in findings:
		cluster_id = cluster_assignments.get(finding.finding_id, -1)
		if cluster_id < 0:
			group_key = f"singleton:{finding.finding_id}"
		else:
			group_key = f"cluster:{cluster_id}"
		grouped.setdefault(group_key, []).append(finding)
	return list(grouped.values())


def _best_matching_prior_theme(
	group: list[FindingIssueRecord],
	previous_themes: list[ConceptTheme],
	reused_theme_ids: set[str],
) -> ConceptTheme | None:
	group_member_ids = {finding.finding_id for finding in group}
	best_theme: ConceptTheme | None = None
	best_score = 0.0
	for theme in previous_themes:
		if theme.concept_theme_id in reused_theme_ids:
			continue
		score = _jaccard(group_member_ids, set(theme.member_finding_ids))
		if score >= 0.7 and score > best_score:
			best_score = score
			best_theme = theme
	return best_theme


def _next_theme_number(previous_themes: Iterable[ConceptTheme]) -> int:
	numbers = [int(theme.concept_theme_id.split("-", 1)[1]) for theme in previous_themes if theme.concept_theme_id.startswith("C-")]
	return max(numbers, default=0) + 1


def _jaccard(left: set[str], right: set[str]) -> float:
	if not left and not right:
		return 1.0
	union = left | right
	if not union:
		return 0.0
	return len(left & right) / len(union)


def _synthesize_title(group: list[FindingIssueRecord]) -> str:
	summaries = [finding.summary.strip() for finding in group if finding.summary.strip()]
	if not summaries:
		return "Concept theme"
	if len(summaries) == 1:
		return summaries[0]
	prefix = _common_prefix(summaries).strip(" -_:;,.\t")
	if len(prefix) >= 16:
		return prefix
	return min(summaries, key=lambda summary: (len(summary), summary.lower()))


def _common_prefix(values: list[str]) -> str:
	prefix = values[0]
	for value in values[1:]:
		limit = min(len(prefix), len(value))
		index = 0
		while index < limit and prefix[index] == value[index]:
			index += 1
		prefix = prefix[:index]
		if not prefix:
			break
	return prefix


def _synthesize_root_cause_summary(group: list[FindingIssueRecord]) -> str:
	descriptions = [finding.description.strip() for finding in group if finding.description.strip()]
	if not descriptions:
		return ""
	if len(descriptions) == 1:
		return descriptions[0]
	first = descriptions[0].rstrip(".")
	second = descriptions[1].rstrip(".")
	if first == second:
		return descriptions[0]
	return f"{first}. {second}."


def _synthesize_remediation_strategy(group: list[FindingIssueRecord]) -> str:
	fixes = [finding.suggested_fix.strip() for finding in group if finding.suggested_fix.strip()]
	if not fixes:
		return ""
	counts: dict[str, int] = {}
	for fix in fixes:
		counts[fix] = counts.get(fix, 0) + 1
	return max(counts.items(), key=lambda item: (item[1], -len(item[0]), item[0]))[0]


def _max_ranked(values: list[str], ranking: dict[str, int], default: str) -> str:
	if not values:
		return default
	return min(values, key=lambda value: ranking.get(value, ranking.get(default, 999)))


__all__ = [
	"build_concept_themes",
	"build_concept_themes_from_descriptions",
	"load_concept_themes",
	"write_concept_themes",
]
