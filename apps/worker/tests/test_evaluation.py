import re
from pathlib import Path

import pytest
from pydantic import BaseModel
from sqlalchemy import select

from rastro_db import create_engine, create_session_factory
from rastro_db.models import Repository
from rastro_llm import ExtractedRule, FakeProvider, ImpactAssessment, RuleExtraction
from rastro_worker.evaluation import (
    FIXTURE_GITHUB_ID_BASE,
    Report,
    format_report,
    load_fixture,
    load_pull_requests,
    run_evaluation,
)

FIXTURE = Path(__file__).resolve().parents[3] / "fixtures" / "clinica-demo"

pytestmark = pytest.mark.db


def test_fixture_matches_the_spec_requirements() -> None:
    commits = load_fixture(FIXTURE)
    planted = [e for c in commits for e in c.expected]
    technical = [c for c in commits if not c.expected]

    assert len(planted) >= 10
    assert len(technical) >= 3
    assert any(e.rationale is None and e.action == "create" for e in planted)  # no reason in chat
    assert any(e.rationale for e in planted)
    assert any(e.action == "update" for e in planted)
    assert sum(e.updates is not None for e in planted) >= 2


def test_every_expected_entry_is_consistent_with_its_own_session() -> None:
    for commit in load_fixture(FIXTURE):
        conversation = " ".join(commit.session)
        for expected in commit.expected:
            assert expected.quote in conversation, expected.id
            assert all(
                any(word.lower() in expected.statement.lower() for word in group)
                for group in expected.match
            ), expected.id
            assert all(key.split("::")[0] in commit.files for key in expected.symbols), expected.id


async def evaluate(
    llm: FakeProvider, *, oracle: bool = False, impact_oracle: bool = True
) -> Report:
    engine = create_engine()
    try:
        return await run_evaluation(
            create_session_factory(engine),
            load_fixture(FIXTURE),
            llm=llm,
            pull_requests=load_pull_requests(FIXTURE),
            oracle=llm if oracle else None,
            impact_oracle=impact_oracle,
        )
    finally:
        await engine.dispose()


async def test_oracle_replay_scores_perfectly_through_the_real_pipeline() -> None:
    report = await evaluate(FakeProvider(), oracle=True)

    assert report.planted == 11
    assert (report.recall, report.rationale_accuracy, report.update_accuracy) == (1.0, 1.0, 1.0)
    assert report.success_rate == 1.0 and report.passed
    assert report.false_positive_commits == 0
    assert all(c.status == "analyzed" for c in report.commits)
    assert [(p.name, p.warned) for p in report.pull_requests] == [
        ("01-implants-saturday-morning", True),
        ("02-shorter-cancellation-window", True),
        ("03-add-date-helper", False),
        ("04-simplify-duration-lookup", False),
        ("05-extract-reminder-constant", False),
    ]
    assert (report.pr_missed_warnings, report.pr_irrelevant_comments) == (0, 0)
    assert "PASS" in format_report(report)


async def test_a_model_that_finds_nothing_fails_without_false_positives() -> None:
    def nothing(prompt: str, schema: type[BaseModel]) -> BaseModel:
        return RuleExtraction(rules=[])

    report = await evaluate(FakeProvider(responder=nothing))

    assert report.success_rate == 0.0 and not report.passed
    assert report.false_positive_commits == 0
    assert "FAIL" in format_report(report)


async def test_a_model_that_sees_rules_everywhere_is_caught_on_technical_commits() -> None:
    def everywhere(prompt: str, schema: type[BaseModel]) -> BaseModel:
        key = re.search(r'<symbol key="([^"]+)"', prompt)
        assert key is not None
        return RuleExtraction(
            rules=[
                ExtractedRule(
                    action="create",
                    existing_rule_id=None,
                    statement="Something in the code changed",
                    rationale="An invented reason",
                    origin_quote="a quote nobody wrote",
                    symbols=[key.group(1)],
                    confidence=0.9,
                )
            ]
        )

    report = await evaluate(FakeProvider(responder=everywhere))

    assert report.false_positive_commits == 4  # the four technical-only commits
    assert not report.passed


async def test_the_evaluation_leaves_no_data_behind() -> None:
    await evaluate(FakeProvider(), oracle=True)

    engine = create_engine()
    try:
        async with create_session_factory(engine)() as session:
            leftovers = (
                await session.scalars(
                    select(Repository.id).where(
                        Repository.github_id.between(
                            FIXTURE_GITHUB_ID_BASE, FIXTURE_GITHUB_ID_BASE + 99_999
                        )
                    )
                )
            ).all()
    finally:
        await engine.dispose()
    assert leftovers == []


def test_fixture_pull_requests_match_the_spec_requirements() -> None:
    prs = load_pull_requests(FIXTURE)

    assert len(prs) == 5
    assert sum(p.expect_warning for p in prs) == 2  # two that must warn, three that must not
    assert all(p.oracle for p in prs if p.expect_warning)


async def test_a_model_that_flags_every_pull_request_fails_the_irrelevant_comment_budget() -> None:
    alarmist = FakeProvider(
        responder=lambda prompt, schema: (
            ImpactAssessment(
                behavior_changed=True,
                counterexample="Some input.",
                risk="breaks",
                confidence=0.95,
                explanation="This surely breaks the rule.",
            )
            if schema is ImpactAssessment
            else RuleExtraction(rules=[])
        )
    )
    report = await evaluate(alarmist, oracle=True, impact_oracle=False)

    assert report.pr_irrelevant_comments >= 2 and not report.pr_ok and not report.passed
    assert "IRRELEVANT COMMENT" in format_report(report)


async def test_a_model_that_never_flags_a_pull_request_misses_the_warnings() -> None:
    quiet = FakeProvider(
        responder=lambda prompt, schema: (
            ImpactAssessment(
                behavior_changed=False,
                counterexample=None,
                risk="none",
                confidence=0.95,
                explanation="Not affected.",
            )
            if schema is ImpactAssessment
            else RuleExtraction(rules=[])
        )
    )

    report = await evaluate(quiet)

    assert report.pr_missed_warnings == 2 and report.pr_irrelevant_comments == 0
    assert "MISSED" in format_report(report)
