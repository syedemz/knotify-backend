"""
docker-compose unit test: RLS fail-closed and gender-visibility enforcement
on the users table.

Migration 0007 creates the `users_opposite_sex_only` SELECT policy:

    USING (
        sex != current_setting('app.requesting_user_sex', true)
        OR user_id = current_setting('app.requesting_user_id', true)::uuid
    )

With FORCE ROW LEVEL SECURITY applied to the table owner as well, this policy
is the sole gatekeeper for all SELECT access by the `app_user` role.

Three assertions tested here:

  1. Fail-closed (GUCs unset): SELECT * FROM users as app_user returns zero rows
     when neither `app.requesting_user_id` nor `app.requesting_user_sex` has been
     SET. Both sides of the USING expression evaluate to NULL (missing_ok=true),
     so the policy result is NULL → row is filtered. Zero rows proves the DB
     enforces fail-closed by default — Lambda cannot accidentally leak data if it
     forgets to call set_rls_context.

  2. Policy-applied (Male-A requester): after SET app.requesting_user_id = Male-A
     and SET app.requesting_user_sex = 'Male', the SELECT returns EXACTLY TWO ROWS:
       - Male-A's own row (identity exception: user_id = requesting_user_id)
       - Female-C's row (opposite-sex rule: sex != 'Male')
     Male-B's row is filtered (same sex as requester, different identity).

  3. Policy-applied (Female-C requester): after RESET + SET to Female-C, the SELECT
     returns EXACTLY THREE ROWS: Female-C + Male-A + Male-B. Both males are
     opposite-sex relative to Female, so both pass the sex != 'Female' branch.

Seed uses master credentials so the INSERT bypasses app_user's RLS INSERT policy,
which would require the GUC to be set correctly.

Run against a local docker-compose Postgres container with all migrations applied:
    python -m pytest infrastructure/src/tests/db/test_rls_fail_closed.py -v

To skip in suites that don't have the docker-compose database:
    pytest -m "not integration"   (this test carries no marker, so it always runs)

Environment variables (with defaults matching docker-compose.yml):
    PGHOST      localhost
    PGPORT      5432
    PGDATABASE  knotify
    PGPASSWORD  knotify        (master role password)
    APP_USER_PASSWORD  app_user  (set by local_init.sql after yoyo apply)
"""

import os
import uuid

import psycopg2
import psycopg2.extras
import pytest

psycopg2.extras.register_uuid()

# ---------------------------------------------------------------------------
# Connection constants (mirror docker-compose.yml / local_init.sql defaults)
# ---------------------------------------------------------------------------

_HOST = os.environ.get("PGHOST", "localhost")
_PORT = int(os.environ.get("PGPORT", "5432"))
_DBNAME = os.environ.get("PGDATABASE", "knotify")
_MASTER_USER = "knotify"
_MASTER_PASS = os.environ.get("PGPASSWORD", "knotify")
_APP_USER = "app_user"
_APP_USER_PASS = os.environ.get("APP_USER_PASSWORD", "app_user")


def _master_conn():
    """Return an autocommit connection using the knotify master role (bypasses RLS)."""
    conn = psycopg2.connect(
        host=_HOST,
        port=_PORT,
        dbname=_DBNAME,
        user=_MASTER_USER,
        password=_MASTER_PASS,
    )
    conn.autocommit = True
    return conn


def _app_user_conn():
    """Return a connection as app_user (RLS applies — FORCE ROW LEVEL SECURITY)."""
    return psycopg2.connect(
        host=_HOST,
        port=_PORT,
        dbname=_DBNAME,
        user=_APP_USER,
        password=_APP_USER_PASS,
    )


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

def _insert_user(cur, user_id: uuid.UUID, email: str, sex: str) -> None:
    """
    Insert a minimal users row as master.

    Inserts only the columns required by NOT-NULL and CHECK constraints.
    profile_complete_verified is left at its DEFAULT false so the
    profile_complete_requires_required_fields CHECK constraint does not fire
    (that constraint only activates when the flag is true AND required fields
    are NULL).

    The immutable-fields trigger (0008) fires on UPDATE — INSERT is unaffected.
    """
    cur.execute(
        "INSERT INTO users (user_id, email, sex) VALUES (%s, %s, %s)",
        (user_id, email, sex),
    )


# ---------------------------------------------------------------------------
# Test: all three RLS assertions in a single setup/teardown context
# ---------------------------------------------------------------------------

def test_given_users_table_when_rls_applied_then_fail_closed_and_gender_policy_enforced():
    """
    Seed Male-A, Male-B, Female-C as master, then verify three app_user SELECT
    outcomes that together prove the RLS policy is correct:

      1. No GUCs set      → 0 rows  (fail-closed)
      2. GUCs = Male-A    → 2 rows  (own row + Female-C)
      3. GUCs = Female-C  → 3 rows  (own row + Male-A + Male-B)
    """
    male_a = uuid.uuid4()
    male_b = uuid.uuid4()
    female_c = uuid.uuid4()

    master = _master_conn()
    app = _app_user_conn()

    try:
        # ------------------------------------------------------------------
        # Seed: insert three users as master (bypasses RLS)
        # ------------------------------------------------------------------
        with master.cursor() as cur:
            _insert_user(cur, male_a, f"rls_test_male_a_{uuid.uuid4().hex[:8]}@test.invalid", "Male")
            _insert_user(cur, male_b, f"rls_test_male_b_{uuid.uuid4().hex[:8]}@test.invalid", "Male")
            _insert_user(cur, female_c, f"rls_test_female_c_{uuid.uuid4().hex[:8]}@test.invalid", "Female")

        seeded_ids = {male_a, male_b, female_c}

        # ------------------------------------------------------------------
        # Assertion 1: fail-closed — no GUCs set
        #
        # Both sides of the USING expression evaluate to NULL when GUCs are
        # absent (missing_ok=true returns NULL for unset parameters).
        # NULL != NULL → NULL; user_id = NULL::uuid → NULL; NULL OR NULL → NULL.
        # PostgreSQL treats NULL policy result as DENY → zero rows returned.
        # ------------------------------------------------------------------
        with app.cursor() as cur:
            cur.execute("SELECT user_id FROM users")
            visible_ids = {row[0] for row in cur.fetchall()} & seeded_ids

        assert visible_ids == set(), (
            f"Fail-closed violated: expected 0 seeded rows visible when GUCs are "
            f"unset, got {len(visible_ids)}: {visible_ids}"
        )

        app.rollback()  # clear any implicit transaction state

        # ------------------------------------------------------------------
        # Assertion 2: Male-A as requester → should see own row + Female-C
        #
        # sex != 'Male' → Female-C passes
        # user_id = Male-A → Male-A passes (identity exception)
        # Male-B: sex = 'Male' (== requester sex) AND user_id != Male-A → filtered
        # ------------------------------------------------------------------
        with app.cursor() as cur:
            cur.execute("SET app.requesting_user_id = %s", (str(male_a),))
            cur.execute("SET app.requesting_user_sex = 'Male'")
            cur.execute("SELECT user_id FROM users")
            visible_ids = {row[0] for row in cur.fetchall()} & seeded_ids

        expected_male_a_view = {male_a, female_c}
        assert visible_ids == expected_male_a_view, (
            f"Male-A requester view incorrect: expected {expected_male_a_view}, "
            f"got {visible_ids}. "
            "Own row (identity exception) + Female-C (opposite sex) must be visible; "
            "Male-B (same sex, different identity) must be filtered."
        )

        app.rollback()  # RESET GUCs by rolling back (SET is transaction-scoped)

        # ------------------------------------------------------------------
        # Assertion 3: Female-C as requester → should see own row + Male-A + Male-B
        #
        # sex != 'Female' → both Male-A and Male-B pass (opposite sex)
        # user_id = Female-C → Female-C passes (identity exception)
        # All three seeded rows are visible.
        # ------------------------------------------------------------------
        with app.cursor() as cur:
            cur.execute("SET app.requesting_user_id = %s", (str(female_c),))
            cur.execute("SET app.requesting_user_sex = 'Female'")
            cur.execute("SELECT user_id FROM users")
            visible_ids = {row[0] for row in cur.fetchall()} & seeded_ids

        expected_female_c_view = {female_c, male_a, male_b}
        assert visible_ids == expected_female_c_view, (
            f"Female-C requester view incorrect: expected {expected_female_c_view}, "
            f"got {visible_ids}. "
            "Own row + Male-A + Male-B (both opposite-sex) must all be visible."
        )

    finally:
        app.close()
        # Teardown: remove seeded rows as master
        with master.cursor() as cur:
            cur.execute(
                "DELETE FROM users WHERE user_id = ANY(%s)",
                ([male_a, male_b, female_c],),
            )
        master.close()
