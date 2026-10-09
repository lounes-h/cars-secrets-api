import logging
from datetime import datetime, timedelta

import pytest
from cryptography.exceptions import InvalidTag

from app import outbox
from app.mail import Mail

NOW = datetime.fromisoformat("2026-10-08T12:00Z")
MAIL = Mail("Subject 042917", "Code 042917", "<p>Code 042917</p>")


def queue(client, conn, to="dana@example.com", kind="verify", minutes=10, **kwargs) -> int:
    return outbox.queue(conn, client.app.state.mail_key, kind, to, MAIL, now=NOW,
                        expires_at=NOW + timedelta(minutes=minutes), **kwargs)


def flush(client, at=NOW, ids=None) -> None:
    outbox.flush(client.app.state, ids=ids, now=at)


def rows(conn) -> list[dict]:
    return conn.execute("SELECT id, kind, to_address, attempts, send_after, last_error "
                        "FROM app.outbox ORDER BY id").fetchall()


def test_a_body_is_sealed_for_its_address():
    key = outbox.derive_key(b"secret")
    body = outbox.seal(key, "a@b.test", MAIL)
    assert b"042917" not in body
    assert outbox.unseal(key, "a@b.test", body) == MAIL
    with pytest.raises(InvalidTag):
        outbox.unseal(key, "eve@b.test", body)
    with pytest.raises(InvalidTag):
        outbox.unseal(outbox.derive_key(b"other"), "a@b.test", body)
    assert outbox.derive_key(b"secret") != b"secret".ljust(32, b"\0")


def test_a_queued_email_is_sent_once_and_deleted(client, mailer, conn):
    queue(client, conn)
    stored = conn.execute("SELECT body FROM app.outbox").fetchone()["body"]
    assert b"042917" not in bytes(stored), "the table never shows the code"

    flush(client)
    flush(client)

    assert mailer.sent == [("dana@example.com", "Subject 042917", "Code 042917")]
    assert mailer.html == ["<p>Code 042917</p>"] and rows(conn) == []


def test_only_the_given_emails_are_sent_when_asked(client, mailer, conn):
    first = queue(client, conn, "a@b.test")
    queue(client, conn, "c@d.test")

    flush(client, ids=[first])

    assert [to for to, _, _ in mailer.sent] == ["a@b.test"]
    assert [row["to_address"] for row in rows(conn)] == ["c@d.test"]


def test_a_failed_send_is_tried_again_later_then_dropped(client, mailer, conn, caplog):
    queue(client, conn, minutes=24 * 60)
    mailer.failures = 5
    at = NOW
    with caplog.at_level(logging.WARNING, logger="app.outbox"):
        for attempt, wait in enumerate([1, 5, 15, 60], start=1):
            flush(client, at)
            [row] = rows(conn)
            assert row["attempts"] == attempt and row["send_after"] == at + timedelta(minutes=wait)
            assert "SMTPServerDisconnected" in row["last_error"]
            flush(client, at + timedelta(minutes=wait) - timedelta(seconds=1))
            assert rows(conn)[0]["attempts"] == attempt, "not before its time"
            at += timedelta(minutes=wait)
        flush(client, at)

    assert rows(conn) == [] and mailer.sent == []
    assert "dropped after 5 attempts" in caplog.text


def test_a_failed_send_that_succeeds_later_goes_out_once(client, mailer, conn):
    queue(client, conn)
    mailer.failures = 1

    flush(client)
    flush(client, NOW + timedelta(minutes=1))

    assert len(mailer.sent) == 1 and rows(conn) == []


def test_an_email_past_its_time_is_dropped_unsent(client, mailer, conn, caplog):
    queue(client, conn, minutes=10)
    with caplog.at_level(logging.WARNING, logger="app.outbox"):
        flush(client, NOW + timedelta(minutes=10))

    assert mailer.sent == [] and rows(conn) == []
    assert "expired unsent after 0 attempts" in caplog.text


def test_an_email_this_key_cannot_read_is_dropped(client, mailer, conn, caplog):
    outbox.queue(conn, outbox.derive_key(b"an earlier secret"), "verify", "a@b.test", MAIL,
                 now=NOW, expires_at=NOW + timedelta(minutes=10))
    with caplog.at_level(logging.WARNING, logger="app.outbox"):
        flush(client)

    assert mailer.sent == [] and rows(conn) == []
    assert "cannot read it" in caplog.text


def test_a_new_email_replaces_an_unsent_one_of_its_kind_only(client, mailer, conn):
    queue(client, conn, kind="verify")
    queue(client, conn, kind="reset")
    queue(client, conn, "eve@example.com", kind="verify")

    queue(client, conn, kind="verify", replace=True)

    assert [(row["kind"], row["to_address"]) for row in rows(conn)] == [
        ("reset", "dana@example.com"), ("verify", "eve@example.com"),
        ("verify", "dana@example.com")]


def test_an_email_another_sender_holds_is_left_alone(client, mailer, conn):
    queue(client, conn)
    taken = outbox.claim(conn, NOW, None)

    flush(client)
    assert mailer.sent == [] and len(taken) == 1, "leased for five minutes"
    flush(client, NOW + outbox.LEASE)
    assert len(mailer.sent) == 1, "a sender that died leaves it to the next"
