"""
Unit tests for the refresh_deck_view Lambda handler.

These tests are fully isolated — they do NOT require a running database,
AWS credentials, or a deployed Lambda. All external collaborators (DB
connection, psycopg2, boto3) are replaced with in-memory stubs.

Run:
    pytest infrastructure/src/functions/refresh_deck_view/tests/test_handler_unit.py -v

Conventions:
  - Each test name follows the pattern:
      given_<context>_when_<action>_then_<outcome>
  - One behaviour per test.
  - The handler module is imported from a local path, not from a deployed zip.

Test coverage by area:
  A. Advisory lock — lock acquired, refresh executed, log inserted
  B. Advisory lock — lock held, refresh skipped, no log insert
  C. Advisory lock constant — DECK_VIEW_LOCK_KEY is a stable integer
  D. _refresh_deck_view — executes correct SQL statements in order
  E. _try_refresh — correct behavior when advisory lock returns false
"""

from __future__ import annotations

import importlib
import os
import sys
from types import ModuleType
from unittest.mock import MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Module import helper
# ---------------------------------------------------------------------------

_HANDLER_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
)


def _import_handler() -> ModuleType:
    """Import the refresh_deck_view handler module (not cached across tests)."""
    spec = importlib.util.spec_from_file_location(
        "refresh_deck_view_handler_" + str(id(object())),
        os.path.join(_HANDLER_DIR, "handler.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Shared helper — build a fake psycopg2 connection
# ---------------------------------------------------------------------------


def _make_fake_conn(lock_acquired: bool = True):
    """
    Return a MagicMock psycopg2 connection.

    The cursor's fetchone is pre-configured so that pg_try_advisory_lock
    returns (lock_acquired,) on the first call.

    Args:
        lock_acquired: True → lock obtained; False → lock already held.
    """
    fake_conn = MagicMock()
    fake_cur = MagicMock()
    fake_cur.__enter__ = MagicMock(return_value=fake_cur)
    fake_cur.__exit__ = MagicMock(return_value=False)
    # pg_try_advisory_lock returns a single-column row: (bool,)
    fake_cur.fetchone.return_value = (lock_acquired,)
    fake_conn.cursor.return_value = fake_cur
    return fake_conn, fake_cur


# ---------------------------------------------------------------------------
# Area A: Lock acquired — refresh executed, log inserted
# ---------------------------------------------------------------------------


class TestLockAcquiredRefreshExecuted:
    """When pg_try_advisory_lock returns true, the handler must refresh and log."""

    def test_given_lock_acquired_when_handler_invoked_then_returns_200(self):
        mod = _import_handler()
        fake_conn, _ = _make_fake_conn(lock_acquired=True)

        with (
            patch.object(mod, "_get_conn", return_value=fake_conn),
            patch.dict(os.environ, {"DB_SECRET_NAME": "test-secret"}),
        ):
            result = mod.handler({}, None)

        assert result["statusCode"] == 200

    def test_given_lock_acquired_when_handler_invoked_then_refresh_deck_view_called(self):
        """SELECT refresh_deck_view() must appear in the cursor execute calls."""
        mod = _import_handler()
        fake_conn, fake_cur = _make_fake_conn(lock_acquired=True)

        with (
            patch.object(mod, "_get_conn", return_value=fake_conn),
            patch.dict(os.environ, {"DB_SECRET_NAME": "test-secret"}),
        ):
            mod.handler({}, None)

        sql_calls = [str(c) for c in fake_cur.execute.call_args_list]
        assert any("refresh_deck_view" in s for s in sql_calls), (
            f"Expected a call containing 'refresh_deck_view' but saw: {sql_calls}"
        )

    def test_given_lock_acquired_when_handler_invoked_then_refresh_log_inserted(self):
        """INSERT INTO refresh_log must appear in the cursor execute calls."""
        mod = _import_handler()
        fake_conn, fake_cur = _make_fake_conn(lock_acquired=True)

        with (
            patch.object(mod, "_get_conn", return_value=fake_conn),
            patch.dict(os.environ, {"DB_SECRET_NAME": "test-secret"}),
        ):
            mod.handler({}, None)

        sql_calls = [str(c) for c in fake_cur.execute.call_args_list]
        assert any("refresh_log" in s and "INSERT" in s.upper() for s in sql_calls), (
            f"Expected an INSERT INTO refresh_log call but saw: {sql_calls}"
        )

    def test_given_lock_acquired_when_handler_invoked_then_advisory_lock_released(self):
        """pg_advisory_unlock must be called after the refresh."""
        mod = _import_handler()
        fake_conn, fake_cur = _make_fake_conn(lock_acquired=True)

        with (
            patch.object(mod, "_get_conn", return_value=fake_conn),
            patch.dict(os.environ, {"DB_SECRET_NAME": "test-secret"}),
        ):
            mod.handler({}, None)

        sql_calls = [str(c) for c in fake_cur.execute.call_args_list]
        assert any("pg_advisory_unlock" in s for s in sql_calls), (
            f"Expected a pg_advisory_unlock call but saw: {sql_calls}"
        )

    def test_given_lock_acquired_when_handler_invoked_then_operations_ordered_correctly(self):
        """
        The execute call order must be:
          1. pg_try_advisory_lock
          2. refresh_deck_view
          3. INSERT INTO refresh_log
          4. pg_advisory_unlock
        """
        mod = _import_handler()
        fake_conn, fake_cur = _make_fake_conn(lock_acquired=True)

        with (
            patch.object(mod, "_get_conn", return_value=fake_conn),
            patch.dict(os.environ, {"DB_SECRET_NAME": "test-secret"}),
        ):
            mod.handler({}, None)

        sql_calls = [str(c.args[0]) if c.args else str(c) for c in fake_cur.execute.call_args_list]
        lock_idx = next((i for i, s in enumerate(sql_calls) if "pg_try_advisory_lock" in s), None)
        refresh_idx = next((i for i, s in enumerate(sql_calls) if "refresh_deck_view" in s), None)
        log_idx = next((i for i, s in enumerate(sql_calls) if "refresh_log" in s.lower()), None)
        unlock_idx = next((i for i, s in enumerate(sql_calls) if "pg_advisory_unlock" in s), None)

        assert lock_idx is not None, "pg_try_advisory_lock not found"
        assert refresh_idx is not None, "refresh_deck_view not found"
        assert log_idx is not None, "refresh_log INSERT not found"
        assert unlock_idx is not None, "pg_advisory_unlock not found"

        assert lock_idx < refresh_idx, "lock must precede refresh"
        assert refresh_idx < log_idx, "refresh must precede log insert"
        assert log_idx < unlock_idx, "log insert must precede unlock"


# ---------------------------------------------------------------------------
# Area B: Lock held — skip, no log insert
# ---------------------------------------------------------------------------


class TestLockHeldRefreshSkipped:
    """When pg_try_advisory_lock returns false, the handler must skip silently."""

    def test_given_lock_held_when_handler_invoked_then_returns_200(self):
        """Skip is not an error — handler returns 200."""
        mod = _import_handler()
        fake_conn, _ = _make_fake_conn(lock_acquired=False)

        with (
            patch.object(mod, "_get_conn", return_value=fake_conn),
            patch.dict(os.environ, {"DB_SECRET_NAME": "test-secret"}),
        ):
            result = mod.handler({}, None)

        assert result["statusCode"] == 200

    def test_given_lock_held_when_handler_invoked_then_refresh_not_called(self):
        """refresh_deck_view() must NOT be called when the lock is held."""
        mod = _import_handler()
        fake_conn, fake_cur = _make_fake_conn(lock_acquired=False)

        with (
            patch.object(mod, "_get_conn", return_value=fake_conn),
            patch.dict(os.environ, {"DB_SECRET_NAME": "test-secret"}),
        ):
            mod.handler({}, None)

        sql_calls = [str(c) for c in fake_cur.execute.call_args_list]
        assert not any("refresh_deck_view" in s for s in sql_calls), (
            f"refresh_deck_view must not be called when lock held, but saw: {sql_calls}"
        )

    def test_given_lock_held_when_handler_invoked_then_refresh_log_not_inserted(self):
        """INSERT INTO refresh_log must NOT happen on a skip."""
        mod = _import_handler()
        fake_conn, fake_cur = _make_fake_conn(lock_acquired=False)

        with (
            patch.object(mod, "_get_conn", return_value=fake_conn),
            patch.dict(os.environ, {"DB_SECRET_NAME": "test-secret"}),
        ):
            mod.handler({}, None)

        sql_calls = [str(c) for c in fake_cur.execute.call_args_list]
        assert not any("refresh_log" in s and "INSERT" in s.upper() for s in sql_calls), (
            f"INSERT INTO refresh_log must not be called on skip, but saw: {sql_calls}"
        )

    def test_given_lock_held_when_handler_invoked_then_body_indicates_skipped(self):
        """Response body must indicate the refresh was skipped."""
        import json
        mod = _import_handler()
        fake_conn, _ = _make_fake_conn(lock_acquired=False)

        with (
            patch.object(mod, "_get_conn", return_value=fake_conn),
            patch.dict(os.environ, {"DB_SECRET_NAME": "test-secret"}),
        ):
            result = mod.handler({}, None)

        body = json.loads(result["body"])
        assert body.get("status") == "skipped", (
            f"Expected status='skipped' in body, got: {body}"
        )


# ---------------------------------------------------------------------------
# Area C: DECK_VIEW_LOCK_KEY is a stable integer
# ---------------------------------------------------------------------------


class TestLockKeyConstant:
    """The advisory lock key must be a stable non-zero integer constant."""

    def test_given_handler_module_when_imported_then_lock_key_is_integer(self):
        mod = _import_handler()
        assert isinstance(mod.DECK_VIEW_LOCK_KEY, int), (
            f"DECK_VIEW_LOCK_KEY must be an int, got {type(mod.DECK_VIEW_LOCK_KEY)}"
        )

    def test_given_handler_module_when_imported_then_lock_key_is_non_zero(self):
        mod = _import_handler()
        assert mod.DECK_VIEW_LOCK_KEY != 0, "DECK_VIEW_LOCK_KEY must be non-zero"

    def test_given_two_imports_when_lock_key_compared_then_stable(self):
        """Same constant value across multiple imports (not random)."""
        mod1 = _import_handler()
        mod2 = _import_handler()
        assert mod1.DECK_VIEW_LOCK_KEY == mod2.DECK_VIEW_LOCK_KEY


# ---------------------------------------------------------------------------
# Area D: pg_try_advisory_lock uses the DECK_VIEW_LOCK_KEY constant
# ---------------------------------------------------------------------------


class TestAdvisoryLockUsesConstant:
    """The advisory lock SQL must reference DECK_VIEW_LOCK_KEY by value."""

    def test_given_lock_acquired_when_handler_invoked_then_lock_sql_contains_key_value(self):
        mod = _import_handler()
        fake_conn, fake_cur = _make_fake_conn(lock_acquired=True)
        lock_key = mod.DECK_VIEW_LOCK_KEY

        with (
            patch.object(mod, "_get_conn", return_value=fake_conn),
            patch.dict(os.environ, {"DB_SECRET_NAME": "test-secret"}),
        ):
            mod.handler({}, None)

        # Find the pg_try_advisory_lock call and assert the lock key appears
        lock_calls = [
            str(c)
            for c in fake_cur.execute.call_args_list
            if "pg_try_advisory_lock" in str(c)
        ]
        assert lock_calls, "No pg_try_advisory_lock call found"
        # The lock key value should appear somewhere in the call args
        assert any(str(lock_key) in s for s in lock_calls), (
            f"Expected lock key {lock_key} in lock call but saw: {lock_calls}"
        )
