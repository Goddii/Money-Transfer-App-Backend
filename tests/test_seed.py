"""Tests for the first-start admin seeding script.

Covers the security contract of ``seed.py``:

* the administrator password comes only from ``ADMIN_PASSWORD`` and is never
  hardcoded in the source;
* seeding fails loudly (non-zero exit) when that variable is missing, instead
  of silently installing a publicly known default credential;
* the password never reaches stdout, stderr, the logs, or an exception
  message;
* seeding stays idempotent, so every redeploy is safe.

No test contains a real or reusable password: each one generates a random
throwaway value and never asserts on its content except to prove it is absent
from output.
"""

import os
import re
import uuid

import pytest

import seed
from app.extensions import db
from app.models import User

SEED_SOURCE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "seed.py"
)


def _throwaway_password():
    """A random password that exists only for the lifetime of one test."""
    return f"Seed-{uuid.uuid4().hex}"


@pytest.fixture
def seed_env(monkeypatch):
    """Configure the seed environment and return the generated password."""

    def _seed_env(password=None, email=None):
        password = password if password is not None else _throwaway_password()

        monkeypatch.setenv(seed.ADMIN_PASSWORD_ENV, password)

        if email is None:
            monkeypatch.delenv(seed.ADMIN_EMAIL_ENV, raising=False)
        else:
            monkeypatch.setenv(seed.ADMIN_EMAIL_ENV, email)

        return password

    return _seed_env


class TestAdminCreation:
    """An admin is created when the required configuration is present."""

    def test_creates_admin_with_expected_role_and_fields(self, app, seed_env):
        seed_env()

        with app.app_context():
            admin, created = seed.seed_admin()

            assert created is True
            assert admin.email == seed.DEFAULT_ADMIN_EMAIL
            assert admin.role == "admin"
            assert admin.is_active is True
            assert admin.status == "Active"
            assert admin.first_name == "Admin"
            assert admin.last_name == "User"
            assert admin.id is not None

    def test_admin_is_persisted_and_can_authenticate(self, app, seed_env):
        password = seed_env()

        with app.app_context():
            seed.seed_admin()

            stored = User.query.filter_by(
                email=seed.DEFAULT_ADMIN_EMAIL
            ).one()

            # The password is stored only as a hash, never in plaintext.
            assert stored.password_hash != password
            assert password not in stored.password_hash
            assert stored.check_password(password) is True

    def test_honours_custom_admin_email(self, app, seed_env):
        seed_env(email="ops-admin@vyloc.test")

        with app.app_context():
            admin, created = seed.seed_admin()

            assert created is True
            assert admin.email == "ops-admin@vyloc.test"
            assert admin.role == "admin"

    def test_admin_email_is_normalised_to_lowercase(self, app, seed_env):
        """Login lowercases the email, so seeding must match that."""
        seed_env(email="  OPS-Admin@Vyloc.TEST  ")

        with app.app_context():
            admin, _ = seed.seed_admin()

            assert admin.email == "ops-admin@vyloc.test"

    def test_blank_admin_email_falls_back_to_default(self, app, seed_env):
        seed_env(email="   ")

        with app.app_context():
            admin, _ = seed.seed_admin()

            assert admin.email == seed.DEFAULT_ADMIN_EMAIL


class TestIdempotency:
    """Repeated seeding must never duplicate the administrator."""

    def test_repeated_seeding_creates_exactly_one_admin(self, app, seed_env):
        seed_env()

        with app.app_context():
            first, created_first = seed.seed_admin()
            first_id = first.id

            assert created_first is True

            for _ in range(3):
                again, created_again = seed.seed_admin()

                assert created_again is False
                assert again.id == first_id

            assert User.query.filter_by(role="admin").count() == 1
            assert (
                User.query.filter_by(email=seed.DEFAULT_ADMIN_EMAIL).count()
                == 1
            )

    def test_existing_admin_password_is_not_rotated(self, app, seed_env):
        """Re-running the seed must not overwrite the live admin credential."""
        original_password = seed_env()

        with app.app_context():
            admin, _ = seed.seed_admin()
            original_hash = admin.password_hash

        # A later deploy supplies a different value in the environment.
        rotated_password = _throwaway_password()
        os.environ[seed.ADMIN_PASSWORD_ENV] = rotated_password

        with app.app_context():
            admin, created = seed.seed_admin()

            assert created is False
            assert admin.password_hash == original_hash
            assert admin.check_password(original_password) is True

    def test_main_reports_existing_admin_on_second_run(
        self, app, seed_env, monkeypatch, capsys
    ):
        seed_env()
        monkeypatch.setattr(seed, "create_app", lambda: app)

        assert seed.main() == 0
        assert seed.main() == 0

        output = capsys.readouterr().out

        assert "Admin already exists" in output

        with app.app_context():
            assert User.query.filter_by(role="admin").count() == 1


class TestMissingCredentialsFailSafely:
    """A missing password must stop the deploy, not weaken it."""

    def test_seed_admin_raises_when_password_missing(
        self, app, monkeypatch
    ):
        monkeypatch.delenv(seed.ADMIN_PASSWORD_ENV, raising=False)
        monkeypatch.delenv(seed.ADMIN_EMAIL_ENV, raising=False)

        with app.app_context():
            with pytest.raises(seed.SeedConfigError) as excinfo:
                seed.seed_admin()

            assert seed.ADMIN_PASSWORD_ENV in str(excinfo.value)

            # Nothing was written: no half-seeded administrator.
            assert User.query.filter_by(role="admin").count() == 0

    @pytest.mark.parametrize("blank", ["", "   ", "\t", "\n"])
    def test_blank_password_is_treated_as_missing(
        self, app, monkeypatch, blank
    ):
        monkeypatch.setenv(seed.ADMIN_PASSWORD_ENV, blank)

        with app.app_context():
            with pytest.raises(seed.SeedConfigError):
                seed.seed_admin()

            assert User.query.filter_by(role="admin").count() == 0

    def test_main_exits_non_zero_when_password_missing(
        self, app, monkeypatch, capsys
    ):
        """A non-zero exit breaks the && chain so gunicorn never starts."""
        monkeypatch.delenv(seed.ADMIN_PASSWORD_ENV, raising=False)
        monkeypatch.setattr(seed, "create_app", lambda: app)

        exit_code = seed.main()

        assert exit_code == 1

        captured = capsys.readouterr()

        assert "Seeding failed" in captured.err
        assert seed.ADMIN_PASSWORD_ENV in captured.err

        with app.app_context():
            assert User.query.filter_by(role="admin").count() == 0

    def test_get_admin_password_has_no_fallback_value(self, monkeypatch):
        """There must be no fallback value for the password, ever."""
        monkeypatch.delenv(seed.ADMIN_PASSWORD_ENV, raising=False)

        with pytest.raises(seed.SeedConfigError):
            seed.get_admin_password()

        # ...whereas the email intentionally does have a demo default.
        monkeypatch.delenv(seed.ADMIN_EMAIL_ENV, raising=False)

        assert seed.get_admin_email() == seed.DEFAULT_ADMIN_EMAIL


class TestPasswordIsNeverDisclosed:
    """The password must not leak into output, logs, or exceptions."""

    def test_main_does_not_print_the_password(
        self, app, seed_env, monkeypatch, capsys, caplog
    ):
        password = seed_env()
        monkeypatch.setattr(seed, "create_app", lambda: app)

        with caplog.at_level("DEBUG"):
            assert seed.main() == 0

        captured = capsys.readouterr()

        assert password not in captured.out
        assert password not in captured.err
        assert password not in caplog.text

        # The useful, non-sensitive identifiers are still reported.
        assert "Admin created" in captured.out
        assert seed.DEFAULT_ADMIN_EMAIL in captured.out
        assert "role=admin" in captured.out
        assert "password" not in captured.out.lower()

    def test_seed_admin_logs_nothing_sensitive(
        self, app, seed_env, caplog, capsys
    ):
        password = seed_env()

        with app.app_context():
            with caplog.at_level("DEBUG"):
                seed.seed_admin()

        captured = capsys.readouterr()

        assert password not in caplog.text
        assert password not in captured.out
        assert password not in captured.err

    def test_error_message_contains_no_supplied_value(self, app, monkeypatch):
        """A blank-but-present value must not be echoed back."""
        sentinel = "   "
        monkeypatch.setenv(seed.ADMIN_PASSWORD_ENV, sentinel)

        with app.app_context():
            with pytest.raises(seed.SeedConfigError) as excinfo:
                seed.seed_admin()

        message = str(excinfo.value)

        assert seed.ADMIN_PASSWORD_ENV in message
        assert "Refusing to seed" in message

    def test_source_contains_no_hardcoded_password(self):
        """Regression guard: set_password must never take a literal."""
        source = open(SEED_SOURCE_PATH, encoding="utf-8").read()

        literal_calls = re.findall(r"set_password\(\s*['\"]", source)

        assert not literal_calls, (
            "seed.py must not pass a literal string to set_password(); "
            "the password must come from the environment"
        )

    def test_source_never_prints_a_password(self):
        """No print/log statement in seed.py may reference the password."""
        source = open(SEED_SOURCE_PATH, encoding="utf-8").read()

        emitting = [
            line.strip()
            for line in source.splitlines()
            if re.search(r"\b(print|logger|logging|warning|info|debug)\s*\(", line)
            and "password" in line.lower()
        ]

        assert not emitting, f"seed.py may leak the password via: {emitting}"
