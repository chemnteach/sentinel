"""GitHub API client wrapper for the Sentinel workflow layer."""

from __future__ import annotations

from typing import Any

import httpx

from sentinel.workflow.config import WorkflowConfig
from sentinel.workflow.models import ClosureReason, GitHubIssueSnapshot


_STATE_REASON_BY_CLOSURE: dict[ClosureReason, str] = {
	"no-longer-reproduces": "completed",
	"pr-merged": "completed",
	"superseded": "not_planned",
}


class GitHubWorkflowClient:
	"""Thin GitHub Issues client for workflow synchronization."""

	def __init__(
		self,
		*,
		owner: str,
		repo: str,
		token: str,
		api_base: str = "https://api.github.com",
		timeout: float = 30.0,
	) -> None:
		self._owner = owner
		self._repo = repo
		headers = {
			"Accept": "application/vnd.github+json",
			"Authorization": f"Bearer {token}",
			"X-GitHub-Api-Version": "2022-11-28",
		}
		self._http = httpx.Client(base_url=api_base, headers=headers, timeout=timeout)

	@classmethod
	def from_config(
		cls,
		config: WorkflowConfig,
		*,
		token: str,
		api_base: str = "https://api.github.com",
		timeout: float = 30.0,
	) -> "GitHubWorkflowClient":
		"""Construct a client from a ``WorkflowConfig`` plus an auth token.

		``owner`` and ``repo`` are read from ``config.github``. The token
		is passed separately because secrets do not belong in the TOML
		config file (spec section 10.1 does not specify a token field).
		"""
		return cls(
			owner=config.github.owner,
			repo=config.github.repo,
			token=token,
			api_base=api_base,
			timeout=timeout,
		)

	def search_issues(self, query: str) -> list[GitHubIssueSnapshot]:
		response = self._http.get("/search/issues", params={"q": query})
		response.raise_for_status()
		payload = response.json()
		return [_issue_snapshot_from_payload(item) for item in payload.get("items", [])]

	def find_finding_issue(self, finding_id: str) -> GitHubIssueSnapshot | None:
		query = (
			f'repo:{self._owner}/{self._repo} is:issue label:sentinel-auto '
			f'in:body "{finding_id}"'
		)
		issues = self.search_issues(query)
		return issues[0] if issues else None

	def find_theme_issue(self, theme_id: str, lens: str) -> GitHubIssueSnapshot | None:
		rollup_label = "is-struct-theme" if lens == "struct" else "is-concept-theme"
		query = (
			f'repo:{self._owner}/{self._repo} is:issue label:{rollup_label} '
			f'in:title "[{theme_id}]"'
		)
		issues = self.search_issues(query)
		return issues[0] if issues else None

	def create_issue(
		self,
		title: str,
		body: str,
		labels: list[str],
	) -> GitHubIssueSnapshot:
		response = self._http.post(
			f"/repos/{self._owner}/{self._repo}/issues",
			json={"title": title, "body": body, "labels": labels},
		)
		response.raise_for_status()
		return _issue_snapshot_from_payload(response.json())

	def update_issue(
		self,
		issue_number: int,
		*,
		title: str,
		body: str,
		labels: list[str],
	) -> GitHubIssueSnapshot:
		response = self._http.patch(
			f"/repos/{self._owner}/{self._repo}/issues/{issue_number}",
			json={"title": title, "body": body, "labels": labels},
		)
		response.raise_for_status()
		return _issue_snapshot_from_payload(response.json())

	def close_issue(
		self,
		issue_number: int,
		*,
		reason: ClosureReason,
	) -> GitHubIssueSnapshot:
		"""Close a GitHub issue with a ``state_reason`` derived from ``reason``.

		``no-longer-reproduces`` and ``pr-merged`` map to ``completed``;
		``superseded`` maps to ``not_planned``.
		"""
		state_reason = _STATE_REASON_BY_CLOSURE[reason]
		response = self._http.patch(
			f"/repos/{self._owner}/{self._repo}/issues/{issue_number}",
			json={"state": "closed", "state_reason": state_reason},
		)
		response.raise_for_status()
		return _issue_snapshot_from_payload(response.json())

	def post_issue_comment(
		self,
		issue_number: int,
		body: str,
	) -> dict[str, Any]:
		"""Post a comment to an existing issue.

		Returns the parsed GitHub comment payload (raw dict). Used for
		workflow notes such as the supersede link emitted alongside
		:meth:`close_issue` under spec section 6.2.
		"""
		response = self._http.post(
			f"/repos/{self._owner}/{self._repo}/issues/{issue_number}/comments",
			json={"body": body},
		)
		response.raise_for_status()
		return response.json()

	def close(self) -> None:
		self._http.close()

	def __enter__(self) -> "GitHubWorkflowClient":
		return self

	def __exit__(self, *_: Any) -> None:
		self.close()


def _issue_snapshot_from_payload(payload: dict[str, Any]) -> GitHubIssueSnapshot:
	labels = payload.get("labels", [])
	label_names = tuple(
		label["name"] if isinstance(label, dict) else str(label)
		for label in labels
	)
	return GitHubIssueSnapshot(
		issue_number=int(payload["number"]),
		title=str(payload.get("title", "")),
		body=str(payload.get("body", "")),
		labels=label_names,
		html_url=payload.get("html_url"),
	)


__all__ = ["GitHubWorkflowClient"]
