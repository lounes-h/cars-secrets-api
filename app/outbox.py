"""Outgoing email, kept in `app.outbox` until it is sent, so a failed send is tried again.

`queue` stores an email in the caller's transaction: an alert and its email are stored together
or not at all. `flush` sends what is due: the auth routes right after queuing, the alert job after
each run, which also retries the failures. A sender takes its rows for LEASE (FOR UPDATE SKIP
LOCKED), so two processes never send the same email and a sender that dies leaves its rows to the
next. A sent email is deleted. A failed one is tried again after each RETRY delay, and dropped
after its last attempt or once it expires: a code or a reminder is worth nothing late.

Bodies are encrypted (AES-GCM, keyed from EMAIL_CODE_SECRET and bound to the address), so the
table never shows a live confirmation code or reset link."""

import hashlib
import json
import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .mail import Mail

log = logging.getLogger(__name__)

RETRY = (timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=15), timedelta(hours=1))
LEASE = timedelta(minutes=5)
BATCH = 50


def derive_key(secret: bytes) -> bytes:
    """The outbox's AES-256 key, its own, derived from the confirmation codes' secret."""
    return hashlib.sha256(b"outbox\0" + secret).digest()


def seal(key: bytes, to: str, mail: Mail) -> bytes:
    nonce = os.urandom(12)
    return nonce + AESGCM(key).encrypt(nonce, json.dumps(mail).encode(), to.encode())


def unseal(key: bytes, to: str, body: bytes) -> Mail:
    return Mail(*json.loads(AESGCM(key).decrypt(body[:12], body[12:], to.encode())))


def queue(conn: psycopg.Connection, key: bytes, kind: str, to: str, mail: Mail, *,
          now: datetime, expires_at: datetime, replace: bool = False) -> int:
    """Stores an email to send from `now` until `expires_at`. With `replace`, it takes the place
    of any unsent email of its kind to that address: a new confirmation code voids the last."""
    with conn.transaction():
        if replace:
            conn.execute("DELETE FROM app.outbox WHERE kind = %s AND to_address = %s", (kind, to))
        return conn.execute(
            """INSERT INTO app.outbox (kind, to_address, body, send_after, expires_at)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (kind, to, seal(key, to, mail), now, expires_at)).fetchone()["id"]


def flush(state: Any, ids: list[int] | None = None, now: datetime | None = None) -> None:
    """Sends the emails that are due (only `ids`, when given), a batch at a time."""
    now = now or datetime.now(UTC)
    with state.rw_pool.connection() as conn:
        for row in conn.execute("""DELETE FROM app.outbox WHERE expires_at <= %s
                                   RETURNING id, kind, attempts""", (now,)):
            log.warning("mail %s (%s) expired unsent after %s attempts", row["id"], row["kind"],
                        row["attempts"])
        while rows := claim(conn, now, ids):
            for row in rows:
                deliver(conn, state, row, now)


def claim(conn: psycopg.Connection, now: datetime, ids: list[int] | None) -> list[dict[str, Any]]:
    rows = conn.execute(
        """UPDATE app.outbox SET send_after = %(lease)s, attempts = attempts + 1
           WHERE id IN (SELECT id FROM app.outbox
                        WHERE send_after <= %(now)s AND expires_at > %(now)s
                          AND (%(ids)s::bigint[] IS NULL OR id = ANY(%(ids)s))
                        ORDER BY id LIMIT %(batch)s FOR UPDATE SKIP LOCKED)
           RETURNING id, kind, to_address, body, attempts""",
        {"now": now, "lease": now + LEASE, "ids": ids, "batch": BATCH}).fetchall()
    return sorted(rows, key=lambda row: row["id"])


def deliver(conn: psycopg.Connection, state: Any, row: dict[str, Any], now: datetime) -> None:
    done = "DELETE FROM app.outbox WHERE id = %s"
    try:
        mail = unseal(state.mail_key, row["to_address"], row["body"])
    except InvalidTag:
        # Sealed under another key: EMAIL_CODE_SECRET changed, or a development process restarted.
        conn.execute(done, (row["id"],))
        log.warning("mail %s (%s) dropped: this EMAIL_CODE_SECRET cannot read it", row["id"],
                    row["kind"])
        return
    try:
        state.mailer.send(row["to_address"], *mail)
    except Exception as e:
        attempts = row["attempts"]
        if attempts > len(RETRY):
            conn.execute(done, (row["id"],))
            log.error("mail %s (%s) dropped after %s attempts: %r", row["id"], row["kind"],
                      attempts, e)
        else:
            conn.execute("UPDATE app.outbox SET send_after = %s, last_error = %s WHERE id = %s",
                         (now + RETRY[attempts - 1], repr(e)[:500], row["id"]))
            log.warning("mail %s (%s) failed, attempt %s: %r", row["id"], row["kind"], attempts,
                        e)
        return
    conn.execute(done, (row["id"],))
