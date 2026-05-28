"""
Integration test for knotify_db — Aurora-access layer RLS helper.

Requires a running docker-compose postgres container (pgvector/pgvector:0.8.2-pg16)
with all migrations applied and local_init.sql executed by the db_with_migrations
fixture in conftest.py.

Run:
    pytest infrastructure/src/layers/db/tests/test_knotify_db_integration.py -v -m integration

Skip in CI that lacks docker:
    pytest -m "not integration"

Story 3.3 AC tested here:
  - set_rls_context with a Male user → SELECT FROM users returns only Female
    rows plus the Male's own row (AC3 verbatim).
  - get_connection() returns a working psycopg2 connection.
  - rls_context context manager works end-to-end: BEGIN on entry, COMMIT on
    normal exit, GUCs expire at transaction end.
"""

import os
import uuid

import pytest
import psycopg2
import psycopg2.extras

psycopg2.extras.register_uuid()

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Connection parameters (match docker-compose.yml defaults)
# ---------------------------------------------------------------------------

_HOST = os.environ.get("PGHOST", "localhost")
_PORT = int(os.environ.get("PGPORT", "5432"))
_DBNAME = os.environ.get("PGDATABASE", "knotify")
_MASTER_USER = "knotify"
_MASTER_PASS = os.environ.get("PGPASSWORD", "knotify")
_APP_USER = "app_user"
_APP_PASS = os.environ.get("APP_USER_PASSWORD", "app_user")


def _master_conn():
    """Direct psycopg2 connection as the master user (bypasses RLS)."""
    conn = psycopg2.connect(
        host=_HOST, port=_PORT, dbname=_DBNAME,
        user=_MASTER_USER, password=_MASTER_PASS,
    )
    conn.autocommit = True
    return conn


def _app_conn_direct():
    """Direct psycopg2 connection as app_user (subject to RLS)."""
    return psycopg2.connect(
        host=_HOST, port=_PORT, dbname=_DBNAME,
        user=_APP_USER, password=_APP_PASS,
    )


# ---------------------------------------------------------------------------
# Module-scoped seed fixture (depends on db_with_migrations from conftest.py)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def seeded_users(db_with_migrations):
    """
    Insert one Male and one Female user as master (bypasses RLS).
    Yield their UUIDs.  Clean up after the module.
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
                male_id, f"male_{male_id}@test.invalid",
                f"testmale_{male_id}", "TestMale", "User", "Male",
            ))
            cur.execute(insert_sql, (
                female_id, f"female_{female_id}@test.invalid",
                f"testfemale_{female_id}", "TestFemale", "User", "Female",
            ))
    finally:
        conn.close()

    yield {"male_id": male_id, "female_id": female_id}

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
# AC3: set_rls_context with Male → sees own row + Female row only
# ---------------------------------------------------------------------------

def test_given_male_user_when_set_rls_context_then_sees_own_row_and_female_row(seeded_users):
    """
    AC3 (story 3.3): set_rls_context with a Male user → SELECT FROM users
    returns only female rows plus the male's own row.

    Uses knotify_db.set_rls_context within a manual BEGIN/ROLLBACK to
    scope the SET LOCAL GUCs.
    """
    from knotify_db import set_rls_context

    male_id = seeded_users["male_id"]
    female_id = seeded_users["female_id"]

    conn = _app_conn_direct()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute("BEGIN")

            # Use the knotify_db helper — this is the call under test
            set_rls_context(conn, str(male_id), "Male")

            cur.execute(
                "SELECT user_id, sex FROM users "
                "WHERE user_id IN (%s, %s) ORDER BY sex",
                (male_id, female_id),
            )
            rows = cur.fetchall()
            cur.execute("ROLLBACK")
    finally:
        conn.close()

    visible_ids = {row["user_id"] for row in rows}

    assert len(rows) == 2, (
        f"Expected 2 rows visible to Male (own + Female), got {len(rows)}. "
        f"Rows: {[dict(r) for r in rows]}"
    )
    assert male_id in visible_ids, (
        f"Male's own row ({male_id}) not visible. Visible: {visible_ids}"
    )
    assert female_id in visible_ids, (
        f"Female's row ({female_id}) not visible. Visible: {visible_ids}"
    )


def test_given_rls_context_manager_when_body_completes_then_transaction_committed(seeded_users):
    """
    rls_context owns the transaction lifecycle.
    After the context block exits normally, conn.commit() is called,
    which means SET LOCAL GUCs expire — a fresh connection (simulating the
    next request) sees the RLS fail-closed behaviour (no GUCs set → 0 rows).
    """
    from knotify_db import rls_context

    male_id = seeded_users["male_id"]
    female_id = seeded_users["female_id"]

    # Inside the context manager: transaction is open, GUCs are set
    rows_inside = []
    conn = _app_conn_direct()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            # rls_context issues BEGIN internally — connection must be in
            # autocommit=False mode (psycopg2 default), but NOT inside an
            # existing transaction before the context manager is entered.
            with rls_context(conn, str(male_id), "Male"):
                cur.execute(
                    "SELECT user_id FROM users "
                    "WHERE user_id IN (%s, %s)",
                    (male_id, female_id),
                )
                rows_inside = cur.fetchall()
            # After context exit: transaction committed, GUCs expired.
            # A new transaction on the same connection sees no GUCs.
            # Because the GUC session value is '' after the first SET LOCAL in
            # this session, we use a FRESH connection for the fail-closed check.
    finally:
        conn.close()

    # Fresh connection — no GUCs ever set → fail-closed (0 rows)
    conn2 = _app_conn_direct()
    try:
        with conn2.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM users WHERE user_id IN (%s, %s)",
                (male_id, female_id),
            )
            (count_fresh,) = cur.fetchone()
    finally:
        conn2.close()

    assert len(rows_inside) == 2, (
        f"Expected 2 rows inside rls_context (Male sees own + Female), "
        f"got {len(rows_inside)}"
    )
    assert count_fresh == 0, (
        f"Expected 0 rows on fresh connection (no GUCs → fail-closed), "
        f"got {count_fresh}"
    )


def test_given_get_connection_with_env_dict_when_called_then_returns_working_connection(
    db_with_migrations,
):
    """
    get_connection accepts an env-style dict with host/port/dbname/user/password
    and returns a connection that can execute a simple query.
    """
    from knotify_db import get_connection

    env_config = {
        "host": _HOST,
        "port": _PORT,
        "dbname": _DBNAME,
        "username": _APP_USER,
        "password": _APP_PASS,
    }

    conn = get_connection(env_config)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            (result,) = cur.fetchone()
        assert result == 1, f"Expected SELECT 1 = 1, got {result}"
    finally:
        conn.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-m", "integration"])
