# Car's Secrets API

Read-only HTTP API over the auction database filled by
[cars-secrets-data-ingest](https://github.com/lounes-h/cars-secrets-data-ingest). It serves
active lots to the Car's Secrets web app. Bidding is not handled here: every lot carries a
`listing_url` to the auction site.

## Run it

```bash
cp .env.example .env      # set DATABASE_URL to the ingest Postgres (127.0.0.1:5433)
uv sync
uv run uvicorn app.main:app_factory --factory --reload   # http://localhost:8000/api/docs
```

## Endpoints

| Route | Returns |
|---|---|
| `GET /api/health` | `{"status": "ok"}` once the database answers |
| `GET /api/lots` | `{items, total}` of active lots, soonest sale first. Filters: `q` (lot number, VIN, make or model prefix), `make`, `source`, `year_min`, `year_max`, `limit` (≤ 100), `offset` |
| `GET /api/lots/{source}/{lot_number}` | One active lot, 404 otherwise |

## Development

```bash
uv run ruff check app tests
uv run pytest             # starts a throwaway Postgres via Docker
TEST_DATABASE_URL=postgresql://… uv run pytest   # reuse a Postgres (its public schema is wiped)
```

See [AGENTS.md](AGENTS.md) for conventions, [docs/architecture.md](docs/architecture.md) and
[docs/security.md](docs/security.md).
