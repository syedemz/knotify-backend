"""
Integration tests for story 9.11 — hard_purge Lambda handler.

Requires a running docker-compose postgres container with all migrations applied
and local_init.sql executed (the db_with_migrations fixture from the layers/db
conftest applies this automatically).

Run:
    pytest infrastructure/src/functions/hard_purge/tests/test_hard_purge_integration.py \\
           -v -m integration

Skip in unit test runs (default):
    pytest -m "not integration"

Acceptance criteria covered (story 9.11):

  IT-9.11-1  Scheduled mode — users row and all CASCADE-linked rows purged:
               - Insert one user, one row in EACH of (siblings, friendships,
                 friend_requests, bookmarks, blocks) referencing that user.
               - Soft-delete the user (deleted_at = NOW() - INTERVAL '31 days').
               - Invoke Lambda (scheduled mode, no user_id).
               - Assert users row is GONE.
               - Assert ALL 5 cascading-table rows for that user_id are GONE.

  IT-9.11-2  Scheduled mode respects the 30-day window — 29-day-old user stays:
               - Insert a second user with deleted_at = NOW() - INTERVAL '29 days'.
               - Invoke Lambda (scheduled mode).
               - Assert that second user row still EXISTS (window not exceeded).

  IT-9.11-3  Per-user mode bypasses the 30-day window:
               - Insert a user, soft-delete them only 1 day ago.
               - Invoke Lambda with explicit user_id input.
               - Assert the users row is GONE (window bypassed).

  IT-9.11-4  Per-user mode retains soft-delete guard:
               - Insert a fresh user (no deleted_at).
               - Invoke Lambda with that user's user_id.
               - Assert rows_affected=0 and the user row still EXISTS.
"""

from __future__ import annotations

import os
import sys
import uuid
import datetime

import pytest

# ---------------------------------------------------------------------------
# Skip gate — integration tests require a running postgres container
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.integration

_PGHOST = os.environ.get("PGHOST", "localhost")
_PGPORT = int(os.environ.get("PGPORT", "5432"))
_PGDATABASE = os.environ.get("PGDATABASE", "knotify")
_PGPASSWORD = os.environ.get("PGPASSWORD", "knotify")
_APP_USER_PASS = os.environ.get("APP_USER_PASSWORD", "app_user")


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _master_conn():
    """Direct psycopg2 connection as the master user (bypasses RLS)."""
    import psycopg2

    conn = psycopg2.connect(
        host=_PGHOST,
        port=_PGPORT,
        dbname=_PGDATABASE,
        user="knotify",
        password=_PGPASSWORD,
    )
    conn.autocommit = True
    return conn


def _app_conn_dict() -> dict:
    """Return a connection-params dict for the app_user."""
    return {
        "host": _PGHOST,
        "port": _PGPORT,
        "dbname": _PGDATABASE,
        "username": "app_user",
        "password": _APP_USER_PASS,
    }


def _invoke_handler(event: dict) -> dict:
    """
    Invoke the hard_purge handler with a local DB connection (bypasses
    Secrets Manager).  Resets the module-level connection cache before and
    after each call so successive test invocations start with a clean state.
    """
    import knotify_db
    import hard_purge.handler as mod

    mod._conn = None
    _conn = knotify_db.get_connection(_app_conn_dict())

    def _local_get_conn():
        return _conn

    original = mod._get_conn
    mod._get_conn = _local_get_conn
    try:
        from hard_purge import handler as h
        result = h.handler(event, None)
    finally:
        mod._get_conn = original
        _conn.close()
        mod._conn = None

    return result


# ---------------------------------------------------------------------------
# Shared INSERT helpers
# ---------------------------------------------------------------------------


def _insert_user(conn, user_id: uuid.UUID, deleted_at_offset_days: int | None = None):
    """
    Insert a minimal user row.  If deleted_at_offset_days is given,
    set deleted_at = NOW() - INTERVAL '<n> days'.
    """
    if deleted_at_offset_days is not None:
        sql = """
            INSERT INTO users (
                user_id, email, sex, birthday, religion, deleted_at
            ) VALUES (
                %s, %s, 'Female', '1995-01-01'::date, 'Islam',
                NOW() - (%s || ' days')::interval
            )
        """
        params = (user_id, f"purge_test_{user_id}@test.invalid", str(deleted_at_offset_days))
    else:
        sql = """
            INSERT INTO users (user_id, email, sex, birthday, religion)
            VALUES (%s, %s, 'Female', '1995-01-01'::date, 'Islam')
        """
        params = (user_id, f"purge_test_{user_id}@test.invalid")

    with conn.cursor() as cur:
        cur.execute(sql, params)


def _insert_sibling(conn, user_id: uuid.UUID):
    sql = "INSERT INTO siblings (user_id, name) VALUES (%s, 'TestSibling')"
    with conn.cursor() as cur:
        cur.execute(sql, (user_id,))


def _insert_friendship(conn, user_a: uuid.UUID, user_b: uuid.UUID):
    """Insert a canonicalized friendship row (user_a < user_b)."""
    a, b = (user_a, user_b) if str(user_a) < str(user_b) else (user_b, user_a)
    sql = "INSERT INTO friendships (user_a, user_b) VALUES (%s, %s)"
    with conn.cursor() as cur:
        cur.execute(sql, (a, b))


def _insert_friend_request(conn, from_id: uuid.UUID, to_id: uuid.UUID):
    sql = """
        INSERT INTO friend_requests (from_user_id, to_user_id, status)
        VALUES (%s, %s, 'pending')
    """
    with conn.cursor() as cur:
        cur.execute(sql, (from_id, to_id))


def _insert_bookmark(conn, user_id: uuid.UUID, bookmarked_id: uuid.UUID):
    sql = "INSERT INTO bookmarks (user_id, bookmarked_user_id) VALUES (%s, %s)"
    with conn.cursor() as cur:
        cur.execute(sql, (user_id, bookmarked_id))


def _insert_block(conn, blocker_id: uuid.UUID, blocked_id: uuid.UUID):
    sql = "INSERT INTO blocks (blocker_id, blocked_id) VALUES (%s, %s)"
    with conn.cursor() as cur:
        cur.execute(sql, (blocker_id, blocked_id))


def _count_rows(conn, table: str, user_id: uuid.UUID) -> int:
    """
    Return the number of rows in `table` that reference `user_id` in any
    of the standard user-FK columns.
    """
    column_map = {
        "users": "user_id",
        "siblings": "user_id",
        "friendships": None,  # special — two columns
        "friend_requests": None,  # special — two columns
        "bookmarks": None,  # special — two columns
        "blocks": None,  # special — two columns
    }
    if table == "friendships":
        sql = (
            "SELECT COUNT(*) FROM friendships "
            "WHERE user_a = %s OR user_b = %s"
        )
        with conn.cursor() as cur:
            cur.execute(sql, (user_id, user_id))
            return cur.fetchone()[0]
    if table == "friend_requests":
        sql = (
            "SELECT COUNT(*) FROM friend_requests "
            "WHERE from_user_id = %s OR to_user_id = %s"
        )
        with conn.cursor() as cur:
            cur.execute(sql, (user_id, user_id))
            return cur.fetchone()[0]
    if table == "bookmarks":
        sql = (
            "SELECT COUNT(*) FROM bookmarks "
            "WHERE user_id = %s OR bookmarked_user_id = %s"
        )
        with conn.cursor() as cur:
            cur.execute(sql, (user_id, user_id))
            return cur.fetchone()[0]
    if table == "blocks":
        sql = (
            "SELECT COUNT(*) FROM blocks "
            "WHERE blocker_id = %s OR blocked_id = %s"
        )
        with conn.cursor() as cur:
            cur.execute(sql, (user_id, user_id))
            return cur.fetchone()[0]
    col = column_map[table]
    sql = f"SELECT COUNT(*) FROM {table} WHERE {col} = %s"  # noqa: S608 (test code)
    with conn.cursor() as cur:
        cur.execute(sql, (user_id,))
        return cur.fetchone()[0]


def _user_exists(conn, user_id: uuid.UUID) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM users WHERE user_id = %s", (user_id,))
        return cur.fetchone() is not None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def two_users_with_cascade_rows(db_with_migrations):
    """
    Insert:
      - user_a: deleted_at = 31 days ago (should be purged in scheduled mode)
      - user_b: a helper user to form relationships with user_a
      - One row in each of: siblings, friendships, friend_requests, bookmarks, blocks
        referencing user_a.
      - user_c: deleted_at = 29 days ago (must NOT be purged in scheduled mode)

    Yield (user_a_id, user_b_id, user_c_id).  Cleanup runs after the test.
    """
    import psycopg2.extras

    psycopg2.extras.register_uuid()

    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    user_c = uuid.uuid4()

    conn = _master_conn()
    try:
        # Insert user_b and user_c first (user_a depends on them for FKs)
        _insert_user(conn, user_b)
        _insert_user(conn, user_c, deleted_at_offset_days=29)
        # Insert user_a with deleted_at 31 days ago
        _insert_user(conn, user_a, deleted_at_offset_days=31)

        # Cascade rows referencing user_a
        _insert_sibling(conn, user_a)
        _insert_friendship(conn, user_a, user_b)
        _insert_friend_request(conn, user_a, user_b)
        _insert_bookmark(conn, user_a, user_b)
        _insert_block(conn, user_a, user_b)
    finally:
        conn.close()

    yield str(user_a), str(user_b), str(user_c)

    # Cleanup — delete in reverse FK order (CASCADE would handle most but
    # we delete users directly to be safe; CASCADE removes siblings/etc.)
    conn = _master_conn()
    try:
        for uid in (user_a, user_b, user_c):
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM friendships WHERE user_a = %s OR user_b = %s",
                    (uid, uid),
                )
                cur.execute(
                    "DELETE FROM friend_requests WHERE from_user_id = %s OR to_user_id = %s",
                    (uid, uid),
                )
                cur.execute(
                    "DELETE FROM bookmarks WHERE user_id = %s OR bookmarked_user_id = %s",
                    (uid, uid),
                )
                cur.execute(
                    "DELETE FROM blocks WHERE blocker_id = %s OR blocked_id = %s",
                    (uid, uid),
                )
                cur.execute("DELETE FROM siblings WHERE user_id = %s", (uid,))
                cur.execute("DELETE FROM users WHERE user_id = %s", (uid,))
    finally:
        conn.close()


@pytest.fixture()
def one_day_soft_deleted_user(db_with_migrations):
    """
    Insert a user soft-deleted only 1 day ago.  Yield user_id as str.
    Cleanup after.
    """
    import psycopg2.extras

    psycopg2.extras.register_uuid()

    user_id = uuid.uuid4()
    conn = _master_conn()
    try:
        _insert_user(conn, user_id, deleted_at_offset_days=1)
    finally:
        conn.close()

    yield str(user_id)

    conn = _master_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE user_id = %s", (user_id,))
    finally:
        conn.close()


@pytest.fixture()
def fresh_user_no_soft_delete(db_with_migrations):
    """
    Insert a user with no deleted_at (not soft-deleted).  Yield user_id as str.
    Cleanup after.
    """
    import psycopg2.extras

    psycopg2.extras.register_uuid()

    user_id = uuid.uuid4()
    conn = _master_conn()
    try:
        _insert_user(conn, user_id)
    finally:
        conn.close()

    yield str(user_id)

    conn = _master_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE user_id = %s", (user_id,))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# IT-9.11-1: scheduled mode purges stale user + all CASCADE-linked rows
# ---------------------------------------------------------------------------


def test_it_9_11_1_scheduled_mode_purges_stale_user_and_all_cascade_rows(
    two_users_with_cascade_rows,
):
    """
    IT-9.11-1: invoke in scheduled mode on a 31-day-old soft-deleted user.
    users row must be GONE; all 5 cascading-table rows must be GONE.
    """
    import psycopg2.extras

    psycopg2.extras.register_uuid()

    user_a_id, user_b_id, _user_c_id = two_users_with_cascade_rows
    user_a_uuid = uuid.UUID(user_a_id)

    result = _invoke_handler({})

    assert result["mode"] == "scheduled"

    conn = _master_conn()
    try:
        # users row must be gone
        assert not _user_exists(conn, user_a_uuid), (
            f"User {user_a_id} should have been hard-purged but still exists in users"
        )
        # All 5 cascading tables must have zero rows for user_a
        for table in ("siblings", "friendships", "friend_requests", "bookmarks", "blocks"):
            count = _count_rows(conn, table, user_a_uuid)
            assert count == 0, (
                f"Expected 0 rows in {table} for user {user_a_id} after purge, got {count}"
            )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# IT-9.11-2: scheduled mode respects the 30-day window (29-day user stays)
# ---------------------------------------------------------------------------


def test_it_9_11_2_scheduled_mode_does_not_purge_user_within_30_day_window(
    two_users_with_cascade_rows,
):
    """
    IT-9.11-2: the 29-day-old soft-deleted user must remain after scheduled
    invocation — their deleted_at is not yet past the 30-day threshold.
    """
    import psycopg2.extras

    psycopg2.extras.register_uuid()

    _user_a_id, _user_b_id, user_c_id = two_users_with_cascade_rows
    user_c_uuid = uuid.UUID(user_c_id)

    _invoke_handler({})

    conn = _master_conn()
    try:
        assert _user_exists(conn, user_c_uuid), (
            f"User {user_c_id} (deleted 29 days ago) should NOT have been purged"
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# IT-9.11-3: per-user mode bypasses the 30-day window
# ---------------------------------------------------------------------------


def test_it_9_11_3_per_user_mode_bypasses_30_day_window(
    one_day_soft_deleted_user,
):
    """
    IT-9.11-3: per-user invocation on a user soft-deleted only 1 day ago
    must purge the row (30-day window is dropped in per-user mode).
    """
    import psycopg2.extras

    psycopg2.extras.register_uuid()

    user_id = one_day_soft_deleted_user
    user_uuid = uuid.UUID(user_id)

    result = _invoke_handler({"user_id": user_id})

    assert result["mode"] == "per_user"
    assert result["rows_affected"] == 1

    conn = _master_conn()
    try:
        assert not _user_exists(conn, user_uuid), (
            f"User {user_id} (1 day old) should have been purged by per-user invocation"
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# IT-9.11-4: per-user mode retains soft-delete guard
# ---------------------------------------------------------------------------


def test_it_9_11_4_per_user_mode_retains_soft_delete_guard(
    fresh_user_no_soft_delete,
):
    """
    IT-9.11-4: invoking per-user on a user who has NOT been soft-deleted
    must return rows_affected=0 and leave the user row intact.
    The Lambda must NOT raise an exception — rows_affected=0 is the signal.
    """
    import psycopg2.extras

    psycopg2.extras.register_uuid()

    user_id = fresh_user_no_soft_delete
    user_uuid = uuid.UUID(user_id)

    result = _invoke_handler({"user_id": user_id})

    assert result["mode"] == "per_user"
    assert result["rows_affected"] == 0, (
        f"Expected rows_affected=0 for non-soft-deleted user, got {result['rows_affected']}"
    )

    conn = _master_conn()
    try:
        assert _user_exists(conn, user_uuid), (
            f"User {user_id} (not soft-deleted) must still exist after per-user invocation"
        )
    finally:
        conn.close()
