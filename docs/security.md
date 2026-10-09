# Security

## Surface

- `/api/lots` and `/api/health` are public and read-only. `/api/auth/*` is public by nature;
  `logout` and `me` need a bearer session. Routes for signed-in dealers declare
  `user: auth.User`; `/api/me/lot-preferences` reads and writes only the caller's own row.
- `/api/me/saved-lots`, `/api/me/saved-searches`, `/api/me/alerts` and `/api/me/alert-settings`
  read and write only the caller's rows: every statement filters on the session's user, so
  another dealer's search id or alert id changes nothing (a 404, or no row marked read). Saved
  searches are validated as `LotFilters` and capped (name 80 characters, query 2000); an account
  keeps at most 50 searches and 500 lots (`409 limit_reached`).
- Alert emails go only to a confirmed address, the account's own; they carry no token and link
  only to `WEB_URL`. The saved search's name is the dealer's own text, sent back to them as
  text; every value an email's HTML shows, the auction's included, is escaped.
- Email waiting in `app.outbox` is encrypted with AES-GCM under a key derived from
  `EMAIL_CODE_SECRET` and bound to its address, so the table never shows a live code or reset
  link, and a body cannot be moved to another address. A sent email is deleted, and one that
  cannot be sent is dropped with its code or link, at the latest. Logs name an email by its id
  and kind, never its address.
- Lot filters are typed (`LotFilters`): enumerated values, at most 50 values per list, capped
  text. Their SQL is built from code constants and every value is a parameter; the stored
  preference string is capped at 2000 characters and only known parameters are counted.
- Only typed lot columns are served, never the raw `attributes` export data. User responses
  carry id, email, names, phone and the verified flag; never hashes or tokens.
- Register, resend-verification and forgot-password answer the same `200 {"ok": true}` whether
  or not the address exists. Verify-email answers the same `400 invalid_code` for an unknown
  address, a confirmed one, a missing or expired code and wrong digits. Login answers a generic `401` for unknown addresses and wrong
  passwords, after verifying against a dummy hash so timing does not differ.

## Sessions

Bearer tokens of 32 random bytes; only their sha256 is stored (`app.sessions`). A "remember
me" session lasts 30 days, otherwise 12 hours of inactivity; every use slides the expiry.
Logout revokes the session; a password reset revokes all of the user's sessions.

## Passwords

Argon2id (argon2-cffi defaults). 8 to 128 characters, with at least one that is not a letter
or a digit — the same rule the web form applies, so a password it accepts is never refused
here for its shape. On registration
and reset the password is checked against Have I Been Pwned by k-anonymity: only the first
five characters of its SHA-1 leave the server, and a network failure lets the password through
(`CHECK_BREACHED_PASSWORDS=false` disables the check).

## Throttling and captcha

Failed logins are counted per email (3) and per client IP (10) over 15 minutes in
`app.throttle`; past a limit the login needs a valid Cloudflare Turnstile token
(`429 captcha_required`). Register, resend-verification and forgot-password always require a
captcha token when `TURNSTILE_SECRET` is set. Without it, nothing is enforced: set it in
production.

Those three endpoints mail an address the caller names, and the caller need not own it, so
each one is also capped: `MAILS_PER_EMAIL` (3) per address and `MAILS_PER_IP` (10) per client
over the same window, counted only when a mail actually goes out (`auth.mail_allowed`). Past
the cap nothing is sent and the answer is the unchanged `200 {"ok": true}`, so a capped
address is indistinguishable from an unknown one and nobody's inbox can be used as a weapon.

The client IP is `X-Client-IP` only when the request also carries `X-Internal-Key` equal to
`INTERNAL_API_KEY` (compared in constant time); otherwise the TCP peer. The web server sets
both, so throttling applies to visitors and not to the web server's address.

## Email confirmation codes

- Six digits from `secrets.randbelow` (a million values), valid 10 minutes, single use; one
  live code per user, and a new one replaces the last with a fresh attempt count.
- Stored as HMAC-SHA256 over `user_id:code` keyed with `EMAIL_CODE_SECRET`: without the key a
  leaked table cannot be brute-forced offline, and a code is bound to its account. Without the
  variable each process makes a random key (development only). Compared in constant time, and
  hashed for unknown addresses too. A pending account still costs one more `UPDATE` than an
  unknown address, a timing difference far below network noise; the answer is identical.
- Guessing is bounded three ways: 5 guesses per code (spent by an atomic `UPDATE`, so parallel
  requests cannot share one), then 10 wrong codes per address and 20 per client over 15
  minutes, after which every guess gets `429 too_many_attempts`. New codes are capped by the
  mail limits below and by one a minute per account. That leaves an attacker about 10 guesses
  per address per 15 minutes against a million values. The price of the per-address lock is
  that someone can hold back a stranger's confirmation for 15 minutes; it cannot take over
  the account.
- A right code signs the dealer in (12 hours idle, or 30 days with `remember`): it proves the inbox, which is all a
  password reset asks for. A login with the right password on an unconfirmed account mails a
  new code, so the owner is never stuck.
- The code is in the mail's body, never its subject (lock-screen notifications), and the mail
  holds no link, so scanners that open links cannot use it.

## Reset tokens

Reset tokens (1 h) are random, stored hashed, single use, bound to a purpose. Links point at
the web app, whose page needs a click before consuming a token, so mail scanners that
prefetch links do not. A reset mails the account a "password changed" notice with a way to
reset it again, so a takeover does not go unnoticed.

## Social login

Authorization-code flow with PKCE (S256) and a nonce; the state is single use, provider-bound
and expires after 10 minutes. The ID token's signature is verified against the provider's
JWKS, plus issuer, audience, expiry and nonce. An identity is linked to an existing account
only when the provider says the email is verified. Apple's client secret is an ES256 JWT built
from `APPLE_PRIVATE_KEY` at each exchange. Only `google` and `apple` are accepted as provider
names.

## Database

- The API has its own database. Its `public` tables (lots, filled by the ingest worker) are
  read through connections opened with `default_transaction_read_only=on`; the `app` schema is
  written through a separate pool, which saved lots and alerts also use to read `lots` in the
  same query as their own tables (they never write `public`). The ingest database is never
  reachable from the API, and
  the lots copy carries no raw export data (`attributes`). Before deploying beyond localhost,
  give the API a role that can only read `public` and write `app`, separate from the ingest
  worker's role, and require TLS (`sslmode=require`) to a hosted database.
- SQL is always parameterized. The only f-string SQL interpolates code constants (column lists
  and fixed WHERE fragments). LIKE patterns escape `\`, `%` and `_`.

## Secrets

`DATABASE_URL`, `INTERNAL_API_KEY`, `EMAIL_CODE_SECRET`, `TURNSTILE_SECRET`, `SMTP_PASSWORD`, `GOOGLE_CLIENT_SECRET`
and `APPLE_PRIVATE_KEY` live in `.env`, which is git-ignored; `.env.example` holds the names
only. `EMAIL_CODE_SECRET` keys both the stored confirmation codes and the outbox, so changing it
voids the live codes and drops the email still waiting. Request bodies and tokens are never
logged; the log mailer (development only) prints confirmation codes and reset links.
