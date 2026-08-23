"""add wallet ledger, mpesa transactions, transfer note and wallet uniqueness

Revision ID: 938d6dbdb93f
Revises: 61b617f1cc85
Create Date: 2026-08-23 00:35:11.528654

This migration is additive only. It:

* adds the ``wallet_ledger`` audit table (unique on
  ``wallet_id + entry_type + reference`` to prevent duplicate wallet credits);
* adds the ``mpesa_transactions`` table (unique ``checkout_request_id`` and
  ``mpesa_receipt_number`` for idempotent Daraja callbacks);
* adds the nullable ``transactions.note`` column used by transfer notes;
* adds lookup indexes used by wallet/transaction queries;
* normalises legacy ``users`` rows whose ``status``/``is_active`` are NULL so
  existing accounts are not locked out, and sets sensible server defaults for
  future rows;
* backfills a wallet for existing users that do not have one and then enforces
  one wallet per user.

No existing rows are deleted or modified beyond the explicit NULL normalisation
above (which only touches NULL columns, never explicit inactive/frozen rows).
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '938d6dbdb93f'
down_revision = '61b617f1cc85'
branch_labels = None
depends_on = None


def _dialect_name():
    return op.get_bind().dialect.name


def _now_sql():
    """UTC timestamp expression matching the application's ``datetime.utcnow``.

    SQLite already stores UTC, so ``CURRENT_TIMESTAMP`` is correct. PostgreSQL
    returns a session-timezone ``timestamptz`` for ``now()``/``CURRENT_TIMESTAMP``,
    so we explicitly shift to UTC to stay consistent with application rows.
    """
    if _dialect_name() == "postgresql":
        return "timezone('UTC', now())"
    return "CURRENT_TIMESTAMP"


def _create_index(table, name, columns, unique=False):
    """Create an (ordinary) index safely for the active dialect.

    On PostgreSQL, indexes added to *existing* tables (with data) are built
    ``CONCURRENTLY`` so they never take a long blocking lock on hot tables
    during a deploy. ``CREATE INDEX CONCURRENTLY`` must run outside a
    transaction, so we use Alembic's autocommit block (which first commits the
    DDL from earlier steps so the target table is visible, then runs the
    concurrent build on the same connection).
    """
    if _dialect_name() == "postgresql":
        kind = "UNIQUE " if unique else ""
        stmt = sa.text(
            f"CREATE {kind}INDEX CONCURRENTLY {name} ON {table} "
            f"({', '.join(columns)})"
        )
        with op.get_context().autocommit_block():
            op.execute(stmt)
    else:
        op.create_index(name, table, columns, unique=unique)


def _create_unique_constraint_concurrently(table, name, columns):
    """Build a named UNIQUE constraint on an existing table without long locks.

    R5: the unique index is built ``CONCURRENTLY`` (no blocking lock), then
    promoted to a proper constraint via ``USING INDEX``. The resulting object
    matches the model's ``UniqueConstraint('user_id', name='unique_wallet_user')``
    so ``flask db check`` stays clean.
    """
    if _dialect_name() == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(
                sa.text(
                    f"CREATE UNIQUE INDEX CONCURRENTLY {name} ON {table} "
                    f"({', '.join(columns)})"
                )
            )
        op.execute(
            sa.text(
                f"ALTER TABLE {table} ADD CONSTRAINT {name} UNIQUE "
                f"USING INDEX {name}"
            )
        )
    else:
        with op.batch_alter_table(table) as batch_op:
            batch_op.create_unique_constraint(name, columns)


def _drop_index(name):
    op.execute(sa.text(f"DROP INDEX IF EXISTS {name}"))


def _drop_unique_constraint(name, table):
    if _dialect_name() == "postgresql":
        # Dropping the constraint also drops the underlying unique index.
        op.execute(sa.text(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}"))
    else:
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_constraint(name, type_="unique")


def _normalize_users():
    """Fix legacy NULL ``status``/``is_active`` and set server defaults.

    Only NULL values are corrected; explicitly inactive/frozen accounts are
    left untouched. Server defaults prevent future NULLs.
    """
    op.execute(sa.text("UPDATE users SET status = 'Active' WHERE status IS NULL"))
    op.execute(
        sa.text("UPDATE users SET is_active = true WHERE is_active IS NULL")
    )

    if _dialect_name() == "postgresql":
        op.alter_column(
            'users', 'status', server_default='Active',
            existing_type=sa.String(length=20),
        )
        op.alter_column(
            'users', 'is_active', server_default=sa.true(),
            existing_type=sa.Boolean(),
        )
    else:
        with op.batch_alter_table('users') as batch_op:
            batch_op.alter_column(
                'status', server_default='Active',
                existing_type=sa.String(length=20),
            )
            batch_op.alter_column(
                'is_active', server_default=sa.true(),
                existing_type=sa.Boolean(),
            )


def _revert_user_defaults():
    if _dialect_name() == "postgresql":
        op.alter_column(
            'users', 'status', server_default=None,
            existing_type=sa.String(length=20),
        )
        op.alter_column(
            'users', 'is_active', server_default=None,
            existing_type=sa.Boolean(),
        )
    else:
        with op.batch_alter_table('users') as batch_op:
            batch_op.alter_column(
                'status', server_default=None,
                existing_type=sa.String(length=20),
            )
            batch_op.alter_column(
                'is_active', server_default=None,
                existing_type=sa.Boolean(),
            )


def _backfill_missing_wallets():
    """Give every existing user a wallet before enforcing uniqueness.

    Existing wallets and balances are never touched. If a user already has more
    than one wallet the migration stops instead of deleting financial data.
    """
    connection = op.get_bind()

    duplicates = connection.execute(
        sa.text(
            "SELECT user_id FROM wallets GROUP BY user_id HAVING COUNT(*) > 1"
        )
    ).fetchall()

    if duplicates:
        duplicate_ids = ", ".join(str(row[0]) for row in duplicates)
        raise RuntimeError(
            "Cannot enforce one wallet per user: duplicate wallets exist for "
            f"user_id(s) {duplicate_ids}. Resolve these rows manually before "
            "running this migration."
        )

    connection.execute(
        sa.text(
            "INSERT INTO wallets (user_id, balance, currency, created_at) "
            f"SELECT u.id, 0.00, 'USD', {_now_sql()} "
            "FROM users u "
            "LEFT JOIN wallets w ON w.user_id = u.id "
            "WHERE w.id IS NULL"
        )
    )


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table('mpesa_transactions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('wallet_id', sa.Integer(), nullable=False),
    sa.Column('transaction_id', sa.Integer(), nullable=True),
    sa.Column('account_reference', sa.String(length=20), nullable=False),
    sa.Column('phone_number', sa.String(length=20), nullable=False),
    sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('merchant_request_id', sa.String(length=64), nullable=True),
    sa.Column('checkout_request_id', sa.String(length=64), nullable=True),
    sa.Column('mpesa_receipt_number', sa.String(length=32), nullable=True),
    sa.Column('result_code', sa.String(length=10), nullable=True),
    sa.Column('result_desc', sa.String(length=255), nullable=True),
    sa.Column('transaction_date', sa.String(length=20), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['transaction_id'], ['transactions.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.ForeignKeyConstraint(['wallet_id'], ['wallets.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('checkout_request_id'),
    sa.UniqueConstraint('mpesa_receipt_number')
    )
    with op.batch_alter_table('mpesa_transactions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_mpesa_transactions_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_mpesa_transactions_transaction_id'), ['transaction_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_mpesa_transactions_user_id'), ['user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_mpesa_transactions_wallet_id'), ['wallet_id'], unique=False)

    op.create_table('wallet_ledger',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('wallet_id', sa.Integer(), nullable=False),
    sa.Column('transaction_id', sa.Integer(), nullable=True),
    sa.Column('entry_type', sa.String(length=10), nullable=False),
    sa.Column('amount', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('balance_before', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('balance_after', sa.Numeric(precision=12, scale=2), nullable=False),
    sa.Column('reference', sa.String(length=64), nullable=True),
    sa.Column('description', sa.String(length=255), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['transaction_id'], ['transactions.id'], ),
    sa.ForeignKeyConstraint(['wallet_id'], ['wallets.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('wallet_id', 'entry_type', 'reference', name='unique_wallet_ledger_reference')
    )
    with op.batch_alter_table('wallet_ledger', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_wallet_ledger_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_wallet_ledger_reference'), ['reference'], unique=False)
        batch_op.create_index(batch_op.f('ix_wallet_ledger_transaction_id'), ['transaction_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_wallet_ledger_wallet_id'), ['wallet_id'], unique=False)

    with op.batch_alter_table('transactions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('note', sa.String(length=255), nullable=True))

    # R5: indexes on the existing (populated) ``transactions`` table are built
    # concurrently on PostgreSQL so they never block live transfers/deposits.
    _create_index('transactions', 'ix_transactions_receiver_id', ['receiver_id'])
    _create_index('transactions', 'ix_transactions_sender_id', ['sender_id'])
    _create_index('transactions', 'ix_transactions_timestamp', ['timestamp'])

    # R3: normalise legacy NULL account state before anything else depends on it.
    _normalize_users()

    # R3/R5: backfill wallets (uses UTC timestamp) and then enforce uniqueness.
    _backfill_missing_wallets()

    _create_index('wallets', 'ix_wallets_user_id', ['user_id'])
    _create_unique_constraint_concurrently('wallets', 'unique_wallet_user', ['user_id'])

    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    _drop_unique_constraint('unique_wallet_user', 'wallets')
    _drop_index('ix_wallets_user_id')
    _drop_index('ix_transactions_timestamp')
    _drop_index('ix_transactions_sender_id')
    _drop_index('ix_transactions_receiver_id')

    # Data UPDATEs from _normalize_users are intentionally not reverted.
    _revert_user_defaults()

    with op.batch_alter_table('transactions', schema=None) as batch_op:
        batch_op.drop_column('note')

    with op.batch_alter_table('wallet_ledger', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_wallet_ledger_wallet_id'))
        batch_op.drop_index(batch_op.f('ix_wallet_ledger_transaction_id'))
        batch_op.drop_index(batch_op.f('ix_wallet_ledger_reference'))
        batch_op.drop_index(batch_op.f('ix_wallet_ledger_created_at'))

    op.drop_table('wallet_ledger')
    with op.batch_alter_table('mpesa_transactions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_mpesa_transactions_wallet_id'))
        batch_op.drop_index(batch_op.f('ix_mpesa_transactions_user_id'))
        batch_op.drop_index(batch_op.f('ix_mpesa_transactions_transaction_id'))
        batch_op.drop_index(batch_op.f('ix_mpesa_transactions_created_at'))

    op.drop_table('mpesa_transactions')
    # ### end Alembic commands ###
