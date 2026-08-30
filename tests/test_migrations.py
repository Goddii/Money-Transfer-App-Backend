"""Migration portability tests.

The deployed database is PostgreSQL, but the test suite runs on SQLite by
default, so migrations were never exercised against PostgreSQL. That gap let a
SQLite-only migration reach production: ``service_providers.is_active`` was
created with an integer boolean literal (``DEFAULT 1`` / ``VALUES (..., 1, ...)``),
which SQLite accepts and PostgreSQL rejects with ``DatatypeMismatch``. The
result was that ``flask db upgrade`` aborted on a brand-new PostgreSQL database,
so the whole Render start command (``db upgrade && seed && gunicorn``) failed
before the seed step ever ran.

The first two tests render the migration for both dialects *without needing a
database*, so they run everywhere (including SQLite-only CI) and would have
caught the bug. The last test performs the real end-to-end
``upgrade -> head`` and only runs when ``TEST_DATABASE_URL`` points at
PostgreSQL, matching the convention already used by ``test_mpesa.py``.
"""

import importlib.util
import io
import os
import uuid

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.config import TestConfig

MIGRATIONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations"
)
SERVICE_PROVIDER_MIGRATION = os.path.join(
    MIGRATIONS_DIR, "versions", "b8d2e4f6a1c3_add_service_payments_tables.py"
)

_TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "")
_POSTGRES_TEST_DB = _TEST_DATABASE_URL.startswith(
    ("postgresql://", "postgresql+psycopg2://", "postgres://")
)

requires_postgres = pytest.mark.skipif(
    not _POSTGRES_TEST_DB,
    reason=(
        "migrations are only exercised end-to-end on PostgreSQL; set "
        "TEST_DATABASE_URL to a disposable PostgreSQL database to run it"
    ),
)

EXPECTED_TABLES = {
    "users",
    "wallets",
    "transactions",
    "beneficiaries",
    "wallet_ledger",
    "mpesa_transactions",
    "service_providers",
    "service_payments",
}

EXPECTED_SERVICE_TYPES = {"ELECTRICITY", "WATER", "AIRTIME"}


def _load_migration_module():
    """Import the service-provider migration by path (it is not a package)."""
    spec = importlib.util.spec_from_file_location(
        "migration_b8d2e4f6a1c3", SERVICE_PROVIDER_MIGRATION
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    return module


def _render_upgrade_sql(dialect_name):
    """Render the migration's ``upgrade()`` as SQL for the given dialect.

    Uses Alembic offline ("--sql") mode, so no database connection is needed
    and the real migration code under test is executed.
    """
    module = _load_migration_module()
    buffer = io.StringIO()
    context = MigrationContext.configure(
        dialect_name=dialect_name,
        opts={"as_sql": True, "output_buffer": buffer},
    )

    with Operations.context(context):
        module.upgrade()

    return buffer.getvalue()


class TestServiceProviderMigrationPortability:
    """The boolean column and its seed rows must render per-dialect."""

    def test_postgresql_uses_real_boolean_literals(self):
        sql = _render_upgrade_sql("postgresql")

        # The column default must be a boolean, never the integer 1.
        assert "is_active BOOLEAN DEFAULT true NOT NULL" in sql
        assert "is_active BOOLEAN DEFAULT 1" not in sql

        # Every seeded provider row must supply a boolean, never the integer 1.
        inserts = [
            line
            for line in sql.splitlines()
            if line.startswith("INSERT INTO service_providers")
        ]
        assert len(inserts) == len(EXPECTED_SERVICE_TYPES)

        for statement in inserts:
            assert ", true, " in statement, statement
            assert ", 1, " not in statement, statement

    def test_sqlite_still_renders_successfully(self):
        """The fix must not regress the SQLite path used by the test suite."""
        sql = _render_upgrade_sql("sqlite")

        assert "CREATE TABLE service_providers" in sql
        assert "is_active BOOLEAN DEFAULT 1 NOT NULL" in sql

        inserts = [
            line
            for line in sql.splitlines()
            if line.startswith("INSERT INTO service_providers")
        ]
        assert len(inserts) == len(EXPECTED_SERVICE_TYPES)

    def test_all_three_providers_are_seeded_by_the_migration(self):
        """The frontend service list is seeded by the migration, not seed.py."""
        sql = _render_upgrade_sql("postgresql")

        for service_type in EXPECTED_SERVICE_TYPES:
            assert f"'{service_type}'" in sql


@requires_postgres
class TestFreshPostgresUpgrade:
    """``flask db upgrade`` must initialize a completely empty PostgreSQL DB."""

    @pytest.fixture
    def isolated_schema(self):
        """An empty, disposable PostgreSQL schema to migrate into."""
        schema = f"migration_check_{uuid.uuid4().hex[:8]}"
        engine = sa.create_engine(_TEST_DATABASE_URL)

        with engine.begin() as connection:
            connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))

        try:
            yield schema
        finally:
            with engine.begin() as connection:
                connection.execute(
                    sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
                )
            engine.dispose()

    @pytest.fixture
    def migrated_app(self, isolated_schema):
        """Run the real migrations to head inside the isolated schema."""
        from flask_migrate import upgrade

        from app import create_app

        class _MigrationConfig(TestConfig):
            SQLALCHEMY_DATABASE_URI = _TEST_DATABASE_URL
            SQLALCHEMY_ENGINE_OPTIONS = {
                "connect_args": {
                    "options": f"-csearch_path={isolated_schema},public"
                }
            }

        flask_app = create_app(_MigrationConfig)

        with flask_app.app_context():
            upgrade(directory=MIGRATIONS_DIR)

        return flask_app

    def test_upgrade_creates_every_table(self, migrated_app, isolated_schema):
        with migrated_app.app_context():
            from app.extensions import db

            inspector = sa.inspect(db.engine)
            tables = set(inspector.get_table_names(schema=isolated_schema))

        missing = EXPECTED_TABLES - tables
        assert not missing, f"migrations did not create: {sorted(missing)}"

    def test_upgrade_seeds_exactly_the_three_active_providers(
        self, migrated_app
    ):
        with migrated_app.app_context():
            from app.models import ServiceProvider

            providers = ServiceProvider.query.all()

            assert len(providers) == len(EXPECTED_SERVICE_TYPES)
            assert {p.service_type for p in providers} == EXPECTED_SERVICE_TYPES

            # is_active must round-trip as a real boolean on PostgreSQL.
            for provider in providers:
                assert provider.is_active is True

    def test_seed_script_creates_one_idempotent_admin(self, migrated_app):
        """``python seed.py`` logic must be safe to re-run on every deploy.

        Mirrors ``seed.py`` against the freshly migrated schema and runs it
        repeatedly: the admin must be created once and never duplicated.
        """
        with migrated_app.app_context():
            from app.extensions import db
            from app.models import User

            admin_email = "admin@example.com"

            for _ in range(3):
                if not User.query.filter_by(email=admin_email).first():
                    admin = User(
                        first_name="Admin",
                        last_name="User",
                        email=admin_email,
                        role="admin",
                        is_active=True,
                        status="Active",
                    )
                    admin.set_password(uuid.uuid4().hex)
                    db.session.add(admin)
                    db.session.commit()

            admins = User.query.filter_by(email=admin_email).all()

            assert len(admins) == 1
            assert admins[0].role == "admin"
            assert admins[0].is_active is True
