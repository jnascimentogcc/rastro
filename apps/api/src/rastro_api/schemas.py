import uuid
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

MAX_TOTAL_TEXT_CHARS = 200_000


class ExcerptIn(BaseModel):
    role: Literal["user", "assistant"]
    kind: Literal["intent", "context", "assistant_note", "edit"]
    text: str = Field(max_length=20_000)
    timestamp: str = Field(max_length=64)
    files: list[str] = Field(default_factory=list, max_length=100)


class ConsultedIn(BaseModel):
    """One time the agent hook told the agent about a file's rules (no rule text)."""

    at: str = Field(max_length=40)
    agent: str | None = Field(default=None, max_length=100)  # set for a subagent
    file: str = Field(default="", max_length=500)  # empty only for the session-start marker
    rule_ids: list[str] = Field(default_factory=list, max_length=20)
    via: Literal["read", "deny", "start"]  # "start": the hook ran when the session began

    @model_validator(mode="after")
    def _file_unless_start(self) -> "ConsultedIn":
        if self.via != "start" and not self.file:
            raise ValueError("an announcement needs the file it was about")
        return self

    @field_validator("rule_ids")
    @classmethod
    def _short_ids(cls, value: list[str]) -> list[str]:
        if any(len(item) > 64 for item in value):
            raise ValueError("rule ids are limited to 64 characters")
        return value


class SessionCaptureIn(BaseModel):
    """Payload sent by `rastro capture` for one session linked to one commit."""

    commit_sha: str = Field(pattern=r"^[0-9a-fA-F]{7,64}$")
    session_ref: str = Field(min_length=1, max_length=200)
    agent: str = Field(default="claude-code", max_length=50)
    agent_version: str | None = Field(default=None, max_length=50)
    git_branch: str | None = Field(default=None, max_length=300)
    excerpts: list[ExcerptIn] = Field(max_length=1000)
    consulted: list[ConsultedIn] = Field(
        default_factory=list, max_length=2000
    )  # older CLIs omit it
    # Did the agent hook run in this session? None = the CLI cannot tell (older CLI, no hook installed)
    agent_hook: bool | None = None

    @field_validator("commit_sha")
    @classmethod
    def _lower_sha(cls, value: str) -> str:
        return value.lower()

    @model_validator(mode="after")
    def _limit_total_size(self) -> "SessionCaptureIn":
        if sum(len(e.text) for e in self.excerpts) > MAX_TOTAL_TEXT_CHARS:
            raise ValueError(f"excerpts exceed {MAX_TOTAL_TEXT_CHARS} characters in total")
        return self


class SessionCaptureOut(BaseModel):
    id: uuid.UUID
    commit_sha: str
    session_ref: str
    excerpts: int
    created: bool


class WhoAmIOut(BaseModel):
    repository_id: uuid.UUID
    full_name: str


class AnchorOut(BaseModel):
    file_path: str
    qualified_name: str
    role: str  # primary | duplicate
    status: str  # active | removed
    start_line: int | None
    end_line: int | None


class OriginOut(BaseModel):
    commit_sha: str
    quote: str | None
    committed_at: datetime | None
    session_ref: str | None


Validation = Literal["validated", "unvalidated", "rejected"]
Verdict = Literal["confirmed", "corrected", "rejected", "rationale_added"]
MAX_REVIEW_TEXT = 500  # reviewer text is data shown to others: keep it short


class RuleOut(BaseModel):
    id: uuid.UUID
    statement: str
    rationale: str | None  # null = unknown origin, never guessed
    rationale_source: str | None = None  # conversation | human
    confidence: float
    status: str  # active | orphaned | superseded | rejected
    validation: Validation = "unvalidated"  # a human fact, separate from the model's confidence
    revision: int = 1
    validated_by: str | None = None
    validated_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    files: list[str]
    origin: OriginOut | None  # the earliest commit that produced the rule
    similarity: float | None = None  # only for semantic search


class ReviewOut(BaseModel):
    verdict: str
    rule_revision: int
    reviewer: str
    reviewer_source: str  # github (verified) | cli (not verified)
    statement_after: str | None
    rationale_after: str | None
    note: str | None
    created_at: datetime


class RuleDetailOut(RuleOut):
    origins: list[OriginOut]
    anchors: list[AnchorOut]
    reviews: list[ReviewOut] = Field(default_factory=list)  # newest first


class ReviewIn(BaseModel):
    verdict: Verdict
    reviewer: str = Field(min_length=1, max_length=200)
    statement: str | None = Field(default=None, max_length=MAX_REVIEW_TEXT)
    rationale: str | None = Field(default=None, max_length=MAX_REVIEW_TEXT)
    note: str | None = Field(default=None, max_length=MAX_REVIEW_TEXT)

    @field_validator("reviewer", "statement", "rationale", "note")
    @classmethod
    def _strip(cls, value: str | None) -> str | None:
        return (value.strip() or None) if value is not None else None

    @model_validator(mode="after")
    def _required_text(self) -> "ReviewIn":
        if not self.reviewer:
            raise ValueError("reviewer must not be blank")
        if self.verdict == "corrected" and not self.statement:
            raise ValueError("a correction needs the new statement")
        if self.verdict == "rationale_added" and not self.rationale:
            raise ValueError("adding a reason needs the reason")
        return self


class RuleListOut(BaseModel):
    items: list[RuleOut]
    total: int
    search: Literal["semantic", "text"] | None


class SnapshotAnchorOut(BaseModel):
    """Where a rule lives: path and qualified name only (lines describe the default branch, which
    the developer's checkout may not be on)."""

    file_path: str
    qualified_name: str
    role: str  # primary | duplicate


class SnapshotRuleOut(BaseModel):
    id: uuid.UUID
    statement: str
    rationale: str | None
    rationale_source: str | None = None
    validation: Validation
    revision: int
    validated_by: str | None = None
    validated_at: datetime | None = None
    anchors: list[SnapshotAnchorOut]


class RulesSnapshotOut(BaseModel):
    """Every active rule of a repository in one compact payload, for the local agent cache."""

    head_sha: str | None  # latest default-branch commit Rastro knows; the rules describe it
    generated_at: datetime
    truncated: bool
    rules: list[SnapshotRuleOut]


class MetricOut(BaseModel):
    id: str
    label: str
    numerator: int | None = None
    denominator: int | None = None
    value: float | None = None
    available: bool = True
    na_reason: str | None = None  # why a figure cannot be given: never shown as 0%
    small_sample: bool = False
    detail: dict[str, Any] = Field(default_factory=dict)


class ZoneOut(BaseModel):
    name: str
    symbols: int
    symbols_with_rule: int
    rules: int
    validated: int
    without_reason: int


class QueueItemOut(BaseModel):
    rule_id: uuid.UUID
    statement: str
    reasons: list[str]  # no_rationale | not_validated | validation_outdated
    callers: int
    command: str


class DefinitionOut(BaseModel):
    title: str
    formula: str
    source: str
    not_proof: str


class FigureOut(BaseModel):
    """A figure of a snapshot: the numbers only."""

    numerator: int | None = None
    denominator: int | None = None
    value: float | None = None
    available: bool = True
    small_sample: bool = False


class ComparedToOut(BaseModel):
    taken_on: date
    days_ago: int
    metrics: dict[str, FigureOut]


class HealthSnapshotOut(BaseModel):
    taken_on: date
    window_days: int
    definitions_version: int
    head_sha: str | None
    figures: dict[str, FigureOut]


class HealthHistoryOut(BaseModel):
    snapshots: list[HealthSnapshotOut]  # most recent first


class HealthOut(BaseModel):
    repository_id: uuid.UUID
    generated_at: datetime
    window_days: int
    head_sha: str | None
    definitions_version: int
    warnings: list[str]
    pipeline: dict[str, int]
    knowledge: list[MetricOut]
    process: list[MetricOut]
    intent: list[MetricOut]
    agents: list[MetricOut]
    zones: list[ZoneOut]
    queue: list[QueueItemOut]
    compared_to: ComparedToOut | None = None  # the oldest comparable snapshot of the window
    definitions: dict[str, DefinitionOut] | None = None
