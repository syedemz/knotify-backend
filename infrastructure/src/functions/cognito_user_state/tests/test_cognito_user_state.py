"""
Unit tests for story 9.3 — cognito_user_state Lambda handler.

TDD: these tests were written BEFORE handler.py to drive the implementation.

Acceptance criteria tested:
  A. mode="disable" invokes cognito-idp AdminDisableUser for the given user_id
     and returns success.
  B. mode="delete" invokes cognito-idp AdminDeleteUser for the given user_id
     and returns success.
  C. mode="disable" when user does not exist (UserNotFoundException) is treated
     as success (idempotent — user already gone, nothing to disable).
  D. mode="disable" when user is already disabled (NotAuthorizedException with
     "already disabled" message) is treated as success.
  E. mode="delete" when user does not exist (UserNotFoundException) is treated
     as no-op success (idempotent).
  F. Unknown mode raises ValueError; neither AdminDisableUser nor AdminDeleteUser
     is called.
  G. The return value for all success paths includes {"user_id": ..., "mode": ...}.
  H. mode="disable" on a present, enabled user calls AdminDisableUser (not
     AdminDeleteUser).
  I. mode="delete" on a present user calls AdminDeleteUser (not AdminDisableUser).
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Environment variables required before module import
# ---------------------------------------------------------------------------

os.environ.setdefault("USER_POOL_ID", "eu-central-1_TESTPOOL")
os.environ.setdefault("AWS_DEFAULT_REGION", "eu-central-1")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(mode: str, user_id: str = "test-user-sub-001") -> dict:
    """Build a minimal Lambda input event for the given mode."""
    return {"mode": mode, "user_id": user_id}


def _make_client_error(code: str, message: str = ""):
    """Return a botocore ClientError with the given code and message."""
    from botocore.exceptions import ClientError

    return ClientError(
        {"Error": {"Code": code, "Message": message}},
        "operation_name",
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_cognito():
    """
    Patch handler._get_cognito_client so no real AWS call is made.
    Returns the mock Cognito IDP client so tests can assert calls.
    """
    with patch("cognito_user_state.handler._get_cognito_client") as mock_factory:
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        yield mock_client


# ---------------------------------------------------------------------------
# Test A: mode=disable happy path
# ---------------------------------------------------------------------------


def test_given_mode_disable_when_user_exists_then_admin_disable_user_is_called(
    mock_cognito,
):
    """
    AC-A: mode=disable calls AdminDisableUser with correct UserPoolId and Username.
    """
    from cognito_user_state import handler

    event = _make_event("disable", user_id="user-abc-001")
    handler.handler(event, None)

    mock_cognito.admin_disable_user.assert_called_once_with(
        UserPoolId="eu-central-1_TESTPOOL",
        Username="user-abc-001",
    )


def test_given_mode_disable_when_called_then_admin_delete_user_is_not_called(
    mock_cognito,
):
    """
    AC-H: mode=disable must NOT call AdminDeleteUser.
    """
    from cognito_user_state import handler

    handler.handler(_make_event("disable"), None)

    mock_cognito.admin_delete_user.assert_not_called()


def test_given_mode_disable_when_success_then_returns_user_id_and_mode(
    mock_cognito,
):
    """
    AC-G: success response includes user_id and mode keys.
    """
    from cognito_user_state import handler

    result = handler.handler(_make_event("disable", user_id="user-abc-002"), None)

    assert result["user_id"] == "user-abc-002"
    assert result["mode"] == "disable"


# ---------------------------------------------------------------------------
# Test B: mode=delete happy path
# ---------------------------------------------------------------------------


def test_given_mode_delete_when_user_exists_then_admin_delete_user_is_called(
    mock_cognito,
):
    """
    AC-B: mode=delete calls AdminDeleteUser with correct UserPoolId and Username.
    """
    from cognito_user_state import handler

    event = _make_event("delete", user_id="user-abc-003")
    handler.handler(event, None)

    mock_cognito.admin_delete_user.assert_called_once_with(
        UserPoolId="eu-central-1_TESTPOOL",
        Username="user-abc-003",
    )


def test_given_mode_delete_when_called_then_admin_disable_user_is_not_called(
    mock_cognito,
):
    """
    AC-I: mode=delete must NOT call AdminDisableUser.
    """
    from cognito_user_state import handler

    handler.handler(_make_event("delete"), None)

    mock_cognito.admin_disable_user.assert_not_called()


def test_given_mode_delete_when_success_then_returns_user_id_and_mode(
    mock_cognito,
):
    """
    AC-G: success response includes user_id and mode keys.
    """
    from cognito_user_state import handler

    result = handler.handler(_make_event("delete", user_id="user-abc-004"), None)

    assert result["user_id"] == "user-abc-004"
    assert result["mode"] == "delete"


# ---------------------------------------------------------------------------
# Test C: mode=disable idempotency — UserNotFoundException
# ---------------------------------------------------------------------------


def test_given_mode_disable_when_user_not_found_then_returns_success(
    mock_cognito,
):
    """
    AC-C: mode=disable on a missing user (UserNotFoundException) is a success.
    The deleted user cannot be disabled; treating this as success makes the
    state machine's DisableCognitoUser step safe to retry after the user has
    already been deleted by another path.
    """
    from cognito_user_state import handler

    mock_cognito.admin_disable_user.side_effect = _make_client_error(
        "UserNotFoundException", "User does not exist"
    )

    result = handler.handler(_make_event("disable", user_id="user-already-gone"), None)

    assert result["user_id"] == "user-already-gone"
    assert result["mode"] == "disable"


# ---------------------------------------------------------------------------
# Test D: mode=disable idempotency — already-disabled NotAuthorizedException
# ---------------------------------------------------------------------------


def test_given_mode_disable_when_already_disabled_then_returns_success(
    mock_cognito,
):
    """
    AC-D: mode=disable when Cognito returns NotAuthorizedException with
    "already disabled" message treats the call as success.
    Cognito does NOT raise a distinct error for "already disabled"; instead it
    raises NotAuthorizedException with a message containing "already disabled".
    """
    from cognito_user_state import handler

    mock_cognito.admin_disable_user.side_effect = _make_client_error(
        "NotAuthorizedException",
        "User is already disabled",
    )

    result = handler.handler(_make_event("disable", user_id="user-was-disabled"), None)

    assert result["user_id"] == "user-was-disabled"
    assert result["mode"] == "disable"


def test_given_mode_disable_when_not_auth_without_already_disabled_msg_then_raises(
    mock_cognito,
):
    """
    AC-D boundary: NotAuthorizedException that is NOT about already-disabled
    must propagate (e.g. permission denied on the role itself).
    """
    from cognito_user_state import handler

    mock_cognito.admin_disable_user.side_effect = _make_client_error(
        "NotAuthorizedException",
        "Access denied",
    )

    with pytest.raises(Exception):
        handler.handler(_make_event("disable"), None)


# ---------------------------------------------------------------------------
# Test E: mode=delete idempotency — UserNotFoundException
# ---------------------------------------------------------------------------


def test_given_mode_delete_when_user_not_found_then_returns_success(
    mock_cognito,
):
    """
    AC-E: mode=delete on a missing user (UserNotFoundException) is a no-op success.
    The user is already gone — the delete is idempotent.
    """
    from cognito_user_state import handler

    mock_cognito.admin_delete_user.side_effect = _make_client_error(
        "UserNotFoundException", "User does not exist"
    )

    result = handler.handler(_make_event("delete", user_id="user-already-deleted"), None)

    assert result["user_id"] == "user-already-deleted"
    assert result["mode"] == "delete"


# ---------------------------------------------------------------------------
# Test F: unknown mode raises ValueError
# ---------------------------------------------------------------------------


def test_given_unknown_mode_when_handler_called_then_raises_value_error(
    mock_cognito,
):
    """
    AC-F: an unrecognised mode raises ValueError; neither Cognito API is called.
    """
    from cognito_user_state import handler

    with pytest.raises(ValueError, match="invalid_mode"):
        handler.handler(_make_event("suspend"), None)

    mock_cognito.admin_disable_user.assert_not_called()
    mock_cognito.admin_delete_user.assert_not_called()


def test_given_empty_mode_when_handler_called_then_raises_value_error(
    mock_cognito,
):
    """
    AC-F: empty string mode raises ValueError.
    """
    from cognito_user_state import handler

    with pytest.raises(ValueError):
        handler.handler(_make_event(""), None)

    mock_cognito.admin_disable_user.assert_not_called()
    mock_cognito.admin_delete_user.assert_not_called()


# ---------------------------------------------------------------------------
# Test: double-disable is idempotent (second call also succeeds)
# ---------------------------------------------------------------------------


def test_given_mode_disable_when_called_twice_then_both_succeed(
    mock_cognito,
):
    """
    AC-D: invoking mode=disable twice on the same user succeeds on both calls.
    Second call simulates the "already disabled" path.
    """
    from cognito_user_state import handler

    # First call succeeds normally
    handler.handler(_make_event("disable", user_id="user-double-disable"), None)

    # Second call: Cognito reports user is already disabled
    mock_cognito.admin_disable_user.side_effect = _make_client_error(
        "NotAuthorizedException",
        "User is already disabled",
    )
    result = handler.handler(_make_event("disable", user_id="user-double-disable"), None)

    assert result["user_id"] == "user-double-disable"


# ---------------------------------------------------------------------------
# Test: delete after disable succeeds
# ---------------------------------------------------------------------------


def test_given_mode_delete_after_disable_when_called_then_succeeds(
    mock_cognito,
):
    """
    The state machine wires DisableCognitoUser before DeleteCognitoUser.
    After disable, delete must succeed normally.
    """
    from cognito_user_state import handler

    result = handler.handler(_make_event("delete", user_id="user-post-disable"), None)

    mock_cognito.admin_delete_user.assert_called_once()
    assert result["mode"] == "delete"
