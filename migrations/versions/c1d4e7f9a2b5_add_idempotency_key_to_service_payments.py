"""add idempotency_key to service_payments

Revision ID: c1d4e7f9a2b5
Revises: b8d2e4f6a1c3
Create Date: 2026-08-30 00:30:00.000000

"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c1d4e7f9a2b5'
down_revision = 'b8d2e4f6a1c3'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        'service_payments',
        sa.Column('idempotency_key', sa.String(64), nullable=True),
    )
    # Unique across the table; NULLs are not considered equal, so payments
    # created without a key are not rejected. Enforces exactly-once debit for
    # clients that supply an idempotency key.
    op.create_index(
        'uq_service_payments_idempotency_key',
        'service_payments',
        ['idempotency_key'],
        unique=True,
    )


def downgrade():
    op.drop_index('uq_service_payments_idempotency_key', table_name='service_payments')
    op.drop_column('service_payments', 'idempotency_key')
