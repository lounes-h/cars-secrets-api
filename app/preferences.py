"""A dealer's lot list preferences: the filters they last left it with, and how often they set
each one, so the list comes back the way they left it on any browser and their filters move up."""

from typing import Any
from urllib.parse import parse_qs

import psycopg
from fastapi import APIRouter
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from .auth import RwConn, User
from .lots import LotQuery

router = APIRouter(prefix="/me/lot-preferences", tags=["preferences"])


class Preferences(BaseModel):
    # A query string of the lot list ("title=salvage&sort=sale"); the web validates it on use.
    filters: str = ""
    usage: dict[str, int] = {}


class PreferencesIn(BaseModel):
    filters: str = Field(max_length=2000)


def preferences(conn: psycopg.Connection, user_id: Any) -> Preferences:
    row = conn.execute("SELECT filters, usage FROM app.lot_preferences WHERE user_id = %s",
                       (user_id,)).fetchone()
    return Preferences(**row) if row else Preferences()


@router.get("")
def get_preferences(user: User, conn: RwConn) -> Preferences:
    return preferences(conn, user["id"])


@router.put("")
def put_preferences(body: PreferencesIn, user: User, conn: RwConn) -> Preferences:
    """Keeps the dealer's filters. Each list parameter they set or changed counts as one more
    use; anything that is not one is ignored."""
    old = preferences(conn, user["id"])
    before, after = parse_qs(old.filters), parse_qs(body.filters)
    usage = dict(old.usage)
    for key, values in after.items():
        if key in LotQuery.model_fields and before.get(key) != values:
            usage[key] = usage.get(key, 0) + 1
    row = conn.execute(
        """INSERT INTO app.lot_preferences (user_id, filters, usage) VALUES (%s, %s, %s)
           ON CONFLICT (user_id) DO UPDATE
           SET filters = EXCLUDED.filters, usage = EXCLUDED.usage, updated_at = now()
           RETURNING filters, usage""",
        (user["id"], body.filters, Jsonb(usage)),
    ).fetchone()
    return Preferences(**row)
