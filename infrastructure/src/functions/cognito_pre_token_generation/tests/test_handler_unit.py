"""
Unit tests for cognito_pre_token_generation handler.

All tests use unittest.mock — no real DB or AWS credentials needed.

Behavior under test:
  - V2 TokenGeneration_Authentication event → profile_complete_verified=True
    → both idTokenGeneration and accessTokenGeneration claims set to "true"
  - V2 TokenGeneration_Authentication event → profile_complete_verified=False
    → both claims set to "false"
  - V2 TokenGeneration_RefreshTokens event is handled identically (same V2 shape)
  - Missing users row → claims set to "false" on both tokens, warning logged,
    handler does NOT raise (failing PreTokenGeneration blocks login — Md2)
  - DB exception → claims set to "false" on both tokens, error logged,
    handler does NOT raise
  - Handler always returns the mutated event (never raises)
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers — build minimal V2 PreTokenGeneration events
# ---------------------------------------------------------------------------

def _make_v2_event(
    sub: str,
    trigger_source: str = "TokenGeneration_Authentication",
) -> dict:
    """
    Minimal V2 PreTokenGeneration event.

    The V2 shape requires `response` to be a plain dict — Cognito reads the
    handler's return value and merges claimsAndScopeOverrideDetails from it.
    """
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


def _invoke(event: dict, conn_mock: MagicMock) -> dict:
    """
    Invoke the handler with a patched _get_conn.

    conn_mock should already be configured with the desired cursor behavior.
    """
    import handler as h
    with patch.object(h, "_get_conn", return_value=conn_mock):
        return h.handler(event, {})


def _make_cursor_returning(row):
    """Return a mock cursor whose fetchone() returns the given row."""
    cursor = MagicMock()
    cursor.__enter__ = lambda s: s
    cursor.__exit__ = MagicMock(return_value=False)
    cursor.fetchone.return_value = row
    return cursor


def _make_conn(fetchone_row):
    """Build a minimal psycopg2 connection mock that returns fetchone_row."""
    conn = MagicMock()
    cursor = _make_cursor_returning(fetchone_row)
    conn.cursor.return_value = cursor
    return conn


# ---------------------------------------------------------------------------
# Test 1: profile_complete_verified=True → both token claims = "true"
# ---------------------------------------------------------------------------

def test_given_profile_complete_true_when_authentication_event_then_both_claims_are_true():
    """
    Given users row has profile_complete_verified=True,
    when a TokenGeneration_Authentication event arrives,
    then both idTokenGeneration and accessTokenGeneration claims are "true".
    """
    sub = str(uuid.uuid4())
    event = _make_v2_event(sub, "TokenGeneration_Authentication")
    conn = _make_conn(fetchone_row=(True,))

    result = _invoke(event, conn)

    claims_override = result["response"]["claimsAndScopeOverrideDetails"]
    id_claims = claims_override["idTokenGeneration"]["claimsToAddOrOverride"]
    access_claims = claims_override["accessTokenGeneration"]["claimsToAddOrOverride"]

    assert id_claims["custom:profile_complete"] == "true", (
        "idTokenGeneration custom:profile_complete must be 'true' when DB row is True"
    )
    assert access_claims["custom:profile_complete"] == "true", (
        "accessTokenGeneration custom:profile_complete must be 'true' when DB row is True"
    )


# ---------------------------------------------------------------------------
# Test 2: profile_complete_verified=False → both token claims = "false"
# ---------------------------------------------------------------------------

def test_given_profile_complete_false_when_authentication_event_then_both_claims_are_false():
    """
    Given users row has profile_complete_verified=False,
    when a TokenGeneration_Authentication event arrives,
    then both idTokenGeneration and accessTokenGeneration claims are "false".
    """
    sub = str(uuid.uuid4())
    event = _make_v2_event(sub, "TokenGeneration_Authentication")
    conn = _make_conn(fetchone_row=(False,))

    result = _invoke(event, conn)

    claims_override = result["response"]["claimsAndScopeOverrideDetails"]
    id_claims = claims_override["idTokenGeneration"]["claimsToAddOrOverride"]
    access_claims = claims_override["accessTokenGeneration"]["claimsToAddOrOverride"]

    assert id_claims["custom:profile_complete"] == "false", (
        "idTokenGeneration custom:profile_complete must be 'false' when DB row is False"
    )
    assert access_claims["custom:profile_complete"] == "false", (
        "accessTokenGeneration custom:profile_complete must be 'false' when DB row is False"
    )


# ---------------------------------------------------------------------------
# Test 3: RefreshTokens trigger source handled identically to Authentication
# ---------------------------------------------------------------------------

def test_given_profile_complete_true_when_refresh_tokens_event_then_both_claims_are_true():
    """
    V2 TokenGeneration_RefreshTokens events share the same shape as
    TokenGeneration_Authentication. The handler must set claims on both tokens.
    """
    sub = str(uuid.uuid4())
    event = _make_v2_event(sub, "TokenGeneration_RefreshTokens")
    conn = _make_conn(fetchone_row=(True,))

    result = _invoke(event, conn)

    claims_override = result["response"]["claimsAndScopeOverrideDetails"]
    assert claims_override["idTokenGeneration"]["claimsToAddOrOverride"]["custom:profile_complete"] == "true"
    assert claims_override["accessTokenGeneration"]["claimsToAddOrOverride"]["custom:profile_complete"] == "true"


# ---------------------------------------------------------------------------
# Test 4: Missing users row → claims "false" on both tokens, no raise (Md2)
# ---------------------------------------------------------------------------

def test_given_missing_users_row_when_handler_invoked_then_claims_false_and_no_exception():
    """
    Brainstorm Md2: if the users row is missing (fetchone returns None),
    the handler returns custom:profile_complete="false" on BOTH tokens and
    does NOT raise — raising PreTokenGeneration blocks login.
    """
    sub = str(uuid.uuid4())
    event = _make_v2_event(sub, "TokenGeneration_Authentication")
    conn = _make_conn(fetchone_row=None)  # row missing

    result = _invoke(event, conn)  # must not raise

    claims_override = result["response"]["claimsAndScopeOverrideDetails"]
    id_claims = claims_override["idTokenGeneration"]["claimsToAddOrOverride"]
    access_claims = claims_override["accessTokenGeneration"]["claimsToAddOrOverride"]

    assert id_claims["custom:profile_complete"] == "false", (
        "idTokenGeneration must be 'false' when users row is missing"
    )
    assert access_claims["custom:profile_complete"] == "false", (
        "accessTokenGeneration must be 'false' when users row is missing"
    )


# ---------------------------------------------------------------------------
# Test 5: DB exception → claims "false", no raise
# ---------------------------------------------------------------------------

def test_given_db_exception_when_handler_invoked_then_claims_false_and_no_exception():
    """
    DB exceptions must be caught and logged — not re-raised.
    Both token claims must still be set to "false" so the login is not blocked.
    """
    sub = str(uuid.uuid4())
    event = _make_v2_event(sub, "TokenGeneration_Authentication")

    conn = MagicMock()
    conn.cursor.side_effect = Exception("simulated DB connection failure")

    result = _invoke(event, conn)  # must not raise

    claims_override = result["response"]["claimsAndScopeOverrideDetails"]
    assert claims_override["idTokenGeneration"]["claimsToAddOrOverride"]["custom:profile_complete"] == "false"
    assert claims_override["accessTokenGeneration"]["claimsToAddOrOverride"]["custom:profile_complete"] == "false"


# ---------------------------------------------------------------------------
# Test 6: Handler always returns the event object (mutated, not a new object)
# ---------------------------------------------------------------------------

def test_given_any_event_handler_returns_the_event_dict():
    """
    Cognito requires the PreTokenGeneration handler to return the (mutated)
    event dict. Returning a different object would break Cognito's claim
    injection. Verify the returned object is the same dict (identity check).
    """
    sub = str(uuid.uuid4())
    event = _make_v2_event(sub, "TokenGeneration_Authentication")
    conn = _make_conn(fetchone_row=(True,))

    result = _invoke(event, conn)

    assert result is event, "Handler must return the same event dict it received"


# ---------------------------------------------------------------------------
# Test 7: Connection is closed after successful DB read
# ---------------------------------------------------------------------------

def test_given_successful_db_read_when_handler_invoked_then_connection_closed():
    """
    Lambda functions open one connection per invocation. The connection must
    be closed before the handler returns to avoid ENI/file-descriptor leaks.
    """
    sub = str(uuid.uuid4())
    event = _make_v2_event(sub, "TokenGeneration_Authentication")
    conn = _make_conn(fetchone_row=(True,))

    _invoke(event, conn)

    conn.close.assert_called_once()


# ---------------------------------------------------------------------------
# Test 8: Connection is closed even when DB raises (finally block coverage)
# ---------------------------------------------------------------------------

def test_given_db_exception_when_handler_invoked_then_connection_closed():
    """
    The connection must be closed in a finally block so it is always released,
    even when the DB query raises an exception.
    """
    sub = str(uuid.uuid4())
    event = _make_v2_event(sub, "TokenGeneration_Authentication")

    # cursor context manager works, but execute raises
    conn = MagicMock()
    cursor = MagicMock()
    cursor.__enter__ = lambda s: s
    cursor.__exit__ = MagicMock(return_value=False)
    cursor.execute.side_effect = Exception("query failed")
    conn.cursor.return_value = cursor

    _invoke(event, conn)  # must not raise

    conn.close.assert_called_once()
