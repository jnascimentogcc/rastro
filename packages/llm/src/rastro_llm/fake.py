"""Offline provider for tests and evaluations of the scoring logic: no key, no network, no cost."""

import hashlib
import math
import re
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from rastro_llm.provider import LLMError, Usage, UsageSink

Responder = Callable[[str, type[BaseModel]], BaseModel]


@dataclass(frozen=True)
class RecordedCall:
    purpose: str
    prompt: str
    schema: type[BaseModel]
    metadata: dict[str, Any]


def hashing_embedding(text: str, dimensions: int) -> list[float]:
    """Deterministic bag-of-words embedding: texts sharing words get a high cosine similarity,
    which is enough to exercise similarity-based code paths."""
    vector = [0.0] * dimensions
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        digest = hashlib.blake2b(word.encode(), digest_size=4).digest()
        vector[int.from_bytes(digest, "big") % dimensions] += 1.0
    norm = math.sqrt(sum(v * v for v in vector)) or 1.0
    return [v / norm for v in vector]


class FakeProvider:
    """Replays queued responses (in order) or computes them with `responder`."""

    def __init__(
        self,
        responses: list[BaseModel | Exception] | None = None,
        *,
        responder: Responder | None = None,
        dimensions: int = 1024,
        usage_sink: UsageSink | None = None,
    ) -> None:
        self._queue: deque[BaseModel | Exception] = deque(responses or [])
        self.responder = responder
        self._dimensions = dimensions
        self._usage_sink = usage_sink
        self.calls: list[RecordedCall] = []
        self.embedded: list[str] = []

    def queue(self, response: BaseModel | Exception) -> None:
        self._queue.append(response)

    async def extract[T: BaseModel](
        self,
        prompt: str,
        schema: type[T],
        *,
        purpose: str = "extraction",
        metadata: dict[str, Any] | None = None,
    ) -> T:
        self.calls.append(RecordedCall(purpose, prompt, schema, metadata or {}))
        if self._usage_sink is not None:
            await self._usage_sink(
                Usage(purpose, "fake", len(prompt) // 4, 50, 0.0, metadata or {})
            )
        if self._queue:
            item = self._queue.popleft()
            if isinstance(item, Exception):
                raise item
            response = item
        elif self.responder is not None:
            response = self.responder(prompt, schema)
        else:
            raise LLMError("FakeProvider has no response queued")
        if not isinstance(response, schema):
            raise LLMError(f"expected {schema.__name__}, got {type(response).__name__}")
        return response

    async def embed(
        self, texts: list[str], *, metadata: dict[str, Any] | None = None
    ) -> list[list[float]]:
        self.embedded.extend(texts)
        return [hashing_embedding(text, self._dimensions) for text in texts]
