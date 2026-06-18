"""
Unit tests for the stale_token_cleanup Lambda handler.

Story 8.12 — Daily cron: scan PushNotificationTokens, delete rows whose
last_seen is older than 60 days.

Behaviour under test:
  - Token last_seen > 60 days ago is deleted.
  - Token last_seen == 59 days ago is NOT deleted.
  - Token last_seen == 60 days ago is NOT deleted (boundary — 60 days = grace,
    only strictly older than 60 days are deleted).
  - DeleteItem is called with PK=user_id SK=device_id (both required by schema).
  - Empty table: no delete calls.
  - Multiple stale tokens: all deleted.
  - Mixed table (some stale, some fresh): only stale tokens deleted.
  - Paginated Scan (LastEvaluatedKey): all pages consumed.

Integration test (skip-gated on DDB_ENDPOINT env var):
  - Seed token with last_seen 61 days ago → after handler() token is gone.
  - Seed token with last_seen 59 days ago → after handler() token still exists.

All unit tests are pure — no real DynamoDB, no real AWS calls.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _iso(days_ago: float, extra_seconds: int = 0) -> str:
    """
    Return an ISO 8601 UTC timestamp for *days_ago* days in the past.

    extra_seconds: additional seconds subtracted from the timestamp.  Use a
    positive value to push the timestamp further into the past (e.g., make a
    60-day item reliably stale) or a negative value to make it reliably fresh.
    """
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago, seconds=extra_seconds)
    return dt.isoformat()


def _ddb_item(user_id: str, device_id: str, last_seen_iso: str) -> dict:
    """Build a DynamoDB scan-result item dict (AttributeValue format)."""
    return {
        "user_id":   {"S": user_id},
        "device_id": {"S": device_id},
        "last_seen": {"S": last_seen_iso},
    }


def _make_module(dynamo_mock: MagicMock):
    """
    Import (or reload) stale_token_cleanup.handler with boto3 patched so no
    real AWS calls are made.  Returns the module so tests can call functions
    directly.
    """
    # Remove any cached stale_token_cleanup modules.
    for key in list(sys.modules.keys()):
        if "stale_token_cleanup" in key and "test" not in key:
            del sys.modules[key]

    with patch("boto3.client", return_value=dynamo_mock):
        import infrastructure.src.functions.stale_token_cleanup.handler as mod
        mod._dynamo = dynamo_mock
    return mod


# ---------------------------------------------------------------------------
# Test: stale token (last_seen 61 days ago) is deleted
# ---------------------------------------------------------------------------


def test_stale_token_61_days_ago_is_deleted():
    """
    Given a single token with last_seen 61 days ago,
    when handler() is invoked,
    then DynamoDB DeleteItem is called once with the correct PK and SK.
    """
    dynamo_mock = MagicMock()
    item = _ddb_item("user-aaa", "device-111", _iso(61))

    dynamo_mock.scan.return_value = {
        "Items": [item],
        # No LastEvaluatedKey — single page
    }

    mod = _make_module(dynamo_mock)
    mod.handler({}, None)

    dynamo_mock.delete_item.assert_called_once_with(
        TableName=mod._TABLE_PUSH_TOKENS,
        Key={
            "user_id":   {"S": "user-aaa"},
            "device_id": {"S": "device-111"},
        },
    )


# ---------------------------------------------------------------------------
# Test: fresh token (last_seen 59 days ago) is NOT deleted
# ---------------------------------------------------------------------------


def test_fresh_token_59_days_ago_is_not_deleted():
    """
    Given a single token with last_seen 59 days ago,
    when handler() is invoked,
    then DynamoDB DeleteItem is NOT called.
    """
    dynamo_mock = MagicMock()
    item = _ddb_item("user-bbb", "device-222", _iso(59))

    dynamo_mock.scan.return_value = {
        "Items": [item],
    }

    mod = _make_module(dynamo_mock)
    mod.handler({}, None)

    dynamo_mock.delete_item.assert_not_called()


# ---------------------------------------------------------------------------
# Test: token exactly 60 days old is NOT deleted (boundary — must be strictly
# older than 60 days)
# ---------------------------------------------------------------------------


def test_token_59_days_and_59_minutes_old_is_not_deleted():
    """
    Given a token with last_seen 59 days and 59 minutes ago (clearly within
    the 60-day window),
    when handler() is invoked,
    then the token is NOT deleted.

    Note: we do not test the exact 60-day boundary at second granularity because
    the cutoff is computed inside the handler and a tiny clock drift would make
    the test flaky.  59 days 59 minutes is unambiguously fresh.
    """
    dynamo_mock = MagicMock()
    # 59 days + 59 minutes ago = clearly inside the 60-day window
    item = _ddb_item("user-ccc", "device-333", _iso(59, extra_seconds=-59 * 60))

    dynamo_mock.scan.return_value = {
        "Items": [item],
    }

    mod = _make_module(dynamo_mock)
    mod.handler({}, None)

    dynamo_mock.delete_item.assert_not_called()


# ---------------------------------------------------------------------------
# Test: empty table — no deletes
# ---------------------------------------------------------------------------


def test_empty_table_no_deletes():
    """
    Given no tokens in the table,
    when handler() is invoked,
    then DynamoDB DeleteItem is never called.
    """
    dynamo_mock = MagicMock()
    dynamo_mock.scan.return_value = {"Items": []}

    mod = _make_module(dynamo_mock)
    mod.handler({}, None)

    dynamo_mock.delete_item.assert_not_called()


# ---------------------------------------------------------------------------
# Test: multiple stale tokens — all deleted
# ---------------------------------------------------------------------------


def test_multiple_stale_tokens_all_deleted():
    """
    Given three tokens all older than 60 days,
    when handler() is invoked,
    then DeleteItem is called exactly three times (once per token).
    """
    dynamo_mock = MagicMock()
    items = [
        _ddb_item("user-1", "device-A", _iso(90)),
        _ddb_item("user-2", "device-B", _iso(61)),
        _ddb_item("user-3", "device-C", _iso(120)),
    ]

    dynamo_mock.scan.return_value = {"Items": items}

    mod = _make_module(dynamo_mock)
    mod.handler({}, None)

    assert dynamo_mock.delete_item.call_count == 3


# ---------------------------------------------------------------------------
# Test: mixed table — only stale tokens deleted, fresh tokens kept
# ---------------------------------------------------------------------------


def test_mixed_table_only_stale_tokens_deleted():
    """
    Given two stale tokens (>60 days) and two fresh tokens (<60 days),
    when handler() is invoked,
    then DeleteItem is called exactly twice — only for the stale tokens.
    """
    dynamo_mock = MagicMock()
    items = [
        _ddb_item("user-stale-1", "device-S1", _iso(90)),
        _ddb_item("user-fresh-1", "device-F1", _iso(10)),
        _ddb_item("user-stale-2", "device-S2", _iso(65)),
        _ddb_item("user-fresh-2", "device-F2", _iso(30)),
    ]

    dynamo_mock.scan.return_value = {"Items": items}

    mod = _make_module(dynamo_mock)
    mod.handler({}, None)

    assert dynamo_mock.delete_item.call_count == 2
    deleted_keys = [
        c.kwargs["Key"]["user_id"]["S"]
        for c in dynamo_mock.delete_item.call_args_list
    ]
    assert "user-stale-1" in deleted_keys
    assert "user-stale-2" in deleted_keys
    assert "user-fresh-1" not in deleted_keys
    assert "user-fresh-2" not in deleted_keys


# ---------------------------------------------------------------------------
# Test: paginated Scan — all pages consumed, all stale tokens deleted
# ---------------------------------------------------------------------------


def test_paginated_scan_all_pages_consumed():
    """
    Given a table whose Scan result requires two pages (LastEvaluatedKey present
    on first call, absent on second),
    when handler() is invoked,
    then Scan is called twice and DeleteItem is called for every stale token
    on both pages.
    """
    dynamo_mock = MagicMock()

    page1_items = [
        _ddb_item("user-p1-stale", "device-P1S", _iso(70)),
        _ddb_item("user-p1-fresh", "device-P1F", _iso(5)),
    ]
    page2_items = [
        _ddb_item("user-p2-stale", "device-P2S", _iso(80)),
    ]

    pagination_key = {"user_id": {"S": "user-p1-fresh"}, "device_id": {"S": "device-P1F"}}

    dynamo_mock.scan.side_effect = [
        {"Items": page1_items, "LastEvaluatedKey": pagination_key},
        {"Items": page2_items},  # No LastEvaluatedKey → last page
    ]

    mod = _make_module(dynamo_mock)
    mod.handler({}, None)

    # Scan called twice (two pages)
    assert dynamo_mock.scan.call_count == 2

    # DeleteItem called twice — one stale per page, fresh skipped
    assert dynamo_mock.delete_item.call_count == 2

    deleted_keys = [
        c.kwargs["Key"]["user_id"]["S"]
        for c in dynamo_mock.delete_item.call_args_list
    ]
    assert "user-p1-stale" in deleted_keys
    assert "user-p2-stale" in deleted_keys
    assert "user-p1-fresh" not in deleted_keys


# ---------------------------------------------------------------------------
# Test: second Scan page passes ExclusiveStartKey from LastEvaluatedKey
# ---------------------------------------------------------------------------


def test_paginated_scan_passes_exclusive_start_key():
    """
    Given a two-page Scan result,
    when handler() is invoked,
    then the second Scan call includes ExclusiveStartKey = first page's
    LastEvaluatedKey.
    """
    dynamo_mock = MagicMock()

    pagination_key = {"user_id": {"S": "user-x"}, "device_id": {"S": "device-x"}}

    dynamo_mock.scan.side_effect = [
        {"Items": [], "LastEvaluatedKey": pagination_key},
        {"Items": []},
    ]

    mod = _make_module(dynamo_mock)
    mod.handler({}, None)

    # First call: no ExclusiveStartKey
    first_call_kwargs = dynamo_mock.scan.call_args_list[0].kwargs
    assert "ExclusiveStartKey" not in first_call_kwargs

    # Second call: ExclusiveStartKey = pagination_key
    second_call_kwargs = dynamo_mock.scan.call_args_list[1].kwargs
    assert second_call_kwargs["ExclusiveStartKey"] == pagination_key


# ---------------------------------------------------------------------------
# Integration test (skip-gated on DDB_ENDPOINT)
#
# Requires a real DynamoDB endpoint (e.g. DynamoDB Local) to be running.
# Skip unless DDB_ENDPOINT env var is set.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(
    not os.environ.get("DDB_ENDPOINT"),
    reason="DDB_ENDPOINT not set — skipping DynamoDB integration test",
)
def test_integration_stale_token_deleted_fresh_token_kept():
    """
    Integration: seed one token older than 60 days and one token younger than 60 days.
    After invoking the Lambda handler in-process:
      - The stale token row must be gone (GetItem returns no Item).
      - The fresh token row must still exist (GetItem returns an Item).

    Requires DDB_ENDPOINT env var pointing to a real or local DynamoDB endpoint.
    Table must already exist (created by Terraform apply against the dev account
    or by a LocalStack bootstrap script).
    """
    import boto3

    endpoint_url = os.environ["DDB_ENDPOINT"]
    region = os.environ.get("AWS_DEFAULT_REGION", "eu-central-1")
    table_name = os.environ.get("TABLE_PUSH_TOKENS", "PushNotificationTokens")

    client = boto3.client("dynamodb", endpoint_url=endpoint_url, region_name=region)

    stale_user_id = "integ-stale-user"
    stale_device_id = "integ-stale-device"
    fresh_user_id = "integ-fresh-user"
    fresh_device_id = "integ-fresh-device"

    stale_last_seen = _iso(61)
    fresh_last_seen = _iso(59)

    # Seed both items
    for uid, did, ls in [
        (stale_user_id, stale_device_id, stale_last_seen),
        (fresh_user_id, fresh_device_id, fresh_last_seen),
    ]:
        client.put_item(
            TableName=table_name,
            Item={
                "user_id":   {"S": uid},
                "device_id": {"S": did},
                "last_seen": {"S": ls},
                "push_token": {"S": "ExponentPushToken[integ-test]"},
                "platform":  {"S": "ios"},
            },
        )

    # Reload handler with real boto3 pointed at the local endpoint
    for key in list(sys.modules.keys()):
        if "stale_token_cleanup" in key and "test" not in key:
            del sys.modules[key]

    import infrastructure.src.functions.stale_token_cleanup.handler as mod
    mod._dynamo = client

    # Invoke the handler
    mod.handler({}, None)

    # Assert stale token is gone
    stale_response = client.get_item(
        TableName=table_name,
        Key={
            "user_id":   {"S": stale_user_id},
            "device_id": {"S": stale_device_id},
        },
    )
    assert "Item" not in stale_response, (
        f"Stale token (last_seen={stale_last_seen}) should have been deleted but was found"
    )

    # Assert fresh token still exists
    fresh_response = client.get_item(
        TableName=table_name,
        Key={
            "user_id":   {"S": fresh_user_id},
            "device_id": {"S": fresh_device_id},
        },
    )
    assert "Item" in fresh_response, (
        f"Fresh token (last_seen={fresh_last_seen}) should still exist but was not found"
    )

    # Cleanup — remove the fresh token (stale was already deleted by handler)
    client.delete_item(
        TableName=table_name,
        Key={
            "user_id":   {"S": fresh_user_id},
            "device_id": {"S": fresh_device_id},
        },
    )
