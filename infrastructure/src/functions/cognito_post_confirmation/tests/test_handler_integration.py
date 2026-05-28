"""
Integration tests for cognito_post_confirmation handler.

Requires a running docker-compose postgres container (pgvector/pgvector:0.8.2-pg16)
with all migrations applied and local_init.sql executed by the db_with_migrations
fixture in conftest.py.

Run:
    pytest infrastructure/src/functions/cognito_post_confirmation/tests/ -v -m integration

Skip in CI that lacks docker:
    pytest -m "not integration"

Story 3.6 AC (bullet 5) tested here — five scenarios:
  (a) full attribute set → row with all populated columns
  (b) social-login minimal (email + given_name + family_name only,
      no gender / no birthdate) → row with first_name + last_name populated,
      sex / birthday / username NULL
  (c) gender variants "male", "M", "Female", "x" → mapped to Male, Male, Female, NULL
  (d) two successive invocations with identical event → exactly one users row
  (e) event missing email → no insert, handler returns event unmodified after logging
"""

import datetime
import os
import uuid

import psycopg2
import psycopg2.extras
import pytest

pytestmark = pytest.mark.integration

psycopg2.extras.register_uuid()

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

# The knotify_db connection dict — used to inject into the handler
# so it talks to the local container rather than fetching from Secrets Manager.
_APP_CONN_DICT = {
    "host": _HOST,
    "port": _PORT,
    "dbname": _DBNAME,
    "username": _APP_USER,
    "password": _APP_PASS,
}


def _master_conn():
    conn = psycopg2.connect(
        host=_HOST, port=_PORT, dbname=_DBNAME,
        user=_MASTER_USER, password=_MASTER_PASS,
    )
    conn.autocommit = True
    return conn


def _fetch_user_row(user_id: str) -> dict | None:
    """Fetch the users row for the given user_id as master (bypasses RLS)."""
    conn = _master_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT user_id, email, first_name, last_name, sex, birthday, username "
                "FROM users WHERE user_id = %s",
                (uuid.UUID(user_id),),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def _delete_user(user_id: str) -> None:
    conn = _master_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE user_id = %s", (uuid.UUID(user_id),))
    finally:
        conn.close()


def _count_users_with_id(user_id: str) -> int:
    conn = _master_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM users WHERE user_id = %s",
                (uuid.UUID(user_id),),
            )
            (count,) = cur.fetchone()
    finally:
        conn.close()
    return count


def _make_event(
    sub: str,
    email: str | None,
    given_name: str | None = None,
    family_name: str | None = None,
    gender: str | None = None,
    birthdate: str | None = None,
) -> dict:
    attrs: dict = {"sub": sub}
    if email is not None:
        attrs["email"] = email
    if given_name is not None:
        attrs["given_name"] = given_name
    if family_name is not None:
        attrs["family_name"] = family_name
    if gender is not None:
        attrs["gender"] = gender
    if birthdate is not None:
        attrs["birthdate"] = birthdate

    return {
        "version": "1",
        "triggerSource": "PostConfirmation_ConfirmSignUp",
        "region": "eu-central-1",
        "userPoolId": "eu-central-1_TESTPOOL",
        "userName": sub,
        "callerContext": {
            "awsSdkVersion": "aws-sdk-unknown-unknown",
            "clientId": "test-client-id",
        },
        "request": {"userAttributes": attrs},
        "response": {},
    }


def _invoke_handler(event: dict, conn_dict: dict | None = None) -> dict:
    """
    Invoke the handler with the local DB connection dict injected via
    monkey-patching the module-level _get_conn so no Secrets Manager call
    is made.
    """
    from unittest.mock import patch
    import handler as h
    from knotify_db import get_connection

    if conn_dict is None:
        conn_dict = _APP_CONN_DICT

    # Patch _get_conn at the handler module level so the handler
    # gets a real psycopg2 connection to the local container.
    with patch.object(h, "_get_conn", wraps=lambda: get_connection(conn_dict)):
        return h.handler(event, {})


# ---------------------------------------------------------------------------
# Scenario (a): Full attribute set → all columns populated
# ---------------------------------------------------------------------------

def test_given_full_attributes_when_handler_invoked_then_row_has_all_populated_columns(
    db_with_migrations,
):
    """
    AC scenario (a): full attribute set → row with all populated columns.
    """
    sub = str(uuid.uuid4())
    event = _make_event(
        sub=sub,
        email=f"{sub}@example.com",
        given_name="Alice",
        family_name="Smith",
        gender="female",
        birthdate="1995-06-15",
    )

    try:
        result = _invoke_handler(event)
        assert result == event, "Handler must return the event"

        row = _fetch_user_row(sub)
        assert row is not None, f"Expected users row for {sub}, got None"
        assert row["email"] == f"{sub}@example.com"
        assert row["first_name"] == "Alice"
        assert row["last_name"] == "Smith"
        assert row["sex"] == "Female"
        assert row["birthday"] == datetime.date(1995, 6, 15)
        assert row["username"] is None  # username not in Cognito event
    finally:
        _delete_user(sub)


# ---------------------------------------------------------------------------
# Scenario (b): Social-login minimal (email + names only, no gender/birthdate)
# ---------------------------------------------------------------------------

def test_given_social_login_minimal_event_when_handler_invoked_then_names_populated_sex_birthday_null(
    db_with_migrations,
):
    """
    AC scenario (b): social-login minimal → first_name + last_name populated,
    sex / birthday / username NULL.
    """
    sub = str(uuid.uuid4())
    event = _make_event(
        sub=sub,
        email=f"{sub}@social.example.com",
        given_name="Bob",
        family_name="Jones",
        gender=None,
        birthdate=None,
    )

    try:
        _invoke_handler(event)

        row = _fetch_user_row(sub)
        assert row is not None
        assert row["first_name"] == "Bob"
        assert row["last_name"] == "Jones"
        assert row["sex"] is None
        assert row["birthday"] is None
        assert row["username"] is None
    finally:
        _delete_user(sub)


# ---------------------------------------------------------------------------
# Scenario (c): Gender variants → correct canonical values
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw_gender,expected_sex", [
    ("male",   "Male"),
    ("M",      "Male"),
    ("Female", "Female"),
    ("x",      None),
])
def test_given_gender_variant_when_handler_invoked_then_sex_column_correct(
    raw_gender, expected_sex, db_with_migrations,
):
    """
    AC scenario (c): gender variants "male", "M", "Female", "x" →
    mapped to Male, Male, Female, NULL respectively.
    """
    sub = str(uuid.uuid4())
    event = _make_event(
        sub=sub,
        email=f"{sub}@gendertest.example.com",
        given_name=None,
        family_name=None,
        gender=raw_gender,
        birthdate=None,
    )

    try:
        _invoke_handler(event)

        row = _fetch_user_row(sub)
        assert row is not None
        assert row["sex"] == expected_sex, (
            f"For gender='{raw_gender}': expected sex='{expected_sex}', got '{row['sex']}'"
        )
    finally:
        _delete_user(sub)


# ---------------------------------------------------------------------------
# Scenario (d): Two successive invocations → exactly one users row
# ---------------------------------------------------------------------------

def test_given_two_successive_identical_invocations_when_handler_invoked_then_exactly_one_row(
    db_with_migrations,
):
    """
    AC scenario (d): two successive invocations with identical event →
    exactly one users row (ON CONFLICT DO NOTHING idempotency).
    """
    sub = str(uuid.uuid4())
    event = _make_event(
        sub=sub,
        email=f"{sub}@idempotent.example.com",
        given_name="Carol",
        family_name="White",
        gender="f",
        birthdate="2000-01-01",
    )

    try:
        _invoke_handler(event)
        _invoke_handler(event)  # second invocation — must be a no-op

        count = _count_users_with_id(sub)
        assert count == 1, (
            f"Expected exactly 1 users row after two invocations, got {count}"
        )
    finally:
        _delete_user(sub)


# ---------------------------------------------------------------------------
# Scenario (e): Missing email → no insert, event returned unmodified
# ---------------------------------------------------------------------------

def test_given_event_missing_email_when_handler_invoked_then_no_insert_and_returns_event(
    db_with_migrations,
):
    """
    AC scenario (e): event missing email → no insert,
    handler returns event unmodified after logging.
    """
    sub = str(uuid.uuid4())
    event = _make_event(
        sub=sub,
        email=None,  # deliberately missing
        given_name="Dave",
        family_name="Brown",
        gender="m",
        birthdate="1988-04-04",
    )

    result = _invoke_handler(event)

    assert result == event, "Handler must return event unmodified"

    count = _count_users_with_id(sub)
    assert count == 0, (
        f"Expected 0 rows when email is missing, found {count}"
    )
