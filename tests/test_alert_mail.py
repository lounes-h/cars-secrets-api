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

    subject, body, page = alert_mail.news_mail([reminder, reminder], [withdrawn], "en",
                                               NEW_YORK, "https://web.test")

    assert subject == "Auction reminders: 2 saved lots"
    assert "2021 HONDA CIVIC EX\n  Lot 7 · Copart · DALLAS, TX" in body
    assert "Buy now: $9,500 → not offered" in body
    assert "https://web.test/active-lots/copart/8\n" in body
    assert '<a href="https://web.test/active-lots/copart/8"' in page
    assert ">2021 HONDA CIVIC EX</a>" in page and ">Your alerts</a>" in page



def test_a_future_sale_given_its_date_reads_as_set():
    unset = {"at": None, "date": None}
    timed = {"lot": LOT, "changes": {"sale": {"from": unset, "to": {"at": LOT["sale_at"],
                                                                    "date": None}}}}
    dated = {"lot": LOT, "changes": {"sale": {"from": unset, "to": {"at": None,
                                                                    "date": "2026-10-09"}}}}

    _, body, _ = alert_mail.news_mail([], [timed, dated], "en", NEW_YORK, "https://web.test")
    _, cuerpo, _ = alert_mail.news_mail([], [dated], "es", NEW_YORK, "https://web.test")

    assert "Auction date set: Thu, Oct 8, 11:00 AM EDT" in body
    assert "Auction date set: Oct 9, 2026" in body and "Future sale" not in body
    assert "Fecha de subasta fijada: 9 oct 2026" in cuerpo

def test_what_changed_heads_the_email_and_its_new_value_stands_out():
    unset = {"at": None, "date": None}
    cheaper = {"lot": LOT, "changes": {"buy_now": {"from": 9500.0, "to": 8900.0}}}
    both = {"lot": LOT | {"lot_number": "8"}, "changes": {
        "sale": {"from": unset, "to": {"at": None, "date": "2026-10-09"}},
        "buy_now": {"from": None, "to": 4000.0}}}

    subject, body, page = alert_mail.news_mail([], [cheaper], "en", NEW_YORK, "https://web.test")
    asunto, cuerpo, _ = alert_mail.news_mail([], [cheaper, both], "es", MEXICO,
                                             "https://web.test")

    assert subject == "Buy now price changed: 2021 HONDA CIVIC EX"
    assert "Buy now price changed\n  2021 HONDA CIVIC EX" in body, "and heads the lot's section"
    assert "<div>Buy now: $9,500 → <strong>$8,900</strong></div>" in page
    assert asunto == ("Fecha de subasta fijada y cambió el precio de compra inmediata en 2 lotes "
                      "guardados")
    assert "Fecha de subasta fijada y cambió el precio de compra inmediata\n" in cuerpo


def test_a_date_set_on_one_lot_and_moved_on_another_reads_as_changed():
    unset, dated = {"at": None, "date": None}, {"at": None, "date": "2026-10-09"}
    later = {"at": None, "date": "2026-10-10"}

    assert alert_mail.what([{"sale": {"from": unset, "to": dated}},
                            {"sale": {"from": dated, "to": later}}], "en") == "Auction date changed"
    assert alert_mail.what([{"sale": {"from": unset, "to": dated}}], "es") == (
        "Fecha de subasta fijada")


def test_a_summary_in_spanish():
    found = LOT | {"high_bid": 1500.0, "buy_it_now_price": 4000.0, "searches": ["Hondas"]}
    unbid = LOT | {"lot_number": "8", "searches": ["Hondas"]}  # no bid yet, no Buy now
    payload = {"total": 3, "searches": [
        {"id": "s1", "name": "Hondas", "query": "", "total": 3, "lots": [found, unbid]}]}

    subject, body, page = alert_mail.summary_mail(payload, "es", MEXICO, "https://web.test")

    assert subject == "3 lotes nuevos coinciden con tus búsquedas guardadas"
    assert "Hondas: 3 lotes nuevos" in body
    assert ("Subasta: jue 8 oct, 9:00 a. m. CST · Oferta actual: $1,500 · "
            "Compra inmediata: $4,000") in body
    assert "Subasta: jue 8 oct, 9:00 a. m. CST\n  https://web.test/es/active-lots/copart/8" in body
    assert "Ver los 3: https://web.test/es/active-lots\n" in body
    assert "Tus alertas: https://web.test/es/alerts" in body


def test_what_dealers_and_auctions_wrote_is_escaped_in_the_html():
    lot = LOT | {"make": "<b>HONDA</b>", "searches": ['<script>alert("x")</script>']}
    payload = {"total": 1, "searches": [{"id": "s1", "name": '<script>alert("x")</script>',
                                         "query": "make=A&b=<c>", "total": 2, "lots": [lot]}]}

    subject, body, page = alert_mail.summary_mail(payload, "en", UTC, "https://web.test")

    assert "<script>" not in page and "<b>HONDA" not in page
    assert "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;: 2 new lots" in page
    assert "2021 &lt;b&gt;HONDA&lt;/b&gt; CIVIC EX" in page
    assert 'href="https://web.test/active-lots?make=A&amp;b=%3Cc%3E"' in page
    assert '<script>alert("x")</script>: 2 new lots' in body, "plain text is shown as text"
