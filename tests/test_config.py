"""Tests for safe configuration parsing (R4)."""

import os

from app.config import _parse_int_env


def _with_env(name, value, fn):
    os.environ[name] = value
    try:
        return fn()
    finally:
        os.environ.pop(name, None)


def test_parse_int_env_uses_default_when_missing():
    assert _parse_int_env("VYLOC_TEST_MISSING_VAR", 30) == 30


def test_parse_int_env_uses_default_when_empty():
    assert _with_env("VYLOC_TEST_TIMEOUT", "", lambda: _parse_int_env("VYLOC_TEST_TIMEOUT", 30)) == 30


def test_parse_int_env_uses_default_when_invalid():
    assert _with_env("VYLOC_TEST_TIMEOUT", "not-a-number", lambda: _parse_int_env("VYLOC_TEST_TIMEOUT", 30)) == 30


def test_parse_int_env_parses_valid_value():
    assert _with_env("VYLOC_TEST_TIMEOUT", "45", lambda: _parse_int_env("VYLOC_TEST_TIMEOUT", 30)) == 45
