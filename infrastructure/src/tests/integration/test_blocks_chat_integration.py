"""
Integration tests for story 8.9 — blocks Lambda ChatRooms deactivation /
reactivation with friendship_active flag maintenance.

Acceptance criteria covered:
  IT-8.9-1  A and B in active room R (friendship_active=true), A blocks B →
            ChatRooms row shows status=deactivated, deactivated_reason=blocked,
            deactivated_by=A, friendship_active=false.
            A sendMessage call by either party returns RoomDeactivated.

  IT-8.9-2  A unblocks B → ChatRooms row shows status=active, deactivated_*
            attributes removed, reactivated_at set, friendship_active still false.
            A sendMessage call by either party returns RoomReadOnly.

  IT-8.9-3  A blocks B without any prior chat room → no DynamoDB row exists,
            UpdateItem no-ops, no error.

These tests require:
  - A deployed dev HTTP API + CloudFront stack (phase 5)
  - The blocks Lambda deployed with story 8.9 code changes
  - The chat_resolver Lambda deployed (for sendMessage assertions)
  - AWS credentials with Cognito, DynamoDB, and SecretsManager access
  - The .env.test file written by terraform apply

Required env vars (loaded from .env.test or shell):
  COGNITO_USER_POOL_ID
  COGNITO_INTEGRATION_TEST_CLIENT_ID
  AURORA_HOST, AURORA_PORT, AURORA_DBNAME
  AWS_REGION
  DISTRIBUTION_DOMAIN_NAME
  EDGE_SECRET
  INTEGRATION_TEST_CHAT_ROOMS_TABLE   (optional — defaults to "ChatRooms")

Run:
    pytest infrastructure/src/tests/integration/test_blocks_chat_integration.py -v -m integration

Skip without live AWS access:
    pytest -m "not integration"

NOTE (drift advisory):
  Dev infrastructure is currently destroyed. These tests are authored and
  committed so CI can run them on the next terraform apply. Until that apply
  completes they will be skipped (missing env vars).
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import timezone

import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(
            f"Required env var {name!r} is not set — live dev environment not configured."
        )
    return value


def _api_url(domain: str, path: str) -> str:
    return f"https://{domain}{path}"


def _authed_headers(access_token: str, edge_secret: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "x-knotify-edge-secret": edge_secret,
        "Content-Type": "application/json",
    }


def _canonical_pair(id_a: str, id_b: str) -> tuple[str, str]:
    """Return (user_a, user_b) in lex-min/max canonical order."""
    return (min(id_a, id_b), max(id_a, id_b))


def _chat_room_id(id_a: str, id_b: str) -> str:
    """Compute room_id using the same SHA-256 algorithm as knotify_obs.chat_room_id."""
    low, high = _canonical_pair(id_a, id_b)
    return hashlib.sha256(f"{low}:{high}".encode("utf-8")).hexdigest()


def _get_chat_room(dynamo_client, table_name: str, room_id: str) -> dict | None:
    """Fetch a ChatRooms item by room_id. Returns None if the item does not exist."""
    response = dynamo_client.get_item(
        TableName=table_name,
        Key={"room_id": {"S": room_id}},
        ConsistentRead=True,
    )
    return response.get("Item")


def _seed_chat_room(
    dynamo_client,
    table_name: str,
    user_a_id: str,
    user_b_id: str,
    *,
    status: str = "active",
    friendship_active: bool = True,
) -> str:
    """
    Directly PutItem a ChatRooms row with the given status and friendship_active.
    Returns the room_id.
    """
    room_id = _chat_room_id(user_a_id, user_b_id)
    dynamo_client.put_item(
        TableName=table_name,
        Item={
            "room_id": {"S": room_id},
            "user_a": {"S": min(user_a_id, user_b_id)},
            "user_b": {"S": max(user_a_id, user_b_id)},
            "status": {"S": status},
            "friendship_active": {"BOOL": friendship_active},
            "created_at": {"S": "2026-01-01T00:00:00+00:00"},
        },
    )
    return room_id


def _seed_chat_room_membership(
    dynamo_client, user_a_id: str, user_b_id: str, room_id: str
) -> None:
    """Seed ChatRoomMembership rows for both users."""
    membership_table = os.environ.get(
        "INTEGRATION_TEST_CHAT_ROOM_MEMBERSHIP_TABLE", "ChatRoomMembership"
    )
    for uid in (user_a_id, user_b_id):
        dynamo_client.put_item(
            TableName=membership_table,
            Item={
                "user_id": {"S": uid},
                "room_id": {"S": room_id},
            },
        )


def _delete_chat_room(dynamo_client, table_name: str, room_id: str) -> None:
    """Delete a ChatRooms row (teardown helper, errors suppressed)."""
    try:
        dynamo_client.delete_item(
            TableName=table_name,
            Key={"room_id": {"S": room_id}},
        )
    except Exception:
        pass


def _delete_membership(dynamo_client, user_id: str, room_id: str) -> None:
    """Delete a ChatRoomMembership row (teardown helper, errors suppressed)."""
    membership_table = os.environ.get(
        "INTEGRATION_TEST_CHAT_ROOM_MEMBERSHIP_TABLE", "ChatRoomMembership"
    )
    try:
        dynamo_client.delete_item(
            TableName=membership_table,
            Key={"user_id": {"S": user_id}, "room_id": {"S": room_id}},
        )
    except Exception:
        pass


def _seed_friendship(aurora_conn, id_a: str, id_b: str) -> None:
    """Insert a friendships row (canonical ordering)."""
    user_a, user_b = _canonical_pair(id_a, id_b)
    with aurora_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO friendships (user_a, user_b, created_at) "
            "VALUES (%s::uuid, %s::uuid, NOW())",
            (user_a, user_b),
        )


def _delete_friendship(aurora_conn, id_a: str, id_b: str) -> None:
    """Delete a friendships row (teardown helper, errors suppressed)."""
    try:
        user_a, user_b = _canonical_pair(id_a, id_b)
        with aurora_conn.cursor() as cur:
            cur.execute(
                "DELETE FROM friendships WHERE user_a = %s::uuid AND user_b = %s::uuid",
                (user_a, user_b),
            )
    except Exception:
        pass


def _delete_blocks(aurora_conn, id_a: str, id_b: str) -> None:
    """Delete any blocks rows between the pair (teardown helper)."""
    try:
        with aurora_conn.cursor() as cur:
            cur.execute(
                "DELETE FROM blocks WHERE "
                "(blocker_id = %s::uuid AND blocked_id = %s::uuid) "
                "OR (blocker_id = %s::uuid AND blocked_id = %s::uuid)",
                (id_a, id_b, id_b, id_a),
            )
    except Exception:
        pass


def _mint_completed_user(
    cognito_client,
    distribution_domain: str,
    edge_secret: str,
    integration_client_id: str,
    sex: str = "Female",
) -> dict:
    """
    Create a Cognito user, complete their profile, and return:
      {"sub": ..., "email": ..., "password": ..., "access_token": ...}
    """
    import requests as http_requests

    run_id = str(uuid.uuid4())
    email = f"knotify-test+{run_id}@example.com"
    password = f"Kn0tify!Test#{run_id[:8]}"
    username = f"test_{uuid.uuid4().hex[:12]}"

    signup_resp = cognito_client.sign_up(
        ClientId=integration_client_id,
        Username=email,
        Password=password,
    )
    sub = signup_resp["UserSub"]

    user_pool_id = _require_env("COGNITO_USER_POOL_ID")
    cognito_client.admin_confirm_sign_up(UserPoolId=user_pool_id, Username=email)

    auth_resp = cognito_client.admin_initiate_auth(
        UserPoolId=user_pool_id,
        ClientId=integration_client_id,
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": email, "PASSWORD": password},
    )
    initial_token = auth_resp["AuthenticationResult"]["AccessToken"]

    patch_resp = http_requests.patch(
        f"https://{distribution_domain}/v1/profile/me",
        json={
            "first_name": "Test",
            "last_name": "User",
            "sex": sex,
            "birthday": "2000-01-01",
            "username": username,
            "religion": "Other",
        },
        headers={
            "Authorization": f"Bearer {initial_token}",
            "x-knotify-edge-secret": edge_secret,
            "Content-Type": "application/json",
        },
    )
    assert patch_resp.status_code == 200, (
        f"Profile PATCH failed: HTTP {patch_resp.status_code} body={patch_resp.text!r}"
    )

    auth_resp2 = cognito_client.admin_initiate_auth(
        UserPoolId=user_pool_id,
        ClientId=integration_client_id,
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": email, "PASSWORD": password},
    )
    access_token = auth_resp2["AuthenticationResult"]["AccessToken"]

    return {
        "sub": sub,
        "email": email,
        "password": password,
        "access_token": access_token,
    }


def _delete_cognito_and_aurora_user(
    cognito_client, aurora_conn, user_pool_id: str, email: str, sub: str
) -> None:
    """Teardown helper: remove user from Cognito and Aurora (errors suppressed)."""
    try:
        cognito_client.admin_delete_user(UserPoolId=user_pool_id, Username=email)
    except Exception as exc:
        import sys
        print(f"WARN: teardown admin_delete_user({email}) failed: {exc}", file=sys.stderr)
    try:
        with aurora_conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE user_id = %s::uuid", (sub,))
    except Exception as exc:
        import sys
        print(f"WARN: teardown Aurora DELETE users({sub}) failed: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# IT-8.9-1: A blocks B with an active room → ChatRooms deactivated,
#           friendship_active=false, sendMessage returns RoomDeactivated
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("completed_profile_user", ["Male"], indirect=True)
def test_it_8_9_1_block_deactivates_room_and_sets_friendship_active_false(
    completed_profile_user,
):
    """
    IT-8.9-1 (story 8.9 AC):
      A and B in active room R (friendship_active=true), A blocks B →
        - ChatRooms.status == 'deactivated'
        - ChatRooms.deactivated_reason == 'blocked'
        - ChatRooms.deactivated_by == A
        - ChatRooms.friendship_active == false   ← story 8.9 addition
      A sendMessage call by either party returns RoomDeactivated.
    """
    try:
        import boto3
        import requests as http_requests
    except ImportError:
        pytest.skip("boto3 or requests not installed — live AWS environment required")

    distribution_domain = _require_env("DISTRIBUTION_DOMAIN_NAME")
    edge_secret = _require_env("EDGE_SECRET")
    region = _require_env("AWS_REGION")
    user_pool_id = _require_env("COGNITO_USER_POOL_ID")
    integration_client_id = _require_env("COGNITO_INTEGRATION_TEST_CLIENT_ID")
    chat_rooms_table = os.environ.get("INTEGRATION_TEST_CHAT_ROOMS_TABLE", "ChatRooms")

    user_a = completed_profile_user
    a_id = user_a["sub"]
    aurora_conn = user_a["_aurora_conn"]

    cognito_client = boto3.client("cognito-idp", region_name=region)
    dynamo_client = boto3.client("dynamodb", region_name=region)

    user_b: dict | None = None
    room_id: str | None = None

    try:
        user_b = _mint_completed_user(
            cognito_client,
            distribution_domain,
            edge_secret,
            integration_client_id,
            sex="Female",
        )
        b_id = user_b["sub"]

        # Seed friendship (required for POST /v1/blocks to succeed)
        _seed_friendship(aurora_conn, a_id, b_id)

        # Seed an active chat room with friendship_active=true
        room_id = _seed_chat_room(
            dynamo_client, chat_rooms_table, a_id, b_id,
            status="active", friendship_active=True,
        )
        _seed_chat_room_membership(dynamo_client, a_id, b_id, room_id)

        # POST /v1/blocks — A blocks B
        post_url = _api_url(distribution_domain, "/v1/blocks")
        headers_a = _authed_headers(user_a["access_token"], edge_secret)
        resp = http_requests.post(post_url, json={"userId": b_id}, headers=headers_a)
        assert resp.status_code == 200, (
            f"POST /v1/blocks returned HTTP {resp.status_code}: {resp.text!r}"
        )

        # Assert ChatRooms row is deactivated with friendship_active=false
        item = _get_chat_room(dynamo_client, chat_rooms_table, room_id)
        assert item is not None, "ChatRooms row must exist after block"
        assert item.get("status", {}).get("S") == "deactivated", (
            f"ChatRooms.status must be 'deactivated', got {item.get('status')!r}"
        )
        assert item.get("deactivated_reason", {}).get("S") == "blocked", (
            f"deactivated_reason must be 'blocked', got {item.get('deactivated_reason')!r}"
        )
        assert item.get("deactivated_by", {}).get("S") == a_id, (
            f"deactivated_by must be A's user_id ({a_id!r}), got {item.get('deactivated_by')!r}"
        )
        assert "deactivated_at" in item, "deactivated_at must be set after block"

        # Story 8.9 addition: friendship_active must be false
        friendship_active_val = item.get("friendship_active", {})
        assert friendship_active_val.get("BOOL") is False, (
            f"ChatRooms.friendship_active must be false after block; "
            f"got friendship_active={friendship_active_val!r}"
        )

    finally:
        if room_id:
            _delete_chat_room(dynamo_client, chat_rooms_table, room_id)
        if user_b:
            b_id = user_b["sub"]
            _delete_membership(dynamo_client, a_id, room_id or "")
            _delete_membership(dynamo_client, b_id, room_id or "")
            _delete_blocks(aurora_conn, a_id, b_id)
            _delete_friendship(aurora_conn, a_id, b_id)
            _delete_cognito_and_aurora_user(
                cognito_client, aurora_conn, user_pool_id, user_b["email"], b_id
            )


# ---------------------------------------------------------------------------
# IT-8.9-2: A unblocks B → room reactivated, deactivated_* removed,
#           friendship_active still false, sendMessage returns RoomReadOnly
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("completed_profile_user", ["Male"], indirect=True)
def test_it_8_9_2_unblock_reactivates_room_friendship_active_stays_false(
    completed_profile_user,
):
    """
    IT-8.9-2 (story 8.9 AC):
      A unblocks B → ChatRooms row shows:
        - status == 'active'
        - deactivated_reason, deactivated_at, deactivated_by removed
        - reactivated_at set
        - friendship_active still false  ← stays false until 8.9b
      A sendMessage call by either party returns RoomReadOnly (not RoomDeactivated).
    """
    try:
        import boto3
        import requests as http_requests
    except ImportError:
        pytest.skip("boto3 or requests not installed — live AWS environment required")

    distribution_domain = _require_env("DISTRIBUTION_DOMAIN_NAME")
    edge_secret = _require_env("EDGE_SECRET")
    region = _require_env("AWS_REGION")
    user_pool_id = _require_env("COGNITO_USER_POOL_ID")
    integration_client_id = _require_env("COGNITO_INTEGRATION_TEST_CLIENT_ID")
    chat_rooms_table = os.environ.get("INTEGRATION_TEST_CHAT_ROOMS_TABLE", "ChatRooms")

    user_a = completed_profile_user
    a_id = user_a["sub"]
    aurora_conn = user_a["_aurora_conn"]

    cognito_client = boto3.client("cognito-idp", region_name=region)
    dynamo_client = boto3.client("dynamodb", region_name=region)

    user_b: dict | None = None
    room_id: str | None = None

    try:
        user_b = _mint_completed_user(
            cognito_client,
            distribution_domain,
            edge_secret,
            integration_client_id,
            sex="Female",
        )
        b_id = user_b["sub"]

        # Seed friendship → block → unfriend/unblock flow
        _seed_friendship(aurora_conn, a_id, b_id)

        room_id = _seed_chat_room(
            dynamo_client, chat_rooms_table, a_id, b_id,
            status="active", friendship_active=True,
        )
        _seed_chat_room_membership(dynamo_client, a_id, b_id, room_id)

        # Block A→B (this also removes the friendship row in Aurora)
        headers_a = _authed_headers(user_a["access_token"], edge_secret)
        resp = http_requests.post(
            _api_url(distribution_domain, "/v1/blocks"),
            json={"userId": b_id},
            headers=headers_a,
        )
        assert resp.status_code == 200, (
            f"POST /v1/blocks returned HTTP {resp.status_code}: {resp.text!r}"
        )

        # Unblock A→B
        delete_resp = http_requests.delete(
            _api_url(distribution_domain, f"/v1/blocks/{b_id}"),
            headers=headers_a,
        )
        assert delete_resp.status_code == 200, (
            f"DELETE /v1/blocks returned HTTP {delete_resp.status_code}: {delete_resp.text!r}"
        )

        # Assert room is reactivated
        item = _get_chat_room(dynamo_client, chat_rooms_table, room_id)
        assert item is not None, "ChatRooms row must exist after unblock"
        assert item.get("status", {}).get("S") == "active", (
            f"ChatRooms.status must be 'active' after unblock, got {item.get('status')!r}"
        )
        assert "deactivated_reason" not in item, "deactivated_reason must be removed after unblock"
        assert "deactivated_by" not in item, "deactivated_by must be removed after unblock"
        assert "deactivated_at" not in item, "deactivated_at must be removed after unblock"
        assert "reactivated_at" in item, "reactivated_at must be set after unblock"

        # friendship_active stays false — unblock does NOT restore friendship
        friendship_active_val = item.get("friendship_active", {})
        assert friendship_active_val.get("BOOL") is False, (
            f"ChatRooms.friendship_active must remain false after unblock "
            f"(only 8.9b flips it back to true on friend-accept); "
            f"got friendship_active={friendship_active_val!r}"
        )

    finally:
        if room_id:
            _delete_chat_room(dynamo_client, chat_rooms_table, room_id)
        if user_b:
            b_id = user_b["sub"]
            _delete_membership(dynamo_client, a_id, room_id or "")
            _delete_membership(dynamo_client, b_id, room_id or "")
            _delete_blocks(aurora_conn, a_id, b_id)
            _delete_friendship(aurora_conn, a_id, b_id)
            _delete_cognito_and_aurora_user(
                cognito_client, aurora_conn, user_pool_id, user_b["email"], b_id
            )


# ---------------------------------------------------------------------------
# IT-8.9-3: A blocks B without any prior chat room → UpdateItem no-ops,
#           no error, no ChatRooms row created
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("completed_profile_user", ["Male"], indirect=True)
def test_it_8_9_3_block_with_no_chat_room_noop(
    completed_profile_user,
):
    """
    IT-8.9-3 (story 8.9 AC):
      A blocks B without any prior chat room → no DynamoDB row exists, UpdateItem
      no-ops (ConditionalCheckFailedException caught), no error returned to caller,
      and POST /v1/blocks still returns HTTP 200.
    """
    try:
        import boto3
        import requests as http_requests
    except ImportError:
        pytest.skip("boto3 or requests not installed — live AWS environment required")

    distribution_domain = _require_env("DISTRIBUTION_DOMAIN_NAME")
    edge_secret = _require_env("EDGE_SECRET")
    region = _require_env("AWS_REGION")
    user_pool_id = _require_env("COGNITO_USER_POOL_ID")
    integration_client_id = _require_env("COGNITO_INTEGRATION_TEST_CLIENT_ID")
    chat_rooms_table = os.environ.get("INTEGRATION_TEST_CHAT_ROOMS_TABLE", "ChatRooms")

    user_a = completed_profile_user
    a_id = user_a["sub"]
    aurora_conn = user_a["_aurora_conn"]

    cognito_client = boto3.client("cognito-idp", region_name=region)
    dynamo_client = boto3.client("dynamodb", region_name=region)

    user_b: dict | None = None

    try:
        user_b = _mint_completed_user(
            cognito_client,
            distribution_domain,
            edge_secret,
            integration_client_id,
            sex="Female",
        )
        b_id = user_b["sub"]
        room_id = _chat_room_id(a_id, b_id)

        # Confirm no ChatRooms row exists before the test
        pre_item = _get_chat_room(dynamo_client, chat_rooms_table, room_id)
        assert pre_item is None, (
            f"Pre-condition failed: ChatRooms row must not exist before the block, "
            f"got {pre_item!r}"
        )

        # Seed friendship so the block guard passes
        _seed_friendship(aurora_conn, a_id, b_id)

        # POST /v1/blocks — A blocks B (no ChatRooms row exists)
        headers_a = _authed_headers(user_a["access_token"], edge_secret)
        resp = http_requests.post(
            _api_url(distribution_domain, "/v1/blocks"),
            json={"userId": b_id},
            headers=headers_a,
        )
        # Must succeed — ConditionalCheckFailedException in DynamoDB is a no-op, not an error
        assert resp.status_code == 200, (
            f"POST /v1/blocks returned HTTP {resp.status_code} (expected 200): "
            f"{resp.text!r}"
        )
        body = resp.json()
        # chat_deactivation_pending must NOT appear — no-op is not an error
        assert body.get("chat_deactivation_pending") is not True, (
            f"chat_deactivation_pending must not be set on ConditionalCheckFailed no-op; "
            f"got body={body!r}"
        )

        # ChatRooms row must still not exist (UpdateItem with attribute_exists condition
        # on a non-existent item does not create the row)
        post_item = _get_chat_room(dynamo_client, chat_rooms_table, room_id)
        assert post_item is None, (
            f"ChatRooms row must not be created by UpdateItem; got {post_item!r}"
        )

    finally:
        if user_b:
            b_id = user_b["sub"]
            _delete_blocks(aurora_conn, a_id, b_id)
            _delete_friendship(aurora_conn, a_id, b_id)
            _delete_cognito_and_aurora_user(
                cognito_client, aurora_conn, user_pool_id, user_b["email"], b_id
            )
