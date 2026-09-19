def test_app_schema_is_created_by_migrations(conn):
    tables = {
        r["table_name"] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'app'"
        )
    }
    assert {"users", "identities", "sessions", "tokens", "oauth_states", "throttle",
            "alembic_version"} <= tables
    assert conn.execute("SELECT version_num FROM app.alembic_version").fetchone() == {
        "version_num": "0001"
    }


def test_cli_takes_database_url_from_settings(conn, db_url, monkeypatch):
    """`alembic upgrade head` from a shell reads DATABASE_URL like the app (env or .env)."""
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    from app.config import get_settings

    root = Path(__file__).resolve().parent.parent
    conn.execute("DROP SCHEMA app CASCADE")
    monkeypatch.setenv("DATABASE_URL", db_url)
    get_settings.cache_clear()
    try:
        command.upgrade(Config(str(root / "alembic.ini")), "head")   # no database_url attribute
    finally:
        get_settings.cache_clear()
    assert conn.execute("SELECT version_num FROM app.alembic_version").fetchone() == {
        "version_num": "0001"
    }
