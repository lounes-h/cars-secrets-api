"""Each dealer's lot list preferences: last filters and how often each one is used.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-03
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE app.lot_preferences (
            user_id    uuid PRIMARY KEY REFERENCES app.users (id) ON DELETE CASCADE,
            filters    text NOT NULL DEFAULT '',
            usage      jsonb NOT NULL DEFAULT '{}',
            updated_at timestamptz NOT NULL DEFAULT now()
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE app.lot_preferences;")
