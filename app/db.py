from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


def make_pool(database_url: str) -> ConnectionPool:
    """Connections to the ingest database. The API only reads it, so every connection is
    opened read-only: a stray write fails instead of touching the ingest's tables."""
    return ConnectionPool(
        database_url, min_size=1, max_size=10, open=False,
        kwargs={
            "autocommit": True, "row_factory": dict_row,
            "options": "-c default_transaction_read_only=on",
        },
    )
