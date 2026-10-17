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


Risk = Literal["breaks", "changes", "inconsistent_duplicate", "none"]


class ImpactAssessment(BaseModel):
    """Whether a pull request can violate or change one business rule (spec section 7.7)."""

    # Reasoning comes first: the model has to commit to whether behaviour changes, and to a
    # concrete example, before it is allowed to name a risk.
    behavior_changed: bool = Field(
        description="True only if the new code behaves differently from the old code for some "
        "input that matters to the rule; false for refactors, renames, formatting, constants"
    )
    counterexample: str | None = Field(
        description="One concrete input where old and new code differ, and how; null if none"
    )
    risk: Risk = Field(
        description="breaks: violates the rule; changes: alters its conditions or behaviour; "
        "inconsistent_duplicate: changes the rule's logic here but not in an unchanged copy; "
        "none: the rule is not affected"
    )
    confidence: float = Field(description="0.0 to 1.0")
    explanation: str = Field(
        description="One or two sentences saying what in the change affects the rule"
    )
