import uuid
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

MAX_TOTAL_TEXT_CHARS = 200_000


class ExcerptIn(BaseModel):
    role: Literal["user", "assistant"]
    kind: Literal["intent", "context", "assistant_note", "edit"]
    text: str = Field(max_length=20_000)
    timestamp: str = Field(max_length=64)
    files: list[str] = Field(default_factory=list, max_length=100)


class SessionCaptureIn(BaseModel):
    """Payload sent by `rastro capture` for one session linked to one commit."""

    commit_sha: str = Field(pattern=r"^[0-9a-fA-F]{7,64}$")
    session_ref: str = Field(min_length=1, max_length=200)
    agent: str = Field(default="claude-code", max_length=50)
    agent_version: str | None = Field(default=None, max_length=50)
    git_branch: str | None = Field(default=None, max_length=300)
    excerpts: list[ExcerptIn] = Field(max_length=1000)

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
