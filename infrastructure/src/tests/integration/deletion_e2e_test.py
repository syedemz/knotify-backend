"""
End-to-end integration test for the full Knotify account-deletion workflow — story 9.13.

Three sub-tests:

  1. test_e2e_soft_delete_full_workflow
       Signs up users A (deleter) and B (survivor), makes them friends, exchanges
       three chat messages, registers a push token for A.  A calls
       DELETE /v1/profile/me (purge_immediately=false).  The test polls
       GET /v1/profile/me/deletion-status until SUCCEEDED, then asserts the
       full set of soft-delete side effects across Cognito, Aurora, DynamoDB.

  2. test_e2e_block_filter_regression
       Replicates the oq-9.C scenario: B blocks A before A's deletion.  After
       soft-delete A calls hard-purge (the 9.11 Lambda invoked directly with
       user_id to bypass the 30-day window).  Asserts B's deck query returns
       zero rows attributable to A, and that B can still read the deactivated
       room's message history with sender_id='[deleted-user]'.

  3. test_e2e_purge_immediately
       Signs up users C and D, exchanges messages, then C calls DELETE with
       purge_immediately=true.  Asserts C's ChatMessages rows are GONE and the
       audit row has dynamodb_retention='hard_deleted'.

Prerequisites (operators only — CI does NOT run this by default):
  - All phase-9 infra deployed in the dev environment (Step Functions state
    machine, all deletion Lambdas, HTTP API routes).
  - Aurora Data API (HTTP endpoint) enabled on the cluster — set
    enable_data_api=true in the dev aurora module and apply. No VPC tunnel
    or bastion is required; the test reaches Aurora through rds-data.
  - AWS credentials with permissions to:
      cognito-idp: sign_up, admin_confirm_sign_up, admin_initiate_auth,
                   admin_delete_user, admin_get_user
      dynamodb: GetItem, PutItem, DeleteItem, Query, UpdateItem on all tables
      rds-data: ExecuteStatement on the dev Aurora cluster
      secretsmanager: GetSecretValue (Aurora master secret)
      stepfunctions: StartExecution, DescribeExecution
      lambda: InvokeFunction (hard_purge Lambda — for sub-test 2)

Required environment variables (all skip-gated — tests skip when absent):
  COGNITO_USER_POOL_ID               — e.g. eu-central-1_abc123
  COGNITO_APP_CLIENT_ID              — app client with ADMIN_USER_PASSWORD_AUTH flow
  API_BASE_URL                       — CloudFront HTTPS base URL, no trailing slash
  APPSYNC_GRAPHQL_URL                — e.g. https://<id>.appsync-api.<region>.amazonaws.com/graphql
  AWS_REGION                         — e.g. eu-central-1
  AURORA_CLUSTER_ARN                 — full cluster ARN, e.g.
                                       arn:aws:rds:eu-central-1:<acct>:cluster:knotify-dev-aurora
  AURORA_DBNAME                      — normally "knotify"
  AURORA_MASTER_SECRET_ARN           — ARN of the Aurora-managed master secret
  EDGE_SECRET                        — value of the x-knotify-edge-secret header
  HARD_PURGE_LAMBDA_NAME             — name of the knotify-hard-purge-<env> Lambda
                                       e.g. "knotify-hard-purge-dev"

Run:
    KNOTIFY_RUN_INTEGRATION=1 \\
    pytest infrastructure/src/tests/integration/deletion_e2e_test.py -v -m integration -s

Skip without live AWS:
    pytest -m "not integration"

Design notes:
  - Each sub-test creates fresh users with uuid-suffixed emails so reruns are
    independent regardless of leftover state from prior runs.
  - Teardown for each sub-test does NOT delete users via the test helper — the
    account-deletion workflow itself is the cleanup mechanism for the deleting
    user.  The survivor (B / D) is cleaned up via admin_delete_user +
    Aurora DELETE in the fixture's finally block.
  - Polling: uses the GET /v1/profile/me/deletion-status endpoint (story 9.10).
    Cap: 30 attempts × 10 s = 5 minutes maximum.  If the execution does not
    reach SUCCEEDED within that window the test fails with the last status.
  - The block_filter sub-test invokes the hard_purge Lambda directly with
    {"user_id": "..."} (per-user mode bypasses the 30-day window).  This is
    valid: the Lambda's per-user mode is documented for exactly this use case.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import time
import uuid
from typing import Any

import pytest

from .conftest import build_profile_completion_payload

# ---------------------------------------------------------------------------
# Pytest marker
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Environment-variable gate
#
# ALL required vars must be present.  A single missing var causes every test
# in this module to skip so the failure is obvious and actionable.
# ---------------------------------------------------------------------------

_REQUIRED_ENV_VARS = [
    "COGNITO_USER_POOL_ID",
    "COGNITO_APP_CLIENT_ID",
    "API_BASE_URL",
    "APPSYNC_GRAPHQL_URL",
    "AWS_REGION",
    "AURORA_CLUSTER_ARN",
    "AURORA_DBNAME",
    "AURORA_MASTER_SECRET_ARN",
    "EDGE_SECRET",
    "HARD_PURGE_LAMBDA_NAME",
]

_KNOTIFY_RUN_INTEGRATION = os.environ.get("KNOTIFY_RUN_INTEGRATION", "")


def _check_run_gate() -> None:
    """Skip unless KNOTIFY_RUN_INTEGRATION=1 is set."""
    if _KNOTIFY_RUN_INTEGRATION != "1":
        pytest.skip(
            "Deletion E2E tests require KNOTIFY_RUN_INTEGRATION=1 "
            "and all required env vars to be set against a live dev environment."
        )


def _require_env(name: str) -> str:
    """Return env var value; pytest.skip (not raise) if absent."""
    value = os.environ.get(name)
    if not value:
        pytest.skip(
            f"Required env var {name!r} is not set — "
            "live dev AWS environment not configured. "
            "Set all required env vars to run E2E tests."
        )
    return value


def _check_all_env_vars() -> None:
    """Skip early if any required env var is missing."""
    missing = [v for v in _REQUIRED_ENV_VARS if not os.environ.get(v)]
    if missing:
        pytest.skip(
            "The following required env vars are not set — "
            "live dev AWS environment not configured: "
            + ", ".join(missing)
        )


# ---------------------------------------------------------------------------
# Lazy import helpers — collection works without boto3/requests installed
# ---------------------------------------------------------------------------


def _boto3():
    try:
        import boto3 as _b
        return _b
    except ImportError:
        pytest.skip("boto3 is not installed — live AWS environment required")


def _requests():
    try:
        import requests as _r
        return _r
    except ImportError:
        pytest.skip("requests is not installed — live AWS environment required")


def _rds_data_client(region: str):
    """
    Returns a boto3 rds-data client. Aurora must have the Data API
    (HTTP endpoint) enabled on the cluster for this to work — set
    enable_data_api=true in the dev environment's aurora module.
    """
    boto3 = _boto3()
    return boto3.client("rds-data", region_name=region)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _api_url(base: str, path: str) -> str:
    return base.rstrip("/") + path


def _authed_headers(access_token: str, edge_secret: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "x-knotify-edge-secret": edge_secret,
        "Content-Type": "application/json",
    }


def _graphql_headers(id_token: str) -> dict:
    return {
        "Authorization": id_token,
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------


def _decode_jwt_claims(token: str) -> dict:
    """Decode JWT payload claims WITHOUT signature verification (test use only)."""
    payload_b64 = token.split(".")[1]
    padding = 4 - len(payload_b64) % 4
    if padding != 4:
        payload_b64 += "=" * padding
    return json.loads(base64.urlsafe_b64decode(payload_b64))


# ---------------------------------------------------------------------------
# DynamoDB helpers
# ---------------------------------------------------------------------------


def _ddb_client(region: str):
    import boto3
    return boto3.client("dynamodb", region_name=region)


def _chat_room_id(id_a: str, id_b: str) -> str:
    lo, hi = min(id_a, id_b), max(id_a, id_b)
    return hashlib.sha256(f"{lo}:{hi}".encode()).hexdigest()


def _query_chat_messages(ddb, room_id: str) -> list[dict]:
    """Return all ChatMessages items for a room_id (consistent read)."""
    resp = ddb.query(
        TableName="ChatMessages",
        KeyConditionExpression="room_id = :rid",
        ExpressionAttributeValues={":rid": {"S": room_id}},
        ConsistentRead=True,
    )
    return resp.get("Items", [])


def _get_chat_room(ddb, room_id: str) -> dict | None:
    resp = ddb.get_item(
        TableName="ChatRooms",
        Key={"room_id": {"S": room_id}},
        ConsistentRead=True,
    )
    return resp.get("Item")


def _query_membership(ddb, user_id: str) -> list[dict]:
    """Return all ChatRoomMembership rows for user_id (consistent read)."""
    resp = ddb.query(
        TableName="ChatRoomMembership",
        KeyConditionExpression="user_id = :uid",
        ExpressionAttributeValues={":uid": {"S": user_id}},
        ConsistentRead=True,
    )
    return resp.get("Items", [])


def _get_membership_for_room(ddb, user_id: str, room_id: str) -> dict | None:
    resp = ddb.get_item(
        TableName="ChatRoomMembership",
        Key={"user_id": {"S": user_id}, "room_id": {"S": room_id}},
        ConsistentRead=True,
    )
    return resp.get("Item")


def _query_push_tokens(ddb, user_id: str) -> list[dict]:
    resp = ddb.query(
        TableName="PushNotificationTokens",
        KeyConditionExpression="user_id = :uid",
        ExpressionAttributeValues={":uid": {"S": user_id}},
        ConsistentRead=True,
    )
    return resp.get("Items", [])


def _query_notifications(ddb, user_id: str) -> list[dict]:
    resp = ddb.query(
        TableName="Notifications",
        KeyConditionExpression="user_id = :uid",
        ExpressionAttributeValues={":uid": {"S": user_id}},
        ConsistentRead=True,
    )
    return resp.get("Items", [])


def _query_audit_log(ddb, user_id: str) -> list[dict]:
    """Return all account_deletion_audit rows for user_id (consistent read)."""
    resp = ddb.query(
        TableName="account_deletion_audit",
        KeyConditionExpression="user_id = :uid",
        ExpressionAttributeValues={":uid": {"S": user_id}},
        ConsistentRead=True,
    )
    return resp.get("Items", [])


def _delete_ddb_item(ddb, table: str, key: dict) -> None:
    """Delete a DynamoDB item; swallow errors so teardown never masks test failures."""
    try:
        ddb.delete_item(TableName=table, Key=key)
    except Exception as exc:
        print(f"WARN: teardown DDB DeleteItem {table} {key!r} failed: {exc}", file=sys.stderr)


def _delete_all_chat_messages(ddb, room_id: str) -> None:
    items = _query_chat_messages(ddb, room_id)
    for item in items:
        _delete_ddb_item(
            ddb,
            "ChatMessages",
            {
                "room_id": {"S": room_id},
                "created_at_message_id": item["created_at_message_id"],
            },
        )


def _delete_membership(ddb, user_id: str, room_id: str) -> None:
    _delete_ddb_item(
        ddb,
        "ChatRoomMembership",
        {"user_id": {"S": user_id}, "room_id": {"S": room_id}},
    )


def _delete_chat_room(ddb, room_id: str) -> None:
    _delete_ddb_item(ddb, "ChatRooms", {"room_id": {"S": room_id}})


def _delete_audit_rows(ddb, user_id: str) -> None:
    """Delete all audit rows for user_id (teardown only)."""
    rows = _query_audit_log(ddb, user_id)
    for row in rows:
        _delete_ddb_item(
            ddb,
            "account_deletion_audit",
            {"user_id": {"S": user_id}, "event_id": row["event_id"]},
        )


# ---------------------------------------------------------------------------
# Aurora helpers
# ---------------------------------------------------------------------------


class _AuroraDataApi:
    """
    Thin shim over boto3 rds-data so the rest of this test file reads like the
    old psycopg2-based code: `aurora.execute(sql, params)` returns rows as a
    list of dicts keyed by column name.

    Data API parameter style is `:name`; UUID columns require an explicit
    cast (`cast(:user_id as uuid)`) because Data API passes all params as
    string values.
    """

    def __init__(self, client, cluster_arn: str, secret_arn: str, database: str):
        self._client = client
        self._cluster_arn = cluster_arn
        self._secret_arn = secret_arn
        self._database = database

    def execute(self, sql: str, params: dict | None = None) -> list[dict]:
        param_list = []
        for name, value in (params or {}).items():
            if value is None:
                param_list.append({"name": name, "value": {"isNull": True}})
            else:
                param_list.append({"name": name, "value": {"stringValue": str(value)}})

        try:
            resp = self._client.execute_statement(
                resourceArn=self._cluster_arn,
                secretArn=self._secret_arn,
                database=self._database,
                sql=sql,
                parameters=param_list,
                includeResultMetadata=True,
            )
        except self._client.exceptions.BadRequestException as exc:
            if "HttpEndpointNotEnabled" in str(exc) or "HTTP endpoint" in str(exc):
                pytest.skip(
                    "Aurora Data API is not enabled on the dev cluster. "
                    "Set enable_data_api=true in infrastructure/environments/dev "
                    "and apply, then re-run."
                )
            raise

        cols = [c["name"] for c in resp.get("columnMetadata", [])]
        rows: list[dict] = []
        for record in resp.get("records", []):
            row: dict = {}
            for name, field in zip(cols, record):
                if field.get("isNull"):
                    row[name] = None
                elif "stringValue" in field:
                    row[name] = field["stringValue"]
                elif "longValue" in field:
                    row[name] = field["longValue"]
                elif "doubleValue" in field:
                    row[name] = field["doubleValue"]
                elif "booleanValue" in field:
                    row[name] = field["booleanValue"]
                elif "blobValue" in field:
                    row[name] = field["blobValue"]
                elif "arrayValue" in field:
                    row[name] = field["arrayValue"]
                else:
                    row[name] = None
            rows.append(row)
        return rows

    def close(self) -> None:
        # Data API is stateless — nothing to close.
        return


def _get_aurora_user_row(aurora: _AuroraDataApi, user_id: str) -> dict | None:
    """
    Return the users row for user_id as a dict, or None if the row does not exist.
    """
    rows = aurora.execute(
        """
        SELECT user_id, email, phone_number, photo_url, chosen_profile_avatar,
               username, first_name, last_name, preferences, preference_vector,
               deleted_at
        FROM users
        WHERE user_id = cast(:user_id as uuid)
        """,
        {"user_id": user_id},
    )
    return rows[0] if rows else None


def _delete_aurora_user(aurora: _AuroraDataApi, user_id: str) -> None:
    """Hard-delete a user from Aurora (teardown only)."""
    try:
        aurora.execute(
            "DELETE FROM users WHERE user_id = cast(:user_id as uuid)",
            {"user_id": user_id},
        )
    except Exception as exc:
        print(
            f"WARN: teardown Aurora user delete {user_id!r} failed: {exc}",
            file=sys.stderr,
        )


def _delete_friendship(aurora: _AuroraDataApi, id_a: str, id_b: str) -> None:
    try:
        user_a, user_b = min(id_a, id_b), max(id_a, id_b)
        aurora.execute(
            "DELETE FROM friendships WHERE user_a = cast(:a as uuid) AND user_b = cast(:b as uuid)",
            {"a": user_a, "b": user_b},
        )
    except Exception as exc:
        print(f"WARN: teardown friendship delete failed: {exc}", file=sys.stderr)


def _delete_blocks_aurora(aurora: _AuroraDataApi, id_a: str, id_b: str) -> None:
    try:
        aurora.execute(
            """
            DELETE FROM blocks WHERE
              (blocker_id = cast(:a as uuid) AND blocked_id = cast(:b as uuid))
              OR (blocker_id = cast(:b as uuid) AND blocked_id = cast(:a as uuid))
            """,
            {"a": id_a, "b": id_b},
        )
    except Exception as exc:
        print(f"WARN: teardown blocks delete failed: {exc}", file=sys.stderr)


def _delete_friend_requests_aurora(aurora: _AuroraDataApi, id_a: str, id_b: str) -> None:
    try:
        aurora.execute(
            """
            DELETE FROM friend_requests WHERE
              (from_user_id = cast(:a as uuid) AND to_user_id = cast(:b as uuid))
              OR (from_user_id = cast(:b as uuid) AND to_user_id = cast(:a as uuid))
            """,
            {"a": id_a, "b": id_b},
        )
    except Exception as exc:
        print(f"WARN: teardown friend_requests delete failed: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Cognito helpers
# ---------------------------------------------------------------------------


def _mint_completed_user(
    cognito_client,
    http_requests,
    *,
    user_pool_id: str,
    client_id: str,
    api_base: str,
    edge_secret: str,
    sex: str,
) -> dict:
    """
    Create a Cognito test user, complete their profile, and return fresh tokens.

    Returns a dict:
        sub, email, password, id_token, access_token, username
    """
    run_id = str(uuid.uuid4())
    email = f"knotify-e2e+del-{run_id}@example.com"
    password = f"Kn0tify!Del#{run_id[:8]}"
    username = f"del_{uuid.uuid4().hex[:12]}"

    signup_resp = cognito_client.sign_up(
        ClientId=client_id,
        Username=email,
        Password=password,
    )
    sub = signup_resp["UserSub"]
    cognito_client.admin_confirm_sign_up(UserPoolId=user_pool_id, Username=email)

    # Initial auth
    auth_resp = cognito_client.admin_initiate_auth(
        UserPoolId=user_pool_id,
        ClientId=client_id,
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": email, "PASSWORD": password},
    )
    initial_access = auth_resp["AuthenticationResult"]["AccessToken"]

    # Complete profile so PreTokenGeneration flips profile_complete="true".
    # The conftest helper assembles the full 34-field payload required by the
    # post-story-7.0b CHECK constraint (migration 0012). A 6-field payload
    # would leave profile_complete_verified=false, the AdminUpdateUserAttributes
    # call in profile/handler.py would never fire, and the assertion below
    # would fail.
    completion_payload = build_profile_completion_payload(sex=sex, username=username)
    completion_payload["first_name"] = "DelTest"
    patch_resp = http_requests.patch(
        _api_url(api_base, "/v1/profile/me"),
        json=completion_payload,
        headers={
            "Authorization": f"Bearer {initial_access}",
            "x-knotify-edge-secret": edge_secret,
            "Content-Type": "application/json",
        },
    )
    assert patch_resp.status_code == 200, (
        f"Profile PATCH failed for sex={sex!r}: HTTP {patch_resp.status_code} "
        f"body={patch_resp.text[:300]!r}"
    )

    # Fresh auth — PreTokenGeneration now embeds profile_complete="true"
    auth_resp2 = cognito_client.admin_initiate_auth(
        UserPoolId=user_pool_id,
        ClientId=client_id,
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": email, "PASSWORD": password},
    )
    fresh = auth_resp2["AuthenticationResult"]

    id_claims = _decode_jwt_claims(fresh["IdToken"])
    assert id_claims.get("custom:profile_complete") == "true", (
        f"Expected custom:profile_complete='true' in IdToken after PATCH, "
        f"got {id_claims.get('custom:profile_complete')!r}"
    )

    return {
        "sub": sub,
        "email": email,
        "password": password,
        "id_token": fresh["IdToken"],
        "access_token": fresh["AccessToken"],
        "username": username,
    }


def _teardown_cognito_user(cognito_client, user_pool_id: str, email: str) -> None:
    """Delete a Cognito user; suppress UserNotFoundException (already deleted by workflow)."""
    try:
        cognito_client.admin_delete_user(UserPoolId=user_pool_id, Username=email)
    except cognito_client.exceptions.UserNotFoundException:
        pass  # Deleted by the workflow — expected for the deleter user
    except Exception as exc:
        print(
            f"WARN: teardown admin_delete_user({email!r}) failed: {exc}",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# AppSync GraphQL helpers (HTTP mutations only — no subscriptions needed)
# ---------------------------------------------------------------------------

_GQL_CREATE_OR_GET_ROOM = """
mutation CreateOrGetRoom($otherUserId: ID!) {
  createOrGetRoom(otherUserId: $otherUserId) {
    roomId
    userA
    userB
    status
    friendshipActive
  }
}
"""

_GQL_SEND_MESSAGE = """
mutation SendMessage($roomId: ID!, $content: String!, $contentType: String) {
  sendMessage(roomId: $roomId, content: $content, contentType: $contentType) {
    roomId
    messageId
    senderId
    content
    contentType
    deliveredAt
  }
}
"""


def _appsync_mutation(
    graphql_url: str,
    id_token: str,
    query: str,
    variables: dict,
    http_requests,
) -> dict:
    """Execute a GraphQL mutation against AppSync using HTTP + Cognito JWT."""
    resp = http_requests.post(
        graphql_url,
        json={"query": query, "variables": variables},
        headers=_graphql_headers(id_token),
        timeout=15,
    )
    assert resp.status_code == 200, (
        f"AppSync mutation HTTP {resp.status_code}: {resp.text[:400]!r}"
    )
    return resp.json()


# ---------------------------------------------------------------------------
# REST API helpers
# ---------------------------------------------------------------------------


def _make_friends(
    http_requests,
    *,
    requester: dict,
    target: dict,
    api_base: str,
    edge_secret: str,
) -> str:
    """
    Send a friend request from requester to target and have target accept.

    Returns the request_id.
    """
    fr_resp = http_requests.post(
        _api_url(api_base, "/v1/friend-requests"),
        json={"toUserId": target["sub"]},
        headers=_authed_headers(requester["access_token"], edge_secret),
        timeout=15,
    )
    assert fr_resp.status_code in (200, 201), (
        f"POST /v1/friend-requests returned HTTP {fr_resp.status_code}: "
        f"{fr_resp.text[:300]!r}"
    )
    request_id = fr_resp.json().get("request_id")
    assert request_id, f"Expected 'request_id' in response, got {fr_resp.json()!r}"

    accept_resp = http_requests.post(
        _api_url(api_base, f"/v1/friend-requests/{request_id}/accept"),
        headers=_authed_headers(target["access_token"], edge_secret),
        timeout=15,
    )
    assert accept_resp.status_code == 200, (
        f"POST .../accept returned HTTP {accept_resp.status_code}: "
        f"{accept_resp.text[:300]!r}"
    )
    return request_id


def _register_push_token(
    user: dict,
    api_base: str,
    device_id: str,
    push_token: str,
    http_requests,
) -> None:
    resp = http_requests.post(
        _api_url(api_base, "/v1/push-tokens"),
        json={
            "platform": "ios",
            "push_token": push_token,
            "device_id": device_id,
            "app_version": "1.0.0-e2e-del",
        },
        headers={
            "Authorization": f"Bearer {user['access_token']}",
            "Content-Type": "application/json",
        },
        timeout=15,
    )
    assert resp.status_code == 200, (
        f"POST /v1/push-tokens failed: HTTP {resp.status_code} body={resp.text[:200]!r}"
    )


# ---------------------------------------------------------------------------
# Step Functions polling helper
# ---------------------------------------------------------------------------

_POLL_MAX_ATTEMPTS = 30
_POLL_SLEEP_SECS = 10


def _poll_deletion_status(
    http_requests,
    *,
    api_base: str,
    access_token: str,
    edge_secret: str,
    execution_arn: str,
) -> str:
    """
    Poll GET /v1/profile/me/deletion-status until the execution reaches
    a terminal state (SUCCEEDED, FAILED, TIMED_OUT, ABORTED).

    Cap: _POLL_MAX_ATTEMPTS × _POLL_SLEEP_SECS (default 30 × 10s = 5 minutes).

    Returns the final status string.

    Raises:
        AssertionError — if the execution does not reach a terminal state
                         within the polling budget.
    """
    terminal_states = {"SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"}
    last_status = "UNKNOWN"

    for attempt in range(1, _POLL_MAX_ATTEMPTS + 1):
        resp = http_requests.get(
            _api_url(api_base, "/v1/profile/me/deletion-status"),
            params={"executionArn": execution_arn},
            headers=_authed_headers(access_token, edge_secret),
            timeout=20,
        )
        assert resp.status_code == 200, (
            f"GET /v1/profile/me/deletion-status returned HTTP {resp.status_code} "
            f"on attempt {attempt}: {resp.text[:300]!r}"
        )
        body = resp.json()
        last_status = body.get("status", "UNKNOWN")

        if last_status in terminal_states:
            return last_status

        print(
            f"  [poll {attempt}/{_POLL_MAX_ATTEMPTS}] status={last_status!r} — sleeping {_POLL_SLEEP_SECS}s",
            file=sys.stderr,
        )
        time.sleep(_POLL_SLEEP_SECS)

    pytest.fail(
        f"Deletion execution did not reach a terminal state within "
        f"{_POLL_MAX_ATTEMPTS * _POLL_SLEEP_SECS} seconds. "
        f"Last observed status: {last_status!r}. "
        f"executionArn: {execution_arn!r}"
    )


# ---------------------------------------------------------------------------
# Lambda invoke helper for hard_purge
# ---------------------------------------------------------------------------


def _invoke_hard_purge_lambda(
    boto3_module,
    *,
    region: str,
    function_name: str,
    user_id: str,
) -> dict:
    """
    Invoke the knotify-hard-purge-<env> Lambda in per-user mode.

    Returns the parsed response payload.

    Raises:
        AssertionError — if the Lambda invocation returns a FunctionError.
    """
    client = boto3_module.client("lambda", region_name=region)
    payload = json.dumps({"user_id": user_id}).encode()

    resp = client.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=payload,
    )

    # A FunctionError key is present when the Lambda itself raised an exception.
    function_error = resp.get("FunctionError")
    payload_bytes = resp["Payload"].read()
    payload_parsed = json.loads(payload_bytes)

    assert function_error is None, (
        f"hard_purge Lambda returned FunctionError={function_error!r}. "
        f"Payload: {payload_parsed!r}"
    )
    return payload_parsed


# ===========================================================================
# Sub-test 1: soft-delete full workflow
# ===========================================================================


def test_e2e_soft_delete_full_workflow():  # noqa: C901 (flat sequential by design)
    """
    End-to-end soft-delete workflow (purge_immediately=false).

    Story 9.13 acceptance criteria (soft-delete assertions block):
      - Cognito user A is deleted (AdminGetUser returns UserNotFoundException).
      - Aurora users row for A has deleted_at set with PII nulled.
      - All 3 ChatMessages rows A sent now have sender_id='[deleted-user]'
        with content preserved.
      - ChatRooms row is status='deactivated' with deactivated_reason='user_deleted_account'.
      - A's ChatRoomMembership rows are empty (Query PK=A.user_id returns zero rows).
      - B's ChatRoomMembership row for the shared room is still present.
      - Notifications and PushNotificationTokens for A are empty.
      - An audit row "deletion_completed" exists with
        dynamodb_retention='permanent_anonymized'.
    """
    _check_run_gate()
    _check_all_env_vars()

    boto3 = _boto3()
    http_requests = _requests()

    user_pool_id = _require_env("COGNITO_USER_POOL_ID")
    client_id = _require_env("COGNITO_APP_CLIENT_ID")
    api_base = _require_env("API_BASE_URL").rstrip("/")
    graphql_url = _require_env("APPSYNC_GRAPHQL_URL")
    region = _require_env("AWS_REGION")
    aurora_cluster_arn = _require_env("AURORA_CLUSTER_ARN")
    aurora_dbname = _require_env("AURORA_DBNAME")
    master_secret_arn = _require_env("AURORA_MASTER_SECRET_ARN")
    edge_secret = _require_env("EDGE_SECRET")

    cognito = boto3.client("cognito-idp", region_name=region)
    ddb = _ddb_client(region)

    aurora = _AuroraDataApi(
        client=_rds_data_client(region),
        cluster_arn=aurora_cluster_arn,
        secret_arn=master_secret_arn,
        database=aurora_dbname,
    )

    user_a: dict | None = None
    user_b: dict | None = None

    try:
        # ------------------------------------------------------------------
        # Step 1: Provision users A (deleter) and B (survivor)
        # ------------------------------------------------------------------
        user_a = _mint_completed_user(
            cognito, http_requests,
            user_pool_id=user_pool_id, client_id=client_id,
            api_base=api_base, edge_secret=edge_secret,
            sex="Male",
        )
        user_b = _mint_completed_user(
            cognito, http_requests,
            user_pool_id=user_pool_id, client_id=client_id,
            api_base=api_base, edge_secret=edge_secret,
            sex="Female",
        )
        a_id = user_a["sub"]
        b_id = user_b["sub"]
        room_id = _chat_room_id(a_id, b_id)

        # ------------------------------------------------------------------
        # Step 2: Make A and B friends
        # ------------------------------------------------------------------
        _make_friends(
            http_requests,
            requester=user_a,
            target=user_b,
            api_base=api_base,
            edge_secret=edge_secret,
        )

        # ------------------------------------------------------------------
        # Step 3: Register a push token for A
        # ------------------------------------------------------------------
        a_device_id = f"e2e-del-device-{a_id}"
        a_push_token = f"ExponentPushToken[e2e-del-a-{uuid.uuid4().hex[:16]}]"
        _register_push_token(user_a, api_base, a_device_id, a_push_token, http_requests)

        # ------------------------------------------------------------------
        # Step 4: Create the chat room (A calls createOrGetRoom)
        # ------------------------------------------------------------------
        room_resp = _appsync_mutation(
            graphql_url, user_a["id_token"],
            _GQL_CREATE_OR_GET_ROOM,
            {"otherUserId": b_id},
            http_requests,
        )
        assert "errors" not in room_resp, (
            f"createOrGetRoom returned errors: {room_resp.get('errors')!r}"
        )
        assert room_resp["data"]["createOrGetRoom"]["roomId"] == room_id

        # ------------------------------------------------------------------
        # Step 5: A sends 3 messages
        # ------------------------------------------------------------------
        messages_sent = ["first message", "second message", "third message"]
        for content in messages_sent:
            send_resp = _appsync_mutation(
                graphql_url, user_a["id_token"],
                _GQL_SEND_MESSAGE,
                {"roomId": room_id, "content": content, "contentType": "text"},
                http_requests,
            )
            assert "errors" not in send_resp or not send_resp.get("errors"), (
                f"sendMessage({content!r}) returned errors: {send_resp.get('errors')!r}"
            )
            assert send_resp["data"]["sendMessage"]["senderId"] == a_id

        # Verify all 3 messages are in DynamoDB before deletion
        pre_delete_msgs = _query_chat_messages(ddb, room_id)
        a_msgs_pre = [m for m in pre_delete_msgs if m.get("sender_id", {}).get("S") == a_id]
        assert len(a_msgs_pre) == 3, (
            f"Expected 3 messages from A before deletion, found {len(a_msgs_pre)}"
        )

        # ------------------------------------------------------------------
        # Step 6: A calls DELETE /v1/profile/me (purge_immediately=false)
        # ------------------------------------------------------------------
        delete_resp = http_requests.delete(
            _api_url(api_base, "/v1/profile/me"),
            json={"purge_immediately": False},
            headers=_authed_headers(user_a["access_token"], edge_secret),
            timeout=20,
        )
        assert delete_resp.status_code == 202, (
            f"DELETE /v1/profile/me returned HTTP {delete_resp.status_code}: "
            f"{delete_resp.text[:300]!r}"
        )
        execution_arn = delete_resp.json().get("executionArn")
        assert execution_arn, f"Expected executionArn in 202 response, got {delete_resp.json()!r}"

        # ------------------------------------------------------------------
        # Step 7: Poll deletion-status until SUCCEEDED
        # ------------------------------------------------------------------
        final_status = _poll_deletion_status(
            http_requests,
            api_base=api_base,
            access_token=user_a["access_token"],
            edge_secret=edge_secret,
            execution_arn=execution_arn,
        )
        assert final_status == "SUCCEEDED", (
            f"Expected deletion execution to reach SUCCEEDED, got {final_status!r}. "
            f"executionArn: {execution_arn!r}"
        )

        # ------------------------------------------------------------------
        # Assertions: Cognito user A is deleted
        # ------------------------------------------------------------------
        try:
            cognito.admin_get_user(UserPoolId=user_pool_id, Username=user_a["email"])
            pytest.fail(
                f"Expected AdminGetUser for user A ({user_a['email']!r}) to raise "
                "UserNotFoundException after soft-delete, but it succeeded."
            )
        except cognito.exceptions.UserNotFoundException:
            pass  # Correct — user was deleted by the workflow

        # ------------------------------------------------------------------
        # Assertions: Aurora users row for A has deleted_at set and PII nulled
        # ------------------------------------------------------------------
        aurora_row = _get_aurora_user_row(aurora, a_id)
        assert aurora_row is not None, (
            f"Aurora users row for A ({a_id!r}) not found after soft-delete. "
            "Expected the row to exist with deleted_at set (soft-delete, not hard-purge)."
        )
        assert aurora_row["deleted_at"] is not None, (
            f"Aurora users.deleted_at is None for user A after soft-delete workflow. "
            f"Row: {aurora_row!r}"
        )
        # PII fields must be nulled
        for pii_field in ["email", "phone_number", "photo_url", "chosen_profile_avatar", "preference_vector"]:
            assert aurora_row[pii_field] is None, (
                f"Aurora users.{pii_field} must be NULL after soft-delete PII strip, "
                f"got {aurora_row[pii_field]!r}"
            )
        assert aurora_row["username"] == "[deleted-user]", (
            f"Aurora users.username must be '[deleted-user]' after soft-delete, "
            f"got {aurora_row['username']!r}"
        )
        assert aurora_row["first_name"] == "Deleted", (
            f"Aurora users.first_name must be 'Deleted' after soft-delete, "
            f"got {aurora_row['first_name']!r}"
        )
        assert aurora_row["last_name"] == "User", (
            f"Aurora users.last_name must be 'User' after soft-delete, "
            f"got {aurora_row['last_name']!r}"
        )

        # ------------------------------------------------------------------
        # Assertions: All 3 ChatMessages from A are anonymized
        # ------------------------------------------------------------------
        post_delete_msgs = _query_chat_messages(ddb, room_id)
        a_msgs_post = [m for m in post_delete_msgs if m.get("sender_id", {}).get("S") == a_id]
        assert len(a_msgs_post) == 0, (
            f"Expected 0 messages still attributed to A's user_id after anonymization, "
            f"found {len(a_msgs_post)}"
        )
        anonymized_msgs = [
            m for m in post_delete_msgs
            if m.get("sender_id", {}).get("S") == "[deleted-user]"
        ]
        assert len(anonymized_msgs) == 3, (
            f"Expected 3 messages with sender_id='[deleted-user]' after anonymization, "
            f"found {len(anonymized_msgs)}. All messages: {post_delete_msgs!r}"
        )
        # Content must be preserved
        actual_contents = sorted(m["content"]["S"] for m in anonymized_msgs)
        expected_contents = sorted(messages_sent)
        assert actual_contents == expected_contents, (
            f"Chat message content changed after anonymization. "
            f"Expected {expected_contents!r}, got {actual_contents!r}"
        )

        # ------------------------------------------------------------------
        # Assertions: ChatRooms row is deactivated with correct reason
        # ------------------------------------------------------------------
        room_item = _get_chat_room(ddb, room_id)
        assert room_item is not None, f"ChatRooms row {room_id!r} missing after soft-delete"
        assert room_item.get("status", {}).get("S") == "deactivated", (
            f"ChatRooms.status must be 'deactivated', got {room_item.get('status')!r}"
        )
        assert room_item.get("deactivated_reason", {}).get("S") == "user_deleted_account", (
            f"ChatRooms.deactivated_reason must be 'user_deleted_account', "
            f"got {room_item.get('deactivated_reason')!r}"
        )

        # ------------------------------------------------------------------
        # Assertions: A's ChatRoomMembership rows are empty
        # ------------------------------------------------------------------
        a_memberships = _query_membership(ddb, a_id)
        assert len(a_memberships) == 0, (
            f"Expected 0 ChatRoomMembership rows for A after soft-delete, "
            f"found {len(a_memberships)}: {a_memberships!r}"
        )

        # ------------------------------------------------------------------
        # Assertions: B's ChatRoomMembership row for the shared room is present
        # ------------------------------------------------------------------
        b_membership = _get_membership_for_room(ddb, b_id, room_id)
        assert b_membership is not None, (
            f"B's ChatRoomMembership row for room {room_id!r} must still exist "
            "after A's soft-delete (surviving participant's history is preserved)."
        )

        # ------------------------------------------------------------------
        # Assertions: Notifications and PushNotificationTokens for A are empty
        # ------------------------------------------------------------------
        a_notifications = _query_notifications(ddb, a_id)
        assert len(a_notifications) == 0, (
            f"Expected 0 Notifications rows for A after soft-delete, "
            f"found {len(a_notifications)}"
        )
        a_push_tokens = _query_push_tokens(ddb, a_id)
        assert len(a_push_tokens) == 0, (
            f"Expected 0 PushNotificationTokens rows for A after soft-delete, "
            f"found {len(a_push_tokens)}"
        )

        # ------------------------------------------------------------------
        # Assertions: Audit row "deletion_completed" with permanent_anonymized
        # ------------------------------------------------------------------
        audit_rows = _query_audit_log(ddb, a_id)
        completed_rows = [
            r for r in audit_rows
            if r.get("event_type", {}).get("S") == "deletion_completed"
        ]
        assert len(completed_rows) >= 1, (
            f"Expected at least one 'deletion_completed' audit row for A, "
            f"found {len(completed_rows)}. All audit rows: {audit_rows!r}"
        )
        completed_row = completed_rows[0]
        dynamodb_retention = completed_row.get("dynamodb_retention", {}).get("S")
        assert dynamodb_retention == "permanent_anonymized", (
            f"audit 'deletion_completed' row must have dynamodb_retention='permanent_anonymized', "
            f"got {dynamodb_retention!r}"
        )

    finally:
        # ------------------------------------------------------------------
        # Teardown — clean up survivor B and any leftover DDB rows.
        # User A's Cognito entry was deleted by the workflow; Aurora row for A
        # retains deleted_at for 30-day retention (no manual cleanup needed
        # beyond the test — the scheduled hard_purge handles it).
        # We do NOT manually delete B's friendship/blocks since the deletion
        # workflow itself cleaned up A's side; just clean B's user + leftover rows.
        # ------------------------------------------------------------------
        a_id_td = (user_a or {}).get("sub")
        b_id_td = (user_b or {}).get("sub")

        if a_id_td and b_id_td:
            room_id_td = _chat_room_id(a_id_td, b_id_td)
            _delete_all_chat_messages(ddb, room_id_td)
            _delete_membership(ddb, a_id_td, room_id_td)
            _delete_membership(ddb, b_id_td, room_id_td)
            _delete_chat_room(ddb, room_id_td)
            _delete_audit_rows(ddb, a_id_td)

        # Clean up user A's Aurora row (still has deleted_at set but not hard-purged)
        if user_a:
            _delete_aurora_user(aurora, user_a["sub"])
            # Cognito: UserNotFoundException is expected — already deleted by workflow
            _teardown_cognito_user(cognito, user_pool_id, user_a["email"])

        if user_b:
            _delete_aurora_user(aurora, user_b["sub"])
            _teardown_cognito_user(cognito, user_pool_id, user_b["email"])

        try:
            aurora.close()
        except Exception:
            pass


# ===========================================================================
# Sub-test 2: block_filter regression (oq-9.C)
# ===========================================================================


def test_e2e_block_filter_regression():  # noqa: C901 (flat sequential by design)
    """
    block_filter regression scenario — open question oq-9.C.

    B blocks A before A's deletion.  After A is soft-deleted the test invokes
    the hard_purge Lambda directly with {"user_id": A.sub} to bypass the 30-day
    window and assert the following:

      - B's GET /v1/match/deck response contains no rows where user_id == A.sub.
        (After hard-purge, A's users row is gone so the deck JOIN cannot surface A.)

      - B can still read the deactivated shared room's message history; the
        anonymized messages have sender_id='[deleted-user]'.
    """
    _check_run_gate()
    _check_all_env_vars()

    boto3 = _boto3()
    http_requests = _requests()

    user_pool_id = _require_env("COGNITO_USER_POOL_ID")
    client_id = _require_env("COGNITO_APP_CLIENT_ID")
    api_base = _require_env("API_BASE_URL").rstrip("/")
    graphql_url = _require_env("APPSYNC_GRAPHQL_URL")
    region = _require_env("AWS_REGION")
    aurora_cluster_arn = _require_env("AURORA_CLUSTER_ARN")
    aurora_dbname = _require_env("AURORA_DBNAME")
    master_secret_arn = _require_env("AURORA_MASTER_SECRET_ARN")
    edge_secret = _require_env("EDGE_SECRET")
    hard_purge_function = _require_env("HARD_PURGE_LAMBDA_NAME")

    cognito = boto3.client("cognito-idp", region_name=region)
    ddb = _ddb_client(region)

    aurora = _AuroraDataApi(
        client=_rds_data_client(region),
        cluster_arn=aurora_cluster_arn,
        secret_arn=master_secret_arn,
        database=aurora_dbname,
    )

    user_a: dict | None = None
    user_b: dict | None = None

    try:
        # ------------------------------------------------------------------
        # Step 1: Provision users A (deleter) and B (survivor, blocker)
        # ------------------------------------------------------------------
        user_a = _mint_completed_user(
            cognito, http_requests,
            user_pool_id=user_pool_id, client_id=client_id,
            api_base=api_base, edge_secret=edge_secret,
            sex="Male",
        )
        user_b = _mint_completed_user(
            cognito, http_requests,
            user_pool_id=user_pool_id, client_id=client_id,
            api_base=api_base, edge_secret=edge_secret,
            sex="Female",
        )
        a_id = user_a["sub"]
        b_id = user_b["sub"]
        room_id = _chat_room_id(a_id, b_id)

        # ------------------------------------------------------------------
        # Step 2: A and B become friends
        # ------------------------------------------------------------------
        _make_friends(
            http_requests,
            requester=user_a,
            target=user_b,
            api_base=api_base,
            edge_secret=edge_secret,
        )

        # ------------------------------------------------------------------
        # Step 3: Create chat room and send a message so the room exists
        # ------------------------------------------------------------------
        room_resp = _appsync_mutation(
            graphql_url, user_a["id_token"],
            _GQL_CREATE_OR_GET_ROOM,
            {"otherUserId": b_id},
            http_requests,
        )
        assert "errors" not in room_resp, (
            f"createOrGetRoom returned errors: {room_resp.get('errors')!r}"
        )

        send_resp = _appsync_mutation(
            graphql_url, user_a["id_token"],
            _GQL_SEND_MESSAGE,
            {"roomId": room_id, "content": "block regression message", "contentType": "text"},
            http_requests,
        )
        assert "errors" not in send_resp or not send_resp.get("errors"), (
            f"sendMessage returned errors: {send_resp.get('errors')!r}"
        )

        # ------------------------------------------------------------------
        # Step 4: B blocks A (before A's deletion)
        # ------------------------------------------------------------------
        block_resp = http_requests.post(
            _api_url(api_base, "/v1/blocks"),
            json={"userId": a_id},
            headers=_authed_headers(user_b["access_token"], edge_secret),
            timeout=15,
        )
        assert block_resp.status_code == 200, (
            f"B POST /v1/blocks returned HTTP {block_resp.status_code}: "
            f"{block_resp.text[:300]!r}"
        )

        # ------------------------------------------------------------------
        # Step 5: A calls DELETE /v1/profile/me (purge_immediately=false)
        # ------------------------------------------------------------------
        delete_resp = http_requests.delete(
            _api_url(api_base, "/v1/profile/me"),
            json={"purge_immediately": False},
            headers=_authed_headers(user_a["access_token"], edge_secret),
            timeout=20,
        )
        assert delete_resp.status_code == 202, (
            f"DELETE /v1/profile/me returned HTTP {delete_resp.status_code}: "
            f"{delete_resp.text[:300]!r}"
        )
        execution_arn = delete_resp.json().get("executionArn")
        assert execution_arn

        # ------------------------------------------------------------------
        # Step 6: Poll until SUCCEEDED
        # ------------------------------------------------------------------
        final_status = _poll_deletion_status(
            http_requests,
            api_base=api_base,
            access_token=user_a["access_token"],
            edge_secret=edge_secret,
            execution_arn=execution_arn,
        )
        assert final_status == "SUCCEEDED", (
            f"Expected SUCCEEDED, got {final_status!r}. executionArn: {execution_arn!r}"
        )

        # ------------------------------------------------------------------
        # Step 7: Hard-purge A via 9.11 Lambda directly (bypasses 30-day window)
        # ------------------------------------------------------------------
        purge_result = _invoke_hard_purge_lambda(
            boto3,
            region=region,
            function_name=hard_purge_function,
            user_id=a_id,
        )
        assert purge_result.get("mode") == "per_user", (
            f"Expected hard_purge to run in per_user mode, got {purge_result!r}"
        )
        assert purge_result.get("rows_affected") == 1, (
            f"Expected hard_purge to delete 1 Aurora row for A, "
            f"got rows_affected={purge_result.get('rows_affected')!r}. "
            f"Full result: {purge_result!r}"
        )

        # Verify the Aurora row is gone
        aurora_row = _get_aurora_user_row(aurora, a_id)
        assert aurora_row is None, (
            f"Aurora users row for A must be gone after hard-purge, "
            f"but row still exists: {aurora_row!r}"
        )

        # ------------------------------------------------------------------
        # Assertion: B's deck query returns zero rows attributable to A
        #
        # After hard-purge, A's users row is gone.  deck_view joins to users
        # so A cannot appear.  We request the deck as B and assert no result
        # carries user_id == a_id.
        # ------------------------------------------------------------------
        deck_resp = http_requests.get(
            _api_url(api_base, "/v1/match/deck"),
            headers=_authed_headers(user_b["access_token"], edge_secret),
            timeout=20,
        )
        assert deck_resp.status_code == 200, (
            f"GET /v1/match/deck for B returned HTTP {deck_resp.status_code}: "
            f"{deck_resp.text[:300]!r}"
        )
        deck_body = deck_resp.json()
        deck_users = deck_body.get("users") or deck_body.get("candidates") or deck_body.get("results") or []
        a_in_deck = [u for u in deck_users if u.get("user_id") == a_id or u.get("userId") == a_id]
        assert len(a_in_deck) == 0, (
            f"After hard-purge of A, B's deck must contain zero rows for A, "
            f"found {len(a_in_deck)}: {a_in_deck!r}"
        )

        # ------------------------------------------------------------------
        # Assertion: B's message history on the deactivated room shows
        # anonymized messages with sender_id='[deleted-user]'
        # ------------------------------------------------------------------
        room_msgs = _query_chat_messages(ddb, room_id)
        # All messages from A should be anonymized (soft-delete path)
        a_still_attributed = [
            m for m in room_msgs if m.get("sender_id", {}).get("S") == a_id
        ]
        assert len(a_still_attributed) == 0, (
            f"No messages should still carry A's user_id after anonymization, "
            f"found {len(a_still_attributed)}"
        )
        anonymized = [
            m for m in room_msgs if m.get("sender_id", {}).get("S") == "[deleted-user]"
        ]
        assert len(anonymized) >= 1, (
            f"Expected at least 1 anonymized message with sender_id='[deleted-user]' "
            f"in the deactivated room, found {len(anonymized)}. "
            f"All messages: {room_msgs!r}"
        )

    finally:
        a_id_td = (user_a or {}).get("sub")
        b_id_td = (user_b or {}).get("sub")

        if a_id_td and b_id_td:
            room_id_td = _chat_room_id(a_id_td, b_id_td)
            _delete_all_chat_messages(ddb, room_id_td)
            _delete_membership(ddb, a_id_td, room_id_td)
            _delete_membership(ddb, b_id_td, room_id_td)
            _delete_chat_room(ddb, room_id_td)
            _delete_audit_rows(ddb, a_id_td)
            # Clean up the block row (B blocked A)
            _delete_blocks_aurora(aurora, a_id_td, b_id_td)

        if user_a:
            # Aurora row may already be gone (hard-purged above); no-op if absent
            _delete_aurora_user(aurora, user_a["sub"])
            _teardown_cognito_user(cognito, user_pool_id, user_a["email"])

        if user_b:
            _delete_aurora_user(aurora, user_b["sub"])
            _teardown_cognito_user(cognito, user_pool_id, user_b["email"])

        try:
            aurora.close()
        except Exception:
            pass


# ===========================================================================
# Sub-test 3: purge_immediately variant
# ===========================================================================


def test_e2e_purge_immediately():  # noqa: C901 (flat sequential by design)
    """
    purge_immediately=true variant — story 9.13 AC (purge_immediately block).

    Signs up users C and D, exchanges messages, then C calls DELETE with
    purge_immediately=true.  Asserts:
      - C's ChatMessages rows are GONE (query the shared room returns only
        D's messages).
      - The audit row has dynamodb_retention='hard_deleted'.
    """
    _check_run_gate()
    _check_all_env_vars()

    boto3 = _boto3()
    http_requests = _requests()

    user_pool_id = _require_env("COGNITO_USER_POOL_ID")
    client_id = _require_env("COGNITO_APP_CLIENT_ID")
    api_base = _require_env("API_BASE_URL").rstrip("/")
    graphql_url = _require_env("APPSYNC_GRAPHQL_URL")
    region = _require_env("AWS_REGION")
    aurora_cluster_arn = _require_env("AURORA_CLUSTER_ARN")
    aurora_dbname = _require_env("AURORA_DBNAME")
    master_secret_arn = _require_env("AURORA_MASTER_SECRET_ARN")
    edge_secret = _require_env("EDGE_SECRET")

    cognito = boto3.client("cognito-idp", region_name=region)
    ddb = _ddb_client(region)

    aurora = _AuroraDataApi(
        client=_rds_data_client(region),
        cluster_arn=aurora_cluster_arn,
        secret_arn=master_secret_arn,
        database=aurora_dbname,
    )

    user_c: dict | None = None
    user_d: dict | None = None

    try:
        # ------------------------------------------------------------------
        # Step 1: Provision users C (deleter) and D (survivor)
        # ------------------------------------------------------------------
        user_c = _mint_completed_user(
            cognito, http_requests,
            user_pool_id=user_pool_id, client_id=client_id,
            api_base=api_base, edge_secret=edge_secret,
            sex="Male",
        )
        user_d = _mint_completed_user(
            cognito, http_requests,
            user_pool_id=user_pool_id, client_id=client_id,
            api_base=api_base, edge_secret=edge_secret,
            sex="Female",
        )
        c_id = user_c["sub"]
        d_id = user_d["sub"]
        room_id = _chat_room_id(c_id, d_id)

        # ------------------------------------------------------------------
        # Step 2: Make C and D friends
        # ------------------------------------------------------------------
        _make_friends(
            http_requests,
            requester=user_c,
            target=user_d,
            api_base=api_base,
            edge_secret=edge_secret,
        )

        # ------------------------------------------------------------------
        # Step 3: Create the chat room and exchange messages
        # ------------------------------------------------------------------
        room_resp = _appsync_mutation(
            graphql_url, user_c["id_token"],
            _GQL_CREATE_OR_GET_ROOM,
            {"otherUserId": d_id},
            http_requests,
        )
        assert "errors" not in room_resp, (
            f"createOrGetRoom returned errors: {room_resp.get('errors')!r}"
        )

        # C sends 2 messages, D sends 1 message
        c_messages = ["c first", "c second"]
        for content in c_messages:
            send = _appsync_mutation(
                graphql_url, user_c["id_token"],
                _GQL_SEND_MESSAGE,
                {"roomId": room_id, "content": content, "contentType": "text"},
                http_requests,
            )
            assert "errors" not in send or not send.get("errors"), (
                f"C sendMessage({content!r}) errors: {send.get('errors')!r}"
            )

        d_message = "d message"
        d_send = _appsync_mutation(
            graphql_url, user_d["id_token"],
            _GQL_SEND_MESSAGE,
            {"roomId": room_id, "content": d_message, "contentType": "text"},
            http_requests,
        )
        assert "errors" not in d_send or not d_send.get("errors"), (
            f"D sendMessage({d_message!r}) errors: {d_send.get('errors')!r}"
        )

        # Verify pre-delete message counts
        pre_msgs = _query_chat_messages(ddb, room_id)
        c_msgs_pre = [m for m in pre_msgs if m.get("sender_id", {}).get("S") == c_id]
        d_msgs_pre = [m for m in pre_msgs if m.get("sender_id", {}).get("S") == d_id]
        assert len(c_msgs_pre) == 2, f"Expected 2 messages from C before deletion, found {len(c_msgs_pre)}"
        assert len(d_msgs_pre) == 1, f"Expected 1 message from D before deletion, found {len(d_msgs_pre)}"

        # ------------------------------------------------------------------
        # Step 4: C calls DELETE /v1/profile/me with purge_immediately=true
        # ------------------------------------------------------------------
        delete_resp = http_requests.delete(
            _api_url(api_base, "/v1/profile/me"),
            json={"purge_immediately": True},
            headers=_authed_headers(user_c["access_token"], edge_secret),
            timeout=20,
        )
        assert delete_resp.status_code == 202, (
            f"DELETE /v1/profile/me (purge_immediately=true) returned HTTP "
            f"{delete_resp.status_code}: {delete_resp.text[:300]!r}"
        )
        execution_arn = delete_resp.json().get("executionArn")
        assert execution_arn

        # ------------------------------------------------------------------
        # Step 5: Poll until SUCCEEDED
        # ------------------------------------------------------------------
        final_status = _poll_deletion_status(
            http_requests,
            api_base=api_base,
            access_token=user_c["access_token"],
            edge_secret=edge_secret,
            execution_arn=execution_arn,
        )
        assert final_status == "SUCCEEDED", (
            f"Expected SUCCEEDED for purge_immediately execution, got {final_status!r}. "
            f"executionArn: {execution_arn!r}"
        )

        # ------------------------------------------------------------------
        # Assertion: C's ChatMessages rows are GONE
        # ------------------------------------------------------------------
        post_msgs = _query_chat_messages(ddb, room_id)
        c_msgs_post = [m for m in post_msgs if m.get("sender_id", {}).get("S") == c_id]
        assert len(c_msgs_post) == 0, (
            f"Expected 0 ChatMessages rows from C after purge_immediately=true, "
            f"found {len(c_msgs_post)}: {c_msgs_post!r}"
        )
        # Messages from C must not be anonymized either — they must be GONE
        anon_msgs = [m for m in post_msgs if m.get("sender_id", {}).get("S") == "[deleted-user]"]
        assert len(anon_msgs) == 0, (
            f"purge_immediately path must hard-delete messages, not anonymize them. "
            f"Found {len(anon_msgs)} anonymized messages: {anon_msgs!r}"
        )

        # ------------------------------------------------------------------
        # Assertion: Only D's message remains in the room
        # ------------------------------------------------------------------
        d_msgs_post = [m for m in post_msgs if m.get("sender_id", {}).get("S") == d_id]
        assert len(d_msgs_post) == 1, (
            f"Expected D's 1 message to survive purge_immediately, "
            f"found {len(d_msgs_post)}: {d_msgs_post!r}"
        )
        assert d_msgs_post[0].get("content", {}).get("S") == d_message, (
            f"D's message content must be preserved, "
            f"got {d_msgs_post[0].get('content')!r}"
        )

        # ------------------------------------------------------------------
        # Assertion: Audit row has dynamodb_retention='hard_deleted'
        # ------------------------------------------------------------------
        audit_rows = _query_audit_log(ddb, c_id)
        completed_rows = [
            r for r in audit_rows
            if r.get("event_type", {}).get("S") == "deletion_completed"
        ]
        assert len(completed_rows) >= 1, (
            f"Expected at least one 'deletion_completed' audit row for C, "
            f"found {len(completed_rows)}. All audit rows: {audit_rows!r}"
        )
        completed_row = completed_rows[0]
        dynamodb_retention = completed_row.get("dynamodb_retention", {}).get("S")
        assert dynamodb_retention == "hard_deleted", (
            f"audit 'deletion_completed' row must have dynamodb_retention='hard_deleted' "
            f"for purge_immediately=true, got {dynamodb_retention!r}"
        )

        # ------------------------------------------------------------------
        # Assertion: C's Aurora row is gone (hard_purge ran in the workflow)
        # ------------------------------------------------------------------
        c_aurora_row = _get_aurora_user_row(aurora, c_id)
        assert c_aurora_row is None, (
            f"Aurora users row for C must be gone after purge_immediately=true, "
            f"but row still exists: {c_aurora_row!r}"
        )

    finally:
        c_id_td = (user_c or {}).get("sub")
        d_id_td = (user_d or {}).get("sub")

        if c_id_td and d_id_td:
            room_id_td = _chat_room_id(c_id_td, d_id_td)
            _delete_all_chat_messages(ddb, room_id_td)
            _delete_membership(ddb, c_id_td, room_id_td)
            _delete_membership(ddb, d_id_td, room_id_td)
            _delete_chat_room(ddb, room_id_td)
            _delete_audit_rows(ddb, c_id_td)

        if user_c:
            # C's Aurora row is expected to be gone (hard-purged by workflow)
            _delete_aurora_user(aurora, user_c["sub"])
            _teardown_cognito_user(cognito, user_pool_id, user_c["email"])

        if user_d:
            _delete_aurora_user(aurora, user_d["sub"])
            _teardown_cognito_user(cognito, user_pool_id, user_d["email"])

        try:
            aurora.close()
        except Exception:
            pass
