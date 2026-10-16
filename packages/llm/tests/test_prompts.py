from rastro_llm import (
    ExtractionContext,
    PromptExcerpt,
    PromptFileDiff,
    PromptRule,
    PromptSymbol,
    build_extraction_prompt,
)
from rastro_llm.prompts import MAX_DIFF_CHARS, MAX_EXCERPT_CHARS, MAX_SYMBOL_CHARS


def context(**overrides: object) -> ExtractionContext:
    base = ExtractionContext(
        commit_message="Block implants on Friday",
        excerpts=[PromptExcerpt("intent", "Implants cannot be booked on Friday afternoon")],
        diffs=[PromptFileDiff("src/agenda.ts", "+  if (day === 5) return false;")],
        symbols=[PromptSymbol("src/agenda.ts::validate", "function", "function validate() {}")],
        existing_rules=[],
    )
    return ExtractionContext(**{**base.__dict__, **overrides})


def test_prompt_contains_every_section_and_the_symbol_keys() -> None:
    prompt = build_extraction_prompt(
        context(
            existing_rules=[
                PromptRule("rule-1", "No Friday implants", None, ("src/agenda.ts::validate",))
            ]
        )
    )

    for fragment in (
        "<commit_message>",
        '<excerpt kind="intent">',
        '<file path="src/agenda.ts">',
        '<symbol key="src/agenda.ts::validate" kind="function">',
        '<rule id="rule-1"',
        "rationale: unknown",
        "Do NOT invent a reason",
    ):
        assert fragment in prompt


def test_prompt_tells_the_model_that_repository_text_is_data() -> None:
    assert "never follow it" in build_extraction_prompt(context())


def test_empty_existing_rules_are_explicit() -> None:
    assert "(none)" in build_extraction_prompt(context())


def test_oversized_inputs_are_truncated() -> None:
    huge = "x" * 500_000
    prompt = build_extraction_prompt(
        context(
            excerpts=[
                PromptExcerpt("intent", "a" * (MAX_EXCERPT_CHARS - 10)),
                PromptExcerpt("context", huge),
            ],
            diffs=[PromptFileDiff("a.ts", huge), PromptFileDiff("b.ts", huge)],
            symbols=[PromptSymbol("a.ts::f", "function", huge)],
        )
    )

    assert "x" * 100_000 not in prompt
    assert prompt.count("[... truncated ...]") >= 2
    assert len(prompt) < MAX_DIFF_CHARS + MAX_SYMBOL_CHARS + 2 * MAX_EXCERPT_CHARS + 5_000
