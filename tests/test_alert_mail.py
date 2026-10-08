from zoneinfo import ZoneInfo

from app import alert_mail

NEW_YORK, MEXICO, UTC = (ZoneInfo(name) for name in ("America/New_York", "America/Mexico_City",
                                                     "UTC"))
LOT = {"source": "copart", "lot_number": "7", "year": 2021, "make": "HONDA", "model": "CIVIC",
       "trim": "EX", "sale_at": "2026-10-08T15:00:00Z", "sale_date": "2026-10-08",
       "location_city": "DALLAS", "location_state": "TX", "thumbnail_url": None, "high_bid": None,
       "buy_it_now_price": None, "currency": "USD", "odometer": None, "damage_primary": None}


def test_times_read_in_the_dealers_language_and_zone():
    assert alert_mail.when("2026-10-08T15:00:00Z", "en", NEW_YORK) == "Thu, Oct 8, 11:00 AM EDT"
    assert alert_mail.when("2026-12-01T17:30:00Z", "en", NEW_YORK) == "Tue, Dec 1, 12:30 PM EST"
    assert alert_mail.when("2026-10-09T00:05:00+00:00", "en", UTC) == "Fri, Oct 9, 12:05 AM UTC"
    assert alert_mail.when("2026-10-08T15:00:00Z", "es", MEXICO) == "jue 8 oct, 9:00 a. m. CST"
    assert alert_mail.when("2026-10-08T21:00:00Z", "es", MEXICO) == "jue 8 oct, 3:00 p. m. CST"


def test_a_sale_without_a_time_reads_as_its_day_and_without_a_day_as_a_future_sale():
    assert alert_mail.sale(None, "2026-10-09", "en", UTC) == "Oct 9, 2026"
    assert alert_mail.sale(None, "2026-09-09", "es", UTC) == "9 sept 2026"
    assert alert_mail.sale(None, None, "en", UTC) == "Future sale"
    assert alert_mail.sale(None, None, "es", UTC) == "Venta futura"


def test_amounts_are_dollars_unless_the_lot_says_otherwise():
    assert alert_mail.money(9500, "USD") == alert_mail.money(9500, None) == "$9,500"
    assert alert_mail.money(12000.4, "CAD") == "12,000 CAD"


def test_a_lot_without_a_name_goes_by_its_number():
    nameless = LOT | {"year": None, "make": None, "model": None, "trim": None}
    assert alert_mail.title(nameless, "en") == "Lot 7"
    assert alert_mail.title(nameless, "es") == "Lote 7"


def test_several_reminders_and_a_buy_now_taken_away():
    reminder = {"lot": LOT, "reminders": ["day"], "changes": None}
    withdrawn = {"lot": LOT | {"lot_number": "8"},
                 "changes": {"buy_now": {"from": 9500.0, "to": None}}}

    subject, body = alert_mail.news_mail([reminder, reminder], [withdrawn], "en", NEW_YORK,
                                         "https://web.test")

    assert subject == "Auction reminders: 2 saved lots"
    assert "2021 HONDA CIVIC EX\n  Lot 7 · Copart · DALLAS, TX" in body
    assert "Buy now: $9,500 → not offered" in body
    assert "https://web.test/active-lots/copart/8\n" in body


def test_a_summary_in_spanish():
    found = LOT | {"high_bid": 1500.0, "buy_it_now_price": 4000.0, "searches": ["Hondas"]}
    unbid = LOT | {"lot_number": "8", "searches": ["Hondas"]}  # no bid yet, no Buy now
    payload = {"total": 3, "searches": [
        {"id": "s1", "name": "Hondas", "query": "", "total": 3, "lots": [found, unbid]}]}

    subject, body = alert_mail.summary_mail(payload, "es", MEXICO, "https://web.test")

    assert subject == "3 lotes nuevos coinciden con tus búsquedas guardadas"
    assert "Hondas: 3 lotes nuevos" in body
    assert ("Subasta: jue 8 oct, 9:00 a. m. CST · Oferta actual: $1,500 · "
            "Compra inmediata: $4,000") in body
    assert "Subasta: jue 8 oct, 9:00 a. m. CST\n  https://web.test/es/active-lots/copart/8" in body
    assert "Ver los 3: https://web.test/es/active-lots\n" in body
    assert "Tus alertas: https://web.test/es/alerts" in body
