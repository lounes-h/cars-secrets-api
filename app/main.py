"""Car's Secrets API: accounts and read-only vehicle auction data for the web app."""

import logging
import secrets
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from importlib.metadata import version
from typing import Annotated

import httpx
import psycopg
from fastapi import APIRouter, Depends, FastAPI

from . import auth, lots, preferences
from .config import Settings, get_settings
from .db import make_pool
from .mail import Mailer, make_mailer


def create_app(settings: Settings | None = None, *, mailer: Mailer | None = None,
               http: httpx.Client | None = None) -> FastAPI:
    settings = settings or get_settings()
    pool = make_pool(settings.database_url)                     # synced lots, read-only
    rw_pool = make_pool(settings.database_url, read_only=False)  # the `app` schema
    http = http or httpx.Client()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        pool.open()
        rw_pool.open()
        yield
        rw_pool.close()
        pool.close()
        http.close()

    app = FastAPI(title="Car's Secrets API", version=version("cars-secrets-api"), lifespan=lifespan,
                  docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.settings, app.state.pool, app.state.rw_pool = settings, pool, rw_pool
    app.state.mailer, app.state.http = mailer or make_mailer(settings), http
    app.state.code_key = settings.email_code_secret.encode() or dev_code_key()

    def db() -> Iterator[psycopg.Connection]:
        with pool.connection() as conn:
            yield conn

    Conn = Annotated[psycopg.Connection, Depends(db)]

    public = APIRouter(prefix="/api")
    # Routes for signed-in dealers take `auth.User` (lot preferences); `/api/lots` stays public
    # for now.
    api = APIRouter(prefix="/api")

    @public.get("/health")
    def health(conn: Conn) -> dict[str, str]:
        conn.execute("SELECT 1")
        return {"status": "ok"}

    public.include_router(auth.router)
    api.include_router(lots.router(Conn))
    api.include_router(preferences.router)
    app.include_router(public)
    app.include_router(api)
    return app


def dev_code_key() -> bytes:
    logging.getLogger(__name__).warning(
        "EMAIL_CODE_SECRET is not set: confirmation codes use a random key that dies with this "
        "process and is not shared between workers. Never run like this in production.")
    return secrets.token_bytes(32)


def app_factory() -> FastAPI:  # uvicorn --factory entrypoint
    # uvicorn only configures its own loggers; without this the log mailer's links are lost.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    return create_app()
