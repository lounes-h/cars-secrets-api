import hashlib
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


def query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


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


def test_development_setting_lets_unverified_accounts_sign_in(client, mailer, settings, caplog):
    register(client)
    settings.skip_email_verification = True
    r = login(client)
    assert r.status_code == 200
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {r.json()['token']}"})
    assert me.json()["email_verified"] is False   # still unverified, only not enforced
    assert login(client, password="wrong-password").status_code == 401


def test_skipping_verification_is_logged_at_startup(settings, caplog):
    import logging

    from app.main import create_app
    settings.skip_email_verification = True
    with caplog.at_level(logging.WARNING, logger="app.main"):
        create_app(settings)
    assert "SKIP_EMAIL_VERIFICATION is on" in caplog.text


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
    settings.turnstile_secret = "sec"
    http_handler.fn = lambda r: httpx.Response(200, json={"success": False})
    verified_user_with_captcha(client, mailer, http_handler)
    http_handler.fn = lambda r: httpx.Response(200, json={"success": False})
    for _ in range(3):
        r = login(client, password="wrong-password")
        assert r.status_code == 401 and r.json()["detail"]["code"] == "invalid_credentials"
    r = login(client)   # right password, but throttled
    assert r.status_code == 429 and r.json()["detail"]["code"] == "captcha_required"
    assert login(client, captcha_token="bad").status_code == 429
    http_handler.fn = lambda r: httpx.Response(200, json={"success": True})
    assert login(client, captcha_token="tok").status_code == 200


def test_throttle_does_not_lock_out_when_no_captcha_is_configured(client, mailer):
    # Regression: with TURNSTILE_SECRET empty the form shows no captcha, yet after three
    # failures the API answered 429 captcha_required for the whole 15-minute window.
    verified_user(client, mailer)
    for _ in range(3):
        assert login(client, password="wrong-password").status_code == 401
    assert login(client).status_code == 200


def verified_user_with_captcha(client, mailer, http_handler):
    http_handler.fn = lambda r: httpx.Response(200, json={"success": True})
    assert register(client, captcha_token="tok").status_code == 200
    token = mailer.last_link("/verify-email")
    assert client.post("/api/auth/verify-email", json={"token": token}).status_code == 200


def test_login_throttles_per_client_ip(client, mailer, settings, http_handler):
    verified_user(client, mailer)
    settings.turnstile_secret = "sec"
    http_handler.fn = lambda r: httpx.Response(200, json={"success": False})
    for n in range(10):
        client.post("/api/auth/login", headers=INTERNAL,
                    json={"email": f"nobody{n}@example.com", "password": "wrong-password"})
    r = client.post("/api/auth/login", headers=INTERNAL,
                    json={"email": "dana@example.com", "password": "long-enough-pw"})
    assert r.status_code == 429
    settings.turnstile_secret = ""   # the remaining check is about which address is counted
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


def test_resend_verification(client, mailer):
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
    return r.json()["state"], query(r.json()["authorize_url"])["nonce"]


def test_oauth_unconfigured_provider(client):
    r = client.post("/api/auth/oauth/google/start", json={})
    assert r.status_code == 501 and r.json()["detail"]["code"] == "provider_not_configured"
    assert client.post("/api/auth/oauth/github/start", json={}).status_code == 404


def test_google_sign_in_creates_then_reuses_the_user(client, google, http_handler, conn):
    state, nonce = start_google(client)
    q = query(client.post("/api/auth/oauth/google/start", json={}).json()["authorize_url"])
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
    state, nonce = r.json()["state"], query(r.json()["authorize_url"])["nonce"]
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
