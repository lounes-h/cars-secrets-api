"""Unhandled server errors are mailed to the admin, each distinct one once per interval."""

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


class BrokenMailer:
    def send(self, to, subject, body):
        raise OSError("smtp unreachable")


def failing_app(settings, mailer):
    app = create_app(settings, mailer=mailer)

    @app.get("/api/boom/{lot}")
    def boom(lot: str):
        raise RuntimeError(f"broke on {lot}")

    @app.get("/api/other")
    def other():
        raise KeyError("x")

    return app


@pytest.fixture
def alerting(conn, settings):
    return settings.model_copy(update={"alert_email_to": "admin@cs.test"})


def test_server_error_is_mailed_once_per_route_within_the_interval(alerting, mailer):
    with TestClient(failing_app(alerting, mailer), raise_server_exceptions=False) as tc:
        r = tc.get("/api/boom/1")
        assert r.status_code == 500
        assert r.json()["detail"]["code"] == "internal_error"
        tc.get("/api/boom/2")  # same route, other lot: not mailed again
        tc.get("/api/other")
    assert [(to, subject) for to, subject, _ in mailer.sent] == [
        ("admin@cs.test", "[Cars API] Server error: RuntimeError in GET /api/boom/{lot}"),
        ("admin@cs.test", "[Cars API] Server error: KeyError in GET /api/other"),
    ]
    body = mailer.sent[0][2]
    assert "Traceback" in body and "RuntimeError: broke on 1" in body


def test_server_error_is_mailed_again_after_the_interval(alerting, mailer):
    settings = alerting.model_copy(update={"alert_email_interval_seconds": 0})
    with TestClient(failing_app(settings, mailer), raise_server_exceptions=False) as tc:
        tc.get("/api/boom/1")
        tc.get("/api/boom/1")
    assert len(mailer.sent) == 2


def test_server_error_is_not_mailed_without_a_recipient(conn, settings, mailer):
    with TestClient(failing_app(settings, mailer), raise_server_exceptions=False) as tc:
        assert tc.get("/api/other").status_code == 500
    assert mailer.sent == []


def test_a_mail_failure_still_answers_500(alerting, caplog):
    with TestClient(failing_app(alerting, BrokenMailer()), raise_server_exceptions=False) as tc:
        assert tc.get("/api/other").status_code == 500
    assert "could not mail the error alert" in caplog.text
