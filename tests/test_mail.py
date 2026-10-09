import logging
from email.message import EmailMessage

import pytest

from app import mail
from app.config import Settings

WEB = "https://cars.test"


def test_links_open_the_web_app_in_the_dealers_language():
    assert "https://cars.test/reset-password?token=tok-2" in mail.reset_mail(
        "tok-2", 60, "en", WEB).text
    assert "https://cars.test/es/reset-password?token=tok-2" in mail.reset_mail(
        "tok-2", 60, "es", WEB).text
    hint = mail.existing_account_mail("es", WEB)
    assert "https://cars.test/es/login" in hint.text
    assert "https://cars.test/es/forgot-password" in hint.text
    assert hint.subject == "Ya tienes una cuenta"


def test_verification_mail_carries_the_code_but_not_in_its_subject():
    english = mail.verification_mail("042917", 10, "en", WEB)
    assert "042917" in english.text and "042917" not in english.subject
    assert "10 minutes" in english.text and "http" not in english.text
    spanish = mail.verification_mail("042917", 10, "es", WEB)
    assert spanish.subject == "Tu código de confirmación"
    assert "042917" in spanish.text and "10 minutos" in spanish.text
    assert ">042917</span>" in spanish.html


def test_the_password_changed_notice_offers_a_reset():
    notice = mail.password_changed_mail("en", WEB)
    assert notice.subject == "Your password was changed"
    assert "Reset your password: https://cars.test/forgot-password" in notice.text
    assert 'href="https://cars.test/forgot-password"' in notice.html
    assert "Restablecer la contraseña" in mail.password_changed_mail("es", WEB).text


@pytest.mark.parametrize("message", [
    mail.verification_mail("042917", 10, "es", WEB), mail.reset_mail("t", 60, "en", WEB),
    mail.existing_account_mail("en", WEB), mail.password_changed_mail("es", WEB)])
def test_every_email_has_the_brand_and_an_html_version(message):
    assert "CarBidInsight" in message.text and "Car's Secrets" not in message.text
    assert message.html.startswith("<!doctype html>")
    assert '<img src="https://cars.test/logo.png" width="192" height="32" alt="CarBidInsight"' \
        in message.html
    assert f"<title>{mail.esc(message.subject)}</title>" in message.html


def test_html_blocks_escape_what_they_are_given():
    assert mail.paragraph("<b>&").html == '<p style="margin:0 0 16px">&lt;b&gt;&amp;</p>'
    block = mail.button('Go "now"', 'https://x.test/?a=1&b="2"')
    assert 'href="https://x.test/?a=1&amp;b=&quot;2&quot;"' in block.html
    assert ">Go &quot;now&quot;</a>" in block.html
    assert block.text == 'Go "now": https://x.test/?a=1&b="2"'
    page = mail.compose("<Hi>", [], "es", WEB).html
    assert '<html lang="es">' in page and "<title>&lt;Hi&gt;</title>" in page


def test_log_mailer_logs_the_plain_text(caplog):
    with caplog.at_level(logging.INFO, logger="app.mail"):
        mail.LogMailer().send("a@b.test", "Hello", "Body line", "<p>Body line</p>")
    assert "a@b.test" in caplog.text and "Body line" in caplog.text
    assert "<p>" not in caplog.text


def test_smtp_mailer_sends_plain_text_and_html(monkeypatch):
    sent: list = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            assert (host, port) == ("smtp.test", 2525)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self):
            sent.append("tls")

        def login(self, username, password):
            sent.append((username, password))

        def send_message(self, message: EmailMessage):
            sent.append(message)

    monkeypatch.setattr(mail.smtplib, "SMTP", FakeSMTP)
    settings = Settings(_env_file=None, database_url="x", smtp_host="smtp.test", smtp_port=2525,
                        smtp_username="u", smtp_password="p", mail_from="CS <no-reply@cs.test>")
    mail.SmtpMailer(settings).send("a@b.test", "Hi", "Body", "<p>Body</p>")
    assert sent[0] == "tls" and sent[1] == ("u", "p")
    message = sent[2]
    assert message["To"] == "a@b.test" and message["From"] == "CS <no-reply@cs.test>"
    assert message["Subject"] == "Hi" and message.get_content_type() == "multipart/alternative"
    assert message.get_body(("plain",)).get_content().strip() == "Body"
    assert message.get_body(("html",)).get_content().strip() == "<p>Body</p>"


def test_the_sender_defaults_to_the_brand():
    assert Settings(_env_file=None, database_url="x").mail_from.startswith("CarBidInsight <")


def test_log_mailer_warns_that_links_end_up_in_the_log(caplog):
    with caplog.at_level(logging.WARNING, logger="app.mail"):
        mail.make_mailer(Settings(_env_file=None, database_url="x"))
    assert "SMTP_HOST is not set" in caplog.text


def test_make_mailer_picks_smtp_only_when_configured():
    assert isinstance(mail.make_mailer(Settings(_env_file=None, database_url="x")), mail.LogMailer)
    assert isinstance(mail.make_mailer(Settings(_env_file=None, database_url="x", smtp_host="h")),
                      mail.SmtpMailer)
