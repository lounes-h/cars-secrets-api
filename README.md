# Car's Secrets API

HTTP API for the Car's Secrets web app: dealer accounts, and read-only access to the auction
lots ingested by [cars-secrets-data-ingest](https://github.com/lounes-h/cars-secrets-data-ingest).
Bidding is not handled here: every lot carries a `listing_url` to the auction site.

## Run it

```bash
cp .env.example .env      # set DATABASE_URL to the ingest Postgres (127.0.0.1:5433)
uv sync
uv run alembic upgrade head                              # creates the `app` schema (accounts)
uv run uvicorn app.main:app_factory --factory --reload   # http://localhost:8000/api/docs
```

Without `SMTP_HOST`, verification and reset links are printed in the log. Without
`TURNSTILE_SECRET`, the captcha is not enforced. Google and Apple sign-in answer
`501 provider_not_configured` until their variables are set.

## Endpoints

| Route | Returns |
|---|---|
| `GET /api/health` | `{"status": "ok"}` once the database answers |
| `GET /api/lots` | `{items, total}` of active lots, the most recently listed first. Filters: `q` (lot number, VIN, make or model prefix), `make`, `source`, `year_min`, `year_max`, `limit` (≤ 100), `offset` |
| `GET /api/lots/{source}/{lot_number}` | One active lot, 404 otherwise |
| `POST /api/auth/register` | Always `{ok: true}`; sends a verification email (or an "account exists" hint) |
| `POST /api/auth/verify-email` | Marks the address verified from the emailed token |
| `POST /api/auth/resend-verification` | Always `{ok: true}` |
| `POST /api/auth/login` | `{token, expires_at}`; `401`, `403 email_unverified`, `429 captcha_required` |
| `POST /api/auth/logout`, `GET /api/auth/me` | Bearer session |
| `POST /api/auth/forgot-password` | Always `{ok: true}`; sends the reset link |
| `POST /api/auth/reset-password` | Sets the password from the emailed token, revokes all sessions |
| `POST /api/auth/oauth/{google\|apple}/start` | `{authorize_url, state}` |
| `POST /api/auth/oauth/{google\|apple}/callback` | `{token, expires_at, new_user, redirect_to}` |

## Development

```bash
uv run ruff check app tests migrations
uv run pytest             # starts a throwaway Postgres via Docker
TEST_DATABASE_URL=postgresql://… uv run pytest   # reuse a Postgres (its public and app schemas are wiped)
uv run alembic revision -m "describe change"     # new migration under migrations/versions/
```

See [AGENTS.md](AGENTS.md) for conventions, [docs/architecture.md](docs/architecture.md) and
[docs/security.md](docs/security.md).
