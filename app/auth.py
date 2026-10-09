"""Accounts: registration, email verification, login, sessions, password reset, social login."""

import hmac
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

import phonenumbers
import psycopg
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field

from . import accounts, mail, oauth, outbox, security

router = APIRouter(prefix="/auth", tags=["auth"])


# --- dependencies -----------------------------------------------------------------------------

def rw_conn(request: Request) -> Iterator[psycopg.Connection]:
    with request.app.state.rw_pool.connection() as conn:
        yield conn


RwConn = Annotated[psycopg.Connection, Depends(rw_conn)]


def error(status: int, code: str, message: str) -> HTTPException:
    return HTTPException(status, {"code": code, "message": message})


def field_error(field: str, message: str) -> HTTPException:
    return HTTPException(422, [{"loc": ["body", field], "msg": message, "type": "value_error"}])


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
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    user = None
    if scheme.lower() == "bearer" and token:
        user = accounts.session_user(conn, token, remember_seconds=s.session_remember_seconds,
                                     idle_seconds=s.session_idle_seconds)
    if user is None:
        raise error(401, "not_authenticated", "Sign in to continue.")
    request.state.session_token = token
    return user


User = Annotated[dict[str, Any], Depends(current_user)]


def require_captcha(request: Request, token: str | None) -> None:
    s = request.app.state.settings
    if not security.captcha_ok(token, s.turnstile_secret, client_ip(request),
                               request.app.state.http):
        raise error(400, "captcha_failed", "Please complete the captcha and try again.")


def mail_allowed(request: Request, conn: psycopg.Connection, kind: str, email: str) -> bool:
    """Whether we may mail this address again. Anyone can name any address in register,
    resend-verification and forgot-password, so each one is capped per address and per client
    over the throttle window. The answer to the caller never changes: still `{"ok": true}`."""
    s = request.app.state.settings
    keys = (f"mail:{kind}:{email}", f"mail:ip:{client_ip(request)}")
    limits = (s.mails_per_email, s.mails_per_ip)
    if any(accounts.throttle_count(conn, k, s.throttle_window_seconds) >= n
           for k, n in zip(keys, limits, strict=True)):
        return False
    for k in keys:
        accounts.throttle_hit(conn, k)
    return True


def send_mail(request: Request, conn: psycopg.Connection, kind: str, to: str, message: mail.Mail,
              seconds: int, *, replace: bool = False) -> None:
    """Queues an email that is worth sending for `seconds`, and sends it now. When that fails,
    the alert job tries again (app/outbox.py); the caller's answer stays the same."""
    state, now = request.app.state, datetime.now(UTC)
    sent = outbox.queue(conn, state.mail_key, kind, to, message, now=now,
                        expires_at=now + timedelta(seconds=seconds), replace=replace)
    outbox.flush(state, ids=[sent], now=now)


# A notice stays worth sending for a day.
NOTICE_SECONDS = 24 * 3600


def check_password(request: Request, password: str) -> None:
    if not security.MIN_PASSWORD <= len(password) <= security.MAX_PASSWORD:
        raise field_error("password", f"Use {security.MIN_PASSWORD} to {security.MAX_PASSWORD} "
                          "characters.")
    if all(character.isalnum() for character in password):
        raise field_error("password", "Add at least one special character, like ! ? or #.")
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
# The longest address RFC 5321 allows, and what the web form lets anyone type.
Email = Annotated[EmailStr, Field(max_length=254)]


# --- registration and verification ------------------------------------------------------------

class RegisterIn(accounts.Context):
    first_name: Name
    last_name: Name
    email: Email
    phone: str | None = None
    password: str
    accept_terms: bool
    captcha_token: str | None = None
    # The page's language and the browser's zone: every email's from now on, until Settings.
    locale: Literal["en", "es"]
    time_zone: str = Field(max_length=64)


@router.post("/register")
def register(body: RegisterIn, request: Request, conn: RwConn) -> dict[str, bool]:
    """Always answers ok: an existing address gets a hint mail instead of a new account."""
    require_captcha(request, body.captcha_token)
    if not body.accept_terms:
        raise field_error("accept_terms", "You must accept the Terms of Use and Privacy Policy.")
    phone = normalize_phone(body.phone)
    check_password(request, body.password)
    s = request.app.state.settings
    email = accounts.normalize_email(body.email)
    if owner := accounts.user_by_email(conn, email):
        if mail_allowed(request, conn, "existing", email):
            send_mail(request, conn, "existing", email, mail.existing_account_mail(
                accounts.email_locale(conn, owner["id"]), s.web_url), NOTICE_SECONDS)
        return {"ok": True}
    user = accounts.create_user(
        conn, email=email, first_name=body.first_name.strip(), last_name=body.last_name.strip(),
        phone=phone, password_hash=security.hash_password(body.password), terms_accepted=True)
    accounts.seed_settings(conn, user["id"], body)
    send_code(request, conn, user)
    return {"ok": True}


def send_code(request: Request, conn: psycopg.Connection, user: dict[str, Any]) -> None:
    """Mails a new confirmation code, replacing the last one, unless one went out less than a
    minute ago or the address has had its share of mail. Callers answer the same either way."""
    s = request.app.state.settings
    if (accounts.email_code_sent_within(conn, user["id"], s.verify_code_resend_seconds)
            or not mail_allowed(request, conn, "verify", user["email"])):
        return
    code = security.new_code()
    accounts.save_email_code(conn, user["id"],
                             security.code_hash(request.app.state.code_key, user["id"], code),
                             s.verify_code_seconds)
    send_mail(request, conn, "verify", user["email"], mail.verification_mail(
        code, s.verify_code_seconds // 60, accounts.email_locale(conn, user["id"]), s.web_url),
        s.verify_code_seconds, replace=True)


# Six ASCII digits; `\d` would also accept other scripts' digits.
Code = Annotated[str, Field(pattern=r"^[0-9]{6}$")]


class VerifyEmailIn(BaseModel):
    email: Email
    code: Code
    remember: bool = False   # carried over when the code step interrupts a login


@router.post("/verify-email")
def verify_email(body: VerifyEmailIn, request: Request, conn: RwConn) -> dict[str, Any]:
    """Confirms the address with the mailed code and signs the dealer in: the code proves they
    own the inbox, which is all a password reset would ask for anyway.

    Every failure is the same `400 invalid_code`, whether the address is unknown, already
    confirmed, has no live code or got the digits wrong, so it reveals nothing about accounts.
    Each code allows `verify_code_attempts` guesses; wrong guesses are also counted per address
    and per client, and past either limit all guesses are refused for the throttle window."""
    s = request.app.state.settings
    email = accounts.normalize_email(body.email)
    keys = (f"code:email:{email}", f"code:ip:{client_ip(request)}")
    limits = (s.code_failures_per_email, s.code_failures_per_ip)
    if any(accounts.throttle_count(conn, k, s.throttle_window_seconds) >= n
           for k, n in zip(keys, limits, strict=True)):
        raise error(429, "too_many_attempts",
                    "Too many incorrect codes. Wait 15 minutes, then request a new code.")
    user = accounts.user_by_email(conn, email)
    pending = user is not None and user["email_verified_at"] is None
    stored = (accounts.spend_email_code_attempt(conn, user["id"], s.verify_code_attempts)
              if pending else None)
    # Hashed even when there is nothing to compare with, so no failure skips the HMAC.
    given = security.code_hash(request.app.state.code_key, user["id"] if user else email,
                               body.code)
    if not (stored and hmac.compare_digest(stored, given)
            and accounts.consume_email_code(conn, user["id"], stored)):
        for k in keys:
            accounts.throttle_hit(conn, k)
        raise error(400, "invalid_code", "That code is incorrect or has expired. Check the "
                    "latest email we sent, or request a new code.")
    accounts.mark_verified(conn, user["id"])
    return issue_session(request, conn, user["id"], remember=body.remember)


class EmailIn(BaseModel):
    email: Email
    captcha_token: str | None = None


@router.post("/resend-verification")
def resend_verification(body: EmailIn, request: Request, conn: RwConn) -> dict[str, bool]:
    require_captcha(request, body.captcha_token)
    user = accounts.user_by_email(conn, body.email)
    if user and user["email_verified_at"] is None:
        send_code(request, conn, user)
    return {"ok": True}


# --- login and sessions -----------------------------------------------------------------------

class LoginIn(BaseModel):
    email: Email
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
        # The password is right, so this is the owner: send a code to finish the confirmation.
        send_code(request, conn, user)
        raise error(403, "email_unverified", "Confirm your email address before signing in: "
                    "enter the code we emailed you.")
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
    s = request.app.state.settings
    user = accounts.user_by_email(conn, body.email)
    if user and mail_allowed(request, conn, "reset", user["email"]):
        token = accounts.create_token(conn, user["id"], "reset_password", s.reset_token_seconds)
        send_mail(request, conn, "reset", user["email"], mail.reset_mail(
            token, s.reset_token_seconds // 60, accounts.email_locale(conn, user["id"]),
            s.web_url), s.reset_token_seconds)
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
    user = accounts.user_by_id(conn, user_id)
    send_mail(request, conn, "password_changed", user["email"], mail.password_changed_mail(
        accounts.email_locale(conn, user_id), request.app.state.settings.web_url),
        NOTICE_SECONDS)
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
