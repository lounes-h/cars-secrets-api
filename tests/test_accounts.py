from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from app import accounts
from app.security import hash_password, token_hash


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
                 "WHERE token_hash = %s", (token_hash(short),))
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
    token = accounts.create_token(conn, user["id"], "reset_password", 60)
    assert accounts.consume_token(conn, token, "verify_email") is None     # wrong purpose
    assert accounts.consume_token(conn, token, "reset_password") == user["id"]
    assert accounts.consume_token(conn, token, "reset_password") is None   # single use
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
