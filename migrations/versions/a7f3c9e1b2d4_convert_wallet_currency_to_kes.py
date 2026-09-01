"""convert existing wallet currency from USD to KES

Revision ID: a7f3c9e1b2d4
Revises: 938d6dbdb93f
Create Date: 2026-08-23 23:30:00.000000

Vyloc is a local-currency (Kenyan Shilling) application. The application model
now defaults new wallets to ``KES`` (see ``app/models/wallet.py``), but the
previously applied migration ``938d6dbdb93f`` backfilled existing wallets with
``'USD'``. This data migration converts those legacy rows so every wallet uses
the application's local currency.

This is a data-only migration: no schema is altered and no rows are deleted.
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a7f3c9e1b2d4'
down_revision = '938d6dbdb93f'
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    # Convert legacy USD wallets to the application's local currency (KES).
    connection.execute(
        sa.text(
            "UPDATE wallets SET currency = 'KES' WHERE currency = 'USD'"
        )
    )


def downgrade():
    connection = op.get_bind()
    # Inverse of upgrade: revert KES wallets back to USD. This precisely undoes
    # the conversion performed above.
    connection.execute(
        sa.text(
            "UPDATE wallets SET currency = 'USD' WHERE currency = 'KES'"
        )
    )
