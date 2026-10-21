"""Squash and rebase merges: the sessions stay under the PR's original SHAs, which never reach
the default branch, so a merged pull request releases them."""

from typing import Any

from procrastinate import testing
from sqlalchemy import update
from test_rule_extraction import (
    AFTER,
    AFTER_SATURDAY,
    AGENDA,
    BEFORE,
    KEY,
    SHA1,
    SHA2,
    SHA3,
    rule,
)
from worker_support import WorkerContext, user_excerpt

from rastro_db.models import Commit
from rastro_llm import RuleExtraction
from rastro_worker import tasks
from rastro_worker.app import app
from rastro_worker.merged_pr import release_pull_request_commits
from rastro_worker.rule_extraction import extract_rules

NUMBER = 7
SQUASH = "4" * 40  # the commit the squash merge created on the default branch


async def stage_pull_request(worker: WorkerContext) -> None:
    """Two commits with sessions, one without, all still on the feature branch."""
    worker.stage_commit(SHA1, {AGENDA: BEFORE}, {AGENDA: None})  # no session: a chore commit
    worker.stage_commit(SHA2, {AGENDA: AFTER}, {AGENDA: BEFORE})
    worker.stage_commit(SHA3, {AGENDA: AFTER_SATURDAY}, {AGENDA: AFTER})
    for sha in (SHA1, SHA2, SHA3):
        await worker.add_commit_row(sha)
        await worker.set_on_default_branch(sha, False)
    await worker.add_session(SHA2, [user_excerpt("Implants cannot be booked on Friday afternoon.")])
    await worker.add_session(SHA3, [user_excerpt("Saturdays work like Fridays for implants.")])
    worker.github.set_pull_request_commits(worker.full_name, NUMBER, [SHA1, SHA2, SHA3])


async def run_merged(worker: WorkerContext, merge_sha: str | None = SQUASH) -> list[dict[str, Any]]:
    connector = testing.InMemoryConnector()
    with app.replace_connector(connector) as test_app:
        async with test_app.open_async():
            await tasks.pull_request_merged(str(worker.repository_id), NUMBER, merge_sha)
    return list(connector.jobs.values())


def queued(jobs: list[dict[str, Any]], task: str) -> list[str]:
    return [j["args"]["sha"] for j in jobs if j["task_name"] == task]


async def test_a_merged_pull_request_releases_the_commits_that_have_sessions(
    worker: WorkerContext,
) -> None:
    await stage_pull_request(worker)

    jobs = await run_merged(worker)

    assert queued(jobs, "rule_extraction") == [SHA2, SHA3]  # oldest first; SHA1 has no session
    assert queued(jobs, "analyze_commit") == [SQUASH]  # symbols for the merged state
    assert all(j["lock"] == str(worker.repository_id) for j in jobs)
    for sha in (SHA1, SHA2, SHA3):
        commit = await worker.commit(sha)
        assert commit is not None and commit.on_default_branch is True


async def test_commits_unknown_to_the_database_are_created_on_the_default_branch(
    worker: WorkerContext,
) -> None:
    await stage_pull_request(worker)
    worker.github.set_pull_request_commits(worker.full_name, NUMBER, [SHA1, SHA2, SHA3, "5" * 40])

    await run_merged(worker)

    created = await worker.commit("5" * 40)
    assert created is not None and created.on_default_branch is True


async def test_squashed_commits_extract_from_their_own_diffs_in_order(
    worker: WorkerContext,
) -> None:
    """The squash commit has no session and the PR's SHAs are not on the default branch, yet
    each commit is analysed from its own diff and later commits update earlier rules."""
    await stage_pull_request(worker)
    jobs = await run_merged(worker)
    worker.llm.queue(RuleExtraction(rules=[rule()]))

    first = await extract_rules(
        worker.deps, worker.repository_id, queued(jobs, "rule_extraction")[0]
    )
    [created] = await worker.rules()
    worker.llm.queue(
        RuleExtraction(
            rules=[
                rule(
                    action="update",
                    existing_rule_id=str(created.id),
                    statement="Implants cannot be booked on Friday or Saturday afternoon",
                    rationale=None,
                    origin_quote=None,
                )
            ]
        )
    )
    second = await extract_rules(
        worker.deps, worker.repository_id, queued(jobs, "rule_extraction")[1]
    )

    assert (first.status, first.created) == ("analyzed", 1)
    assert (second.status, second.updated) == ("analyzed", 1)
    [final] = await worker.rules()
    assert final.id == created.id and "Saturday" in final.statement
    assert final.rationale == created.rationale  # the update brought no new reason: kept
    assert str(created.id) in worker.llm.calls[1].prompt  # the second commit saw the first's rule
    assert await worker.anchors(final.id) == {(AGENDA, "AgendaService.validate"): "primary"}
    assert KEY in worker.llm.calls[0].prompt


async def test_the_release_is_idempotent(worker: WorkerContext) -> None:
    await stage_pull_request(worker)
    jobs = await run_merged(worker)
    worker.llm.queue(RuleExtraction(rules=[rule()]))
    worker.llm.queue(RuleExtraction(rules=[]))
    for sha in queued(jobs, "rule_extraction"):
        await extract_rules(worker.deps, worker.repository_id, sha)
    calls = len(worker.llm.calls)

    again = await run_merged(worker)  # webhook redelivered with a new delivery id
    for sha in queued(again, "rule_extraction"):
        result = await extract_rules(worker.deps, worker.repository_id, sha)
        assert result.status == "analyzed"

    assert len(worker.llm.calls) == calls  # nothing is analysed twice
    assert len(await worker.rules()) == 1


async def test_a_session_uploaded_after_the_merge_is_extracted_at_once(
    worker: WorkerContext,
) -> None:
    """The flag set by the release is what lets a late upload (POST /v1/sessions) through."""
    worker.stage_commit(SHA2, {AGENDA: AFTER}, {AGENDA: BEFORE})
    await worker.add_commit_row(SHA2)
    await worker.set_on_default_branch(SHA2, False)
    worker.github.set_pull_request_commits(worker.full_name, NUMBER, [SHA2])
    jobs = await run_merged(worker)
    assert queued(jobs, "rule_extraction") == []  # no session yet

    await worker.add_session(SHA2, [user_excerpt("Implants cannot be booked on Friday afternoon.")])
    worker.llm.queue(RuleExtraction(rules=[rule()]))

    assert (await extract_rules(worker.deps, worker.repository_id, SHA2)).status == "analyzed"


async def test_without_a_merge_commit_sha_only_the_extractions_are_queued(
    worker: WorkerContext,
) -> None:
    await stage_pull_request(worker)

    jobs = await run_merged(worker, merge_sha=None)

    assert queued(jobs, "analyze_commit") == []
    assert queued(jobs, "rule_extraction") == [SHA2, SHA3]


async def test_the_pull_request_is_recorded_on_its_commits_and_on_the_merge_commit(
    worker: WorkerContext,
) -> None:
    await stage_pull_request(worker)
    async with worker.deps.session_factory() as session, session.begin():
        await session.execute(
            update(Commit)
            .where(Commit.repository_id == worker.repository_id, Commit.sha == SHA1)
            .values(merged_pr_number=99)  # already recorded with an earlier pull request
        )

    await release_pull_request_commits(
        worker.deps, worker.repository_id, NUMBER, merge_commit_sha=SQUASH, author="dev-ana"
    )

    numbers = {
        sha: (await worker.commit(sha)).merged_pr_number  # type: ignore[union-attr]
        for sha in (SHA1, SHA2, SHA3, SQUASH)
    }
    assert numbers == {
        SHA1: 99,
        SHA2: NUMBER,
        SHA3: NUMBER,
        SQUASH: NUMBER,
    }  # a commit keeps its first PR
    squash = await worker.commit(SQUASH)
    assert squash is not None and squash.on_default_branch is True  # the squash commit exists now
