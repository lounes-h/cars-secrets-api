from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # The API's own database: accounts (`app`, migrated here) and the lots the ingest worker
    # keeps in step (`public`, owned by cars-secrets-data-ingest). No default: it holds a password.
    database_url: str
    # Public origin of the web app: links in emails and OAuth redirect URIs point there.
    web_url: str = "http://localhost:3000"
    # Shared with the web server. When a request carries it in X-Internal-Key, the API trusts
    # X-Client-IP for rate limiting. Empty: the header is ignored.
    internal_api_key: str = Field(default="", repr=False)

    session_remember_seconds: int = 30 * 24 * 3600
    session_idle_seconds: int = 12 * 3600
    reset_token_seconds: int = 3600
    oauth_state_seconds: int = 600

    # Email confirmation codes: six digits, valid for 10 minutes, 5 guesses each, one new code
    # a minute at most. Wrong codes are also counted per address and per client over the
    # throttle window; past either limit every guess is refused until the window passes.
    verify_code_seconds: int = 600
    verify_code_attempts: int = 5
    verify_code_resend_seconds: int = 60
    code_failures_per_email: int = 10
    code_failures_per_ip: int = 20
    # HMAC key for stored codes, so a leaked table cannot be brute-forced offline (a million
    # candidates). 32+ random chars. Empty: a random key per process (development only).
    email_code_secret: str = Field(default="", repr=False)

    # Login failures in the window before a captcha is required.
    login_failures_per_email: int = 3
    login_failures_per_ip: int = 10
    throttle_window_seconds: int = 900

    # Mails an address, or one client, can trigger in the window. Anyone can ask us to mail
    # any address, so the cap is what keeps an inbox from being used as a weapon.
    mails_per_email: int = 3
    mails_per_ip: int = 10

    # Cloudflare Turnstile; empty secret = captcha not enforced (development, tests).
    turnstile_secret: str = Field(default="", repr=False)
    check_breached_passwords: bool = True

    # How often the alert job runs in the API process (reminders, saved-lot changes, search
    # summaries; app/alerts.py). 0 turns it off, as the tests do to run it themselves.
    alerts_interval_seconds: float = 60

    # Outgoing mail; without smtp_host, messages are logged instead of sent.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = Field(default="", repr=False)
    mail_from: str = "Car's Secrets <no-reply@localhost>"

    google_client_id: str = ""
    google_client_secret: str = Field(default="", repr=False)
    apple_client_id: str = ""
    apple_team_id: str = ""
    apple_key_id: str = ""
    apple_private_key: str = Field(default="", repr=False)  # PEM; "\n" escapes accepted


@lru_cache
def get_settings() -> Settings:
    return Settings()
