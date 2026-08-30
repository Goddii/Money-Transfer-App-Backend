"""widen mpesa_transactions.status to VARCHAR(50)

Revision ID: e5c7d9b1a3f2
Revises: c1d4e7f9a2b5
Create Date: 2026-08-30 14:00:00.000000

The ``MpesaTransactionStatus.RECONCILIATION_PENDING`` value
(``"ReconciliationPending"``) is 21 characters long, but the original
``mpesa_transactions.status`` column was created as ``VARCHAR(20)``. On
PostgreSQL this raises ``StringDataRightTruncation`` whenever an inconclusive
M-Pesa callback or the recovery/sweeper flow attempts to persist that status,
returning a 500 instead of recording the recoverable deposit. SQLite never
enforces the length, so the bug only surfaced on the deployed PostgreSQL
backend.

This migration widens the column to ``VARCHAR(50)``, which leaves generous
headroom for future status values while remaining a fixed-width ``varchar``
(no meaningful storage or index-size penalty).

Portability: follows the dialect-branching pattern established by
``938d6dbdb93f_add_wallet_ledger_mpesa_transactions_.py``.

* On PostgreSQL we issue a plain ``ALTER COLUMN ... TYPE VARCHAR(50)`` (a fast
  metadata-only change that takes no long lock on the table).
* On SQLite, which cannot ``ALTER COLUMN`` in place, we use
  ``op.batch_alter_table`` (recreate-and-copy).

Downgrade safety: shrinking the column back to ``VARCHAR(20)`` must never
silently corrupt a status value. We guard explicitly: if any row carries a
status longer than 20 characters we refuse the downgrade with a clear error
instead of relying on implicit engine behaviour.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e5c7d9b1a3f2'
down_revision = 'c1d4e7f9a2b5'
branch_labels = None
depends_on = None

_STATUS_OLD_LENGTH = 20
_STATUS_NEW_LENGTH = 50


def _dialect_name():
    return op.get_context().dialect.name


def upgrade():
    if _dialect_name() == "postgresql":
        op.alter_column(
            'mpesa_transactions',
            'status',
            type_=sa.String(length=_STATUS_NEW_LENGTH),
            existing_type=sa.String(length=_STATUS_OLD_LENGTH),
            existing_nullable=False,
        )
    else:
        with op.batch_alter_table('mpesa_transactions', schema=None) as batch_op:
            batch_op.alter_column(
                'status',
                type_=sa.String(length=_STATUS_NEW_LENGTH),
                existing_type=sa.String(length=_STATUS_OLD_LENGTH),
                existing_nullable=False,
            )


def downgrade():
    # Explicit guard: never silently truncate a status value longer than the
    # original 20-character width. Fail loudly instead. This only runs online
    # (during ``--sql`` offline rendering there is no live connection to
    # inspect, and the column type is simply emitted).
    context = op.get_context()
    if not getattr(context, "as_sql", False):
        long_rows = context.bind.execute(
            sa.text(
                "SELECT COUNT(*) FROM mpesa_transactions "
                f"WHERE length(status) > {_STATUS_OLD_LENGTH}"
            )
        ).scalar()
        if long_rows and long_rows > 0:
            raise RuntimeError(
                f"Refusing to downgrade mpesa_transactions.status to "
                f"VARCHAR({_STATUS_OLD_LENGTH}): {long_rows} row(s) carry a "
                f"status value longer than {_STATUS_OLD_LENGTH} characters "
                f"(e.g. 'ReconciliationPending'). Shrinking the column would "
                f"corrupt those rows. Backfill or migrate those rows before "
                f"downgrading."
            )

    if _dialect_name() == "postgresql":
        op.alter_column(
            'mpesa_transactions',
            'status',
            type_=sa.String(length=_STATUS_OLD_LENGTH),
            existing_type=sa.String(length=_STATUS_NEW_LENGTH),
            existing_nullable=False,
        )
    else:
        with op.batch_alter_table('mpesa_transactions', schema=None) as batch_op:
            batch_op.alter_column(
                'status',
                type_=sa.String(length=_STATUS_OLD_LENGTH),
                existing_type=sa.String(length=_STATUS_NEW_LENGTH),
                existing_nullable=False,
            )
