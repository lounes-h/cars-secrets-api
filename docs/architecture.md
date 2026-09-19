# Architecture

```
cars-secrets-data-ingest ──► PostgreSQL (`ingest`) ◄── read-only ── cars-secrets-api ◄── cars-secrets-web
```

The API owns no data yet. It reads the `lots` table that the ingest repo fills and migrates.
Every connection is opened with `default_transaction_read_only=on`, so the API cannot write
ingest tables even by mistake.

| Module (`app/`) | Responsibility |
|---|---|
| `config.py` | `Settings` from the environment (`DATABASE_URL`) |
| `db.py` | The psycopg connection pool (read-only, `dict_row`, autocommit) |
| `main.py` | `create_app()`: lifespan opens the pool, `public` and `api` routers, `/api/health` |
| `lots.py` | The `Lot` response model, the SQL, and the `/api/lots` routes |

## Routers

`main.py` has two routers under `/api`: `public` (health) and `api` (everything else). When
dealers get accounts, authentication becomes a dependency of the `api` router; no route moves.

## Lots

`Lot` is an explicit contract: the typed columns the web app needs, `model_group` exposed as
`model`, plus `listing_url` built from `LISTING_URLS[source]` (Copart:
`https://www.copart.com/lot/<n>`, `null` for unknown sources). The `attributes` JSONB that
holds every raw export column is not exposed. Amounts are numbers (JSON), not strings.

Only active lots (`removed_at IS NULL`) are served; a removed lot is a 404. `q` matches a lot
number exactly, a VIN case-insensitively, or a make / model prefix with LIKE wildcards
escaped. Results are ordered `sale_at ASC NULLS LAST, lot_number`.

## When the API needs tables of its own

Saved lots, searches, alerts and users go in a separate `app` schema of the same database,
migrated by this repo's own Alembic history, so the two repos never edit each other's tables.

## Tests

Tests run against a real Postgres (`TEST_DATABASE_URL`, or a testcontainers `postgres:17`).
`tests/schema.sql` is a copy of the ingest's `lots` DDL: the API cannot import the ingest
migrations, so the copy must be refreshed when the ingest adds a column the API exposes.
