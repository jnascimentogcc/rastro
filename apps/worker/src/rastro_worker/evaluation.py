"""Evaluate rule extraction against a fixture repository with planted rules (spec sections 2, 12).

The fixture is a sequence of commits, each with the files it changes, the user requests of a
simulated agent session and the business rules it plants. The whole pipeline runs for real
(static analysis, prompt, validation, anchoring, database); only GitHub is replaced by an
in-memory fake. The model is either a real provider or an "oracle" that replays the ground
truth, which checks the fixture and the harness without spending a cent.
"""

import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rastro_db.models import AgentSession, Commit, LlmCall, Repository, Rule, Symbol
from rastro_github import ChangedFile
from rastro_github.testing import FakeGitHub, blob_sha
from rastro_llm import ExtractedRule, FakeProvider, LLMProvider, RuleExtraction
from rastro_worker import indexing
from rastro_worker.deps import WorkerDeps
from rastro_worker.rule_extraction import extract_rules

SUCCESS_THRESHOLD = 0.8
FIXTURE_GITHUB_ID_BASE = 9_300_000


@dataclass(frozen=True)
class ExpectedRule:
    id: str
    action: str  # create | update
    statement: str
    match: list[list[str]]  # every group needs one of its words in the extracted statement
    rationale: list[str] | None  # None: must stay null; else any of these words must appear
    symbols: list[str]
    quote: str
    updates: str | None = None


@dataclass(frozen=True)
class FixtureCommit:
    name: str
    message: str
    session: list[str]
    files: dict[str, str]
    expected: list[ExpectedRule]


def load_fixture(root: Path) -> list[FixtureCommit]:
    commits: list[FixtureCommit] = []
    for directory in sorted((root / "commits").iterdir()):
        if not directory.is_dir():
            continue
        data = json.loads((directory / "commit.json").read_text(encoding="utf-8"))
        files_root = directory / "files"
        files = {
            path.relative_to(files_root).as_posix(): path.read_text(encoding="utf-8")
            for path in sorted(files_root.rglob("*"))
            if path.is_file()
        }
        expected = [
            ExpectedRule(
                id=item["id"],
                action=item["action"],
                statement=item["statement"],
                match=item["match"],
                rationale=(item["rationale"] or {}).get("any") if item["rationale"] else None,
                symbols=item["symbols"],
                quote=item["quote"],
                updates=item.get("updates"),
            )
            for item in data["expected"]
        ]
        commits.append(
            FixtureCommit(
                name=directory.name,
                message=data["message"],
                session=[entry["text"] for entry in data["session"]],
                files=files,
                expected=expected,
            )
        )
    return commits


# --------------------------------------------------------------------------- results


@dataclass
class EventResult:
    expected: ExpectedRule
    matched: bool = False
    rationale_ok: bool = False
    update_ok: bool = True  # only meaningful for updates

    @property
    def correct(self) -> bool:
        return self.matched and self.rationale_ok and self.update_ok


@dataclass
class CommitResult:
    name: str
    status: str
    events: list[EventResult] = field(default_factory=list)
    extra_rules: int = 0  # rules touched that match no planted rule
    false_positive: bool = False  # a technical-only commit produced rules


@dataclass
class Report:
    commits: list[CommitResult]
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    model_calls: int = 0

    @property
    def events(self) -> list[EventResult]:
        return [e for c in self.commits for e in c.events]

    @property
    def planted(self) -> int:
        return len(self.events)

    @property
    def recall(self) -> float:
        return _ratio(sum(e.matched for e in self.events), self.planted)

    @property
    def rationale_accuracy(self) -> float:
        matched = [e for e in self.events if e.matched]
        return _ratio(sum(e.rationale_ok for e in matched), len(matched))

    @property
    def update_accuracy(self) -> float:
        updates = [e for e in self.events if e.expected.action == "update"]
        return _ratio(sum(e.matched and e.update_ok for e in updates), len(updates))

    @property
    def success_rate(self) -> float:
        """Planted rules extracted with the right statement, reason and (for updates) target."""
        return _ratio(sum(e.correct for e in self.events), self.planted)

    @property
    def false_positive_commits(self) -> int:
        return sum(c.false_positive for c in self.commits)

    @property
    def passed(self) -> bool:
        return self.success_rate >= SUCCESS_THRESHOLD


def _ratio(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 1.0


def format_report(report: Report) -> str:
    lines = [f"{'commit':<28} {'status':<20} planted  correct  extras  false+"]
    for commit in report.commits:
        correct = sum(e.correct for e in commit.events)
        lines.append(
            f"{commit.name:<28} {commit.status:<20} {len(commit.events):>7}  {correct:>7}  "
            f"{commit.extra_rules:>6}  {'YES' if commit.false_positive else '-':>6}"
        )
        for event in commit.events:
            if not event.correct:
                problems = []
                if not event.matched:
                    problems.append("statement not found")
                if event.matched and not event.rationale_ok:
                    problems.append("wrong or invented reason")
                if event.matched and not event.update_ok:
                    problems.append("did not update the existing rule")
                lines.append(f"    - {event.expected.id}: {', '.join(problems)}")
    cost = "unknown" if report.cost_usd is None else f"${report.cost_usd:.4f}"
    lines += [
        "",
        f"planted rules:        {report.planted}",
        f"statement recall:     {report.recall:.0%}",
        f"reason accuracy:      {report.rationale_accuracy:.0%}",
        f"update accuracy:      {report.update_accuracy:.0%}",
        f"false-positive commits: {report.false_positive_commits}",
        f"SUCCESS RATE:         {report.success_rate:.0%} (target {SUCCESS_THRESHOLD:.0%}) "
        f"-> {'PASS' if report.passed else 'FAIL'}",
        f"model calls: {report.model_calls}, tokens in/out: {report.input_tokens}/"
        f"{report.output_tokens}, estimated cost: {cost}",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- running


def _statement_matches(statement: str, groups: list[list[str]]) -> bool:
    text = statement.lower()
    return all(any(word.lower() in text for word in group) for group in groups)


def _rationale_ok(expected: list[str] | None, actual: str | None) -> bool:
    if expected is None:
        return actual is None
    return actual is not None and any(word.lower() in actual.lower() for word in expected)


def _oracle_response(commit: FixtureCommit, rule_ids: dict[str, uuid.UUID]) -> RuleExtraction:
    return RuleExtraction(
        rules=[
            ExtractedRule(
                action="update" if item.action == "update" else "create",
                existing_rule_id=str(rule_ids[item.updates]) if item.updates else None,
                statement=item.statement,
                # Only a reason the quote itself carries; an update that states none keeps the old one.
                rationale=(
                    "Stated by the user: " + item.quote
                    if item.rationale
                    and any(word.lower() in item.quote.lower() for word in item.rationale)
                    else None
                ),
                origin_quote=item.quote,
                symbols=item.symbols,
                confidence=0.95,
            )
            for item in commit.expected
        ]
    )


async def run_evaluation(
    session_factory: async_sessionmaker[AsyncSession],
    fixture: list[FixtureCommit],
    *,
    llm: LLMProvider,
    oracle: FakeProvider | None = None,
    keep_data: bool = False,
    on_commit: Callable[[str], Awaitable[None]] | None = None,
) -> Report:
    """Run every fixture commit through the real pipeline. Pass `oracle` (the same object given
    as `llm`) to have it queue the ground-truth answer before each commit."""
    repository_id = uuid.uuid4()
    full_name = f"fixture/clinica-demo-{repository_id.hex[:8]}"
    github = FakeGitHub()
    github_id = FIXTURE_GITHUB_ID_BASE + repository_id.int % 99_999
    github.add_repo(github_id, full_name)
    async with session_factory() as session, session.begin():
        session.add(
            Repository(
                id=repository_id,
                github_id=github_id,
                full_name=full_name,
                installation_id=1,
                default_branch="main",
            )
        )
    deps = WorkerDeps(session_factory=session_factory, github=github, llm=llm)

    report = Report(commits=[])
    tree: dict[str, str] = {}
    rule_ids: dict[str, uuid.UUID] = {}
    try:
        for index, commit in enumerate(fixture, start=1):
            if on_commit is not None:
                await on_commit(commit.name)
            sha = f"{index:040x}"
            previous = dict(tree)
            tree.update(commit.files)
            github.set_tree(full_name, sha, dict(tree))
            github.add_commit(
                full_name,
                sha,
                [
                    ChangedFile(
                        path,
                        "modified" if path in previous else "added",
                        blob_sha=blob_sha(content.encode()),
                        patch=_patch(previous.get(path, ""), content),
                    )
                    for path, content in commit.files.items()
                ],
            )
            async with session_factory() as session, session.begin():
                session.add(Commit(repository_id=repository_id, sha=sha, message=commit.message))
                session.add(
                    AgentSession(
                        repository_id=repository_id,
                        commit_sha=sha,
                        session_ref=commit.name,
                        excerpts=[
                            {
                                "role": "user",
                                "kind": "intent",
                                "text": text,
                                "timestamp": "2026-10-01T10:00:00.000Z",
                                "files": list(commit.files),
                            }
                            for text in commit.session
                        ],
                    )
                )
            if oracle is not None:
                oracle.queue(_oracle_response(commit, rule_ids))

            await indexing.analyze_commit(deps, repository_id, sha, True)  # symbols, as in prod
            summary = await extract_rules(deps, repository_id, sha)
            report.commits.append(
                await _score(session_factory, commit, summary.status, summary.rule_ids, rule_ids)
            )
        (
            report.model_calls,
            report.input_tokens,
            report.output_tokens,
            report.cost_usd,
        ) = await _usage(session_factory, repository_id)
    finally:
        if not keep_data:
            await _cleanup(session_factory, repository_id)
    return report


def _patch(old: str, new: str) -> str:
    import difflib

    lines = difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=3)
    return "\n".join(list(lines)[2:])


async def _score(
    session_factory: async_sessionmaker[AsyncSession],
    commit: FixtureCommit,
    status: str,
    touched_ids: list[uuid.UUID],
    rule_ids: dict[str, uuid.UUID],
) -> CommitResult:
    async with session_factory() as session:
        touched = (
            list(await session.scalars(select(Rule).where(Rule.id.in_(touched_ids))))
            if touched_ids
            else []
        )
    result = CommitResult(name=commit.name, status=status)
    used: set[uuid.UUID] = set()
    for expected in commit.expected:
        event = EventResult(expected)
        candidates = [r for r in touched if r.id not in used or expected.action == "update"]
        found = next(
            (r for r in candidates if _statement_matches(r.statement, expected.match)), None
        )
        if found is not None:
            event.matched = True
            used.add(found.id)
            event.rationale_ok = _rationale_ok(expected.rationale, found.rationale)
            if expected.updates:
                event.update_ok = found.id == rule_ids.get(expected.updates)
                rule_ids[expected.id] = rule_ids.get(expected.updates, found.id)
            else:
                rule_ids[expected.id] = found.id
        result.events.append(event)
    result.extra_rules = len([r for r in touched if r.id not in used])
    result.false_positive = not commit.expected and bool(touched)
    return result


async def _usage(
    session_factory: async_sessionmaker[AsyncSession], repository_id: uuid.UUID
) -> tuple[int, int, int, float | None]:
    async with session_factory() as session:
        row = (
            await session.execute(
                select(
                    func.count(LlmCall.id),
                    func.coalesce(func.sum(LlmCall.input_tokens), 0),
                    func.coalesce(func.sum(LlmCall.output_tokens), 0),
                    func.sum(LlmCall.cost_usd),
                    func.count(LlmCall.cost_usd),
                ).where(LlmCall.repository_id == repository_id)
            )
        ).one()
    calls, tokens_in, tokens_out, cost, priced = row
    return (
        int(calls),
        int(tokens_in),
        int(tokens_out),
        float(cost) if priced and cost is not None else None,
    )


async def _cleanup(
    session_factory: async_sessionmaker[AsyncSession], repository_id: uuid.UUID
) -> None:
    async with session_factory() as session, session.begin():
        await session.execute(delete(LlmCall).where(LlmCall.repository_id == repository_id))
        await session.execute(delete(Rule).where(Rule.repository_id == repository_id))
        await session.execute(delete(Symbol).where(Symbol.repository_id == repository_id))
        await session.execute(
            delete(AgentSession).where(AgentSession.repository_id == repository_id)
        )
        await session.execute(delete(Commit).where(Commit.repository_id == repository_id))
        await session.execute(delete(Repository).where(Repository.id == repository_id))
