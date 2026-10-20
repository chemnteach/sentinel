from __future__ import annotations

import httpx
import respx

from sentinel.workflow.github_client import GitHubWorkflowClient


@respx.mock
def test_find_finding_issue_uses_github_search() -> None:
    route = respx.get("https://api.github.com/search/issues").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "number": 101,
                        "title": "[P1] layer2a-quick-review: Optional issue",
                        "body": "- **Finding ID:** F-123456789abc",
                        "labels": [
                            {"name": "severity:p1"},
                            {"name": "state:triage"},
                            {"name": "sentinel-auto"},
                        ],
                        "html_url": "https://github.com/chemnteach/sentinel/issues/101",
                    }
                ]
            },
        )
    )

    with GitHubWorkflowClient(
        owner="chemnteach",
        repo="sentinel",
        token="ghp_test",
    ) as client:
        snapshot = client.find_finding_issue("F-123456789abc")

    assert route.called
    assert snapshot is not None
    assert snapshot.issue_number == 101
    request = route.calls[0].request
    query = request.url.params.get("q", "")
    assert "repo:chemnteach/sentinel" in query
    assert "F-123456789abc" in query


@respx.mock
def test_update_issue_patches_title_body_and_labels() -> None:
    route = respx.patch("https://api.github.com/repos/chemnteach/sentinel/issues/42").mock(
        return_value=httpx.Response(
            200,
            json={
                "number": 42,
                "title": "Updated title",
                "body": "Updated body",
                "labels": [
                    {"name": "severity:p1"},
                    {"name": "state:owner-assessed"},
                ],
                "html_url": "https://github.com/chemnteach/sentinel/issues/42",
            },
        )
    )

    with GitHubWorkflowClient(
        owner="chemnteach",
        repo="sentinel",
        token="ghp_test",
    ) as client:
        snapshot = client.update_issue(
            42,
            title="Updated title",
            body="Updated body",
            labels=["severity:p1", "state:owner-assessed"],
        )

    assert route.called
    assert snapshot.issue_number == 42
    assert snapshot.labels == ("severity:p1", "state:owner-assessed")

@respx.mock
def test_close_issue_no_longer_reproduces_maps_to_completed() -> None:
    route = respx.patch("https://api.github.com/repos/chemnteach/sentinel/issues/77").mock(
        return_value=httpx.Response(
            200,
            json={
                "number": 77,
                "title": "closed finding",
                "body": "",
                "labels": [],
                "html_url": "https://github.com/chemnteach/sentinel/issues/77",
            },
        )
    )

    with GitHubWorkflowClient(
        owner="chemnteach",
        repo="sentinel",
        token="ghp_test",
    ) as client:
        snapshot = client.close_issue(77, reason="no-longer-reproduces")

    assert route.called
    assert snapshot.issue_number == 77
    body = route.calls[0].request.read().decode()
    assert '"state":"closed"' in body
    assert '"state_reason":"completed"' in body


@respx.mock
def test_close_issue_pr_merged_maps_to_completed() -> None:
    route = respx.patch("https://api.github.com/repos/chemnteach/sentinel/issues/78").mock(
        return_value=httpx.Response(
            200,
            json={
                "number": 78,
                "title": "merged finding",
                "body": "",
                "labels": [],
                "html_url": "https://github.com/chemnteach/sentinel/issues/78",
            },
        )
    )

    with GitHubWorkflowClient(
        owner="chemnteach",
        repo="sentinel",
        token="ghp_test",
    ) as client:
        client.close_issue(78, reason="pr-merged")

    body = route.calls[0].request.read().decode()
    assert '"state_reason":"completed"' in body


@respx.mock
def test_close_issue_superseded_maps_to_not_planned() -> None:
    route = respx.patch("https://api.github.com/repos/chemnteach/sentinel/issues/79").mock(
        return_value=httpx.Response(
            200,
            json={
                "number": 79,
                "title": "superseded theme",
                "body": "",
                "labels": [],
                "html_url": "https://github.com/chemnteach/sentinel/issues/79",
            },
        )
    )

    with GitHubWorkflowClient(
        owner="chemnteach",
        repo="sentinel",
        token="ghp_test",
    ) as client:
        client.close_issue(79, reason="superseded")

    body = route.calls[0].request.read().decode()
    assert '"state_reason":"not_planned"' in body



@respx.mock
def test_post_issue_comment_posts_body_to_comments_endpoint() -> None:
	route = respx.post("https://api.github.com/repos/chemnteach/sentinel/issues/55/comments").mock(
		return_value=httpx.Response(
			201,
			json={
				"id": 9991,
				"body": "hello world",
				"html_url": "https://github.com/chemnteach/sentinel/issues/55#issuecomment-9991",
			},
		)
	)

	with GitHubWorkflowClient(
		owner="chemnteach",
		repo="sentinel",
		token="ghp_test",
	) as client:
		payload = client.post_issue_comment(55, "hello world")

	assert route.called
	assert payload["id"] == 9991
	request_body = route.calls[0].request.read().decode()
	assert '"body":"hello world"' in request_body
