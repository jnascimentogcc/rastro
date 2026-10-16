"""`python -m rastro_worker.evaluate fixtures/clinica-demo [--oracle]`"""

import argparse
import asyncio
import sys
from pathlib import Path

from rastro_db import create_engine, create_session_factory
from rastro_llm import FakeProvider, LLMSettings, OpenAIProvider
from rastro_worker.deps import usage_recorder
from rastro_worker.evaluation import format_report, load_fixture, run_evaluation


async def main_async(fixture_path: Path, oracle: bool, keep: bool) -> int:
    engine = create_engine()
    factory = create_session_factory(engine)
    try:
        fixture = load_fixture(fixture_path)
        if oracle:
            fake = FakeProvider(usage_sink=usage_recorder(factory))
            report = await run_evaluation(factory, fixture, llm=fake, oracle=fake, keep_data=keep)
        else:
            settings = LLMSettings()
            if not settings.configured:
                print("Set OPENAI_API_KEY, LLM_MODEL and EMBEDDING_MODEL (or use --oracle).")
                return 2
            provider = OpenAIProvider(settings, usage_sink=usage_recorder(factory))

            async def progress(name: str) -> None:
                print(f"  analysing {name} ...", flush=True)

            report = await run_evaluation(
                factory, fixture, llm=provider, keep_data=keep, on_commit=progress
            )
        print(format_report(report))
        return 0 if report.passed else 1
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(prog="rastro_worker.evaluate")
    parser.add_argument("fixture", type=Path, help="fixture directory, e.g. fixtures/clinica-demo")
    parser.add_argument("--oracle", action="store_true", help="replay the ground truth, no model")
    parser.add_argument(
        "--keep", action="store_true", help="keep the evaluation data in the database"
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(main_async(args.fixture, args.oracle, args.keep)))


if __name__ == "__main__":
    main()
