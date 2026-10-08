"""Saved lots and searches, their alerts and reminders, and each dealer's alert inbox.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-07
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        -- The lot list's filters under a name (a query string, the web's own parameters).
        CREATE TABLE app.saved_searches (
            id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id         uuid NOT NULL REFERENCES app.users (id) ON DELETE CASCADE,
            name            text NOT NULL,
            query           text NOT NULL DEFAULT '',
            alerts          boolean NOT NULL DEFAULT false,
            -- Lots first seen after this are still to report; reset whenever alerts start
            -- (created, resumed, filters edited), so existing lots never are.
            matched_through timestamptz NOT NULL DEFAULT now(),
            created_at      timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX saved_searches_user ON app.saved_searches (user_id, created_at);

        CREATE TABLE app.saved_lots (
            user_id          uuid NOT NULL REFERENCES app.users (id) ON DELETE CASCADE,
            source           text NOT NULL,
            lot_number       text NOT NULL,
            saved_at         timestamptz NOT NULL DEFAULT now(),
            -- The lot when it was saved: what Saved shows once the auction no longer lists it.
            lot              jsonb NOT NULL,
            change_alerts    boolean NOT NULL DEFAULT false,
            -- What the dealer was last told, which changes are measured against.
            told_sale_at     timestamptz,
            told_sale_date   date,
            told_buy_now     numeric,
            remind_hour      boolean NOT NULL DEFAULT false,
            remind_day       boolean NOT NULL DEFAULT false,
            -- A reminder whose time had already come when it was set never goes out.
            reminders_set_at timestamptz,
            -- The sale time each reminder last went out for: one per schedule.
            hour_sent_for    timestamptz,
            day_sent_for     timestamptz,
            PRIMARY KEY (user_id, source, lot_number)
        );

        CREATE TABLE app.alert_settings (
            user_id         uuid PRIMARY KEY REFERENCES app.users (id) ON DELETE CASCADE,
            email_matches   boolean NOT NULL DEFAULT true,
            email_changes   boolean NOT NULL DEFAULT true,
            email_reminders boolean NOT NULL DEFAULT true,
            twice_daily     boolean NOT NULL DEFAULT false,
            time_zone       text NOT NULL DEFAULT 'UTC',
            locale          text NOT NULL DEFAULT 'en' CHECK (locale IN ('en', 'es')),
            summary_at      timestamptz  -- the last summary slot handled
        );

        -- The inbox: one row per email section, whether or not it was emailed.
        CREATE TABLE app.alerts (
            id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            user_id    uuid NOT NULL REFERENCES app.users (id) ON DELETE CASCADE,
            kind       text NOT NULL CHECK (kind IN ('matches', 'changes', 'reminder')),
            payload    jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            read_at    timestamptz
        );
        CREATE INDEX alerts_user ON app.alerts (user_id, id DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE app.alerts, app.alert_settings, app.saved_lots, app.saved_searches;")
