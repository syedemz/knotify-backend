"""
Unit tests for story 9.2 — validate_deletion_request Lambda handler.

TDD: these tests were written BEFORE handler.py to drive the implementation.

Acceptance criteria tested:
  A. input.user_id == input.jwt_sub is required; mismatch raises UserIdMismatch
     (typed exception — class name controls Step Functions error matching).
     No DynamoDB call is made when user_id != jwt_sub.
  B. If no in-progress deletion exists, writes a "deletion_initiated" audit
     record to account_deletion_audit via DynamoDB PutItem and returns
     {validated: True, audit_event_id: <non-empty str>}.
  C. If an in-progress deletion row exists (event_type="deletion_initiated"
     with no corresponding "deletion_completed" or "deletion_failed" for the
     same user_id), raises DeletionInProgress without writing a new row.
  D. The "deletion_initiated" PutItem item includes user_id, event_id (uuid4),
     event_type="deletion_initiated", and expire_at as a DynamoDB Number
     (Unix epoch seconds, 7 years from now).
  E. audit_event_id in the response matches the event_id written to DynamoDB.
  F. PutItem targets the account_deletion_audit table (TABLE_AUDIT env var).
  G. Idempotency query uses KeyConditionExpression on user_id (PK), then
     filters in-memory to determine in-progress status — no separate
     "deletion_completed" / "deletion_failed" rows means the user is in-progress.
  H. The handler does NOT invoke the write_audit_log Lambda — it writes
     directly to DynamoDB. This keeps the dependency one-way (Step Functions
     calls both as separate tasks; this Lambda writes its own initiated row).

Input contract (documented for story 9.9 wiring):
  event = {
    "user_id":          str,   # Cognito sub of the account to be deleted
    "jwt_sub":          str,   # JWT sub extracted by the initiator Lambda (story 9.9)
    "purge_immediately": bool, # forwarded unchanged from StartExecution input
  }

Output contract:
  {
    "validated":     True,
    "audit_event_id": str,   # uuid4 of the "deletion_initiated" row written
  }

  The state machine uses ResultPath: '$.validation' so the original input
  fields (user_id, purge_immediately) survive unchanged past this task.

Exception contract (Step Functions Catch matches by Error string = class name):
  UserIdMismatch    — user_id != jwt_sub
  DeletionInProgress — an in-progress deletion already exists for user_id
"""

from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TABLE_NAME = "account_deletion_audit"
_SEVEN_YEARS_SECS = 7 * 365 * 86400


def _make_event(
    user_id: str = "user-abc-123",
    jwt_sub: str = "user-abc-123",
    purge_immediately: bool = False,
) -> dict:
    """Build a minimal Lambda input event."""
    return {
        "user_id": user_id,
        "jwt_sub": jwt_sub,
        "purge_immediately": purge_immediately,
    }


def _ddb_item(event_type: str, user_id: str = "user-abc-123") -> dict:
    """Return a minimal DynamoDB-typed item dict for a given event_type."""
    return {
        "user_id": {"S": user_id},
        "event_id": {"S": str(uuid.uuid4())},
        "event_type": {"S": event_type},
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_dynamo():
    """
    Patch handler._get_dynamo so no real AWS call is made.
    Returns the mock DynamoDB client so tests can assert on calls.
    By default query returns an empty Items list (no prior deletion).
    """
    with patch("validate_deletion_request.handler._get_dynamo") as mock_factory:
        mock_client = MagicMock()
        # Default: no in-progress deletion
        mock_client.query.return_value = {"Items": []}
        mock_factory.return_value = mock_client
        yield mock_client


# ---------------------------------------------------------------------------
# Test A: user_id / jwt_sub mismatch raises UserIdMismatch
# ---------------------------------------------------------------------------


def test_given_user_id_and_jwt_sub_mismatch_when_handler_called_then_raises_user_id_mismatch(
    mock_dynamo,
):
    """
    AC-A: when input.user_id != input.jwt_sub the handler must raise
    an exception whose class name is 'UserIdMismatch' so Step Functions
    can Catch it by that Error string.
    No DynamoDB call is made.
    """
    from validate_deletion_request import handler

    event = _make_event(user_id="user-abc-123", jwt_sub="completely-different-sub")
    with pytest.raises(Exception) as exc_info:
        handler.handler(event, None)

    assert type(exc_info.value).__name__ == "UserIdMismatch", (
        f"Expected exception class UserIdMismatch, got {type(exc_info.value).__name__}"
    )
    # No DynamoDB calls when the JWT check fails
    mock_dynamo.query.assert_not_called()
    mock_dynamo.put_item.assert_not_called()


def test_given_matching_user_id_and_jwt_sub_when_handler_called_then_no_user_id_mismatch_raised(
    mock_dynamo,
):
    """
    AC-A (negative): when user_id == jwt_sub no UserIdMismatch is raised.
    """
    from validate_deletion_request import handler

    event = _make_event(user_id="user-same", jwt_sub="user-same")
    # Should not raise
    handler.handler(event, None)


# ---------------------------------------------------------------------------
# Test B: happy path — no prior deletion, writes initiated row, returns summary
# ---------------------------------------------------------------------------


def test_given_no_prior_deletion_when_handler_called_then_returns_validated_true(
    mock_dynamo,
):
    """
    AC-B: happy path returns {validated: True, audit_event_id: <str>}.
    """
    from validate_deletion_request import handler

    result = handler.handler(_make_event(), None)

    assert result["validated"] is True


def test_given_no_prior_deletion_when_handler_called_then_returns_non_empty_audit_event_id(
    mock_dynamo,
):
    """
    AC-B: audit_event_id in the response is a non-empty string.
    """
    from validate_deletion_request import handler

    result = handler.handler(_make_event(), None)

    assert "audit_event_id" in result
    assert isinstance(result["audit_event_id"], str)
    assert len(result["audit_event_id"]) > 0


def test_given_no_prior_deletion_when_handler_called_then_put_item_called_once(
    mock_dynamo,
):
    """
    AC-B: exactly one PutItem call on the audit table.
    """
    from validate_deletion_request import handler

    handler.handler(_make_event(), None)

    mock_dynamo.put_item.assert_called_once()


# ---------------------------------------------------------------------------
# Test C: in-progress deletion raises DeletionInProgress
# ---------------------------------------------------------------------------


def test_given_in_progress_deletion_when_handler_called_then_raises_deletion_in_progress(
    mock_dynamo,
):
    """
    AC-C: if Query returns a 'deletion_initiated' row with no
    'deletion_completed' or 'deletion_failed' counterpart, the handler
    must raise DeletionInProgress.  No new PutItem is called.
    """
    from validate_deletion_request import handler

    # Simulate a prior initiated row only (no completed/failed)
    mock_dynamo.query.return_value = {
        "Items": [_ddb_item("deletion_initiated")]
    }

    event = _make_event()
    with pytest.raises(Exception) as exc_info:
        handler.handler(event, None)

    assert type(exc_info.value).__name__ == "DeletionInProgress", (
        f"Expected DeletionInProgress, got {type(exc_info.value).__name__}"
    )
    mock_dynamo.put_item.assert_not_called()


def test_given_prior_completed_deletion_when_handler_called_then_no_deletion_in_progress_raised(
    mock_dynamo,
):
    """
    AC-C (negative): if there is a 'deletion_initiated' row AND a
    'deletion_completed' row, the prior deletion finished — not in-progress.
    The handler should succeed and write a new initiated row.
    """
    from validate_deletion_request import handler

    uid = "user-completed-before"
    mock_dynamo.query.return_value = {
        "Items": [
            _ddb_item("deletion_initiated", uid),
            _ddb_item("deletion_completed", uid),
        ]
    }

    result = handler.handler(_make_event(user_id=uid, jwt_sub=uid), None)

    assert result["validated"] is True
    mock_dynamo.put_item.assert_called_once()


def test_given_prior_failed_deletion_when_handler_called_then_no_deletion_in_progress_raised(
    mock_dynamo,
):
    """
    AC-C (negative): if there is a 'deletion_initiated' row AND a
    'deletion_failed' row, the prior deletion ended (failed) — not in-progress.
    """
    from validate_deletion_request import handler

    uid = "user-failed-before"
    mock_dynamo.query.return_value = {
        "Items": [
            _ddb_item("deletion_initiated", uid),
            _ddb_item("deletion_failed", uid),
        ]
    }

    result = handler.handler(_make_event(user_id=uid, jwt_sub=uid), None)

    assert result["validated"] is True
    mock_dynamo.put_item.assert_called_once()


# ---------------------------------------------------------------------------
# Test D: PutItem item shape
# ---------------------------------------------------------------------------


def test_given_no_prior_deletion_when_handler_called_then_put_item_has_correct_event_type(
    mock_dynamo,
):
    """
    AC-D: written item has event_type S='deletion_initiated'.
    """
    from validate_deletion_request import handler

    handler.handler(_make_event(user_id="user-shape-check", jwt_sub="user-shape-check"), None)

    item = mock_dynamo.put_item.call_args.kwargs["Item"]
    assert item["event_type"]["S"] == "deletion_initiated"


def test_given_no_prior_deletion_when_handler_called_then_put_item_has_correct_user_id(
    mock_dynamo,
):
    """
    AC-D: written item has user_id (PK) matching the input.
    """
    from validate_deletion_request import handler

    uid = "user-shape-pk-check"
    handler.handler(_make_event(user_id=uid, jwt_sub=uid), None)

    item = mock_dynamo.put_item.call_args.kwargs["Item"]
    assert item["user_id"]["S"] == uid


def test_given_no_prior_deletion_when_handler_called_then_put_item_has_non_empty_event_id(
    mock_dynamo,
):
    """
    AC-D: written item has a non-empty event_id SK string.
    """
    from validate_deletion_request import handler

    handler.handler(_make_event(), None)

    item = mock_dynamo.put_item.call_args.kwargs["Item"]
    assert "event_id" in item
    assert isinstance(item["event_id"]["S"], str)
    assert len(item["event_id"]["S"]) > 0


def test_given_no_prior_deletion_when_handler_called_then_put_item_has_expire_at_as_number(
    mock_dynamo,
):
    """
    AC-D: expire_at must be a DynamoDB Number (N), not a String (S).
    Its value must be approximately now + 7 years (within ±60 seconds).
    """
    import time

    from validate_deletion_request import handler

    before = int(time.time()) + _SEVEN_YEARS_SECS
    handler.handler(_make_event(), None)
    after = int(time.time()) + _SEVEN_YEARS_SECS

    item = mock_dynamo.put_item.call_args.kwargs["Item"]
    assert "N" in item["expire_at"], (
        f"expire_at must be DynamoDB Number (N), got keys: {list(item['expire_at'].keys())}"
    )
    expire_value = int(item["expire_at"]["N"])
    assert before - 60 <= expire_value <= after + 60, (
        f"expire_at={expire_value} not within 7-year window [{before-60}, {after+60}]"
    )


# ---------------------------------------------------------------------------
# Test E: audit_event_id in response matches DDB event_id
# ---------------------------------------------------------------------------


def test_given_no_prior_deletion_when_handler_called_then_audit_event_id_matches_written_event_id(
    mock_dynamo,
):
    """
    AC-E: the audit_event_id in the response must be the same uuid that
    was written as the event_id SK in DynamoDB.
    """
    from validate_deletion_request import handler

    result = handler.handler(_make_event(), None)

    item = mock_dynamo.put_item.call_args.kwargs["Item"]
    ddb_event_id = item["event_id"]["S"]
    assert result["audit_event_id"] == ddb_event_id


# ---------------------------------------------------------------------------
# Test F: PutItem targets correct table
# ---------------------------------------------------------------------------


def test_given_no_prior_deletion_when_handler_called_then_put_item_targets_audit_table(
    mock_dynamo,
):
    """
    AC-F: PutItem must target the account_deletion_audit table.
    """
    from validate_deletion_request import handler

    handler.handler(_make_event(), None)

    call_kwargs = mock_dynamo.put_item.call_args.kwargs
    assert call_kwargs["TableName"] == _TABLE_NAME


# ---------------------------------------------------------------------------
# Test G: idempotency query targets correct table and user_id
# ---------------------------------------------------------------------------


def test_given_any_valid_input_when_handler_called_then_query_targets_audit_table(
    mock_dynamo,
):
    """
    AC-G: the idempotency check Query must target account_deletion_audit.
    """
    from validate_deletion_request import handler

    handler.handler(_make_event(), None)

    call_kwargs = mock_dynamo.query.call_args.kwargs
    assert call_kwargs["TableName"] == _TABLE_NAME


def test_given_any_valid_input_when_handler_called_then_query_uses_correct_user_id(
    mock_dynamo,
):
    """
    AC-G: the idempotency check Query must filter by the input user_id.
    """
    from validate_deletion_request import handler

    uid = "user-query-check"
    handler.handler(_make_event(user_id=uid, jwt_sub=uid), None)

    call_kwargs = mock_dynamo.query.call_args.kwargs
    # The expression attribute values must contain the user_id
    expr_values = call_kwargs.get("ExpressionAttributeValues", {})
    user_id_values = [v["S"] for v in expr_values.values() if "S" in v]
    assert uid in user_id_values, (
        f"Query must filter by user_id={uid!r}. ExpressionAttributeValues={expr_values}"
    )


# ---------------------------------------------------------------------------
# Test H: no write_audit_log Lambda invocation (direct DDB PutItem only)
# ---------------------------------------------------------------------------


def test_given_no_prior_deletion_when_handler_called_then_no_lambda_invocation(
    mock_dynamo,
):
    """
    AC-H: the handler writes directly to DynamoDB — it must NOT invoke the
    write_audit_log Lambda.  Patching boto3.client to detect any Lambda
    client creation proves this.
    """
    import boto3

    from validate_deletion_request import handler

    with patch("boto3.client") as mock_boto3_client:
        # Re-inject mock dynamo via _get_dynamo so the DDB path works
        with patch("validate_deletion_request.handler._get_dynamo") as mgd:
            mock_client = MagicMock()
            mock_client.query.return_value = {"Items": []}
            mgd.return_value = mock_client

            handler.handler(_make_event(), None)

        # boto3.client must not have been called with "lambda" — no Lambda invocation
        lambda_calls = [
            c for c in mock_boto3_client.call_args_list if c.args and c.args[0] == "lambda"
        ]
        assert len(lambda_calls) == 0, (
            "handler must not create a Lambda client — it should write to DynamoDB directly"
        )
