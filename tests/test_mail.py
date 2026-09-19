import logging
from email.message import EmailMessage

from app import mail
from app.config import Settings


def test_message_builders_link_to_the_web_app():
    subject, body = mail.verification_mail("https://cars.test", "tok-1")
    assert "https://cars.test/verify-email?token=tok-1" in body
    subject, body = mail.reset_mail("https://cars.test", "tok-2")
    assert "https://cars.test/reset-password?token=tok-2" in body
    subject, body = mail.existing_account_mail("https://cars.test")
    assert "https://cars.test/forgot-password" in body


def test_log_mailer_logs_the_message(caplog):
    with caplog.at_level(logging.INFO, logger="app.mail"):
        mail.LogMailer().send("a@b.test", "Hello", "Body line")
    assert "a@b.test" in caplog.text and "Body line" in caplog.text


def test_smtp_mailer_sends_a_plain_text_message(monkeypatch):
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
    mail.SmtpMailer(settings).send("a@b.test", "Hi", "Body")
    assert sent[0] == "tls" and sent[1] == ("u", "p")
    message = sent[2]
    assert message["To"] == "a@b.test" and message["From"] == "CS <no-reply@cs.test>"
    assert message["Subject"] == "Hi" and message.get_content().strip() == "Body"


def test_make_mailer_picks_smtp_only_when_configured():
    assert isinstance(mail.make_mailer(Settings(_env_file=None, database_url="x")), mail.LogMailer)
    assert isinstance(mail.make_mailer(Settings(_env_file=None, database_url="x", smtp_host="h")),
                      mail.SmtpMailer)
