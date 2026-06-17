"""
Integration test: deck_view materialized view and refresh_deck_view() function

Architecture reference: architecture.md §5.1

deck_view is a MATERIALIZED VIEW — a snapshot of user rows that meet the
swipe-deck eligibility criteria:
    - deleted_at IS NULL   (not soft-deleted)
    - profile_complete_verified = true

The view is NOT subject to RLS. Materialized views store a physical snapshot;
PostgreSQL does not re-evaluate RLS policies when reading from the snapshot.
Lambda reads deck_view to build the swipe deck; the data it receives is the
same regardless of the calling role's GUC settings.

Role choices in this test:
    - INSERT and REFRESH: knotify master role (bypasses RLS, appropriate for
      trusted write/admin paths — mirrors a scheduler or admin Lambda that
      triggers refresh after a profile update).
    - SELECT FROM deck_view: also knotify master, because RLS does not apply
      to materialized views. Using app_user here would not add correctness
      coverage; the view grants SELECT to app_user for Lambda's read path,
      but for these tests the master is sufficient and reduces test surface.

CONCURRENT refresh note:
    REFRESH MATERIALIZED VIEW CONCURRENTLY requires the unique index to exist.
    Migration 0009 creates CREATE UNIQUE INDEX idx_deck_user ON deck_view
    (user_id) immediately after the view, so the concurrent refresh is safe.

Assertions:
  A. Positive — eligible user (profile_complete_verified=true, deleted_at NULL)
     appears in deck_view after refresh_deck_view() is called.
  B. Negative — ineligible user (profile_complete_verified=false) does NOT
     appear in deck_view after refresh, even if they are inserted into users.
  C. Extended columns — a verified user with non-NULL preference_vector and
     religion values appears in deck_view with those exact values after
     migration 0011 (deck_view extended columns) is applied and the view is
     refreshed. Verifies that preference_vector and religion are projected
     through the materialized view as new columns.

Run against a local docker-compose Postgres container with all migrations
applied (through 0011 for assertions A/B/C):
    python -m pytest infrastructure/db/tests/test_deck_view.py -v

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

_INSERT_USER_SQL = """
    INSERT INTO users (
        user_id, email, username, first_name, last_name,
        birthday, sex, religion,
        profile_complete_verified
    ) VALUES (
        %s, %s, %s, %s, %s,
        '1992-04-10'::date, %s, 'None',
        %s
    )
"""


@pytest.fixture(scope="module")
def seeded_users():
    """
    Insert one eligible user (profile_complete_verified=true, deleted_at NULL)
    and one ineligible user (profile_complete_verified=false) as the master
    role, yield their UUIDs, then delete both during teardown.

    The master role bypasses RLS, so these inserts reach the table regardless
    of any GUC state — consistent with how a trusted admin path would seed
    fixture data.
    """
    eligible_id = uuid.uuid4()
    ineligible_id = uuid.uuid4()

    conn = _master_conn()
    try:
        with conn.cursor() as cur:
            # Eligible: will appear in deck_view after refresh
            cur.execute(
                _INSERT_USER_SQL,
                (
                    eligible_id,
                    f"deck_eligible_{eligible_id}@test.invalid",
                    f"deck_elig_{eligible_id}",
                    "DeckElig",
                    "User",
                    "Male",
                    True,  # profile_complete_verified
                ),
            )
            # Ineligible: profile_complete_verified=false — must NOT appear
            cur.execute(
                _INSERT_USER_SQL,
                (
                    ineligible_id,
                    f"deck_inelig_{ineligible_id}@test.invalid",
                    f"deck_inelig_{ineligible_id}",
                    "DeckInelig",
                    "User",
                    "Female",
                    False,  # profile_complete_verified
                ),
            )
    finally:
        conn.close()

    yield {"eligible_id": eligible_id, "ineligible_id": ineligible_id}

    # Teardown — delete both seeded rows so they don't pollute other tests
    conn = _master_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM users WHERE user_id IN (%s, %s)",
                (eligible_id, ineligible_id),
            )
    finally:
        conn.close()


@pytest.fixture(scope="module", autouse=True)
def refresh_view(seeded_users):
    """
    Call refresh_deck_view() once after seed inserts.

    Scoped to module so that assertion A and assertion B share the same
    post-refresh snapshot — consistent with a single refresh cycle.
    """
    conn = _master_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT refresh_deck_view()")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Assertion A: eligible user appears in deck_view after refresh
# ---------------------------------------------------------------------------

def test_given_eligible_user_when_deck_view_refreshed_then_user_is_present(
    seeded_users,
    refresh_view,
):
    """
    A user with profile_complete_verified=true and deleted_at IS NULL must
    appear in deck_view after refresh_deck_view() is called.

    The SELECT uses the master role — materialized views do not honor RLS so
    the role choice does not affect which rows are visible; app_user would
    see the same result (both have SELECT on deck_view).
    """
    eligible_id = seeded_users["eligible_id"]

    conn = _master_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id FROM deck_view WHERE user_id = %s",
                (eligible_id,),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    assert row is not None, (
        f"Expected eligible user {eligible_id} to appear in deck_view after "
        "refresh_deck_view(), but it was absent. Check that the view filter "
        "is (deleted_at IS NULL AND profile_complete_verified = true) and "
        "that refresh_deck_view() was called after the insert."
    )


# ---------------------------------------------------------------------------
# Assertion B: ineligible user does NOT appear in deck_view after refresh
# ---------------------------------------------------------------------------

def test_given_ineligible_user_when_deck_view_refreshed_then_user_is_absent(
    seeded_users,
    refresh_view,
):
    """
    A user with profile_complete_verified=false must NOT appear in deck_view
    after refresh_deck_view() is called, even if deleted_at IS NULL.

    This confirms the view filter correctly excludes incomplete profiles.
    """
    ineligible_id = seeded_users["ineligible_id"]

    conn = _master_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT user_id FROM deck_view WHERE user_id = %s",
                (ineligible_id,),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    assert row is None, (
        f"Expected ineligible user {ineligible_id} (profile_complete_verified=false) "
        "to be absent from deck_view, but it was found. Check that the view "
        "filter includes AND profile_complete_verified = true."
    )


# ---------------------------------------------------------------------------
# Assertion C: extended columns (migration 0011) — preference_vector and
# religion are projected correctly through the refreshed materialized view
# ---------------------------------------------------------------------------

_INSERT_USER_WITH_VECTOR_SQL = """
    INSERT INTO users (
        user_id, email, username, first_name, last_name,
        birthday, sex, religion,
        preference_vector,
        profile_complete_verified
    ) VALUES (
        %s, %s, %s, %s, %s,
        '1990-01-15'::date, %s, %s,
        %s::vector,
        true
    )
"""

_KNOWN_RELIGION = "Islam"
# 20-dimensional vector with 1.0 at index 0 and 0.0 elsewhere.
# Stored as a Postgres vector literal string and cast with ::vector in the SQL.
_KNOWN_VECTOR = "[1.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0,0.0]"


@pytest.fixture(scope="module")
def seeded_user_with_vector():
    """
    Insert one verified user whose preference_vector and religion are both
    non-NULL, yield their UUID, then delete during teardown.

    This fixture is independent of seeded_users so the two can be used in
    the same pytest session without ordering constraints.
    """
    user_id = uuid.uuid4()

    conn = _master_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                _INSERT_USER_WITH_VECTOR_SQL,
                (
                    user_id,
                    f"deck_vec_{user_id}@test.invalid",
                    f"deck_vec_{user_id}",
                    "DeckVec",
                    "User",
                    "Female",
                    _KNOWN_RELIGION,
                    _KNOWN_VECTOR,
                ),
            )
    finally:
        conn.close()

    yield user_id

    conn = _master_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE user_id = %s", (user_id,))
    finally:
        conn.close()


@pytest.fixture(scope="module")
def refresh_view_after_vector_seed(seeded_user_with_vector):
    """
    Refresh deck_view after seeding the vector user so the materialized view
    snapshot includes the new row.
    """
    conn = _master_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT refresh_deck_view()")
    finally:
        conn.close()


def test_given_verified_user_with_preference_vector_and_religion_when_view_refreshed_then_extended_columns_present(
    seeded_user_with_vector,
    refresh_view_after_vector_seed,
):
    """
    After migration 0011 drops and recreates deck_view with preference_vector
    and religion columns, a verified user whose users row has non-NULL values
    for those columns must appear in deck_view with the exact same values after
    refresh_deck_view() is called.

    This test catches any regression where the SELECT list in the CREATE
    MATERIALIZED VIEW statement omits or mis-aliases the two new columns.
    """
    user_id = seeded_user_with_vector

    conn = _master_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT preference_vector, religion "
                "FROM deck_view "
                "WHERE user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
    finally:
        conn.close()

    assert row is not None, (
        f"Expected user {user_id} to appear in deck_view after refresh, "
        "but no row was found. Verify migration 0011 was applied and "
        "refresh_deck_view() was called after the insert."
    )
    assert row["religion"] == _KNOWN_RELIGION, (
        f"Expected religion='{_KNOWN_RELIGION}' in deck_view, "
        f"got: {row['religion']!r}. Check the SELECT list in migration 0011."
    )
    # preference_vector is returned as a string by psycopg2 in the absence of
    # the pgvector adapter.  Parse it and compare element-wise.
    raw = row["preference_vector"]
    assert raw is not None, (
        "preference_vector was NULL in deck_view but users row had a non-NULL "
        "value. Check the SELECT list in migration 0011."
    )
    # Postgres returns the vector as a string like '[1,0,0,...]'; strip brackets
    # and split on commas to get float values.
    parsed = [float(v) for v in raw.strip("[]").split(",")]
    expected = [float(v) for v in _KNOWN_VECTOR.strip("[]").split(",")]
    assert parsed == expected, (
        f"preference_vector mismatch in deck_view. "
        f"Expected: {expected}, got: {parsed}. "
        "Check the SELECT list in migration 0011."
    )
