# Architecture

```
cars-secrets-data-ingest ──► PostgreSQL ◄── cars-secrets-api ◄── cars-secrets-web (server actions)
                              public.*  read-only          │
                              app.*     read/write ◄───────┘
```

One database, two schemas. The ingest repo owns `public` (`lots` and friends) and migrates
it; this API only reads it, through a pool whose connections are opened with
`default_transaction_read_only=on`. This API owns `app` (accounts) and migrates it with its
own Alembic history (`migrations/`, version table `app.alembic_version`), through a second,
writable pool. The two never touch each other's tables.

| Module (`app/`) | Responsibility |
|---|---|
| `config.py` | `Settings` from the environment |
| `db.py` | The two psycopg pools (`make_pool(read_only=...)`), `run_migrations` |
| `main.py` | `create_app()`: pools, mailer and HTTP client on `app.state`; routers; `/api/health` |
| `lots.py` | The `Lot` contract, SQL and `/api/lots` routes (read-only pool) |
| `auth.py` | `/api/auth` routes and the `current_user` bearer dependency |
| `accounts.py` | Storage in the `app` schema: users, sessions, tokens, identities, throttle |
| `security.py` | Argon2id hashing, opaque tokens, breached-password check, Turnstile |
| `mail.py` | `Mailer` (SMTP or log) and the account email texts |
| `oauth.py` | Google / Apple OpenID Connect: authorize URL, PKCE, code exchange, ID token check |
| `migrations/` | Alembic, raw SQL in `versions/NNNN_description.py` |

## Routers

`main.py` mounts two routers under `/api`: `public` (health, auth) and `api` (lots, for now
public too). A route for a signed-in dealer declares `user: auth.User`, which reads
`Authorization: Bearer <token>` and resolves the session; that is where the web server puts
the token it keeps in its cookie.

## Lots

`Lot` is an explicit contract: the typed columns the web app needs, `model_group` exposed as
`model`, plus `listing_url` built from `LISTING_URLS[source]` (Copart:
`https://www.copart.com/lot/<n>`, `null` for unknown sources). The `attributes` JSONB that
holds every raw export column is not exposed. Amounts are numbers (JSON), not strings.

Only active lots (`removed_at IS NULL`) are served; a removed lot is a 404. `q` matches a lot
number exactly, a VIN case-insensitively, or a make / model prefix with LIKE wildcards
escaped. Results are ordered `sale_at ASC NULLS LAST, lot_number`.

## Accounts (`app` schema)

| Table | Holds |
|---|---|
| `users` | email (lowercased, unique), names, phone (E.164), Argon2id hash (NULL for social-only), `email_verified_at`, `terms_accepted_at` |
| `identities` | `(provider, subject)` → user, for Google and Apple sign-in |
| `sessions` | sha256 of the bearer token, `remember`, sliding `expires_at`, `revoked_at` |
| `tokens` | sha256 of email verification / password reset tokens, purpose, expiry, `used_at` |
| `oauth_states` | state → provider, PKCE verifier, nonce, `redirect_to`; deleted when used |
| `throttle` | one row per counted event (`login:email:…`, `login:ip:…`) |

## Auth flows

- **Register** → user row (unverified) + `verify_email` token → mail with
  `WEB_URL/verify-email?token=…`. An existing address gets an "account exists" mail instead,
  and the response is identical. **Verify** consumes the token. Login refuses unverified
  accounts with `403 email_unverified`; **resend-verification** issues a new token.
- **Login** counts failures per email and per client IP; past the limits it demands a
  Turnstile token (`429 captcha_required`). Success returns a session token; the web server
  stores it in a cookie and sends it back as a bearer. `remember` picks a 30-day session
  instead of 12 hours idle; every use slides the expiry.
- Mails that anyone can trigger for an address they do not own (verification, the "you already have an account" hint, password reset) go through `auth.mail_allowed`, which caps them per address and per client.
- **Forgot / reset**: a `reset_password` token by mail; reset sets the new hash, marks the
  address verified and revokes every session of the user.
- **Social login**: `start` stores `(state, nonce, PKCE verifier)` and returns the provider's
  authorize URL with redirect URI `WEB_URL/auth/callback/{provider}`. The web app receives the
  provider's redirect (Apple posts it) and calls `callback` with `code` and `state`. The API
  exchanges the code, verifies the ID token (JWKS signature, issuer, audience, expiry,
  nonce), then finds the identity, or links it to the user with that verified email, or
  creates the user. Apple sends the name only on the first sign-in, as JSON in `user`.

## Outbound HTTP and mail

`app.state.http` (one `httpx.Client`) is used for Turnstile, Have I Been Pwned and the OAuth
token endpoints; `app.state.mailer` for email. Tests pass a `MockTransport` and a capturing
mailer through `create_app(settings, mailer=..., http=...)`.

## Tests

Tests run against a real Postgres (`TEST_DATABASE_URL`, or a testcontainers `postgres:17`).
Each test starts from a fresh database: `public` from `tests/schema.sql` (a copy of the
ingest's `lots` DDL, to refresh when the ingest adds a column the API exposes) and `app` from
this repo's migrations.
