"""Read-only access to auction lots (the ingest repo's `lots` table)."""

from datetime import datetime
from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

# Where a dealer goes to bid; the API never handles bidding itself.
LISTING_URLS = {"copart": "https://www.copart.com/lot/{lot_number}"}

COLUMNS = """source, lot_number, year, make, model_group AS model, model_detail, trim, vin,
    body_style, color, damage_primary, damage_secondary, title_type, title_state, has_keys,
    odometer, odometer_brand, runs_drives, est_retail_value, repair_cost, high_bid,
    buy_it_now_price, currency, sale_at, yard_name, location_city, location_state,
    thumbnail_url, image_url, first_seen_at, last_changed_at"""


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
    sale_at: datetime | None
    yard_name: str | None
    location_city: str | None
    location_state: str | None
    thumbnail_url: str | None
    image_url: str | None
    listing_url: str | None
    first_seen_at: datetime
    last_changed_at: datetime


class LotPage(BaseModel):
    items: list[Lot]
    total: int


def to_lot(row: dict[str, Any]) -> Lot:
    template = LISTING_URLS.get(row["source"])
    url = template.format(lot_number=row["lot_number"]) if template else None
    return Lot(**row, listing_url=url)


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def list_lots(
    conn: psycopg.Connection, *, q: str = "", make: str | None = None, source: str | None = None,
    year_min: int | None = None, year_max: int | None = None, limit: int = 25, offset: int = 0,
) -> LotPage:
    where = [
        "removed_at IS NULL",
        "(%(source)s::text IS NULL OR source = %(source)s)",
        "(%(make)s::text IS NULL OR make ILIKE %(make)s)",
        "(%(year_min)s::int IS NULL OR year >= %(year_min)s)",
        "(%(year_max)s::int IS NULL OR year <= %(year_max)s)",
    ]
    q = q.strip()
    if q:
        where.append("""(lot_number = %(q)s OR vin = upper(%(q)s)
                         OR make ILIKE %(prefix)s OR model_group ILIKE %(prefix)s)""")
    clause = " AND ".join(where)
    params = {
        "source": source, "make": make, "year_min": year_min, "year_max": year_max,
        "q": q, "prefix": escape_like(q) + "%", "limit": limit, "offset": offset,
    }
    rows = conn.execute(
        f"""SELECT {COLUMNS} FROM lots WHERE {clause}
            ORDER BY first_seen_at DESC, lot_number LIMIT %(limit)s OFFSET %(offset)s""",
        params,
    ).fetchall()
    total = conn.execute(f"SELECT count(*) AS n FROM lots WHERE {clause}", params).fetchone()
    return LotPage(items=[to_lot(r) for r in rows], total=total["n"])


def get_lot(conn: psycopg.Connection, source: str, lot_number: str) -> Lot | None:
    row = conn.execute(
        f"SELECT {COLUMNS} FROM lots WHERE source = %s AND lot_number = %s "
        "AND removed_at IS NULL", (source, lot_number),
    ).fetchone()
    return to_lot(row) if row else None


def router(Conn: Any) -> APIRouter:
    r = APIRouter(prefix="/lots", tags=["lots"])

    @r.get("")
    def lots(
        conn: Conn, q: str = "", make: str | None = None, source: str | None = None,
        year_min: int | None = None, year_max: int | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 25, offset: Annotated[int, Query(ge=0)] = 0,
    ) -> LotPage:
        """Active lots, the most recently listed first."""
        return list_lots(conn, q=q, make=make, source=source, year_min=year_min,
                         year_max=year_max, limit=limit, offset=offset)

    @r.get("/{source}/{lot_number}")
    def lot(source: str, lot_number: str, conn: Conn) -> Lot:
        found = get_lot(conn, source, lot_number)
        if found is None:
            raise HTTPException(404, "lot not found")
        return found

    return r
