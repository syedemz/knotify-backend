"""
Unit tests for story 9.11 — hard_purge Lambda handler.

TDD: these tests are written BEFORE handler.py to drive the implementation.

Acceptance criteria tested:
  A. Scheduled mode (no user_id in event): executes DELETE FROM users
     WHERE deleted_at IS NOT NULL AND deleted_at < NOW() - INTERVAL '30 days'
  B. Per-user mode (user_id present in event): executes DELETE FROM users
     WHERE user_id = :id AND deleted_at IS NOT NULL  (30-day window dropped)
  C. Per-user mode RETAINS the soft-delete guard (deleted_at IS NOT NULL):
     a user who has NOT been soft-deleted is NOT deleted; rows_affected=0
  D. Return shape: {"rows_affected": <int>, "mode": "scheduled"|"per_user"}
  E. Scheduled mode returns mode="scheduled"; per-user returns mode="per_user"
  F. handler returns rows_affected=0 for a per-user invocation on a non-soft-deleted
     user and exits cleanly (no exception raised — rows_affected=0 is the signal)
  G. DB errors propagate — never swallowed
  H. Per-user query uses a parameterised binding (no string interpolation)
  I. Connection is rolled back on exception (no partial state left open)
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

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


def _make_scheduled_event() -> dict:
    """Empty event — scheduled (CloudWatch rule) invocation."""
    return {}


def _make_per_user_event(user_id: str = "550e8400-e29b-41d4-a716-446655440000") -> dict:
    """Per-user invocation event with an explicit user_id."""
    return {"user_id": user_id}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_conn():
    """
    Patch handler._get_conn so no real DB call is made and the module-level
    connection cache is bypassed entirely.

    The cursor is returned from conn.cursor() called as a context manager
    (`with conn.cursor() as cur:`).  mock_cursor is both the return value
    of cursor() and the context-manager __enter__ value.

    Default rowcount=1 (DELETE matched one row).
    """
    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_cursor.rowcount = 1

    mock_connection = MagicMock()
    mock_connection.closed = False
    mock_connection.cursor.return_value = mock_cursor

    with patch(
        "hard_purge.handler._get_conn",
        return_value=mock_connection,
    ):
        yield mock_connection, mock_cursor


# ---------------------------------------------------------------------------
# Test A: scheduled mode SQL guards
# ---------------------------------------------------------------------------


def test_given_empty_event_when_handler_called_then_scheduled_sql_has_age_guard(
    mock_conn,
):
    """
    AC-A: empty event → scheduled mode → SQL must include
    deleted_at IS NOT NULL AND deleted_at < NOW() - INTERVAL '30 days'
    """
    from hard_purge import handler

    mock_connection, mock_cursor = mock_conn
    handler.handler(_make_scheduled_event(), None)

    assert mock_cursor.execute.called, "cursor.execute was never called"

    executed_sql: str = mock_cursor.execute.call_args[0][0].lower()

    assert "deleted_at is not null" in executed_sql, (
        "Scheduled SQL must include 'deleted_at IS NOT NULL'"
    )
    assert "interval '30 days'" in executed_sql, (
        "Scheduled SQL must include INTERVAL '30 days' age guard"
    )


# ---------------------------------------------------------------------------
# Test B: per-user mode SQL drops the 30-day guard and uses a parameterised user_id
# ---------------------------------------------------------------------------


def test_given_user_id_event_when_handler_called_then_per_user_sql_has_no_age_guard(
    mock_conn,
):
    """
    AC-B: per-user mode SQL must NOT contain the 30-day interval guard,
    but MUST contain the deleted_at IS NOT NULL soft-delete guard.
    """
    from hard_purge import handler

    mock_connection, mock_cursor = mock_conn
    handler.handler(_make_per_user_event(), None)

    executed_sql: str = mock_cursor.execute.call_args[0][0].lower()

    assert "interval '30 days'" not in executed_sql, (
        "Per-user SQL must NOT include 30-day interval guard"
    )
    assert "deleted_at is not null" in executed_sql, (
        "Per-user SQL must RETAIN the soft-delete guard (deleted_at IS NOT NULL)"
    )


# ---------------------------------------------------------------------------
# Test C: per-user mode retains soft-delete guard — returns 0 when not soft-deleted
# ---------------------------------------------------------------------------


def test_given_non_soft_deleted_user_when_per_user_handler_called_then_rows_affected_zero(
    mock_conn,
):
    """
    AC-C + AC-F: when the target user has no deleted_at set (not yet soft-deleted),
    the WHERE guard deleted_at IS NOT NULL prevents the DELETE.
    rows_affected=0 is returned, no exception is raised.
    """
    from hard_purge import handler

    mock_connection, mock_cursor = mock_conn
    mock_cursor.rowcount = 0  # simulate: WHERE guard prevented deletion

    result = handler.handler(_make_per_user_event(), None)

    assert result["rows_affected"] == 0, (
        f"Expected rows_affected=0 for non-soft-deleted user, got {result['rows_affected']}"
    )
    # Must not raise — clean exit is the contract
    assert result["mode"] == "per_user"


# ---------------------------------------------------------------------------
# Test D + E: return shape and mode fields
# ---------------------------------------------------------------------------


def test_given_empty_event_when_handler_called_then_returns_scheduled_mode(
    mock_conn,
):
    """
    AC-D + AC-E: scheduled invocation must return
    {"rows_affected": <int>, "mode": "scheduled"}.
    """
    from hard_purge import handler

    mock_connection, mock_cursor = mock_conn
    mock_cursor.rowcount = 3  # simulate 3 stale rows deleted

    result = handler.handler(_make_scheduled_event(), None)

    assert result["rows_affected"] == 3
    assert result["mode"] == "scheduled"


def test_given_user_id_event_when_handler_called_then_returns_per_user_mode(
    mock_conn,
):
    """
    AC-D + AC-E: per-user invocation must return
    {"rows_affected": <int>, "mode": "per_user"}.
    """
    from hard_purge import handler

    mock_connection, mock_cursor = mock_conn
    mock_cursor.rowcount = 1

    result = handler.handler(_make_per_user_event(user_id="aaaaaaaa-0000-0000-0000-000000000001"), None)

    assert result["rows_affected"] == 1
    assert result["mode"] == "per_user"


# ---------------------------------------------------------------------------
# Test G: DB errors propagate
# ---------------------------------------------------------------------------


def test_given_db_error_when_handler_called_then_exception_propagates(mock_conn):
    """
    AC-G: a psycopg2 OperationalError must propagate — not be swallowed.
    Step Functions needs the exception to trigger retry / Catch logic.
    """
    import psycopg2

    from hard_purge import handler

    mock_connection, mock_cursor = mock_conn
    mock_cursor.execute.side_effect = psycopg2.OperationalError("connection lost")

    with pytest.raises(psycopg2.OperationalError):
        handler.handler(_make_scheduled_event(), None)


# ---------------------------------------------------------------------------
# Test H: per-user query is parameterised (no string interpolation)
# ---------------------------------------------------------------------------


def test_given_per_user_event_when_execute_called_then_user_id_is_passed_as_parameter(
    mock_conn,
):
    """
    AC-H: the user_id must be passed as a bind parameter, NOT interpolated
    into the SQL string. Prevents SQL injection.
    """
    from hard_purge import handler

    mock_connection, mock_cursor = mock_conn
    test_user_id = "dddddddd-1234-1234-1234-dddddddddddd"

    handler.handler(_make_per_user_event(user_id=test_user_id), None)

    call_args = mock_cursor.execute.call_args
    assert len(call_args[0]) >= 2, (
        "cursor.execute must be called with a params argument when in per-user mode"
    )

    sql_string: str = call_args[0][0]
    params = call_args[0][1]

    assert test_user_id not in sql_string, (
        "user_id must NOT be interpolated into the SQL string (SQL injection risk)"
    )
    if isinstance(params, dict):
        assert test_user_id in params.values(), "user_id not found in params dict values"
    else:
        assert test_user_id in params, "user_id not found in params tuple/list"


# ---------------------------------------------------------------------------
# Test I: connection rolled back on exception
# ---------------------------------------------------------------------------


def test_given_db_error_when_handler_called_then_connection_is_rolled_back(mock_conn):
    """
    AC-I: connection must be rolled back if execute raises so no open
    transaction is left on the cached connection object.
    """
    import psycopg2

    from hard_purge import handler

    mock_connection, mock_cursor = mock_conn
    mock_cursor.execute.side_effect = psycopg2.OperationalError("boom")

    with pytest.raises(psycopg2.OperationalError):
        handler.handler(_make_scheduled_event(), None)

    mock_connection.rollback.assert_called_once()


# ---------------------------------------------------------------------------
# Test: scheduled mode does NOT pass bind params (no user_id to bind)
# ---------------------------------------------------------------------------


def test_given_scheduled_event_when_handler_called_then_no_bind_params_needed(
    mock_conn,
):
    """
    Scheduled SQL has no bind variables — user_id is not in play.
    execute is called with the SQL string only (no second argument), OR
    with an empty params tuple.  Either is acceptable.
    """
    from hard_purge import handler

    mock_connection, mock_cursor = mock_conn
    handler.handler(_make_scheduled_event(), None)

    call_args = mock_cursor.execute.call_args[0]
    # Either no second arg, or second arg is empty (None / () / []).
    if len(call_args) >= 2:
        params = call_args[1]
        assert not params, (
            "Scheduled mode must not pass bind params to execute"
        )
