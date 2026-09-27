# Car's Secrets API

HTTP API for the Car's Secrets web app: dealer accounts, and read-only access to the auction
lots ingested by [cars-secrets-data-ingest](https://github.com/lounes-h/cars-secrets-data-ingest).
It has its own database: the ingest's worker keeps its active lots in step, and this repo
migrates its accounts.
Bidding is not handled here: every lot carries a `listing_url` to the auction site.

## Run it

### First-time setup

From the repository root, with uv installed and the API's PostgreSQL database running:

```bash
cp .env.example .env      # set DATABASE_URL to the API database (the ingest's api-db, 127.0.0.1:5434)
uv sync
uv run alembic upgrade head                              # creates the `app` schema (accounts)
```

### Start the service

After setup, run this from the repository root:

```bash
uv run uvicorn app.main:app_factory --factory --reload --port 8001
```

The development server runs at http://localhost:8001 and reloads when Python files change.
Open [API docs](http://localhost:8001/api/docs) or check
[health](http://localhost:8001/api/health). Stop it with `Ctrl+C`.

The web app defaults to `http://localhost:8001`. Set `API_URL=http://localhost:8001`
in `../cars-secrets-web/.env.local` if it still overrides the URL with another port,
and restart the web development server after changing it.

The Docker image also listens on port 8001. Publish it with `-p 8001:8001` when
running the container; supply the database and other settings through the environment.

Without `SMTP_HOST`, confirmation codes and reset links are printed in the log. Without
`TURNSTILE_SECRET`, the captcha is not enforced. Google and Apple sign-in answer
`501 provider_not_configured` until their variables are set.

## Moving accounts off the ingest database (once)

Until September 2026 the API shared the ingest's database, so existing accounts live in its
`app` schema. Stop the API, then copy them over from the ingest repo's folder (the containers'
own `pg_dump`/`psql`, so versions match):

```bash
cd ../cars-secrets-data-ingest
docker compose exec -T db pg_dump -U ingest -d ingest --schema=app --no-owner \
  | docker compose exec -T api-db psql -U api -d api -v ON_ERROR_STOP=1 --single-transaction
```

The dump carries `app.alembic_version`, so `uv run alembic upgrade head` then has nothing to do.
Point `DATABASE_URL` at the API database, start the API, sign in to check, and only then drop
the old copy: `docker compose exec db psql -U ingest -d ingest -c 'DROP SCHEMA app CASCADE'`.
For a hosted API database, pipe the same dump into `psql "$DATABASE_URL"`.

## Endpoints

| Route | Returns |
|---|---|
| `GET /api/health` | `{"status": "ok"}` once the database answers |
| `GET /api/lots` | `{items, total}` of active lots, the most recently listed first. Filters: `q` (lot number, VIN, make or model prefix), `make`, `source`, `year_min`, `year_max`, `limit` (≤ 100), `offset` |
| `GET /api/lots/{source}/{lot_number}` | One active lot, 404 otherwise |
| `GET /api/lots/{source}/{lot_number}/images` | `{images: [{thumb, full}]}`: every photo the auction publishes for the lot, empty when it has none or is unreachable |
| `POST /api/auth/register` | Always `{ok: true}`; mails a six-digit confirmation code (or an "account exists" hint) |
| `POST /api/auth/verify-email` | `{email, code, remember?}` → `{token, expires_at}`: confirms the address and signs in; `400 invalid_code`, `429 too_many_attempts` |
| `POST /api/auth/resend-verification` | Always `{ok: true}`; a new code at most once a minute |
| `POST /api/auth/login` | `{token, expires_at}`; `401`, `403 email_unverified` (a new code is mailed), `429 captcha_required` |
| `POST /api/auth/logout`, `GET /api/auth/me` | Bearer session |
| `POST /api/auth/forgot-password` | Always `{ok: true}`; sends the reset link |
| `POST /api/auth/reset-password` | Sets the password from the emailed token, revokes all sessions |
| `POST /api/auth/oauth/{google\|apple}/start` | `{authorize_url, state}` |
| `POST /api/auth/oauth/{google\|apple}/callback` | `{token, expires_at, new_user, redirect_to}` |

## Development

```bash
uv run ruff check app tests migrations
uv run pytest             # starts a throwaway Postgres via Docker
uv run pytest --cov       # the same, failing below the 95 % coverage minimum
TEST_DATABASE_URL=postgresql://… uv run pytest   # reuse a Postgres (its public and app schemas are wiped)
uv run alembic revision -m "describe change"     # new migration under migrations/versions/
```

See [AGENTS.md](AGENTS.md) for conventions, [docs/architecture.md](docs/architecture.md) and
[docs/security.md](docs/security.md).
