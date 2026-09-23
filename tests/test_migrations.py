def test_app_schema_is_created_by_migrations(conn):
    tables = {
        r["table_name"] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'app'"
        )
    }
    assert {"users", "email_codes", "identities", "sessions", "tokens", "oauth_states", "throttle",
            "alembic_version"} <= tables
    assert conn.execute("SELECT version_num FROM app.alembic_version").fetchone() == {
        "version_num": "0002"
    }
