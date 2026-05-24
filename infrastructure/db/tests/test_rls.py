"""
Integration test: Row-Level Security policy — users_opposite_sex_only

Tests connect as the `app_user` role (non-superuser, non-BYPASSRLS).
Seed inserts are performed as the `knotify` master role (bypasses RLS
intentionally — mirrors the trusted write path).

Assertions:
  1. GUCs SET   → Male user sees his own row + the Female row (exactly 2 rows).
  2. GUCs NOT SET → SELECT FROM users returns 0 rows (fail-closed; three-valued
                    SQL logic: NULL != NULL → NULL → row filtered out).

Run against a local docker-compose Postgres container with all 7 migrations applied:
    python -m pytest infrastructure/db/tests/test_rls.py -v

Environment variables (with defaults matching docker-compose.yml):
    PGHOST      localhost
    PGPORT      5432
    PGDATABASE  knotify
    PGPASSWORD  knotify        (master role password)
    APP_USER_PASSWORD  app_user  (matches migration 0007)
"""

import os
import sys
import uuid

import psycopg2
import psycopg2.extras
import psycopg2.extensions
import pytest

# Register the UUID adapter so psycopg2 passes uuid.UUID objects as strings
# to Postgres without the caller having to cast to str everywhere.
psycopg2.extras.register_uuid()

# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

_HOST = os.environ.get("PGHOST", "localhost")
_PORT = int(os.environ.get("PGPORT", "5432"))
_DBNAME = os.environ.get("PGDATABASE", "knotify")
_MASTER_USER = "knotify"
_MASTER_PASS = os.environ.get("PGPASSWORD", "knotify")
_APP_USER = "app_user"
_APP_PASS = os.environ.get("APP_USER_PASSWORD", "app_user")


def _master_conn():
    """Return an autocommit connection as the knotify master (bypasses RLS)."""
    conn = psycopg2.connect(
        host=_HOST,
        port=_PORT,
        dbname=_DBNAME,
        user=_MASTER_USER,
        password=_MASTER_PASS,
    )
    conn.autocommit = True
    return conn


def _app_conn():
    """Return a connection as the app_user role (subject to RLS)."""
    return psycopg2.connect(
        host=_HOST,
        port=_PORT,
        dbname=_DBNAME,
        user=_APP_USER,
        password=_APP_PASS,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def seeded_users():
    """
    Insert one Male and one Female user as the master (bypasses RLS), yield
    their UUIDs, then clean up.

    The minimal required columns for the users table are inserted.  Optional
    columns (location, preferences, profile_vector, etc.) are left NULL.
    """
    male_id = uuid.uuid4()
    female_id = uuid.uuid4()

    insert_sql = """
        INSERT INTO users (
            user_id, email, username, first_name, last_name,
            birthday, sex, religion
        ) VALUES (
            %s, %s, %s, %s, %s,
            '1995-06-15'::date, %s, 'None'
        )
    """

    conn = _master_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(insert_sql, (
                male_id,
                f"male_{male_id}@test.invalid",
                f"testmale_{male_id}",
                "TestMale", "User",
                "Male",
            ))
            cur.execute(insert_sql, (
                female_id,
                f"female_{female_id}@test.invalid",
                f"testfemale_{female_id}",
                "TestFemale", "User",
                "Female",
            ))
    finally:
        conn.close()

    yield {"male_id": male_id, "female_id": female_id}

    # Teardown — delete by primary key (master bypasses RLS for DELETE too)
    conn = _master_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM users WHERE user_id IN (%s, %s)",
                (male_id, female_id),
            )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Assertion 1: GUCs SET — Male sees his own row + the Female row
# ---------------------------------------------------------------------------

def test_given_gucs_set_male_user_sees_own_row_and_female_row(seeded_users):
    """
    When app.requesting_user_id and app.requesting_user_sex are set for the
    Male user, SELECT FROM users must return exactly two rows:
      - the Male's own row (own_id clause)
      - the Female row    (sex != 'Male' clause)
    """
    male_id = seeded_users["male_id"]
    female_id = seeded_users["female_id"]

    conn = _app_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("BEGIN")
            cur.execute(
                "SET LOCAL app.requesting_user_id = %s", (str(male_id),)
            )
            cur.execute(
                "SET LOCAL app.requesting_user_sex = %s", ("Male",)
            )
            cur.execute(
                "SELECT user_id, sex FROM users "
                "WHERE user_id IN (%s, %s) "
                "ORDER BY sex",
                (male_id, female_id),
            )
            rows = cur.fetchall()
            cur.execute("ROLLBACK")
    finally:
        conn.close()

    visible_ids = {row["user_id"] for row in rows}
    visible_sexes = {row["sex"] for row in rows}

    assert len(rows) == 2, (
        f"Expected 2 rows visible to Male (own + Female), got {len(rows)}. "
        f"Rows: {[dict(r) for r in rows]}"
    )
    assert male_id in visible_ids, (
        f"Male's own row ({male_id}) not visible. Visible IDs: {visible_ids}"
    )
    assert female_id in visible_ids, (
        f"Female's row ({female_id}) not visible. Visible IDs: {visible_ids}"
    )
    assert "Female" in visible_sexes, "Female row missing from result set"
    assert "Male" in visible_sexes, "Male row missing from result set"


# ---------------------------------------------------------------------------
# Assertion 2: GUCs NOT SET — fail-closed, 0 rows visible
# ---------------------------------------------------------------------------

def test_given_gucs_not_set_zero_rows_visible(seeded_users):
    """
    When neither GUC is set, current_setting(..., true) returns NULL.
    The USING clause evaluates to NULL → row is filtered.
    The table must return 0 rows (fail-closed behavior).

    This test uses a fresh connection so no prior SET LOCAL bleeds over.
    """
    male_id = seeded_users["male_id"]
    female_id = seeded_users["female_id"]

    conn = _app_conn()
    try:
        with conn.cursor() as cur:
            # No SET LOCAL calls — GUCs deliberately absent
            cur.execute(
                "SELECT count(*) FROM users WHERE user_id IN (%s, %s)",
                (male_id, female_id),
            )
            (count,) = cur.fetchone()
    finally:
        conn.close()

    assert count == 0, (
        f"Expected 0 rows when GUCs are not set (fail-closed), got {count}. "
        "Check that FORCE ROW LEVEL SECURITY is enabled and the policy uses "
        "missing_ok=true in current_setting()."
    )
