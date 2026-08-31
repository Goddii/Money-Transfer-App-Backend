"""add daraja_throttle table for cross-process rate limiting

Revision ID: a1b2c3d4e5f6
Revises: e5c7d9b1a3f2
Create Date: 2026-08-31 00:00:00.000000

All outbound Daraja traffic (OAuth, STK Push, STK Query, reconciliation,
callback, sweeper, admin) is funnelled through one shared rate limiter so a
single Render instance or Gunicorn worker cannot exhaust Daraja's
application-wide quota on its own. On PostgreSQL the limiter is a database-backed
token bucket shared by every process/instance (this table); on other backends an
in-process token bucket is used instead.

The same row also carries the global upstream cooldown (403/429/5xx) so one
upstream incident does not inflate every pending transaction's attempt counter.

* ``tokens`` / ``capacity`` / ``refill_per_sec`` implement the token bucket.
* ``last_refill`` is the UTC timestamp of the last refill.
* ``cooldown_until`` (nullable) is when the global upstream cooldown ends.

The single row (id = 1) is created lazily by the application on first use via
``INSERT ... ON CONFLICT DO NOTHING``, so this migration only needs to create
the schema. On SQLite the table is created for portability but the in-process
bucket is what actually runs.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f6'
down_revision = 'e5c7d9b1a3f2'
branch_labels = None
depends_on = None


def _timestamp_type():
    if op.get_context().dialect.name == "postgresql":
        return sa.TIMESTAMP(timezone=True)
    return sa.DateTime()


def upgrade():
    op.create_table(
        'daraja_throttle',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column('tokens', sa.Numeric(precision=12, scale=4), nullable=False),
        sa.Column('last_refill', _timestamp_type(), nullable=False),
        sa.Column('capacity', sa.Numeric(precision=12, scale=4), nullable=False),
        sa.Column(
            'refill_per_sec', sa.Numeric(precision=18, scale=6), nullable=False
        ),
        sa.Column('cooldown_until', _timestamp_type(), nullable=True),
        sa.Column('cooldown_reason', sa.String(length=50), nullable=True),
    )


def downgrade():
    op.drop_table('daraja_throttle')
