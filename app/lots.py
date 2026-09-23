"""Read-only access to auction lots (the ingest repo's `lots` table)."""

from datetime import date, datetime
from typing import Annotated, Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import psycopg
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

# Where a dealer goes to bid; the API never handles bidding itself.
LISTING_URLS = {"copart": "https://www.copart.com/lot/{lot_number}"}

# Copart's public photo list and the host its photos live on. Only these hosts are ever
# fetched or handed out, whatever the ingest stored in `image_url` / `thumbnail_url`.
IMAGE_API_HOST = "inventoryv2.copart.io"
IMAGE_HOST = "cs.copart.com"

# Every canonical column of the ingest's `lots` table except its bookkeeping (attributes,
# row_hash, last_seen_at, removed_at, file ids): the lot page shows all of them.
COLUMNS = """source, lot_number, year, make, model_group AS model, model_detail, trim, vin,
    body_style, color, vehicle_type, engine, cylinders, fuel_type, transmission, drive,
    damage_primary, damage_secondary, title_type, title_state, has_keys,
    odometer, odometer_brand, runs_drives, est_retail_value, repair_cost, high_bid,
    buy_it_now_price, currency, sale_status, sale_date, sale_at, yard_code, yard_name,
    seller_name, location_city, location_state, location_zip, location_country,
    offsite_address, offsite_city, offsite_state, offsite_zip,
    thumbnail_url, image_url, source_created_at, source_updated_at,
    first_seen_at, last_changed_at"""


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
