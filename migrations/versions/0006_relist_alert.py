"""A saved lot whose auction ended unsold may be listed again, which its dealer is told.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-09
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        -- Its auction ended on a minimum bid or the seller's approval, so it may be listed
        -- again: the alert job tells the dealer when it is (app/alerts.py).
        ALTER TABLE app.saved_lots ADD COLUMN relist_alert boolean NOT NULL DEFAULT false;
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE app.saved_lots DROP COLUMN relist_alert;")
