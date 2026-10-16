"""`python -m rastro_worker.evaluate fixtures/clinica-demo [--oracle] [--runs N] [--verbose]`

Models answer differently from run to run, so use `--runs 3` (or more) before trusting a number.
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from rastro_db import create_engine, create_session_factory
from rastro_llm import FakeProvider, LLMSettings, OpenAIProvider
from rastro_worker.deps import usage_recorder
from rastro_worker.evaluation import (
    SUCCESS_THRESHOLD,
    Report,
    format_report,
    load_fixture,
    run_evaluation,
)


async def main_async(fixture_path: Path, oracle: bool, keep: bool, verbose: bool, runs: int) -> int:
    settings = LLMSettings()
    if not oracle and not settings.configured:
        print("Set OPENAI_API_KEY, LLM_MODEL and EMBEDDING_MODEL (or use --oracle).")
        return 2

    engine = create_engine()
    factory = create_session_factory(engine)
    reports: list[Report] = []
    try:
        fixture = load_fixture(fixture_path)
        for number in range(1, runs + 1):
            if runs > 1:
                print(f"--- run {number}/{runs}", flush=True)
            if oracle:
                fake = FakeProvider(usage_sink=usage_recorder(factory))
                report = await run_evaluation(
                    factory, fixture, llm=fake, oracle=fake, keep_data=keep
                )
            else:
                provider = OpenAIProvider(settings, usage_sink=usage_recorder(factory))

                async def progress(name: str) -> None:
                    print(f"  analysing {name} ...", flush=True)

                report = await run_evaluation(
                    factory, fixture, llm=provider, keep_data=keep, on_commit=progress
                )
            reports.append(report)
            if runs == 1 or verbose:
                print(format_report(report, verbose=verbose))
    finally:
        await engine.dispose()

    if runs > 1:
        print("\nrun  success  recall  reasons  updates  false+  tokens in/out")
        for number, report in enumerate(reports, start=1):
            print(
                f"{number:>3}  {report.success_rate:>6.0%}  {report.recall:>6.0%}  "
                f"{report.rationale_accuracy:>7.0%}  {report.update_accuracy:>7.0%}  "
                f"{report.false_positive_commits:>5}  {report.input_tokens}/{report.output_tokens}"
            )
        mean = sum(r.success_rate for r in reports) / len(reports)
        worst = min(r.success_rate for r in reports)
        print(
            f"mean success {mean:.0%}, worst {worst:.0%} "
            f"(target {SUCCESS_THRESHOLD:.0%}) -> {'PASS' if mean >= SUCCESS_THRESHOLD else 'FAIL'}"
        )
        return 0 if mean >= SUCCESS_THRESHOLD else 1
    return 0 if reports[0].passed else 1


def main() -> None:
    parser = argparse.ArgumentParser(prog="rastro_worker.evaluate")
    parser.add_argument("fixture", type=Path, help="fixture directory, e.g. fixtures/clinica-demo")
    parser.add_argument("--oracle", action="store_true", help="replay the ground truth, no model")
    parser.add_argument("--keep", action="store_true", help="keep the data in the database")
    parser.add_argument("--verbose", action="store_true", help="print every rule produced")
    parser.add_argument("--runs", type=int, default=1, help="repeat and average (default 1)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING, format="    ! %(message)s")
    sys.exit(asyncio.run(main_async(args.fixture, args.oracle, args.keep, args.verbose, args.runs)))


if __name__ == "__main__":
    main()
