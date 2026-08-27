"""Aggregate analytics for users and the platform.

All aggregation is performed with standard SQL (``sum``/``count``/``coalesce``)
plus a small amount of Python grouping for time-series data. This keeps the
logic database-agnostic so it runs identically on SQLite (tests) and
PostgreSQL (production) without dialect-specific functions such as
PostgreSQL's ``to_char``.
"""

from collections import defaultdict
from datetime import datetime

from sqlalchemy import func, or_

from app.extensions import db
from app.models import Transaction, User, Wallet
from app.models.transaction import TransactionType
from app.utils.helpers import ZERO_MONEY, money_to_string, to_money


class AnalyticsService:
    """Read-only aggregate queries over existing models."""

    @staticmethod
    def _month_series(months_back):
        """Return a list of ``(year, month)`` tuples ending on the current month."""

        today = datetime.utcnow()
        series = []
        for offset in range(months_back - 1, -1, -1):
            total = (today.month - 1) - offset
            year = today.year + (total // 12)
            month = (total % 12) + 1
            series.append((year, month))
        return series

    @staticmethod
    def user_wallet_analytics(user_id):
        """Compute wallet analytics for a single user.

        Returns numeric values (not formatted strings) so the frontend can
        chart them directly.
        """

        wallet = Wallet.query.filter_by(user_id=user_id).first()
        current_balance = to_money(wallet.balance) if wallet else ZERO_MONEY

        total_sent = (
            db.session.query(
                func.coalesce(func.sum(Transaction.amount), 0)
            )
            .filter(
                Transaction.sender_id == user_id,
                Transaction.tx_type == TransactionType.TRANSFER,
            )
            .scalar()
            or ZERO_MONEY
        )

        total_received = (
            db.session.query(
                func.coalesce(func.sum(Transaction.amount), 0)
            )
            .filter(
                Transaction.receiver_id == user_id,
                Transaction.tx_type == TransactionType.TRANSFER,
            )
            .scalar()
            or ZERO_MONEY
        )

        total_deposits = (
            db.session.query(
                func.coalesce(func.sum(Transaction.amount), 0)
            )
            .filter(
                Transaction.receiver_id == user_id,
                Transaction.tx_type == TransactionType.DEPOSIT,
            )
            .scalar()
            or ZERO_MONEY
        )

        total_transfers = Transaction.query.filter(
            or_(
                Transaction.sender_id == user_id,
                Transaction.receiver_id == user_id,
            ),
            Transaction.tx_type == TransactionType.TRANSFER,
        ).count()

        transaction_count = Transaction.query.filter(
            or_(
                Transaction.sender_id == user_id,
                Transaction.receiver_id == user_id,
            )
        ).count()

        monthly_trend = AnalyticsService._user_monthly_trend(user_id)

        return {
            "current_balance": float(current_balance),
            "total_received": float(total_received),
            "total_sent": float(total_sent),
            "total_deposits": float(total_deposits),
            "total_transfers": total_transfers,
            "transaction_count": transaction_count,
            "monthly_trend": monthly_trend,
        }

    @staticmethod
    def _user_monthly_trend(user_id):
        """Last 6 months of inflow/outflow for the user (DB-agnostic)."""

        months = AnalyticsService._month_series(6)
        start_year, start_month = months[0]
        start = datetime(start_year, start_month, 1)

        transactions = (
            Transaction.query.filter(
                or_(
                    Transaction.sender_id == user_id,
                    Transaction.receiver_id == user_id,
                ),
                Transaction.timestamp >= start,
            )
            .order_by(Transaction.timestamp.asc())
            .all()
        )

        inflow = defaultdict(lambda: ZERO_MONEY)
        outflow = defaultdict(lambda: ZERO_MONEY)

        for tx in transactions:
            if not tx.timestamp:
                continue
            key = (tx.timestamp.year, tx.timestamp.month)
            if key not in months:
                continue
            amount = to_money(tx.amount)
            if tx.tx_type == TransactionType.DEPOSIT and tx.receiver_id == user_id:
                inflow[key] += amount
            elif tx.tx_type == TransactionType.TRANSFER:
                if tx.receiver_id == user_id:
                    inflow[key] += amount
                if tx.sender_id == user_id:
                    outflow[key] += amount

        trend = []
        for year, month in months:
            label = datetime(year, month, 1).strftime("%b %Y")
            trend.append(
                {
                    "month": f"{year}-{month:02d}",
                    "label": label,
                    "inflow": money_to_string(inflow.get((year, month), ZERO_MONEY)),
                    "outflow": money_to_string(outflow.get((year, month), ZERO_MONEY)),
                }
            )

        return trend

    @staticmethod
    def platform_analytics():
        """Compute platform-wide analytics for the admin dashboard."""

        total_users = User.query.count()
        active_wallets = Wallet.query.filter(Wallet.balance > 0).count()
        total_liquidity = (
            db.session.query(func.coalesce(func.sum(Wallet.balance), 0)).scalar()
            or ZERO_MONEY
        )
        collected_fees = (
            db.session.query(func.coalesce(func.sum(Transaction.fee), 0)).scalar()
            or ZERO_MONEY
        )

        months = AnalyticsService._month_series(12)
        start_year, start_month = months[0]
        start = datetime(start_year, start_month, 1)
        month_set = set(months)

        transactions = (
            Transaction.query.filter(Transaction.timestamp >= start)
            .order_by(Transaction.timestamp.asc())
            .all()
        )

        volume_by_month = defaultdict(lambda: ZERO_MONEY)
        transfer_volume = defaultdict(lambda: ZERO_MONEY)
        transfer_count = defaultdict(int)
        transfer_tx_count = 0

        for tx in transactions:
            if not tx.timestamp:
                continue
            key = (tx.timestamp.year, tx.timestamp.month)
            if key not in month_set:
                continue
            amount = to_money(tx.amount)
            volume_by_month[key] += amount
            if tx.tx_type == TransactionType.TRANSFER:
                transfer_tx_count += 1
                if tx.sender_id:
                    transfer_volume[tx.sender_id] += amount
                    transfer_count[tx.sender_id] += 1
                if tx.receiver_id:
                    transfer_volume[tx.receiver_id] += amount
                    transfer_count[tx.receiver_id] += 1

        growth_curve = []
        for year, month in months:
            label = datetime(year, month, 1).strftime("%b")
            growth_curve.append(
                {
                    "month": f"{year}-{month:02d}",
                    "label": label,
                    "value": float(volume_by_month.get((year, month), ZERO_MONEY)),
                }
            )

        today = datetime.utcnow()
        current_key = (today.year, today.month)
        previous_month = today.month - 1 or 12
        previous_year = today.year if today.month > 1 else today.year - 1
        previous_key = (previous_year, previous_month)

        current_volume = volume_by_month.get(current_key, ZERO_MONEY)
        previous_volume = volume_by_month.get(previous_key, ZERO_MONEY)

        if previous_volume > 0:
            volume_delta = float(
                (current_volume - previous_volume) / previous_volume * 100
            )
        else:
            volume_delta = None

        month_start = datetime(today.year, today.month, 1)
        new_users_mo = User.query.filter(User.created_at >= month_start).count()

        avg_tx_size = (
            float(sum((to_money(t.amount) for t in transactions), ZERO_MONEY))
            / len(transactions)
            if transactions
            else 0.0
        )

        top = sorted(
            transfer_volume.items(), key=lambda item: item[1], reverse=True
        )[:5]
        most_active = []
        for user_id, volume in top:
            user = User.query.get(user_id)
            if not user:
                continue
            most_active.append(
                {
                    "id": user.id,
                    "name": user.name,
                    "volume": float(volume),
                    "transactions": transfer_count[user_id],
                }
            )

        return {
            "volumeMonthly": float(current_volume),
            "volumeDelta": volume_delta,
            "newUsersMo": new_users_mo,
            "newUsersNote": f"{new_users_mo} new users this month",
            "avgTxSize": avg_tx_size,
            "avgTxSizeNote": "Average transfer size",
            "growthCurve": growth_curve,
            "mostActive": most_active,
            "totalUsers": total_users,
            "activeWallets": active_wallets,
            "platformLiquidity": float(total_liquidity),
            "collectedFees": float(collected_fees),
        }
