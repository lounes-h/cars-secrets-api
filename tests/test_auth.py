import hashlib
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app import alerts, oauth

RSA_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
GOOD = {"first_name": "Dana", "last_name": "Lee", "email": "Dana@Example.com",
        "phone": "+14155552671", "password": "long-enough-pw", "accept_terms": True,
        "locale": "en", "time_zone": "UTC"}
INTERNAL = {"X-Internal-Key": "k" * 32, "X-Client-IP": "9.9.9.9"}


def register(client, **overrides):
    return client.post("/api/auth/register", json={**GOOD, **overrides})


def login(client, password="long-enough-pw", **extra):
    return client.post("/api/auth/login",
                       json={"email": "dana@example.com", "password": password, **extra})


def verify(client, code, email="dana@example.com", **kwargs):
    return client.post("/api/auth/verify-email", json={"email": email, "code": code}, **kwargs)


def wrong(code: str) -> str:
    return f"{(int(code) + 1) % 1_000_000:06d}"


def verified_user(client, mailer):
    assert register(client).status_code == 200
    assert verify(client, mailer.last_code()).status_code == 200


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

    code = mailer.last_code()
    r = verify(client, code, email="DANA@example.com ")
    assert r.status_code == 200
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {r.json()['token']}"})
    assert me.json()["email_verified"] is True, "the code signs the dealer in"
    assert verify(client, code).json()["detail"]["code"] == "invalid_code", "single use"

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
    ("password", "short"), ("password", "x" * 129), ("password", "alldigits1"),
    ("phone", "12345"), ("phone", "abc"), ("accept_terms", False),
    ("email", "not-an-email"), ("email", f"{'a' * 250}@example.com"), ("first_name", " "),
])
def test_register_validation(client, conn, field, value):
    r = register(client, **{field: value})
    assert r.status_code == 422, r.text
    assert any(field in err["loc"] for err in r.json()["detail"])
    assert conn.execute("SELECT count(*) AS n FROM app.users").fetchone()["n"] == 0


def test_register_accepts_the_shortest_allowed_password(client, conn):
    """The web form asks for the same: 8 characters with one that is not a letter or a digit."""
    assert register(client, password="abcdefg!").status_code == 200
    assert conn.execute("SELECT count(*) AS n FROM app.users").fetchone()["n"] == 1


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
    for n in range(10):
        client.post("/api/auth/login", headers=INTERNAL,
                    json={"email": f"nobody{n}@example.com", "password": "wrong-password"})
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


def test_resend_verification(client, mailer, settings):
    settings.verify_code_resend_seconds = 0
    register(client)
    n = len(mailer.sent)
    assert client.post("/api/auth/resend-verification",
                       json={"email": "dana@example.com"}).status_code == 200
    assert len(mailer.sent) == n + 1
    assert client.post("/api/auth/resend-verification",
                       json={"email": "nobody@example.com"}).status_code == 200
    assert len(mailer.sent) == n + 1


def test_an_address_cannot_be_mailed_over_and_over(client, mailer, settings):
    """Anyone can name any address here, so the number of mails it can trigger is capped —
    without telling the caller anything: the answer stays {"ok": true}."""
    settings.mails_per_email = 2
    settings.verify_code_resend_seconds = 0
    register(client)                      # the verification mail itself counts as one
    n = len(mailer.sent)
    resend = lambda: client.post("/api/auth/resend-verification",  # noqa: E731
                                 json={"email": "dana@example.com"})
    assert resend().status_code == 200 and len(mailer.sent) == n + 1
    assert resend().status_code == 200
    assert len(mailer.sent) == n + 1, "the third mail to that address is not sent"


def test_forgot_password_is_capped_per_address(client, mailer, settings):
    settings.mails_per_email = 1
    verified_user(client, mailer)
    n = len(mailer.sent)
    for _ in range(3):
        assert client.post("/api/auth/forgot-password",
                           json={"email": "dana@example.com"}).status_code == 200
    assert len(mailer.sent) == n + 1


def test_registering_again_stops_mailing_the_owner(client, mailer, settings):
    settings.mails_per_email = 1
    register(client)
    n = len(mailer.sent)
    assert register(client).status_code == 200
    assert len(mailer.sent) == n + 1      # the "you already have an account" hint
    assert register(client).status_code == 200
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
    to, subject, body = mailer.sent[-1]
    assert (to, subject) == ("dana@example.com", "Your password was changed")
    assert "https://web.test/forgot-password" in body


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


# --- the language of every email --------------------------------------------------------------

def test_registering_keeps_the_pages_language_and_the_browsers_zone(client, mailer, conn):
    register(client, locale="es", time_zone="America/Chicago")

    assert conn.execute("SELECT locale, time_zone FROM app.alert_settings").fetchone() == {
        "locale": "es", "time_zone": "America/Chicago"}
    to, subject, body = mailer.sent[-1]
    assert subject == "Tu código de confirmación" and "Ingresa este código" in body
    assert '<html lang="es">' in mailer.html[-1]


def test_registering_requires_the_language_and_zone_and_mends_an_unknown_zone(client, conn):
    assert register(client, locale=None).status_code == 422
    assert register(client, locale="fr").status_code == 422
    assert register(client, time_zone=None).status_code == 422
    assert register(client, time_zone="Mars/Olympus").status_code == 200
    assert conn.execute("SELECT time_zone FROM app.alert_settings").fetchone() == {
        "time_zone": "UTC"}


def test_every_account_email_follows_the_language_settings_hold(client, mailer, conn):
    register(client, locale="es")
    verify(client, mailer.last_code())

    register(client, locale="en")  # someone else, on the English page: the owner reads Spanish
    assert mailer.sent[-1][1] == "Ya tienes una cuenta"
    assert "https://web.test/es/login" in mailer.sent[-1][2]

    conn.execute("UPDATE app.alert_settings SET locale = 'en'")  # Settings changed it
    client.post("/api/auth/forgot-password", json={"email": "dana@example.com"})
    assert mailer.sent[-1][1] == "Reset your password"
    assert "https://web.test/reset-password?token=" in mailer.sent[-1][2]

    conn.execute("UPDATE app.alert_settings SET locale = 'es'")
    client.post("/api/auth/reset-password",
                json={"token": mailer.last_link("/reset-password"), "password": "brand-new-pw!"})
    assert mailer.sent[-1][1] == "Tu contraseña cambió"
    assert "https://web.test/es/forgot-password" in mailer.sent[-1][2]


def test_an_account_without_a_language_is_mailed_in_english(client, mailer, conn):
    register(client, locale="es")
    conn.execute("DELETE FROM app.alert_settings")  # as a Google or Apple sign-up has none
    client.post("/api/auth/forgot-password", json={"email": "dana@example.com"})
    assert mailer.sent[-1][1] == "Reset your password"


# --- when sending fails -------------------------------------------------------------------------

def test_a_code_that_could_not_be_sent_goes_out_at_the_next_run(client, mailer, conn):
    mailer.failures = 1
    assert register(client).json() == {"ok": True}, "the answer never changes"
    assert mailer.sent == []

    alerts.tick(client.app.state, now=datetime.now(UTC) + timedelta(minutes=1))

    assert verify(client, mailer.last_code()).status_code == 200


def test_a_new_code_takes_the_place_of_one_still_unsent(client, mailer, conn):
    mailer.failures = 1
    register(client)
    conn.execute("UPDATE app.email_codes SET created_at = now() - interval '2 minutes'")
    client.post("/api/auth/resend-verification", json={"email": "dana@example.com"})
    alerts.tick(client.app.state, now=datetime.now(UTC) + timedelta(minutes=1))

    assert len(mailer.sent) == 1, "the first code is void, so it is never sent"
    assert verify(client, mailer.last_code()).status_code == 200


# --- email confirmation codes -----------------------------------------------------------------

def test_the_confirmation_mail_holds_a_code_and_no_link(client, mailer, conn):
    register(client)
    to, subject, body = mailer.sent[-1]
    code = mailer.last_code()
    assert to == "dana@example.com" and code not in subject and "http" not in body
    row = conn.execute("SELECT code_hash, attempts FROM app.email_codes").fetchone()
    assert code.encode() not in bytes(row["code_hash"]) and row["attempts"] == 0


@pytest.mark.parametrize("code", ["12345", "1234567", "12a456", "", "١٢٣٤٥٦", " 123456"])
def test_verify_rejects_malformed_codes(client, code):
    assert verify(client, code).status_code == 422


def test_verify_answers_the_same_for_every_failure(client, mailer, conn):
    """Unknown address, wrong digits and an already confirmed account are indistinguishable."""
    register(client)
    code = mailer.last_code()
    answers = [verify(client, wrong(code)).json(),
               verify(client, code, "nobody@example.com").json()]
    assert verify(client, code).status_code == 200
    answers.append(verify(client, code).json())
    assert all(a == answers[0] for a in answers) and answers[0]["detail"]["code"] == "invalid_code"


def test_a_code_allows_five_guesses_then_dies(client, mailer):
    register(client)
    code = mailer.last_code()
    for _ in range(5):
        assert verify(client, wrong(code)).status_code == 400
    assert verify(client, code).status_code == 400, "even the right code is refused now"


def test_an_expired_code_is_refused(client, mailer, conn):
    register(client)
    conn.execute("UPDATE app.email_codes SET expires_at = now() - interval '1 second'")
    assert verify(client, mailer.last_code()).status_code == 400


def test_a_new_code_replaces_the_last(client, mailer, conn):
    register(client)
    first = mailer.last_code()
    verify(client, wrong(first))
    conn.execute("UPDATE app.email_codes SET created_at = now() - interval '2 minutes'")
    client.post("/api/auth/resend-verification", json={"email": "dana@example.com"})
    second = mailer.last_code()
    assert conn.execute("SELECT attempts FROM app.email_codes").fetchone()["attempts"] == 0
    if second != first:
        assert verify(client, first).status_code == 400
    assert verify(client, second).status_code == 200


def test_wrong_codes_lock_the_address(client, mailer, settings):
    settings.verify_code_attempts = 100
    register(client)
    code = mailer.last_code()
    for _ in range(settings.code_failures_per_email):
        assert verify(client, wrong(code)).status_code == 400
    r = verify(client, code)
    assert r.status_code == 429 and r.json()["detail"]["code"] == "too_many_attempts"


def test_wrong_codes_lock_the_client(client, mailer, settings):
    register(client)
    code = mailer.last_code()
    for n in range(settings.code_failures_per_ip):
        verify(client, "000000", f"nobody{n}@example.com", headers=INTERNAL)
    assert verify(client, code, headers=INTERNAL).status_code == 429
    # Another client is not affected.
    other = {**INTERNAL, "X-Client-IP": "8.8.8.8"}
    assert verify(client, code, headers=other).status_code == 200


def test_resending_waits_a_minute_between_codes(client, mailer, conn):
    register(client)
    n = len(mailer.sent)
    resend = lambda: client.post("/api/auth/resend-verification",  # noqa: E731
                                 json={"email": "dana@example.com"})
    assert resend().json() == {"ok": True} and len(mailer.sent) == n
    conn.execute("UPDATE app.email_codes SET created_at = now() - interval '61 seconds'")
    assert resend().json() == {"ok": True} and len(mailer.sent) == n + 1


def test_signing_in_unconfirmed_sends_a_code_only_with_the_right_password(client, mailer, conn):
    register(client)
    conn.execute("UPDATE app.email_codes SET created_at = now() - interval '2 minutes'")
    n = len(mailer.sent)
    assert login(client, password="wrong-password").status_code == 401
    assert len(mailer.sent) == n
    r = login(client)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "email_unverified"
    assert len(mailer.sent) == n + 1
    r = client.post("/api/auth/verify-email", json={"email": "dana@example.com",
                                                    "code": mailer.last_code(), "remember": True})
    left = datetime.fromisoformat(r.json()["expires_at"]) - datetime.now(UTC)
    assert left > timedelta(days=29), "remember me survives the code step"
    assert login(client).status_code == 200
