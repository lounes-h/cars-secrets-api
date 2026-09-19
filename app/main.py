"""Car's Secrets API: read-only vehicle auction data for the web app."""

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, FastAPI

from . import lots
from .config import Settings, get_settings
from .db import make_pool


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    pool = make_pool(settings.database_url)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        pool.open()
        yield
        pool.close()

    app = FastAPI(title="Car's Secrets API", lifespan=lifespan, docs_url="/api/docs",
                  openapi_url="/api/openapi.json")
    app.state.pool = pool

    def db() -> Iterator[psycopg.Connection]:
        with pool.connection() as conn:
            yield conn

    Conn = Annotated[psycopg.Connection, Depends(db)]

    public = APIRouter(prefix="/api")
    # Everything that will need a signed-in dealer goes here; auth is added as a dependency
    # of this router, never per route.
    api = APIRouter(prefix="/api")

    @public.get("/health")
    def health(conn: Conn) -> dict[str, str]:
        conn.execute("SELECT 1")
        return {"status": "ok"}

    api.include_router(lots.router(Conn))
    app.include_router(public)
    app.include_router(api)
    return app


def app_factory() -> FastAPI:  # uvicorn --factory entrypoint
    return create_app()
