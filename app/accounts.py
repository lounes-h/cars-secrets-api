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


# --- one-time tokens (password reset) ---------------------------------------------------------

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


# --- email confirmation codes -----------------------------------------------------------------

def save_email_code(conn: psycopg.Connection, user_id: UUID, code_hash: bytes,
                    ttl_seconds: int) -> None:
    """One live code per user: a new one replaces the last and its spent attempts."""
    conn.execute(
        """INSERT INTO app.email_codes (user_id, code_hash, expires_at)
           VALUES (%s, %s, now() + make_interval(secs => %s))
           ON CONFLICT (user_id) DO UPDATE
           SET code_hash = excluded.code_hash, attempts = 0, created_at = now(),
               expires_at = excluded.expires_at""",
        (user_id, code_hash, ttl_seconds),
    )


def email_code_sent_within(conn: psycopg.Connection, user_id: UUID, seconds: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM app.email_codes WHERE user_id = %s "
        "AND created_at > now() - make_interval(secs => %s)", (user_id, seconds),
    ).fetchone() is not None


def spend_email_code_attempt(conn: psycopg.Connection, user_id: UUID,
                             max_attempts: int) -> bytes | None:
    """Uses up one guess and returns the stored hash, or None when there is no live code
    (none, expired, or out of guesses). The increment is one statement, so parallel guesses
    cannot share an attempt."""
    row = conn.execute(
        """UPDATE app.email_codes SET attempts = attempts + 1
           WHERE user_id = %s AND attempts < %s AND expires_at > now()
           RETURNING code_hash""", (user_id, max_attempts),
    ).fetchone()
    return bytes(row["code_hash"]) if row else None


def consume_email_code(conn: psycopg.Connection, user_id: UUID, code_hash: bytes) -> bool:
    """Deletes the code if it is still the one checked; only one caller can win."""
    return conn.execute(
        "DELETE FROM app.email_codes WHERE user_id = %s AND code_hash = %s RETURNING user_id",
        (user_id, code_hash),
    ).fetchone() is not None


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
