# Accounts and authentication: design

Date: 2026-09-19. Covers two repositories: `cars-secrets-api` (this one) and
`cars-secrets-web`. Each ships in its own pull request; the API goes first.

## Goal

Dealers can register, verify their email, sign in (password, Google or Apple), stay signed in
("remember me") and reset a forgotten password by email. Every account lives in the API, so
later features (saved lots, searches, alerts) can rely on `Authorization: Bearer`.

## Decisions

| Topic | Decision |
|---|---|
| Owner of accounts | The API. Tables in a new `app` schema of the ingest Postgres, migrated by this repo's own Alembic history (`alembic_version` table lives in `app`). Ingest tables are never touched. |
| Web ↔ API | Next.js server actions call the API from the server and keep the session token in an httpOnly cookie on the web domain. The browser never talks to the API (no CORS). |
| Social login | Google and Apple through OpenID Connect authorization-code flow with PKCE and a nonce. Built and tested with fakes; the endpoints answer `501 provider_not_configured` until the provider's environment variables are set. |
| Captcha | Cloudflare Turnstile. Required on register, resend-verification and forgot-password; on login only after repeated failures. Verified server-side; skipped when `TURNSTILE_SECRET` is unset (development, tests). |
| Email | Standard-library SMTP with STARTTLS, configured by `SMTP_*`. Without `SMTP_HOST`, the message (including its link) is written to the API log. |
| Email verification | Required before the first password login. Social logins with a provider-verified email are verified on creation. |
| Remember me | Checked: session valid 30 days, extended on use, cookie persisted. Unchecked: cookie expires with the browser, server session after 12 h of inactivity. |
| Passwords | 10–128 characters, any content. Rejected if found in Have I Been Pwned (k-anonymity range API, 5 hash characters sent; fails open on network error). Hashed with Argon2id. |
| Phone | Optional, validated with `phonenumbers`, stored in E.164. |
| Terms | The register form has a required "I accept the Terms of Use and Privacy Policy" checkbox. The documents do not exist yet; the API records `terms_accepted_at`. |

## Data model (`app` schema)

```
users          id uuid pk, email text unique (lowercased), first_name, last_name,
               phone text null (E.164), password_hash text null (social-only accounts),
               email_verified_at, terms_accepted_at, created_at, updated_at
identities     provider text ('google'|'apple'), subject text, user_id fk, email text,
               created_at; pk (provider, subject)
sessions       token_hash bytea pk (sha256 of the token), user_id fk, remember bool,
               created_at, last_seen_at, expires_at, revoked_at
tokens         token_hash bytea pk, user_id fk, purpose text ('verify_email'|'reset_password'),
               expires_at, used_at, created_at
oauth_states   state text pk, provider, code_verifier, nonce, redirect_to, expires_at
throttle       key text, at timestamptz; index (key, at). Rows older than the window are
               deleted opportunistically.
```

Only hashes of session, verification and reset tokens are stored. Tokens are 32 random bytes,
URL-safe base64.

## API (`/api/auth/…`, JSON, on the `public` router, rate-limited)

| Route | Body | Response |
|---|---|---|
| `POST register` | first_name, last_name, email, phone?, password, accept_terms (must be true), captcha_token | `200 {ok: true}` always. Existing email: sends a "you already have an account" mail instead of creating one. Validation errors: `422` with per-field messages (password rules, phone format, terms). |
| `POST verify-email` | token | `200` (marks verified), `400 invalid_token` |
| `POST resend-verification` | email, captcha_token | `200` always |
| `POST login` | email, password, remember, captcha_token? | `200 {token, expires_at}`; `401 invalid_credentials`; `403 email_unverified`; `429 captcha_required` after 3 failures per email or 10 per IP in 15 min, then a valid captcha token lifts it |
| `POST logout` | (bearer) | `204`, session revoked |
| `GET me` | (bearer) | `200 {id, email, first_name, last_name, phone, email_verified}` |
| `POST forgot-password` | email, captcha_token | `200` always; mail with reset link when the account exists |
| `POST reset-password` | token, password | `200`; all sessions of the user revoked; `400 invalid_token`; `422` password rules |
| `POST oauth/{provider}/start` | redirect_to? | `200 {authorize_url, state}`; `501 provider_not_configured` |
| `POST oauth/{provider}/callback` | code, state, user? (Apple sends the name once, as JSON) | `200 {token, expires_at, new_user}`; `400 invalid_state` / `invalid_token` |

Bearer sessions: `Authorization: Bearer <token>`, looked up by sha256. Sliding expiry for
remember sessions (extend when more than a day has passed). A dependency `current_user` is
attached to the `api` router; `/api/lots` stays public.

Client IP for throttling: taken from `X-Client-IP` only when the request carries
`X-Internal-Key` equal to `INTERNAL_API_KEY`; otherwise the peer address. The web server
sets both.

Error responses use `{"detail": {"code": "...", "message": "..."}}`; field errors from
FastAPI validation keep their default shape.

## Web (`cars-secrets-web`, unstyled)

Pages: `/register`, `/login`, `/forgot-password`, `/reset-password?token=`,
`/verify-email?token=` (a confirm button; the GET never consumes the token, so mail scanners
cannot), `/account` (signed-in user and a logout button), route handler
`/auth/callback/[provider]` (GET for Google, POST for Apple's `form_post`).

- Server actions in one module validate input, call the API through a small server-only
  client (`API_URL`, `API_INTERNAL_KEY`, forwards the visitor IP), and set the `cs_session`
  cookie: httpOnly, `SameSite=Lax`, `Secure` outside development, `maxAge` 30 days when
  remember is checked, none otherwise.
- Client components only where interaction needs it: password show/hide, the phone field
  (country select with emoji flag and dial code; list generated from phone-number metadata,
  names via `Intl.DisplayNames`), the Turnstile widget (renders nothing without
  `NEXT_PUBLIC_TURNSTILE_SITE_KEY`).
- Social buttons above both forms; the action asks the API for the authorize URL, stores
  `state` in a short-lived httpOnly cookie (`SameSite=None; Secure` because Apple posts the
  callback cross-site) and redirects. The callback route compares the state cookie, calls the
  API and sets the session cookie.
- Route registry gains `noindex?: true` so reset and verify pages are registered (the registry
  test requires it) but excluded from the sitemap and marked noindex.

## Security summary

Argon2id hashes; generic responses on register / forgot-password / login; constant-time
comparisons; tokens hashed at rest and single use; reset revokes all sessions; email
lowercased and trimmed; throttling in the database; captcha on unauthenticated
write endpoints; OIDC ID tokens verified (signature via JWKS, issuer, audience, expiry,
nonce); PKCE; Apple client secret is an ES256 JWT built from the configured private key;
`.env` never committed.

## Out of scope

Terms and privacy documents, MFA, account deletion, profile editing, email change, SMS,
password-less login, admin roles.

## Testing

API: pytest on real Postgres with the Alembic migration applied; `Mailer` protocol with a
capturing fake; captcha and breach check disabled by settings; OAuth exchanged against
`httpx.MockTransport` with a locally generated signing key. Web: vitest for actions (mocked
fetch), components in jsdom, country list, cookie options.
