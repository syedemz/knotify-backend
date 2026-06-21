"""
Unit tests for story 9.5 — soft_delete_aurora Lambda handler.

TDD: these tests were written BEFORE handler.py to drive the implementation.

Acceptance criteria tested:
  A. handler executes the §11.1 step-2 UPDATE SQL with correct columns set:
       deleted_at=NOW(), email=NULL, phone_number=NULL, photo_url=NULL,
       chosen_profile_avatar=NULL, preferences='{}', preference_vector=NULL,
       username='[deleted-user]', first_name='Deleted', last_name='User'
       WHERE user_id=:id AND deleted_at IS NULL
  B. The UPDATE is conditional on deleted_at IS NULL so a second invocation
     on an already-soft-deleted user returns rows_affected=0 (idempotent no-op).
  C. handler returns {"user_id": ..., "rows_affected": <int>} on success.
  D. handler returns rows_affected=1 when the row exists and is not yet deleted.
  E. handler returns rows_affected=0 when the row is already soft-deleted.
  F. A missing user_id in the event raises KeyError.
  G. DB errors propagate — they are NOT silently swallowed.
  H. The UPDATE uses a parameterised query (no string interpolation of user_id).
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Environment variables required before module import
# ---------------------------------------------------------------------------

os.environ.setdefault("DB_SECRET_NAME", "knotify-test-app-user-credential")
os.environ.setdefault("AURORA_HOST", "localhost")
os.environ.setdefault("AURORA_PORT", "5432")
os.environ.setdefault("AURORA_DBNAME", "knotify_test")
os.environ.setdefault("AWS_DEFAULT_REGION", "eu-central-1")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(user_id: str = "550e8400-e29b-41d4-a716-446655440000") -> dict:
    """Build a minimal Step Functions task input event."""
    return {"user_id": user_id}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_conn():
    """
    Patch handler._get_conn so no real DB call is made and the module-level
    connection cache is bypassed entirely.

    The cursor is returned from conn.cursor() which is called as a context
    manager (`with conn.cursor() as cur:`).  We configure mock_cursor as both
    the return value of cursor() AND the context-manager __enter__ value so
    that `cur` inside the handler is this mock_cursor.

    Default rowcount=1 (UPDATE matched one row).  Tests that need rowcount=0
    set mock_cursor.rowcount = 0 before calling the handler.
    """
    mock_cursor = MagicMock()
    # `with conn.cursor() as cur:` calls cursor().__enter__()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_cursor.rowcount = 1

    mock_connection = MagicMock()
    mock_connection.closed = False
    # cursor() returns mock_cursor; mock_cursor is its own context manager.
    mock_connection.cursor.return_value = mock_cursor

    with patch(
        "soft_delete_aurora.handler._get_conn",
        return_value=mock_connection,
    ):
        yield mock_connection, mock_cursor


# ---------------------------------------------------------------------------
# Test A: correct SQL columns are set
# ---------------------------------------------------------------------------


def test_given_fresh_user_when_handler_called_then_update_sets_all_pii_columns_null(
    mock_conn,
):
    """
    AC-A: the executed SQL UPDATE sets deleted_at, nulls PII columns, and resets
    username/first_name/last_name to sentinel values.
    """
    from soft_delete_aurora import handler

    mock_connection, mock_cursor = mock_conn
    handler.handler(_make_event(), None)

    # The cursor must have been used (execute called at least once).
    assert mock_cursor.execute.called, "cursor.execute was never called"

    # Inspect the SQL string passed to execute.
    executed_sql: str = mock_cursor.execute.call_args[0][0].lower()

    # All required columns must appear in the SET clause.
    for col in (
        "deleted_at",
        "email",
        "phone_number",
        "photo_url",
        "chosen_profile_avatar",
        "preferences",
        "preference_vector",
        "username",
        "first_name",
        "last_name",
    ):
        assert col in executed_sql, f"Column '{col}' missing from UPDATE SQL"

    # WHERE clause must guard on deleted_at IS NULL (idempotency guard).
    assert "deleted_at is null" in executed_sql, (
        "UPDATE SQL must have WHERE deleted_at IS NULL guard"
    )


# ---------------------------------------------------------------------------
# Test B/C/D: happy path — fresh user returns rows_affected=1
# ---------------------------------------------------------------------------


def test_given_fresh_user_when_handler_called_then_returns_rows_affected_one(
    mock_conn,
):
    """
    AC-C + AC-D: handler returns {"user_id": ..., "rows_affected": 1} for a
    fresh (not yet soft-deleted) user.
    """
    from soft_delete_aurora import handler

    mock_connection, mock_cursor = mock_conn
    mock_cursor.rowcount = 1

    result = handler.handler(_make_event(user_id="aaaaaaaa-0000-0000-0000-000000000001"), None)

    assert result["user_id"] == "aaaaaaaa-0000-0000-0000-000000000001"
    assert result["rows_affected"] == 1


# ---------------------------------------------------------------------------
# Test B/C/E: idempotent — already-soft-deleted returns rows_affected=0
# ---------------------------------------------------------------------------


def test_given_already_deleted_user_when_handler_called_then_returns_rows_affected_zero(
    mock_conn,
):
    """
    AC-B + AC-E: when the row has deleted_at already set the WHERE guard
    prevents the UPDATE from matching any row.  rows_affected=0 is returned
    as a success (no-op), not an error.
    """
    from soft_delete_aurora import handler

    mock_connection, mock_cursor = mock_conn
    mock_cursor.rowcount = 0

    result = handler.handler(_make_event(user_id="bbbbbbbb-0000-0000-0000-000000000002"), None)

    assert result["user_id"] == "bbbbbbbb-0000-0000-0000-000000000002"
    assert result["rows_affected"] == 0


# ---------------------------------------------------------------------------
# Test F: missing user_id raises KeyError
# ---------------------------------------------------------------------------


def test_given_event_without_user_id_when_handler_called_then_raises():
    """
    AC-F: a missing user_id field in the event must raise — do NOT swallow it.
    Step Functions will mark the task as failed, enabling retry logic.
    """
    from soft_delete_aurora import handler

    with pytest.raises(KeyError):
        handler.handler({}, None)


# ---------------------------------------------------------------------------
# Test G: DB errors propagate
# ---------------------------------------------------------------------------


def test_given_db_error_when_handler_called_then_exception_propagates(mock_conn):
    """
    AC-G: a psycopg2 OperationalError (e.g. lost connection) must propagate
    rather than being swallowed.  Step Functions needs the exception to trigger
    retry / Catch logic.
    """
    import psycopg2

    from soft_delete_aurora import handler

    mock_connection, mock_cursor = mock_conn
    mock_cursor.execute.side_effect = psycopg2.OperationalError("connection lost")

    with pytest.raises(psycopg2.OperationalError):
        handler.handler(_make_event(), None)


# ---------------------------------------------------------------------------
# Test H: parameterised query — user_id is NOT interpolated into the string
# ---------------------------------------------------------------------------


def test_given_handler_when_execute_called_then_user_id_is_passed_as_parameter(
    mock_conn,
):
    """
    AC-H: the user_id is passed as a bind parameter to cursor.execute, NOT
    embedded in the SQL string.  This prevents SQL injection.
    """
    from soft_delete_aurora import handler

    mock_connection, mock_cursor = mock_conn
    test_user_id = "cccccccc-1234-1234-1234-cccccccccccc"

    handler.handler(_make_event(user_id=test_user_id), None)

    # Execute must have been called with at least 2 positional arguments:
    # (sql_string, params_tuple_or_dict).
    call_args = mock_cursor.execute.call_args
    assert len(call_args[0]) >= 2, (
        "cursor.execute was not called with a params argument — user_id must be parameterised"
    )

    # The user_id must appear in the params, NOT in the SQL string.
    sql_string: str = call_args[0][0]
    params = call_args[0][1]

    assert test_user_id not in sql_string, (
        "user_id must NOT be interpolated into the SQL string (SQL injection risk)"
    )
    # params can be a tuple, list, or dict — check containment accordingly.
    if isinstance(params, dict):
        assert test_user_id in params.values(), "user_id not found in params dict values"
    else:
        assert test_user_id in params, "user_id not found in params tuple/list"


# ---------------------------------------------------------------------------
# Test: connection is rolled back on exception (no partial writes)
# ---------------------------------------------------------------------------


def test_given_db_error_when_handler_called_then_connection_is_rolled_back(mock_conn):
    """
    The connection must be rolled back if execute raises so that any partial
    state (BEGIN was called) does not leave an open transaction on the connection.
    """
    import psycopg2

    from soft_delete_aurora import handler

    mock_connection, mock_cursor = mock_conn
    mock_cursor.execute.side_effect = psycopg2.OperationalError("boom")

    with pytest.raises(psycopg2.OperationalError):
        handler.handler(_make_event(), None)

    mock_connection.rollback.assert_called_once()
