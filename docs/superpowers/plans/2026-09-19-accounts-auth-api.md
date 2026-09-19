# Accounts and Authentication (API) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Dealers can register, verify their email, sign in with a password or Google/Apple, stay signed in, and reset a forgotten password, all through JSON endpoints under `/api/auth`.

**Architecture:** Accounts live in a new `app` schema of the ingest Postgres, migrated by this repo's own Alembic history. A second, writable connection pool serves the `app` schema while the existing read-only pool keeps serving `lots`. Flows are plain functions over a psycopg connection; outbound calls (Turnstile, Have I Been Pwned, OAuth token endpoints) go through one `httpx.Client` on `app.state` so tests swap in a `MockTransport`.

**Tech Stack:** Python 3.12, FastAPI, psycopg 3, Alembic + SQLAlchemy (migrations only), argon2-cffi, PyJWT[crypto], phonenumbers, httpx, pydantic `EmailStr` (email-validator), stdlib `smtplib`.

**Spec:** `docs/superpowers/specs/2026-09-19-accounts-auth-design.md`

## Global Constraints

- Python `>=3.12`, uv, ruff line length 100, rules `E F I B UP SIM`. `uv run ruff check app tests migrations` must pass.
- Tests run on a real Postgres (`TEST_DATABASE_URL` or testcontainers). Every test gets a fresh database: `public` from `tests/schema.sql`, `app` from Alembic.
- Ingest tables are never written. The read-only pool stays as is; only `app.*` is written, through the new pool.
- Only hashes of session / verification / reset tokens are stored. Tokens are `secrets.token_urlsafe(32)`.
- Passwords 10–128 characters, Argon2id. Breach check by HIBP range API, fail open.
- Register, resend-verification and forgot-password always answer `200 {"ok": true}`; never reveal whether an email exists.
- Error bodies: `{"detail": {"code": "...", "message": "..."}}`; field errors use FastAPI's list shape.
- Terminology: **lot**, not vehicle; **user**, not customer.
- Commit after every task with a `feat:`/`test:`/`docs:` message ending in `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

---

### Task 1: Dependencies, settings, writable pool, Alembic scaffolding and the `app` schema migration

**Files:**
- Modify: `pyproject.toml`
- Modify: `app/config.py`
- Modify: `app/db.py`
- Create: `alembic.ini`, `migrations/env.py`, `migrations/script.py.mako`, `migrations/versions/0001_accounts.py`
- Modify: `tests/conftest.py`
- Modify: `Dockerfile` (copy `alembic.ini` and `migrations`)
- Test: `tests/test_migrations.py`

**Interfaces:**
- Produces: `Settings` fields listed below; `make_pool(url, *, read_only=True)`; `run_migrations(url)`; `sqlalchemy_url(url)`.

- [ ] **Step 1: Add dependencies**

In `pyproject.toml` replace the `dependencies` list with:

```toml
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.30",
    "psycopg[binary,pool]>=3.2",
    "pydantic-settings>=2.4",
    "pydantic[email]>=2.8",
    "alembic>=1.13",
    "sqlalchemy>=2.0",
    "argon2-cffi>=23.1",
    "pyjwt[crypto]>=2.9",
    "phonenumbers>=8.13",
    "httpx>=0.27",
]
```

Remove `"httpx>=0.27"` from the `dev` group (it is a runtime dependency now). Change the ruff line under `[tool.ruff]` nothing; but add `migrations` to the CI/AGENTS ruff command later (Task 8). Run `uv sync`.

- [ ] **Step 2: Extend settings**

Replace `app/config.py` with:

```python
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # The ingest database (owned by cars-secrets-data-ingest). No default: it holds a password.
    database_url: str
    # Public origin of the web app: links in emails and OAuth redirect URIs point there.
    web_url: str = "http://localhost:3000"
    # Shared with the web server. When a request carries it in X-Internal-Key, the API trusts
    # X-Client-IP for rate limiting. Empty: the header is ignored.
    internal_api_key: str = Field(default="", repr=False)

    session_remember_seconds: int = 30 * 24 * 3600
    session_idle_seconds: int = 12 * 3600
    verify_token_seconds: int = 24 * 3600
    reset_token_seconds: int = 3600
    oauth_state_seconds: int = 600

    # Login failures in the window before a captcha is required.
    login_failures_per_email: int = 3
    login_failures_per_ip: int = 10
    throttle_window_seconds: int = 900

    # Cloudflare Turnstile; empty secret = captcha not enforced (development, tests).
    turnstile_secret: str = Field(default="", repr=False)
    check_breached_passwords: bool = True

    # Outgoing mail; without smtp_host, messages are logged instead of sent.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = Field(default="", repr=False)
    mail_from: str = "Car's Secrets <no-reply@localhost>"

    google_client_id: str = ""
    google_client_secret: str = Field(default="", repr=False)
    apple_client_id: str = ""
    apple_team_id: str = ""
    apple_key_id: str = ""
    apple_private_key: str = Field(default="", repr=False)  # PEM; "\n" escapes accepted


@lru_cache
def get_settings() -> Settings:
    return Settings()
```

- [ ] **Step 3: Writable pool and migrations helper**

Replace `app/db.py` with:

```python
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
```

- [ ] **Step 4: Alembic files**

`alembic.ini`:

```ini
[alembic]
script_location = migrations
```

`migrations/env.py`:

```python
"""Migrations for the API-owned `app` schema. The ingest repo owns everything in `public`."""

import os

from alembic import context
from sqlalchemy import create_engine, text

from app.db import sqlalchemy_url

config = context.config
url = config.attributes.get("database_url") or os.environ["DATABASE_URL"]

engine = create_engine(sqlalchemy_url(url))
with engine.connect() as connection:
    # The version table lives in `app`, so the schema must exist before Alembic looks for it.
    connection.execute(text("CREATE SCHEMA IF NOT EXISTS app"))
    connection.commit()
    context.configure(connection=connection, target_metadata=None, version_table_schema="app")
    with context.begin_transaction():
        context.run_migrations()
```

`migrations/script.py.mako`:

```mako
"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""

from alembic import op

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    op.execute("")


def downgrade() -> None:
    op.execute("")
```

`migrations/versions/0001_accounts.py`:

```python
"""Accounts: users, social identities, sessions, one-time tokens, OAuth states, throttling.

Revision ID: 0001
Revises:
Create Date: 2026-09-19
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.users (
            id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            email             text NOT NULL UNIQUE,          -- lowercased, trimmed
            first_name        text NOT NULL,
            last_name         text NOT NULL,
            phone             text,                          -- E.164
            password_hash     text,                          -- NULL for social-only accounts
            email_verified_at timestamptz,
            terms_accepted_at timestamptz,
            created_at        timestamptz NOT NULL DEFAULT now(),
            updated_at        timestamptz NOT NULL DEFAULT now()
        );

        CREATE TABLE app.identities (
            provider   text NOT NULL,                        -- 'google' | 'apple'
            subject    text NOT NULL,                        -- the provider's stable user id
            user_id    uuid NOT NULL REFERENCES app.users (id) ON DELETE CASCADE,
            email      text,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (provider, subject)
        );
        CREATE INDEX identities_user ON app.identities (user_id);

        -- Only the sha256 of the bearer token is stored.
        CREATE TABLE app.sessions (
            token_hash   bytea PRIMARY KEY,
            user_id      uuid NOT NULL REFERENCES app.users (id) ON DELETE CASCADE,
            remember     boolean NOT NULL,
            created_at   timestamptz NOT NULL DEFAULT now(),
            last_seen_at timestamptz NOT NULL DEFAULT now(),
            expires_at   timestamptz NOT NULL,
            revoked_at   timestamptz
        );
        CREATE INDEX sessions_user ON app.sessions (user_id);

        -- Email verification and password reset links; single use.
        CREATE TABLE app.tokens (
            token_hash bytea PRIMARY KEY,
            user_id    uuid NOT NULL REFERENCES app.users (id) ON DELETE CASCADE,
            purpose    text NOT NULL CHECK (purpose IN ('verify_email', 'reset_password')),
            created_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL,
            used_at    timestamptz
        );

        CREATE TABLE app.oauth_states (
            state         text PRIMARY KEY,
            provider      text NOT NULL,
            code_verifier text NOT NULL,
            nonce         text NOT NULL,
            redirect_to   text,
            expires_at    timestamptz NOT NULL
        );

        -- One row per counted event (failed login, registration...) for rate limits.
        CREATE TABLE app.throttle (
            key text NOT NULL,
            at  timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX throttle_key_at ON app.throttle (key, at);
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TABLE app.throttle, app.oauth_states, app.tokens, app.sessions, "
        "app.identities, app.users"
    )
```

- [ ] **Step 5: Dockerfile copies the migrations**

In `Dockerfile` replace `COPY pyproject.toml ./` and `COPY app app` with:

```dockerfile
COPY pyproject.toml alembic.ini ./
COPY migrations migrations
COPY app app
```

- [ ] **Step 6: Write the failing migration test**

`tests/test_migrations.py`:

```python
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
```

- [ ] **Step 7: Make conftest run the migrations**

In `tests/conftest.py` change the import and the `conn` fixture:

```python
from app.db import run_migrations
```

```python
@pytest.fixture
def conn(db_url: str) -> Iterator[psycopg.Connection]:
    """A fresh database for every test: the ingest `lots` table from schema.sql and this
    repo's `app` schema from its migrations."""
    with psycopg.connect(db_url, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS app CASCADE; DROP SCHEMA public CASCADE; "
                  "CREATE SCHEMA public")
        c.execute(SCHEMA.read_text())
        run_migrations(db_url)
        yield c
```

- [ ] **Step 8: Run the tests**

Run: `uv run ruff check app tests migrations && uv run pytest -q`
Expected: all pass, including `test_app_schema_is_created_by_migrations`.

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml uv.lock app/config.py app/db.py alembic.ini migrations Dockerfile tests
git commit -m "feat: app schema migration, writable pool and auth settings"
```

---

### Task 2: Security primitives (password hashing, tokens, breach check, captcha)

**Files:**
- Create: `app/security.py`
- Test: `tests/test_security.py`

**Interfaces:**
- Produces: `hash_password(str) -> str`, `verify_password(hash: str | None, password: str) -> bool`, `new_token() -> str`, `token_hash(str) -> bytes`, `password_is_breached(password, http) -> bool`, `captcha_ok(token, secret, remote_ip, http) -> bool`, constants `MIN_PASSWORD = 10`, `MAX_PASSWORD = 128`.

- [ ] **Step 1: Write the failing tests**

`tests/test_security.py`:

```python
import hashlib

import httpx

from app import security


def test_password_hash_round_trip():
    h = security.hash_password("correct horse battery")
    assert h.startswith("$argon2id$")
    assert security.verify_password(h, "correct horse battery")
    assert not security.verify_password(h, "wrong")
    assert not security.verify_password(None, "anything")   # social-only account
    assert not security.verify_password("garbage", "anything")


def test_tokens_are_random_and_hashed():
    a, b = security.new_token(), security.new_token()
    assert a != b and len(a) >= 40
    assert security.token_hash(a) == hashlib.sha256(a.encode()).digest()


def hibp(lines: str):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.pwnedpasswords.com"
        assert len(request.url.path.rsplit("/", 1)[1]) == 5   # only the prefix leaves
        return httpx.Response(200, text=lines)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_breach_check_matches_on_suffix():
    sha1 = hashlib.sha1(b"password123").hexdigest().upper()
    assert security.password_is_breached("password123", hibp(f"{sha1[5:]}:42\r\nABCDEF:1"))
    assert not security.password_is_breached("password123", hibp("ABCDEF:1"))


def test_breach_check_fails_open():
    def down(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")
    client = httpx.Client(transport=httpx.MockTransport(down))
    assert not security.password_is_breached("password123", client)


def turnstile(success: bool):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={"success": success})
    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def test_captcha_is_skipped_without_secret():
    client, seen = turnstile(False)
    assert security.captcha_ok(None, "", "1.2.3.4", client)
    assert seen == {}


def test_captcha_verifies_with_cloudflare():
    client, seen = turnstile(True)
    assert security.captcha_ok("tok", "secret", "1.2.3.4", client)
    assert seen == {"secret": "secret", "response": "tok", "remoteip": "1.2.3.4"}
    client, _ = turnstile(False)
    assert not security.captcha_ok("tok", "secret", "1.2.3.4", client)
    assert not security.captcha_ok(None, "secret", "1.2.3.4", client)
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_security.py -q`
Expected: ImportError / AttributeError on `app.security`.

- [ ] **Step 3: Implement**

`app/security.py`:

```python
"""Password hashing, opaque tokens, breached-password check and captcha verification."""

import hashlib
import secrets

import httpx
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

MIN_PASSWORD, MAX_PASSWORD = 10, 128
HIBP_URL = "https://api.pwnedpasswords.com/range/"
TURNSTILE_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"

_hasher = PasswordHasher()
# Verified against when the account does not exist, so a login attempt costs the same time
# whether or not the email is registered.
DUMMY_HASH = _hasher.hash(secrets.token_hex(16))


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or DUMMY_HASH, password) and password_hash is not None
    except (VerificationError, InvalidHashError):
        return False


def new_token() -> str:
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def password_is_breached(password: str, http: httpx.Client) -> bool:
    """Have I Been Pwned range query: only the first 5 characters of the SHA-1 leave the
    server. Unreachable service = not breached (registration must keep working)."""
    sha1 = hashlib.sha1(password.encode()).hexdigest().upper()  # noqa: S324 - HIBP protocol
    try:
        response = http.get(HIBP_URL + sha1[:5], headers={"Add-Padding": "true"}, timeout=3)
        response.raise_for_status()
    except httpx.HTTPError:
        return False
    return any(line.split(":", 1)[0] == sha1[5:] for line in response.text.splitlines())


def captcha_ok(token: str | None, secret: str, remote_ip: str, http: httpx.Client) -> bool:
    """Cloudflare Turnstile server-side validation. No secret configured = not enforced."""
    if not secret:
        return True
    if not token:
        return False
    try:
        response = http.post(
            TURNSTILE_URL, data={"secret": secret, "response": token, "remoteip": remote_ip},
            timeout=5,
        )
        return bool(response.json().get("success"))
    except (httpx.HTTPError, ValueError):
        return False
```

- [ ] **Step 4: Run the tests**

Run: `uv run ruff check app tests migrations && uv run pytest tests/test_security.py -q`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add app/security.py tests/test_security.py
git commit -m "feat: password hashing, tokens, breach check and captcha verification"
```

---

### Task 3: Mail sending

**Files:**
- Create: `app/mail.py`
- Test: `tests/test_mail.py`

**Interfaces:**
- Produces: `Mailer` protocol with `send(to: str, subject: str, body: str) -> None`; `LogMailer`; `SmtpMailer(settings)`; `make_mailer(settings) -> Mailer`; message builders `verification_mail(web_url, token)`, `reset_mail(web_url, token)`, `existing_account_mail(web_url)` each returning `(subject, body)`.

- [ ] **Step 1: Write the failing tests**

`tests/test_mail.py`:

```python
import logging
from email.message import EmailMessage

from app import mail
from app.config import Settings


def test_message_builders_link_to_the_web_app():
    subject, body = mail.verification_mail("https://cars.test", "tok-1")
    assert "https://cars.test/verify-email?token=tok-1" in body
    subject, body = mail.reset_mail("https://cars.test", "tok-2")
    assert "https://cars.test/reset-password?token=tok-2" in body
    subject, body = mail.existing_account_mail("https://cars.test")
    assert "https://cars.test/forgot-password" in body


def test_log_mailer_logs_the_message(caplog):
    with caplog.at_level(logging.INFO, logger="app.mail"):
        mail.LogMailer().send("a@b.test", "Hello", "Body line")
    assert "a@b.test" in caplog.text and "Body line" in caplog.text


def test_smtp_mailer_sends_a_plain_text_message(monkeypatch):
    sent: list[EmailMessage] = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            assert (host, port) == ("smtp.test", 2525)
        def __enter__(self):
            return self
        def __exit__(self, *exc):
            return False
        def starttls(self):
            sent.append("tls")
        def login(self, username, password):
            sent.append((username, password))
        def send_message(self, message):
            sent.append(message)

    monkeypatch.setattr(mail.smtplib, "SMTP", FakeSMTP)
    settings = Settings(_env_file=None, database_url="x", smtp_host="smtp.test", smtp_port=2525,
                        smtp_username="u", smtp_password="p", mail_from="CS <no-reply@cs.test>")
    mail.SmtpMailer(settings).send("a@b.test", "Hi", "Body")
    assert sent[0] == "tls" and sent[1] == ("u", "p")
    message = sent[2]
    assert message["To"] == "a@b.test" and message["From"] == "CS <no-reply@cs.test>"
    assert message["Subject"] == "Hi" and message.get_content().strip() == "Body"


def test_make_mailer_picks_smtp_only_when_configured():
    assert isinstance(mail.make_mailer(Settings(_env_file=None, database_url="x")), mail.LogMailer)
    assert isinstance(mail.make_mailer(Settings(_env_file=None, database_url="x", smtp_host="h")),
                      mail.SmtpMailer)
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_mail.py -q`
Expected: ImportError on `app.mail`.

- [ ] **Step 3: Implement**

`app/mail.py`:

```python
"""Outgoing email: SMTP when configured, otherwise the log (development)."""

import logging
import smtplib
from email.message import EmailMessage
from typing import Protocol

from .config import Settings

log = logging.getLogger(__name__)


class Mailer(Protocol):
    def send(self, to: str, subject: str, body: str) -> None: ...


class LogMailer:
    def send(self, to: str, subject: str, body: str) -> None:
        log.info("mail to=%s subject=%r\n%s", to, subject, body)


class SmtpMailer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def send(self, to: str, subject: str, body: str) -> None:
        s = self.settings
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = s.mail_from, to, subject
        message.set_content(body)
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=10) as smtp:
            smtp.starttls()
            if s.smtp_username:
                smtp.login(s.smtp_username, s.smtp_password)
            smtp.send_message(message)


def make_mailer(settings: Settings) -> Mailer:
    return SmtpMailer(settings) if settings.smtp_host else LogMailer()


def verification_mail(web_url: str, token: str) -> tuple[str, str]:
    return "Confirm your email address", (
        "Welcome to Car's Secrets.\n\nConfirm your email address by opening this link "
        f"(valid for 24 hours):\n\n{web_url}/verify-email?token={token}\n\n"
        "If you did not create an account, ignore this message."
    )


def reset_mail(web_url: str, token: str) -> tuple[str, str]:
    return "Reset your password", (
        "Someone asked to reset the password of your Car's Secrets account. Open this link "
        f"(valid for 1 hour) to choose a new password:\n\n{web_url}/reset-password?token={token}"
        "\n\nIf that was not you, ignore this message; your password has not changed."
    )


def existing_account_mail(web_url: str) -> tuple[str, str]:
    return "You already have an account", (
        "Someone tried to register on Car's Secrets with this email address, but an account "
        f"already exists. You can sign in at {web_url}/login or reset your password at "
        f"{web_url}/forgot-password.\n\nIf that was not you, ignore this message."
    )
```

- [ ] **Step 4: Run the tests**

Run: `uv run ruff check app tests migrations && uv run pytest tests/test_mail.py -q`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add app/mail.py tests/test_mail.py
git commit -m "feat: SMTP and log mailers with account email templates"
```

---

### Task 4: Account storage (`app/accounts.py`)

**Files:**
- Create: `app/accounts.py`
- Test: `tests/test_accounts.py`

**Interfaces:**
- Produces (all take a writable `psycopg.Connection` first):
  - `normalize_email(str) -> str`
  - `create_user(conn, *, email, first_name, last_name, phone=None, password_hash=None, terms_accepted=False, email_verified=False) -> dict`
  - `user_by_email(conn, email) -> dict | None`, `user_by_id(conn, user_id) -> dict | None`
  - `set_password(conn, user_id, password_hash)`, `mark_verified(conn, user_id)`
  - `create_session(conn, user_id, *, remember, remember_seconds, idle_seconds) -> tuple[str, datetime]`
  - `session_user(conn, token, *, remember_seconds, idle_seconds) -> dict | None`
  - `revoke_session(conn, token)`, `revoke_all_sessions(conn, user_id)`
  - `create_token(conn, user_id, purpose, ttl_seconds) -> str`, `consume_token(conn, token, purpose) -> UUID | None`
  - `throttle_hit(conn, key)`, `throttle_count(conn, key, window_seconds) -> int`
  - `link_identity(conn, provider, subject, user_id, email)`, `user_by_identity(conn, provider, subject) -> dict | None`
  - `save_oauth_state(conn, *, state, provider, code_verifier, nonce, redirect_to, ttl_seconds)`, `pop_oauth_state(conn, state, provider) -> dict | None`

- [ ] **Step 1: Write the failing tests**

`tests/test_accounts.py`:

```python
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from app import accounts
from app.security import hash_password


@pytest.fixture
def user(conn):
    return accounts.create_user(
        conn, email="Dana@Example.com ", first_name="Dana", last_name="Lee",
        password_hash=hash_password("pw-pw-pw-pw"), terms_accepted=True,
    )


def test_create_and_find_user(conn, user):
    assert user["email"] == "dana@example.com"
    assert user["email_verified_at"] is None and user["terms_accepted_at"] is not None
    assert accounts.user_by_email(conn, " DANA@example.com")["id"] == user["id"]
    assert accounts.user_by_id(conn, user["id"])["first_name"] == "Dana"
    assert accounts.user_by_email(conn, "nobody@example.com") is None
    with pytest.raises(psycopg.errors.UniqueViolation):
        accounts.create_user(conn, email="dana@example.com", first_name="D", last_name="L")


def test_verify_and_set_password(conn, user):
    accounts.mark_verified(conn, user["id"])
    accounts.set_password(conn, user["id"], "new-hash")
    row = accounts.user_by_id(conn, user["id"])
    assert row["email_verified_at"] is not None and row["password_hash"] == "new-hash"


def test_sessions(conn, user):
    token, expires = accounts.create_session(
        conn, user["id"], remember=True, remember_seconds=3600, idle_seconds=60)
    assert expires - datetime.now(UTC) > timedelta(seconds=3500)
    assert accounts.session_user(conn, token, remember_seconds=3600, idle_seconds=60)["id"] == \
        user["id"]
    assert accounts.session_user(conn, "nope", remember_seconds=3600, idle_seconds=60) is None

    short, expires = accounts.create_session(
        conn, user["id"], remember=False, remember_seconds=3600, idle_seconds=60)
    assert expires - datetime.now(UTC) < timedelta(seconds=90)
    conn.execute("UPDATE app.sessions SET expires_at = now() - interval '1s' "
                 "WHERE token_hash = %s", (accounts.token_hash(short),))
    assert accounts.session_user(conn, short, remember_seconds=3600, idle_seconds=60) is None

    accounts.revoke_session(conn, token)
    assert accounts.session_user(conn, token, remember_seconds=3600, idle_seconds=60) is None
    t2, _ = accounts.create_session(conn, user["id"], remember=True, remember_seconds=3600,
                                    idle_seconds=60)
    accounts.revoke_all_sessions(conn, user["id"])
    assert accounts.session_user(conn, t2, remember_seconds=3600, idle_seconds=60) is None


def test_session_use_slides_expiry(conn, user):
    token, _ = accounts.create_session(conn, user["id"], remember=False, remember_seconds=3600,
                                       idle_seconds=600)
    conn.execute("UPDATE app.sessions SET expires_at = now() + interval '10s'")
    accounts.session_user(conn, token, remember_seconds=3600, idle_seconds=600)
    row = conn.execute("SELECT expires_at - now() AS left FROM app.sessions").fetchone()
    assert row["left"] > timedelta(seconds=500)


def test_one_time_tokens(conn, user):
    token = accounts.create_token(conn, user["id"], "verify_email", 60)
    assert accounts.consume_token(conn, token, "reset_password") is None   # wrong purpose
    assert accounts.consume_token(conn, token, "verify_email") == user["id"]
    assert accounts.consume_token(conn, token, "verify_email") is None     # single use
    expired = accounts.create_token(conn, user["id"], "reset_password", -1)
    assert accounts.consume_token(conn, expired, "reset_password") is None


def test_throttle(conn):
    assert accounts.throttle_count(conn, "login:ip:1.1.1.1", 900) == 0
    for _ in range(3):
        accounts.throttle_hit(conn, "login:ip:1.1.1.1")
    accounts.throttle_hit(conn, "login:ip:2.2.2.2")
    assert accounts.throttle_count(conn, "login:ip:1.1.1.1", 900) == 3
    conn.execute("UPDATE app.throttle SET at = now() - interval '1 hour'")
    assert accounts.throttle_count(conn, "login:ip:1.1.1.1", 900) == 0


def test_identities(conn, user):
    assert accounts.user_by_identity(conn, "google", "sub-1") is None
    accounts.link_identity(conn, "google", "sub-1", user["id"], "dana@example.com")
    assert accounts.user_by_identity(conn, "google", "sub-1")["id"] == user["id"]


def test_oauth_state_is_single_use_and_provider_bound(conn):
    accounts.save_oauth_state(conn, state="s1", provider="google", code_verifier="v",
                              nonce="n", redirect_to="/account", ttl_seconds=60)
    assert accounts.pop_oauth_state(conn, "s1", "apple") is None
    row = accounts.pop_oauth_state(conn, "s1", "google")
    assert row["code_verifier"] == "v" and row["nonce"] == "n" and row["redirect_to"] == "/account"
    assert accounts.pop_oauth_state(conn, "s1", "google") is None
    accounts.save_oauth_state(conn, state="s2", provider="google", code_verifier="v",
                              nonce="n", redirect_to=None, ttl_seconds=-1)
    assert accounts.pop_oauth_state(conn, "s2", "google") is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_accounts.py -q`
Expected: ImportError on `app.accounts`.

- [ ] **Step 3: Implement**

`app/accounts.py`:

```python
"""Storage for accounts in the `app` schema. Every function takes a writable connection."""

from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg

from .security import new_token, token_hash

USER_COLUMNS = """id, email, first_name, last_name, phone, password_hash, email_verified_at,
    terms_accepted_at, created_at"""


def normalize_email(email: str) -> str:
    return email.strip().lower()


def create_user(
    conn: psycopg.Connection, *, email: str, first_name: str, last_name: str,
    phone: str | None = None, password_hash: str | None = None, terms_accepted: bool = False,
    email_verified: bool = False,
) -> dict[str, Any]:
    return conn.execute(
        f"""INSERT INTO app.users (email, first_name, last_name, phone, password_hash,
                                   terms_accepted_at, email_verified_at)
            VALUES (%s, %s, %s, %s, %s, CASE WHEN %s THEN now() END, CASE WHEN %s THEN now() END)
            RETURNING {USER_COLUMNS}""",
        (normalize_email(email), first_name, last_name, phone, password_hash, terms_accepted,
         email_verified),
    ).fetchone()


def user_by_email(conn: psycopg.Connection, email: str) -> dict[str, Any] | None:
    return conn.execute(f"SELECT {USER_COLUMNS} FROM app.users WHERE email = %s",
                        (normalize_email(email),)).fetchone()


def user_by_id(conn: psycopg.Connection, user_id: UUID) -> dict[str, Any] | None:
    return conn.execute(f"SELECT {USER_COLUMNS} FROM app.users WHERE id = %s",
                        (user_id,)).fetchone()


def set_password(conn: psycopg.Connection, user_id: UUID, password_hash: str) -> None:
    conn.execute("UPDATE app.users SET password_hash = %s, updated_at = now() WHERE id = %s",
                 (password_hash, user_id))


def mark_verified(conn: psycopg.Connection, user_id: UUID) -> None:
    conn.execute("""UPDATE app.users SET email_verified_at = coalesce(email_verified_at, now()),
                    updated_at = now() WHERE id = %s""", (user_id,))


# --- sessions ---------------------------------------------------------------------------------

def create_session(
    conn: psycopg.Connection, user_id: UUID, *, remember: bool, remember_seconds: int,
    idle_seconds: int,
) -> tuple[str, datetime]:
    token = new_token()
    row = conn.execute(
        """INSERT INTO app.sessions (token_hash, user_id, remember, expires_at)
           VALUES (%s, %s, %s, now() + make_interval(secs => %s)) RETURNING expires_at""",
        (token_hash(token), user_id, remember, remember_seconds if remember else idle_seconds),
    ).fetchone()
    return token, row["expires_at"]


def session_user(
    conn: psycopg.Connection, token: str, *, remember_seconds: int, idle_seconds: int,
) -> dict[str, Any] | None:
    """The user of a live session; using the session extends it (sliding expiry)."""
    row = conn.execute(
        """UPDATE app.sessions
           SET last_seen_at = now(),
               expires_at = now() + make_interval(secs => CASE WHEN remember THEN %s ELSE %s END)
           WHERE token_hash = %s AND revoked_at IS NULL AND expires_at > now()
           RETURNING user_id""",
        (remember_seconds, idle_seconds, token_hash(token)),
    ).fetchone()
    return user_by_id(conn, row["user_id"]) if row else None


def revoke_session(conn: psycopg.Connection, token: str) -> None:
    conn.execute("UPDATE app.sessions SET revoked_at = now() WHERE token_hash = %s",
                 (token_hash(token),))


def revoke_all_sessions(conn: psycopg.Connection, user_id: UUID) -> None:
    conn.execute("UPDATE app.sessions SET revoked_at = now() "
                 "WHERE user_id = %s AND revoked_at IS NULL", (user_id,))


# --- one-time tokens (email verification, password reset) -------------------------------------

def create_token(conn: psycopg.Connection, user_id: UUID, purpose: str, ttl_seconds: int) -> str:
    token = new_token()
    conn.execute(
        """INSERT INTO app.tokens (token_hash, user_id, purpose, expires_at)
           VALUES (%s, %s, %s, now() + make_interval(secs => %s))""",
        (token_hash(token), user_id, purpose, ttl_seconds),
    )
    return token


def consume_token(conn: psycopg.Connection, token: str, purpose: str) -> UUID | None:
    row = conn.execute(
        """UPDATE app.tokens SET used_at = now()
           WHERE token_hash = %s AND purpose = %s AND used_at IS NULL AND expires_at > now()
           RETURNING user_id""",
        (token_hash(token), purpose),
    ).fetchone()
    return row["user_id"] if row else None


# --- throttling -------------------------------------------------------------------------------

def throttle_hit(conn: psycopg.Connection, key: str) -> None:
    conn.execute("INSERT INTO app.throttle (key) VALUES (%s)", (key,))
    conn.execute("DELETE FROM app.throttle WHERE key = %s AND at < now() - interval '1 day'",
                 (key,))


def throttle_count(conn: psycopg.Connection, key: str, window_seconds: int) -> int:
    return conn.execute(
        "SELECT count(*) AS n FROM app.throttle "
        "WHERE key = %s AND at > now() - make_interval(secs => %s)", (key, window_seconds),
    ).fetchone()["n"]


# --- social identities and OAuth handshake state ----------------------------------------------

def link_identity(conn: psycopg.Connection, provider: str, subject: str, user_id: UUID,
                  email: str | None) -> None:
    conn.execute("INSERT INTO app.identities (provider, subject, user_id, email) "
                 "VALUES (%s, %s, %s, %s)", (provider, subject, user_id, email))


def user_by_identity(conn: psycopg.Connection, provider: str, subject: str) -> dict | None:
    row = conn.execute("SELECT user_id FROM app.identities WHERE provider = %s AND subject = %s",
                       (provider, subject)).fetchone()
    return user_by_id(conn, row["user_id"]) if row else None


def save_oauth_state(
    conn: psycopg.Connection, *, state: str, provider: str, code_verifier: str, nonce: str,
    redirect_to: str | None, ttl_seconds: int,
) -> None:
    conn.execute(
        """INSERT INTO app.oauth_states (state, provider, code_verifier, nonce, redirect_to,
                                         expires_at)
           VALUES (%s, %s, %s, %s, %s, now() + make_interval(secs => %s))""",
        (state, provider, code_verifier, nonce, redirect_to, ttl_seconds),
    )


def pop_oauth_state(conn: psycopg.Connection, state: str, provider: str) -> dict | None:
    conn.execute("DELETE FROM app.oauth_states WHERE expires_at < now() - interval '1 hour'")
    return conn.execute(
        """DELETE FROM app.oauth_states WHERE state = %s AND provider = %s AND expires_at > now()
           RETURNING code_verifier, nonce, redirect_to""", (state, provider),
    ).fetchone()
```

- [ ] **Step 4: Run the tests**

Run: `uv run ruff check app tests migrations && uv run pytest tests/test_accounts.py -q`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add app/accounts.py tests/test_accounts.py
git commit -m "feat: account storage for users, sessions, tokens, identities and throttling"
```

---

### Task 5: OAuth / OpenID Connect providers (`app/oauth.py`)

**Files:**
- Create: `app/oauth.py`
- Test: `tests/test_oauth.py`

**Interfaces:**
- Produces: `Provider` dataclass (`name, authorize_url, token_url, jwks_url, issuers, client_id, client_secret: Callable[[], str], scope, extra_authorize`), `providers(settings) -> dict[str, Provider]` (configured ones only), `pkce_pair() -> tuple[verifier, challenge]`, `authorize_url(p, *, redirect_uri, state, nonce, code_challenge) -> str`, `exchange_code(p, http, *, code, redirect_uri, code_verifier) -> dict`, `verify_id_token(p, id_token, *, nonce) -> dict` (raises `OAuthError`), `signing_key(p, token)` (monkeypatched in tests), `apple_client_secret(settings) -> str`, `class OAuthError(Exception)`.

- [ ] **Step 1: Write the failing tests**

`tests/test_oauth.py`:

```python
import base64
import hashlib
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from app import oauth
from app.config import Settings

RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def settings(**kw) -> Settings:
    return Settings(_env_file=None, database_url="x", **kw)


def test_only_configured_providers_are_offered():
    assert oauth.providers(settings()) == {}
    assert set(oauth.providers(settings(google_client_id="g", google_client_secret="s"))) == {
        "google"}


def test_pkce_pair_is_s256():
    verifier, challenge = oauth.pkce_pair()
    digest = hashlib.sha256(verifier.encode()).digest()
    assert challenge == base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def test_authorize_url_for_google():
    p = oauth.providers(settings(google_client_id="g", google_client_secret="s"))["google"]
    url = oauth.authorize_url(p, redirect_uri="https://w/auth/callback/google", state="st",
                              nonce="no", code_challenge="ch")
    parts = urlsplit(url)
    assert parts.scheme == "https" and parts.netloc == "accounts.google.com"
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert q == {
        "client_id": "g", "redirect_uri": "https://w/auth/callback/google",
        "response_type": "code", "scope": "openid email profile", "state": "st",
        "nonce": "no", "code_challenge": "ch", "code_challenge_method": "S256",
    }


def test_authorize_url_for_apple_posts_the_callback():
    p = oauth.providers(settings(apple_client_id="a", apple_team_id="t", apple_key_id="k",
                                 apple_private_key="pem"))["apple"]
    q = {k: v[0] for k, v in parse_qs(urlsplit(oauth.authorize_url(
        p, redirect_uri="https://w/cb", state="s", nonce="n", code_challenge="c")).query).items()}
    assert q["response_mode"] == "form_post" and q["scope"] == "name email"


def test_apple_client_secret_is_an_es256_jwt():
    key = ec.generate_private_key(ec.SECP256R1())
    from cryptography.hazmat.primitives import serialization
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    s = settings(apple_client_id="com.cs.web", apple_team_id="TEAM", apple_key_id="KID",
                 apple_private_key=pem.replace("\n", "\\n"))   # as an env var would hold it
    secret = oauth.apple_client_secret(s)
    assert jwt.get_unverified_header(secret) == {"alg": "ES256", "kid": "KID", "typ": "JWT"}
    claims = jwt.decode(secret, key.public_key(), algorithms=["ES256"],
                        audience="https://appleid.apple.com")
    assert claims["iss"] == "TEAM" and claims["sub"] == "com.cs.web"


def google_provider():
    return oauth.providers(settings(google_client_id="g", google_client_secret="s"))["google"]


def id_token(**claims) -> str:
    base = {"iss": "https://accounts.google.com", "aud": "g", "sub": "123", "nonce": "no",
            "iat": int(time.time()), "exp": int(time.time()) + 60}
    return jwt.encode({**base, **claims}, RSA_KEY, algorithm="RS256")


def test_exchange_code_posts_the_verifier():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://oauth2.googleapis.com/token"
        seen.update({k: v[0] for k, v in parse_qs(request.content.decode()).items()})
        return httpx.Response(200, json={"id_token": "x", "access_token": "y"})
    http = httpx.Client(transport=httpx.MockTransport(handler))
    tokens = oauth.exchange_code(google_provider(), http, code="c", redirect_uri="https://w/cb",
                                 code_verifier="v")
    assert tokens["id_token"] == "x"
    assert seen == {"grant_type": "authorization_code", "code": "c", "client_id": "g",
                    "client_secret": "s", "redirect_uri": "https://w/cb", "code_verifier": "v"}


def test_exchange_code_failure_raises():
    http = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(400, json={"error": "invalid_grant"})))
    with pytest.raises(oauth.OAuthError):
        oauth.exchange_code(google_provider(), http, code="c", redirect_uri="u", code_verifier="v")


def test_verify_id_token(monkeypatch):
    monkeypatch.setattr(oauth, "signing_key", lambda p, t: RSA_KEY.public_key())
    p = google_provider()
    claims = oauth.verify_id_token(p, id_token(email="d@x.test"), nonce="no")
    assert claims["sub"] == "123" and claims["email"] == "d@x.test"
    for bad in (id_token(nonce="other"), id_token(aud="someone-else"),
                id_token(iss="https://evil.test"), id_token(exp=int(time.time()) - 10)):
        with pytest.raises(oauth.OAuthError):
            oauth.verify_id_token(p, bad, nonce="no")
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_oauth.py -q`
Expected: ImportError on `app.oauth`.

- [ ] **Step 3: Implement**

`app/oauth.py`:

```python
"""Google and Apple sign-in through OpenID Connect (authorization code + PKCE + nonce)."""

import base64
import hashlib
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from jwt import PyJWKClient

from .config import Settings

APPLE_AUDIENCE = "https://appleid.apple.com"


class OAuthError(Exception):
    pass


@dataclass
class Provider:
    name: str
    authorize_url: str
    token_url: str
    jwks_url: str
    issuers: list[str]
    client_id: str
    client_secret: Callable[[], str]
    scope: str
    extra_authorize: dict[str, str] = field(default_factory=dict)


def apple_client_secret(settings: Settings) -> str:
    """Apple wants the client secret as a short-lived ES256 JWT signed with the app's key."""
    now = int(time.time())
    return jwt.encode(
        {"iss": settings.apple_team_id, "iat": now, "exp": now + 3600, "aud": APPLE_AUDIENCE,
         "sub": settings.apple_client_id},
        settings.apple_private_key.replace("\\n", "\n"), algorithm="ES256",
        headers={"kid": settings.apple_key_id},
    )


def providers(settings: Settings) -> dict[str, Provider]:
    found: dict[str, Provider] = {}
    if settings.google_client_id and settings.google_client_secret:
        found["google"] = Provider(
            name="google",
            authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
            token_url="https://oauth2.googleapis.com/token",
            jwks_url="https://www.googleapis.com/oauth2/v3/certs",
            issuers=["https://accounts.google.com", "accounts.google.com"],
            client_id=settings.google_client_id,
            client_secret=lambda: settings.google_client_secret,
            scope="openid email profile",
        )
    if (settings.apple_client_id and settings.apple_team_id and settings.apple_key_id
            and settings.apple_private_key):
        found["apple"] = Provider(
            name="apple",
            authorize_url="https://appleid.apple.com/auth/authorize",
            token_url="https://appleid.apple.com/auth/token",
            jwks_url="https://appleid.apple.com/auth/keys",
            issuers=[APPLE_AUDIENCE],
            client_id=settings.apple_client_id,
            client_secret=lambda: apple_client_secret(settings),
            scope="name email",
            # Apple only returns name/email through a POST to the redirect URI.
            extra_authorize={"response_mode": "form_post"},
        )
    return found


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def authorize_url(p: Provider, *, redirect_uri: str, state: str, nonce: str,
                  code_challenge: str) -> str:
    params = {
        "client_id": p.client_id, "redirect_uri": redirect_uri, "response_type": "code",
        "scope": p.scope, "state": state, "nonce": nonce, "code_challenge": code_challenge,
        "code_challenge_method": "S256", **p.extra_authorize,
    }
    return f"{p.authorize_url}?{urlencode(params)}"


def exchange_code(p: Provider, http: httpx.Client, *, code: str, redirect_uri: str,
                  code_verifier: str) -> dict[str, Any]:
    try:
        response = http.post(p.token_url, data={
            "grant_type": "authorization_code", "code": code, "client_id": p.client_id,
            "client_secret": p.client_secret(), "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        }, timeout=10)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as e:
        raise OAuthError(f"token exchange with {p.name} failed") from e


_jwks_clients: dict[str, PyJWKClient] = {}


def signing_key(p: Provider, token: str):
    """The provider's public key for this token, fetched from its JWKS (cached)."""
    client = _jwks_clients.get(p.jwks_url)
    if client is None:
        client = _jwks_clients[p.jwks_url] = PyJWKClient(p.jwks_url, cache_keys=True)
    return client.get_signing_key_from_jwt(token).key


def verify_id_token(p: Provider, id_token: str, *, nonce: str) -> dict[str, Any]:
    try:
        claims = jwt.decode(
            id_token, signing_key(p, id_token), algorithms=["RS256", "ES256"],
            audience=p.client_id, issuer=p.issuers,
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.PyJWTError as e:
        raise OAuthError(f"invalid id_token from {p.name}") from e
    if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise OAuthError("nonce mismatch")
    return claims
```

- [ ] **Step 4: Run the tests**

Run: `uv run ruff check app tests migrations && uv run pytest tests/test_oauth.py -q`
Expected: 9 passed. (If `jwt.decode` rejects a list `issuer`, upgrade PyJWT: it accepts a sequence since 2.9.)

- [ ] **Step 5: Commit**

```bash
git add app/oauth.py tests/test_oauth.py
git commit -m "feat: Google and Apple OpenID Connect helpers with PKCE and nonce"
```

---

### Task 6: Auth routes and wiring (`app/auth.py`, `app/main.py`)

**Files:**
- Create: `app/auth.py`
- Modify: `app/main.py`
- Modify: `tests/conftest.py` (settings and client fixtures with fakes)
- Test: `tests/test_auth.py`

**Interfaces:**
- Consumes: everything from Tasks 2–5.
- Produces: `create_app(settings=None, *, mailer=None, http=None)`; `app.state.settings / rw_pool / pool / mailer / http`; dependency `auth.current_user` (bearer) returning the user dict; router `auth.router` mounted at `/api/auth`.

- [ ] **Step 1: Update conftest**

Replace the `client` fixture in `tests/conftest.py` and add the fakes:

```python
import httpx

from app.config import Settings
from app.main import create_app


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


@pytest.fixture
def settings(db_url: str) -> Settings:
    return Settings(
        _env_file=None, database_url=db_url, web_url="https://web.test",
        internal_api_key="k" * 32, check_breached_passwords=False,
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
```

Keep `insert_lot` and the `conn`, `db_url` fixtures as they are.

- [ ] **Step 2: Write the failing tests**

`tests/test_auth.py`:

```python
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app import oauth

RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
GOOD = {"first_name": "Dana", "last_name": "Lee", "email": "Dana@Example.com",
        "phone": "+14155552671", "password": "long-enough-pw", "accept_terms": True}
INTERNAL = {"X-Internal-Key": "k" * 32, "X-Client-IP": "9.9.9.9"}


def register(client, **overrides):
    return client.post("/api/auth/register", json={**GOOD, **overrides})


def login(client, password="long-enough-pw", **extra):
    return client.post("/api/auth/login",
                       json={"email": "dana@example.com", "password": password, **extra})


def verified_user(client, mailer):
    assert register(client).status_code == 200
    token = mailer.last_link("/verify-email")
    assert client.post("/api/auth/verify-email", json={"token": token}).status_code == 200


def test_register_verify_login_me(client, mailer, conn):
    assert register(client).json() == {"ok": True}
    row = conn.execute("SELECT * FROM app.users").fetchone()
    assert row["email"] == "dana@example.com" and row["phone"] == "+14155552671"
    assert row["email_verified_at"] is None and row["terms_accepted_at"] is not None
    assert row["password_hash"].startswith("$argon2id$")
    assert mailer.sent[-1][0] == "dana@example.com"

    assert login(client).status_code == 403
    assert login(client).json()["detail"]["code"] == "email_unverified"

    token = mailer.last_link("/verify-email")
    assert client.post("/api/auth/verify-email", json={"token": token}).status_code == 200
    assert client.post("/api/auth/verify-email", json={"token": token}).status_code == 400

    r = login(client)
    assert r.status_code == 200
    session = r.json()["token"]
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {session}"})
    assert me.status_code == 200
    assert me.json() == {"id": str(row["id"]), "email": "dana@example.com",
                         "first_name": "Dana", "last_name": "Lee", "phone": "+14155552671",
                         "email_verified": True}
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_register_existing_email_sends_a_hint_instead(client, mailer, conn):
    register(client)
    r = register(client, first_name="Other")
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert conn.execute("SELECT count(*) AS n FROM app.users").fetchone()["n"] == 1
    assert mailer.sent[-1][1] == "You already have an account"


@pytest.mark.parametrize(("field", "value"), [
    ("password", "short"), ("password", "x" * 129), ("phone", "12345"), ("phone", "abc"),
    ("accept_terms", False), ("email", "not-an-email"), ("first_name", " "),
])
def test_register_validation(client, conn, field, value):
    r = register(client, **{field: value})
    assert r.status_code == 422, r.text
    assert any(field in err["loc"] for err in r.json()["detail"])
    assert conn.execute("SELECT count(*) AS n FROM app.users").fetchone()["n"] == 0


def test_register_without_phone(client, conn):
    assert register(client, phone=None).status_code == 200
    assert conn.execute("SELECT phone FROM app.users").fetchone()["phone"] is None


def test_breached_password_is_rejected(client, settings, http_handler):
    import hashlib
    settings.check_breached_passwords = True
    sha1 = hashlib.sha1(b"long-enough-pw").hexdigest().upper()
    http_handler.fn = lambda r: httpx.Response(200, text=f"{sha1[5:]}:7")
    r = register(client)
    assert r.status_code == 422 and "breach" in r.text.lower()


def test_captcha_required_on_register_when_configured(client, settings, http_handler):
    settings.turnstile_secret = "sec"
    assert register(client).status_code == 400
    assert register(client).json()["detail"]["code"] == "captcha_failed"
    http_handler.fn = lambda r: httpx.Response(200, json={"success": True})
    assert register(client, captcha_token="tok").status_code == 200


def test_login_failures_require_a_captcha(client, mailer, settings, http_handler):
    verified_user(client, mailer)
    for _ in range(3):
        r = login(client, password="wrong-password")
        assert r.status_code == 401 and r.json()["detail"]["code"] == "invalid_credentials"
    r = login(client)   # right password, but throttled
    assert r.status_code == 429 and r.json()["detail"]["code"] == "captcha_required"
    settings.turnstile_secret = "sec"
    http_handler.fn = lambda r: httpx.Response(200, json={"success": True})
    assert login(client, captcha_token="tok").status_code == 200


def test_login_throttles_per_client_ip(client, mailer):
    verified_user(client, mailer)
    for _ in range(10):
        client.post("/api/auth/login", headers=INTERNAL,
                    json={"email": f"nobody{_}@x.test", "password": "wrong-password"})
    r = client.post("/api/auth/login", headers=INTERNAL,
                    json={"email": "dana@example.com", "password": "long-enough-pw"})
    assert r.status_code == 429
    # Without the internal key the IP header is ignored, so the peer address is not throttled.
    r = client.post("/api/auth/login", headers={"X-Client-IP": "9.9.9.9"},
                    json={"email": "dana@example.com", "password": "long-enough-pw"})
    assert r.status_code == 200


def test_unknown_email_is_a_plain_401(client):
    assert login(client).status_code == 401


def test_remember_me_controls_session_length(client, mailer):
    verified_user(client, mailer)
    short = datetime.fromisoformat(login(client).json()["expires_at"])
    long = datetime.fromisoformat(login(client, remember=True).json()["expires_at"])
    now = datetime.now(UTC)
    assert timedelta(hours=11) < short - now < timedelta(hours=13)
    assert timedelta(days=29) < long - now < timedelta(days=31)


def test_logout_revokes_the_session(client, mailer):
    verified_user(client, mailer)
    headers = {"Authorization": f"Bearer {login(client).json()['token']}"}
    assert client.post("/api/auth/logout", headers=headers).status_code == 204
    assert client.get("/api/auth/me", headers=headers).status_code == 401


def test_resend_verification(client, mailer, conn):
    register(client)
    n = len(mailer.sent)
    assert client.post("/api/auth/resend-verification",
                       json={"email": "dana@example.com"}).status_code == 200
    assert len(mailer.sent) == n + 1
    assert client.post("/api/auth/resend-verification",
                       json={"email": "nobody@example.com"}).status_code == 200
    assert len(mailer.sent) == n + 1


def test_forgot_and_reset_password(client, mailer):
    verified_user(client, mailer)
    old_session = login(client).json()["token"]
    n = len(mailer.sent)
    assert client.post("/api/auth/forgot-password",
                       json={"email": "nobody@example.com"}).status_code == 200
    assert len(mailer.sent) == n
    assert client.post("/api/auth/forgot-password",
                       json={"email": "DANA@example.com"}).status_code == 200
    token = mailer.last_link("/reset-password")

    r = client.post("/api/auth/reset-password", json={"token": token, "password": "short"})
    assert r.status_code == 422
    r = client.post("/api/auth/reset-password",
                    json={"token": token, "password": "brand-new-password"})
    assert r.status_code == 200
    r = client.post("/api/auth/reset-password",
                    json={"token": token, "password": "brand-new-password"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "invalid_token"

    assert login(client).status_code == 401
    assert login(client, password="brand-new-password").status_code == 200
    assert client.get("/api/auth/me",
                      headers={"Authorization": f"Bearer {old_session}"}).status_code == 401


# --- social login -----------------------------------------------------------------------------

def google_id_token(**claims) -> str:
    base = {"iss": "https://accounts.google.com", "aud": "gid", "sub": "g-123",
            "email": "dana@example.com", "email_verified": True, "given_name": "Dana",
            "family_name": "Lee", "iat": int(time.time()), "exp": int(time.time()) + 60}
    return jwt.encode({**base, **claims}, RSA_KEY, algorithm="RS256")


@pytest.fixture
def google(settings, monkeypatch):
    settings.google_client_id, settings.google_client_secret = "gid", "gsecret"
    monkeypatch.setattr(oauth, "signing_key", lambda p, t: RSA_KEY.public_key())


def start_google(client) -> tuple[str, str]:
    r = client.post("/api/auth/oauth/google/start", json={"redirect_to": "/account"})
    assert r.status_code == 200, r.text
    q = {k: v[0] for k, v in parse_qs(urlsplit(r.json()["authorize_url"]).query).items()}
    return r.json()["state"], q["nonce"]


def test_oauth_unconfigured_provider(client):
    r = client.post("/api/auth/oauth/google/start", json={})
    assert r.status_code == 501 and r.json()["detail"]["code"] == "provider_not_configured"
    assert client.post("/api/auth/oauth/github/start", json={}).status_code == 404


def test_google_sign_in_creates_then_reuses_the_user(client, google, http_handler, conn):
    state, nonce = start_google(client)
    q = {k: v[0] for k, v in parse_qs(urlsplit(client.post(
        "/api/auth/oauth/google/start", json={}).json()["authorize_url"]).query).items()}
    assert q["redirect_uri"] == "https://web.test/auth/callback/google"
    assert q["client_id"] == "gid" and q["code_challenge_method"] == "S256"

    http_handler.fn = lambda r: httpx.Response(200, json={"id_token": google_id_token(nonce=nonce)})
    r = client.post("/api/auth/oauth/google/callback", json={"code": "c", "state": state})
    assert r.status_code == 200, r.text
    assert r.json()["new_user"] is True and r.json()["redirect_to"] == "/account"
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {r.json()['token']}"})
    assert me.json()["first_name"] == "Dana" and me.json()["email_verified"] is True
    assert conn.execute("SELECT password_hash FROM app.users").fetchone()["password_hash"] is None

    state, nonce = start_google(client)
    http_handler.fn = lambda r: httpx.Response(200, json={"id_token": google_id_token(nonce=nonce)})
    r = client.post("/api/auth/oauth/google/callback", json={"code": "c", "state": state})
    assert r.status_code == 200 and r.json()["new_user"] is False
    assert conn.execute("SELECT count(*) AS n FROM app.users").fetchone()["n"] == 1


def test_google_links_to_an_existing_verified_email(client, google, mailer, http_handler, conn):
    verified_user(client, mailer)
    state, nonce = start_google(client)
    http_handler.fn = lambda r: httpx.Response(200, json={"id_token": google_id_token(nonce=nonce)})
    r = client.post("/api/auth/oauth/google/callback", json={"code": "c", "state": state})
    assert r.status_code == 200 and r.json()["new_user"] is False
    assert conn.execute("SELECT count(*) AS n FROM app.identities").fetchone()["n"] == 1


def test_google_rejects_bad_state_and_nonce(client, google, http_handler):
    r = client.post("/api/auth/oauth/google/callback", json={"code": "c", "state": "forged"})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "invalid_state"
    state, _ = start_google(client)
    http_handler.fn = lambda r: httpx.Response(200, json={"id_token": google_id_token(nonce="x")})
    r = client.post("/api/auth/oauth/google/callback", json={"code": "c", "state": state})
    assert r.status_code == 400 and r.json()["detail"]["code"] == "invalid_token"
    # A state is single use: the same one cannot be replayed.
    r = client.post("/api/auth/oauth/google/callback", json={"code": "c", "state": state})
    assert r.json()["detail"]["code"] == "invalid_state"


def test_apple_first_sign_in_takes_the_posted_name(client, settings, monkeypatch, http_handler,
                                                   conn):
    settings.apple_client_id, settings.apple_team_id = "aid", "team"
    settings.apple_key_id, settings.apple_private_key = "kid", "pem"
    monkeypatch.setattr(oauth, "signing_key", lambda p, t: RSA_KEY.public_key())
    monkeypatch.setattr(oauth, "apple_client_secret", lambda s: "secret")
    r = client.post("/api/auth/oauth/apple/start", json={})
    state = r.json()["state"]
    nonce = {k: v[0] for k, v in parse_qs(urlsplit(r.json()["authorize_url"]).query).items()}[
        "nonce"]
    token = jwt.encode({"iss": "https://appleid.apple.com", "aud": "aid", "sub": "a-1",
                        "email": "dana@privaterelay.appleid.com", "email_verified": "true",
                        "nonce": nonce, "iat": int(time.time()), "exp": int(time.time()) + 60},
                       RSA_KEY, algorithm="RS256")
    http_handler.fn = lambda r: httpx.Response(200, json={"id_token": token})
    r = client.post("/api/auth/oauth/apple/callback", json={
        "code": "c", "state": state,
        "user": '{"name": {"firstName": "Dana", "lastName": "Lee"}}'})
    assert r.status_code == 200, r.text
    row = conn.execute("SELECT first_name, last_name, email_verified_at FROM app.users").fetchone()
    assert (row["first_name"], row["last_name"]) == ("Dana", "Lee")
    assert row["email_verified_at"] is not None
```

- [ ] **Step 3: Run to verify they fail**

Run: `uv run pytest tests/test_auth.py -q`
Expected: failures (404 on `/api/auth/...`, `create_app()` rejecting `mailer`).

- [ ] **Step 4: Implement the routes**

`app/auth.py`:

```python
"""Accounts: registration, email verification, login, sessions, password reset, social login."""

import hmac
import json
from collections.abc import Iterator
from typing import Annotated, Any

import phonenumbers
import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field

from . import accounts, mail, oauth, security

router = APIRouter(prefix="/auth", tags=["auth"])


# --- dependencies -----------------------------------------------------------------------------

def rw_conn(request: Request) -> Iterator[psycopg.Connection]:
    with request.app.state.rw_pool.connection() as conn:
        yield conn


RwConn = Annotated[psycopg.Connection, Depends(rw_conn)]


def client_ip(request: Request) -> str:
    """The visitor's address. The web server forwards it in X-Client-IP, trusted only when the
    request also carries the shared internal key; otherwise the peer address is used."""
    key = request.app.state.settings.internal_api_key
    given = request.headers.get("x-internal-key", "")
    if key and given and hmac.compare_digest(given, key) and request.headers.get("x-client-ip"):
        return request.headers["x-client-ip"]
    return request.client.host if request.client else "unknown"


def current_user(request: Request, conn: RwConn) -> dict[str, Any]:
    s = request.app.state.settings
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    user = None
    if scheme.lower() == "bearer" and token:
        user = accounts.session_user(conn, token, remember_seconds=s.session_remember_seconds,
                                     idle_seconds=s.session_idle_seconds)
    if user is None:
        raise error(401, "not_authenticated", "Sign in to continue.")
    request.state.session_token = token
    return user


User = Annotated[dict[str, Any], Depends(current_user)]


def error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message})


def field_error(field: str, message: str) -> HTTPException:
    return HTTPException(422, [{"loc": ["body", field], "msg": message, "type": "value_error"}])


def require_captcha(request: Request, token: str | None) -> None:
    s = request.app.state.settings
    if not security.captcha_ok(token, s.turnstile_secret, client_ip(request),
                               request.app.state.http):
        raise error(400, "captcha_failed", "Please complete the captcha and try again.")


def check_password(request: Request, password: str) -> None:
    if not security.MIN_PASSWORD <= len(password) <= security.MAX_PASSWORD:
        raise field_error("password", f"Use {security.MIN_PASSWORD} to {security.MAX_PASSWORD} "
                          "characters.")
    s = request.app.state.settings
    if s.check_breached_passwords and security.password_is_breached(password,
                                                                    request.app.state.http):
        raise field_error("password", "This password appears in a known data breach. "
                          "Choose a different one.")


def normalize_phone(phone: str | None) -> str | None:
    if phone is None or not phone.strip():
        return None
    try:
        parsed = phonenumbers.parse(phone.strip(), None)
    except phonenumbers.NumberParseException:
        parsed = None
    if parsed is None or not phonenumbers.is_valid_number(parsed):
        raise field_error("phone", "Enter a valid phone number with its country code.")
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(user["id"]), "email": user["email"], "first_name": user["first_name"],
        "last_name": user["last_name"], "phone": user["phone"],
        "email_verified": user["email_verified_at"] is not None,
    }


def issue_session(request: Request, conn: psycopg.Connection, user_id, *, remember: bool) -> dict:
    s = request.app.state.settings
    token, expires_at = accounts.create_session(
        conn, user_id, remember=remember, remember_seconds=s.session_remember_seconds,
        idle_seconds=s.session_idle_seconds)
    return {"token": token, "expires_at": expires_at}


Name = Annotated[str, Field(min_length=1, max_length=100, pattern=r"\S")]


# --- registration and verification ------------------------------------------------------------

class RegisterIn(BaseModel):
    first_name: Name
    last_name: Name
    email: EmailStr
    phone: str | None = None
    password: str
    accept_terms: bool
    captcha_token: str | None = None


@router.post("/register")
def register(body: RegisterIn, request: Request, conn: RwConn) -> dict[str, bool]:
    """Always answers ok: an existing address gets a hint mail instead of a new account."""
    require_captcha(request, body.captcha_token)
    if not body.accept_terms:
        raise field_error("accept_terms", "You must accept the Terms of Use and Privacy Policy.")
    phone = normalize_phone(body.phone)
    check_password(request, body.password)
    s, mailer = request.app.state.settings, request.app.state.mailer
    email = accounts.normalize_email(body.email)
    if accounts.user_by_email(conn, email):
        mailer.send(email, *mail.existing_account_mail(s.web_url))
        return {"ok": True}
    user = accounts.create_user(
        conn, email=email, first_name=body.first_name.strip(), last_name=body.last_name.strip(),
        phone=phone, password_hash=security.hash_password(body.password), terms_accepted=True)
    token = accounts.create_token(conn, user["id"], "verify_email", s.verify_token_seconds)
    mailer.send(email, *mail.verification_mail(s.web_url, token))
    return {"ok": True}


class TokenIn(BaseModel):
    token: str


@router.post("/verify-email")
def verify_email(body: TokenIn, conn: RwConn) -> dict[str, bool]:
    user_id = accounts.consume_token(conn, body.token, "verify_email")
    if user_id is None:
        raise error(400, "invalid_token", "This link is invalid or has expired.")
    accounts.mark_verified(conn, user_id)
    return {"ok": True}


class EmailIn(BaseModel):
    email: EmailStr
    captcha_token: str | None = None


@router.post("/resend-verification")
def resend_verification(body: EmailIn, request: Request, conn: RwConn) -> dict[str, bool]:
    require_captcha(request, body.captcha_token)
    s, mailer = request.app.state.settings, request.app.state.mailer
    user = accounts.user_by_email(conn, body.email)
    if user and user["email_verified_at"] is None:
        token = accounts.create_token(conn, user["id"], "verify_email", s.verify_token_seconds)
        mailer.send(user["email"], *mail.verification_mail(s.web_url, token))
    return {"ok": True}


# --- login and sessions -----------------------------------------------------------------------

class LoginIn(BaseModel):
    email: EmailStr
    password: str
    remember: bool = False
    captcha_token: str | None = None


@router.post("/login")
def login(body: LoginIn, request: Request, conn: RwConn) -> dict[str, Any]:
    s = request.app.state.settings
    email, ip = accounts.normalize_email(body.email), client_ip(request)
    keys = (f"login:email:{email}", f"login:ip:{ip}")
    limits = (s.login_failures_per_email, s.login_failures_per_ip)
    throttled = any(accounts.throttle_count(conn, k, s.throttle_window_seconds) >= n
                    for k, n in zip(keys, limits, strict=True))
    if throttled and not (body.captcha_token and security.captcha_ok(
            body.captcha_token, s.turnstile_secret, ip, request.app.state.http)):
        raise error(429, "captcha_required", "Too many attempts. Complete the captcha to continue.")
    user = accounts.user_by_email(conn, email)
    if not security.verify_password(user["password_hash"] if user else None, body.password):
        for k in keys:
            accounts.throttle_hit(conn, k)
        raise error(401, "invalid_credentials", "Incorrect email or password.")
    if user["email_verified_at"] is None:
        raise error(403, "email_unverified", "Confirm your email address before signing in.")
    return issue_session(request, conn, user["id"], remember=body.remember)


@router.post("/logout", status_code=204)
def logout(request: Request, user: User, conn: RwConn) -> None:
    accounts.revoke_session(conn, request.state.session_token)


@router.get("/me")
def me(user: User) -> dict[str, Any]:
    return public_user(user)


# --- password reset ---------------------------------------------------------------------------

@router.post("/forgot-password")
def forgot_password(body: EmailIn, request: Request, conn: RwConn) -> dict[str, bool]:
    require_captcha(request, body.captcha_token)
    s, mailer = request.app.state.settings, request.app.state.mailer
    user = accounts.user_by_email(conn, body.email)
    if user:
        token = accounts.create_token(conn, user["id"], "reset_password", s.reset_token_seconds)
        mailer.send(user["email"], *mail.reset_mail(s.web_url, token))
    return {"ok": True}


class ResetIn(BaseModel):
    token: str
    password: str


@router.post("/reset-password")
def reset_password(body: ResetIn, request: Request, conn: RwConn) -> dict[str, bool]:
    check_password(request, body.password)
    user_id = accounts.consume_token(conn, body.token, "reset_password")
    if user_id is None:
        raise error(400, "invalid_token", "This link is invalid or has expired.")
    accounts.set_password(conn, user_id, security.hash_password(body.password))
    accounts.mark_verified(conn, user_id)   # the link proved ownership of the address
    accounts.revoke_all_sessions(conn, user_id)
    return {"ok": True}


# --- social login -----------------------------------------------------------------------------

def provider_or_error(request: Request, name: str) -> oauth.Provider:
    if name not in ("google", "apple"):
        raise error(404, "unknown_provider", f"Unknown provider {name!r}.")
    p = oauth.providers(request.app.state.settings).get(name)
    if p is None:
        raise error(501, "provider_not_configured", f"Sign-in with {name} is not set up yet.")
    return p


def redirect_uri(request: Request, name: str) -> str:
    return f"{request.app.state.settings.web_url}/auth/callback/{name}"


class OAuthStartIn(BaseModel):
    redirect_to: str | None = None


@router.post("/oauth/{name}/start")
def oauth_start(name: str, body: OAuthStartIn, request: Request, conn: RwConn) -> dict[str, str]:
    p = provider_or_error(request, name)
    state, nonce = security.new_token(), security.new_token()
    verifier, challenge = oauth.pkce_pair()
    accounts.save_oauth_state(
        conn, state=state, provider=name, code_verifier=verifier, nonce=nonce,
        redirect_to=body.redirect_to, ttl_seconds=request.app.state.settings.oauth_state_seconds)
    url = oauth.authorize_url(p, redirect_uri=redirect_uri(request, name), state=state,
                              nonce=nonce, code_challenge=challenge)
    return {"authorize_url": url, "state": state}


class OAuthCallbackIn(BaseModel):
    code: str
    state: str
    user: str | None = None   # Apple: JSON with the name, sent on the first sign-in only


def apple_name(user_json: str | None) -> tuple[str, str]:
    try:
        name = json.loads(user_json or "{}").get("name") or {}
        return str(name.get("firstName", "")).strip(), str(name.get("lastName", "")).strip()
    except (ValueError, AttributeError):
        return "", ""


@router.post("/oauth/{name}/callback")
def oauth_callback(name: str, body: OAuthCallbackIn, request: Request,
                   conn: RwConn) -> dict[str, Any]:
    p = provider_or_error(request, name)
    handshake = accounts.pop_oauth_state(conn, body.state, name)
    if handshake is None:
        raise error(400, "invalid_state", "The sign-in attempt expired. Please try again.")
    try:
        tokens = oauth.exchange_code(p, request.app.state.http, code=body.code,
                                     redirect_uri=redirect_uri(request, name),
                                     code_verifier=handshake["code_verifier"])
        claims = oauth.verify_id_token(p, tokens.get("id_token", ""), nonce=handshake["nonce"])
    except oauth.OAuthError as e:
        raise error(400, "invalid_token", f"{name} did not confirm your identity.") from e

    subject = str(claims["sub"])
    email = accounts.normalize_email(claims.get("email") or "")
    verified = str(claims.get("email_verified", "")).lower() == "true"
    if not email:
        raise error(400, "email_required", f"{name} did not share an email address.")

    new_user = False
    user = accounts.user_by_identity(conn, name, subject)
    if user is None:
        user = accounts.user_by_email(conn, email)
        if user is None:
            first, last = ((str(claims.get("given_name", "")), str(claims.get("family_name", "")))
                           if name == "google" else apple_name(body.user))
            user = accounts.create_user(conn, email=email, first_name=first, last_name=last,
                                        email_verified=verified)
            new_user = True
        elif not verified:
            raise error(400, "email_unverified",
                        f"{name} has not verified this email address, so it cannot be linked.")
        accounts.link_identity(conn, name, subject, user["id"], email)
    if verified:
        accounts.mark_verified(conn, user["id"])
    session = issue_session(request, conn, user["id"], remember=True)
    return {**session, "new_user": new_user, "redirect_to": handshake["redirect_to"]}
```

- [ ] **Step 5: Wire it in `app/main.py`**

Replace `app/main.py` with:

```python
"""Car's Secrets API: accounts and read-only vehicle auction data for the web app."""

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Annotated

import httpx
import psycopg
from fastapi import APIRouter, Depends, FastAPI

from . import auth, lots
from .config import Settings, get_settings
from .db import make_pool
from .mail import Mailer, make_mailer


def create_app(settings: Settings | None = None, *, mailer: Mailer | None = None,
               http: httpx.Client | None = None) -> FastAPI:
    settings = settings or get_settings()
    pool = make_pool(settings.database_url)                     # ingest tables, read-only
    rw_pool = make_pool(settings.database_url, read_only=False)  # the `app` schema
    http = http or httpx.Client()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        pool.open()
        rw_pool.open()
        yield
        rw_pool.close()
        pool.close()
        http.close()

    app = FastAPI(title="Car's Secrets API", lifespan=lifespan, docs_url="/api/docs",
                  openapi_url="/api/openapi.json")
    app.state.settings, app.state.pool, app.state.rw_pool = settings, pool, rw_pool
    app.state.mailer, app.state.http = mailer or make_mailer(settings), http

    def db() -> Iterator[psycopg.Connection]:
        with pool.connection() as conn:
            yield conn

    Conn = Annotated[psycopg.Connection, Depends(db)]

    public = APIRouter(prefix="/api")
    # Routes for signed-in dealers take `auth.User`; `/api/lots` stays public for now.
    api = APIRouter(prefix="/api")

    @public.get("/health")
    def health(conn: Conn) -> dict[str, str]:
        conn.execute("SELECT 1")
        return {"status": "ok"}

    public.include_router(auth.router)
    api.include_router(lots.router(Conn))
    app.include_router(public)
    app.include_router(api)
    return app


def app_factory() -> FastAPI:  # uvicorn --factory entrypoint
    return create_app()
```

- [ ] **Step 6: Run everything**

Run: `uv run ruff check app tests migrations && uv run pytest -q`
Expected: all pass. Typical pitfalls: `EmailStr` needs `email-validator` (installed by `pydantic[email]`); `request.client` is set by TestClient (`testclient`); `settings` mutations in tests work because the fixture instance is the one on `app.state`.

- [ ] **Step 7: Commit**

```bash
git add app/auth.py app/main.py tests/conftest.py tests/test_auth.py
git commit -m "feat: register, verify, login, sessions, password reset and social login routes"
```

---

### Task 7: Live smoke test and Docker build

**Files:** none changed.

- [ ] **Step 1: Run against the ingest database**

```bash
cd /Users/lou/lab/cars-secrets-api
PW=$(grep '^POSTGRES_PASSWORD=' ../cars-secrets-data-ingest/.env | cut -d= -f2-)
export DATABASE_URL="postgresql://ingest:${PW}@localhost:5433/ingest"
uv run alembic upgrade head            # creates the app schema in the live database
uv run uvicorn app.main:app_factory --factory --port 8001 &
sleep 3
curl -s -X POST localhost:8001/api/auth/register -H 'content-type: application/json' \
  -d '{"first_name":"Test","last_name":"User","email":"smoke@example.com","password":"long-enough-pw","accept_terms":true}'
# the verification link is in the uvicorn log (LogMailer); copy the token
curl -s -X POST localhost:8001/api/auth/verify-email -H 'content-type: application/json' -d '{"token":"<token>"}'
curl -s -X POST localhost:8001/api/auth/login -H 'content-type: application/json' \
  -d '{"email":"smoke@example.com","password":"long-enough-pw","remember":true}'
curl -s localhost:8001/api/auth/me -H 'authorization: Bearer <token>'
curl -s localhost:8001/api/lots?limit=1 | head -c 200      # lots still served
kill %1
docker build -q -t cars-secrets-api .
```

Expected: `{"ok":true}`, `{"ok":true}`, a token, the profile, a lot, image built. Then delete the smoke user: `psql "$DATABASE_URL" -c "DELETE FROM app.users WHERE email = 'smoke@example.com'"` (or via `docker exec cars-ingest-db-1 psql -U ingest -d ingest -c ...`).

---

### Task 8: Documentation and environment

**Files:**
- Modify: `AGENTS.md` (stack, commands, architecture rules, security rules, migrations, tests, current state)
- Modify: `README.md` (endpoints table, env vars)
- Modify: `docs/architecture.md` (modules table, `app` schema, auth flows)
- Modify: `docs/security.md` (auth, sessions, throttling, captcha, OAuth, secrets)
- Modify: `.env.example` (all new variables with comments)
- Modify: `.github/workflows/ci.yml` (ruff also checks `migrations`)

- [ ] **Step 1: `.env.example`**

```dotenv
# The ingest database (owned by cars-secrets-data-ingest). Ingest tables are read-only from
# this API; accounts live in the `app` schema, migrated by this repo (`uv run alembic upgrade head`).
DATABASE_URL=postgresql://ingest:change-me@localhost:5433/ingest

# Public origin of the web app: links in emails and OAuth redirect URIs.
WEB_URL=http://localhost:3000
# Shared with the web server; lets the API trust X-Client-IP for rate limiting. 32+ random chars.
INTERNAL_API_KEY=

# Cloudflare Turnstile secret. Empty = captcha not enforced.
TURNSTILE_SECRET=
# Reject passwords found in Have I Been Pwned (k-anonymity lookup).
CHECK_BREACHED_PASSWORDS=true

# Outgoing mail. Empty SMTP_HOST = messages (with their links) go to the log.
SMTP_HOST=
SMTP_PORT=587
SMTP_USERNAME=
SMTP_PASSWORD=
MAIL_FROM=Car's Secrets <no-reply@localhost>

# Social login. A provider is offered only when all of its variables are set.
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
APPLE_CLIENT_ID=
APPLE_TEAM_ID=
APPLE_KEY_ID=
APPLE_PRIVATE_KEY=
```

- [ ] **Step 2: Update the documents**

`AGENTS.md` top half: add to the tech stack line "Alembic for the `app` schema, argon2-cffi, PyJWT, phonenumbers, httpx"; to commands add `uv run alembic upgrade head` and `uv run alembic revision -m "..."`, and change the ruff command to `uv run ruff check app tests migrations`; in architecture rules replace the "no migrations yet" bullets with:

```markdown
- Accounts live in the `app` schema (`migrations/versions/`, applied with `uv run alembic
  upgrade head`; tests apply them too). Migrations are raw SQL through `op.execute`, never
  import application code, and never touch `public` (the ingest repo's tables).
- Two pools: `app.state.pool` (read-only, ingest tables) and `app.state.rw_pool` (the `app`
  schema). Auth routes use `auth.RwConn`; lot routes keep the read-only `Conn`.
- Auth flows are in `app/auth.py`; storage in `app/accounts.py`; primitives in
  `app/security.py`, `app/mail.py`, `app/oauth.py`. Outbound HTTP goes through
  `app.state.http` so tests can mock it; mail through `app.state.mailer`.
- Routes for signed-in dealers take `user: auth.User` (bearer session). `/api/lots` is public.
```

Replace "## Migrations" with a short paragraph pointing at Alembic and the `app` schema; "## Security rules" with a pointer to `docs/security.md` plus "never log request bodies or tokens; every new unauthenticated write endpoint gets `require_captcha` and a throttle"; "## Current state" with the list of auth features, "Google and Apple built but need credentials", "no terms/privacy documents yet".

`docs/architecture.md`: add rows for `accounts.py`, `auth.py`, `security.py`, `mail.py`, `oauth.py`, `migrations/`; add an "Accounts (`app` schema)" section listing the tables; add "Auth flows" describing register → verify → login, reset, social login with the state/nonce/PKCE handshake and that the web app owns the redirect URIs `/auth/callback/{provider}`.

`docs/security.md`: replace "No authentication yet" with sessions (bearer, sha256 at rest, sliding expiry, 30 d / 12 h), passwords (Argon2id, 10–128, HIBP), throttling (3 per email / 10 per IP in 15 min, then captcha), captcha (Turnstile, skipped without secret), email tokens (hashed, single use, 24 h / 1 h, reset revokes sessions and never says whether an email exists), OAuth (PKCE, nonce, JWKS verification, identities linked only to verified emails, Apple client secret JWT), client IP trust (`X-Internal-Key`), secrets in `.env`.

`README.md`: add the `/api/auth/*` routes to the table and the new env vars.

`.github/workflows/ci.yml`: `- run: uv run ruff check app tests migrations`.

- [ ] **Step 3: Verify and commit**

Run: `uv run ruff check app tests migrations && uv run pytest -q`

```bash
git add AGENTS.md README.md docs .env.example .github/workflows/ci.yml
git commit -m "docs: accounts, sessions and social login"
```

---

### Task 9: Push and pull request

- [ ] **Step 1:** `git push -u origin feat/auth`
- [ ] **Step 2:** `gh pr create --base main --head feat/auth --title "feat: accounts, sessions, password reset and social login" --body "<what changed, why, validation>"` ending with `🤖 Generated with [Claude Code](https://claude.com/claude-code)`. Mention that the web repo's follow-up PR consumes this contract.

---

## Self-review

- **Spec coverage:** register (T6), verify + resend (T6), login with captcha after failures and per-IP throttle (T6), logout/me (T6), forgot/reset with session revocation (T6), OAuth start/callback with PKCE/nonce/JWKS and 501 when unconfigured (T5, T6), remember-me durations (T4, T6), Argon2id + HIBP (T2), Turnstile (T2), SMTP/log mail (T3), `app` schema + Alembic (T1), client IP trust (T6), docs and env (T8). Terms checkbox recorded as `terms_accepted_at` (T1, T6). Phone E.164 (T6).
- **Placeholders:** none; every step has its code.
- **Type consistency:** `create_session(conn, user_id, *, remember, remember_seconds, idle_seconds)` and `session_user(conn, token, *, remember_seconds, idle_seconds)` are used with the same keyword names in T4 and T6; `pop_oauth_state` returns `code_verifier`, `nonce`, `redirect_to` (T4) and T6 reads exactly those; `Provider.client_secret` is a callable (T5) and `exchange_code` calls it.
