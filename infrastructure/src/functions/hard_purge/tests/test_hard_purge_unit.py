"""
Unit tests for story 9.11 — hard_purge Lambda handler.

Acceptance criteria tested:
  A. Scheduled mode (no user_id in event): SELECT eligible user_ids with a
     30-day age guard, then DELETE each under an RLS context.
  B. Per-user mode (user_id present in event): single DELETE under an RLS
     context bound to that user.  30-day window dropped.
  C. Per-user mode RETAINS the soft-delete guard (deleted_at IS NOT NULL):
     a user who has NOT been soft-deleted is NOT deleted; rows_affected=0
  D. Return shape: {"rows_affected": <int>, "mode": "scheduled"|"per_user"}
  E. Scheduled mode returns mode="scheduled"; per-user returns mode="per_user"
  F. handler returns rows_affected=0 for a per-user invocation on a non-soft-deleted
     user and exits cleanly (no exception raised — rows_affected=0 is the signal)
  G. DB errors propagate — never swallowed
  H. Per-user DELETE uses a parameterised binding (no string interpolation)
  I. Each DELETE runs inside knotify_db.rls_context with the target user_id —
     this is what the migration 0017 DELETE policy requires.
"""

from __future__ import annotations

import contextlib
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
def mock_conn(monkeypatch):
    """
    Patch handler._get_conn so no real DB call is made.

    Also patches knotify_db.rls_context to a no-op context manager so the
    unit tests focus on the SQL the handler issues, not the transaction
    bookkeeping (which is exercised end-to-end by the integration tests).

    The cursor returned from `with conn.cursor() as cur:` is a single
    MagicMock — call_args / call_args_list capture every SQL the handler
    executes across all `with conn.cursor()` blocks.

    Default rowcount=1 (DELETE matched one row).
    Default fetchall=[] (no eligible users in scheduled mode).
    """
    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_cursor.rowcount = 1
    mock_cursor.fetchall.return_value = []

    mock_connection = MagicMock()
    mock_connection.closed = False
    mock_connection.cursor.return_value = mock_cursor

    @contextlib.contextmanager
    def _noop_rls_context(conn, user_id, user_sex):
        yield

    monkeypatch.setattr(
        "hard_purge.handler.knotify_db.rls_context",
        _noop_rls_context,
    )

    with patch(
        "hard_purge.handler._get_conn",
        return_value=mock_connection,
    ):
        yield mock_connection, mock_cursor


def _executed_sql_strings(mock_cursor) -> list[str]:
    """Return every SQL string passed to cursor.execute, lowercased."""
    return [
        call.args[0].lower()
        for call in mock_cursor.execute.call_args_list
        if call.args  # defensive: ignore calls with no positional args
    ]


# ---------------------------------------------------------------------------
# Test A: scheduled mode SQL guards
# ---------------------------------------------------------------------------


def test_given_empty_event_when_handler_called_then_eligibility_select_has_age_guard(
    mock_conn,
):
    """
    AC-A: empty event → scheduled mode → the eligibility SELECT must include
    deleted_at IS NOT NULL AND deleted_at < NOW() - INTERVAL '30 days'.
    """
    from hard_purge import handler

    _mock_connection, mock_cursor = mock_conn
    handler.handler(_make_scheduled_event(), None)

    sqls = _executed_sql_strings(mock_cursor)

    assert any(
        "deleted_at is not null" in s and "interval '30 days'" in s
        for s in sqls
    ), (
        "Scheduled mode must run an eligibility SELECT with both "
        "'deleted_at IS NOT NULL' and INTERVAL '30 days'. Executed SQLs: "
        f"{sqls}"
    )


# ---------------------------------------------------------------------------
# Test B: per-user mode SQL drops the 30-day guard, retains soft-delete guard
# ---------------------------------------------------------------------------


def test_given_user_id_event_when_handler_called_then_per_user_sql_has_no_age_guard(
    mock_conn,
):
    """
    AC-B + AC-C: per-user mode DELETE must NOT contain the 30-day interval
    guard, but MUST contain the deleted_at IS NOT NULL soft-delete guard.
    """
    from hard_purge import handler

    _mock_connection, mock_cursor = mock_conn
    handler.handler(_make_per_user_event(), None)

    sqls = _executed_sql_strings(mock_cursor)

    assert not any("interval '30 days'" in s for s in sqls), (
        f"Per-user SQL must NOT include 30-day interval guard. SQLs: {sqls}"
    )
    assert any(
        "delete from users" in s and "deleted_at is not null" in s
        for s in sqls
    ), (
        "Per-user SQL must DELETE FROM users with the soft-delete guard. "
        f"SQLs: {sqls}"
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

    _mock_connection, mock_cursor = mock_conn
    mock_cursor.rowcount = 0  # simulate: WHERE guard prevented deletion

    result = handler.handler(_make_per_user_event(), None)

    assert result["rows_affected"] == 0, (
        f"Expected rows_affected=0 for non-soft-deleted user, got {result['rows_affected']}"
    )
    assert result["mode"] == "per_user"


# ---------------------------------------------------------------------------
# Test D + E: return shape and mode fields
# ---------------------------------------------------------------------------


def test_given_empty_event_with_eligible_users_when_handler_called_then_total_rows_returned(
    mock_conn,
):
    """
    AC-D + AC-E: scheduled invocation returns the sum of per-row deletions,
    with mode='scheduled'.  Three eligible users → three DELETEs → rows_affected=3.
    """
    from hard_purge import handler

    _mock_connection, mock_cursor = mock_conn
    mock_cursor.fetchall.return_value = [
        ("aaaaaaaa-0000-0000-0000-000000000001",),
        ("aaaaaaaa-0000-0000-0000-000000000002",),
        ("aaaaaaaa-0000-0000-0000-000000000003",),
    ]
    mock_cursor.rowcount = 1  # each DELETE removes 1 row

    result = handler.handler(_make_scheduled_event(), None)

    assert result["rows_affected"] == 3
    assert result["mode"] == "scheduled"


def test_given_empty_event_with_no_eligible_users_when_handler_called_then_zero_rows(
    mock_conn,
):
    """
    Empty eligibility set → no DELETEs → rows_affected=0, mode='scheduled'.
    """
    from hard_purge import handler

    _mock_connection, mock_cursor = mock_conn
    mock_cursor.fetchall.return_value = []  # no eligible users

    result = handler.handler(_make_scheduled_event(), None)

    assert result["rows_affected"] == 0
    assert result["mode"] == "scheduled"


def test_given_user_id_event_when_handler_called_then_returns_per_user_mode(
    mock_conn,
):
    """
    AC-D + AC-E: per-user invocation must return
    {"rows_affected": <int>, "mode": "per_user"}.
    """
    from hard_purge import handler

    _mock_connection, mock_cursor = mock_conn
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

    _mock_connection, mock_cursor = mock_conn
    mock_cursor.execute.side_effect = psycopg2.OperationalError("connection lost")

    with pytest.raises(psycopg2.OperationalError):
        handler.handler(_make_per_user_event(), None)


# ---------------------------------------------------------------------------
# Test H: per-user DELETE is parameterised (no string interpolation)
# ---------------------------------------------------------------------------


def test_given_per_user_event_when_execute_called_then_user_id_is_passed_as_parameter(
    mock_conn,
):
    """
    AC-H: the user_id must be passed as a bind parameter, NOT interpolated
    into the SQL string. Prevents SQL injection.
    """
    from hard_purge import handler

    _mock_connection, mock_cursor = mock_conn
    test_user_id = "dddddddd-1234-1234-1234-dddddddddddd"

    handler.handler(_make_per_user_event(user_id=test_user_id), None)

    delete_calls = [
        call
        for call in mock_cursor.execute.call_args_list
        if call.args and "delete from users" in call.args[0].lower()
    ]
    assert delete_calls, "expected at least one DELETE FROM users execute call"

    sql_string: str = delete_calls[-1].args[0]
    params = delete_calls[-1].args[1] if len(delete_calls[-1].args) >= 2 else None

    assert test_user_id not in sql_string, (
        "user_id must NOT be interpolated into the SQL string (SQL injection risk)"
    )
    assert params is not None, "DELETE must be invoked with bind params"
    if isinstance(params, dict):
        assert test_user_id in params.values(), "user_id not found in params dict values"
    else:
        assert test_user_id in params, "user_id not found in params tuple/list"


# ---------------------------------------------------------------------------
# Test I: per-user DELETE runs inside rls_context with the target user_id
# ---------------------------------------------------------------------------


def test_given_per_user_event_when_handler_called_then_rls_context_bound_to_target_user(
    monkeypatch,
):
    """
    AC-I: the DELETE must run inside knotify_db.rls_context(conn, user_id, "")
    so the migration 0017 DELETE policy
    (user_id = current_setting('app.requesting_user_id', true)::uuid) matches.
    """
    from hard_purge import handler

    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_cursor.rowcount = 1
    mock_cursor.fetchall.return_value = []

    mock_connection = MagicMock()
    mock_connection.closed = False
    mock_connection.cursor.return_value = mock_cursor

    rls_calls: list[tuple] = []

    @contextlib.contextmanager
    def _capturing_rls_context(conn, user_id, user_sex):
        rls_calls.append((user_id, user_sex))
        yield

    monkeypatch.setattr(
        "hard_purge.handler.knotify_db.rls_context",
        _capturing_rls_context,
    )

    target_user = "11111111-2222-3333-4444-555555555555"
    with patch("hard_purge.handler._get_conn", return_value=mock_connection):
        handler.handler(_make_per_user_event(user_id=target_user), None)

    assert (target_user, "") in rls_calls, (
        f"DELETE must be wrapped in rls_context({target_user!r}, ''). "
        f"rls_context calls: {rls_calls}"
    )
