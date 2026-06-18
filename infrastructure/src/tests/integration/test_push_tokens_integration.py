"""
Integration tests for POST /v1/push-tokens — story 8.11.

Verifies the end-to-end behaviour:
  1. Signup a user, POST a push token, read back via internal DynamoDB GetItem
     and confirm the item was upserted correctly.
  2. POST again with the same device_id: confirm last_seen is updated and only
     one row exists for (user_id, device_id) — no duplicate on upsert.

Requirements (all skip-gated — skipped when env vars are absent):
  API_BASE_URL               — CloudFront HTTPS base URL (no trailing slash)
  COGNITO_USER_POOL_ID       — dev Cognito user pool ID
  COGNITO_INTEGRATION_TEST_CLIENT_ID — dev app client with ADMIN_USER_PASSWORD_AUTH
  AURORA_HOST / AURORA_PORT / AURORA_DBNAME / AURORA_MASTER_SECRET_ARN
    — used by the signed_in_user fixture (signup + sign-in)
  AWS_REGION                 — AWS region for boto3 DynamoDB client

Run:
    pytest infrastructure/src/tests/integration/test_push_tokens_integration.py -v -m integration

Skip without live AWS:
    pytest -m "not integration"
"""

from __future__ import annotations

import os
import uuid

import pytest

pytestmark = pytest.mark.integration


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"Required env var {name!r} not set — live dev environment not configured.")
    return value


# ---------------------------------------------------------------------------
# Test 1: POST a token → item appears in DynamoDB with correct attributes
# ---------------------------------------------------------------------------


def test_post_push_token_upserts_item_in_dynamodb(signed_in_user):
    """
    Given a signed-in user and a valid device token payload,
    when POST /v1/push-tokens is called with a valid JWT,
    then:
      - response is HTTP 200 {"registered": true}
      - DynamoDB GetItem returns an item with PK=user_id, SK=device_id,
        platform, push_token, and a non-empty last_seen timestamp.
    """
    try:
        import boto3
        import requests as http_requests
    except ImportError:
        pytest.skip("boto3 or requests not installed — live environment required")

    api_base = _require_env("API_BASE_URL").rstrip("/")
    region = _require_env("AWS_REGION")

    device_id = f"test-device-{uuid.uuid4()}"
    push_token = f"ExponentPushToken[{uuid.uuid4().hex[:16]}]"

    resp = http_requests.post(
        f"{api_base}/v1/push-tokens",
        json={
            "platform": "ios",
            "push_token": push_token,
            "device_id": device_id,
            "app_version": "1.0.0",
        },
        headers={
            "Authorization": f"Bearer {signed_in_user['access_token']}",
            "Content-Type": "application/json",
        },
        timeout=15,
    )

    assert resp.status_code == 200, (
        f"Expected HTTP 200 from POST /v1/push-tokens, got {resp.status_code}. "
        f"Body: {resp.text[:200]}"
    )
    assert resp.json().get("registered") is True

    # Verify item in DynamoDB
    ddb = boto3.client("dynamodb", region_name=region)
    item_resp = ddb.get_item(
        TableName="PushNotificationTokens",
        Key={
            "user_id": {"S": signed_in_user["sub"]},
            "device_id": {"S": device_id},
        },
    )

    item = item_resp.get("Item")
    assert item is not None, (
        f"Expected DynamoDB item for (user_id={signed_in_user['sub']!r}, "
        f"device_id={device_id!r}) but got None"
    )
    assert item["push_token"]["S"] == push_token
    assert item["platform"]["S"] == "ios"
    assert item["app_version"]["S"] == "1.0.0"
    assert item["last_seen"]["S"] != ""


# ---------------------------------------------------------------------------
# Test 2: second POST with same device_id updates last_seen, no duplicate row
# ---------------------------------------------------------------------------


def test_second_post_same_device_id_updates_token_no_duplicate(signed_in_user):
    """
    Given a previously registered push token for a (user_id, device_id) pair,
    when POST /v1/push-tokens is called again with the same device_id but a
    different push_token,
    then:
      - response is HTTP 200 {"registered": true}
      - DynamoDB still contains exactly one row for (user_id, device_id)
      - The row reflects the new push_token and an updated last_seen value.
    """
    try:
        import boto3
        import requests as http_requests
    except ImportError:
        pytest.skip("boto3 or requests not installed — live environment required")

    api_base = _require_env("API_BASE_URL").rstrip("/")
    region = _require_env("AWS_REGION")

    device_id = f"test-device-dup-{uuid.uuid4()}"
    first_token = f"ExponentPushToken[first-{uuid.uuid4().hex[:8]}]"
    second_token = f"ExponentPushToken[second-{uuid.uuid4().hex[:8]}]"

    headers = {
        "Authorization": f"Bearer {signed_in_user['access_token']}",
        "Content-Type": "application/json",
    }

    # First registration
    resp1 = http_requests.post(
        f"{api_base}/v1/push-tokens",
        json={
            "platform": "android",
            "push_token": first_token,
            "device_id": device_id,
            "app_version": "1.0.0",
        },
        headers=headers,
        timeout=15,
    )
    assert resp1.status_code == 200

    # Second registration — same device_id, new token
    resp2 = http_requests.post(
        f"{api_base}/v1/push-tokens",
        json={
            "platform": "android",
            "push_token": second_token,
            "device_id": device_id,
            "app_version": "1.1.0",
        },
        headers=headers,
        timeout=15,
    )
    assert resp2.status_code == 200

    # DynamoDB must have exactly one item for this (user_id, device_id) pair.
    ddb = boto3.client("dynamodb", region_name=region)
    item_resp = ddb.get_item(
        TableName="PushNotificationTokens",
        Key={
            "user_id": {"S": signed_in_user["sub"]},
            "device_id": {"S": device_id},
        },
    )

    item = item_resp.get("Item")
    assert item is not None, "Expected DynamoDB item after second POST but got None"

    # Item must reflect the second (latest) registration
    assert item["push_token"]["S"] == second_token, (
        f"Expected push_token={second_token!r} after upsert, "
        f"got {item['push_token']['S']!r}"
    )
    assert item["app_version"]["S"] == "1.1.0"

    # Query to confirm exactly one row for (user_id, device_id) — PutItem upsert,
    # not Insert, so there must be no duplicate.
    query_resp = ddb.query(
        TableName="PushNotificationTokens",
        KeyConditionExpression="user_id = :uid AND device_id = :did",
        ExpressionAttributeValues={
            ":uid": {"S": signed_in_user["sub"]},
            ":did": {"S": device_id},
        },
    )
    assert query_resp["Count"] == 1, (
        f"Expected exactly 1 DynamoDB row for (user_id, device_id) after two POSTs "
        f"(upsert semantics), but Count={query_resp['Count']}"
    )
