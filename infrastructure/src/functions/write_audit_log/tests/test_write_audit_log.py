"""
Unit tests for story 9.8 — write_audit_log Lambda handler.

TDD: these tests were written BEFORE handler.py to drive the implementation.

Acceptance criteria tested:
  A. event_type="deletion_initiated" writes a record with user_id, event_id,
     event_type, created_at, and expire_at (7-year TTL as Unix epoch Number).
  B. event_type="deletion_completed" with purge_immediately=False writes a
     record with branches_succeeded, completed_at, execution_arn, and
     dynamodb_retention="permanent_anonymized".
  C. event_type="deletion_completed" with purge_immediately=True writes a
     record with dynamodb_retention="hard_deleted".
  D. event_type="deletion_failed" writes a record including failed_state_name,
     error, cause, and execution_arn.
  E. expire_at is an integer Unix epoch seconds (NOT an ISO string) equal to
     approximately now + 7*365*86400 (within a 5-second window).
  F. PutItem is called on the account_deletion_audit table.
  G. event_id is a non-empty string (unique per call — different ULIDs each call).
  H. Unknown event_type raises ValueError and does NOT call PutItem.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TABLE_NAME = "account_deletion_audit"
_SEVEN_YEARS_SECS = 7 * 365 * 86400


def _make_event(
    event_type: str,
    user_id: str = "user-abc-123",
    execution_arn: str = "arn:aws:states:eu-central-1:123456789012:execution:knotify-dev-account-deletion:exec-001",
    **extra,
) -> dict:
    """Build a minimal Lambda input event for the given event_type."""
    payload: dict = {
        "event_type": event_type,
        "user_id": user_id,
        "execution_arn": execution_arn,
    }
    payload.update(extra)
    return payload


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_dynamo():
    """
    Patch handler._get_dynamo so no real AWS call is made.
    Returns the mock DynamoDB client so tests can assert on put_item calls.
    """
    with patch("write_audit_log.handler._get_dynamo") as mock_factory:
        mock_client = MagicMock()
        mock_factory.return_value = mock_client
        yield mock_client


# ---------------------------------------------------------------------------
# Test A: deletion_initiated row shape
# ---------------------------------------------------------------------------


def test_given_deletion_initiated_when_handler_called_then_put_item_called_once(
    mock_dynamo,
):
    """
    AC-A: event_type=deletion_initiated causes exactly one PutItem call.
    """
    from write_audit_log import handler

    event = _make_event("deletion_initiated")
    handler.handler(event, None)

    mock_dynamo.put_item.assert_called_once()


def test_given_deletion_initiated_when_handler_called_then_row_has_correct_event_type(
    mock_dynamo,
):
    """
    AC-A: written item has event_type S=deletion_initiated.
    """
    from write_audit_log import handler

    event = _make_event("deletion_initiated", user_id="user-test-1")
    handler.handler(event, None)

    call_kwargs = mock_dynamo.put_item.call_args.kwargs
    item = call_kwargs["Item"]
    assert item["event_type"]["S"] == "deletion_initiated"


def test_given_deletion_initiated_when_handler_called_then_row_has_user_id(
    mock_dynamo,
):
    """
    AC-A: written item has user_id (PK) matching the input.
    """
    from write_audit_log import handler

    user_id = "user-test-uid-check"
    event = _make_event("deletion_initiated", user_id=user_id)
    handler.handler(event, None)

    call_kwargs = mock_dynamo.put_item.call_args.kwargs
    item = call_kwargs["Item"]
    assert item["user_id"]["S"] == user_id


def test_given_deletion_initiated_when_handler_called_then_row_has_non_empty_event_id(
    mock_dynamo,
):
    """
    AC-G: written item has a non-empty event_id SK string.
    """
    from write_audit_log import handler

    event = _make_event("deletion_initiated")
    handler.handler(event, None)

    call_kwargs = mock_dynamo.put_item.call_args.kwargs
    item = call_kwargs["Item"]
    assert "event_id" in item
    event_id = item["event_id"]["S"]
    assert isinstance(event_id, str) and len(event_id) > 0


def test_given_two_calls_when_handler_invoked_twice_then_event_ids_are_different(
    mock_dynamo,
):
    """
    AC-G: event_id must be unique across calls (ULID or UUID-based generation).
    """
    from write_audit_log import handler

    handler.handler(_make_event("deletion_initiated", user_id="u1"), None)
    handler.handler(_make_event("deletion_initiated", user_id="u2"), None)

    calls = mock_dynamo.put_item.call_args_list
    id1 = calls[0].kwargs["Item"]["event_id"]["S"]
    id2 = calls[1].kwargs["Item"]["event_id"]["S"]
    assert id1 != id2, "event_id must be unique per invocation"


# ---------------------------------------------------------------------------
# Test E: expire_at is a Number (Unix epoch) ~= now + 7 years
# ---------------------------------------------------------------------------


def test_given_any_event_type_when_handler_called_then_expire_at_is_number_near_seven_years_from_now(
    mock_dynamo,
):
    """
    AC-E: expire_at must be an integer Unix epoch Number — NOT a string.
    Its value must be approximately now + 7*365*86400 (within ±60 seconds).
    """
    from write_audit_log import handler

    before = int(time.time()) + _SEVEN_YEARS_SECS
    handler.handler(_make_event("deletion_initiated"), None)
    after = int(time.time()) + _SEVEN_YEARS_SECS

    call_kwargs = mock_dynamo.put_item.call_args.kwargs
    item = call_kwargs["Item"]

    # Must be a Number (N), not a String (S)
    assert "N" in item["expire_at"], (
        f"expire_at must be DynamoDB Number (N), got keys: {list(item['expire_at'].keys())}"
    )
    expire_at_value = int(item["expire_at"]["N"])
    assert before - 60 <= expire_at_value <= after + 60, (
        f"expire_at={expire_at_value} not within expected 7-year window [{before-60}, {after+60}]"
    )


# ---------------------------------------------------------------------------
# Test F: PutItem table name
# ---------------------------------------------------------------------------


def test_given_any_valid_event_type_when_handler_called_then_putitem_uses_correct_table(
    mock_dynamo,
):
    """
    AC-F: PutItem must target the account_deletion_audit table (from TABLE_AUDIT env var).
    """
    from write_audit_log import handler

    event = _make_event("deletion_initiated")
    handler.handler(event, None)

    call_kwargs = mock_dynamo.put_item.call_args.kwargs
    assert call_kwargs["TableName"] == _TABLE_NAME


# ---------------------------------------------------------------------------
# Test B: deletion_completed soft-delete row shape
# ---------------------------------------------------------------------------


def test_given_deletion_completed_soft_delete_when_handler_called_then_dynamodb_retention_is_permanent_anonymized(
    mock_dynamo,
):
    """
    AC-B: deletion_completed with purge_immediately=False sets
    dynamodb_retention="permanent_anonymized".
    """
    from write_audit_log import handler

    event = _make_event(
        "deletion_completed",
        purge_immediately=False,
        branches_succeeded=["SoftDeleteAurora", "DeleteDynamoDBPersonalData", "AnonymizeChatMessages"],
        completed_at="2026-06-20T10:00:00.000000Z",
    )
    handler.handler(event, None)

    call_kwargs = mock_dynamo.put_item.call_args.kwargs
    item = call_kwargs["Item"]
    assert item["event_type"]["S"] == "deletion_completed"
    assert item["dynamodb_retention"]["S"] == "permanent_anonymized"


def test_given_deletion_completed_soft_delete_when_handler_called_then_has_branches_succeeded(
    mock_dynamo,
):
    """
    AC-B: deletion_completed row must include branches_succeeded as a List of strings.
    """
    from write_audit_log import handler

    branches = ["SoftDeleteAurora", "DeleteDynamoDBPersonalData", "AnonymizeChatMessages"]
    event = _make_event(
        "deletion_completed",
        purge_immediately=False,
        branches_succeeded=branches,
        completed_at="2026-06-20T10:00:00.000000Z",
    )
    handler.handler(event, None)

    call_kwargs = mock_dynamo.put_item.call_args.kwargs
    item = call_kwargs["Item"]
    assert "branches_succeeded" in item
    # DynamoDB List: {"L": [{"S": "..."}, ...]}
    stored = [entry["S"] for entry in item["branches_succeeded"]["L"]]
    assert stored == branches


def test_given_deletion_completed_soft_delete_when_handler_called_then_has_completed_at(
    mock_dynamo,
):
    """
    AC-B: deletion_completed row must include completed_at string.
    """
    from write_audit_log import handler

    completed_at = "2026-06-20T10:00:00.000000Z"
    event = _make_event(
        "deletion_completed",
        purge_immediately=False,
        branches_succeeded=[],
        completed_at=completed_at,
    )
    handler.handler(event, None)

    call_kwargs = mock_dynamo.put_item.call_args.kwargs
    item = call_kwargs["Item"]
    assert item["completed_at"]["S"] == completed_at


def test_given_deletion_completed_soft_delete_when_handler_called_then_has_execution_arn(
    mock_dynamo,
):
    """
    AC-B: deletion_completed row must include execution_arn.
    """
    from write_audit_log import handler

    exec_arn = "arn:aws:states:eu-central-1:123456789012:execution:knotify-dev-account-deletion:exec-soft"
    event = _make_event(
        "deletion_completed",
        execution_arn=exec_arn,
        purge_immediately=False,
        branches_succeeded=[],
        completed_at="2026-06-20T10:00:00.000000Z",
    )
    handler.handler(event, None)

    call_kwargs = mock_dynamo.put_item.call_args.kwargs
    item = call_kwargs["Item"]
    assert item["execution_arn"]["S"] == exec_arn


# ---------------------------------------------------------------------------
# Test C: deletion_completed purge_immediately row shape
# ---------------------------------------------------------------------------


def test_given_deletion_completed_purge_immediately_when_handler_called_then_dynamodb_retention_is_hard_deleted(
    mock_dynamo,
):
    """
    AC-C: deletion_completed with purge_immediately=True sets
    dynamodb_retention="hard_deleted".
    """
    from write_audit_log import handler

    event = _make_event(
        "deletion_completed",
        purge_immediately=True,
        branches_succeeded=["SoftDeleteAurora", "HardPurgeNow", "DeleteDynamoDBPersonalData", "HardDeleteUserChatMessages"],
        completed_at="2026-06-20T11:00:00.000000Z",
    )
    handler.handler(event, None)

    call_kwargs = mock_dynamo.put_item.call_args.kwargs
    item = call_kwargs["Item"]
    assert item["dynamodb_retention"]["S"] == "hard_deleted"


# ---------------------------------------------------------------------------
# Test D: deletion_failed row shape
# ---------------------------------------------------------------------------


def test_given_deletion_failed_when_handler_called_then_row_has_failed_state_name(
    mock_dynamo,
):
    """
    AC-D: deletion_failed row must include failed_state_name.
    """
    from write_audit_log import handler

    event = _make_event(
        "deletion_failed",
        failed_state_name="SoftDeleteAurora",
        error="Lambda.ServiceException",
        cause="Aurora connection timeout",
    )
    handler.handler(event, None)

    call_kwargs = mock_dynamo.put_item.call_args.kwargs
    item = call_kwargs["Item"]
    assert item["event_type"]["S"] == "deletion_failed"
    assert item["failed_state_name"]["S"] == "SoftDeleteAurora"


def test_given_deletion_failed_when_handler_called_then_row_has_error_and_cause(
    mock_dynamo,
):
    """
    AC-D: deletion_failed row must include error and cause strings.
    """
    from write_audit_log import handler

    event = _make_event(
        "deletion_failed",
        failed_state_name="DeactivateChatRooms",
        error="States.TaskFailed",
        cause="DynamoDB throughput exceeded",
    )
    handler.handler(event, None)

    call_kwargs = mock_dynamo.put_item.call_args.kwargs
    item = call_kwargs["Item"]
    assert item["error"]["S"] == "States.TaskFailed"
    assert item["cause"]["S"] == "DynamoDB throughput exceeded"


def test_given_deletion_failed_when_handler_called_then_row_has_execution_arn(
    mock_dynamo,
):
    """
    AC-D: deletion_failed row must include execution_arn.
    """
    from write_audit_log import handler

    exec_arn = "arn:aws:states:eu-central-1:123456789012:execution:knotify-dev-account-deletion:exec-fail"
    event = _make_event(
        "deletion_failed",
        execution_arn=exec_arn,
        failed_state_name="SoftDeleteAurora",
        error="Lambda.ServiceException",
        cause="timeout",
    )
    handler.handler(event, None)

    call_kwargs = mock_dynamo.put_item.call_args.kwargs
    item = call_kwargs["Item"]
    assert item["execution_arn"]["S"] == exec_arn


# ---------------------------------------------------------------------------
# Test H: Unknown event_type raises ValueError, PutItem not called
# ---------------------------------------------------------------------------


def test_given_unknown_event_type_when_handler_called_then_raises_value_error(
    mock_dynamo,
):
    """
    AC-H: unknown event_type must raise ValueError without calling PutItem.
    """
    from write_audit_log import handler

    event = _make_event("deletion_unknown_type")
    with pytest.raises(ValueError, match="deletion_unknown_type"):
        handler.handler(event, None)

    mock_dynamo.put_item.assert_not_called()
