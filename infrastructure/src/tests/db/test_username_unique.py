"""
docker-compose unit test: case-insensitive partial unique index on users.username

Migration 0010 adds:
    CREATE UNIQUE INDEX users_username_lower_unique
        ON users (lower(username))
        WHERE username IS NOT NULL;

The index is PARTIAL (WHERE username IS NOT NULL) so the many NULL-username
bootstrap rows produced by the post-confirmation Lambda do not collide.

Assertions:
  A. Two rows with the same username (same case) → second INSERT raises
     psycopg2.errors.UniqueViolation.
  B. Two rows with the same username (different case) → second INSERT raises
     psycopg2.errors.UniqueViolation  (case-insensitive enforcement).
  C. Two rows with username = NULL → both INSERTs succeed
     (partial index: NULL rows are excluded from the uniqueness check).

Run against a local docker-compose Postgres container with all 10 migrations
applied:
    python -m pytest infrastructure/src/tests/db/test_username_unique.py -v

Environment variables (with defaults matching docker-compose.yml):
    PGHOST      localhost
    PGPORT      5432
    PGDATABASE  knotify
    PGPASSWORD  knotify        (master role password)
"""

import os
import uuid

import psycopg2
import psycopg2.errors
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


def _insert_user(cur, user_id: uuid.UUID, email: str, username: str | None) -> None:
    """
    Insert a minimal user row.  username may be None for NULL-username bootstrap rows.
    """
    cur.execute(
        """
        INSERT INTO users (user_id, email, username)
        VALUES (%s, %s, %s)
        """,
        (user_id, email, username),
    )


# ---------------------------------------------------------------------------
# Helpers — generate unique per-test email addresses
# ---------------------------------------------------------------------------

def _email(tag: str) -> str:
    return f"username_unique_test_{tag}_{uuid.uuid4().hex[:8]}@test.invalid"


# ---------------------------------------------------------------------------
# Assertion A: same username, same case → UniqueViolation
# ---------------------------------------------------------------------------

def test_given_two_rows_with_identical_username_when_second_inserted_then_unique_violation():
    """
    Two users with the same username value (identical case) must raise
    UniqueViolation on the second INSERT because the partial unique index
    rejects duplicate lower(username) values.
    """
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    shared_username = f"alice_{uuid.uuid4().hex[:8]}"

    conn = _master_conn()
    try:
        with conn.cursor() as cur:
            # First INSERT succeeds.
            _insert_user(cur, user_a, _email("a1"), shared_username)

            # Second INSERT with the same username must fail.
            with pytest.raises(psycopg2.errors.UniqueViolation):
                _insert_user(cur, user_b, _email("a2"), shared_username)
    finally:
        # Teardown — remove the first row (second never committed due to exception).
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE user_id IN (%s, %s)", (user_a, user_b))
        conn.close()


# ---------------------------------------------------------------------------
# Assertion B: same username, differing case → UniqueViolation
# ---------------------------------------------------------------------------

def test_given_two_rows_with_same_username_differing_case_when_second_inserted_then_unique_violation():
    """
    Two users whose usernames differ only in case (e.g. 'Alice' vs 'alice')
    must raise UniqueViolation because the index is on lower(username) and
    lower('Alice') == lower('alice').
    """
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    base = uuid.uuid4().hex[:8]
    username_lower = f"alice_{base}"
    username_upper = f"Alice_{base}"  # same when lowercased

    conn = _master_conn()
    try:
        with conn.cursor() as cur:
            # First INSERT (lowercase) succeeds.
            _insert_user(cur, user_a, _email("b1"), username_lower)

            # Second INSERT (uppercase variant) must fail.
            with pytest.raises(psycopg2.errors.UniqueViolation):
                _insert_user(cur, user_b, _email("b2"), username_upper)
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE user_id IN (%s, %s)", (user_a, user_b))
        conn.close()


# ---------------------------------------------------------------------------
# Assertion C: two rows with NULL username → both succeed (partial index)
# ---------------------------------------------------------------------------

def test_given_two_rows_with_null_username_when_both_inserted_then_both_succeed():
    """
    Two users with username = NULL must both be inserted successfully.
    The partial index condition `WHERE username IS NOT NULL` excludes NULL
    rows from the uniqueness check, so multiple NULL-username bootstrap rows
    are permitted.
    """
    user_a = uuid.uuid4()
    user_b = uuid.uuid4()

    conn = _master_conn()
    try:
        with conn.cursor() as cur:
            # Both INSERTs must succeed without raising any exception.
            _insert_user(cur, user_a, _email("c1"), None)
            _insert_user(cur, user_b, _email("c2"), None)

            # Verify both rows are actually present.
            cur.execute(
                "SELECT COUNT(*) FROM users WHERE user_id IN (%s, %s)",
                (user_a, user_b),
            )
            count = cur.fetchone()[0]

        assert count == 2, (
            f"Expected 2 NULL-username rows to exist but found {count}. "
            "The partial unique index must not block NULL-username inserts."
        )
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE user_id IN (%s, %s)", (user_a, user_b))
        conn.close()
