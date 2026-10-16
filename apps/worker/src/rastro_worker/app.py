import asyncio
import sys

import procrastinate

from rastro_db import psycopg_conninfo

# psycopg cannot use the default Proactor event loop on Windows (dev machines only).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def build_app(connector: procrastinate.BaseConnector | None = None) -> procrastinate.App:
    """Create the Procrastinate app. Tests pass an in-memory connector."""
    if connector is None:
        connector = procrastinate.PsycopgConnector(conninfo=psycopg_conninfo())
    return procrastinate.App(connector=connector, import_paths=["rastro_worker.tasks"])


app = build_app()
