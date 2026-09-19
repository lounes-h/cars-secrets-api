# Security

## Surface

- No authentication yet: every route is public and read-only. Routes that will require a
  signed-in dealer already live on the `api` router in `app/main.py`, so auth is added once,
  as a router dependency.
- The API serves only typed lot columns, never the raw `attributes` export data.

## Database

- The API connects to the ingest Postgres with `default_transaction_read_only=on` on every
  connection: any write fails with `ReadOnlySqlTransaction`. A dedicated read-only role
  should be created by the ingest repo before deploying beyond localhost.
- SQL is always parameterized. The only f-string SQL interpolates code constants (the column
  list and the WHERE clause built from fixed fragments), never request data. LIKE patterns
  escape `\`, `%` and `_`.

## Secrets

`DATABASE_URL` holds the database password. `.env` is git-ignored; only `.env.example` is
committed. Nothing is logged from request data.
