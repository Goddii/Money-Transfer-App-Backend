"""add service_providers and service_payments tables

Revision ID: b8d2e4f6a1c3
Revises: f4a9b2c3d5e6
Create Date: 2026-08-30 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b8d2e4f6a1c3'
down_revision = 'f4a9b2c3d5e6'
branch_labels = None
depends_on = None


def upgrade():
    # --- service_providers ---
    op.create_table(
        'service_providers',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('name', sa.String(50), nullable=False),
        sa.Column('service_type', sa.String(20), unique=True, nullable=False),
        sa.Column('display_name', sa.String(100), nullable=False),
        sa.Column('description', sa.String(255), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('1')),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )

    # Seed the three simulated providers.
    op.execute("""
        INSERT INTO service_providers (name, service_type, display_name, description, is_active, created_at)
        VALUES
            ('Electricity', 'ELECTRICITY', 'Electricity', 'Purchase simulated prepaid electricity', 1, CURRENT_TIMESTAMP),
            ('Water', 'WATER', 'Water', 'Pay a simulated water bill', 1, CURRENT_TIMESTAMP),
            ('Airtime', 'AIRTIME', 'Airtime', 'Purchase simulated airtime', 1, CURRENT_TIMESTAMP)
    """)

    # --- service_payments ---
    op.create_table(
        'service_payments',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False, index=True),
        sa.Column('wallet_id', sa.Integer(), sa.ForeignKey('wallets.id'), nullable=False, index=True),
        sa.Column('transaction_id', sa.Integer(), sa.ForeignKey('transactions.id'), nullable=True, index=True),
        sa.Column('service_type', sa.String(20), nullable=False),
        sa.Column('account_number', sa.String(30), nullable=False),
        sa.Column('amount', sa.Numeric(12, 2), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='Initiated'),
        sa.Column('payment_reference', sa.String(30), unique=True, nullable=False),
        sa.Column('provider_reference', sa.String(64), nullable=True),
        sa.Column('failure_reason', sa.String(255), nullable=True),
        sa.Column('result_metadata', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True, index=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
    )


def downgrade():
    op.drop_table('service_payments')
    op.drop_table('service_providers')
