from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # The ingest database (owned by cars-secrets-data-ingest). No default: it holds a password.
    database_url: str
    # Public origin of the web app: links in emails and OAuth redirect URIs point there.
    web_url: str = "http://localhost:3000"
    # Shared with the web server. When a request carries it in X-Internal-Key, the API trusts
    # X-Client-IP for rate limiting. Empty: the header is ignored.
    internal_api_key: str = Field(default="", repr=False)

    session_remember_seconds: int = 30 * 24 * 3600
    session_idle_seconds: int = 12 * 3600
    verify_token_seconds: int = 24 * 3600
    reset_token_seconds: int = 3600
    oauth_state_seconds: int = 600

    # Login failures in the window before a captcha is required.
    login_failures_per_email: int = 3
    login_failures_per_ip: int = 10
    throttle_window_seconds: int = 900

    # Cloudflare Turnstile; empty secret = captcha not enforced (development, tests).
    turnstile_secret: str = Field(default="", repr=False)
    check_breached_passwords: bool = True

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
