"""Container healthcheck: exit 0 when at least one Procrastinate worker is registered."""

import sys

import psycopg

from rastro_db import psycopg_conninfo


def main() -> int:
    try:
        with psycopg.connect(psycopg_conninfo(), connect_timeout=3) as conn:
            row = conn.execute("SELECT count(*) FROM procrastinate_workers").fetchone()
    except psycopg.Error as exc:
        print(f"unhealthy: {exc}", file=sys.stderr)
        return 1
    return 0 if row is not None and row[0] > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
