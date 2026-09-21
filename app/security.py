"""Password hashing, opaque tokens, breached-password check and captcha verification."""

import hashlib
import secrets

import httpx
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

MIN_PASSWORD, MAX_PASSWORD = 8, 128
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
