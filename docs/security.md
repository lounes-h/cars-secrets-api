# Security

## Surface

- `/api/lots` and `/api/health` are public and read-only. `/api/auth/*` is public by nature;
  `logout` and `me` need a bearer session. Routes for signed-in dealers declare
  `user: auth.User`.
- Only typed lot columns are served, never the raw `attributes` export data. User responses
  carry id, email, names, phone and the verified flag; never hashes or tokens.
- Register, resend-verification and forgot-password answer the same `200 {"ok": true}` whether
  or not the address exists. Login answers a generic `401` for unknown addresses and wrong
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

## Email tokens

Verification (24 h) and reset (1 h) tokens are random, stored hashed, single use, bound to a
purpose. Links point at the web app, whose pages need a click before consuming a token, so
mail scanners that prefetch links do not.

## Social login

Authorization-code flow with PKCE (S256) and a nonce; the state is single use, provider-bound
and expires after 10 minutes. The ID token's signature is verified against the provider's
JWKS, plus issuer, audience, expiry and nonce. An identity is linked to an existing account
only when the provider says the email is verified. Apple's client secret is an ES256 JWT built
from `APPLE_PRIVATE_KEY` at each exchange. Only `google` and `apple` are accepted as provider
names.

## Database

- Ingest tables are read through connections opened with `default_transaction_read_only=on`;
  the `app` schema is written through a separate pool. A dedicated database role for the API
  should still be created by the ingest repo before deploying beyond localhost.
- SQL is always parameterized. The only f-string SQL interpolates code constants (column lists
  and fixed WHERE fragments). LIKE patterns escape `\`, `%` and `_`.

## Secrets

`DATABASE_URL`, `INTERNAL_API_KEY`, `TURNSTILE_SECRET`, `SMTP_PASSWORD`, `GOOGLE_CLIENT_SECRET`
and `APPLE_PRIVATE_KEY` live in `.env`, which is git-ignored; `.env.example` holds the names
only. Request bodies and tokens are never logged; the log mailer (development only) prints
verification and reset links.
