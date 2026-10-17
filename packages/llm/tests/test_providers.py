from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest
from pydantic import SecretStr

from rastro_llm import (
    ExtractedRule,
    FakeProvider,
    LLMError,
    LLMQuotaError,
    LLMSettings,
    LLMTransientError,
    OpenAIProvider,
    RuleExtraction,
    Usage,
    hashing_embedding,
)

RULE = ExtractedRule(
    action="create",
    existing_rule_id=None,
    statement="Implants cannot be booked on Friday afternoon",
    rationale=None,
    origin_quote=None,
    symbols=["src/agenda.ts::validate"],
    confidence=0.9,
)


def settings(**overrides: Any) -> LLMSettings:
    values: dict[str, Any] = {
        "openai_api_key": SecretStr("sk-test"),
        "llm_model": "model-a",
        "llm_model_impact": "model-b",
        "embedding_model": "embed-a",
        "llm_price_input_per_mtok": 2.0,
        "llm_price_output_per_mtok": 10.0,
        "embedding_price_per_mtok": 0.1,
    }
    return LLMSettings(**(values | overrides))


def _request() -> Any:  # the OpenAI SDK types its arguments with its own httpx build
    return httpx.Request("POST", "https://x")


def _response(status: int) -> Any:
    return httpx.Response(status, request=_request())


class FakeOpenAI:
    """Just enough of AsyncOpenAI for the provider: chat.completions.parse and embeddings."""

    def __init__(self, *, parsed: Any = None, refusal: str | None = None) -> None:
        self.parse_calls: list[dict[str, Any]] = []
        self.embed_calls: list[dict[str, Any]] = []
        message = SimpleNamespace(parsed=parsed, refusal=refusal)
        usage = SimpleNamespace(prompt_tokens=1000, completion_tokens=200)

        async def parse(**kwargs: Any) -> Any:
            self.parse_calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)

        async def create(**kwargs: Any) -> Any:
            self.embed_calls.append(kwargs)
            data = [
                SimpleNamespace(index=1, embedding=[0.2]),
                SimpleNamespace(index=0, embedding=[0.1]),
            ]
            return SimpleNamespace(data=data, usage=SimpleNamespace(prompt_tokens=50))

        self.chat = SimpleNamespace(completions=SimpleNamespace(parse=parse))
        self.embeddings = SimpleNamespace(create=create)


async def test_openai_provider_parses_with_the_schema_and_records_cost() -> None:
    recorded: list[Usage] = []

    async def sink(usage: Usage) -> None:
        recorded.append(usage)

    client = FakeOpenAI(parsed=RuleExtraction(rules=[RULE]))
    provider = OpenAIProvider(settings(), usage_sink=sink, client=client)  # type: ignore[arg-type]

    result = await provider.extract("prompt", RuleExtraction, metadata={"repository_id": "r1"})

    assert result.rules == [RULE]
    assert client.parse_calls[0]["model"] == "model-a"
    assert client.parse_calls[0]["response_format"] is RuleExtraction
    assert recorded == [
        Usage(
            "extraction",
            "model-a",
            1000,
            200,
            (1000 * 2.0 + 200 * 10.0) / 1_000_000,
            {"repository_id": "r1"},
        )
    ]


async def test_impact_purpose_uses_the_impact_model() -> None:
    client = FakeOpenAI(parsed=RuleExtraction(rules=[]))
    provider = OpenAIProvider(settings(), client=client)  # type: ignore[arg-type]

    await provider.extract("p", RuleExtraction, purpose="impact")

    assert client.parse_calls[0]["model"] == "model-b"


async def test_cost_is_unknown_without_prices() -> None:
    recorded: list[Usage] = []

    async def sink(usage: Usage) -> None:
        recorded.append(usage)

    provider = OpenAIProvider(
        settings(llm_price_input_per_mtok=None),
        usage_sink=sink,
        client=FakeOpenAI(parsed=RuleExtraction(rules=[])),  # type: ignore[arg-type]
    )

    await provider.extract("p", RuleExtraction)

    assert recorded[0].cost_usd is None


async def test_refusal_and_missing_output_raise_llm_error() -> None:
    refused = OpenAIProvider(settings(), client=FakeOpenAI(refusal="no"))  # type: ignore[arg-type]
    empty = OpenAIProvider(settings(), client=FakeOpenAI(parsed=None))  # type: ignore[arg-type]

    with pytest.raises(LLMError, match="refused"):
        await refused.extract("p", RuleExtraction)
    with pytest.raises(LLMError, match="no parsed"):
        await empty.extract("p", RuleExtraction)


async def test_embed_requests_configured_dimensions_and_keeps_input_order() -> None:
    recorded: list[Usage] = []

    async def sink(usage: Usage) -> None:
        recorded.append(usage)

    client = FakeOpenAI()
    provider = OpenAIProvider(settings(), usage_sink=sink, client=client)  # type: ignore[arg-type]

    vectors = await provider.embed(["a", "b"])

    assert vectors == [[0.1], [0.2]]
    assert client.embed_calls[0]["dimensions"] == 1024
    assert client.embed_calls[0]["model"] == "embed-a"
    assert recorded[0].purpose == "embedding" and recorded[0].cost_usd == pytest.approx(5e-6)
    assert await provider.embed([]) == []


def test_unconfigured_provider_is_refused() -> None:
    with pytest.raises(LLMError):
        OpenAIProvider(LLMSettings(openai_api_key=None, llm_model=None, embedding_model=None))


async def test_fake_provider_replays_queued_responses_and_errors() -> None:
    fake = FakeProvider([RuleExtraction(rules=[RULE]), LLMError("boom")])

    assert (await fake.extract("first", RuleExtraction)).rules == [RULE]
    with pytest.raises(LLMError, match="boom"):
        await fake.extract("second", RuleExtraction)
    with pytest.raises(LLMError, match="no response"):
        await fake.extract("third", RuleExtraction)
    assert [c.prompt for c in fake.calls] == ["first", "second", "third"]


def test_hashing_embedding_is_deterministic_and_reflects_shared_words() -> None:
    a = hashing_embedding("book implants friday", 64)
    b = hashing_embedding("book implants friday", 64)
    close = hashing_embedding("implants friday booking", 64)
    far = hashing_embedding("completely unrelated words here", 64)

    def cosine(x: list[float], y: list[float]) -> float:
        return sum(i * j for i, j in zip(x, y, strict=True))

    assert a == b
    assert cosine(a, close) > cosine(a, far)


@pytest.mark.parametrize(
    ("error", "transient"),
    [
        (openai.RateLimitError("slow down", response=_response(429), body=None), True),
        (openai.InternalServerError("oops", response=_response(503), body=None), True),
        (openai.APIConnectionError(request=_request()), True),
        (openai.APITimeoutError(request=_request()), True),
        (openai.AuthenticationError("bad key", response=_response(401), body=None), False),
        (openai.BadRequestError("bad", response=_response(400), body=None), False),
    ],
)
async def test_only_retryable_provider_errors_are_marked_transient(
    error: Exception, transient: bool
) -> None:
    class Failing(FakeOpenAI):
        def __init__(self) -> None:
            super().__init__()

            async def parse(**kwargs: Any) -> Any:
                raise error

            self.chat = SimpleNamespace(completions=SimpleNamespace(parse=parse))

    provider = OpenAIProvider(settings(), client=Failing())  # type: ignore[arg-type]

    with pytest.raises(LLMError) as raised:
        await provider.extract("p", RuleExtraction)

    assert isinstance(raised.value, LLMTransientError) is transient


def _rate_limit(code: str) -> openai.RateLimitError:
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    body = {"error": {"message": "limit", "type": "x", "code": code}}
    response = httpx.Response(429, request=request, json=body)
    # The SDK is typed against its own httpx copy; the real class is compatible at runtime.
    return openai.RateLimitError("limit", response=response, body=body["error"])  # type: ignore[arg-type]


def _failing_client(error: Exception) -> Any:
    async def fail(**kwargs: Any) -> Any:
        raise error

    return SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(parse=fail)),
        embeddings=SimpleNamespace(create=fail),
    )


@pytest.mark.parametrize("code", ["insufficient_quota", "credit_balance_exhausted"])
async def test_an_empty_balance_is_a_quota_error_and_not_a_transient_one(code: str) -> None:
    provider = OpenAIProvider(settings(), client=_failing_client(_rate_limit(code)))

    with pytest.raises(LLMQuotaError) as extract_error:
        await provider.extract("p", RuleExtraction)
    with pytest.raises(LLMQuotaError):
        await provider.embed(["text"])  # embeddings share the account

    assert not isinstance(extract_error.value, LLMTransientError)


async def test_a_real_rate_limit_stays_transient() -> None:
    provider = OpenAIProvider(
        settings(), client=_failing_client(_rate_limit("rate_limit_exceeded"))
    )

    with pytest.raises(LLMTransientError):
        await provider.extract("p", RuleExtraction)
