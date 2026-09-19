"""Accounts: users, social identities, sessions, one-time tokens, OAuth states, throttling.

Revision ID: 0001
Revises:
Create Date: 2026-09-19
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.users (
            id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            email             text NOT NULL UNIQUE,          -- lowercased, trimmed
            first_name        text NOT NULL,
            last_name         text NOT NULL,
            phone             text,                          -- E.164
            password_hash     text,                          -- NULL for social-only accounts
            email_verified_at timestamptz,
            terms_accepted_at timestamptz,
            created_at        timestamptz NOT NULL DEFAULT now(),
            updated_at        timestamptz NOT NULL DEFAULT now()
        );

        CREATE TABLE app.identities (
            provider   text NOT NULL,                        -- 'google' | 'apple'
            subject    text NOT NULL,                        -- the provider's stable user id
            user_id    uuid NOT NULL REFERENCES app.users (id) ON DELETE CASCADE,
            email      text,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (provider, subject)
        );
        CREATE INDEX identities_user ON app.identities (user_id);

        -- Only the sha256 of the bearer token is stored.
        CREATE TABLE app.sessions (
            token_hash   bytea PRIMARY KEY,
            user_id      uuid NOT NULL REFERENCES app.users (id) ON DELETE CASCADE,
            remember     boolean NOT NULL,
            created_at   timestamptz NOT NULL DEFAULT now(),
            last_seen_at timestamptz NOT NULL DEFAULT now(),
            expires_at   timestamptz NOT NULL,
            revoked_at   timestamptz
        );
        CREATE INDEX sessions_user ON app.sessions (user_id);

        -- Email verification and password reset links; single use.
        CREATE TABLE app.tokens (
            token_hash bytea PRIMARY KEY,
            user_id    uuid NOT NULL REFERENCES app.users (id) ON DELETE CASCADE,
            purpose    text NOT NULL CHECK (purpose IN ('verify_email', 'reset_password')),
            created_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL,
            used_at    timestamptz
        );

        CREATE TABLE app.oauth_states (
            state         text PRIMARY KEY,
            provider      text NOT NULL,
            code_verifier text NOT NULL,
            nonce         text NOT NULL,
            redirect_to   text,
            expires_at    timestamptz NOT NULL
        );

        -- One row per counted event (failed login, registration...) for rate limits.
        CREATE TABLE app.throttle (
            key text NOT NULL,
            at  timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX throttle_key_at ON app.throttle (key, at);
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TABLE app.throttle, app.oauth_states, app.tokens, app.sessions, "
        "app.identities, app.users"
    )
