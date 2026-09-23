"""Email confirmation by six-digit code instead of a link.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-22
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        -- One live code per user; only its keyed HMAC is stored.
        CREATE TABLE app.email_codes (
            user_id    uuid PRIMARY KEY REFERENCES app.users (id) ON DELETE CASCADE,
            code_hash  bytea NOT NULL,
            attempts   integer NOT NULL DEFAULT 0,
            created_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL
        );

        -- Confirmation links already mailed stop working: a new code is one click away.
        DELETE FROM app.tokens WHERE purpose = 'verify_email';
        ALTER TABLE app.tokens DROP CONSTRAINT tokens_purpose_check,
            ADD CONSTRAINT tokens_purpose_check CHECK (purpose = 'reset_password');
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE app.tokens DROP CONSTRAINT tokens_purpose_check,
            ADD CONSTRAINT tokens_purpose_check
            CHECK (purpose IN ('verify_email', 'reset_password'));
        DROP TABLE app.email_codes;
        """
    )
