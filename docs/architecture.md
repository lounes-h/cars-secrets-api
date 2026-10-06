# Architecture

```
ingest database ── ingest worker ──► API database ◄── cars-secrets-api ◄── cars-secrets-web
 (every lot ever,    (after each file   public.*  read-only       │        (server actions)
  full history)       + every 15 min)   app.*     read/write ◄────┘
```

The API has its own database, separate from the ingest's, with two schemas. The ingest
worker owns `public`: it creates `lots` and `ingest_sync` and keeps `lots` in step with the
ingest database, copying only the lots that changed, in one transaction per sync. `lots`
holds every active lot, plus removed lots until their auction is over (sale time + 4 h), with
only the served columns. So it stays around 200k rows instead of growing with every lot ever
seen. This API only reads `public`, through a pool whose connections are opened with
`default_transaction_read_only=on`. It owns `app` (accounts) and migrates it with its own
Alembic history (`migrations/`, version table `app.alembic_version`), through a second,
writable pool. The two never touch each other's tables.

| Module (`app/`) | Responsibility |
|---|---|
| `config.py` | `Settings` from the environment |
| `db.py` | The two psycopg pools (`make_pool(read_only=...)`), `run_migrations` |
| `main.py` | `create_app()`: pools, mailer and HTTP client on `app.state`; routers; `/api/health` |
| `lots.py` | The `Lot` contract, SQL and `/api/lots` routes (read-only pool): search, filters, counts |
| `preferences.py` | `/api/me/lot-preferences`: a dealer's last lot filters and how often each is used |
| `auth.py` | `/api/auth` routes and the `current_user` bearer dependency |
| `accounts.py` | Storage in the `app` schema: users, sessions, tokens, identities, throttle |
| `security.py` | Argon2id hashing, opaque tokens, breached-password check, Turnstile |
| `mail.py` | `Mailer` (SMTP or log) and the account email texts |
| `oauth.py` | Google / Apple OpenID Connect: authorize URL, PKCE, code exchange, ID token check |
| `migrations/` | Alembic, raw SQL in `versions/NNNN_description.py` |

## Routers

`main.py` mounts two routers under `/api`: `public` (health, auth) and `api` (lots, for now
public too, and lot preferences). A route for a signed-in dealer declares `user: auth.User`, which reads
`Authorization: Bearer <token>` and resolves the session; that is where the web server puts
the token it keeps in its cookie.

## Lots

`Lot` is an explicit contract: every column of the synced `lots` table, with
`model_group` exposed as `model`, plus `listing_url` built from `LISTING_URLS[source]`
(Copart: `https://www.copart.com/lot/<n>`, `null` for unknown sources). The lot page shows
all of them, so a column the ingest adds belongs in `COLUMNS` and `Lot` too. The ingest's
bookkeeping (`attributes`, the raw export columns; `row_hash`; `last_seen_at`; file ids) never
reaches the API database, and `removed_at` is not exposed. Amounts are numbers (JSON), not
strings. `predicted_final_price` is the ingest's XGBoost estimate of the final auction price
(`pricing.py` there), `null` until a model prices the lot. Its model is trained on generated sold
prices for now, so treat it as an estimate: the web app labels it so.

`/{source}/{lot_number}/images` lists a lot's photos. Copart's public photo list is fetched
through `app.state.http`, always from the URL stored for that lot (`image_url`) and never from
anything the caller sends, and only URLs on `cs.copart.com` are returned. When that list is
unavailable — some lots 404 there although copart.com shows photos — the export's thumbnail
stands in, with its full-size twin (`_thb` -> `_ful`). Photos are best effort: an upstream
failure is an empty list, not an error. Nothing is cached, so one lot page is one upstream
call; add a TTL cache if views grow.

Only active lots (`removed_at IS NULL`) are served; a removed lot is a 404. Each word of `q`
must match: a lot number exactly, a VIN case-insensitively, a make or model prefix, or part of
the trim, model detail, colour, body, fuel or drive, with LIKE wildcards escaped. Cars
(vehicle type `V`) always come first, whatever the order (`CARS_FIRST`), then every other type.
Within each, results are ordered `first_seen_at DESC, lot_number` unless `sort` says otherwise:
the lots the ingest saw most recently come first, which is what a dealer wants to look at.

The filters (`LotFilters`) read the lots through `LOTS`, a subquery that adds each list
filter's value as a column (`f_title`, `f_run`, ...), in the words the web shows rather than
Copart's codes. A list filter matches any of its values; filters narrow together. A title's
group (clean, salvage, non-repairable) comes from `copart_titles.json`, a copy of the web's
`src/lib/copart-titles.json` keyed "STATE CODE", passed as one JSONB parameter so the lookup is
per row rather than a join; refresh the copy when the web's file changes. Copart's "DEFAULT"
run status reads as not verified, as on the lot page. Sale windows (`today`, `tomorrow`, the rest
of `this_week` to Sunday, `next_week`) start at midnight in the dealer's zone `tz` (UTC when
unknown); `future` is a lot with neither a sale time nor a sale day, the lot page's "Future
sale". `model` also matches Copart's longer groups of that model ("RAV4" is "RAV4 BASE" too).

`/counts` says how many lots each option of the named filters would show, leaving each
filter's own choice out of its counts so a second value can still be added. It is one scan per
filter (~35 ms at 150k lots), so the web asks for a list's counts when the dealer opens it
rather than with every page; only the 100 most common makes are counted, and models only within
a make. No index serves these: most filters match a large share of the lots, where a sequential
scan wins anyway.

`/activity?tz=` says how many active lots were added (`first_seen_at`) and how many seen
before that changed (`last_changed_at`), today and over the last 3 and 7 days, each window
starting at midnight in the dealer's zone (an unknown zone is UTC). A lot added in a window is
not also counted as updated in it. One scan of the active lots (~35 ms at 150k), for the
summary at the top of the web's lot list.

`/interpret` reads a search the way a dealer means it. One word that is a lot number or a VIN
of exactly one active lot names that lot. Otherwise years ("2019", "2018-2021"), a make (up to
three words, ignoring case and punctuation: "mercedes benz" is "MERCEDES-BENZ") and a model are
taken out, and the other words are left as `q`. Only makes with at least 10 active lots count,
since rarer ones are misspellings ("TOYT") or words a dealer means otherwise ("WHITE" trucks).
A model alone names its make when that make holds at least 90 % of the model's lots ("camry" is
a Toyota, but "1500" is Ram's and GMC's, so it stays a word).

## Accounts (`app` schema)

| Table | Holds |
|---|---|
| `users` | email (lowercased, unique), names, phone (E.164), Argon2id hash (NULL for social-only), `email_verified_at`, `terms_accepted_at` |
| `identities` | `(provider, subject)` → user, for Google and Apple sign-in |
| `sessions` | sha256 of the bearer token, `remember`, sliding `expires_at`, `revoked_at` |
| `tokens` | sha256 of password reset tokens, purpose, expiry, `used_at` |
| `email_codes` | one live confirmation code per user: HMAC of the code, `attempts`, `created_at`, expiry |
| `oauth_states` | state → provider, PKCE verifier, nonce, `redirect_to`; deleted when used |
| `throttle` | one row per counted event (`login:email:…`, `login:ip:…`, `code:email:…`, `code:ip:…`, `mail:…`) |
| `lot_preferences` | per user: the lot list's last `filters` (a query string the web validates on use) and `usage`, how many times each list parameter was set or changed |

## Auth flows

- **Register** → user row (unverified) + a six-digit code (`auth.send_code`) mailed to the
  address, with no link. An existing address gets an "account exists" mail instead, and the
  response is identical. **Verify** takes `{email, code}`, confirms the address and returns a
  session like login does. A new code replaces the last one; **resend-verification** and a
  login with the right password on an unconfirmed account both send one (at most one a
  minute), and that login still answers `403 email_unverified`.
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
tables the ingest worker creates here, to refresh when the ingest adds a column the API
exposes) and `app` from
this repo's migrations.
