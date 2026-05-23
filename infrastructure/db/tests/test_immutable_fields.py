"""
Integration test: enforce_immutable_fields trigger on users table

The trigger (trg_users_immutable) fires BEFORE UPDATE on the users table
and raises an exception if any immutable field is changed.

Immutable fields per architecture.md §5.7:
    first_name, last_name, sex, birthday, religion, subsect

Mutable fields (sample):
    job_title

Tests use the master role (knotify) — the trigger fires regardless of role
because it is a FOR EACH ROW BEFORE UPDATE trigger, not an RLS policy.

Assertions:
  A. UPDATE on an immutable field (first_name) raises
     "Attempted to modify immutable field"
  B. UPDATE on a mutable field (job_title) succeeds and the value is persisted

Run against a local docker-compose Postgres container with all 8 migrations applied:
    python -m pytest infrastructure/db/tests/test_immutable_fields.py -v

Environment variables (with defaults matching docker-compose.yml):
    PGHOST      localhost
    PGPORT      5432
    PGDATABASE  knotify
    PGPASSWORD  knotify        (master role password)
"""

import os
import uuid

import psycopg2
import psycopg2.extras
import pytest

psycopg2.extras.register_uuid()

# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

_HOST = os.environ.get("PGHOST", "localhost")
_PORT = int(os.environ.get("PGPORT", "5432"))
_DBNAME = os.environ.get("PGDATABASE", "knotify")
_MASTER_USER = "knotify"
_MASTER_PASS = os.environ.get("PGPASSWORD", "knotify")


def _master_conn():
    """Return an autocommit connection as the knotify master role."""
    conn = psycopg2.connect(
        host=_HOST,
        port=_PORT,
        dbname=_DBNAME,
        user=_MASTER_USER,
        password=_MASTER_PASS,
    )
    conn.autocommit = True
    return conn


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def seeded_user():
    """
    Insert one user with known immutable fields, yield the user_id, then
    delete the row during teardown.

    The minimal required columns for the users table are populated.
    """
    user_id = uuid.uuid4()

    conn = _master_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (
                    user_id, email, username, first_name, last_name,
                    birthday, sex, religion
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    '1990-01-01'::date, %s, 'None'
                )
                """,
                (
                    user_id,
                    f"immutable_test_{user_id}@test.invalid",
                    f"immtest_{user_id}",
                    "Immutable",
                    "Testuser",
                    "Male",
                ),
            )
    finally:
        conn.close()

    yield user_id

    # Teardown — delete the seeded row
    conn = _master_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE user_id = %s", (user_id,))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Assertion A: UPDATE on an immutable field is rejected
# ---------------------------------------------------------------------------

def test_given_immutable_field_update_when_first_name_changed_then_raises_exception(
    seeded_user,
):
    """
    When an UPDATE touches the immutable column first_name, the trigger must
    raise an exception whose message contains "Attempted to modify immutable field".
    """
    user_id = seeded_user

    conn = _master_conn()
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            with pytest.raises(psycopg2.errors.RaiseException) as exc_info:
                cur.execute(
                    "UPDATE users SET first_name = 'X' WHERE user_id = %s",
                    (user_id,),
                )
        conn.rollback()
    finally:
        conn.close()

    assert "Attempted to modify immutable field" in str(exc_info.value), (
        f"Expected exception message to contain "
        f"'Attempted to modify immutable field', got: {exc_info.value}"
    )


# ---------------------------------------------------------------------------
# Assertion B: UPDATE on a mutable field succeeds
# ---------------------------------------------------------------------------

def test_given_mutable_field_update_when_job_title_changed_then_update_persists(
    seeded_user,
):
    """
    When an UPDATE changes the mutable column job_title, the statement must
    succeed and the new value must be readable in a subsequent SELECT.
    """
    user_id = seeded_user
    new_job_title = "Software Engineer"

    conn = _master_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET job_title = %s WHERE user_id = %s",
                (new_job_title, user_id),
            )
            cur.execute(
                "SELECT job_title FROM users WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    assert row is not None, f"Expected to find user {user_id} after UPDATE"
    assert row[0] == new_job_title, (
        f"Expected job_title='{new_job_title}' after mutable UPDATE, "
        f"got: {row[0]!r}"
    )
