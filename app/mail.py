"""Outgoing email: SMTP when configured, otherwise the log (development)."""

import logging
import smtplib
from email.message import EmailMessage
from typing import Protocol

from .config import Settings

log = logging.getLogger(__name__)


class Mailer(Protocol):
    def send(self, to: str, subject: str, body: str) -> None: ...


class LogMailer:
    def send(self, to: str, subject: str, body: str) -> None:
        log.info("mail to=%s subject=%r\n%s", to, subject, body)


class SmtpMailer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def send(self, to: str, subject: str, body: str) -> None:
        s = self.settings
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = s.mail_from, to, subject
        message.set_content(body)
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=10) as smtp:
            smtp.starttls()
            if s.smtp_username:
                smtp.login(s.smtp_username, s.smtp_password)
            smtp.send_message(message)


def make_mailer(settings: Settings) -> Mailer:
    return SmtpMailer(settings) if settings.smtp_host else LogMailer()


def verification_mail(web_url: str, token: str) -> tuple[str, str]:
    return "Confirm your email address", (
        "Welcome to Car's Secrets.\n\nConfirm your email address by opening this link "
        f"(valid for 24 hours):\n\n{web_url}/verify-email?token={token}\n\n"
        "If you did not create an account, ignore this message."
    )


def reset_mail(web_url: str, token: str) -> tuple[str, str]:
    return "Reset your password", (
        "Someone asked to reset the password of your Car's Secrets account. Open this link "
        f"(valid for 1 hour) to choose a new password:\n\n{web_url}/reset-password?token={token}"
        "\n\nIf that was not you, ignore this message; your password has not changed."
    )


def existing_account_mail(web_url: str) -> tuple[str, str]:
    return "You already have an account", (
        "Someone tried to register on Car's Secrets with this email address, but an account "
        f"already exists. You can sign in at {web_url}/login or reset your password at "
        f"{web_url}/forgot-password.\n\nIf that was not you, ignore this message."
    )
