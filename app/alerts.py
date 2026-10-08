"""Alerts: each dealer's settings and inbox, and the job that fills them, once a minute in the API
process (`loop`, started by app/main.py).

- A reminder goes out when its time comes: 1 hour or 1 day before the auction's scheduled start,
  once per schedule, so it follows the auction when it moves.
- A saved lot's changes (its auction time, its Buy now price) go out at the next run, measured
  against what the dealer was last told: a value that changes back says nothing.
- Lots first seen since a search's last summary that match it go out at 7:00, or 7:00 and
  17:00, in the dealer's zone. Updates to older lots never do.

Everything lands in the inbox, one entry per email section; email follows the dealer's settings.
"""

import logging
import threading
from datetime import UTC, datetime, time, timedelta
from itertools import groupby
from typing import Annotated, Any, Literal

import psycopg
from fastapi import APIRouter, Query
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, ValidationError, field_validator

from . import alert_mail
from .auth import RwConn, User
from .lots import COLUMNS, LOTS, SORTS, Lot, lot_filters, to_lot, zone
from .saved import parse_filters

log = logging.getLogger(__name__)
router = APIRouter(prefix="/me", tags=["alerts"])

Kind = Literal["matches", "changes", "reminder"]


# --- settings ---------------------------------------------------------------------------------

class AlertSettings(BaseModel):
    email_matches: bool = True
    email_changes: bool = True
    email_reminders: bool = True
    # Search summaries at 7:00 and 17:00 rather than 7:00 only, in `time_zone`.
    twice_daily: bool = False
    # None until the dealer's first alert or Settings sets them: the web offers its own then.
    time_zone: str | None = None
    locale: Literal["en", "es"] | None = None


class AlertSettingsIn(BaseModel):
    email_matches: bool
    email_changes: bool
    email_reminders: bool
    twice_daily: bool
    time_zone: str = Field(max_length=64)
    locale: Literal["en", "es"]

    @field_validator("time_zone")
    @classmethod
    def known_zone(cls, value: str) -> str:
        if zone(value).key != value:
            raise ValueError("unknown time zone")
        return value


SETTINGS = ("email_matches", "email_changes", "email_reminders", "twice_daily", "time_zone",
            "locale")


@router.get("/alert-settings")
def get_settings(user: User, conn: RwConn) -> AlertSettings:
    row = conn.execute(f"SELECT {', '.join(SETTINGS)} FROM app.alert_settings WHERE user_id = %s",
                       (user["id"],)).fetchone()
    return AlertSettings(**row) if row else AlertSettings()


@router.put("/alert-settings")
def put_settings(body: AlertSettingsIn, user: User, conn: RwConn) -> AlertSettings:
    row = conn.execute(
        f"""INSERT INTO app.alert_settings (user_id, {', '.join(SETTINGS)})
            VALUES (%(user)s, {', '.join(f'%({name})s' for name in SETTINGS)})
            ON CONFLICT (user_id) DO UPDATE
            SET {', '.join(f'{name} = EXCLUDED.{name}' for name in SETTINGS)}
            RETURNING {', '.join(SETTINGS)}""",
        body.model_dump() | {"user": user["id"]}).fetchone()
    return AlertSettings(**row)


# --- inbox ------------------------------------------------------------------------------------

class Alert(BaseModel):
    id: int
    kind: Kind
    payload: dict[str, Any]
    created_at: datetime
    read: bool


class AlertPage(BaseModel):
    items: list[Alert]
    total: int  # of the kind asked for
    unread: int  # of every kind


class ReadIn(BaseModel):
    """The alerts to mark read; none named means all of them."""

    ids: list[int] | None = Field(None, max_length=100)


def unread_count(conn: psycopg.Connection, user_id: Any) -> int:
    return conn.execute("SELECT count(*) AS n FROM app.alerts WHERE user_id = %s "
                        "AND read_at IS NULL", (user_id,)).fetchone()["n"]


@router.get("/alerts")
def list_alerts(user: User, conn: RwConn, kind: Kind | None = None,
                limit: Annotated[int, Query(ge=1, le=100)] = 20,
                offset: Annotated[int, Query(ge=0)] = 0) -> AlertPage:
    """The dealer's alerts, newest first."""
    params = {"user": user["id"], "kind": kind, "limit": limit, "offset": offset}
    where = "user_id = %(user)s" + (" AND kind = %(kind)s" if kind else "")
    rows = conn.execute(
        f"""SELECT id, kind, payload, created_at, read_at IS NOT NULL AS read FROM app.alerts
            WHERE {where} ORDER BY id DESC LIMIT %(limit)s OFFSET %(offset)s""", params)
    total = conn.execute(f"SELECT count(*) AS n FROM app.alerts WHERE {where}", params).fetchone()
    return AlertPage(items=[Alert(**row) for row in rows], total=total["n"],
                     unread=unread_count(conn, user["id"]))


@router.get("/alerts/unread")
def unread(user: User, conn: RwConn) -> dict[str, int]:
    return {"unread": unread_count(conn, user["id"])}


@router.post("/alerts/read")
def mark_read(body: ReadIn, user: User, conn: RwConn) -> dict[str, int]:
    """Reading changes nothing else: saved lots, searches and settings stay as they are."""
    conn.execute(
        """UPDATE app.alerts SET read_at = now() WHERE user_id = %(user)s AND read_at IS NULL
           AND (%(all)s OR id = ANY(%(ids)s::bigint[]))""",
        {"user": user["id"], "all": body.ids is None, "ids": body.ids or []})
    return {"unread": unread_count(conn, user["id"])}


# --- the job ----------------------------------------------------------------------------------

HOUR, DAY = timedelta(hours=1), timedelta(days=1)
SUMMARY_HOURS = {False: (7,), True: (7, 17)}
# The API database lags the ingest: a lot first seen this close to the last sync may not be here
# yet, so it waits for the next summary rather than be missed by this one.
SYNC_MARGIN = timedelta(minutes=30)
LOTS_PER_SEARCH = 10
# What an alert keeps of a lot: enough to show it once the API database has dropped it.
SUMMARY = {"source", "lot_number", "year", "make", "model", "trim", "sale_at", "sale_date",
           "location_city", "location_state", "thumbnail_url", "high_bid", "buy_it_now_price",
           "currency", "odometer", "damage_primary"}

DEALER = """u.email, u.email_verified_at IS NOT NULL AS verified,
    coalesce(st.email_matches, true) AS email_matches,
    coalesce(st.email_changes, true) AS email_changes,
    coalesce(st.email_reminders, true) AS email_reminders,
    coalesce(st.twice_daily, false) AS twice_daily,
    coalesce(st.time_zone, 'UTC') AS time_zone, coalesce(st.locale, 'en') AS locale,
    st.summary_at"""
# Only the auction's time and the Buy now price can be news; `differences` says whether they are.
CHANGED = """(s.change_alerts AND (l.sale_at IS DISTINCT FROM s.told_sale_at
    OR l.sale_date IS DISTINCT FROM s.told_sale_date
    OR l.buy_it_now_price IS DISTINCT FROM s.told_buy_now))"""
# A lot no longer listed keeps no reminders; it sends no changes either (`lot_news`).
CANCEL_REMINDERS = """UPDATE app.saved_lots s SET remind_hour = false, remind_day = false
    WHERE (remind_hour OR remind_day) AND NOT EXISTS (
        SELECT 1 FROM lots l WHERE l.source = s.source AND l.lot_number = s.lot_number
        AND l.removed_at IS NULL)"""


def due(which: str) -> str:
    """A reminder whose time has come for the auction's current schedule: before the auction,
    set before that time, and not yet sent for this schedule."""
    return f"""(s.remind_{which} AND l.sale_at - %({which})s <= %(now)s AND %(now)s < l.sale_at
                AND s.reminders_set_at <= l.sale_at - %({which})s
                AND s.{which}_sent_for IS DISTINCT FROM l.sale_at)"""


def loop(state: Any, stop: threading.Event) -> None:
    """Runs the job every `alerts_interval_seconds` until `stop` is set."""
    while not stop.wait(state.settings.alerts_interval_seconds):
        try:
            tick(state)
        except Exception:
            log.exception("alerts: a run failed")


def tick(state: Any, now: datetime | None = None) -> None:
    """One run: reminders and changes, then the summaries that are due. One process at a time.
    Each dealer's alerts are stored before their email goes out: a failed send is logged and the
    inbox keeps the alert. ponytail: no retry; add an outbox if sends fail in earnest."""
    now = now or datetime.now(UTC)
    with state.rw_pool.connection() as conn:
        if not conn.execute("SELECT pg_try_advisory_lock(hashtext('api:alerts')) AS ok"
                            ).fetchone()["ok"]:
            return
        try:
            conn.execute(CANCEL_REMINDERS)
            for rows in lot_news(conn, now):
                guarded(send_lot_news, conn, state, rows)
            for dealer in due_summaries(conn, now):
                guarded(send_summary, conn, state, dealer, now)
        finally:
            conn.execute("SELECT pg_advisory_unlock(hashtext('api:alerts'))")


def guarded(work: Any, *args: Any) -> None:
    """One dealer's failure holds up nobody else's alerts."""
    try:
        work(*args)
    except Exception:
        log.exception("alerts: one dealer's alerts failed")


def summary(lot: Lot) -> dict[str, Any]:
    return lot.model_dump(mode="json", include=SUMMARY)


def add(conn: psycopg.Connection, user_id: Any, kind: Kind, payload: dict[str, Any]) -> None:
    conn.execute("INSERT INTO app.alerts (user_id, kind, payload) VALUES (%s, %s, %s)",
                 (user_id, kind, Jsonb(payload)))


def offer(value: Any) -> float | None:
    """A Buy now price; 0 is none."""
    return float(value) if value else None


def differences(row: dict[str, Any]) -> dict[str, Any] | None:
    """What changed since the dealer was last told: the auction time, the Buy now price."""
    changes: dict[str, Any] = {}
    if (row["sale_at"], row["sale_date"]) != (row["told_sale_at"], row["told_sale_date"]):
        changes["sale"] = {
            "from": {"at": iso(row["told_sale_at"]), "date": iso(row["told_sale_date"])},
            "to": {"at": iso(row["sale_at"]), "date": iso(row["sale_date"])}}
    if offer(row["buy_it_now_price"]) != offer(row["told_buy_now"]):
        changes["buy_now"] = {"from": offer(row["told_buy_now"]),
                              "to": offer(row["buy_it_now_price"])}
    return changes or None


def iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def lot_news(conn: psycopg.Connection, now: datetime) -> list[list[dict[str, Any]]]:
    """Every listed saved lot with a reminder due or a change to tell, by dealer."""
    rows = conn.execute(
        f"""SELECT s.user_id, s.change_alerts, s.told_sale_at, s.told_sale_date, s.told_buy_now,
                   {due('hour')} AS hour_due, {due('day')} AS day_due, {DEALER}, l.*
            FROM app.saved_lots s
            JOIN app.users u ON u.id = s.user_id
            LEFT JOIN app.alert_settings st ON st.user_id = s.user_id
            JOIN LATERAL (SELECT {COLUMNS} FROM lots WHERE lots.source = s.source
                          AND lots.lot_number = s.lot_number AND lots.removed_at IS NULL) l ON true
            WHERE {CHANGED} OR {due('hour')} OR {due('day')}
            ORDER BY s.user_id, l.sale_at NULLS LAST, l.lot_number""",
        {"now": now, "hour": HOUR, "day": DAY}).fetchall()
    return [list(rows) for _, rows in groupby(rows, key=lambda row: row["user_id"])]


def send_lot_news(conn: psycopg.Connection, state: Any, rows: list[dict[str, Any]]) -> None:
    """One dealer's reminders and changes: an inbox entry per reminded lot (its changes folded
    in) and one for the other changed lots, then one email."""
    dealer = rows[0]
    reminders, changes = [], []
    for row in rows:
        lot = summary(to_lot(row))
        diff = differences(row) if row["change_alerts"] else None
        if which := [name for name in ("day", "hour") if row[f"{name}_due"]]:
            reminders.append({"lot": lot, "reminders": which, "changes": diff})
        elif diff:
            changes.append({"lot": lot, "changes": diff})
    with conn.transaction():
        for item in reminders:
            add(conn, dealer["user_id"], "reminder", item)
        if changes:
            add(conn, dealer["user_id"], "changes", {"lots": changes})
        for row in rows:
            conn.execute(
                """UPDATE app.saved_lots SET
                       hour_sent_for = CASE WHEN %(hour)s THEN %(sale_at)s ELSE hour_sent_for END,
                       day_sent_for = CASE WHEN %(day)s THEN %(sale_at)s ELSE day_sent_for END,
                       told_sale_at = CASE WHEN %(told)s THEN %(sale_at)s ELSE told_sale_at END,
                       told_sale_date = CASE WHEN %(told)s THEN %(sale_date)s
                                             ELSE told_sale_date END,
                       told_buy_now = CASE WHEN %(told)s THEN %(buy_now)s::numeric
                                           ELSE told_buy_now END
                   WHERE user_id = %(user)s AND source = %(source)s AND lot_number = %(lot)s""",
                {"hour": row["hour_due"], "day": row["day_due"], "told": row["change_alerts"],
                 "sale_at": row["sale_at"], "sale_date": row["sale_date"],
                 "buy_now": row["buy_it_now_price"], "user": row["user_id"],
                 "source": row["source"], "lot": row["lot_number"]})
    emailed = reminders if dealer["email_reminders"] else []
    told = changes + [{"lot": item["lot"], "changes": item["changes"]}
                      for item in reminders if item["changes"] and not emailed]
    told = told if dealer["email_changes"] else []
    if dealer["verified"] and (emailed or told):
        state.mailer.send(dealer["email"], *alert_mail.news_mail(
            emailed, told, dealer["locale"], zone(dealer["time_zone"]), state.settings.web_url))


def last_slot(now: datetime, tz: Any, twice_daily: bool) -> datetime:
    """The latest summary time at or before `now`, in the dealer's zone."""
    local = now.astimezone(tz)
    return max(slot for day in (local.date() - DAY, local.date())
               for hour in SUMMARY_HOURS[twice_daily]
               if (slot := datetime.combine(day, time(hour), tz)) <= local)


def due_summaries(conn: psycopg.Connection, now: datetime) -> list[dict[str, Any]]:
    """The dealers with a search's alerts on whose summary time has come since their last."""
    rows = conn.execute(
        f"""SELECT u.id AS user_id, {DEALER} FROM app.users u
            LEFT JOIN app.alert_settings st ON st.user_id = u.id
            WHERE EXISTS (SELECT 1 FROM app.saved_searches s WHERE s.user_id = u.id AND s.alerts)
            ORDER BY u.id""").fetchall()
    found = []
    for row in rows:
        slot = last_slot(now, zone(row["time_zone"]), row["twice_daily"])
        if row["summary_at"] is None or row["summary_at"] < slot:
            found.append(row | {"slot": slot})
    return found


def send_summary(conn: psycopg.Connection, state: Any, dealer: dict[str, Any],
                 now: datetime) -> None:
    """The lots first seen since each search's last summary (up to a little before the last
    sync, see SYNC_MARGIN) that match it now and are still listed. Windows follow one another,
    so no lot is told twice, and a lot seen before a window never qualifies for it."""
    synced = conn.execute("SELECT synced_at FROM ingest_sync").fetchone()
    cutoff = min(now, synced["synced_at"] if synced else now) - SYNC_MARGIN
    searches = conn.execute(
        """SELECT id, name, query, matched_through FROM app.saved_searches
           WHERE user_id = %s AND alerts ORDER BY created_at, id""",
        (dealer["user_id"],)).fetchall()
    found = []
    for search in searches:
        try:
            filters = parse_filters(search["query"], dealer["time_zone"])
        except ValidationError:
            log.warning("alerts: saved search %s skipped: its filters no longer read",
                        search["id"])
            continue
        conds, params = lot_filters(filters, now)
        clause = " AND ".join(["removed_at IS NULL", "first_seen_at > %(since)s",
                               "first_seen_at <= %(until)s", *conds.values()])
        keys = [(row["source"], row["lot_number"]) for row in conn.execute(
            f"SELECT source, lot_number FROM {LOTS} WHERE {clause} ORDER BY {SORTS['newest']}",
            params | {"since": search["matched_through"], "until": cutoff})]
        if keys:
            found.append((search, keys))
    payload = matches(conn, found)
    with conn.transaction():
        if payload:
            add(conn, dealer["user_id"], "matches", payload)
        conn.execute("""UPDATE app.saved_searches
                        SET matched_through = greatest(matched_through, %s)
                        WHERE user_id = %s AND alerts""", (cutoff, dealer["user_id"]))
        conn.execute("""INSERT INTO app.alert_settings (user_id, summary_at) VALUES (%s, %s)
                        ON CONFLICT (user_id) DO UPDATE SET summary_at = EXCLUDED.summary_at""",
                     (dealer["user_id"], dealer["slot"]))
    if payload and dealer["email_matches"] and dealer["verified"]:
        state.mailer.send(dealer["email"], *alert_mail.summary_mail(
            payload, dealer["locale"], zone(dealer["time_zone"]), state.settings.web_url))


def matches(conn: psycopg.Connection, found: list[tuple[dict[str, Any], list[tuple[str, str]]]]
            ) -> dict[str, Any] | None:
    """Each search's new lots, newest first and at most LOTS_PER_SEARCH of them. A lot matching
    several searches is listed once, under the first, and names every search it matches."""
    if not found:
        return None
    names: dict[tuple[str, str], list[str]] = {}
    for search, keys in found:
        for key in keys:
            names.setdefault(key, []).append(search["name"])
    shown: set[tuple[str, str]] = set()
    groups = []
    for search, keys in found:
        own = [key for key in keys if key not in shown][:LOTS_PER_SEARCH]
        shown.update(own)
        groups.append((search, keys, own))
    rows = conn.execute(
        f"""SELECT {COLUMNS} FROM lots JOIN unnest(%s::text[], %s::text[]) AS k (src, num)
            ON source = k.src AND lot_number = k.num""",
        ([key[0] for key in shown], [key[1] for key in shown]))
    lots = {(row["source"], row["lot_number"]): summary(to_lot(row)) for row in rows}
    return {"total": len(names), "searches": [
        {"id": str(search["id"]), "name": search["name"], "query": search["query"],
         "total": len(keys),
         "lots": [lots[key] | {"searches": names[key]} for key in own if key in lots]}
        for search, keys, own in groups]}
