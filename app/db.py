from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


def make_pool(database_url: str, *, read_only: bool = True) -> ConnectionPool:
    """Connections to the shared database. The ingest tables are only ever read, so the
    default pool opens every connection read-only: a stray write fails instead of touching
    them. The `app` schema (accounts) is written through a second pool with read_only=False."""
    kwargs = {"autocommit": True, "row_factory": dict_row}
    if read_only:
        kwargs["options"] = "-c default_transaction_read_only=on"
    return ConnectionPool(database_url, min_size=1, max_size=10, open=False, kwargs=kwargs)


def sqlalchemy_url(url: str) -> str:
    """Alembic goes through SQLAlchemy, which needs the psycopg 3 driver spelled out."""
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def run_migrations(url: str) -> None:
    """Apply this repo's migrations (the `app` schema only) to the database at `url`."""
    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "migrations"))
    cfg.attributes["database_url"] = url
    command.upgrade(cfg, "head")
