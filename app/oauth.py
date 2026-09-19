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
