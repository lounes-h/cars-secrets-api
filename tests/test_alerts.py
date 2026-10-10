import logging
import time
from datetime import UTC, datetime

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from app import alerts
from app.main import create_app
from tests.conftest import insert_lot
from tests.test_saved import dealer, save, search

WEB = "https://web.test"


def run(client, now: str) -> None:
    """One pass of the alert job, as if the clock read `now`."""
    alerts.tick(client.app.state, now=datetime.fromisoformat(now))


def inbox(conn, kind: str | None = None) -> list[dict]:
    rows = conn.execute("SELECT kind, payload FROM app.alerts ORDER BY id").fetchall()
    return [row for row in rows if kind in (None, row["kind"])]


def instant(value: str) -> datetime:
    return datetime.fromisoformat(value)


def settings(client, headers, **changes) -> None:
    body = {"email_matches": True, "email_changes": True, "email_reminders": True,
            "twice_daily": False, "time_zone": "UTC", "locale": "en"} | changes
    response = client.put("/api/me/alert-settings", json=body, headers=headers)
    assert response.status_code == 200, response.text


# --- settings and inbox -----------------------------------------------------------------------

def test_alert_routes_need_a_signed_in_dealer(client):
    for method, path in [("get", "/api/me/alert-settings"), ("put", "/api/me/alert-settings"),
                         ("get", "/api/me/alerts"), ("get", "/api/me/alerts/unread"),
                         ("post", "/api/me/alerts/read")]:
        assert client.request(method, path, json={}).status_code == 401, path


def test_settings_start_from_registration_and_are_kept(client, mailer, conn):
    headers = dealer(client, mailer)

    assert client.get("/api/me/alert-settings", headers=headers).json() == {
        "email_matches": True, "email_changes": True, "email_reminders": True,
        "twice_daily": False, "time_zone": "UTC", "locale": "en"}
    conn.execute("DELETE FROM app.alert_settings")  # a Google or Apple sign-up has none
    assert client.get("/api/me/alert-settings", headers=headers).json() == {
        "email_matches": True, "email_changes": True, "email_reminders": True,
        "twice_daily": False, "time_zone": None, "locale": None}
    settings(client, headers, email_changes=False, twice_daily=True,
             time_zone="America/Chicago", locale="es")
    assert client.get("/api/me/alert-settings", headers=headers).json() == {
        "email_matches": True, "email_changes": False, "email_reminders": True,
        "twice_daily": True, "time_zone": "America/Chicago", "locale": "es"}
    bad = client.put("/api/me/alert-settings", headers=headers, json={
        "email_matches": True, "email_changes": True, "email_reminders": True,
        "twice_daily": False, "time_zone": "Mars/Olympus", "locale": "en"})
    assert bad.status_code == 422


def test_the_inbox_lists_newest_first_by_kind_and_marks_read(client, mailer, conn):
    dana = dealer(client, mailer)
    eve = dealer(client, mailer, "eve@example.com")
    users = {row["email"]: row["id"] for row in conn.execute("SELECT id, email FROM app.users")}
    for email, kind in [("dana@example.com", "matches"), ("dana@example.com", "reminder"),
                        ("dana@example.com", "changes"), ("eve@example.com", "changes")]:
        conn.execute("INSERT INTO app.alerts (user_id, kind, payload) VALUES (%s, %s, %s)",
                     (users[email], kind, Jsonb({"kind": kind})))
    eve_alert = conn.execute("SELECT id FROM app.alerts WHERE user_id = %s",
                             (users["eve@example.com"],)).fetchone()["id"]

    page = client.get("/api/me/alerts", headers=dana).json()

    assert [alert["kind"] for alert in page["items"]] == ["changes", "reminder", "matches"]
    assert (page["total"], page["unread"]) == (3, 3)
    assert page["items"][0]["payload"] == {"kind": "changes"} and not page["items"][0]["read"]
    only = client.get("/api/me/alerts?kind=reminder", headers=dana).json()
    assert [a["kind"] for a in only["items"]] == ["reminder"] and only["total"] == 1
    assert [a["kind"] for a in client.get("/api/me/alerts?limit=1&offset=1",
                                          headers=dana).json()["items"]] == ["reminder"]
    newest = page["items"][0]["id"]
    read = client.post("/api/me/alerts/read", json={"ids": [newest, eve_alert]}, headers=dana)
    assert read.json() == {"unread": 2}
    assert client.get("/api/me/alerts", headers=dana).json()["items"][0]["read"] is True
    assert client.get("/api/me/alerts/unread", headers=eve).json() == {"unread": 1}, (
        "someone else's alert stays unread")
    assert client.post("/api/me/alerts/read", json={}, headers=dana).json() == {"unread": 0}
    assert client.get("/api/me/alerts/unread", headers=dana).json() == {"unread": 0}


# --- reminders --------------------------------------------------------------------------------

def reminded(client, mailer, conn, sale_at="2026-10-08T15:00Z", **switches) -> dict:
    """Dana's saved lot 1, selling at `sale_at`, with reminders set long before."""
    headers = dealer(client, mailer)
    insert_lot(conn, "1", sale_at=sale_at)
    save(client, headers, **({"remind_hour": True} | switches))
    conn.execute("UPDATE app.saved_lots SET reminders_set_at = '2026-10-01T00:00Z'")
    return headers


def test_a_reminder_goes_out_an_hour_before_the_auction_once(client, mailer, conn):
    reminded(client, mailer, conn)
    start = len(mailer.sent)

    run(client, "2026-10-08T13:59Z")
    assert mailer.sent[start:] == [] and inbox(conn) == []
    run(client, "2026-10-08T14:00Z")
    run(client, "2026-10-08T14:30Z")

    [(to, subject, body)] = mailer.sent[start:]
    assert to == "dana@example.com" and subject == "Auction reminder: 2020 TOYOTA CAMRY"
    assert "1 hour before" in body and "The auction starts Thu, Oct 8, 3:00 PM UTC." in body
    assert f"{WEB}/active-lots/copart/1" in body and f"{WEB}/settings" in body
    [entry] = inbox(conn)
    assert entry["kind"] == "reminder" and entry["payload"]["reminders"] == ["hour"]
    assert entry["payload"]["lot"]["lot_number"] == "1" and entry["payload"]["changes"] is None


def test_the_day_reminder(client, mailer, conn):
    reminded(client, mailer, conn, remind_hour=False, remind_day=True)
    start = len(mailer.sent)

    run(client, "2026-10-07T14:59Z")
    run(client, "2026-10-07T15:00Z")

    [(_, _, body)] = mailer.sent[start:]
    assert "1 day before" in body
    assert inbox(conn)[0]["payload"]["reminders"] == ["day"]


def test_a_sent_reminder_follows_the_auction_to_its_new_time(client, mailer, conn):
    reminded(client, mailer, conn)
    start = len(mailer.sent)
    run(client, "2026-10-08T14:00Z")
    conn.execute("UPDATE lots SET sale_at = '2026-10-09T15:00Z'")  # postponed a day

    run(client, "2026-10-08T14:30Z")
    assert len(mailer.sent) == start + 1, "the new time is a day away"
    run(client, "2026-10-09T14:00Z")

    assert len(mailer.sent) == start + 2 and "Fri, Oct 9, 3:00 PM UTC" in mailer.sent[-1][2]


def test_a_pending_reminder_moves_with_the_auction(client, mailer, conn):
    reminded(client, mailer, conn)
    start = len(mailer.sent)
    conn.execute("UPDATE lots SET sale_at = '2026-10-08T13:00Z'")  # brought forward

    run(client, "2026-10-08T12:00Z")
    run(client, "2026-10-08T14:00Z")  # the old time cancels nothing twice

    assert len(mailer.sent) == start + 1


def test_a_reminder_whose_time_had_come_when_it_was_set_never_goes_out(client, mailer, conn):
    reminded(client, mailer, conn, remind_day=True)
    conn.execute("UPDATE app.saved_lots SET reminders_set_at = '2026-10-08T14:30Z'")
    start = len(mailer.sent)

    run(client, "2026-10-08T14:45Z")

    assert mailer.sent[start:] == [] and inbox(conn) == []


def test_without_an_auction_time_reminders_wait_for_one(client, mailer, conn):
    reminded(client, mailer, conn, sale_at=None)
    start = len(mailer.sent)

    run(client, "2026-10-08T14:00Z")
    assert mailer.sent[start:] == []
    conn.execute("UPDATE lots SET sale_at = '2026-10-08T15:00Z'")
    run(client, "2026-10-08T14:10Z")

    assert len(mailer.sent) == start + 1


def test_a_lot_no_longer_listed_cancels_its_reminders_silently(client, mailer, conn):
    reminded(client, mailer, conn, remind_day=True)
    conn.execute("UPDATE lots SET removed_at = '2026-10-08T13:00Z'")
    start = len(mailer.sent)

    run(client, "2026-10-08T14:00Z")

    assert mailer.sent[start:] == [] and inbox(conn) == []
    assert conn.execute("SELECT remind_hour, remind_day FROM app.saved_lots").fetchone() == {
        "remind_hour": False, "remind_day": False}


def test_reminders_turned_off_by_email_still_reach_the_inbox(client, mailer, conn):
    headers = reminded(client, mailer, conn)
    settings(client, headers, email_reminders=False)
    start = len(mailer.sent)

    run(client, "2026-10-08T14:00Z")

    assert mailer.sent[start:] == [] and len(inbox(conn, "reminder")) == 1


def test_an_unconfirmed_address_gets_no_email(client, mailer, conn):
    reminded(client, mailer, conn)
    conn.execute("UPDATE app.users SET email_verified_at = NULL")
    start = len(mailer.sent)

    run(client, "2026-10-08T14:00Z")

    assert mailer.sent[start:] == [] and len(inbox(conn)) == 1



def test_a_future_sale_given_its_date_tells_a_dealer_waiting_with_reminders(client, mailer, conn):
    reminded(client, mailer, conn, sale_at=None)
    start = len(mailer.sent)
    conn.execute("UPDATE lots SET sale_at = '2026-10-12T15:00Z', buy_it_now_price = 8000")

    run(client, "2026-10-08T12:00Z")
    run(client, "2026-10-08T12:01Z")

    [(_, subject, body)] = mailer.sent[start:]
    assert subject == "A saved lot changed: 2020 TOYOTA CAMRY · Auction date set"
    assert "Auction date set: Mon, Oct 12, 3:00 PM UTC" in body
    assert "Buy now" not in body, "Buy now changes are for change alerts"
    [entry] = inbox(conn)
    [lot] = entry["payload"]["lots"]
    assert entry["kind"] == "changes" and set(lot["changes"]) == {"sale"}


def test_a_date_set_before_reminders_were_is_no_news(client, mailer, conn):
    headers = dealer(client, mailer)
    insert_lot(conn, "1", sale_at=None)
    save(client, headers)  # bookmarked while a future sale
    conn.execute("UPDATE lots SET sale_at = '2026-10-12T15:00Z'")
    save(client, headers, remind_hour=True)  # the date is on the page by now
    start = len(mailer.sent)

    run(client, "2026-10-08T12:00Z")

    assert mailer.sent[start:] == [] and inbox(conn) == []

# --- saved lot changes ------------------------------------------------------------------------

def watched(client, mailer, conn, **lot) -> dict:
    """Dana's saved lot 1 with change alerts on."""
    headers = dealer(client, mailer)
    insert_lot(conn, "1", **({"sale_at": "2026-10-09T15:00Z", "buy_it_now_price": 9500} | lot))
    save(client, headers, change_alerts=True)
    return headers


def test_a_change_goes_out_at_the_next_run_and_once(client, mailer, conn):
    watched(client, mailer, conn)
    start = len(mailer.sent)
    conn.execute("UPDATE lots SET sale_at = '2026-10-10T16:00Z'")

    run(client, "2026-10-08T12:00Z")
    run(client, "2026-10-08T12:01Z")

    [(_, subject, body)] = mailer.sent[start:]
    assert subject == "A saved lot changed: 2020 TOYOTA CAMRY · Auction date changed"
    assert "Auction: Fri, Oct 9, 3:00 PM UTC → Sat, Oct 10, 4:00 PM UTC" in body
    [entry] = inbox(conn)
    [lot] = entry["payload"]["lots"]
    assert entry["kind"] == "changes" and set(lot["changes"]) == {"sale"}
    sale = lot["changes"]["sale"]
    assert instant(sale["from"]["at"]) == datetime(2026, 10, 9, 15, tzinfo=UTC)
    assert instant(sale["to"]["at"]) == datetime(2026, 10, 10, 16, tzinfo=UTC)


def test_a_value_that_changes_back_before_the_run_reports_nothing(client, mailer, conn):
    watched(client, mailer, conn)
    start = len(mailer.sent)
    conn.execute("UPDATE lots SET buy_it_now_price = 8000")
    conn.execute("UPDATE lots SET buy_it_now_price = 9500.00")

    run(client, "2026-10-08T12:00Z")

    assert mailer.sent[start:] == [] and inbox(conn) == []


def test_changes_to_several_lots_make_one_email(client, mailer, conn):
    headers = watched(client, mailer, conn)
    insert_lot(conn, "2", make="HONDA", model_group="CIVIC", buy_it_now_price=None)
    save(client, headers, "2", change_alerts=True)
    start = len(mailer.sent)
    conn.execute("UPDATE lots SET sale_at = NULL, buy_it_now_price = 8900 WHERE lot_number = '1'")
    conn.execute("UPDATE lots SET buy_it_now_price = 4000 WHERE lot_number = '2'")

    run(client, "2026-10-08T12:00Z")

    [(_, subject, body)] = mailer.sent[start:]
    assert subject == "2 saved lots changed · Auction date changed and Buy now price changed"
    assert "Auction: Fri, Oct 9, 3:00 PM UTC → Future sale" in body
    assert "Buy now: $9,500 → $8,900" in body and "Buy now: not offered → $4,000" in body
    [entry] = inbox(conn)
    changed = {lot["lot"]["lot_number"]: set(lot["changes"]) for lot in entry["payload"]["lots"]}
    assert changed == {"1": {"sale", "buy_now"}, "2": {"buy_now"}}


def test_bids_and_other_columns_never_alert(client, mailer, conn):
    watched(client, mailer, conn)
    start = len(mailer.sent)
    conn.execute("UPDATE lots SET high_bid = 4000, odometer = 1, last_changed_at = now(), "
                 "source_updated_at = now()")

    run(client, "2026-10-08T12:00Z")

    assert mailer.sent[start:] == [] and inbox(conn) == []


def test_no_buy_now_and_a_zero_buy_now_are_the_same(client, mailer, conn):
    watched(client, mailer, conn, buy_it_now_price=None)
    start = len(mailer.sent)
    conn.execute("UPDATE lots SET buy_it_now_price = 0")

    run(client, "2026-10-08T12:00Z")

    assert mailer.sent[start:] == []


def test_a_saved_lot_without_change_alerts_sends_no_changes(client, mailer, conn):
    headers = dealer(client, mailer)
    insert_lot(conn, "1")
    save(client, headers)
    start = len(mailer.sent)
    conn.execute("UPDATE lots SET sale_at = '2026-10-11T00:00Z'")

    run(client, "2026-10-08T12:00Z")

    assert mailer.sent[start:] == [] and inbox(conn) == []


def test_a_lot_no_longer_listed_sends_no_changes(client, mailer, conn):
    watched(client, mailer, conn)
    start = len(mailer.sent)
    conn.execute("UPDATE lots SET removed_at = now(), sale_at = '2026-10-11T00:00Z'")

    run(client, "2026-10-08T12:00Z")

    assert mailer.sent[start:] == [] and inbox(conn) == []


# --- auctions that ended -----------------------------------------------------------------------

def ended(client, mailer, conn, sale_status: str | None, **switches) -> dict:
    """Dana's saved lot 1, whose auction on Oct 1 is over: the auction no longer lists it."""
    headers = dealer(client, mailer)
    insert_lot(conn, "1", sale_status=sale_status)
    save(client, headers, **switches)
    conn.execute("UPDATE lots SET removed_at = '2026-10-01T18:00Z'")
    run(client, "2026-10-01T18:01Z")
    return headers


def relist(conn, sale_at="2026-10-15T15:00Z") -> None:
    """Back for another auction, once the API database had dropped it."""
    conn.execute("DELETE FROM lots")
    insert_lot(conn, "1", sale_at=sale_at, last_changed_at="2026-10-08T00:00Z")


def relist_alert(conn) -> bool:
    return conn.execute("SELECT relist_alert FROM app.saved_lots").fetchone()["relist_alert"]


def test_the_saved_copy_follows_the_lot_until_the_api_database_drops_it(client, mailer, conn):
    headers = dealer(client, mailer)
    insert_lot(conn, "1", sale_status="On Minimum Bid", high_bid=1500)
    save(client, headers)
    conn.execute("UPDATE lots SET sale_at = '2026-10-03T15:00Z', sale_status = 'On Approval', "
                 "high_bid = 2100, last_changed_at = '2026-10-02T00:00Z'")

    run(client, "2026-10-02T12:00Z")
    conn.execute("DELETE FROM lots")

    lot = client.get("/api/me/saved-lots/copart/1", headers=headers).json()["lot"]
    assert (lot["sale_status"], lot["high_bid"]) == ("On Approval", 2100)
    assert instant(lot["sale_at"]) == datetime(2026, 10, 3, 15, tzinfo=UTC)


@pytest.mark.parametrize("sale_status", ["On Minimum Bid", "ON APPROVAL"])
def test_a_lot_that_may_not_have_sold_tells_the_dealer_when_it_is_listed_again(
        client, mailer, conn, sale_status):
    ended(client, mailer, conn, sale_status)  # nothing switched on
    assert relist_alert(conn) and inbox(conn) == []
    start = len(mailer.sent)

    relist(conn)
    run(client, "2026-10-08T12:00Z")
    run(client, "2026-10-08T12:01Z")

    [(_, subject, body)] = mailer.sent[start:]
    assert subject == "A saved lot changed: 2020 TOYOTA CAMRY · Relisted"
    assert "Relisted · Auction: Thu, Oct 15, 3:00 PM UTC" in body
    [entry] = inbox(conn)
    [lot] = entry["payload"]["lots"]
    assert entry["kind"] == "changes" and set(lot["changes"]) == {"relisted"}
    assert instant(lot["changes"]["relisted"]["at"]) == datetime(2026, 10, 15, 15, tzinfo=UTC)
    assert not relist_alert(conn)


def test_a_relisted_lot_with_change_alerts_reads_as_relisted_not_moved(client, mailer, conn):
    ended(client, mailer, conn, "On Approval", change_alerts=True)
    start = len(mailer.sent)

    relist(conn)
    run(client, "2026-10-08T12:00Z")
    run(client, "2026-10-08T12:01Z")

    [(_, subject, body)] = mailer.sent[start:]
    assert subject.endswith("· Relisted") and "→" not in body
    assert [set(lot["changes"]) for lot in inbox(conn)[0]["payload"]["lots"]] == [{"relisted"}]


@pytest.mark.parametrize("sale_status", ["Pure Sale", None])
def test_a_pure_sale_or_a_sale_type_not_stated_comes_back_without_news(client, mailer, conn,
                                                                       sale_status):
    ended(client, mailer, conn, sale_status)
    assert not relist_alert(conn)
    start = len(mailer.sent)

    relist(conn)
    run(client, "2026-10-08T12:00Z")

    assert mailer.sent[start:] == [] and inbox(conn) == []


def test_a_lot_still_listed_after_its_auction_waits_to_be_dropped(client, mailer, conn):
    headers = dealer(client, mailer)
    insert_lot(conn, "1", sale_status="On Minimum Bid")
    save(client, headers)

    run(client, "2026-10-01T18:00Z")  # bidding may still be going on

    assert not relist_alert(conn) and inbox(conn) == []


def test_a_reminder_and_a_change_due_together_are_one_email(client, mailer, conn):
    headers = watched(client, mailer, conn)
    save(client, headers, remind_hour=True)
    conn.execute("UPDATE app.saved_lots SET reminders_set_at = '2026-10-01T00:00Z'")
    conn.execute("UPDATE lots SET sale_at = '2026-10-08T12:30Z'")  # brought forward to today
    start = len(mailer.sent)

    run(client, "2026-10-08T12:00Z")
    run(client, "2026-10-08T12:01Z")

    [(_, subject, body)] = mailer.sent[start:]
    assert subject == "Auction reminder: 2020 TOYOTA CAMRY"
    assert "The auction starts Thu, Oct 8, 12:30 PM UTC." in body
    assert "Auction: Fri, Oct 9, 3:00 PM UTC → Thu, Oct 8, 12:30 PM UTC" in body
    [entry] = inbox(conn)
    assert entry["kind"] == "reminder" and set(entry["payload"]["changes"]) == {"sale"}


def test_a_change_is_emailed_even_when_its_reminder_is_not(client, mailer, conn):
    headers = watched(client, mailer, conn)
    save(client, headers, remind_hour=True)
    settings(client, headers, email_reminders=False)
    conn.execute("UPDATE app.saved_lots SET reminders_set_at = '2026-10-01T00:00Z'")
    conn.execute("UPDATE lots SET sale_at = '2026-10-08T12:30Z'")
    start = len(mailer.sent)

    run(client, "2026-10-08T12:00Z")

    [(_, subject, body)] = mailer.sent[start:]
    assert subject == "A saved lot changed: 2020 TOYOTA CAMRY · Auction date changed" and (
        "reminder" not in body.lower())
    assert [entry["kind"] for entry in inbox(conn)] == ["reminder"]


def test_changes_turned_off_by_email_still_reach_the_inbox(client, mailer, conn):
    headers = watched(client, mailer, conn)
    settings(client, headers, email_changes=False)
    start = len(mailer.sent)
    conn.execute("UPDATE lots SET buy_it_now_price = 9000")

    run(client, "2026-10-08T12:00Z")

    assert mailer.sent[start:] == [] and len(inbox(conn, "changes")) == 1


# --- search summaries -------------------------------------------------------------------------

def synced(conn, at: str) -> None:
    conn.execute("""INSERT INTO ingest_sync (sync_version, synced_at) VALUES (1, %s)
                    ON CONFLICT (id) DO UPDATE SET synced_at = EXCLUDED.synced_at""", (at,))


def summarized(client, mailer, conn, name="Toyotas", query="make=TOYOTA", **prefs) -> dict:
    """Dana's search with alerts on since Oct 7, 7:00 UTC, when her last summary went out."""
    headers = dealer(client, mailer)
    settings(client, headers, **prefs)
    search(client, headers, name=name, query=query, alerts=True)
    conn.execute("UPDATE app.saved_searches SET matched_through = '2026-10-07T07:00Z'")
    conn.execute("UPDATE app.alert_settings SET summary_at = '2026-10-07T07:00Z'")
    synced(conn, "2026-10-08T07:00Z")
    return headers


def test_the_summary_lists_lots_first_seen_since_the_last_that_match_now(client, mailer, conn):
    summarized(client, mailer, conn)
    insert_lot(conn, "1", first_seen_at="2026-10-07T10:00Z")
    insert_lot(conn, "2", first_seen_at="2026-10-07T10:00Z", make="HONDA")
    insert_lot(conn, "3", first_seen_at="2026-10-07T06:00Z")  # there before the last summary
    insert_lot(conn, "4", first_seen_at="2026-10-07T11:00Z", removed_at="2026-10-08T01:00Z")
    start = len(mailer.sent)

    run(client, "2026-10-08T06:59Z")
    assert mailer.sent[start:] == []
    run(client, "2026-10-08T07:00Z")
    run(client, "2026-10-08T07:05Z")

    [(_, subject, body)] = mailer.sent[start:]
    assert subject == "1 new lot matches your saved searches"
    assert "Toyotas: 1 new lot" in body and f"{WEB}/active-lots/copart/1\n" in body
    assert all(f"/copart/{n}\n" not in body for n in "234")
    [entry] = inbox(conn)
    assert entry["kind"] == "matches" and entry["payload"]["total"] == 1
    [group] = entry["payload"]["searches"]
    assert (group["name"], group["query"], group["total"]) == ("Toyotas", "make=TOYOTA", 1)
    assert [lot["lot_number"] for lot in group["lots"]] == ["1"]
    windows = conn.execute("SELECT matched_through AS t FROM app.saved_searches").fetchone()
    assert windows["t"] == datetime(2026, 10, 8, 6, 30, tzinfo=UTC)


def test_lots_first_seen_close_to_the_last_sync_wait_for_the_next_summary(client, mailer, conn):
    summarized(client, mailer, conn, twice_daily=True)
    insert_lot(conn, "1", first_seen_at="2026-10-08T06:45Z")
    start = len(mailer.sent)

    run(client, "2026-10-08T07:00Z")
    assert mailer.sent[start:] == []
    synced(conn, "2026-10-08T16:59Z")
    run(client, "2026-10-08T17:00Z")

    assert len(mailer.sent) == start + 1 and "/copart/1\n" in mailer.sent[-1][2]


def test_a_stalled_sync_holds_the_window_back(client, mailer, conn):
    summarized(client, mailer, conn)
    synced(conn, "2026-10-07T09:00Z")  # nothing synced since: later lots may still be coming
    insert_lot(conn, "1", first_seen_at="2026-10-07T08:00Z")
    insert_lot(conn, "2", first_seen_at="2026-10-07T10:00Z")

    run(client, "2026-10-08T07:00Z")

    body = mailer.sent[-1][2]
    assert "/copart/1\n" in body and "/copart/2\n" not in body


def test_summaries_come_at_seven_in_the_dealers_zone(client, mailer, conn):
    summarized(client, mailer, conn, time_zone="America/Chicago")
    conn.execute("UPDATE app.alert_settings SET summary_at = '2026-10-07T12:00Z'")  # 7:00 CDT
    insert_lot(conn, "1", first_seen_at="2026-10-07T10:00Z")
    synced(conn, "2026-10-08T12:00Z")
    start = len(mailer.sent)

    run(client, "2026-10-08T11:59Z")
    assert mailer.sent[start:] == []
    run(client, "2026-10-08T12:00Z")

    assert len(mailer.sent) == start + 1


def test_twice_a_day_adds_five_in_the_afternoon(client, mailer, conn):
    summarized(client, mailer, conn, twice_daily=True)
    insert_lot(conn, "1", first_seen_at="2026-10-08T09:00Z")
    synced(conn, "2026-10-08T16:00Z")
    start = len(mailer.sent)

    run(client, "2026-10-08T07:00Z")
    run(client, "2026-10-08T16:59Z")
    assert mailer.sent[start:] == []
    run(client, "2026-10-08T17:00Z")

    assert len(mailer.sent) == start + 1


def test_a_lot_matching_several_searches_is_listed_once(client, mailer, conn):
    headers = summarized(client, mailer, conn, name="Camry", query="make=TOYOTA&model=CAMRY")
    search(client, headers, name="All Toyota", query="make=TOYOTA", alerts=True)
    conn.execute("UPDATE app.saved_searches SET matched_through = '2026-10-07T07:00Z'")
    insert_lot(conn, "1", first_seen_at="2026-10-07T10:00Z")
    insert_lot(conn, "2", first_seen_at="2026-10-07T11:00Z", model_group="RAV4")

    run(client, "2026-10-08T07:00Z")

    _, subject, body = mailer.sent[-1]
    assert subject == "2 new lots match your saved searches"
    assert body.count(f"{WEB}/active-lots/copart/1\n") == 1
    assert "Also matches: All Toyota" in body
    first, second = inbox(conn)[0]["payload"]["searches"]
    assert (first["name"], first["total"], [lot["lot_number"] for lot in first["lots"]]) == (
        "Camry", 1, ["1"])
    assert first["lots"][0]["searches"] == ["Camry", "All Toyota"]
    assert (second["name"], second["total"], [lot["lot_number"] for lot in second["lots"]]) == (
        "All Toyota", 2, ["2"])


def test_nothing_new_sends_nothing_and_moves_on(client, mailer, conn):
    summarized(client, mailer, conn)
    start = len(mailer.sent)

    run(client, "2026-10-08T07:00Z")

    assert mailer.sent[start:] == [] and inbox(conn) == []
    assert conn.execute("SELECT summary_at FROM app.alert_settings").fetchone()["summary_at"] == (
        datetime(2026, 10, 8, 7, tzinfo=UTC))


def test_a_paused_search_reports_nothing(client, mailer, conn):
    headers = summarized(client, mailer, conn)
    found = client.get("/api/me/saved-searches", headers=headers).json()["items"][0]
    client.patch(f"/api/me/saved-searches/{found['id']}", json={"alerts": False}, headers=headers)
    insert_lot(conn, "1", first_seen_at="2026-10-07T10:00Z")
    start = len(mailer.sent)

    run(client, "2026-10-08T07:00Z")

    assert mailer.sent[start:] == [] and inbox(conn) == []


def test_an_update_never_makes_an_older_lot_news(client, mailer, conn):
    summarized(client, mailer, conn, twice_daily=True)
    insert_lot(conn, "1", first_seen_at="2026-10-07T06:00Z", make="HONDA")  # before the window
    insert_lot(conn, "2", first_seen_at="2026-10-07T10:00Z", make="HONDA")  # in the window
    start = len(mailer.sent)

    run(client, "2026-10-08T07:00Z")  # neither matches
    conn.execute("UPDATE lots SET make = 'TOYOTA', last_changed_at = '2026-10-08T08:00Z'")
    synced(conn, "2026-10-08T16:59Z")
    run(client, "2026-10-08T17:00Z")

    assert mailer.sent[start:] == [] and inbox(conn) == []


def test_each_search_lists_its_newest_lots_and_links_to_the_rest(client, mailer, conn,
                                                                monkeypatch):
    monkeypatch.setattr(alerts, "LOTS_PER_SEARCH", 2)
    summarized(client, mailer, conn)
    for n in (1, 2, 3):
        insert_lot(conn, str(n), first_seen_at=f"2026-10-07T1{n}:00Z")

    run(client, "2026-10-08T07:00Z")

    body = mailer.sent[-1][2]
    assert "Toyotas: 3 new lots" in body and f"See all 3: {WEB}/active-lots?make=TOYOTA" in body
    [group] = inbox(conn)[0]["payload"]["searches"]
    assert [lot["lot_number"] for lot in group["lots"]] == ["3", "2"]


def test_summaries_turned_off_by_email_still_reach_the_inbox(client, mailer, conn):
    summarized(client, mailer, conn, email_matches=False)
    insert_lot(conn, "1", first_seen_at="2026-10-07T10:00Z")
    start = len(mailer.sent)

    run(client, "2026-10-08T07:00Z")

    assert mailer.sent[start:] == [] and len(inbox(conn, "matches")) == 1


# --- language, failures, scheduling -----------------------------------------------------------

def test_a_spanish_speaking_dealer_reads_spanish_in_their_zone(client, mailer, conn):
    headers = reminded(client, mailer, conn)
    settings(client, headers, locale="es", time_zone="America/Mexico_City")
    start = len(mailer.sent)

    run(client, "2026-10-08T14:00Z")

    [(_, subject, body)] = mailer.sent[start:]
    assert subject == "Recordatorio de subasta: 2020 TOYOTA CAMRY"
    assert "La subasta empieza el jue 8 oct, 9:00 a. m. CST." in body
    assert f"{WEB}/es/active-lots/copart/1" in body and f"{WEB}/es/settings" in body


def test_a_broken_search_is_skipped_and_the_others_go_out(client, mailer, conn, caplog):
    headers = summarized(client, mailer, conn)
    dana = conn.execute("SELECT id FROM app.users").fetchone()["id"]
    conn.execute("""INSERT INTO app.saved_searches (user_id, name, query, alerts, matched_through)
                    VALUES (%s, 'Broken', 'year_min=soon', true, '2026-10-07T07:00Z')""", (dana,))
    insert_lot(conn, "1", first_seen_at="2026-10-07T10:00Z")

    with caplog.at_level(logging.WARNING, logger="app.alerts"):
        run(client, "2026-10-08T07:00Z")

    assert [g["name"] for g in inbox(conn)[0]["payload"]["searches"]] == ["Toyotas"]
    assert "Broken" not in mailer.sent[-1][2] and "skipped" in caplog.text
    assert headers


def test_one_dealers_failure_holds_up_nobody_else(client, mailer, conn, caplog, monkeypatch):
    reminded(client, mailer, conn)
    eve = dealer(client, mailer, "eve@example.com")
    save(client, eve, remind_hour=True)
    conn.execute("UPDATE app.saved_lots SET reminders_set_at = '2026-10-01T00:00Z'")
    real = alerts.alert_mail.news_mail
    calls = []

    def news_mail(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("the first dealer's mail breaks")
        return real(*args, **kwargs)

    monkeypatch.setattr(alerts.alert_mail, "news_mail", news_mail)
    start = len(mailer.sent)

    with caplog.at_level(logging.ERROR, logger="app.alerts"):
        run(client, "2026-10-08T14:00Z")
        assert len(mailer.sent) == start + 1, "the other dealer still gets theirs"
        assert len(inbox(conn)) == 1, "nothing of the first dealer's run was kept"
        run(client, "2026-10-08T14:01Z")

    assert sorted(to for to, _, _ in mailer.sent[start:]) == [
        "dana@example.com", "eve@example.com"], "the first dealer's at the next run, nobody twice"
    assert len(inbox(conn)) == 2 and "breaks" in caplog.text


def test_a_failed_send_keeps_the_alert_and_is_tried_again(client, mailer, conn, caplog):
    reminded(client, mailer, conn)
    start = len(mailer.sent)
    mailer.failures = 1

    with caplog.at_level(logging.WARNING, logger="app.outbox"):
        run(client, "2026-10-08T14:00Z")
    assert mailer.sent[start:] == [] and len(inbox(conn)) == 1
    assert "attempt 1" in caplog.text and "Connection unexpectedly closed" in caplog.text
    run(client, "2026-10-08T14:00:30Z")
    assert mailer.sent[start:] == [], "the next try waits a minute"
    run(client, "2026-10-08T14:01Z")

    assert len(mailer.sent) == start + 1 and len(inbox(conn)) == 1, "sent once, stored once"
    assert conn.execute("SELECT count(*) AS n FROM app.outbox").fetchone()["n"] == 0


def test_a_reminder_still_unsent_when_the_auction_starts_is_dropped(client, mailer, conn,
                                                                    caplog):
    reminded(client, mailer, conn)
    start = len(mailer.sent)
    mailer.failures = 4

    with caplog.at_level(logging.WARNING, logger="app.outbox"):
        for now in ("14:00", "14:01", "14:06", "14:21"):
            run(client, f"2026-10-08T{now}Z")
        run(client, "2026-10-08T15:00Z")

    assert mailer.sent[start:] == [] and len(inbox(conn)) == 1
    assert "expired unsent after 4 attempts" in caplog.text
    assert conn.execute("SELECT count(*) AS n FROM app.outbox").fetchone()["n"] == 0


def test_only_one_process_runs_the_job_at_a_time(client, mailer, conn, db_url):
    reminded(client, mailer, conn)
    start = len(mailer.sent)

    with psycopg.connect(db_url, autocommit=True) as other:
        other.execute("SELECT pg_advisory_lock(hashtext('api:alerts'))")
        run(client, "2026-10-08T14:00Z")
        assert mailer.sent[start:] == []
    run(client, "2026-10-08T14:00Z")

    assert len(mailer.sent) == start + 1


def test_the_job_runs_in_the_background_while_the_api_runs(conn, settings, mailer, monkeypatch,
                                                           caplog):
    calls = []

    def tick(state, now=None):
        calls.append(state)
        raise RuntimeError("one bad pass")

    monkeypatch.setattr(alerts, "tick", tick)
    app = create_app(settings.model_copy(update={"alerts_interval_seconds": 0.01}), mailer=mailer)
    with caplog.at_level(logging.ERROR, logger="app.alerts"), TestClient(app):
        deadline = time.monotonic() + 5
        while len(calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)

    assert len(calls) >= 2 and calls[0] is app.state, "a failing pass does not stop the next"
    assert "one bad pass" in caplog.text
