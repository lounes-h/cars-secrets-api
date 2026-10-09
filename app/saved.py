"""A dealer's saved lots and saved searches. Saving notifies nobody: change alerts, reminders and
a search's alerts are each switched on separately, and app/alerts.py sends them."""

from datetime import datetime
from typing import Annotated, Any, get_origin
from urllib.parse import parse_qs
from uuid import UUID

import psycopg
from fastapi import APIRouter
from psycopg.types.json import Jsonb
from pydantic import AfterValidator, BaseModel, Field, StringConstraints, ValidationError

from .accounts import Context, seed_settings
from .auth import RwConn, User, error
from .lots import COLUMNS, Lot, LotFilters, to_lot

router = APIRouter(prefix="/me", tags=["saved"])

# Per account, until plans set their own.
MAX_SAVED_LOTS = 500
MAX_SAVED_SEARCHES = 50


# --- saved lots -------------------------------------------------------------------------------

class SavedLot(BaseModel):
    lot: Lot
    # False once the auction no longer lists it; `lot` is then the last the API saw of it.
    available: bool
    saved_at: datetime
    change_alerts: bool
    remind_hour: bool
    remind_day: bool


class SavedLots(BaseModel):
    items: list[SavedLot]


class SavedLotIn(Context):
    """What to switch on or off; anything left out stays as it is."""

    change_alerts: bool | None = None
    remind_hour: bool | None = None
    remind_day: bool | None = None


SWITCHES = ("change_alerts", "remind_hour", "remind_day")


def saved_lots(conn: psycopg.Connection, user_id: Any,
               key: tuple[str, str] | None = None) -> list[SavedLot]:
    """The dealer's saved lots, last saved first: each as the API database holds it now
    (removed lots stay there until their auction is over), or as it was when saved."""
    where, params = "user_id = %(user)s", {"user": user_id}
    if key:
        where += " AND source = %(source)s AND lot_number = %(lot)s"
        params |= {"source": key[0], "lot": key[1]}
    rows = conn.execute(
        f"""SELECT source, lot_number, saved_at, lot, {", ".join(SWITCHES)} FROM app.saved_lots
            WHERE {where} ORDER BY saved_at DESC, source, lot_number""", params).fetchall()
    live = {(r["source"], r["lot_number"]): r for r in conn.execute(
        f"""SELECT {COLUMNS}, removed_at FROM lots WHERE (source, lot_number) IN
            (SELECT source, lot_number FROM app.saved_lots WHERE {where})""", params)}
    items = []
    for row in rows:
        now = live.get((row["source"], row["lot_number"]))
        items.append(SavedLot(
            lot=to_lot(now) if now else Lot(**row["lot"]),
            available=now is not None and now["removed_at"] is None,
            saved_at=row["saved_at"], **{name: row[name] for name in SWITCHES}))
    return items


@router.get("/saved-lots")
def list_saved_lots(user: User, conn: RwConn) -> SavedLots:
    return SavedLots(items=saved_lots(conn, user["id"]))


@router.get("/saved-lots/{source}/{lot_number}")
def saved_lot(source: str, lot_number: str, user: User, conn: RwConn) -> SavedLot:
    found = saved_lots(conn, user["id"], (source, lot_number))
    if not found:
        raise error(404, "not_saved", "This lot is not saved.")
    return found[0]


@router.put("/saved-lots/{source}/{lot_number}")
def save_lot(source: str, lot_number: str, body: SavedLotIn, user: User,
             conn: RwConn) -> SavedLot:
    """Saves a listed lot (once), and switches its alerts and reminders as asked. Switching
    change alerts on starts from what the lot says now; switching a reminder on records when,
    since a reminder whose time had already come by then never goes out."""
    key = {"user": user["id"], "source": source, "lot": lot_number}
    with conn.transaction():
        live = conn.execute(
            f"""SELECT {COLUMNS} FROM lots WHERE source = %(source)s AND lot_number = %(lot)s
                AND removed_at IS NULL""", key).fetchone()
        old = conn.execute(
            f"""SELECT {", ".join(SWITCHES)} FROM app.saved_lots WHERE user_id = %(user)s
                AND source = %(source)s AND lot_number = %(lot)s FOR UPDATE""", key).fetchone()
        if old is None:
            if live is None:
                raise error(404, "lot_not_found", "This lot is no longer listed.")
            count = conn.execute("SELECT count(*) AS n FROM app.saved_lots WHERE user_id = %s",
                                 (user["id"],)).fetchone()["n"]
            if count >= MAX_SAVED_LOTS:
                raise error(409, "limit_reached",
                            f"An account keeps at most {MAX_SAVED_LOTS} saved lots.")
            conn.execute(
                """INSERT INTO app.saved_lots (user_id, source, lot_number, lot, told_sale_at,
                                               told_sale_date, told_buy_now)
                   VALUES (%(user)s, %(source)s, %(lot)s, %(snapshot)s, %(sale_at)s,
                           %(sale_date)s, %(buy_it_now_price)s)
                   ON CONFLICT DO NOTHING""",
                key | live | {"snapshot": Jsonb(to_lot(live).model_dump(mode="json"))})
            old = dict.fromkeys(SWITCHES, False)
        new = {name: old[name] if getattr(body, name) is None else getattr(body, name)
               for name in SWITCHES}
        armed = any(new[name] and not old[name] for name in ("remind_hour", "remind_day"))
        told = live if new["change_alerts"] and not old["change_alerts"] and live else None
        # Reminders set without change alerts: the dealer knows the date the page shows, so only a
        # date set from now on is news (`SCHEDULED` in app/alerts.py).
        seen = live if armed and not new["change_alerts"] else told
        conn.execute(
            """UPDATE app.saved_lots SET change_alerts = %(change_alerts)s,
                   remind_hour = %(remind_hour)s, remind_day = %(remind_day)s,
                   reminders_set_at = CASE WHEN %(armed)s THEN now() ELSE reminders_set_at END,
                   told_sale_at = CASE WHEN %(seen)s THEN %(sale_at)s ELSE told_sale_at END,
                   told_sale_date = CASE WHEN %(seen)s THEN %(sale_date)s ELSE told_sale_date END,
                   told_buy_now = CASE WHEN %(told)s THEN %(buy_now)s::numeric
                                       ELSE told_buy_now END
               WHERE user_id = %(user)s AND source = %(source)s AND lot_number = %(lot)s""",
            key | new | {"armed": armed, "told": told is not None, "seen": seen is not None,
                         "sale_at": seen and seen["sale_at"],
                         "sale_date": seen and seen["sale_date"],
                         "buy_now": told and told["buy_it_now_price"]})
        seed_settings(conn, user["id"], body)
    return saved_lots(conn, user["id"], (source, lot_number))[0]


@router.delete("/saved-lots/{source}/{lot_number}")
def unsave_lot(source: str, lot_number: str, user: User, conn: RwConn) -> dict[str, bool]:
    """Its change alerts and reminders go with it."""
    conn.execute("DELETE FROM app.saved_lots WHERE user_id = %s AND source = %s "
                 "AND lot_number = %s", (user["id"], source, lot_number))
    return {"ok": True}


# --- saved searches ---------------------------------------------------------------------------

def parse_filters(query: str, tz: str = "UTC") -> LotFilters:
    """A saved search's filters, from the lot list's query string. What the list keeps for
    itself (the order, the page) and unknown parameters are ignored; a bad value raises."""
    values = parse_qs(query)
    data: dict[str, Any] = {"tz": tz}
    for name, field in LotFilters.model_fields.items():
        if name in values and name != "tz":
            data[name] = values[name] if get_origin(field.annotation) is list else values[name][0]
    return LotFilters(**data)


def valid_filters(query: str) -> str:
    try:
        parse_filters(query)
    except ValidationError as e:
        raise ValueError("not a lot list query") from e
    return query


Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
SearchQuery = Annotated[str, Field(max_length=2000), AfterValidator(valid_filters)]


class SavedSearch(BaseModel):
    id: UUID
    name: str
    query: str
    alerts: bool
    created_at: datetime


class SavedSearches(BaseModel):
    items: list[SavedSearch]


class SavedSearchIn(Context):
    name: Name
    query: SearchQuery = ""
    alerts: bool = False


class SavedSearchPatch(Context):
    """What to change; anything left out stays as it is."""

    name: Name | None = None
    query: SearchQuery | None = None
    alerts: bool | None = None


SEARCH = "id, name, query, alerts, created_at"


@router.get("/saved-searches")
def list_searches(user: User, conn: RwConn) -> SavedSearches:
    rows = conn.execute(f"SELECT {SEARCH} FROM app.saved_searches WHERE user_id = %s "
                        "ORDER BY created_at DESC", (user["id"],))
    return SavedSearches(items=[SavedSearch(**row) for row in rows])


@router.post("/saved-searches")
def create_search(body: SavedSearchIn, user: User, conn: RwConn) -> SavedSearch:
    """Alerts, when on, cover lots first seen from now on: never what the search finds today."""
    with conn.transaction():
        count = conn.execute("SELECT count(*) AS n FROM app.saved_searches WHERE user_id = %s",
                             (user["id"],)).fetchone()["n"]
        if count >= MAX_SAVED_SEARCHES:
            raise error(409, "limit_reached",
                        f"An account keeps at most {MAX_SAVED_SEARCHES} saved searches.")
        row = conn.execute(
            f"""INSERT INTO app.saved_searches (user_id, name, query, alerts)
                VALUES (%s, %s, %s, %s) RETURNING {SEARCH}""",
            (user["id"], body.name, body.query, body.alerts)).fetchone()
        seed_settings(conn, user["id"], body)
    return SavedSearch(**row)


@router.patch("/saved-searches/{search_id}")
def update_search(search_id: UUID, body: SavedSearchPatch, user: User,
                  conn: RwConn) -> SavedSearch:
    """Renames it, changes its filters, or pauses or resumes its alerts. Resumed alerts and new
    filters start from now, like a new search's."""
    with conn.transaction():
        old = conn.execute(
            """SELECT name, query, alerts FROM app.saved_searches WHERE id = %s AND user_id = %s
               FOR UPDATE""", (search_id, user["id"])).fetchone()
        if old is None:
            raise error(404, "not_found", "This saved search does not exist.")
        new = {name: old[name] if getattr(body, name) is None else getattr(body, name)
               for name in ("name", "query", "alerts")}
        restart = (new["alerts"] and not old["alerts"]) or new["query"] != old["query"]
        row = conn.execute(
            f"""UPDATE app.saved_searches SET name = %(name)s, query = %(query)s,
                    alerts = %(alerts)s,
                    matched_through = CASE WHEN %(restart)s THEN now() ELSE matched_through END
                WHERE id = %(id)s RETURNING {SEARCH}""",
            new | {"restart": restart, "id": search_id}).fetchone()
        seed_settings(conn, user["id"], body)
    return SavedSearch(**row)


@router.delete("/saved-searches/{search_id}")
def delete_search(search_id: UUID, user: User, conn: RwConn) -> dict[str, bool]:
    """Its alerts stop; lots saved on their own stay saved."""
    conn.execute("DELETE FROM app.saved_searches WHERE id = %s AND user_id = %s",
                 (search_id, user["id"]))
    return {"ok": True}
