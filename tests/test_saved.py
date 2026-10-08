from datetime import UTC, datetime

import pytest

from app import saved
from tests.conftest import insert_lot
from tests.test_auth import register, verify


def dealer(client, mailer, email="dana@example.com") -> dict[str, str]:
    assert register(client, email=email).status_code == 200
    token = verify(client, mailer.last_code(), email=email).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def save(client, headers, lot_number="1", **body):
    return client.put(f"/api/me/saved-lots/copart/{lot_number}", json=body, headers=headers)


def search(client, headers, **body):
    return client.post("/api/me/saved-searches",
                       json={"name": "Camry", "query": "make=TOYOTA", **body}, headers=headers)


def test_saved_routes_need_a_signed_in_dealer(client):
    for method, path in [("get", "/api/me/saved-lots"), ("get", "/api/me/saved-lots/keys"),
                         ("get", "/api/me/saved-lots/copart/1"),
                         ("put", "/api/me/saved-lots/copart/1"),
                         ("delete", "/api/me/saved-lots/copart/1"),
                         ("get", "/api/me/saved-searches"), ("post", "/api/me/saved-searches")]:
        assert client.request(method, path, json={}).status_code == 401, path


def test_saving_a_lot_switches_nothing_on(client, mailer, conn):
    headers = dealer(client, mailer)
    insert_lot(conn, "1", sale_at="2026-10-09T14:00Z", buy_it_now_price=9500)

    response = save(client, headers)

    assert response.status_code == 200, response.text
    item = response.json()
    assert item["lot"]["lot_number"] == "1" and item["lot"]["model"] == "CAMRY"
    assert item["available"] is True
    assert (item["change_alerts"], item["remind_hour"], item["remind_day"]) == (False,) * 3
    assert client.get("/api/me/saved-lots", headers=headers).json()["items"] == [item]
    assert client.get("/api/me/saved-lots/copart/1", headers=headers).json() == item
    assert client.get("/api/me/saved-lots/keys", headers=headers).json() == {
        "keys": [{"source": "copart", "lot_number": "1"}]}


def test_saving_again_keeps_one_and_unsaved_lots_are_not_found(client, mailer, conn):
    headers = dealer(client, mailer)
    insert_lot(conn, "1")
    save(client, headers)

    assert save(client, headers).status_code == 200
    assert len(client.get("/api/me/saved-lots", headers=headers).json()["items"]) == 1
    response = client.get("/api/me/saved-lots/copart/2", headers=headers)
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "not_saved"


def test_only_a_listed_lot_can_be_saved(client, mailer, conn):
    headers = dealer(client, mailer)
    insert_lot(conn, "2", removed_at="2026-10-01T00:00Z")

    for lot_number in ("1", "2"):
        response = save(client, headers, lot_number)
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "lot_not_found"


def test_a_lot_the_auction_dropped_stays_saved_as_it_was(client, mailer, conn):
    headers = dealer(client, mailer)
    insert_lot(conn, "1", make="HONDA", high_bid=1500)
    save(client, headers, remind_hour=True)
    conn.execute("UPDATE lots SET removed_at = now(), high_bid = 2100")

    removed = client.get("/api/me/saved-lots", headers=headers).json()["items"][0]
    assert removed["available"] is False and removed["lot"]["high_bid"] == 2100
    # Purged from the API database once the auction is over: the saved copy stands in.
    conn.execute("DELETE FROM lots")
    gone = client.get("/api/me/saved-lots/copart/1", headers=headers).json()
    assert gone["available"] is False
    assert (gone["lot"]["make"], gone["lot"]["high_bid"]) == ("HONDA", 1500)
    # Still the dealer's to update or remove, though it can no longer be saved anew.
    assert save(client, headers, remind_hour=False).json()["remind_hour"] is False


def test_switching_on_change_alerts_starts_from_what_the_lot_says_now(client, mailer, conn):
    headers = dealer(client, mailer)
    insert_lot(conn, "1", sale_at="2026-10-09T14:00Z", sale_date=None, buy_it_now_price=9500)
    save(client, headers)
    conn.execute("UPDATE lots SET sale_at = '2026-10-10T15:00Z', buy_it_now_price = 8900")

    assert save(client, headers, change_alerts=True).json()["change_alerts"] is True

    told = conn.execute("SELECT told_sale_at, told_buy_now FROM app.saved_lots").fetchone()
    assert told == {"told_sale_at": datetime(2026, 10, 10, 15, tzinfo=UTC), "told_buy_now": 8900}


def test_a_reminder_records_when_it_was_switched_on(client, mailer, conn):
    headers = dealer(client, mailer)
    insert_lot(conn, "1")

    def set_at():
        return conn.execute("SELECT reminders_set_at AS at FROM app.saved_lots").fetchone()["at"]

    save(client, headers)
    assert set_at() is None
    item = save(client, headers, remind_hour=True).json()
    assert (item["remind_hour"], item["remind_day"]) == (True, False)
    first = set_at()
    assert first is not None
    save(client, headers, remind_hour=True)  # unchanged: still the first time
    assert set_at() == first
    save(client, headers, remind_day=True)
    assert set_at() > first


def test_removing_a_saved_lot(client, mailer, conn):
    headers = dealer(client, mailer)
    insert_lot(conn, "1")
    save(client, headers, change_alerts=True, remind_hour=True)

    assert client.delete("/api/me/saved-lots/copart/1", headers=headers).json() == {"ok": True}
    assert client.get("/api/me/saved-lots", headers=headers).json() == {"items": []}
    assert client.delete("/api/me/saved-lots/copart/1", headers=headers).status_code == 200


def test_saved_lots_and_searches_are_capped(client, mailer, conn, monkeypatch):
    headers = dealer(client, mailer)
    monkeypatch.setattr(saved, "MAX_SAVED_LOTS", 1)
    monkeypatch.setattr(saved, "MAX_SAVED_SEARCHES", 1)
    insert_lot(conn, "1")
    insert_lot(conn, "2")

    assert save(client, headers, "1").status_code == 200
    assert save(client, headers, "1").status_code == 200  # already saved: not one more
    full = save(client, headers, "2")
    assert full.status_code == 409 and full.json()["detail"]["code"] == "limit_reached"
    assert search(client, headers).status_code == 200
    full = search(client, headers)
    assert full.status_code == 409 and full.json()["detail"]["code"] == "limit_reached"


def test_the_first_alert_write_takes_the_dealers_language_and_zone(client, mailer, conn):
    headers = dealer(client, mailer)
    insert_lot(conn, "1")

    save(client, headers, locale="es", time_zone="America/Chicago")
    search(client, headers, locale="en", time_zone="Mars/Olympus")  # not the first: ignored

    assert conn.execute("SELECT locale, time_zone FROM app.alert_settings").fetchone() == {
        "locale": "es", "time_zone": "America/Chicago"}


def test_an_unknown_zone_is_utc(client, mailer, conn):
    headers = dealer(client, mailer)

    search(client, headers, time_zone="Mars/Olympus")

    assert conn.execute("SELECT time_zone FROM app.alert_settings").fetchone()["time_zone"] == "UTC"


def test_saving_a_search(client, mailer):
    headers = dealer(client, mailer)

    response = search(client, headers, name="  Camry 2021+ ", query="make=TOYOTA&year_min=2021")

    assert response.status_code == 200, response.text
    found = response.json()
    assert (found["name"], found["query"], found["alerts"]) == (
        "Camry 2021+", "make=TOYOTA&year_min=2021", False)
    assert client.get("/api/me/saved-searches", headers=headers).json() == {"items": [found]}


@pytest.mark.parametrize("body", [
    {"name": ""}, {"name": "   "}, {"name": "x" * 81},
    {"query": "year_min=soon"}, {"query": "sale=someday"}, {"query": "q=" + "x" * 2000},
])
def test_search_names_and_filters_are_checked(client, mailer, body):
    headers = dealer(client, mailer)

    assert search(client, headers, **body).status_code == 422


def test_a_search_keeps_parameters_the_list_ignores(client, mailer):
    # The order and the page are the list's business, not the search's: allowed, not matched.
    headers = dealer(client, mailer)

    assert search(client, headers, query="title=salvage&sort=sale&page=2").status_code == 200


def test_alerts_start_from_now_whenever_they_start(client, mailer, conn):
    headers = dealer(client, mailer)
    found = search(client, headers, alerts=True).json()
    path = f"/api/me/saved-searches/{found['id']}"

    def through():
        return conn.execute("SELECT matched_through AS t FROM app.saved_searches").fetchone()["t"]

    def age():
        conn.execute("UPDATE app.saved_searches SET matched_through = '2026-01-01T00:00Z'")
        return through()

    old = age()
    assert client.patch(path, json={"name": "Renamed"}, headers=headers).status_code == 200
    assert through() == old, "a new name starts nothing"
    assert client.patch(path, json={"alerts": False}, headers=headers).json()["alerts"] is False
    assert through() == old
    assert client.patch(path, json={"alerts": True}, headers=headers).status_code == 200
    assert through() > old, "resumed: lots that came while paused are not news"
    old = age()
    assert client.patch(path, json={"query": "make=TOYOTA"}, headers=headers).status_code == 200
    assert through() == old, "the same filters"
    assert client.patch(path, json={"query": "make=HONDA"}, headers=headers).status_code == 200
    assert through() > old, "new filters: what they match today is not news"


def test_renaming_editing_and_deleting_a_search(client, mailer, conn):
    headers = dealer(client, mailer)
    insert_lot(conn, "1")
    save(client, headers)
    found = search(client, headers).json()
    path = f"/api/me/saved-searches/{found['id']}"

    edited = client.patch(path, json={"name": "Hondas", "query": "make=HONDA", "alerts": True},
                          headers=headers).json()

    assert (edited["name"], edited["query"], edited["alerts"]) == ("Hondas", "make=HONDA", True)
    assert client.patch(path, json={"query": "sale=never"}, headers=headers).status_code == 422
    assert client.delete(path, headers=headers).json() == {"ok": True}
    assert client.get("/api/me/saved-searches", headers=headers).json() == {"items": []}
    assert client.patch(path, json={"name": "x"}, headers=headers).status_code == 404
    # Deleting a search keeps the lots saved on their own.
    assert len(client.get("/api/me/saved-lots", headers=headers).json()["items"]) == 1


def test_each_dealer_sees_only_their_own(client, mailer, conn):
    dana = dealer(client, mailer)
    insert_lot(conn, "1")
    save(client, dana)
    found = search(client, dana).json()
    eve = dealer(client, mailer, "eve@example.com")

    assert client.get("/api/me/saved-lots", headers=eve).json() == {"items": []}
    assert client.get("/api/me/saved-searches", headers=eve).json() == {"items": []}
    path = f"/api/me/saved-searches/{found['id']}"
    assert client.patch(path, json={"name": "Mine"}, headers=eve).status_code == 404
    client.delete(path, headers=eve)
    client.delete("/api/me/saved-lots/copart/1", headers=eve)
    assert client.get("/api/me/saved-searches", headers=dana).json()["items"][0]["name"] == "Camry"
    assert len(client.get("/api/me/saved-lots", headers=dana).json()["items"]) == 1
