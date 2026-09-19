"""Migrations for the API-owned `app` schema. The ingest repo owns everything in `public`."""

from alembic import context
from sqlalchemy import create_engine, text

from app.config import get_settings
from app.db import sqlalchemy_url

config = context.config
# Tests pass the URL in; the CLI reads DATABASE_URL like the app does (environment or .env).
url = config.attributes.get("database_url") or get_settings().database_url

engine = create_engine(sqlalchemy_url(url))
with engine.connect() as connection:
    # The version table lives in `app`, so the schema must exist before Alembic looks for it.
    connection.execute(text("CREATE SCHEMA IF NOT EXISTS app"))
    connection.commit()
    context.configure(connection=connection, target_metadata=None, version_table_schema="app")
    with context.begin_transaction():
        context.run_migrations()
