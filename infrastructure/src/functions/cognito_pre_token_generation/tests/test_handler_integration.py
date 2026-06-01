"""
Integration tests for cognito_pre_token_generation handler.

Requires a running docker-compose postgres container (pgvector/pgvector:0.8.2-pg16)
with all migrations applied and local_init.sql executed by the db_with_migrations
fixture in conftest.py.

Run:
    pytest infrastructure/src/functions/cognito_pre_token_generation/tests/ -v -m integration

Skip in CI that lacks docker:
    pytest -m "not integration"

Story 4.4 AC (integration bullet) tested here — two scenarios:
  (a) profile_complete_verified=False → claim "false" on BOTH
      idTokenGeneration and accessTokenGeneration claimsToAddOrOverride
  (b) profile_complete_verified=True  → claim "true"  on BOTH
"""

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

# knotify_db connection dict injected into the handler so no Secrets Manager
# call is made during integration tests.
_APP_CONN_DICT = {
    "host": _HOST,
    "port": _PORT,
    "dbname": _DBNAME,
    "username": _APP_USER,
    "password": _APP_PASS,
}


# ---------------------------------------------------------------------------
# DB helpers — all run as master to bypass RLS
# ---------------------------------------------------------------------------

def _master_conn():
    conn = psycopg2.connect(
        host=_HOST, port=_PORT, dbname=_DBNAME,
        user=_MASTER_USER, password=_MASTER_PASS,
    )
    conn.autocommit = True
    return conn


def _insert_user(user_id: str, profile_complete: bool) -> None:
    """Insert a minimal users row with the given profile_complete_verified value."""
    conn = _master_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (user_id, email, profile_complete_verified)
                VALUES (%s, %s, %s)
                """,
                (uuid.UUID(user_id), f"{user_id}@example.com", profile_complete),
            )
    finally:
        conn.close()


def _delete_user(user_id: str) -> None:
    conn = _master_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE user_id = %s", (uuid.UUID(user_id),))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Event factory
# ---------------------------------------------------------------------------

def _make_v2_event(sub: str, trigger_source: str = "TokenGeneration_Authentication") -> dict:
    return {
        "version": "2",
        "triggerSource": trigger_source,
        "region": "eu-central-1",
        "userPoolId": "eu-central-1_TESTPOOL",
        "userName": sub,
        "callerContext": {
            "awsSdkVersion": "aws-sdk-unknown-unknown",
            "clientId": "test-client-id",
        },
        "request": {
            "userAttributes": {
                "sub": sub,
                "email": f"{sub}@example.com",
                "email_verified": "true",
            },
            "scopes": [],
        },
        "response": {},
    }


# ---------------------------------------------------------------------------
# Handler invoker — patches _get_conn to use the local container
# ---------------------------------------------------------------------------

def _invoke_handler(event: dict) -> dict:
    from unittest.mock import patch
    import handler as h
    from knotify_db import get_connection

    with patch.object(h, "_get_conn", wraps=lambda: get_connection(_APP_CONN_DICT)):
        return h.handler(event, {})


# ---------------------------------------------------------------------------
# Scenario (a): profile_complete_verified=False → claim "false" on both tokens
# ---------------------------------------------------------------------------

def test_given_profile_complete_false_in_db_when_handler_invoked_then_both_token_claims_are_false(
    db_with_migrations,
):
    """
    AC scenario (a): users row has profile_complete_verified=False.
    Both idTokenGeneration and accessTokenGeneration claimsToAddOrOverride
    must have custom:profile_complete = "false".
    """
    sub = str(uuid.uuid4())
    _insert_user(sub, profile_complete=False)

    try:
        event = _make_v2_event(sub, "TokenGeneration_Authentication")
        result = _invoke_handler(event)

        claims_override = result["response"]["claimsAndScopeOverrideDetails"]

        id_claim = claims_override["idTokenGeneration"]["claimsToAddOrOverride"].get(
            "custom:profile_complete"
        )
        access_claim = claims_override["accessTokenGeneration"]["claimsToAddOrOverride"].get(
            "custom:profile_complete"
        )

        assert id_claim == "false", (
            f"idTokenGeneration custom:profile_complete must be 'false', got '{id_claim}'"
        )
        assert access_claim == "false", (
            f"accessTokenGeneration custom:profile_complete must be 'false', got '{access_claim}'"
        )
    finally:
        _delete_user(sub)


# ---------------------------------------------------------------------------
# Scenario (b): profile_complete_verified=True → claim "true" on both tokens
# ---------------------------------------------------------------------------

def test_given_profile_complete_true_in_db_when_handler_invoked_then_both_token_claims_are_true(
    db_with_migrations,
):
    """
    AC scenario (b): users row has profile_complete_verified=True.
    Both idTokenGeneration and accessTokenGeneration claimsToAddOrOverride
    must have custom:profile_complete = "true".
    """
    sub = str(uuid.uuid4())
    _insert_user(sub, profile_complete=True)

    try:
        event = _make_v2_event(sub, "TokenGeneration_Authentication")
        result = _invoke_handler(event)

        claims_override = result["response"]["claimsAndScopeOverrideDetails"]

        id_claim = claims_override["idTokenGeneration"]["claimsToAddOrOverride"].get(
            "custom:profile_complete"
        )
        access_claim = claims_override["accessTokenGeneration"]["claimsToAddOrOverride"].get(
            "custom:profile_complete"
        )

        assert id_claim == "true", (
            f"idTokenGeneration custom:profile_complete must be 'true', got '{id_claim}'"
        )
        assert access_claim == "true", (
            f"accessTokenGeneration custom:profile_complete must be 'true', got '{access_claim}'"
        )
    finally:
        _delete_user(sub)


# ---------------------------------------------------------------------------
# Scenario (c): Missing users row → claim "false" on both tokens, no raise
# ---------------------------------------------------------------------------

def test_given_missing_users_row_when_handler_invoked_then_both_token_claims_false_and_no_exception(
    db_with_migrations,
):
    """
    Brainstorm Md2: if no users row exists for the sub,
    both claims default to "false" and the handler does not raise.
    This is the missing-row branch — the warning is logged but login proceeds.
    """
    sub = str(uuid.uuid4())
    # Deliberately do NOT insert a users row for this sub

    event = _make_v2_event(sub, "TokenGeneration_Authentication")
    result = _invoke_handler(event)  # must not raise

    claims_override = result["response"]["claimsAndScopeOverrideDetails"]

    id_claim = claims_override["idTokenGeneration"]["claimsToAddOrOverride"].get(
        "custom:profile_complete"
    )
    access_claim = claims_override["accessTokenGeneration"]["claimsToAddOrOverride"].get(
        "custom:profile_complete"
    )

    assert id_claim == "false", (
        f"idTokenGeneration must be 'false' when users row is missing, got '{id_claim}'"
    )
    assert access_claim == "false", (
        f"accessTokenGeneration must be 'false' when users row is missing, got '{access_claim}'"
    )
