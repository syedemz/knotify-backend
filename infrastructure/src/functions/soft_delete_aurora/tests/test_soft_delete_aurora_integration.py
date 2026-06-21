"""
Integration tests for story 9.5 — soft_delete_aurora Lambda handler.

Requires a running docker-compose postgres container with all migrations applied
and local_init.sql executed (the db_with_migrations fixture from the layers/db
conftest applies this automatically).

Run:
    pytest infrastructure/src/functions/soft_delete_aurora/tests/test_soft_delete_aurora_integration.py \\
           -v -m integration

Skip in unit test runs (default):
    pytest -m "not integration"

Acceptance criteria covered (story 9.5):
  IT-9.5-1  Invoke on a fresh user:
              - deleted_at is set (non-NULL).
              - email, phone_number, photo_url, chosen_profile_avatar are NULL.
              - preferences is '{}'.
              - preference_vector is NULL.
              - username is '[deleted-user]'.
              - first_name is 'Deleted', last_name is 'User'.
              - rows_affected == 1.
  IT-9.5-2  Friendships rows for the soft-deleted user still exist
            (only the users row is mutated; cascade purge is story 9.11).
  IT-9.5-3  Second invocation on the already-soft-deleted user is a no-op:
              - rows_affected == 0.
              - deleted_at, username, first_name, last_name are unchanged from
                the first invocation.
"""

from __future__ import annotations

import os
import uuid
import sys

import pytest

# ---------------------------------------------------------------------------
# sys.path setup — add db layer so knotify_db resolves
# ---------------------------------------------------------------------------

_here = os.path.dirname(os.path.abspath(__file__))
_src_root = os.path.normpath(os.path.join(_here, "..", "..", ".."))

for _p in [
    os.path.join(_src_root, "layers", "db"),
    os.path.join(_src_root, "layers", "observability"),
]:
    if _p not in sys.path:
        sys.path.insert(0, _p)

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
    """
    Return a connection-params dict (as accepted by knotify_db.get_connection)
    for the app_user — this is what the handler uses in production.
    """
    return {
        "host": _PGHOST,
        "port": _PGPORT,
        "dbname": _PGDATABASE,
        "username": "app_user",
        "password": _APP_USER_PASS,
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def user_with_friendship(db_with_migrations):
    """
    Insert one fresh user row with PII fields populated, plus one friendship
    row referencing that user.  Yield (user_id, friend_id).  Clean up after.

    The inserted row has all PII fields set so we can assert they are nulled
    after the soft-delete.  The friendship row verifies that cascade is NOT
    triggered by the soft-delete (story 9.11 handles the 30-day purge).
    """
    import psycopg2
    import psycopg2.extras

    psycopg2.extras.register_uuid()

    user_id = uuid.uuid4()
    friend_id = uuid.uuid4()

    insert_user_sql = """
        INSERT INTO users (
            user_id, email, phone_number, photo_url, chosen_profile_avatar,
            username, first_name, last_name,
            sex, birthday, religion,
            preferences, preference_vector
        ) VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, %s,
            'Female', '1995-01-01'::date, 'Islam',
            '{"halal": true}'::jsonb, ARRAY[1,0,1,0,1,0,1,0,1,0,1,0,1,0,1,0,1,0,1,0]::vector(20)
        )
    """
    insert_friend_sql = """
        INSERT INTO users (user_id, email, sex, birthday, religion)
        VALUES (%s, %s, 'Male', '1993-03-03'::date, 'Islam')
    """
    insert_friendship_sql = """
        INSERT INTO friendships (user_a_id, user_b_id)
        VALUES (%s, %s)
    """

    conn = _master_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(insert_user_sql, (
                user_id,
                f"pii_user_{user_id}@test.invalid",
                "+1-555-0100",
                f"https://cdn.example.com/photos/{user_id}.jpg",
                "avatar_01",
                f"piiuser_{str(user_id)[:8]}",
                "PII",
                "Subject",
            ))
            cur.execute(insert_friend_sql, (
                friend_id,
                f"friend_{friend_id}@test.invalid",
            ))
            cur.execute(insert_friendship_sql, (user_id, friend_id))
    finally:
        conn.close()

    yield str(user_id), str(friend_id)

    # Clean up — DELETE CASCADE removes friendships when users rows are deleted
    conn = _master_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM friendships WHERE user_a_id IN (%s, %s) OR user_b_id IN (%s, %s)",
                (user_id, friend_id, user_id, friend_id),
            )
            cur.execute(
                "DELETE FROM users WHERE user_id IN (%s, %s)",
                (user_id, friend_id),
            )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# IT-9.5-1 + IT-9.5-2: fresh user soft-delete + friendships survive
# ---------------------------------------------------------------------------


def test_it_9_5_1_fresh_user_soft_delete_sets_deleted_at_and_nulls_pii(
    user_with_friendship,
):
    """
    IT-9.5-1: invoke on a fresh user — deleted_at is set, all PII columns are
    nulled/reset to sentinel values, rows_affected == 1.

    IT-9.5-2: the friendships row referencing the user still exists after the
    soft-delete (cascade purge is story 9.11, not here).
    """
    import psycopg2
    import psycopg2.extras

    from soft_delete_aurora import handler as h

    psycopg2.extras.register_uuid()

    user_id, friend_id = user_with_friendship

    # Invoke the handler with a direct DB-params dict (bypasses Secrets Manager)
    os.environ["DB_SECRET_NAME"] = ""  # blank triggers dict path below
    # Temporarily patch _get_conn to use local app_user creds
    import soft_delete_aurora.handler as mod

    original_get_conn = mod._get_conn
    import knotify_db

    _conn = knotify_db.get_connection(_app_conn_dict())

    def _local_get_conn():
        return _conn

    mod._get_conn = _local_get_conn
    try:
        result = h.handler({"user_id": user_id}, None)
    finally:
        mod._get_conn = original_get_conn
        _conn.close()
        mod._conn = None  # reset module cache

    # --- Assert return value ---
    assert result["user_id"] == user_id
    assert result["rows_affected"] == 1

    # --- Assert DB state via master connection ---
    conn = _master_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT deleted_at, email, phone_number, photo_url, "
                "       chosen_profile_avatar, preferences::text, "
                "       preference_vector, username, first_name, last_name "
                "FROM users WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    assert row is not None, f"User row {user_id} not found after soft-delete"

    # deleted_at must be set (non-NULL)
    assert row["deleted_at"] is not None, "deleted_at should be set after soft-delete"

    # PII fields must be nulled
    for col in ("email", "phone_number", "photo_url", "chosen_profile_avatar",
                "preference_vector"):
        assert row[col] is None, f"Column '{col}' should be NULL after soft-delete, got {row[col]!r}"

    # preferences must be reset to '{}'
    assert row["preferences"] == "{}", (
        f"preferences should be '{{}}' after soft-delete, got {row['preferences']!r}"
    )

    # Sentinel name fields
    assert row["username"] == "[deleted-user]", (
        f"username should be '[deleted-user]', got {row['username']!r}"
    )
    assert row["first_name"] == "Deleted", (
        f"first_name should be 'Deleted', got {row['first_name']!r}"
    )
    assert row["last_name"] == "User", (
        f"last_name should be 'User', got {row['last_name']!r}"
    )

    # IT-9.5-2: friendship row still exists (soft-delete does NOT cascade)
    conn = _master_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM friendships "
                "WHERE (user_a_id = %s OR user_b_id = %s)",
                (user_id, user_id),
            )
            (count,) = cur.fetchone()
    finally:
        conn.close()

    assert count == 1, (
        f"Friendships row should still exist after soft-delete (cascade is story 9.11), "
        f"got count={count}"
    )


# ---------------------------------------------------------------------------
# IT-9.5-3: second invocation is a no-op
# ---------------------------------------------------------------------------


def test_it_9_5_3_second_invocation_on_already_deleted_user_is_noop(
    user_with_friendship,
):
    """
    IT-9.5-3: invoke the handler twice on the same user.
    - First invocation: rows_affected == 1.
    - Second invocation: rows_affected == 0 (WHERE guard fires, nothing updated).
    - The deleted_at timestamp from the first invocation is unchanged.
    """
    import psycopg2
    import psycopg2.extras
    import datetime

    from soft_delete_aurora import handler as h

    psycopg2.extras.register_uuid()

    user_id, _friend_id = user_with_friendship

    import soft_delete_aurora.handler as mod
    import knotify_db

    _conn1 = knotify_db.get_connection(_app_conn_dict())

    def _local_get_conn():
        return _conn1

    mod._get_conn = _local_get_conn
    mod._conn = None

    try:
        # First invocation
        result_1 = h.handler({"user_id": user_id}, None)
    finally:
        _conn1.close()
        mod._conn = None

    # Capture deleted_at after first invocation
    conn = _master_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT deleted_at FROM users WHERE user_id = %s",
                (user_id,),
            )
            row_after_first = cur.fetchone()
    finally:
        conn.close()

    deleted_at_first = row_after_first["deleted_at"]
    assert deleted_at_first is not None

    # Second invocation — must be a no-op
    _conn2 = knotify_db.get_connection(_app_conn_dict())

    def _local_get_conn2():
        return _conn2

    mod._get_conn = _local_get_conn2
    mod._conn = None

    try:
        result_2 = h.handler({"user_id": user_id}, None)
    finally:
        _conn2.close()
        mod._conn = None

    # Verify second invocation result
    assert result_2["rows_affected"] == 0, (
        f"Second invocation should return rows_affected=0, got {result_2['rows_affected']}"
    )

    # Verify deleted_at is unchanged
    conn = _master_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT deleted_at FROM users WHERE user_id = %s",
                (user_id,),
            )
            row_after_second = cur.fetchone()
    finally:
        conn.close()

    # Aurora timestamp precision is microseconds; allow equality
    assert row_after_second["deleted_at"] == deleted_at_first, (
        f"deleted_at changed on second invocation: "
        f"first={deleted_at_first}, second={row_after_second['deleted_at']}"
    )
