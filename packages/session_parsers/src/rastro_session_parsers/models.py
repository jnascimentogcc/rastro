from dataclasses import dataclass
from typing import Literal

ExcerptKind = Literal["intent", "context", "assistant_note", "edit"]


@dataclass(frozen=True)
class Excerpt:
    """A piece of a session that is safe to send to the server (already redacted).

    kind:
      intent          human request that led to an edit of a file in the commit
      context         other human request of the same session
      assistant_note  assistant text that mentions a file in the commit
      edit            marker that the agent edited `files` (no content is kept)
    """

    role: Literal["user", "assistant"]
    kind: ExcerptKind
    text: str
    timestamp: str
    files: tuple[str, ...] = ()


@dataclass(frozen=True)
class CapturedSession:
    session_ref: str
    agent: str
    agent_version: str | None
    git_branch: str | None
    excerpts: tuple[Excerpt, ...]
