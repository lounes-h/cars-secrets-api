"""Outgoing email: SMTP when configured, otherwise the log (development), and the account emails.

Every email is written once, as blocks that each carry their plain text and their HTML, and
goes out as both (multipart/alternative). The HTML is the web's grayscale look in one table
layout with inline styles, which is what mail clients render. Whatever came from a dealer or an
auction is escaped. The words follow the web's (src/messages), in the dealer's language."""

import html
import logging
import smtplib
from email.message import EmailMessage
from typing import Any, NamedTuple, Protocol

from .config import Settings

log = logging.getLogger(__name__)

BRAND = "CarBidInsight"


class Mail(NamedTuple):
    subject: str
    text: str
    html: str


class Mailer(Protocol):
    def send(self, to: str, subject: str, text: str, html: str) -> None: ...


class LogMailer:
    def send(self, to: str, subject: str, text: str, html: str) -> None:
        log.info("mail to=%s subject=%r\n%s", to, subject, text)


class SmtpMailer:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def send(self, to: str, subject: str, text: str, html: str) -> None:
        s = self.settings
        message = EmailMessage()
        message["From"], message["To"], message["Subject"] = s.mail_from, to, subject
        message.set_content(text)
        message.add_alternative(html, subtype="html")
        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=10) as smtp:
            smtp.starttls()
            if s.smtp_username:
                smtp.login(s.smtp_username, s.smtp_password)
            smtp.send_message(message)


def make_mailer(settings: Settings) -> Mailer:
    if settings.smtp_host:
        return SmtpMailer(settings)
    # The log then holds working confirmation codes and reset links: development only.
    log.warning("SMTP_HOST is not set: emails, including confirmation codes and password reset "
                "links, are written to the log. Never run like this in production.")
    return LogMailer()


# --- blocks: each one's plain text and HTML -----------------------------------------------------

class Block(NamedTuple):
    text: str
    html: str


FONT = "'Work Sans', Helvetica, Arial, sans-serif"
INK, MUTED, BORDER, PRIMARY, PAGE = "#0a0a0a", "#636363", "#e5e5e5", "#171717", "#f5f5f5"


def esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def paragraph(words: str, *, muted: bool = False) -> Block:
    style = (f"margin:0 0 16px;color:{MUTED};font-size:14px;line-height:20px" if muted
             else "margin:0 0 16px")
    return Block(words, f'<p style="{style}">{esc(words)}</p>')


def code(digits: str) -> Block:
    return Block(f"    {digits}", (
        f'<p style="margin:0 0 16px"><span style="display:inline-block;padding:12px 16px;'
        f'border:1px solid {BORDER};border-radius:8px;font-family:Menlo,Consolas,monospace;'
        f'font-size:30px;line-height:36px;font-weight:700;letter-spacing:6px">{esc(digits)}'
        '</span></p>'))


def button(label: str, url: str) -> Block:
    """The email's one action: a 48px black button, and in plain text the label and the link."""
    return Block(f"{label}: {url}", (
        '<table role="presentation" cellpadding="0" cellspacing="0" style="margin:0 0 16px">'
        f'<tr><td style="background:{PRIMARY};border-radius:8px"><a href="{esc(url)}" '
        'style="display:inline-block;padding:12px 24px;color:#ffffff;font-weight:600;'
        f'line-height:24px;text-decoration:none">{esc(label)}</a></td></tr></table>'))


def link(label: str, url: str) -> Block:
    return Block(f"{label}: {url}",
                 f'<p style="margin:0 0 16px"><a href="{esc(url)}" style="color:{INK}">'
                 f'{esc(label)}</a></p>')


def indent(text: str) -> str:
    return "\n".join("  " + line for line in text.split("\n"))


def section(heading: str, blocks: list[Block]) -> Block:
    """A heading over its blocks, which the plain text indents."""
    return Block("\n".join([heading, *(indent(block.text) for block in blocks)]), (
        '<div style="margin:0 0 24px"><h2 style="margin:0 0 12px;font-size:18px;'
        f'line-height:28px;font-weight:600">{esc(heading)}</h2>'
        + "".join(block.html for block in blocks) + "</div>"))


def lot(name: str, url: str, lines: list[str]) -> Block:
    """A lot: its name linking to its page, then a line per fact."""
    facts = "".join(f'<div style="color:{MUTED};font-size:14px;line-height:20px">{esc(line)}'
                    "</div>" for line in lines)
    return Block("\n".join([name, *lines, url]), (
        f'<div style="margin:0 0 12px;padding:16px;border:1px solid {BORDER};border-radius:12px">'
        f'<a href="{esc(url)}" style="color:{INK};font-weight:600;text-decoration:none">'
        f'{esc(name)}</a>{facts}</div>'))


def footer(links: list[tuple[str, str]]) -> Block:
    return Block("\n".join(f"{label}: {url}" for label, url in links), (
        f'<p style="margin:24px 0 0;padding-top:16px;border-top:1px solid {BORDER};color:{MUTED};'
        'font-size:14px;line-height:20px">' + " · ".join(
            f'<a href="{esc(url)}" style="color:{MUTED}">{esc(label)}</a>' for label, url in links)
        + "</p>"))


def compose(subject: str, blocks: list[Block], locale: str, web_url: str) -> Mail:
    """The email: its blocks one after another in plain text, and in the HTML layout under the
    logo (served by the web app, so a client that blocks images shows its alt text)."""
    body = "".join(block.html for block in blocks)
    document = (
        f'<!doctype html><html lang="{esc(locale)}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<meta name="color-scheme" content="light"><title>{esc(subject)}</title></head>'
        f'<body style="margin:0;padding:0;background:{PAGE}">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="background:{PAGE}"><tr><td align="center" style="padding:24px 16px">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="max-width:600px;background:#ffffff;border:1px solid {BORDER};border-radius:12px">'
        f'<tr><td style="padding:24px 24px 0"><a href="{esc(web_url)}/">'
        f'<img src="{esc(web_url)}/logo.png" width="192" height="32" alt="{BRAND}" '
        'style="display:block;border:0"></a></td></tr>'
        f'<tr><td style="padding:24px;font-family:{FONT};font-size:16px;line-height:24px;'
        f'color:{INK}">{body}</td></tr></table></td></tr></table></body></html>')
    return Mail(subject, "\n\n".join(block.text for block in blocks) + "\n", document)


def page(web_url: str, locale: str, path: str) -> str:
    """A page of the web app, in the dealer's language (Spanish pages are under /es)."""
    return f"{web_url}{'' if locale == 'en' else '/' + locale}{path}"


# --- the account emails -------------------------------------------------------------------------

WORDS: dict[str, dict[str, str]] = {
    "en": {
        "verify_subject": "Your confirmation code",
        "verify_intro": f"Welcome to {BRAND}. Enter this code to confirm your email address:",
        "verify_rules": "It expires in {minutes} minutes and works once. Never share it: nobody "
                        f"from {BRAND} will ever ask you for it.",
        "verify_ignore": "If you did not create an account, ignore this message: nothing happens "
                         "without the code.",
        "reset_subject": "Reset your password",
        "reset_intro": f"Someone asked to reset the password of your {BRAND} account. Open this "
                       "link within {minutes} minutes to choose a new password:",
        "reset_button": "Choose a new password",
        "reset_ignore": "If that was not you, ignore this message; your password has not "
                        "changed.",
        "existing_subject": "You already have an account",
        "existing_intro": f"Someone tried to register on {BRAND} with this email address, but "
                          "it already has an account.",
        "sign_in": "Sign in",
        "forgot": "Forgot your password? Reset it",
        "existing_ignore": "If that was not you, ignore this message.",
        "changed_subject": "Your password was changed",
        "changed_intro": f"The password of your {BRAND} account was just changed, and every "
                         "device signed in to it was signed out.",
        "changed_you": "If it was you, there is nothing to do.",
        "changed_not_you": "If it was not you, reset your password now to take the account "
                           "back:",
        "changed_button": "Reset your password",
    },
    "es": {
        "verify_subject": "Tu código de confirmación",
        "verify_intro": f"Te damos la bienvenida a {BRAND}. Ingresa este código para confirmar "
                        "tu correo electrónico:",
        "verify_rules": "Vence en {minutes} minutos y funciona una sola vez. No lo compartas: "
                        f"nadie de {BRAND} te lo pedirá nunca.",
        "verify_ignore": "Si no creaste una cuenta, ignora este mensaje: no pasa nada sin el "
                         "código.",
        "reset_subject": "Restablece tu contraseña",
        "reset_intro": f"Alguien pidió restablecer la contraseña de tu cuenta de {BRAND}. Abre "
                       "este enlace en los próximos {minutes} minutos para elegir una nueva "
                       "contraseña:",
        "reset_button": "Elegir una nueva contraseña",
        "reset_ignore": "Si no fuiste tú, ignora este mensaje; tu contraseña no cambió.",
        "existing_subject": "Ya tienes una cuenta",
        "existing_intro": f"Alguien intentó registrarse en {BRAND} con este correo electrónico, "
                          "pero ya tiene una cuenta.",
        "sign_in": "Iniciar sesión",
        "forgot": "¿Olvidaste tu contraseña? Restablécela",
        "existing_ignore": "Si no fuiste tú, ignora este mensaje.",
        "changed_subject": "Tu contraseña cambió",
        "changed_intro": f"La contraseña de tu cuenta de {BRAND} acaba de cambiar y se cerró la "
                         "sesión en todos los dispositivos.",
        "changed_you": "Si fuiste tú, no tienes que hacer nada.",
        "changed_not_you": "Si no fuiste tú, restablece tu contraseña ahora para recuperar la "
                           "cuenta:",
        "changed_button": "Restablecer la contraseña",
    },
}


def verification_mail(digits: str, minutes: int, locale: str, web_url: str) -> Mail:
    # The code stays out of the subject, so it does not show on a locked phone's notifications.
    w = WORDS[locale]
    return compose(w["verify_subject"], [
        paragraph(w["verify_intro"]), code(digits),
        paragraph(w["verify_rules"].format(minutes=minutes)),
        paragraph(w["verify_ignore"], muted=True)], locale, web_url)


def reset_mail(token: str, minutes: int, locale: str, web_url: str) -> Mail:
    w = WORDS[locale]
    return compose(w["reset_subject"], [
        paragraph(w["reset_intro"].format(minutes=minutes)),
        button(w["reset_button"], page(web_url, locale, f"/reset-password?token={token}")),
        paragraph(w["reset_ignore"], muted=True)], locale, web_url)


def existing_account_mail(locale: str, web_url: str) -> Mail:
    w = WORDS[locale]
    return compose(w["existing_subject"], [
        paragraph(w["existing_intro"]), button(w["sign_in"], page(web_url, locale, "/login")),
        link(w["forgot"], page(web_url, locale, "/forgot-password")),
        paragraph(w["existing_ignore"], muted=True)], locale, web_url)


def password_changed_mail(locale: str, web_url: str) -> Mail:
    """Sent when a reset sets a new password, so a takeover does not go unnoticed."""
    w = WORDS[locale]
    return compose(w["changed_subject"], [
        paragraph(w["changed_intro"]), paragraph(w["changed_you"]),
        paragraph(w["changed_not_you"]),
        button(w["changed_button"], page(web_url, locale, "/forgot-password"))], locale, web_url)
