"""
knotify-blocks Lambda handler.

Implements three HTTP API Gateway v2 routes:
  GET    /v1/blocks           — list all users the caller has blocked
  POST   /v1/blocks           — block a current friend (auto-unfriend + deactivate chat room)
  DELETE /v1/blocks/{userId}  — unblock a user (reactivate chat room if applicable)

All routes require a valid Cognito JWT (enforced by the HTTP API JWT authorizer)
and the x-knotify-edge-secret header (enforced by the @with_edge_secret decorator).

Design notes:
  - User identity comes from the JWT sub claim — never from URL path or request body.
  - POST /v1/blocks:
      1. Check friendship exists (friendship guard). Reject with 409 not_friends if absent.
      2. In a SINGLE Aurora transaction:
           INSERT INTO blocks (blocker_id, blocked_id, created_at)
           DELETE FROM friendships for the canonical pair
           DELETE FROM friend_requests between the pair in either direction
      3. AFTER the Aurora transaction COMMITS, call DynamoDB UpdateItem to deactivate
         the chat room. ConditionalCheckFailedException is a no-op (INFO). Any other
         DynamoDB error → HTTP 200 with chat_deactivation_pending: true. The Aurora
         block is never rolled back when DynamoDB fails.
  - DELETE /v1/blocks/{userId}:
      1. DELETE FROM blocks for the (blocker, blocked) pair in Aurora.
      2. AFTER commit, call DynamoDB UpdateItem to reactivate the chat room.
         Same ConditionalCheckFailedException no-op / other-error-with-flag contract.
         On DynamoDB reactivation failure the response also includes
         chat_deactivation_pending: true. The same flag name is used on both paths
         (POST deactivation lag and DELETE reactivation lag) — it is a generic
         "chat-room status update lagged, client should re-poll" signal. Clients
         only need one flag, and keeping the name symmetric simplifies client logic.

  - _get_user_id_and_sex falls back to "" when the JWT custom:user_sex claim is absent.
    rls_context accepts an empty string (it issues SET LOCAL, which never raises for
    string values). An empty user_sex means the RLS sex-filter evaluates to
    "sex != ''" (always true), which is overly permissive for cross-user reads,
    but blocks-specific writes (INSERT/DELETE on own rows) are not constrained by the
    sex-filter branch of the policy — only by the user_id equality check. An
    incomplete-profile user (no sex claim) has no business calling /v1/blocks;
    completed_profile_user always carries the sex claim.

  - module-level _conn and _dynamo are reused across warm invocations (cold-start
    friendly). _get_conn() / _get_dynamo() are separate functions so tests can patch them.

Dependencies (Lambda layers):
  - knotify_obs: init_logger, with_edge_secret, chat_room_id
  - knotify_db:  get_connection, rls_context
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

import knotify_db
from knotify_obs import chat_room_id, init_logger, require_profile_complete, with_edge_secret

# ---------------------------------------------------------------------------
# Module-level singletons (cold-start optimization)
# ---------------------------------------------------------------------------

logger = init_logger("knotify_blocks")

_DB_SECRET_NAME: str = os.environ.get("DB_SECRET_NAME", "")
_TABLE_CHAT_ROOMS: str = os.environ.get("TABLE_CHAT_ROOMS", "ChatRooms")

_conn = None
_dynamo = None


def _get_conn():
    """Return a (possibly cached) psycopg2 connection."""
    global _conn
    if _conn is None or _conn.closed:
        _conn = knotify_db.get_connection(_DB_SECRET_NAME)
    return _conn


def _get_dynamo():
    """Return a (possibly cached) boto3 DynamoDB client."""
    global _dynamo
    if _dynamo is None:
        import boto3  # deferred — not available in local test environments
        _dynamo = boto3.client("dynamodb")
    return _dynamo


# ---------------------------------------------------------------------------
# SQL statements
# ---------------------------------------------------------------------------

# Check if a friendship exists between two users (canonical pair ordering)
_SELECT_FRIENDSHIP_SQL = """
SELECT 1 FROM friendships
WHERE (user_a = %s::uuid AND user_b = %s::uuid)
   OR (user_a = %s::uuid AND user_b = %s::uuid)
LIMIT 1
"""

# Insert a block row
_INSERT_BLOCK_SQL = """
INSERT INTO blocks (blocker_id, blocked_id, created_at)
VALUES (%s::uuid, %s::uuid, NOW())
"""

# Delete the friendship for the canonical pair (both orderings covered)
_DELETE_FRIENDSHIP_SQL = """
DELETE FROM friendships
WHERE (user_a = %s::uuid AND user_b = %s::uuid)
   OR (user_a = %s::uuid AND user_b = %s::uuid)
"""

# Delete any pending friend_requests between the pair in either direction
_DELETE_FRIEND_REQUESTS_SQL = """
DELETE FROM friend_requests
WHERE (requester_id = %s::uuid AND receiver_id = %s::uuid)
   OR (requester_id = %s::uuid AND receiver_id = %s::uuid)
"""

# Delete a block row
_DELETE_BLOCK_SQL = """
DELETE FROM blocks
WHERE blocker_id = %s::uuid AND blocked_id = %s::uuid
"""

# Fetch the caller's block list
_SELECT_BLOCKS_SQL = """
SELECT blocked_id, created_at
FROM blocks
WHERE blocker_id = %s::uuid
ORDER BY created_at DESC
"""


# ---------------------------------------------------------------------------
# Pure helper functions
# ---------------------------------------------------------------------------


def _get_user_id_and_sex(event: dict) -> tuple[str, str]:
    """Extract (user_id, user_sex) from the JWT claims in the HTTP API event."""
    claims: dict = event["requestContext"]["authorizer"]["jwt"]["claims"]
    user_id: str = claims["sub"]
    user_sex: str = claims.get("custom:user_sex", "")
    return user_id, user_sex


def _json_response(status_code: int, body: Any) -> dict:
    """Build a Lambda HTTP response dict."""
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str),
    }


def _row_to_dict(row: tuple, description) -> dict:
    """Convert a psycopg2 row tuple to a dict using cursor description."""
    return {col[0]: val for col, val in zip(description, row)}


def _now_iso8601() -> str:
    """Return current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# DynamoDB helpers
# ---------------------------------------------------------------------------


def _deactivate_chat_room(blocker_id: str, blocked_id: str) -> bool:
    """
    Call DynamoDB UpdateItem to deactivate the chat room for the pair.

    Returns True when the update succeeded or was a no-op
    (ConditionalCheckFailedException).  Returns False on any other error,
    which the caller converts to a chat_deactivation_pending: true response.
    """
    table = os.environ.get("TABLE_CHAT_ROOMS", _TABLE_CHAT_ROOMS)
    room_id = chat_room_id(blocker_id, blocked_id)
    now = _now_iso8601()

    try:
        _get_dynamo().update_item(
            TableName=table,
            Key={"room_id": {"S": room_id}},
            UpdateExpression=(
                "SET #status = :deactivated, deactivated_reason = :blocked, "
                "deactivated_by = :blocker, deactivated_at = :now"
            ),
            ConditionExpression=(
                "attribute_exists(room_id) AND "
                "(attribute_not_exists(#status) OR #status = :active)"
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":deactivated": {"S": "deactivated"},
                ":blocked": {"S": "blocked"},
                ":blocker": {"S": blocker_id},
                ":now": {"S": now},
                ":active": {"S": "active"},
            },
        )
        return True
    except Exception as exc:
        # Catch botocore.exceptions.ClientError by duck-typing its .response attribute
        # so this module does not need a top-level botocore import (boto3 is a layer dep).
        error_response = getattr(exc, "response", None)
        if error_response is None:
            raise
        code = error_response.get("Error", {}).get("Code", "")
        if code == "ConditionalCheckFailedException":
            logger.info(
                "block: chat-room deactivation no-op (room absent or already "
                "deactivated for another reason)",
                extra={"blocker_id": blocker_id, "blocked_id": blocked_id},
            )
            return True
        logger.error(
            "block: DynamoDB deactivation failed",
            extra={
                "blocker_id": blocker_id,
                "blocked_id": blocked_id,
                "dynamo_error_code": code,
            },
        )
        return False


def _reactivate_chat_room(unblocker_id: str, unblocked_id: str) -> bool:
    """
    Call DynamoDB UpdateItem to reactivate the chat room for the pair.

    Only fires when the room exists, was deactivated by a block, AND the block
    was placed by the same user now removing it (prevents resurrecting rooms
    deactivated for unrelated reasons or by a different blocker).

    Returns True on success or ConditionalCheckFailed no-op.
    Returns False on any other DynamoDB error.
    """
    table = os.environ.get("TABLE_CHAT_ROOMS", _TABLE_CHAT_ROOMS)
    room_id = chat_room_id(unblocker_id, unblocked_id)
    now = _now_iso8601()

    try:
        _get_dynamo().update_item(
            TableName=table,
            Key={"room_id": {"S": room_id}},
            UpdateExpression=(
                "SET #status = :active, reactivated_at = :now "
                "REMOVE deactivated_reason, deactivated_by, deactivated_at"
            ),
            ConditionExpression=(
                "attribute_exists(room_id) AND "
                "deactivated_reason = :blocked AND "
                "deactivated_by = :unblocker"
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":active": {"S": "active"},
                ":now": {"S": now},
                ":blocked": {"S": "blocked"},
                ":unblocker": {"S": unblocker_id},
            },
        )
        return True
    except Exception as exc:
        # Same duck-typing pattern as _deactivate_chat_room — botocore is a layer dep.
        error_response = getattr(exc, "response", None)
        if error_response is None:
            raise
        code = error_response.get("Error", {}).get("Code", "")
        if code == "ConditionalCheckFailedException":
            logger.info(
                "block: chat-room reactivation no-op (room absent or already "
                "deactivated for another reason)",
                extra={"unblocker_id": unblocker_id, "unblocked_id": unblocked_id},
            )
            return True
        logger.error(
            "block: DynamoDB reactivation failed",
            extra={
                "unblocker_id": unblocker_id,
                "unblocked_id": unblocked_id,
                "dynamo_error_code": code,
            },
        )
        return False


# ---------------------------------------------------------------------------
# Sub-handlers
# ---------------------------------------------------------------------------


def _handle_get_blocks(event: dict, user_id: str, user_sex: str) -> dict:
    """
    GET /v1/blocks — return the list of users the caller has blocked.

    Returns a JSON array of {blocked_id, created_at} objects.
    """
    conn = _get_conn()
    try:
        with knotify_db.rls_context(conn, user_id, user_sex):
            with conn.cursor() as cur:
                cur.execute(_SELECT_BLOCKS_SQL, (user_id,))
                rows = cur.fetchall()
                description = cur.description
    except Exception:
        conn.close()
        global _conn
        _conn = None
        raise

    blocks = [_row_to_dict(row, description) for row in rows]
    return _json_response(200, {"blocks": blocks})


def _handle_post_blocks(event: dict, user_id: str, user_sex: str) -> dict:
    """
    POST /v1/blocks — block a current friend.

    Body: {"userId": "<uuid of user to block>"}

    1. Validate body — return 400 if missing or malformed.
    2. Check friendship exists — return 409 not_friends if absent.
    3. In a SINGLE Aurora transaction:
         INSERT INTO blocks
         DELETE FROM friendships
         DELETE FROM friend_requests (both directions)
    4. AFTER commit, call DynamoDB to deactivate the chat room.
       ConditionalCheckFailed → no-op (INFO). Other error → HTTP 200 + pending flag.
    """
    body_raw = event.get("body")
    if not body_raw:
        return _json_response(400, {"error": "missing_body"})

    try:
        body: dict = json.loads(body_raw)
    except (json.JSONDecodeError, TypeError):
        return _json_response(400, {"error": "invalid_json"})

    if not isinstance(body, dict):
        return _json_response(400, {"error": "body_must_be_object"})

    target_user_id = body.get("userId", "").strip() if isinstance(body.get("userId"), str) else ""
    if not target_user_id:
        return _json_response(400, {"error": "userId_required"})

    blocker_id = user_id
    blocked_id = target_user_id

    conn = _get_conn()
    try:
        with knotify_db.rls_context(conn, blocker_id, user_sex):
            with conn.cursor() as cur:
                # Friendship guard — must be friends to block
                cur.execute(
                    _SELECT_FRIENDSHIP_SQL,
                    (blocker_id, blocked_id, blocked_id, blocker_id),
                )
                friendship_row = cur.fetchone()

            if friendship_row is None:
                return _json_response(409, {"error": "not_friends"})

            # Single Aurora transaction: block + unfriend + delete requests
            with conn.cursor() as cur:
                cur.execute(_INSERT_BLOCK_SQL, (blocker_id, blocked_id))
                cur.execute(
                    _DELETE_FRIENDSHIP_SQL,
                    (blocker_id, blocked_id, blocked_id, blocker_id),
                )
                cur.execute(
                    _DELETE_FRIEND_REQUESTS_SQL,
                    (blocker_id, blocked_id, blocked_id, blocker_id),
                )

    except Exception:
        conn.close()
        global _conn
        _conn = None
        raise

    # Aurora transaction has committed at this point.
    # Now call DynamoDB — NEVER roll back Aurora if this fails.
    dynamo_ok = _deactivate_chat_room(blocker_id, blocked_id)

    response_body: dict = {
        "blocker_id": blocker_id,
        "blocked_id": blocked_id,
    }
    if not dynamo_ok:
        response_body["chat_deactivation_pending"] = True

    return _json_response(200, response_body)


def _handle_delete_blocks(event: dict, user_id: str, user_sex: str) -> dict:
    """
    DELETE /v1/blocks/{userId} — unblock a user.

    1. DELETE FROM blocks for the (caller, userId) pair.
    2. AFTER commit, call DynamoDB to reactivate the chat room.
       Same ConditionalCheckFailed no-op / other-error-with-flag contract.
       On DynamoDB failure returns HTTP 200 with chat_deactivation_pending: true
       (same flag name as the POST path — see module docstring for rationale).
    """
    path_params = event.get("pathParameters") or {}
    target_user_id = path_params.get("userId", "").strip()
    if not target_user_id:
        return _json_response(400, {"error": "userId_required"})

    unblocker_id = user_id
    unblocked_id = target_user_id

    conn = _get_conn()
    try:
        with knotify_db.rls_context(conn, unblocker_id, user_sex):
            with conn.cursor() as cur:
                cur.execute(_DELETE_BLOCK_SQL, (unblocker_id, unblocked_id))
    except Exception:
        conn.close()
        global _conn
        _conn = None
        raise

    # Aurora committed — now reactivate the DynamoDB chat room.
    dynamo_ok = _reactivate_chat_room(unblocker_id, unblocked_id)

    response_body: dict = {
        "unblocker_id": unblocker_id,
        "unblocked_id": unblocked_id,
    }
    if not dynamo_ok:
        response_body["chat_deactivation_pending"] = True

    return _json_response(200, response_body)


# ---------------------------------------------------------------------------
# Route dispatcher
# ---------------------------------------------------------------------------

_PATH_BLOCKS = "/v1/blocks"
_PATH_BLOCKS_PREFIX = "/v1/blocks/"


def _dispatch(event: dict, user_id: str, user_sex: str) -> dict:
    """Route the request to the appropriate sub-handler."""
    http = event.get("requestContext", {}).get("http", {})
    method: str = http.get("method", "").upper()
    path: str = http.get("path", "")

    if method == "GET" and path == _PATH_BLOCKS:
        return _handle_get_blocks(event, user_id, user_sex)

    if method == "POST" and path == _PATH_BLOCKS:
        return _handle_post_blocks(event, user_id, user_sex)

    if method == "DELETE" and path.startswith(_PATH_BLOCKS_PREFIX):
        return _handle_delete_blocks(event, user_id, user_sex)

    logger.warning(
        "unmatched_route",
        extra={"method": method, "path": path},
    )
    return _json_response(404, {"error": "not_found"})


# ---------------------------------------------------------------------------
# Lambda entrypoint
# ---------------------------------------------------------------------------


@with_edge_secret
@require_profile_complete
def handler(event: dict, context: object) -> dict:
    """
    knotify-blocks Lambda entrypoint.

    Validates the edge secret (via @with_edge_secret), checks that the caller's
    profile is complete (via @require_profile_complete), extracts user identity
    from JWT claims, and dispatches to the appropriate sub-handler.

    Args:
        event:   HTTP API Gateway v2 Lambda event.
        context: Lambda context object (unused).

    Returns:
        HTTP response dict with statusCode, headers, and body.
    """
    try:
        user_id, user_sex = _get_user_id_and_sex(event)
    except (KeyError, TypeError) as exc:
        logger.error(
            "jwt_claims_missing",
            extra={"error": str(exc)},
        )
        return _json_response(401, {"error": "unauthorized"})

    logger.info(
        "blocks_request",
        extra={
            "user_id": user_id,
            "method": event.get("requestContext", {}).get("http", {}).get("method"),
            "path": event.get("requestContext", {}).get("http", {}).get("path"),
        },
    )

    return _dispatch(event, user_id, user_sex)
