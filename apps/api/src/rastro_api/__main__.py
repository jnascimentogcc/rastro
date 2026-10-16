"""Run the API: `uv run python -m rastro_api [--reload]`.

On Windows psycopg (used by the job queue) cannot run on the default Proactor event loop, so
the selector policy is installed before uvicorn creates its loop.
"""

import argparse
import asyncio
import sys

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(prog="rastro_api")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    uvicorn.run(
        "rastro_api.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        loop="none" if sys.platform == "win32" else "auto",
    )


if __name__ == "__main__":
    main()
