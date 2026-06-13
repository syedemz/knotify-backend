"""
Integration tests for the knotify-blocks Lambda.

Story 6.4 acceptance criteria — tested here (post-apply against dev):

  AC-POS  Positive flow:
            - Mint A=Male and B=Female via completed_profile_user.
            - Seed a friendship row directly via master Aurora credential using
              lex-min/max canonical ordering of (A_id, B_id).
            - Optionally seed a pending friend_request row between A and B.
            - Optionally pre-create a ChatRooms row via direct boto3 PutItem
              keyed on chat_room_id(A_id, B_id) with status='active'.
            - POST /v1/blocks {userId: B} from A → assert HTTP 200.
            - Assert via Aurora: friendship row gone, any friend_request gone,
              blocks row present.
            - Assert via DynamoDB: if ChatRooms row was pre-created, status is
              'deactivated', deactivated_reason='blocked', deactivated_by=A_id,
              deactivated_at set.
            - DELETE /v1/blocks/B from A → assert HTTP 200.
            - Assert via DynamoDB: if ChatRooms row was pre-created, status is
              'active', deactivated_* attrs removed, reactivated_at set.

  AC-NEG  Negative flow:
            - Mint fresh A=Male and B=Female (NOT friends).
            - POST /v1/blocks {userId: B} from A → assert HTTP 409 with
              {"error": "not_friends"}.
            - Assert via Aurora: blocks table row count unchanged.
            - Assert via DynamoDB: no ChatRooms row exists for the pair.

These tests require:
  - A deployed dev HTTP API + CloudFront stack (phase 5)
  - The blocks Lambda deployed and wired to three routes (story 6.4)
  - The profile Lambda deployed (the completed_profile_user fixture calls
    PATCH /v1/profile/me and GET /v1/profiles is needed for setup)
  - AWS credentials with Cognito, SecretsManager, and DynamoDB access
  - The .env.test file written by terraform apply (story 5.6)

Required env vars (loaded from .env.test or shell):
  COGNITO_USER_POOL_ID
  COGNITO_INTEGRATION_TEST_CLIENT_ID
  AURORA_HOST, AURORA_PORT, AURORA_DBNAME, AURORA_MASTER_SECRET_ARN
  AWS_REGION
  DISTRIBUTION_DOMAIN_NAME
  EDGE_SECRET
  INTEGRATION_TEST_CHAT_ROOMS_TABLE  (optional — defaults to "ChatRooms")

Run:
    pytest infrastructure/src/tests/integration/test_blocks.py -v -m integration

Skip without live AWS access:
    pytest -m "not integration"

NOTE (drift advisory):
  Dev infrastructure is currently destroyed. These tests are authored and
  committed so CI can run them on the next terraform apply. Until that apply
  completes they will be skipped (missing env vars).
"""

from __future__ import annotations

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
    """Return (user_a, user_b) in lex-min/max canonical order (same as chat_room_id)."""
    return (min(id_a, id_b), max(id_a, id_b))


def _chat_room_id(id_a: str, id_b: str) -> str:
    """Compute chat_room_id using the same algorithm as knotify_obs.chat_room_id."""
    import hashlib
    low, high = _canonical_pair(id_a, id_b)
    return hashlib.sha256(f"{low}:{high}".encode("utf-8")).hexdigest()


def _blocks_row_count(aurora_conn, blocker_id: str, blocked_id: str) -> int:
    """Return the number of blocks rows for the (blocker, blocked) pair."""
    with aurora_conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM blocks WHERE blocker_id = %s::uuid AND blocked_id = %s::uuid",
            (blocker_id, blocked_id),
        )
        return cur.fetchone()[0]


def _friendship_exists(aurora_conn, id_a: str, id_b: str) -> bool:
    """Return True if a friendships row exists for the canonical pair."""
    user_a, user_b = _canonical_pair(id_a, id_b)
    with aurora_conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM friendships WHERE user_a = %s::uuid AND user_b = %s::uuid LIMIT 1",
            (user_a, user_b),
        )
        return cur.fetchone() is not None


def _friend_request_exists(aurora_conn, id_a: str, id_b: str) -> bool:
    """Return True if any pending friend_request row exists between the pair."""
    with aurora_conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM friend_requests
            WHERE (requester_id = %s::uuid AND receiver_id = %s::uuid)
               OR (requester_id = %s::uuid AND receiver_id = %s::uuid)
            LIMIT 1
            """,
            (id_a, id_b, id_b, id_a),
        )
        return cur.fetchone() is not None


def _get_chat_room(dynamo_client, table_name: str, room_id: str) -> dict | None:
    """
    Fetch a ChatRooms item by room_id. Returns None if the item does not exist.
    """
    response = dynamo_client.get_item(
        TableName=table_name,
        Key={"room_id": {"S": room_id}},
        ConsistentRead=True,
    )
    return response.get("Item")


# ---------------------------------------------------------------------------
# AC-POS: Positive block/unblock flow
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("completed_profile_user", ["Male"], indirect=True)
def test_given_friends_when_a_blocks_b_then_block_created_and_friendship_destroyed(
    completed_profile_user,
    request,
):
    """
    Positive flow (AC-POS):

      1. Mint A=Male via the completed_profile_user fixture.
      2. Mint B=Female via a fresh completed_profile_user call (second fixture).
      3. Seed friendship row via master Aurora INSERT.
      4. Seed a pending friend_request row for cleanup verification.
      5. Pre-create a ChatRooms row via direct boto3 PutItem with status='active'.
      6. POST /v1/blocks {userId: B} from A — assert HTTP 200.
      7. Assert via Aurora: friendship gone, friend_request gone, blocks row present.
      8. Assert via DynamoDB: ChatRooms row is 'deactivated' with expected attrs.
      9. DELETE /v1/blocks/B from A — assert HTTP 200.
     10. Assert via DynamoDB: ChatRooms row is 'active', deactivated_* attrs removed,
         reactivated_at set.
    """
    try:
        import boto3
        import requests as http_requests
    except ImportError:
        pytest.skip("boto3 or requests not installed — live AWS environment required")

    distribution_domain = _require_env("DISTRIBUTION_DOMAIN_NAME")
    edge_secret = _require_env("EDGE_SECRET")
    region = _require_env("AWS_REGION")
    chat_rooms_table = os.environ.get("INTEGRATION_TEST_CHAT_ROOMS_TABLE", "ChatRooms")

    user_a = completed_profile_user  # Male (from fixture parametrize)
    a_id = user_a["sub"]
    aurora_conn = user_a["_aurora_conn"]

    # Mint B=Female — build a second completed_profile_user by directly invoking
    # the fixture machinery via the request object (standard pytest approach).
    # We replicate the core setup inline to avoid nested fixture complexity while
    # keeping teardown clean.
    from conftest import completed_profile_user as _cpf_factory
    # We'll use a second function-scoped signed_in_user via direct Cognito calls.
    # Simpler: inline the Female user creation using the same pattern as conftest.py.

    user_pool_id = _require_env("COGNITO_USER_POOL_ID")
    integration_client_id = _require_env("COGNITO_INTEGRATION_TEST_CLIENT_ID")
    aurora_host = _require_env("AURORA_HOST")
    aurora_port = int(_require_env("AURORA_PORT"))
    aurora_dbname = _require_env("AURORA_DBNAME")
    master_secret_arn = _require_env("AURORA_MASTER_SECRET_ARN")

    cognito_client = boto3.client("cognito-idp", region_name=region)
    sm_client = boto3.client("secretsmanager", region_name=region)
    dynamo_client = boto3.client("dynamodb", region_name=region)

    # ------------------------------------------------------------------
    # Create Female user (B)
    # ------------------------------------------------------------------
    import psycopg2
    from conftest import _get_aurora_master_creds, _aurora_master_conn

    b_run_id = str(uuid.uuid4())
    b_email = f"knotify-test+{b_run_id}@example.com"
    b_password = f"Kn0tify!Test#{b_run_id[:8]}"
    b_id: str | None = None
    b_username = f"test_{uuid.uuid4().hex[:12]}"
    room_id: str | None = None
    chat_room_created = False

    try:
        # Sign up B
        signup_resp = cognito_client.sign_up(
            ClientId=integration_client_id,
            Username=b_email,
            Password=b_password,
        )
        b_id = signup_resp["UserSub"]
        cognito_client.admin_confirm_sign_up(
            UserPoolId=user_pool_id,
            Username=b_email,
        )

        # Initial auth for B
        auth_resp_b = cognito_client.admin_initiate_auth(
            UserPoolId=user_pool_id,
            ClientId=integration_client_id,
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": b_email, "PASSWORD": b_password},
        )
        b_initial_token = auth_resp_b["AuthenticationResult"]["AccessToken"]

        # Complete B's profile via PATCH /v1/profile/me
        patch_url = f"https://{distribution_domain}/v1/profile/me"
        patch_headers = {
            "Authorization": f"Bearer {b_initial_token}",
            "x-knotify-edge-secret": edge_secret,
            "Content-Type": "application/json",
        }
        patch_resp = http_requests.patch(
            patch_url,
            json={
                "first_name": "Test",
                "last_name": "User",
                "sex": "Female",
                "birthday": "2000-01-01",
                "username": b_username,
                "religion": "Other",
            },
            headers=patch_headers,
        )
        assert patch_resp.status_code == 200, (
            f"Female profile PATCH failed: HTTP {patch_resp.status_code} "
            f"body={patch_resp.text!r}"
        )

        # Mint fresh B tokens (post-profile-completion)
        auth_resp_b2 = cognito_client.admin_initiate_auth(
            UserPoolId=user_pool_id,
            ClientId=integration_client_id,
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": b_email, "PASSWORD": b_password},
        )
        b_access_token = auth_resp_b2["AuthenticationResult"]["AccessToken"]

        # ------------------------------------------------------------------
        # Seed friendship row (canonical lex-min/max ordering)
        # ------------------------------------------------------------------
        user_a_col, user_b_col = _canonical_pair(a_id, b_id)
        with aurora_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO friendships (user_a, user_b, created_at) VALUES (%s::uuid, %s::uuid, NOW())",
                (user_a_col, user_b_col),
            )

        # Seed a pending friend_request row (optional; verifies cleanup)
        with aurora_conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO friend_requests (requester_id, receiver_id, status, created_at)
                VALUES (%s::uuid, %s::uuid, 'pending', NOW())
                """,
                (a_id, b_id),
            )

        # Pre-create ChatRooms row via direct DynamoDB PutItem
        room_id = _chat_room_id(a_id, b_id)
        dynamo_client.put_item(
            TableName=chat_rooms_table,
            Item={
                "room_id": {"S": room_id},
                "status": {"S": "active"},
                "created_at": {"S": "2026-01-01T00:00:00+00:00"},
            },
        )
        chat_room_created = True

        # ------------------------------------------------------------------
        # POST /v1/blocks {userId: B} from A
        # ------------------------------------------------------------------
        post_url = _api_url(distribution_domain, "/v1/blocks")
        post_headers = _authed_headers(user_a["access_token"], edge_secret)
        post_resp = http_requests.post(
            post_url,
            json={"userId": b_id},
            headers=post_headers,
        )
        assert post_resp.status_code == 200, (
            f"POST /v1/blocks returned HTTP {post_resp.status_code}: {post_resp.text!r}"
        )

        # ------------------------------------------------------------------
        # Aurora assertions after POST
        # ------------------------------------------------------------------
        assert not _friendship_exists(aurora_conn, a_id, b_id), (
            "Friendship row must be gone after block"
        )
        assert not _friend_request_exists(aurora_conn, a_id, b_id), (
            "Pending friend_request row must be gone after block"
        )
        assert _blocks_row_count(aurora_conn, a_id, b_id) == 1, (
            "blocks row must exist after POST /v1/blocks"
        )

        # ------------------------------------------------------------------
        # DynamoDB assertions after POST (room was pre-created)
        # ------------------------------------------------------------------
        item_after_block = _get_chat_room(dynamo_client, chat_rooms_table, room_id)
        assert item_after_block is not None, "ChatRooms row must still exist after block"
        assert item_after_block.get("status", {}).get("S") == "deactivated", (
            f"ChatRooms status must be 'deactivated', got {item_after_block.get('status')!r}"
        )
        assert item_after_block.get("deactivated_reason", {}).get("S") == "blocked", (
            f"deactivated_reason must be 'blocked', got {item_after_block.get('deactivated_reason')!r}"
        )
        assert item_after_block.get("deactivated_by", {}).get("S") == a_id, (
            f"deactivated_by must be A's user_id ({a_id!r}), got "
            f"{item_after_block.get('deactivated_by')!r}"
        )
        assert "deactivated_at" in item_after_block, (
            "deactivated_at must be set after block"
        )

        # ------------------------------------------------------------------
        # DELETE /v1/blocks/B from A (unblock)
        # ------------------------------------------------------------------
        delete_url = _api_url(distribution_domain, f"/v1/blocks/{b_id}")
        delete_resp = http_requests.delete(delete_url, headers=post_headers)
        assert delete_resp.status_code == 200, (
            f"DELETE /v1/blocks/{{userId}} returned HTTP {delete_resp.status_code}: "
            f"{delete_resp.text!r}"
        )

        # ------------------------------------------------------------------
        # DynamoDB assertions after DELETE (room reactivated)
        # ------------------------------------------------------------------
        item_after_unblock = _get_chat_room(dynamo_client, chat_rooms_table, room_id)
        assert item_after_unblock is not None, "ChatRooms row must still exist after unblock"
        assert item_after_unblock.get("status", {}).get("S") == "active", (
            f"ChatRooms status must be 'active' after unblock, got "
            f"{item_after_unblock.get('status')!r}"
        )
        assert "deactivated_reason" not in item_after_unblock, (
            "deactivated_reason must be removed after unblock"
        )
        assert "deactivated_by" not in item_after_unblock, (
            "deactivated_by must be removed after unblock"
        )
        assert "deactivated_at" not in item_after_unblock, (
            "deactivated_at must be removed after unblock"
        )
        assert "reactivated_at" in item_after_unblock, (
            "reactivated_at must be set after unblock"
        )

    finally:
        # ------------------------------------------------------------------
        # Teardown — clean up all rows seeded directly in this test.
        # Errors are printed but never re-raised — teardown failures must not
        # mask the actual test result.
        # ------------------------------------------------------------------
        # Remove ChatRooms row (if pre-created)
        if chat_room_created and room_id is not None:
            try:
                dynamo_client.delete_item(
                    TableName=chat_rooms_table,
                    Key={"room_id": {"S": room_id}},
                )
            except Exception as exc:
                import sys
                print(
                    f"WARN: blocks test teardown — DynamoDB delete failed for "
                    f"room_id={room_id!r}: {exc}",
                    file=sys.stderr,
                )

        # Remove any remaining blocks row (the DELETE /v1/blocks call removes it,
        # but if the test failed before that step we clean up here)
        if b_id is not None:
            try:
                with aurora_conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM blocks WHERE (blocker_id = %s::uuid AND blocked_id = %s::uuid) "
                        "OR (blocker_id = %s::uuid AND blocked_id = %s::uuid)",
                        (a_id, b_id, b_id, a_id),
                    )
            except Exception as exc:
                import sys
                print(
                    f"WARN: blocks test teardown — blocks cleanup failed: {exc}",
                    file=sys.stderr,
                )

        # Remove friendship row (the POST /v1/blocks removes it, but guard here too)
        if b_id is not None:
            try:
                user_a_col, user_b_col = _canonical_pair(a_id, b_id)
                with aurora_conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM friendships WHERE user_a = %s::uuid AND user_b = %s::uuid",
                        (user_a_col, user_b_col),
                    )
            except Exception as exc:
                import sys
                print(
                    f"WARN: blocks test teardown — friendship cleanup failed: {exc}",
                    file=sys.stderr,
                )

        # Remove friend_request rows
        if b_id is not None:
            try:
                with aurora_conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM friend_requests WHERE "
                        "(requester_id = %s::uuid AND receiver_id = %s::uuid) "
                        "OR (requester_id = %s::uuid AND receiver_id = %s::uuid)",
                        (a_id, b_id, b_id, a_id),
                    )
            except Exception as exc:
                import sys
                print(
                    f"WARN: blocks test teardown — friend_requests cleanup failed: {exc}",
                    file=sys.stderr,
                )

        # Delete the B user from Cognito and Aurora
        if b_id is not None:
            try:
                cognito_client.admin_delete_user(
                    UserPoolId=user_pool_id,
                    Username=b_email,
                )
            except Exception as exc:
                import sys
                print(
                    f"WARN: blocks test teardown — admin_delete_user for B failed: {exc}",
                    file=sys.stderr,
                )

            try:
                with aurora_conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM users WHERE user_id = %s::uuid",
                        (b_id,),
                    )
            except Exception as exc:
                import sys
                print(
                    f"WARN: blocks test teardown — Aurora DELETE for B failed: {exc}",
                    file=sys.stderr,
                )


# ---------------------------------------------------------------------------
# AC-NEG: Negative flow — not friends → 409
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("completed_profile_user", ["Male"], indirect=True)
def test_given_not_friends_when_a_blocks_b_then_returns_409_not_friends(
    completed_profile_user,
):
    """
    Negative flow (AC-NEG):

      1. Mint A=Male via completed_profile_user.
      2. Mint B=Female (directly, not a friend of A — no friendship row seeded).
      3. POST /v1/blocks {userId: B} from A — assert HTTP 409 with
         {"error": "not_friends"}.
      4. Assert via Aurora: blocks row count for the pair is still 0.
      5. Assert via DynamoDB: no ChatRooms row exists for the pair
         (blocks never creates rooms — only conditionally updates existing ones).
    """
    try:
        import boto3
        import requests as http_requests
    except ImportError:
        pytest.skip("boto3 or requests not installed — live AWS environment required")

    distribution_domain = _require_env("DISTRIBUTION_DOMAIN_NAME")
    edge_secret = _require_env("EDGE_SECRET")
    region = _require_env("AWS_REGION")
    chat_rooms_table = os.environ.get("INTEGRATION_TEST_CHAT_ROOMS_TABLE", "ChatRooms")

    user_a = completed_profile_user
    a_id = user_a["sub"]
    aurora_conn = user_a["_aurora_conn"]

    user_pool_id = _require_env("COGNITO_USER_POOL_ID")
    integration_client_id = _require_env("COGNITO_INTEGRATION_TEST_CLIENT_ID")

    cognito_client = boto3.client("cognito-idp", region_name=region)
    dynamo_client = boto3.client("dynamodb", region_name=region)

    b_run_id = str(uuid.uuid4())
    b_email = f"knotify-test+{b_run_id}@example.com"
    b_password = f"Kn0tify!Test#{b_run_id[:8]}"
    b_id: str | None = None
    b_username = f"test_{uuid.uuid4().hex[:12]}"

    # Get initial blocks count before the test
    with aurora_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM blocks WHERE blocker_id = %s::uuid", (a_id,))
        initial_blocks_count: int = cur.fetchone()[0]

    try:
        # Create B (Female) but do NOT seed a friendship
        import psycopg2
        from conftest import _get_aurora_master_creds, _aurora_master_conn

        signup_resp = cognito_client.sign_up(
            ClientId=integration_client_id,
            Username=b_email,
            Password=b_password,
        )
        b_id = signup_resp["UserSub"]
        cognito_client.admin_confirm_sign_up(
            UserPoolId=user_pool_id,
            Username=b_email,
        )

        # Complete B's profile (needed so Aurora has a users row for B)
        auth_resp_b = cognito_client.admin_initiate_auth(
            UserPoolId=user_pool_id,
            ClientId=integration_client_id,
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={"USERNAME": b_email, "PASSWORD": b_password},
        )
        b_initial_token = auth_resp_b["AuthenticationResult"]["AccessToken"]

        patch_url = f"https://{distribution_domain}/v1/profile/me"
        patch_headers = {
            "Authorization": f"Bearer {b_initial_token}",
            "x-knotify-edge-secret": edge_secret,
            "Content-Type": "application/json",
        }
        patch_resp = http_requests.patch(
            patch_url,
            json={
                "first_name": "Test",
                "last_name": "User",
                "sex": "Female",
                "birthday": "2000-01-01",
                "username": b_username,
                "religion": "Other",
            },
            headers=patch_headers,
        )
        assert patch_resp.status_code == 200, (
            f"Female profile PATCH failed: HTTP {patch_resp.status_code}"
        )

        # ------------------------------------------------------------------
        # POST /v1/blocks — expect 409 NOT_FRIENDS (no friendship row)
        # ------------------------------------------------------------------
        post_url = _api_url(distribution_domain, "/v1/blocks")
        post_headers = _authed_headers(user_a["access_token"], edge_secret)
        post_resp = http_requests.post(
            post_url,
            json={"userId": b_id},
            headers=post_headers,
        )
        assert post_resp.status_code == 409, (
            f"Expected HTTP 409, got {post_resp.status_code}: {post_resp.text!r}"
        )
        resp_body = post_resp.json()
        assert resp_body.get("error") == "not_friends", (
            f"Expected error='not_friends', got {resp_body!r}"
        )

        # ------------------------------------------------------------------
        # Aurora: blocks count must be unchanged
        # ------------------------------------------------------------------
        with aurora_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM blocks WHERE blocker_id = %s::uuid", (a_id,))
            final_blocks_count: int = cur.fetchone()[0]
        assert final_blocks_count == initial_blocks_count, (
            f"blocks table count changed from {initial_blocks_count} to "
            f"{final_blocks_count} — block must not have been inserted"
        )

        # ------------------------------------------------------------------
        # DynamoDB: no ChatRooms row must exist for the pair
        # ------------------------------------------------------------------
        room_id = _chat_room_id(a_id, b_id)
        item = _get_chat_room(dynamo_client, chat_rooms_table, room_id)
        assert item is None, (
            f"ChatRooms row must not exist when 409 was returned, "
            f"but found item={item!r} for room_id={room_id!r}"
        )

    finally:
        # Teardown B
        if b_id is not None:
            try:
                cognito_client.admin_delete_user(
                    UserPoolId=user_pool_id,
                    Username=b_email,
                )
            except Exception as exc:
                import sys
                print(
                    f"WARN: blocks neg-test teardown — admin_delete_user for B failed: {exc}",
                    file=sys.stderr,
                )

            try:
                with aurora_conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM users WHERE user_id = %s::uuid",
                        (b_id,),
                    )
            except Exception as exc:
                import sys
                print(
                    f"WARN: blocks neg-test teardown — Aurora DELETE for B failed: {exc}",
                    file=sys.stderr,
                )
