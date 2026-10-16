import procrastinate

from rastro_db import psycopg_conninfo


def build_app(connector: procrastinate.BaseConnector | None = None) -> procrastinate.App:
    """Create the Procrastinate app. Tests pass an in-memory connector."""
    if connector is None:
        connector = procrastinate.PsycopgConnector(conninfo=psycopg_conninfo())
    return procrastinate.App(connector=connector, import_paths=["rastro_worker.tasks"])


app = build_app()
