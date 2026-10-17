from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel


class LLMError(RuntimeError):
    """The model call failed, was refused, or returned something that does not validate."""


class LLMTransientError(LLMError):
    """A failure that is worth retrying later (rate limit, timeout, connection, provider 5xx),
    as opposed to a refusal or an invalid answer, which a retry will not fix."""


class LLMQuotaError(LLMError):
    """The account has no credit or quota left. Not transient: retrying changes nothing until
    someone tops it up, so jobs stop at once and mark the work as skipped for a later re-run."""


@dataclass(frozen=True)
class Usage:
    """One model call, as recorded for cost control (spec section 11)."""

    purpose: str  # "extraction", "impact", "embedding"...
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float | None  # None when no price is configured for the model
    metadata: dict[str, Any] = field(default_factory=dict)


UsageSink = Callable[[Usage], Awaitable[None]]


class LLMProvider(Protocol):
    """The only way the rest of the code talks to a model (spec section 7.5.1).

    Nothing outside `rastro_llm` imports a vendor SDK, so providers can be swapped and tests
    can use `FakeProvider` without keys or costs.
    """

    async def extract[T: BaseModel](
        self,
        prompt: str,
        schema: type[T],
        *,
        purpose: str = "extraction",
        metadata: dict[str, Any] | None = None,
    ) -> T:
        """Structured output validated against `schema`. Raises LLMError on refusal/invalid."""
        ...

    async def embed(
        self, texts: list[str], *, metadata: dict[str, Any] | None = None
    ) -> list[list[float]]: ...
