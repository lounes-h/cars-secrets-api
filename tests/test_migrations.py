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


def test_cli_takes_database_url_from_dotenv(conn, db_url, monkeypatch, tmp_path):
    """`alembic upgrade head` from a shell reads DATABASE_URL from .env, like the app."""
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    from app.config import get_settings

    root = Path(__file__).resolve().parent.parent
    conn.execute("DROP SCHEMA app CASCADE")
    (tmp_path / ".env").write_text(f"DATABASE_URL={db_url}\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    get_settings.cache_clear()
    try:
        cfg = Config(str(root / "alembic.ini"))                      # no database_url attribute
        cfg.set_main_option("script_location", str(root / "migrations"))
        command.upgrade(cfg, "head")
    finally:
        get_settings.cache_clear()
    assert conn.execute("SELECT version_num FROM app.alembic_version").fetchone() == {
        "version_num": "0001"
    }
