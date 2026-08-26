"""add reconciliation recovery fields to mpesa_transactions

Revision ID: f4a9b2c3d5e6
Revises: a7f3c9e1b2d4
Create Date: 2026-08-26 10:00:00.000000

Adds the `RECONCILIATION_PENDING` recovery support to the M-Pesa deposit flow:

* `query_result_code` / `query_result_desc` - the most recent Daraja
  server-to-server reconciliation result (observability).
* `reconciliation_attempts` - how many times reconciliation has been attempted
  (NOT NULL, server default 0).
* `last_reconciled_at` - timestamp of the last reconciliation attempt.
* `failure_reason` - the definitive failure reason when a deposit is FAILED, or
  the reason a confirmed payment could not yet be credited.

No existing rows, indexes, or tables are dropped or modified beyond these
additive columns. The new `RECONCILIATION_PENDING` status is a plain string
value within the existing `status` column, so no data migration is required.

`reconciliation_attempts` keeps its `server_default='0'` on purpose. It matches
the model's `server_default="0"` and it means an application version that does
not know about the column can still INSERT into `mpesa_transactions`, so this
migration can be applied before the new code is released (and survives a code
rollback) without breaking deposit initiation.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f4a9b2c3d5e6'
down_revision = 'a7f3c9e1b2d4'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('mpesa_transactions', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('query_result_code', sa.String(length=10), nullable=True)
        )
        batch_op.add_column(
            sa.Column('query_result_desc', sa.String(length=255), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                'reconciliation_attempts',
                sa.Integer(),
                nullable=False,
                server_default='0',
            )
        )
        batch_op.add_column(
            sa.Column('last_reconciled_at', sa.DateTime(), nullable=True)
        )
        batch_op.add_column(
            sa.Column('failure_reason', sa.String(length=255), nullable=True)
        )

    # The server_default is deliberately retained (see the module docstring):
    # dropping it would make INSERTs from an application version that predates
    # this column fail, which would turn a rolling deploy or a code rollback
    # into an M-Pesa outage.


def downgrade():
    with op.batch_alter_table('mpesa_transactions', schema=None) as batch_op:
        batch_op.drop_column('failure_reason')
        batch_op.drop_column('last_reconciled_at')
        batch_op.drop_column('reconciliation_attempts')
        batch_op.drop_column('query_result_desc')
        batch_op.drop_column('query_result_code')
