"""One-off: manually move a stuck M-Pesa deposit to ``ManualReviewRequired``.

Use this when a deposit has been ``Pending`` / ``ReconciliationPending`` for far
too long, the automatic age/attempt cutoffs are not catching it (for example the
sweeper stopped re-querying it, or the age cutoff was not deployed/enabled when
it got stuck), and an operator has decided it needs a human.

It reuses :meth:`MpesaService._transition_to_manual_review` — the exact code
path the automated reconciler uses — instead of raw SQL, so:

* the deposit row is taken under ``SELECT ... FOR UPDATE`` before it changes;
* an already-terminal deposit (``Completed`` / ``Failed`` / ``ManualReviewRequired``)
  is refused, never re-touched;
* NO wallet credit, ledger entry, or internal transaction is created — a deposit
  with no confirmed callback/query result must not move money. ``ManualReviewRequired``
  is a hold, not a payment; the idempotent credit path stays available for a
  future human-confirmed payment.
* a structured ``MPESA_EVENT=MANUAL_REVIEW_REQUIRED`` audit line is logged.

Bootstrapping matches ``seed.py``: ``create_app()`` + an app context, database
selected by ``DATABASE_URL``. Run it wherever the target database is reachable
(e.g. a Render shell for production).

Dry run (reads only, writes nothing) — the default::

    python scripts/resolve_stuck_mpesa_transaction.py --id 7

Apply the change::

    python scripts/resolve_stuck_mpesa_transaction.py --id 7 --commit

Optional::

    --reason "..."   override the recorded failure_reason
    --yes            skip the interactive confirmation prompt (for --commit)
"""

import argparse
import os
import sys
from decimal import Decimal

# Allow ``python scripts/resolve_stuck_mpesa_transaction.py`` from the repo root
# (Python only puts the script's own directory on sys.path, not the cwd).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
from app.extensions import db
from app.models import (
    MpesaTransaction,
    MpesaTransactionStatus,
    Wallet,
    WalletLedger,
)
from app.services.mpesa_service import MpesaService

DEFAULT_REASON = (
    "Manually resolved: stuck ~48h, Daraja query unreachable/unresponsive "
    "for this checkout request"
)


def _wallet_for(mpesa_transaction):
    return Wallet.query.filter_by(id=mpesa_transaction.wallet_id).first()


def _ledger_rows_for(reference):
    if not reference:
        return []
    return WalletLedger.query.filter_by(reference=reference).all()


def _snapshot(mpesa_transaction):
    wallet = _wallet_for(mpesa_transaction)
    ledger = _ledger_rows_for(mpesa_transaction.checkout_request_id)
    age_hours = None
    if mpesa_transaction.created_at is not None:
        from datetime import datetime

        age_hours = round(
            (datetime.utcnow() - mpesa_transaction.created_at).total_seconds()
            / 3600.0,
            1,
        )
    return {
        "id": mpesa_transaction.id,
        "status": mpesa_transaction.status,
        "amount": str(mpesa_transaction.amount),
        "user_id": mpesa_transaction.user_id,
        "wallet_id": mpesa_transaction.wallet_id,
        "wallet_balance": (
            str(wallet.balance) if wallet is not None else "<no wallet>"
        ),
        "reconciliation_attempts": mpesa_transaction.reconciliation_attempts or 0,
        "last_reconciled_at": (
            mpesa_transaction.last_reconciled_at.isoformat()
            if mpesa_transaction.last_reconciled_at
            else None
        ),
        "created_at": (
            mpesa_transaction.created_at.isoformat()
            if mpesa_transaction.created_at
            else None
        ),
        "age_hours": age_hours,
        "checkout_request_id": mpesa_transaction.checkout_request_id,
        "merchant_request_id": mpesa_transaction.merchant_request_id,
        "mpesa_receipt_number": mpesa_transaction.mpesa_receipt_number,
        "internal_transaction_id": mpesa_transaction.transaction_id,
        "failure_reason": mpesa_transaction.failure_reason,
        "wallet_ledger_rows_for_checkout_id": len(ledger),
    }


def _print_snapshot(title, snap):
    print(f"\n{title}")
    print("-" * len(title))
    for key, value in snap.items():
        print(f"  {key:38s}: {value}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--id", type=int, default=7, help="mpesa_transaction id")
    parser.add_argument("--reason", default=DEFAULT_REASON)
    parser.add_argument(
        "--commit",
        action="store_true",
        help="actually apply the transition (default: dry run)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="skip the interactive confirmation prompt",
    )
    args = parser.parse_args(argv)

    app = create_app()
    with app.app_context():
        engine_url = db.engine.url
        print(
            f"Database: {engine_url.drivername}://{engine_url.host or '<local>'}"
            f"/{engine_url.database}"
        )

        mpesa_transaction = MpesaTransaction.query.filter_by(id=args.id).first()
        if mpesa_transaction is None:
            print(f"ERROR: no mpesa_transaction with id={args.id}")
            return 2

        before = _snapshot(mpesa_transaction)
        before_balance = (
            Decimal(before["wallet_balance"])
            if before["wallet_balance"][0].isdigit()
            else None
        )
        _print_snapshot("BEFORE", before)

        if MpesaTransactionStatus.is_terminal(mpesa_transaction.status):
            print(
                f"\nNothing to do: status is already terminal "
                f"({mpesa_transaction.status}). Refusing to touch it."
            )
            return 0

        if mpesa_transaction.transaction_id is not None:
            print(
                "\nWARNING: this deposit already has an internal transaction id "
                f"({mpesa_transaction.transaction_id}) — it may have been "
                "credited. Investigate before forcing a manual-review hold."
            )
            if not args.yes:
                print("Aborting. Re-run with --yes if this is expected.")
                return 3

        print(f"\nWould set:  status -> {MpesaTransactionStatus.MANUAL_REVIEW_REQUIRED}")
        print(f"Would set:  failure_reason -> {args.reason!r}")
        print("Would NOT:  credit the wallet, create a ledger entry, or create "
              "an internal transaction.")

        if not args.commit:
            print("\nDRY RUN — no changes written. Re-run with --commit to apply.")
            return 0

        if not args.yes:
            answer = input(
                f"\nApply this transition to mpesa_transaction id={args.id} on "
                f"{engine_url.database}? [type 'yes' to confirm]: "
            ).strip()
            if answer != "yes":
                print("Aborted; no changes written.")
                return 1

        outcome = MpesaService._transition_to_manual_review(
            args.id,
            reason=args.reason,
            enforce_attempt_budget=False,
        )
        print(f"\n_transition_to_manual_review returned: {outcome!r}")

        db.session.expire_all()
        after_row = MpesaTransaction.query.filter_by(id=args.id).first()
        after = _snapshot(after_row)
        after_balance = (
            Decimal(after["wallet_balance"])
            if after["wallet_balance"][0].isdigit()
            else None
        )
        _print_snapshot("AFTER", after)

        print("\nVerification")
        print("------------")
        ok = True

        if after["status"] == MpesaTransactionStatus.MANUAL_REVIEW_REQUIRED:
            print("  status transitioned to ManualReviewRequired ........ OK")
        else:
            print(f"  status is {after['status']!r}, expected ManualReviewRequired  FAIL")
            ok = False

        if before["internal_transaction_id"] == after["internal_transaction_id"]:
            print(
                "  internal transaction id unchanged "
                f"({after['internal_transaction_id']}) ......... OK  (no credit)"
            )
        else:
            print("  internal transaction id CHANGED — a credit may have occurred  FAIL")
            ok = False

        if before_balance is not None and after_balance is not None:
            if before_balance == after_balance:
                print(
                    f"  wallet balance unchanged ({after_balance}) ......... OK  "
                    "(no money moved)"
                )
            else:
                print(
                    f"  wallet balance CHANGED {before_balance} -> {after_balance}  FAIL"
                )
                ok = False

        if after["wallet_ledger_rows_for_checkout_id"] == before[
            "wallet_ledger_rows_for_checkout_id"
        ]:
            print(
                "  wallet_ledger rows for this checkout id unchanged "
                f"({after['wallet_ledger_rows_for_checkout_id']}) ... OK"
            )
        else:
            print("  wallet_ledger rows CHANGED for this checkout id  FAIL")
            ok = False

        print("\nRESULT:", "OK — deposit held for manual review, no money moved."
              if ok else "PROBLEM — review the FAIL lines above.")
        return 0 if ok else 4


if __name__ == "__main__":
    sys.exit(main())
