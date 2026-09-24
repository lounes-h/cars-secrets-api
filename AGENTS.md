# Cars Secrets API

HTTP API for the Car's Secrets dealer platform: dealer accounts (register, email
verification, login, sessions, password reset, Google and Apple sign-in) and read-only access
to the auction lots that [cars-secrets-data-ingest](https://github.com/lounes-h/cars-secrets-data-ingest)
fills. The web app (`cars-secrets-web`) calls it from its server. Bidding is never handled
here: each lot carries a `listing_url` to the auction site.

## Project specification
read `docs/project-specification.md`

## Tech stack

- Python 3.12 (`.python-version`), managed with uv. Package: `app`.
- FastAPI + uvicorn, psycopg 3 (raw SQL, `dict_row`, two pools), pydantic-settings,
  Alembic (the `app` schema only), argon2-cffi, PyJWT, phonenumbers, httpx, stdlib smtplib.
- Tests: pytest + httpx against a real PostgreSQL 17 (testcontainers, or `TEST_DATABASE_URL`);
  pytest-cov with a 95 % minimum.
- Lint: ruff (line length 100, rules `E F I B UP SIM`).
- Hosting is not chosen yet. A Dockerfile exists; there is no compose file.

## Commands

```bash
cp .env.example .env                                    # DATABASE_URL of the API database, …
uv sync                                                 # create .venv with dev dependencies
uv run alembic upgrade head                             # apply this repo's migrations (app schema)
uv run alembic revision -m "describe change"            # new migration file
uv run uvicorn app.main:app_factory --factory --reload  # http://localhost:8000/api/docs
uv run ruff check app tests migrations
uv run pytest                                           # throwaway Postgres via Docker
uv run pytest --cov                                     # the same, failing below 95 % coverage
TEST_DATABASE_URL=postgresql://… uv run pytest          # reuse a Postgres (public + app schemas wiped)
docker build -t cars-secrets-api .
```

Run `uv run ruff check app tests migrations && uv run pytest --cov` before committing. GitHub
Actions runs both on every pull request and on pushes to `main` (`.github/workflows/ci.yml`).

## Architecture rules (important)

See `docs/architecture.md` for details.

- The API has its own database (`DATABASE_URL`), separate from the ingest's, with two
  schemas. cars-secrets-data-ingest's worker owns `public` (`lots`, `ingest_sync`): it keeps
  `lots` in step with the ingest and creates its columns. This API **reads** it through
  `app.state.pool`, whose connections are opened with `default_transaction_read_only=on`.
  Schema changes to `public` are made in the ingest repo (`api_sync.py`), never here.
- `lots` there holds the active lots, plus removed lots until their auction is over (sale time
  + 4 h), with only the served columns: no `attributes`, row hashes, `last_seen_at` or file
  ids.
- This API owns the `app` schema (accounts), written through `app.state.rw_pool`
  (`auth.RwConn`) and migrated by `migrations/versions/NNNN_description.py`: raw SQL via
  `op.execute`, never importing application code, never touching `public`. Never edit a
  migration that has run anywhere shared; add a new one.
- Use the ingest's terminology: a vehicle listing is a **lot**, keyed by `(source, lot_number)`.
- Routes live under `/api` on two routers in `app/main.py`: `public` (health, auth) and `api`
  (lots). A route for a signed-in dealer declares `user: auth.User` (bearer session); do not
  reimplement session checks.
- Auth flows are in `app/auth.py`, storage in `app/accounts.py`, primitives in
  `app/security.py`, `app/mail.py` and `app/oauth.py`. Outbound HTTP goes through
  `app.state.http` and mail through `app.state.mailer`, so tests can substitute both via
  `create_app(settings, mailer=..., http=...)`.
- Responses are explicit pydantic models or plain dicts with a fixed shape (`app/lots.py`,
  `auth.public_user`). Never expose the raw `attributes` JSONB, password hashes or token
  hashes. Amounts serialize as JSON numbers. Errors are
  `{"detail": {"code": "...", "message": "..."}}`; field errors use FastAPI's list shape
  (`auth.field_error`).
- SQL is raw and always parameterized. Only code constants (column lists, fixed WHERE
  fragments) are interpolated; LIKE patterns go through `escape_like`.
- Only active lots (`removed_at IS NULL`) are served.
- Deal arithmetic (cost, profit, margin, max bid) currently lives in the web repo
  (`src/lib/deal.ts`); do not port it here until the user asks.

## Security rules

See `docs/security.md`. Every new unauthenticated write endpoint calls `auth.require_captcha`
and counts attempts in `app.throttle` when it can be abused. Never reveal whether an email is
registered. Never log request bodies, passwords or tokens. Secrets come from `.env`
(git-ignored); `.env.example` lists the names only.

## Migrations

Alembic, raw SQL, `app` schema only (`migrations/`, version table `app.alembic_version`).
`uv run alembic upgrade head` applies them; tests apply them to their throwaway database.

## Tests

Tests sit in `tests/` and run against a real PostgreSQL. `tests/schema.sql` is a copy of the
tables the ingest worker creates in the API database (`api_sync.ensure_schema`); refresh it
when the ingest adds a column the API exposes. Fixtures in `tests/conftest.py`: `conn` (fresh database), `settings`, `mailer`
(captures messages, `last_link(path)` extracts a token, `last_code()` the confirmation code), `http_handler` (set `.fn` to script
Turnstile / HIBP / OAuth responses), `client` (`TestClient` over `create_app` with those
fakes), `insert_lot`.

**Minimum test coverage is 95 %** of `app/` (lines and branches together).
`uv run pytest --cov` fails below it (`fail_under` under `[tool.coverage.report]` in
`pyproject.toml`), and CI runs that command, so a pull request that drops coverage under 95 %
fails. Cover new code in the same change; never lower the threshold or exclude code to get a
change through.

## Unit Test Requirements

Whenever you make any change to application code, you must add or update unit tests that cover the changed behavior.

This applies to:

- New features
- Bug fixes
- Refactors
- Business logic changes
- Validation changes
- Error handling changes
- API behavior changes
- Utility/helper changes
- Data transformation changes

For every code change:

1. Identify the behavior that changed.
2. Add or update unit tests covering that behavior.
3. Include success, failure, and edge cases when relevant.
4. Run the relevant unit tests.
5. Make sure existing related tests still pass.

### Bug fixes

When fixing a bug, add a regression test that:

1. Reproduces the incorrect behavior.
2. Would fail before the fix.
3. Passes after the fix.

### Refactors

If existing tests already fully cover the affected behavior, new tests are not required solely because code was refactored.

However, if coverage is missing or weak, add the necessary tests.

### Exceptions

Unit tests may be skipped only when they are not reasonably applicable, such as:

- Documentation-only changes
- Comment-only changes
- Formatting-only changes
- Static asset changes
- Pure configuration changes with no testable code behavior

Do not skip tests simply because a change is small.

If no unit test is added, explicitly explain why in the final response.

A code change is not considered complete until the relevant unit tests have been added or updated and executed successfully.

## Current state

Working: its own database, whose lots the ingest worker keeps in step (existing accounts are
moved over once, see README); read-only lots (`/api/lots`), accounts (register with terms acceptance and optional
E.164 phone, email confirmation by a six-digit code that also signs the dealer in, login with remember-me and captcha after repeated failures,
logout, me, forgot / reset password), Google and Apple sign-in (built and tested with fakes;
they need credentials in `.env` to be offered). Mail goes to the log until `SMTP_*` is set;
the captcha is enforced only with `TURNSTILE_SECRET`. The only provider in the lot data is
Copart.

Not done: terms of use and privacy policy documents, profile editing, email change, account
deletion, MFA, saved lots, searches, alerts, deal analysis. CORS is not enabled; the web app
calls the API from its server.

## Branching

Create a branch before starting work. The first Git action of a task is creating its branch, not committing.

`main` is the production branch — what ships. That is the whole reason task
work never lands on it directly: a commit there is a release, not a save.

Check where the working tree is first:

```bash
git branch --show-current
```

Always branch off `main`, whatever happens to be checked out. A branch cut from
another task's branch carries that task's unmerged commits into this one's diff
and its pull request.

```bash
git checkout main
git pull
git checkout -b <prefix>/<short-name>
```

Pull `main` before cutting the branch, every time — the `git pull` above is not
optional. Branching from a stale `main` puts commits somebody else already
merged into this task's diff, and turns the eventual merge into a conflict to
resolve rather than a fast-forward.

Do this before making any changes, so there is nothing to carry across.

Already on the branch for this task: stay on it rather than creating another.

Name the branch after the work, using the same prefixes as the commit messages:

- `feat/user-authentication`
- `fix/expired-access-tokens`
- `docs/api-setup-instructions`

Do not commit task work directly to `main`. This is the repository workflow that
the branch rule under "Push Rules" refers to. `main` is what a branch is cut
FROM; what it is pushed TO is always the current branch — see "Push Rules".

## Git Workflow Requirements

For every coding task in this repository, completing the implementation is **not enough**. The task is only considered complete after the changes have been committed and successfully pushed to the configured GitHub repository.

### Required workflow

After making the requested changes:

1. Run the appropriate tests, type checks, linting, formatting, or build commands for the project.
2. Review the Git diff and verify that only intended changes are included.
3. Check `git status` before committing.
4. Stage the relevant files.
5. Create a Git commit with a clear, concise commit message describing the change.
6. Push the commit to the current remote branch on GitHub.
7. Verify that the push succeeded.
8. Open a pull request for the branch, or update the one it already has.
9. Report the commit hash, the branch name and the pull request URL in the final
   response.

Do not stop after editing files, after creating a local commit, or after
pushing without a pull request.

## Commit Rules

Use descriptive commit messages.

Prefer messages such as:

- `feat: add user authentication`
- `fix: handle expired access tokens`
- `refactor: simplify payment validation`
- `test: add coverage for checkout flow`
- `docs: update API setup instructions`
- `chore: update dependencies`

Do not use vague commit messages such as:

- `changes`
- `updates`
- `fix`
- `work`
- `stuff`

Before committing, inspect the diff so accidental files are not included.

## Push Rules

After committing, push the commit to the GitHub remote.

Determine the current branch with:

```bash
git branch --show-current
```

Determine the configured remote with:

```bash
git remote -v
```

Normally push using:

```bash
git push
```

If the branch does not yet have an upstream, use:

```bash
git push -u origin <branch-name>
```

Do not assume the branch is `main`.

Do not create or switch branches unless the task requires it or the existing repository workflow clearly expects it.

## Pull Request Rules

Every push of task work is followed by a pull request. A pushed branch with no
PR is work that reaches nobody: `main` is what ships, and a pull request is the
only way a branch gets there.

Open it with the `gh` CLI, immediately after the push has succeeded:

```bash
gh pr create --base main --head "$(git branch --show-current)" \
  --title "<same prefix and subject as the commit>" \
  --body "<what changed, why, and what validation was run>"
```

- **One pull request per branch.** A push to a branch that already has an open
  PR updates that PR — check with `gh pr view` first and never open a second.
- **The base is always `main`**, never another task's branch.
- The title uses the same prefix as the commits (`feat:`, `fix:`, `docs:`, …).
- The body says what changed, why, and how it was validated.
- **Never merge the pull request.** Opening it is the end of the task; merging
  is the user's decision.
- If `gh` is not authenticated, or the remote refuses the pull request, stop and
  report the exact blocker together with the branch that was pushed — the same
  as a failed push.

Report the pull request URL in the final response.

## Safety Rules

Never use destructive Git commands unless explicitly requested.

Do **not** use:

```bash
git push --force
git push --force-with-lease
git reset --hard
git clean -fd
```

Do not rewrite published Git history.

Do not amend an existing commit unless explicitly requested.

Do not overwrite, revert, discard, or commit unrelated changes made by the user or another agent.

If unrelated uncommitted changes already exist, preserve them and commit only the files or hunks associated with the current task.

## Secrets and Sensitive Files

Before committing, make sure no secrets or sensitive information have been introduced.

Never commit files containing credentials such as:

- `.env`
- API keys
- access tokens
- private keys
- passwords
- cloud credentials
- GitHub tokens
- production secrets

If sensitive information is discovered, do not commit or push it. Remove it from the proposed changes and notify the user.

## Validation Before Commit

Before committing, run the most relevant validation available for the repository.

Examples include:

```bash
npm test
npm run lint
npm run typecheck
npm run build
```

or the equivalent commands for the project's language and tooling.

Use the repository's existing scripts and documentation rather than inventing new validation commands.

If a validation command fails because of the changes you made, fix the problem before committing.

If validation cannot be run because of an environment limitation or an unrelated pre-existing issue, clearly mention that in the final response. This should not prevent committing and pushing otherwise valid requested changes unless doing so would create a broken repository.

## Handling Push Failures

If `git push` fails:

1. Read the Git error.
2. Diagnose whether the issue is authentication, upstream configuration, remote changes, permissions, branch protection, or another problem.
3. Resolve non-destructive issues when possible.
4. Retry the push.

Do not use force push to work around a rejected push.

If the remote branch contains new commits, fetch the latest state and integrate it safely without destroying local or remote work.

If GitHub authentication, repository permissions, branch protection, or another external restriction prevents the push, stop and clearly explain the exact blocker.

A local commit alone does **not** count as successful completion when pushing is possible.

## Documentation Requirements

Documentation is part of the change, not a follow-up. Whenever a change makes an
existing document wrong, incomplete or misleading, update it in the same commit.

Update the document that owns the subject:

| Subject | Document |
|---|---|
| rules, commands, current state | `AGENTS.md` |
| layering, routers, the lot contract | `docs/architecture.md` |
| auth, database access, anything returning data | `docs/security.md` |

Prefer correcting the existing document to adding a new one. A stale sentence is
worse than no sentence, because it is trusted — so delete what stopped being
true rather than appending the new version next to it.

A contract change is the one that reaches furthest: renaming a field or changing
a status code makes the app's documentation wrong as well as this repository's.
Say so in the pull request even when the other repository's change is not yours
to make.

Not every change needs a documentation update: a refactor with no visible
behaviour change, a test-only change or a typo fix usually does not. If nothing
needed updating, say so explicitly in the final response.

## Definition of Done

A coding task is complete only when all applicable items below are true:

- The requested implementation is finished.
- Relevant validation has been run.
- The documentation affected by the change has been updated, or the final
  response states that no document was affected.
- The Git diff has been reviewed.
- Relevant changes have been committed, documentation included.
- The commit has been successfully pushed to GitHub.
- A pull request has been opened for the branch, or the branch's existing pull
  request has been updated by the push.
- The final response identifies the pushed branch, the commit and the pull
  request URL.

At the end of the task, provide a concise summary similar to:

```text
Implemented the requested changes.

Validation:
- Tests passed
- Lint passed

Docs:
- Updated docs/security.md  (or: no documentation affected)

Git:
- Branch: feature/example
- Commit: a1b2c3d
- Pushed to: origin/feature/example
- Pull request: https://github.com/<owner>/<repo>/pull/123
```

Unless explicitly instructed otherwise, always update the affected
documentation, commit, push and open a pull request for completed coding
changes.
