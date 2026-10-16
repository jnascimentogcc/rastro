from typing import Literal

from pydantic import BaseModel, Field


class ExtractedRule(BaseModel):
    """One business rule found in a commit (spec section 7.5).

    Every field is required (OpenAI strict mode); optional ones are `X | None`.
    """

    action: Literal["create", "update", "none"]
    existing_rule_id: str | None = Field(
        description="id of the existing rule being updated; null unless action is 'update'"
    )
    statement: str = Field(description="The rule in business language, one sentence")
    rationale: str | None = Field(
        description="Why the rule exists according to the conversation; null if not stated"
    )
    origin_quote: str | None = Field(
        description="Short verbatim excerpt of the user's request that supports the rule"
    )
    symbols: list[str] = Field(
        description="Keys of the changed symbols that implement the rule, copied from the prompt"
    )
    confidence: float = Field(description="0.0 to 1.0")


class RuleExtraction(BaseModel):
    rules: list[ExtractedRule]
