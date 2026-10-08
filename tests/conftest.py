"""Tests run against a real PostgreSQL: TEST_DATABASE_URL if set, otherwise a throwaway
container started through testcontainers (needs Docker). The `lots` table comes from
tests/schema.sql, a copy of the tables the ingest worker creates in the API database."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

from app.config import Settings
from app.db import run_migrations
from app.main import create_app

SCHEMA = Path(__file__).parent / "schema.sql"


@pytest.fixture(scope="session")
def db_url() -> Iterator[str]:
    url = os.environ.get("TEST_DATABASE_URL")
    if url:
        yield url
        return
    from testcontainers.community.postgres import PostgresContainer

    with PostgresContainer("postgres:17-alpine", driver=None) as pg:
        yield pg.get_connection_url()


@pytest.fixture
def conn(db_url: str) -> Iterator[psycopg.Connection]:
    """A fresh database for every test: the synced `lots` table from schema.sql and this
    repo's `app` schema from its migrations."""
    with psycopg.connect(db_url, autocommit=True, row_factory=dict_row) as c:
        c.execute("DROP SCHEMA IF EXISTS app CASCADE; DROP SCHEMA public CASCADE; "
                  "CREATE SCHEMA public")
        c.execute(SCHEMA.read_text())
        run_migrations(db_url)
        yield c


class FakeMailer:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send(self, to: str, subject: str, body: str) -> None:
        self.sent.append((to, subject, body))

    def last_link(self, path: str) -> str:
        """The token in the last mail's link for `path`, e.g. '/verify-email'."""
        body = self.sent[-1][2]
        start = body.index(f"{path}?token=") + len(f"{path}?token=")
        return body[start:].split()[0]

    def last_code(self) -> str:
        """The six-digit confirmation code in the last mail."""
        return re.search(r"\b[0-9]{6}\b", self.sent[-1][2]).group()


@pytest.fixture
def settings(db_url: str) -> Settings:
    return Settings(
        _env_file=None, database_url=db_url, web_url="https://web.test",
        internal_api_key="k" * 32, check_breached_passwords=False,
        alerts_interval_seconds=0,  # tests run the alert job themselves (alerts.tick)
    )


@pytest.fixture
def mailer() -> FakeMailer:
    return FakeMailer()


@pytest.fixture
def http_handler():
    """Tests replace `handler.fn` to script outbound HTTP (Turnstile, HIBP, OAuth)."""
    class Handler:
        fn = staticmethod(lambda request: httpx.Response(500))

        def __call__(self, request: httpx.Request) -> httpx.Response:
            return self.fn(request)
    return Handler()


@pytest.fixture
def client(conn, settings, mailer, http_handler) -> Iterator[TestClient]:
    http = httpx.Client(transport=httpx.MockTransport(http_handler))
    app = create_app(settings, mailer=mailer, http=http)
    with TestClient(app) as tc:
        yield tc


def insert_lot(conn: psycopg.Connection, lot_number: str, **overrides: Any) -> None:
    row: dict[str, Any] = {
        "source": "copart", "lot_number": lot_number, "year": 2020, "make": "TOYOTA",
        "model_group": "CAMRY", "vin": f"VIN{lot_number:0>14}", "sale_at": "2026-10-01T15:00Z",
        "high_bid": 1500, "est_retail_value": 12000, "repair_cost": 4000,
        "first_seen_at": "2026-09-01T00:00Z", "last_changed_at": "2026-09-10T00:00Z",
        "removed_at": None,
    }
    row.update(overrides)
    cols = ", ".join(row)
    params = ", ".join(f"%({k})s" for k in row)
    conn.execute(f"INSERT INTO lots ({cols}) VALUES ({params})", row)
