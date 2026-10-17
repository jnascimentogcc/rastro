"""Idempotent Procrastinate schema setup: `python -m rastro_worker.schema`.

`procrastinate schema --apply` fails on a database that already has the schema, which makes it
unusable as a deploy step. This applies the schema only when the jobs table is missing.
"""

import asyncio
import sys

import psycopg

from rastro_db import psycopg_conninfo
from rastro_worker.app import app

# The schema is created inside one transaction, so a table that exists means a complete schema.
_JOBS_TABLE = "procrastinate_jobs"


async def ensure_schema(conninfo: str | None = None) -> bool:
    """Apply the schema if it is missing. Returns True when it was applied."""
    async with await psycopg.AsyncConnection.connect(conninfo or psycopg_conninfo()) as conn:
        cur = await conn.execute("SELECT to_regclass(%s)", (_JOBS_TABLE,))
        row = await cur.fetchone()
        if row is not None and row[0] is not None:
            return False
    async with app.open_async():
        await app.schema_manager.apply_schema_async()
    return True


def main() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    applied = asyncio.run(ensure_schema())
    print("procrastinate schema applied" if applied else "procrastinate schema already present")


if __name__ == "__main__":
    main()
