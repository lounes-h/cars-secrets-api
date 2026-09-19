import base64
import hashlib
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
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
