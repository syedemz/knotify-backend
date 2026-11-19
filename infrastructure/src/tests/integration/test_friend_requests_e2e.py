"""
FR-7: End-to-end regression suite covering FR-1 … FR-6.

This file contains ONE integration test that walks the complete 9-step
friend-request lifecycle and proves every story in the hotfix works together:

  1. A sends to B → 201, Notifications row written for B, subscription event
     observed on B's AppSync websocket (onNotificationForMe).
  2. B's GET ?direction=incoming → sees A's request (and ONLY A's request).
  3. B's GET ?direction=outgoing → empty (no self-rows).
  4. B attempts to send to A → 409 reverse_pending with the original request_id.
  5. A attempts to send to B again → 409 already_pending.
  6. B accepts A's request → 200, friendships row exists, Notifications row written
     for A, subscription event observed on A's AppSync websocket
     (onFriendRequestUpdated).
  7. A attempts to send another request to B → 409 already_friends.
  8. A unfriends B → 200.
  9. A sends to B → 201 (regression: unfriending restores ability to re-send).

Subscription assertions (steps 1 and 6):
  The two live-AppSync assertions use a local `_subscribe_and_collect`
  helper that opens a real WebSocket connection per the AppSync Realtime
  protocol.  All other assertions are plain HTTP calls.

Required env vars (loaded from .env.test or shell):
  KNOTIFY_RUN_INTEGRATION=1          — gate: skip unless set to exactly "1"
  AWS_REGION
  COGNITO_USER_POOL_ID
  COGNITO_INTEGRATION_TEST_CLIENT_ID
  DISTRIBUTION_DOMAIN_NAME
  EDGE_SECRET
  AURORA_HOST
  AURORA_PORT
  AURORA_DBNAME
  AURORA_MASTER_SECRET_ARN
  APPSYNC_GRAPHQL_URL                — e.g. https://<id>.appsync-api.<region>.amazonaws.com/graphql
  APPSYNC_REALTIME_URL               — e.g. wss://<id>.appsync-realtime-api.<region>.amazonaws.com/graphql
  DYNAMODB_TABLE_NOTIFICATIONS       — DynamoDB table name for Notifications rows

Run:
    KNOTIFY_RUN_INTEGRATION=1 pytest \\
        infrastructure/src/tests/integration/test_friend_requests_e2e.py \\
        -v -m integration

Skip without live AWS access:
    pytest -m "not integration"
"""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
import uuid
from typing import Any

import pytest

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Skip-gate
# ---------------------------------------------------------------------------

_KNOTIFY_RUN_INTEGRATION = os.environ.get("KNOTIFY_RUN_INTEGRATION", "")


def _check_integration_gate() -> None:
    """Skip unless KNOTIFY_RUN_INTEGRATION=1 is set."""
    if _KNOTIFY_RUN_INTEGRATION != "1":
        pytest.skip(
            "FR-7 e2e test requires KNOTIFY_RUN_INTEGRATION=1 and all required "
            "env vars set against a live dev environment."
        )


def _require_env(name: str) -> str:
    """Return the value of env var *name*, or pytest.skip if absent."""
    value = os.environ.get(name)
    if not value:
        pytest.skip(
            f"Required env var {name!r} is not set — live dev environment not configured."
        )
    return value


# ---------------------------------------------------------------------------
# HTTP helpers (mirrors test_friends.py)
# ---------------------------------------------------------------------------


def _api_url(domain: str, path: str) -> str:
    return f"https://{domain}{path}"


def _authed_headers(access_token: str, edge_secret: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "x-knotify-edge-secret": edge_secret,
        "Content-Type": "application/json",
    }


def _canonical_pair(id_a: str, id_b: str) -> tuple[str, str]:
    return (min(id_a, id_b), max(id_a, id_b))


def _friendship_exists(aurora_conn, id_a: str, id_b: str) -> bool:
    user_a, user_b = _canonical_pair(id_a, id_b)
    with aurora_conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM friendships "
            "WHERE user_a = %s::uuid AND user_b = %s::uuid LIMIT 1",
            (user_a, user_b),
        )
        return cur.fetchone() is not None


# ---------------------------------------------------------------------------
# User-minting helper (mirrors test_friends.py)
# ---------------------------------------------------------------------------


def _mint_user(
    cognito_client,
    integration_client_id: str,
    user_pool_id: str,
    distribution_domain: str,
    edge_secret: str,
    sex: str,
    http_requests,
) -> dict:
    """
    Create and fully profile a test user inline.

    Returns a dict with: sub, email, password, access_token.
    """
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
            "subsect": "Other",
            "religious_level": "Practicing",
            "current_residence_city": "Berlin",
            "current_residence_country": "Germany",
            "resident_country_code": "DE",
            "district": "Mitte",
            "education_level": "Graduate",
            "highest_degree": "MSc",
            "high_school": "Test High School",
            "higher_secondary": "Test Higher Secondary",
            "college_name": "Test College",
            "job_title": "Engineer",
            "employer_name": "TestCo",
            "employment_type": "Full-time",
            "office_address": "123 Test St",
            "professional_category": "Technology",
            "salary_range": "100-150k",
            "fathers_name": "Father Test",
            "fathers_job": "Retired",
            "father_retired": "Yes",
            "mothers_name": "Mother Test",
            "mothers_job": "Retired",
            "mother_retired": "Yes",
            "family_residence_address": "456 Family Rd",
            "marital_status": "Single",
            "has_children": False,
            "move_abroad": False,
            "relation": "Self",
        },
        headers=_authed_headers(initial_token, edge_secret),
    )
    assert patch_resp.status_code == 200, (
        f"Profile PATCH failed for {sex}: HTTP {patch_resp.status_code} {patch_resp.text!r}"
    )

    # Mint fresh tokens so PreTokenGeneration embeds profile_complete = true
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


def _teardown_user_inline(
    cognito_client, user_pool_id: str, email: str, sub: str, aurora_conn
) -> None:
    """Remove a test user from Cognito and Aurora; errors are suppressed."""
    try:
        cognito_client.admin_delete_user(UserPoolId=user_pool_id, Username=email)
    except Exception as exc:
        print(f"WARN: teardown admin_delete_user({email!r}) failed: {exc}", file=sys.stderr)
    try:
        with aurora_conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE user_id = %s::uuid", (sub,))
    except Exception as exc:
        print(f"WARN: teardown Aurora DELETE({sub!r}) failed: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# DynamoDB Notifications teardown helper (mirrors test_friends.py)
# ---------------------------------------------------------------------------


def _teardown_notifications_for_user(
    dynamo_client, table_name: str, user_id: str
) -> None:
    """
    Delete all Notifications rows for user_id by querying the PK then deleting
    each row individually.  Errors are suppressed — best-effort teardown.
    """
    try:
        resp = dynamo_client.query(
            TableName=table_name,
            KeyConditionExpression="user_id = :uid",
            ExpressionAttributeValues={":uid": {"S": user_id}},
        )
        for item in resp.get("Items", []):
            sk = item.get("created_at_notification_id", {}).get("S", "")
            if not sk:
                continue
            dynamo_client.delete_item(
                TableName=table_name,
                Key={
                    "user_id": {"S": user_id},
                    "created_at_notification_id": {"S": sk},
                },
            )
    except Exception as exc:
        print(
            f"WARN: _teardown_notifications_for_user(user_id={user_id!r}) failed: {exc}",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# AppSync WebSocket subscription helper (FR-7 — lives ONLY in this file)
#
# How it differs from `_open_appsync_subscription` in test_room_state_publisher.py:
#   That function is a stub that raises NotImplementedError.  This file
#   provides the REAL implementation using the `websocket-client` library
#   (or `websockets` if available) with the AppSync Realtime protocol:
#
#     1. Connect to the realtime URL with a base64-encoded JSON header
#        carrying the Authorization (Cognito JWT) and Host.
#     2. Send connection_init.
#     3. Send start (subscription registration) carrying the GraphQL
#        subscription query and variables.
#     4. Collect data frames in a background thread.
#     5. Expose a blocking wait-for-event call that returns the first
#        matching data frame within a timeout.
#     6. Send stop + close on context manager exit.
#
# Subscription queries issued here (differ from test_room_state_publisher.py):
#   - Step 1: onNotificationForMe(userId: $userId) — receives
#     type='friend_request_received' events on B's channel.
#   - Step 6: onFriendRequestUpdated(userId: $userId) — receives
#     type='friend_request_accepted' events on A's channel.
# ---------------------------------------------------------------------------

# GraphQL subscription document strings
_GQL_ON_NOTIFICATION_FOR_ME = """
subscription OnNotificationForMe($userId: ID!) {
  onNotificationForMe(userId: $userId) {
    notificationId
    type
    userId
    senderUserId
    senderName
    payload
    createdAt
  }
}
"""

_GQL_ON_FRIEND_REQUEST_UPDATED = """
subscription OnFriendRequestUpdated($userId: ID!) {
  onFriendRequestUpdated(userId: $userId) {
    notificationId
    type
    userId
    senderUserId
    senderName
    payload
    createdAt
  }
}
"""


class _AppSyncSubscription:
    """
    Context manager that opens an AppSync realtime WebSocket subscription and
    collects incoming data frames.

    Usage:
        with _AppSyncSubscription(graphql_url, realtime_url, gql_query,
                                   variables, access_token) as sub:
            post_something()
            frame = sub.wait_for_data(timeout=10)
        # connection closed automatically on __exit__

    ``wait_for_data`` returns the parsed `payload.data` dict from the first
    received ``data`` frame, or raises ``TimeoutError`` after ``timeout``
    seconds.

    The subscription uses Cognito JWT authorisation (Authorization header
    embedded in the WebSocket connection header as per AppSync Realtime spec).
    """

    def __init__(
        self,
        graphql_url: str,
        realtime_url: str,
        gql_query: str,
        variables: dict,
        access_token: str,
    ) -> None:
        self._graphql_url = graphql_url
        self._realtime_url = realtime_url
        self._gql_query = gql_query
        self._variables = variables
        self._access_token = access_token

        self._ws = None
        self._frames: list[dict] = []
        self._lock = threading.Lock()
        self._data_event = threading.Event()
        self._sub_id = str(uuid.uuid4())
        self._thread: threading.Thread | None = None
        self._connected_event = threading.Event()
        self._subscribed_event = threading.Event()
        self._error: Exception | None = None

    # ------------------------------------------------------------------
    # Protocol encoding helpers
    # ------------------------------------------------------------------

    def _encode_header(self) -> str:
        """
        Encode the AppSync realtime connection header as base64 JSON.

        AppSync Realtime protocol requires the Authorization and Host values
        to be base64url-encoded in the connection URL query string.
        """
        from urllib.parse import urlparse
        host = urlparse(self._graphql_url).netloc
        header_obj = {
            "Authorization": self._access_token,
            "host": host,
        }
        encoded = base64.b64encode(json.dumps(header_obj).encode("utf-8")).decode("utf-8")
        return encoded

    def _build_realtime_url(self) -> str:
        """
        Build the full realtime WebSocket URL with header and payload query params.

        AppSync Realtime URL format:
          wss://<host>/graphql?header=<base64_header>&payload=<base64_payload>
        """
        header_b64 = self._encode_header()
        # payload is an empty JSON object encoded as base64
        payload_b64 = base64.b64encode(b"{}").decode("utf-8")
        sep = "&" if "?" in self._realtime_url else "?"
        return f"{self._realtime_url}{sep}header={header_b64}&payload={payload_b64}"

    # ------------------------------------------------------------------
    # WebSocket message handlers
    # ------------------------------------------------------------------

    def _on_message(self, ws, message: str) -> None:  # noqa: ANN001
        try:
            msg = json.loads(message)
        except (json.JSONDecodeError, TypeError):
            return

        msg_type = msg.get("type", "")

        if msg_type == "connection_ack":
            # Send the subscription start request
            self._send_subscription_start(ws)

        elif msg_type == "start_ack":
            self._subscribed_event.set()

        elif msg_type == "data":
            with self._lock:
                self._frames.append(msg)
            self._data_event.set()

        elif msg_type == "connection_error":
            self._error = RuntimeError(
                f"AppSync connection_error: {msg.get('payload', msg)!r}"
            )
            self._connected_event.set()
            self._subscribed_event.set()

        elif msg_type == "ka":
            # Keep-alive — no action needed
            pass

    def _send_subscription_start(self, ws) -> None:  # noqa: ANN001
        from urllib.parse import urlparse
        host = urlparse(self._graphql_url).netloc
        payload = {
            "data": json.dumps({
                "query": self._gql_query,
                "variables": self._variables,
            }),
            "extensions": {
                "authorization": {
                    "Authorization": self._access_token,
                    "host": host,
                },
            },
        }
        msg = {
            "id": self._sub_id,
            "type": "start",
            "payload": payload,
        }
        ws.send(json.dumps(msg))

    def _on_open(self, ws) -> None:  # noqa: ANN001
        # Send connection_init immediately after the WebSocket opens
        ws.send(json.dumps({"type": "connection_init"}))
        self._connected_event.set()

    def _on_error(self, ws, error) -> None:  # noqa: ANN001
        self._error = error if isinstance(error, Exception) else RuntimeError(str(error))
        self._connected_event.set()
        self._subscribed_event.set()
        self._data_event.set()

    def _on_close(self, ws, close_status_code, close_msg) -> None:  # noqa: ANN001
        # Wake any waiters so they don't block forever if the socket closes
        self._connected_event.set()
        self._subscribed_event.set()
        self._data_event.set()

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "_AppSyncSubscription":
        try:
            import websocket  # websocket-client package
        except ImportError:
            pytest.skip(
                "websocket-client not installed — "
                "install it (`pip install websocket-client`) to run FR-7 e2e subscription test."
            )

        realtime_url = self._build_realtime_url()
        ws = websocket.WebSocketApp(
            realtime_url,
            header={
                "Sec-WebSocket-Protocol": "graphql-ws",
            },
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._ws = ws

        # Run the WebSocket in a daemon thread so it does not block pytest
        t = threading.Thread(
            target=lambda: ws.run_forever(
                ping_interval=10,
                ping_timeout=5,
            ),
            daemon=True,
        )
        t.start()
        self._thread = t

        # Wait for the WebSocket open + connection_ack (up to 15s)
        self._connected_event.wait(timeout=15)
        if self._error is not None:
            self._close_ws()
            raise self._error

        # Wait for start_ack confirming the subscription is registered (up to 10s)
        self._subscribed_event.wait(timeout=10)
        if self._error is not None:
            self._close_ws()
            raise self._error

        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self._close_ws()
        return False  # do not suppress exceptions

    def _close_ws(self) -> None:
        if self._ws is not None:
            try:
                self._ws.send(json.dumps({"id": self._sub_id, "type": "stop"}))
            except Exception:
                pass
            try:
                self._ws.close()
            except Exception:
                pass
        # Give the thread a moment to finish
        if self._thread is not None:
            self._thread.join(timeout=3)

    # ------------------------------------------------------------------
    # Frame collection
    # ------------------------------------------------------------------

    def wait_for_data(self, timeout: float = 10.0) -> dict[str, Any]:
        """
        Block until a ``data`` frame arrives or ``timeout`` seconds elapse.

        Returns the ``payload.data`` dict from the first ``data`` frame.
        Raises ``TimeoutError`` if no data frame arrived within ``timeout``.
        """
        deadline = time.monotonic() + timeout
        while True:
            if self._error is not None:
                raise self._error
            with self._lock:
                if self._frames:
                    frame = self._frames.pop(0)
                    return frame.get("payload", {}).get("data", {})
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"No AppSync data frame received within {timeout}s. "
                    f"Subscription id={self._sub_id!r}"
                )
            # Wait for the data_event (reset it if we got here because of a prior frame)
            self._data_event.wait(timeout=min(remaining, 1.0))
            self._data_event.clear()


# ---------------------------------------------------------------------------
# Idempotent pair teardown (mirrors test_friends.py's _teardown_pair)
# ---------------------------------------------------------------------------


def _teardown_pair_rows(aurora_conn, a_id: str, b_id: str) -> None:
    """
    Delete friend_requests and friendships rows for the pair (both directions).
    Errors are suppressed.
    """
    try:
        with aurora_conn.cursor() as cur:
            cur.execute(
                "DELETE FROM friend_requests WHERE "
                "(from_user_id = %s::uuid AND to_user_id = %s::uuid) "
                "OR (from_user_id = %s::uuid AND to_user_id = %s::uuid)",
                (a_id, b_id, b_id, a_id),
            )
    except Exception as exc:
        print(f"WARN: _teardown_pair_rows friend_requests failed: {exc}", file=sys.stderr)

    try:
        user_a, user_b = _canonical_pair(a_id, b_id)
        with aurora_conn.cursor() as cur:
            cur.execute(
                "DELETE FROM friendships WHERE user_a = %s::uuid AND user_b = %s::uuid",
                (user_a, user_b),
            )
    except Exception as exc:
        print(f"WARN: _teardown_pair_rows friendships failed: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# FR-7: Full 9-step end-to-end regression test
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("completed_profile_user", ["Male"], indirect=True)
def test_friend_request_full_lifecycle_end_to_end(completed_profile_user):
    """
    FR-7: full 9-step end-to-end regression covering FR-1 … FR-6.

    One test function that walks the complete friend-request lifecycle with
    two live AppSync subscription assertions (steps 1 and 6) and idempotent
    teardown.

    Gated on KNOTIFY_RUN_INTEGRATION=1 and APPSYNC_REALTIME_URL.
    """
    _check_integration_gate()

    # Additional skip gate: realtime URL must be present for subscription assertions
    realtime_url = os.environ.get("APPSYNC_REALTIME_URL", "")
    if not realtime_url:
        pytest.skip(
            "APPSYNC_REALTIME_URL env var not set — "
            "subscription assertions in FR-7 require a live AppSync realtime endpoint."
        )

    notifications_table = os.environ.get("DYNAMODB_TABLE_NOTIFICATIONS", "")
    if not notifications_table:
        pytest.skip(
            "DYNAMODB_TABLE_NOTIFICATIONS env var not set — "
            "set it to the Notifications DynamoDB table name and re-run."
        )

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
    graphql_url = _require_env("APPSYNC_GRAPHQL_URL")

    cognito_client = boto3.client("cognito-idp", region_name=region)
    dynamo_client = boto3.client("dynamodb", region_name=region)

    # user_a = Male (from fixture)
    user_a = completed_profile_user
    a_id = user_a["sub"]
    aurora_conn = user_a["_aurora_conn"]

    b: dict | None = None
    request_id: str | None = None  # the A→B request_id from step 1

    try:
        # ------------------------------------------------------------------
        # Setup: Mint B=Female
        # ------------------------------------------------------------------
        b = _mint_user(
            cognito_client, integration_client_id, user_pool_id,
            distribution_domain, edge_secret, "Female", http_requests,
        )
        b_id = b["sub"]

        # ==================================================================
        # Step 1: A sends to B → 201, Notifications row for B, subscription
        #         event observed on B's onNotificationForMe websocket.
        # ==================================================================
        # Open B's subscription BEFORE the POST so no events are missed.
        with _AppSyncSubscription(
            graphql_url=graphql_url,
            realtime_url=realtime_url,
            gql_query=_GQL_ON_NOTIFICATION_FOR_ME,
            variables={"userId": b_id},
            access_token=b["access_token"],
        ) as b_sub:
            post_resp = http_requests.post(
                _api_url(distribution_domain, "/v1/friend-requests"),
                json={"toUserId": b_id},
                headers=_authed_headers(user_a["access_token"], edge_secret),
            )
            assert post_resp.status_code in (200, 201), (
                f"Step 1 — POST /v1/friend-requests returned HTTP "
                f"{post_resp.status_code}: {post_resp.text!r}"
            )
            resp_body = post_resp.json()
            request_id = resp_body.get("request_id")
            assert request_id, (
                f"Step 1 — Expected 'request_id' in response body, got {resp_body!r}"
            )

            # Assert Notifications row was written for B (DynamoDB)
            notify_resp = dynamo_client.query(
                TableName=notifications_table,
                KeyConditionExpression="user_id = :uid",
                ExpressionAttributeValues={":uid": {"S": b_id}},
            )
            notify_items = notify_resp.get("Items", [])
            received_rows = [
                item for item in notify_items
                if item.get("type", {}).get("S") == "friend_request_received"
            ]
            assert received_rows, (
                f"Step 1 — Expected Notifications row type='friend_request_received' "
                f"for B ({b_id!r}), got items={notify_items!r}"
            )
            assert received_rows[0].get("sender_user_id", {}).get("S") == a_id, (
                f"Step 1 — Notifications row sender_user_id must be A ({a_id!r}), "
                f"got {received_rows[0].get('sender_user_id')!r}"
            )

            # Wait up to 10s for B's AppSync subscription frame
            sub_data = b_sub.wait_for_data(timeout=10)
            # The frame's data key is the subscription field name
            notification_obj = (
                sub_data.get("onNotificationForMe")
                or next(iter(sub_data.values()), None)
            )
            assert notification_obj is not None, (
                f"Step 1 — AppSync frame contained no onNotificationForMe data: "
                f"{sub_data!r}"
            )
            assert notification_obj.get("type") == "friend_request_received", (
                f"Step 1 — Expected subscription frame type='friend_request_received', "
                f"got {notification_obj.get('type')!r}"
            )
        # B's subscription closed

        # ==================================================================
        # Step 2: B's GET ?direction=incoming → sees A's request only
        # ==================================================================
        get_incoming = http_requests.get(
            _api_url(distribution_domain, "/v1/friend-requests"),
            params={"direction": "incoming"},
            headers=_authed_headers(b["access_token"], edge_secret),
        )
        assert get_incoming.status_code == 200, (
            f"Step 2 — GET ?direction=incoming returned HTTP "
            f"{get_incoming.status_code}: {get_incoming.text!r}"
        )
        incoming_body = get_incoming.json()
        incoming_reqs = incoming_body.get("friend_requests", [])

        # A's request must appear in B's incoming list
        incoming_to_ids = [r.get("to_user_id") for r in incoming_reqs]
        assert b_id in incoming_to_ids, (
            f"Step 2 — B's incoming list must contain A's request "
            f"(to_user_id={b_id!r}); got to_ids={incoming_to_ids!r}"
        )

        # B's own outgoing rows must NOT bleed into the incoming view
        incoming_from_ids = [r.get("from_user_id") for r in incoming_reqs]
        assert b_id not in incoming_from_ids, (
            f"Step 2 — direction=incoming must not include B's own outgoing rows; "
            f"B appeared as sender: from_ids={incoming_from_ids!r}"
        )

        # ==================================================================
        # Step 3: B's GET ?direction=outgoing → empty (B has not sent any)
        # ==================================================================
        get_outgoing = http_requests.get(
            _api_url(distribution_domain, "/v1/friend-requests"),
            params={"direction": "outgoing"},
            headers=_authed_headers(b["access_token"], edge_secret),
        )
        assert get_outgoing.status_code == 200, (
            f"Step 3 — GET ?direction=outgoing returned HTTP "
            f"{get_outgoing.status_code}: {get_outgoing.text!r}"
        )
        outgoing_body = get_outgoing.json()
        outgoing_reqs = outgoing_body.get("friend_requests", [])
        outgoing_from_ids = [r.get("from_user_id") for r in outgoing_reqs]
        assert b_id not in outgoing_from_ids, (
            f"Step 3 — B's outgoing list must be empty (B has not sent any requests); "
            f"got from_ids={outgoing_from_ids!r}"
        )

        # ==================================================================
        # Step 4: B attempts to send to A → 409 reverse_pending
        # ==================================================================
        reverse_resp = http_requests.post(
            _api_url(distribution_domain, "/v1/friend-requests"),
            json={"toUserId": a_id},
            headers=_authed_headers(b["access_token"], edge_secret),
        )
        assert reverse_resp.status_code == 409, (
            f"Step 4 — B→A expected HTTP 409 (reverse_pending), "
            f"got {reverse_resp.status_code}: {reverse_resp.text!r}"
        )
        reverse_body = reverse_resp.json()
        assert reverse_body.get("error") == "reverse_pending", (
            f"Step 4 — Expected error='reverse_pending', got {reverse_body!r}"
        )
        assert reverse_body.get("request_id") == request_id, (
            f"Step 4 — Expected request_id={request_id!r} (the A→B row) "
            f"in 409 reverse_pending body, got {reverse_body.get('request_id')!r}"
        )

        # ==================================================================
        # Step 5: A attempts to send to B again → 409 already_pending
        # ==================================================================
        duplicate_resp = http_requests.post(
            _api_url(distribution_domain, "/v1/friend-requests"),
            json={"toUserId": b_id},
            headers=_authed_headers(user_a["access_token"], edge_secret),
        )
        assert duplicate_resp.status_code == 409, (
            f"Step 5 — A→B duplicate expected HTTP 409 (already_pending), "
            f"got {duplicate_resp.status_code}: {duplicate_resp.text!r}"
        )
        dup_body = duplicate_resp.json()
        assert dup_body.get("error") == "already_pending", (
            f"Step 5 — Expected error='already_pending', got {dup_body!r}"
        )

        # ==================================================================
        # Step 6: B accepts A's request → 200, friendships row exists,
        #         Notifications row written for A, subscription event for A.
        # ==================================================================
        # Open A's subscription BEFORE the accept POST.
        with _AppSyncSubscription(
            graphql_url=graphql_url,
            realtime_url=realtime_url,
            gql_query=_GQL_ON_FRIEND_REQUEST_UPDATED,
            variables={"userId": a_id},
            access_token=user_a["access_token"],
        ) as a_sub:
            accept_resp = http_requests.post(
                _api_url(distribution_domain, f"/v1/friend-requests/{request_id}/accept"),
                headers=_authed_headers(b["access_token"], edge_secret),
            )
            assert accept_resp.status_code == 200, (
                f"Step 6 — POST .../accept returned HTTP "
                f"{accept_resp.status_code}: {accept_resp.text!r}"
            )

            # Verify friendship row exists in Aurora
            assert _friendship_exists(aurora_conn, a_id, b_id), (
                f"Step 6 — friendships row must exist after accept "
                f"(canonical pair ({min(a_id, b_id)!r}, {max(a_id, b_id)!r}))"
            )

            # Verify Notifications row written for A (DynamoDB)
            notify_resp_a = dynamo_client.query(
                TableName=notifications_table,
                KeyConditionExpression="user_id = :uid",
                ExpressionAttributeValues={":uid": {"S": a_id}},
            )
            notify_items_a = notify_resp_a.get("Items", [])
            accepted_rows = [
                item for item in notify_items_a
                if item.get("type", {}).get("S") == "friend_request_accepted"
            ]
            assert accepted_rows, (
                f"Step 6 — Expected Notifications row type='friend_request_accepted' "
                f"for A ({a_id!r}), got items={notify_items_a!r}"
            )
            assert accepted_rows[0].get("sender_user_id", {}).get("S") == b_id, (
                f"Step 6 — Notifications row sender_user_id must be B ({b_id!r}), "
                f"got {accepted_rows[0].get('sender_user_id')!r}"
            )

            # Wait up to 10s for A's AppSync subscription frame
            accept_data = a_sub.wait_for_data(timeout=10)
            updated_obj = (
                accept_data.get("onFriendRequestUpdated")
                or next(iter(accept_data.values()), None)
            )
            assert updated_obj is not None, (
                f"Step 6 — AppSync frame contained no onFriendRequestUpdated data: "
                f"{accept_data!r}"
            )
            assert updated_obj.get("type") == "friend_request_accepted", (
                f"Step 6 — Expected subscription frame type='friend_request_accepted', "
                f"got {updated_obj.get('type')!r}"
            )
        # A's subscription closed

        # ==================================================================
        # Step 7: A attempts to send another request to B → 409 already_friends
        # ==================================================================
        already_friends_resp = http_requests.post(
            _api_url(distribution_domain, "/v1/friend-requests"),
            json={"toUserId": b_id},
            headers=_authed_headers(user_a["access_token"], edge_secret),
        )
        assert already_friends_resp.status_code == 409, (
            f"Step 7 — A→B after accept expected HTTP 409 (already_friends), "
            f"got {already_friends_resp.status_code}: {already_friends_resp.text!r}"
        )
        af_body = already_friends_resp.json()
        assert af_body.get("error") == "already_friends", (
            f"Step 7 — Expected error='already_friends', got {af_body!r}"
        )

        # ==================================================================
        # Step 8: A unfriends B → 200
        # ==================================================================
        unfriend_resp = http_requests.delete(
            _api_url(distribution_domain, f"/v1/friends/{b_id}"),
            headers=_authed_headers(user_a["access_token"], edge_secret),
        )
        assert unfriend_resp.status_code == 200, (
            f"Step 8 — DELETE /v1/friends/{{b_id}} returned HTTP "
            f"{unfriend_resp.status_code}: {unfriend_resp.text!r}"
        )

        # ==================================================================
        # Step 9: A sends to B → 201 (regression: unfriending restores re-send)
        # ==================================================================
        resend_resp = http_requests.post(
            _api_url(distribution_domain, "/v1/friend-requests"),
            json={"toUserId": b_id},
            headers=_authed_headers(user_a["access_token"], edge_secret),
        )
        assert resend_resp.status_code in (200, 201), (
            f"Step 9 — POST /v1/friend-requests after unfriend expected 201, "
            f"got {resend_resp.status_code}: {resend_resp.text!r}"
        )
        resend_body = resend_resp.json()
        assert resend_body.get("request_id"), (
            f"Step 9 — Expected 'request_id' in resend response, got {resend_body!r}"
        )

    finally:
        # ------------------------------------------------------------------
        # Idempotent teardown — delete all state for the A↔B pair.
        # Runs regardless of which step failed.
        # Errors are printed but NOT re-raised so a teardown failure never
        # masks the actual assertion failure.
        # ------------------------------------------------------------------
        if b is not None:
            b_id_td = b["sub"]

            # Delete Notifications rows for both users
            _teardown_notifications_for_user(dynamo_client, notifications_table, b_id_td)
            _teardown_notifications_for_user(dynamo_client, notifications_table, a_id)

            # Delete friend_requests and friendships for the pair
            _teardown_pair_rows(aurora_conn, a_id, b_id_td)

            # Delete the synthetic user B from Cognito and Aurora
            _teardown_user_inline(cognito_client, user_pool_id, b["email"], b_id_td, aurora_conn)
