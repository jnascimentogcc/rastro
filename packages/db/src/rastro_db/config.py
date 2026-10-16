import os

DEFAULT_DATABASE_URL = "postgresql+asyncpg://rastro:rastro@localhost:5432/rastro"

# Must match the embedding model's `dimensions` parameter and the Alembic migration.
EMBEDDING_DIMENSIONS = 1024


def database_url() -> str:
    """SQLAlchemy async URL (asyncpg driver)."""
    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)


def psycopg_conninfo() -> str:
    """Plain libpq URL for libraries that do not use SQLAlchemy (Procrastinate)."""
    return database_url().replace("+asyncpg", "", 1)
