import asyncio
import sys

import procrastinate

from rastro_db import psycopg_conninfo
from rastro_worker.settings import RecoverySettings

# psycopg cannot use the default Proactor event loop on Windows (dev machines only).
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def build_app(connector: procrastinate.BaseConnector | None = None) -> procrastinate.App:
    """Create the Procrastinate app. Tests pass an in-memory connector."""
    if connector is None:
        connector = procrastinate.PsycopgConnector(conninfo=psycopg_conninfo())
    recovery = RecoverySettings()
    return procrastinate.App(
        connector=connector,
        import_paths=["rastro_worker.tasks"],
        # `procrastinate worker` uses these; the stalled timeout also drives `recover_stalled_jobs`.
        worker_defaults={
            "update_heartbeat_interval": recovery.worker_heartbeat_interval_seconds,
            "stalled_worker_timeout": recovery.worker_stalled_timeout_seconds,
        },
    )


app = build_app()
