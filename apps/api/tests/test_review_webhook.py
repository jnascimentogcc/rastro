"""`issue_comment` webhooks: only the ones that can be a review reach the worker."""

import uuid
from typing import Any

import pytest
from api_support import ApiContext
from test_webhooks import GITHUB_ID, RUN, deliver

from rastro_github import COMMENT_MARKER

pytestmark = pytest.mark.db

NUMBER = 7
KEY = "ab12cd34ef56ab12cd34ef56ab12cd34"
RASTRO_COMMENT = f"{COMMENT_MARKER}\n- [x] **A rule** <!-- rastro:proposal:{KEY} -->\n"
BEFORE = RASTRO_COMMENT.replace("[x]", "[ ]")


def comment_payload(
    action: str = "created", body: str = "/rastro ok ab12", **overrides: Any
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "action": action,
        "installation": {"id": 4242},
        "repository": {
            "id": GITHUB_ID,
            "full_name": "acme/webhook-clinic",
            "default_branch": "main",
        },
        "issue": {"number": NUMBER, "pull_request": {"url": "https://api.github.com/x"}},
        "comment": {"id": 555, "body": body},
        "sender": {"login": "ana", "type": "User"},
    }
    if action == "edited":
        payload["changes"] = {"body": {"from": BEFORE}}
    return {**payload, **overrides}


async def send(api: ApiContext, payload: dict[str, Any]) -> Any:
    return await deliver(api, "issue_comment", payload, delivery_id=f"comment-{uuid.uuid4()}{RUN}")


async def test_a_command_on_a_pull_request_is_queued_for_the_worker(api: ApiContext) -> None:
    response = await send(api, comment_payload())

    assert response.status_code == 202 and response.json()["handled"] is True
    [job] = api.jobs("apply_review")
    args = job["args"]
    assert (args["number"], args["comment_id"], args["sender"]) == (NUMBER, 555, "ana")  # type: ignore[index]
    assert (args["event_action"], args["body"]) == ("created", "/rastro ok ab12")  # type: ignore[index]
    assert job["lock"] == args["repository_id"]  # type: ignore[index]  # one job per repository


async def test_commands_are_recognised_whatever_the_case_or_leading_spaces(
    api: ApiContext,
) -> None:
    await send(api, comment_payload(body="  /Rastro reject ab12"))

    assert len(api.jobs("apply_review")) == 1


async def test_an_edit_of_rastros_comment_carries_the_previous_body(api: ApiContext) -> None:
    response = await send(api, comment_payload("edited", RASTRO_COMMENT))

    assert response.json()["handled"] is True
    [job] = api.jobs("apply_review")
    assert job["args"]["previous_body"] == BEFORE  # type: ignore[index]
    assert job["args"]["event_action"] == "edited"  # type: ignore[index]


@pytest.mark.parametrize(
    "payload",
    [
        comment_payload(body="nice work, thanks"),  # not a command
        comment_payload(body="please run /rastro ok ab12"),  # not at the start
        comment_payload(issue={"number": NUMBER}),  # a plain issue, not a pull request
        comment_payload(sender={"login": "rastro-app[bot]", "type": "Bot"}),  # our own edits
        comment_payload(sender={"login": "someone[bot]", "type": "User"}),
        comment_payload(sender={"type": "User"}),  # no login
        comment_payload("edited", "someone else's comment, no marker"),
        comment_payload("deleted"),
        comment_payload("edited", RASTRO_COMMENT, changes={}),  # not a body edit
    ],
)
async def test_everything_else_is_ignored_without_queueing_anything(
    api: ApiContext, payload: dict[str, Any]
) -> None:
    response = await send(api, payload)

    assert response.status_code == 202 and response.json()["handled"] is False
    assert api.jobs("apply_review") == []


async def test_comments_of_inactive_repositories_are_ignored(api: ApiContext) -> None:
    await send(api, comment_payload())  # registers the repository
    api.jobs("apply_review")
    await deliver(
        api,
        "installation",
        {
            "action": "deleted",
            "installation": {"id": 4242},
            "repositories": [{"id": GITHUB_ID, "full_name": "acme/webhook-clinic"}],
        },
        delivery_id=f"gone-{uuid.uuid4()}{RUN}",
    )
    before = len(api.jobs("apply_review"))

    await send(api, comment_payload())

    assert len(api.jobs("apply_review")) == before
