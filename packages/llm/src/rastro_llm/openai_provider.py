from typing import Any

import openai
from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from rastro_llm.provider import LLMError, LLMQuotaError, LLMTransientError, Usage, UsageSink
from rastro_llm.settings import LLMSettings

_QUOTA_CODES = frozenset(
    {"insufficient_quota", "credit_balance_exhausted", "billing_hard_limit_reached"}
)


def _is_quota(error: Exception) -> bool:
    """OpenAI answers an empty balance with HTTP 429, like a real rate limit, but with one of
    these error codes; only the code tells them apart."""
    if not isinstance(error, openai.RateLimitError):
        return False
    body = error.body if isinstance(error.body, dict) else {}
    detail = body.get("error", body)
    codes = {error.code, detail.get("code") if isinstance(detail, dict) else None}
    return bool(codes & _QUOTA_CODES)


def _wrap(error: Exception) -> LLMError:
    """Quota exhaustion is permanent; rate limits, timeouts, connection problems and provider 5xx
    are transient."""
    if _is_quota(error):
        return LLMQuotaError(f"{type(error).__name__}: {error}")
    transient = isinstance(
        error, openai.RateLimitError | openai.APIConnectionError | openai.InternalServerError
    )  # APITimeoutError is an APIConnectionError
    message = f"{type(error).__name__}: {error}"
    return LLMTransientError(message) if transient else LLMError(message)


class OpenAIProvider:
    """`LLMProvider` on the OpenAI API: Structured Outputs via the SDK `parse` method."""

    def __init__(
        self,
        settings: LLMSettings,
        *,
        usage_sink: UsageSink | None = None,
        client: AsyncOpenAI | None = None,
    ) -> None:
        if not settings.configured:
            raise LLMError("Set OPENAI_API_KEY, LLM_MODEL and EMBEDDING_MODEL")
        assert settings.openai_api_key is not None  # for type checkers; checked by `configured`
        self._settings = settings
        self._client = client or AsyncOpenAI(api_key=settings.openai_api_key.get_secret_value())
        self._usage_sink = usage_sink

    async def extract[T: BaseModel](
        self,
        prompt: str,
        schema: type[T],
        *,
        purpose: str = "extraction",
        metadata: dict[str, Any] | None = None,
    ) -> T:
        model = (
            self._settings.impact_model() if purpose == "impact" else self._settings.llm_model
        ) or ""
        try:
            completion = await self._client.chat.completions.parse(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                response_format=schema,
            )
        except (openai.OpenAIError, ValidationError) as error:
            raise _wrap(error) from error

        usage = completion.usage
        await self._record(
            Usage(
                purpose=purpose,
                model=model,
                input_tokens=usage.prompt_tokens if usage else 0,
                output_tokens=usage.completion_tokens if usage else 0,
                cost_usd=self._cost(
                    usage.prompt_tokens if usage else 0,
                    usage.completion_tokens if usage else 0,
                ),
                metadata=metadata or {},
            )
        )
        message = completion.choices[0].message
        if message.refusal:
            raise LLMError(f"model refused: {message.refusal}")
        if message.parsed is None:
            raise LLMError("model returned no parsed output")
        return message.parsed

    async def embed(
        self, texts: list[str], *, metadata: dict[str, Any] | None = None
    ) -> list[list[float]]:
        if not texts:
            return []
        model = self._settings.embedding_model or ""
        try:
            response = await self._client.embeddings.create(
                model=model, input=texts, dimensions=self._settings.embedding_dimensions
            )
        except openai.OpenAIError as error:
            raise _wrap(error) from error
        tokens = response.usage.prompt_tokens if response.usage else 0
        price = self._settings.embedding_price_per_mtok
        await self._record(
            Usage(
                purpose="embedding",
                model=model,
                input_tokens=tokens,
                output_tokens=0,
                cost_usd=None if price is None else tokens * price / 1_000_000,
                metadata=metadata or {},
            )
        )
        return [item.embedding for item in sorted(response.data, key=lambda d: d.index)]

    def _cost(self, input_tokens: int, output_tokens: int) -> float | None:
        s = self._settings
        if s.llm_price_input_per_mtok is None or s.llm_price_output_per_mtok is None:
            return None
        return (
            input_tokens * s.llm_price_input_per_mtok + output_tokens * s.llm_price_output_per_mtok
        ) / 1_000_000

    async def _record(self, usage: Usage) -> None:
        if self._usage_sink is not None:
            await self._usage_sink(usage)
