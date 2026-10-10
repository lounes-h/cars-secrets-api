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
| `main.py` | `create_app()`: pools, mailer, outbox key and HTTP client on `app.state`; routers; `/api/health` |
| `lots.py` | The `Lot` contract, SQL and `/api/lots` routes (read-only pool): search, filters, counts |
| `preferences.py` | `/api/me/lot-preferences`: a dealer's last lot filters and how often each is used |
| `saved.py` | `/api/me/saved-lots`, `/api/me/saved-searches`: what a dealer saved, and what they switched on |
| `alerts.py` | `/api/me/alert-settings`, `/api/me/alerts` (the inbox), and the alert job |
| `alert_mail.py` | The alert emails, in English or Spanish, times in the dealer's zone |
| `outbox.py` | Outgoing email kept until sent: `queue` in the caller's transaction, `flush` sends and retries |
| `auth.py` | `/api/auth` routes and the `current_user` bearer dependency |
| `accounts.py` | Storage in the `app` schema: users, sessions, tokens, identities, throttle; the dealer's language and zone (`seed_settings`, `email_locale`) |
| `security.py` | Argon2id hashing, opaque tokens, breached-password check, Turnstile |
| `mail.py` | `Mailer` (SMTP or log), the blocks every email is built from (plain text and HTML), and the account emails in English or Spanish |
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
| `saved_searches` | per user: a name, the lot list's query string (validated as `LotFilters`), `alerts`, and `matched_through`, after which first-seen lots are still to report |
| `saved_lots` | per user and lot: the lot as saved (shown once the API database drops it), `change_alerts` with the sale time, sale day and Buy now the dealer was last told, the two reminder switches, when they were set and the sale time each last went out for |
| `alert_settings` | per user: email on or off for each kind, once or twice a day, time zone and language (seeded at registration, or by the first alert write of a Google or Apple account, then theirs to change; the language is every email's), the last summary slot |
| `alerts` | the inbox: one row per email section (`matches`, `changes`, `reminder`) with its lots copied in, `read_at` |
| `outbox` | email not yet sent: kind, address, the encrypted body, `send_after`, `attempts`, `expires_at`, `last_error`; deleted once sent or dropped |

## Auth flows

- **Register** → user row (unverified), its settings seeded with the page's `locale` and the
  browser's `time_zone` (both required), + a six-digit code (`auth.send_code`) mailed to the
  address, with no link. An existing address gets an "account exists" mail instead, in its
  owner's language, and the response is identical. **Verify** takes `{email, code}`, confirms the address and returns a
  session like login does. A new code replaces the last one; **resend-verification** and a
  login with the right password on an unconfirmed account both send one (at most one a
  minute), and that login still answers `403 email_unverified`.
- **Login** counts failures per email and per client IP; past the limits it demands a
  Turnstile token (`429 captcha_required`). Success returns a session token; the web server
  stores it in a cookie and sends it back as a bearer. `remember` picks a 30-day session
  instead of 12 hours idle; every use slides the expiry.
- Mails that anyone can trigger for an address they do not own (verification, the "you already have an account" hint, password reset) go through `auth.mail_allowed`, which caps them per address and per client.
- **Forgot / reset**: a `reset_password` token by mail; reset sets the new hash, marks the
  address verified, revokes every session of the user and mails a "password changed" notice
  with a way to reset it again.
- Every account email is in the language `alert_settings` holds (`accounts.email_locale`),
  English for an account without one, and links to the web app's pages in it.
- **Social login**: `start` stores `(state, nonce, PKCE verifier)` and returns the provider's
  authorize URL with redirect URI `WEB_URL/auth/callback/{provider}`. The web app receives the
  provider's redirect (Apple posts it) and calls `callback` with `code` and `state`. The API
  exchanges the code, verifies the ID token (JWKS signature, issuer, audience, expiry,
  nonce), then finds the identity, or links it to the user with that verified email, or
  creates the user. Apple sends the name only on the first sign-in, as JSON in `user`.

## Saved lots, saved searches and alerts

Saving a lot copies it into `saved_lots`; the dealer's list shows each lot as the API database
holds it now (a removed lot stays there until its auction is over) and, once it is purged, as it
was last seen there (the alert job keeps the saved copy, `saved_lots.lot`, in step), marked
unavailable. Saving switches nothing on: change alerts, the 1-hour and 1-day reminders and a
search's alerts each are; only a lot listed again after an auction that may not have sold is news
without them (below). Turning change alerts on starts from what the lot says
then, and so does setting reminders without change alerts (for its sale time and day only);
turning alerts on for a search, or giving it new filters, starts from now, so existing lots are
never news.

`alerts.loop` runs `alerts.tick` every `ALERTS_INTERVAL_SECONDS` (60) in a thread of the API
process, under a Postgres advisory lock (`hashtext('api:alerts')`), so only one process runs it
however many serve requests. One run:

1. Brings each saved lot's copy (`saved_lots.lot`) up to the lot the API database holds, when
   its `last_changed_at` differs, so the last the lot said (its auction, its sale type) stands in
   once it is purged.
2. Clears the reminders of saved lots no longer listed (`removed_at`, or purged), and marks
   those whose auction ended on a minimum bid or the seller's approval (`sale_status` "On
   Minimum Bid" or "On Approval", any case) as `relist_alert`: they may not have sold, and may
   be listed again. A pure sale goes to the highest bid, and a sale type not stated promises
   nothing, so neither is marked.
3. For every listed saved lot, finds the reminders due — the auction's `sale_at` minus 1 hour
   or 1 day has passed, the auction has not, the reminder was set before that time, and it has
   not gone out for this `sale_at` — and, with change alerts on, a different sale time, sale
   day or Buy now price (0 is none) than the dealer was last told; with reminders on, a future
   sale (no time, no day) as last told that now has a date, reported as its date alone ("Auction
   date set: …"), change alerts or not; and, whatever is switched on, a `relist_alert` lot listed
   again, reported as relisted with its new auction alone ("Relisted · Auction: …"), not as a
   move from the auction that ended. Per dealer, in one
   transaction: an inbox entry per reminded lot with that lot's changes folded in, one entry for
   the other changed lots, the new "told" and "sent for" values (a relisted lot's mark cleared),
   and one email.
4. For each dealer with a search's alerts on whose summary time (7:00, or 7:00 and 17:00, in
   their zone) has come since `summary_at`: each such search's lots first seen in
   (`matched_through`, cutoff], still listed and matching now, newest first. The cutoff is the
   last sync (`ingest_sync.synced_at`, or now) minus 30 minutes, since a lot first seen just
   before a sync may not have reached the API database yet; it then waits for the next summary.
   Windows follow one another, so no lot is told twice and an older lot never becomes news
   through an update. A lot matching several searches is listed once, under the first, naming
   the others; each search lists 10 lots and links to the rest. One inbox entry, one email,
   nothing at all when nothing is new.
5. Sends every email that is due (`outbox.flush`), from this run or an earlier failure.

Each dealer's inbox entries, state and email are committed together; a failure is logged,
holds up nobody else, and leaves that dealer's alerts to the next run. Emails are in the
dealer's language, with every time in their zone and named, and link to the web app
(`WEB_URL`, under `/es` in Spanish).

## Outbound HTTP and mail

`app.state.http` (one `httpx.Client`) is used for Turnstile, Have I Been Pwned and the OAuth
token endpoints; `app.state.mailer` for email. Tests pass a `MockTransport` and a capturing
mailer through `create_app(settings, mailer=..., http=...)`.

No route or job calls the mailer itself. Each email is built from `mail.py`'s blocks, which
carry their plain text and their HTML (one table layout with inline styles, grayscale, the
logo from `WEB_URL/logo.png`, everything given escaped), and goes out as both
(`multipart/alternative`). It is stored in `app.outbox` (`outbox.queue`), in the same
transaction as what it reports, then sent: the auth routes send theirs at once, the alert job
everything due after each run. A sender leases its rows for 5 minutes (`FOR UPDATE SKIP
LOCKED`), so no two processes send the same email and one that dies leaves its rows to the
next. A sent email is deleted; a failed one waits 1, 5, 15, then 60 minutes, and is dropped
after the fifth attempt or once it expires: a code with its code (10 minutes), a reset link
with its token (1 hour), a reminder when its auction starts, anything else after a day. A new
confirmation code replaces an unsent one, which no longer works.

## Tests

Tests run against a real Postgres (`TEST_DATABASE_URL`, or a testcontainers `postgres:17`).
Each test starts from a fresh database: `public` from `tests/schema.sql` (a copy of the
tables the ingest worker creates here, to refresh when the ingest adds a column the API
exposes) and `app` from
this repo's migrations.
