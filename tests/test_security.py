import hashlib
import logging

import httpx

from app import security


def test_password_hash_round_trip():
    h = security.hash_password("correct horse battery")
    assert h.startswith("$argon2id$")
    assert security.verify_password(h, "correct horse battery")
    assert not security.verify_password(h, "wrong")
    assert not security.verify_password(None, "anything")   # social-only account
    assert not security.verify_password("garbage", "anything")


def test_tokens_are_random_and_hashed():
    a, b = security.new_token(), security.new_token()
    assert a != b and len(a) >= 40
    assert security.token_hash(a) == hashlib.sha256(a.encode()).digest()


def hibp(lines: str):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.pwnedpasswords.com"
        assert len(request.url.path.rsplit("/", 1)[1]) == 5   # only the prefix leaves
        return httpx.Response(200, text=lines)
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_breach_check_matches_on_suffix():
    sha1 = hashlib.sha1(b"password123").hexdigest().upper()
    assert security.password_is_breached("password123", hibp(f"{sha1[5:]}:42\r\nABCDEF:1"))
    assert not security.password_is_breached("password123", hibp("ABCDEF:1"))


def test_breach_check_fails_open():
    def down(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")
    client = httpx.Client(transport=httpx.MockTransport(down))
    assert not security.password_is_breached("password123", client)


def turnstile(success: bool):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json={"success": success})
    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def test_captcha_is_skipped_without_secret():
    client, seen = turnstile(False)
    assert security.captcha_ok(None, "", "1.2.3.4", client)
    assert seen == {}


def test_captcha_verifies_with_cloudflare():
    client, seen = turnstile(True)
    assert security.captcha_ok("tok", "secret", "1.2.3.4", client)
    assert seen == {"secret": "secret", "response": "tok", "remoteip": "1.2.3.4"}
    client, _ = turnstile(False)
    assert not security.captcha_ok("tok", "secret", "1.2.3.4", client)
    assert not security.captcha_ok(None, "secret", "1.2.3.4", client)


def test_codes_are_six_digits_and_spread():
    codes = [security.new_code() for _ in range(2000)]
    assert all(len(c) == 6 and c.isdigit() for c in codes)
    assert len(set(codes)) > 1990 and any(c.startswith("0") for c in codes)


def test_code_hash_is_keyed_and_bound_to_the_user():
    h = security.code_hash(b"key", "user-1", "123456")
    assert h == security.code_hash(b"key", "user-1", "123456")
    assert h != security.code_hash(b"key", "user-2", "123456")
    assert h != security.code_hash(b"other", "user-1", "123456")
    assert h != security.code_hash(b"key", "user-1", "123457")


def test_the_code_key_comes_from_the_secret_or_a_warned_random(caplog, settings):
    from app.main import create_app

    settings.email_code_secret = "s" * 32
    assert create_app(settings).state.code_key == b"s" * 32
    settings.email_code_secret = ""
    with caplog.at_level(logging.WARNING, logger="app.main"):
        first, second = create_app(settings).state.code_key, create_app(settings).state.code_key
    assert len(first) == 32 and first != second
    assert "EMAIL_CODE_SECRET is not set" in caplog.text
