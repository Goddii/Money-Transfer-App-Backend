import logging
import os

_logger = logging.getLogger(__name__)


def _parse_int_env(name, default):
    """Parse an integer environment variable without crashing startup.

    A missing, empty or non-numeric value falls back to ``default`` and logs a
    warning. Only the variable *name* is logged; values are never logged.
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        _logger.warning(
            "Invalid value for %s; using safe default %s", name, default
        )
        return default


class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-secret-key-change-me')
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL', 'postgresql://postgres:postgres@localhost:5432/money_transfer_app')
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    JWT_SECRET_KEY = os.environ.get('JWT_SECRET_KEY', 'dev-jwt-secret-key-change-me')

    CORS_ORIGINS = os.environ.get('CORS_ORIGINS', 'http://localhost:5173')

    # --- Safaricom Daraja / M-Pesa -------------------------------------
    # Credentials are supplied through the environment only. They are never
    # hardcoded, committed or stored in the database.
    DARAJA_ENV = os.environ.get('DARAJA_ENV', 'sandbox')
    DARAJA_CONSUMER_KEY = os.environ.get('DARAJA_CONSUMER_KEY', '')
    DARAJA_CONSUMER_SECRET = os.environ.get('DARAJA_CONSUMER_SECRET', '')
    DARAJA_SHORTCODE = os.environ.get('DARAJA_SHORTCODE', '')
    DARAJA_PASSKEY = os.environ.get('DARAJA_PASSKEY', '')
    DARAJA_CALLBACK_URL = os.environ.get('DARAJA_CALLBACK_URL', '')
    DARAJA_TRANSACTION_TYPE = os.environ.get(
        'DARAJA_TRANSACTION_TYPE', 'CustomerPayBillOnline'
    )
    DARAJA_BASE_URL = os.environ.get(
        'DARAJA_BASE_URL',
        'https://api.safaricom.co.ke'
        if os.environ.get('DARAJA_ENV', 'sandbox') == 'production'
        else 'https://sandbox.safaricom.co.ke',
    )
    # Safe integer parsing: a malformed value must never crash Gunicorn boot.
    DARAJA_TIMEOUT = _parse_int_env('DARAJA_TIMEOUT', 30)

    # Background reconciliation sweep: how often (seconds) the worker auto-runs
    # recover_deposits() to credit deposits left in PENDING / RECONCILIATION_PENDING
    # (callback never arrived, or arrived while Daraja's live query was still
    # inconclusive). 0 disables the sweeper. Not started under TESTING.
    MPESA_RECONCILIATION_INTERVAL_SECONDS = _parse_int_env(
        'MPESA_RECONCILIATION_INTERVAL_SECONDS', 60
    )

    # Cross-process sweeper de-duplication (Issue 2). When the deployment runs
    # multiple Gunicorn workers or multiple application instances, a PostgreSQL
    # session-level advisory lock with this key ensures only ONE process runs the
    # reconciliation sweep. Ignored on non-PostgreSQL backends (where advisory
    # locks do not exist); there the sweeper runs unconditionally and a warning
    # is logged. Keep this value distinct from any other advisory lock key.
    MPESA_SWEEPER_LEADER_LOCK_ID = _parse_int_env(
        'MPESA_SWEEPER_LEADER_LOCK_ID', 912374561
    )

    # Long-stuck deposit visibility (Issue 4). A deposit must never silently
    # remain PENDING / RECONCILIATION_PENDING for days. When a recoverable
    # deposit is older than this many seconds (or has been reconciled at least
    # this many times) without resolving, the sweeper emits a structured warning
    # carrying the transaction id and recovery metadata. This is visibility only;
    # the deposit is NEVER automatically marked FAILED.
    MPESA_STUCK_DEPOSIT_ALERT_SECONDS = _parse_int_env(
        'MPESA_STUCK_DEPOSIT_ALERT_SECONDS', 86400
    )
    MPESA_MAX_RECONCILIATION_ATTEMPTS = _parse_int_env(
        'MPESA_MAX_RECONCILIATION_ATTEMPTS', 48
    )

    # Optional defence-in-depth: only accept M-Pesa callbacks from these source
    # IPs (comma-separated). Empty/unset means "allow all" — the authoritative
    # protection is Daraja reconciliation (see mpesa_service.query_stk_status),
    # not this allowlist. Safaricom IPs change, so do not rely on it alone.
    _allowed_ips = os.environ.get('DARAJA_CALLBACK_ALLOWED_IPS')
    DARAJA_CALLBACK_ALLOWED_IPS = (
        [ip.strip() for ip in _allowed_ips.split(',') if ip.strip()]
        if _allowed_ips
        else []
    )


class TestConfig(Config):
    """Configuration used by the automated test suite.

    Defaults to an isolated SQLite database so tests never touch a real
    PostgreSQL instance. Set ``TEST_DATABASE_URL`` to run the same suite
    against PostgreSQL.
    """

    TESTING = True
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        'TEST_DATABASE_URL', 'sqlite:///:memory:'
    )
    SECRET_KEY = 'test-secret-key-for-automated-tests-only'
    JWT_SECRET_KEY = 'test-jwt-secret-key-for-automated-tests-only'

    # Deterministic, clearly fake Daraja values. No real credentials are used
    # and no outbound Daraja request is ever performed in tests.
    DARAJA_ENV = 'sandbox'
    DARAJA_CONSUMER_KEY = 'test-consumer-key'
    DARAJA_CONSUMER_SECRET = 'test-consumer-secret'
    DARAJA_SHORTCODE = '174379'
    DARAJA_PASSKEY = 'test-passkey'
    DARAJA_CALLBACK_URL = 'https://example.test/api/mpesa/callback'
    DARAJA_BASE_URL = 'https://sandbox.safaricom.co.ke'
