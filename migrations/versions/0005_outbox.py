"""Outgoing email, kept until it is sent (app/outbox.py).

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-08
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        -- A row is deleted once sent, or dropped after its last attempt or once it expires.
        CREATE TABLE app.outbox (
            id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            kind       text NOT NULL,              -- verify, reset, existing, password_changed, ...
            to_address text NOT NULL,
            body       bytea NOT NULL,             -- subject, text and HTML, AES-GCM encrypted
            send_after timestamptz NOT NULL,       -- also a sender's lease while it sends
            attempts   integer NOT NULL DEFAULT 0,
            expires_at timestamptz NOT NULL,
            last_error text,
            created_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE app.outbox;")
