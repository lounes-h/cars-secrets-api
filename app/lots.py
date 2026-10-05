"""Read-only access to auction lots (the `lots` table the ingest worker keeps in step)."""

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx
import psycopg
from fastapi import APIRouter, HTTPException, Query, Request
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

# Where a dealer goes to bid; the API never handles bidding itself.
LISTING_URLS = {"copart": "https://www.copart.com/lot/{lot_number}"}

# Copart's public photo list and the host its photos live on. Only these hosts are ever
# fetched or handed out, whatever the ingest stored in `image_url` / `thumbnail_url`.
IMAGE_API_HOST = "inventoryv2.copart.io"
IMAGE_HOST = "cs.copart.com"

# Every column of the API database's `lots` table except `removed_at`: the lot page shows all
# of them. The ingest's bookkeeping (attributes, row hashes, file ids) never reaches it.
COLUMNS = """source, lot_number, year, make, model_group AS model, model_detail, trim, vin,
    body_style, color, vehicle_type, engine, cylinders, fuel_type, transmission, drive,
    damage_primary, damage_secondary, title_type, title_state, has_keys,
    odometer, odometer_brand, runs_drives, est_retail_value, repair_cost, high_bid,
    buy_it_now_price, currency, sale_status, sale_date, sale_at, yard_code, yard_name,
    seller_name, location_city, location_state, location_zip, location_country,
    offsite_address, offsite_city, offsite_state, offsite_zip,
    thumbnail_url, image_url, source_created_at, source_updated_at,
    first_seen_at, last_changed_at, predicted_final_price"""


class Lot(BaseModel):
    source: str
    lot_number: str
    year: int | None
    make: str | None
    model: str | None
    model_detail: str | None
    trim: str | None
    vin: str | None
    body_style: str | None
    color: str | None
    vehicle_type: str | None
    engine: str | None
    cylinders: str | None
    fuel_type: str | None
    transmission: str | None
    drive: str | None
    damage_primary: str | None
    damage_secondary: str | None
    title_type: str | None
    title_state: str | None
    has_keys: str | None
    odometer: float | None
    odometer_brand: str | None
    runs_drives: str | None
    est_retail_value: float | None
    repair_cost: float | None
    high_bid: float | None
    buy_it_now_price: float | None
    currency: str | None
    sale_status: str | None
    sale_date: date | None
    sale_at: datetime | None
    yard_code: str | None
    yard_name: str | None
    seller_name: str | None
    location_city: str | None
    location_state: str | None
    location_zip: str | None
    location_country: str | None
    # Where the lot is held when it is not at the yard; all None for most lots.
    offsite_address: str | None
    offsite_city: str | None
    offsite_state: str | None
    offsite_zip: str | None
    thumbnail_url: str | None
    image_url: str | None
    source_created_at: datetime | None
    source_updated_at: datetime | None
    listing_url: str | None
    first_seen_at: datetime
    last_changed_at: datetime
    # The ingest's model estimate of the final auction price; None until a model prices the lot.
    predicted_final_price: float | None


class FacetValue(BaseModel):
    value: str
    count: int


class LotPage(BaseModel):
    items: list[Lot]
    total: int


class LotImage(BaseModel):
    """One photo of a lot, in the sizes Copart publishes for it."""

    thumb: str | None
    full: str | None


class LotImages(BaseModel):
    images: list[LotImage]


def to_lot(row: dict[str, Any]) -> Lot:
    template = LISTING_URLS.get(row["source"])
    url = template.format(lot_number=row["lot_number"]) if template else None
    return Lot(**row, listing_url=url)


def image_url(raw: Any) -> str | None:
    """An https URL on Copart's image host, or None."""
    if not isinstance(raw, str):
        return None
    parts = urlsplit(raw.strip())
    if parts.hostname != IMAGE_HOST:
        return None
    return urlunsplit(("https", parts.netloc, parts.path, parts.query, ""))


def lot_images(http: httpx.Client, row: dict[str, Any]) -> list[LotImage]:
    """Every photo of a lot, from Copart's public photo list. Some lots answer 404 there
    although copart.com shows photos; then only the export's thumbnail is available, whose
    full-size twin is `_thb` -> `_ful`. Photos are best effort: an upstream failure is an
    empty list, never an error. Not cached: one lot page is one upstream call.
    """
    listing = urlsplit((row.get("image_url") or "").strip())
    images: list[LotImage] = []
    if listing.hostname == IMAGE_API_HOST and listing.path.startswith("/v1/lotImages/"):
        url = urlunsplit(("https", listing.netloc, listing.path, listing.query, ""))
        try:
            response = http.get(url, timeout=8)
            response.raise_for_status()
            entries = response.json()["lotImages"]
            for entry in sorted(entries, key=lambda e: e.get("sequence") or 0):
                thumb = full = None
                for link in entry.get("link") or []:
                    photo = None if link.get("isEngineSound") else image_url(link.get("url"))
                    if photo is None:
                        continue
                    if link.get("isThumbNail"):
                        thumb = thumb or photo
                    else:
                        full = full or photo
                if thumb or full:
                    images.append(LotImage(thumb=thumb, full=full))
        except Exception:  # noqa: BLE001 - whatever Copart answered, the lot still has a page
            images = []
    if images:
        return images
    thumb = image_url(row.get("thumbnail_url"))
    if thumb is None:
        return []
    full = thumb.replace("_thb.", "_ful.") if "_thb." in thumb else None
    return [LotImage(thumb=thumb, full=full)]


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# Copart's wording and group for each title, keyed "STATE CODE": a copy of the web's
# src/lib/copart-titles.json (refreshed by its scripts/copart-titles.js). Copart groups the same
# code differently by state, so a lot's title group is looked up by its state and code.
COPART_TITLES: dict[str, list[str]] = json.loads(
    Path(__file__).with_name("copart_titles.json").read_text())
TITLE_GROUPS = Jsonb({pair: {"C": "clean", "S": "salvage", "J": "nonrepairable"}.get(group)
                      for pair, (_, group) in COPART_TITLES.items()})

# What each filter with options matches on, in the words the web shows. A lot whose code is none
# of these matches none of the options (an unknown sale status, a title copart.com never named).
FACETS = {
    "source": "source",
    "title": """%(titles)s::jsonb ->> (upper(trim(title_state)) || ' '
                || upper(trim(title_type)))""",
    # Copart's "DEFAULT" is a lot nobody checked, like a blank one: not verified to start.
    "run": """CASE upper(trim(runs_drives)) WHEN 'RUN & DRIVE VERIFIED' THEN 'verified'
              WHEN 'VEHICLE STARTS' THEN 'starts' ELSE 'unverified' END""",
    "damage": "damage_primary",
    "damage2": "damage_secondary",
    "keys": """CASE upper(trim(has_keys)) WHEN 'YES' THEN 'yes' WHEN 'NO' THEN 'no'
               WHEN 'EXM' THEN 'exempt' END""",
    "sale_type": """CASE upper(trim(sale_status)) WHEN 'PURE SALE' THEN 'pure'
                    WHEN 'ON MINIMUM BID' THEN 'minimum' WHEN 'ON APPROVAL' THEN 'approval' END""",
    "vehicle_type": "vehicle_type",
    "state": "location_state",
    "location": """CASE WHEN coalesce(offsite_address, offsite_city, offsite_state, offsite_zip)
                   IS NULL THEN 'yard' ELSE 'offsite' END""",
    "make": "make",
    "model": "model_group",
}
# The lots with each filter's value as a column (`f_title`, `f_run`, ...), for conditions and
# counts alike.
LOTS = f"""(SELECT lots.*, {", ".join(f"{expr} AS f_{name}" for name, expr in FACETS.items())}
            FROM lots) AS l"""
# A lot the auction has given neither a sale time nor a sale day: the lot page's "Future sale".
FUTURE_SALE = "sale_at IS NULL AND sale_date IS NULL"
SALE_WINDOWS = ("today", "tomorrow", "this_week", "next_week")
# Words that are not a make, model, year or lot match these, besides a make / model prefix.
DESCRIPTION = "concat_ws(' ', model_detail, trim, color, body_style, fuel_type, drive)"
MAKES_SHOWN = 100  # the most common makes; the rest are found through search
SORTS = {
    "newest": "first_seen_at DESC, lot_number",
    "sale": "sale_at ASC NULLS LAST, lot_number",
    "year": "year DESC NULLS LAST, lot_number",
    "mileage": "odometer ASC NULLS LAST, lot_number",
    # No value is not the lowest value.
    "retail": "NULLIF(est_retail_value, 0) ASC NULLS LAST, lot_number",
}

Codes = Annotated[list[str], Field(max_length=50)]
Amount = Annotated[int | None, Field(ge=0)]
Counted = Literal[
    "source", "title", "run", "damage", "damage2", "keys", "sale_type", "vehicle_type", "state",
    "location", "make", "model", "sale", "buy_now",
]


class LotFilters(BaseModel):
    """The lot list's search and filters. Each list filter matches any of its values."""

    q: str = Field("", max_length=200)
    source: Codes = []
    sale: Literal["today", "tomorrow", "this_week", "next_week", "future"] | None = None
    title: list[Literal["clean", "salvage", "nonrepairable"]] = []
    run: list[Literal["verified", "starts", "unverified"]] = []
    damage: Codes = []
    damage2: Codes = []
    keys: list[Literal["yes", "no", "exempt"]] = []
    sale_type: list[Literal["pure", "minimum", "approval"]] = []
    buy_now: bool = False
    vehicle_type: Codes = []
    state: Codes = []
    location: list[Literal["yard", "offsite"]] = []
    make: str | None = Field(None, max_length=100)
    model: str | None = Field(None, max_length=100)
    year_min: int | None = None
    year_max: int | None = None
    odometer_max: Amount = None
    retail_min: Amount = None
    retail_max: Amount = None
    bin_max: Amount = None
    bid_max: Amount = None
    # The dealer's zone, which decides when "today" starts; an unknown zone is UTC.
    tz: str = Field("UTC", max_length=64)


class LotQuery(LotFilters):
    sort: Literal["newest", "sale", "year", "mileage", "retail"] = "newest"
    limit: int = Field(25, ge=1, le=100)
    offset: int = Field(0, ge=0)


class CountQuery(LotFilters):
    # Which filters to count, one query each: the web asks for a list when the dealer opens it.
    facets: list[Counted] = Field(min_length=1)


class LotCounts(BaseModel):
    facets: dict[str, list[FacetValue]]


def zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    # Unknown: ZoneInfoNotFoundError (a KeyError); "../x": ValueError; "America": OSError.
    except (KeyError, ValueError, OSError):
        return ZoneInfo("UTC")


def sale_windows(now: datetime) -> dict[str, tuple[datetime, datetime]]:
    """Today, tomorrow, the rest of this week (to Sunday) and next week, in `now`'s zone."""
    today = now.date()
    monday = today + timedelta(days=7 - today.weekday())

    def start(day: date) -> datetime:
        return datetime(day.year, day.month, day.day, tzinfo=now.tzinfo)

    one = timedelta(days=1)
    return {
        "today": (start(today), start(today + one)),
        "tomorrow": (start(today + one), start(today + 2 * one)),
        "this_week": (start(today), start(monday)),
        "next_week": (start(monday), start(monday + 7 * one)),
    }


def lot_filters(f: LotFilters,
                now: datetime | None = None) -> tuple[dict[str, str], dict[str, Any]]:
    """Each active filter's SQL condition over LOTS, keyed by the filter, and their parameters."""
    now = (now or datetime.now(ZoneInfo("UTC"))).astimezone(zone(f.tz))
    conds: dict[str, str] = {}
    params: dict[str, Any] = {"titles": TITLE_GROUPS}
    for i, word in enumerate(f.q.split()[:8]):
        params |= {f"w{i}": word, f"w{i}_prefix": escape_like(word) + "%",
                   f"w{i}_any": "%" + escape_like(word) + "%"}
        conds[f"q{i}"] = f"""(lot_number = %(w{i})s OR vin = upper(%(w{i})s)
            OR make ILIKE %(w{i}_prefix)s OR model_group ILIKE %(w{i}_prefix)s
            OR {DESCRIPTION} ILIKE %(w{i}_any)s)"""
    for name in ("source", "title", "run", "damage", "damage2", "keys", "sale_type",
                 "vehicle_type", "state", "location"):
        if values := getattr(f, name):
            conds[name] = f"f_{name} = ANY(%({name})s)"
            params[name] = list(values)
    if f.make:
        conds["make"] = "upper(make) = upper(%(make)s)"
        params["make"] = f.make.strip()
    if f.model:
        # "RAV4" is also "RAV4 BASE" and "RAV4 PRIME", as Copart groups them.
        conds["model"] = """(upper(model_group) = upper(%(model)s)
            OR upper(model_group) LIKE upper(%(model_words)s))"""
        params |= {"model": f.model.strip(), "model_words": escape_like(f.model.strip()) + " %"}
    for name, (start, stop) in sale_windows(now).items():
        params |= {f"{name}_from": start, f"{name}_to": stop}
    if f.sale == "future":
        conds["sale"] = FUTURE_SALE
    elif f.sale:
        conds["sale"] = f"sale_at >= %({f.sale}_from)s AND sale_at < %({f.sale}_to)s"
    if f.buy_now:
        conds["buy_now"] = "buy_it_now_price > 0"
    for name, sql in (("year_min", "year >= %(year_min)s"), ("year_max", "year <= %(year_max)s"),
                      ("odometer_max", "odometer <= %(odometer_max)s"),
                      ("retail_min", "est_retail_value >= %(retail_min)s"),
                      ("retail_max", "est_retail_value <= %(retail_max)s"),
                      ("bin_max", "buy_it_now_price > 0 AND buy_it_now_price <= %(bin_max)s"),
                      ("bid_max", "coalesce(high_bid, 0) <= %(bid_max)s")):
        if getattr(f, name) is not None:
            conds[name] = sql
            params[name] = getattr(f, name)
    return conds, params


def list_lots(conn: psycopg.Connection, f: LotQuery, now: datetime | None = None) -> LotPage:
    conds, params = lot_filters(f, now)
    clause = " AND ".join(["removed_at IS NULL", *conds.values()])
    rows = conn.execute(
        f"""SELECT {COLUMNS} FROM {LOTS} WHERE {clause}
            ORDER BY {SORTS[f.sort]} LIMIT %(limit)s OFFSET %(offset)s""",
        params | {"limit": f.limit, "offset": f.offset},
    ).fetchall()
    total = conn.execute(f"SELECT count(*) AS n FROM {LOTS} WHERE {clause}", params).fetchone()
    return LotPage(items=[to_lot(r) for r in rows], total=total["n"])


def count_lots(conn: psycopg.Connection, f: CountQuery, now: datetime | None = None) -> LotCounts:
    """How many lots each option of the asked filters would show, most first. A filter's own
    choice is left out of its counts, so a second damage type can still be added to the first.
    ponytail: a scan per filter (~35 ms each at 150k lots); one pass with GROUPING SETS if the
    "All filters" panel, which asks for all of them, gets slow."""
    conds, params = lot_filters(f, now)

    def where(name: str) -> str:
        return " AND ".join(["removed_at IS NULL", *(c for k, c in conds.items() if k != name)])

    parts = []
    for name in dict.fromkeys(f.facets):
        if name == "sale":
            windows = ", ".join(f"('{w}', %({w}_from)s::timestamptz, %({w}_to)s::timestamptz)"
                                for w in SALE_WINDOWS)
            parts += [f"""SELECT 'sale' AS facet, w.value, count(*) AS n FROM {LOTS}
                          JOIN (VALUES {windows}) AS w (value, start, stop)
                          ON sale_at >= w.start AND sale_at < w.stop
                          WHERE {where('sale')} GROUP BY 2""",
                      f"""SELECT 'sale' AS facet, 'future' AS value, count(*) AS n FROM {LOTS}
                          WHERE {FUTURE_SALE} AND {where('sale')}"""]
        elif name == "buy_now":
            parts.append(f"""SELECT 'buy_now' AS facet, 'yes' AS value, count(*) AS n FROM {LOTS}
                             WHERE buy_it_now_price > 0 AND {where('buy_now')}""")
        elif name != "model" or f.make:  # a model is only listed among its make's
            parts.append(f"""SELECT '{name}' AS facet, f_{name} AS value, count(*) AS n FROM {LOTS}
                             WHERE {where(name)} GROUP BY 2 ORDER BY 3 DESC"""
                         + (f" LIMIT {MAKES_SHOWN}" if name == "make" else ""))
    sql = " UNION ALL ".join(f"({p})" for p in parts)
    rows = conn.execute(sql, params).fetchall() if parts else []
    counts: dict[str, list[FacetValue]] = {}
    for row in rows:
        if row["value"] is not None and row["n"]:
            counts.setdefault(row["facet"], []).append(FacetValue(value=row["value"],
                                                                   count=row["n"]))
    return LotCounts(facets={name: sorted(values, key=lambda v: (-v.count, v.value))
                             for name, values in counts.items()})


ACTIVITY_DAYS = {"today": 1, "last_3_days": 3, "last_7_days": 7}


class ActivityCounts(BaseModel):
    today: int
    last_3_days: int
    last_7_days: int


class Activity(BaseModel):
    """Active lots first seen in each window, and lots seen before it that changed in it."""

    added: ActivityCounts
    updated: ActivityCounts


def lot_activity(conn: psycopg.Connection, tz: str, now: datetime | None = None) -> Activity:
    """Each window starts at midnight in the dealer's zone: today, or 3 or 7 days counting today.
    A lot added in a window is not also counted as updated in it.
    ponytail: one scan of the active lots (~30 ms at 150k); index first_seen_at and
    last_changed_at if it gets slow."""
    now = (now or datetime.now(ZoneInfo("UTC"))).astimezone(zone(tz))
    starts = {}
    for name, days in ACTIVITY_DAYS.items():
        day = now.date() - timedelta(days=days - 1)
        starts[name] = datetime(day.year, day.month, day.day, tzinfo=now.tzinfo)
    columns = ", ".join(
        f"""count(*) FILTER (WHERE first_seen_at >= %({n})s) AS added_{n},
            count(*) FILTER (WHERE last_changed_at >= %({n})s AND first_seen_at < %({n})s)
            AS updated_{n}""" for n in ACTIVITY_DAYS)
    row = conn.execute(f"SELECT {columns} FROM lots WHERE removed_at IS NULL", starts).fetchone()
    return Activity(**{kind: ActivityCounts(**{n: row[f"{kind}_{n}"] for n in ACTIVITY_DAYS})
                       for kind in ("added", "updated")})


class LotKey(BaseModel):
    source: str
    lot_number: str


class Interpretation(BaseModel):
    """What a search names: one lot, or a make, model and years, and the words left over."""

    lot: LotKey | None = None
    make: str | None = None
    model: str | None = None
    year_min: int | None = None
    year_max: int | None = None
    q: str = ""


YEARS = re.compile(r"^((?:19|20)\d\d)(?:-((?:19|20)\d\d))?$")
LOT_NUMBER = re.compile(r"^\d{5,10}$")
VIN = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$", re.IGNORECASE)
# Rarer makes are misspelt ("TOYT") or not cars ("WHITE" trucks): their words stay keywords.
MAKE_MIN_LOTS = 10


def squeeze(text: str) -> str:
    """"Mercedes Benz", "mercedes-benz" and "MERCEDES-BENZ" all read "mercedesbenz"."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def take(words: list[str], names: dict[str, Any]) -> tuple[Any, list[str]]:
    """The first name spelled by up to three consecutive words, longest first, and the rest."""
    for size in (3, 2, 1):
        for i in range(len(words) - size + 1):
            if (key := squeeze("".join(words[i:i + size]))) and key in names:
                return names[key], words[:i] + words[i + size:]
    return None, words


def interpret(conn: psycopg.Connection, q: str) -> Interpretation:
    words = q.split()[:12]
    if len(words) == 1 and (LOT_NUMBER.match(words[0]) or VIN.match(words[0])):
        column = "lot_number" if LOT_NUMBER.match(words[0]) else "vin"
        found = conn.execute(
            f"""SELECT source, lot_number FROM lots WHERE removed_at IS NULL
                AND {column} = upper(%s) LIMIT 2""", (words[0],),
        ).fetchall()
        if len(found) == 1:
            return Interpretation(lot=LotKey(**found[0]))
    years: list[int] = []
    rest: list[str] = []
    for word in words:
        if match := YEARS.match(word):
            years += [int(y) for y in match.groups() if y]
        else:
            rest.append(word)
    pairs = [p for p in conn.execute(
        """SELECT make, model_group, count(*) AS n,
                  sum(count(*)) OVER (PARTITION BY make) AS make_lots
           FROM lots WHERE removed_at IS NULL AND make IS NOT NULL AND model_group IS NOT NULL
           GROUP BY 1, 2""",
    ).fetchall() if p["make_lots"] >= MAKE_MIN_LOTS]
    make, rest = take(rest, {squeeze(p["make"]): p["make"] for p in pairs})
    models: dict[str, dict[tuple[str, str], int]] = {}
    for p in pairs:
        if make is None or p["make"] == make:
            named = models.setdefault(squeeze(p["model_group"]), {})
            named[p["make"], p["model_group"]] = p["n"]
    named, after = take(rest, models)
    model = None
    # A model alone names its make too, when that make has nearly all of its lots: "1500" is a
    # Ram rather than a GMC, but "All Models" is nobody's.
    if named:
        (top, n) = max(named.items(), key=lambda item: (item[1], item[0]))
        if n >= 0.9 * sum(named.values()):
            (make, model), rest = top, after
    return Interpretation(make=make, model=model, year_min=min(years, default=None),
                          year_max=max(years, default=None), q=" ".join(rest))


def get_lot(conn: psycopg.Connection, source: str, lot_number: str) -> Lot | None:
    row = conn.execute(
        f"SELECT {COLUMNS} FROM lots WHERE source = %s AND lot_number = %s "
        "AND removed_at IS NULL", (source, lot_number),
    ).fetchone()
    return to_lot(row) if row else None


def router(Conn: Any) -> APIRouter:
    r = APIRouter(prefix="/lots", tags=["lots"])

    @r.get("")
    def lots(conn: Conn, f: Annotated[LotQuery, Query()]) -> LotPage:
        """Active lots, filtered and ordered, the most recently listed first by default."""
        return list_lots(conn, f)

    @r.get("/counts")
    def counts(conn: Conn, f: Annotated[CountQuery, Query()]) -> LotCounts:
        """How many lots each option of the asked filters would show, given the other filters."""
        return count_lots(conn, f)

    @r.get("/activity")
    def activity(conn: Conn, tz: Annotated[str, Query(max_length=64)] = "UTC") -> Activity:
        """How many lots were added, and how many changed, today and in the last 3 and 7 days,
        in the dealer's zone (an unknown zone is UTC)."""
        return lot_activity(conn, tz)

    @r.get("/interpret")
    def interpret_search(conn: Conn,
                         q: Annotated[str, Query(max_length=200)] = "") -> Interpretation:
        """What a dealer's search names, so the web can turn it into filters or open the lot."""
        return interpret(conn, q)

    @r.get("/{source}/{lot_number}")
    def lot(source: str, lot_number: str, conn: Conn) -> Lot:
        found = get_lot(conn, source, lot_number)
        if found is None:
            raise HTTPException(404, "lot not found")
        return found

    @r.get("/{source}/{lot_number}/images")
    def images(source: str, lot_number: str, conn: Conn, request: Request) -> LotImages:
        """Photos of a lot. Copart is asked with the URLs stored for the lot, never with
        anything the caller sends."""
        row = conn.execute(
            "SELECT image_url, thumbnail_url FROM lots WHERE source = %s AND lot_number = %s "
            "AND removed_at IS NULL", (source, lot_number),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "lot not found")
        return LotImages(images=lot_images(request.app.state.http, row))

    return r
