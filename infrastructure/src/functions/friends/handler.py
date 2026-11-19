"""
knotify-friends Lambda handler.

Implements seven HTTP API Gateway v2 routes:
  GET    /v1/friends                          — block-filtered friend list
  DELETE /v1/friends/{userId}                 — remove a friendship
  GET    /v1/friend-requests[?direction=incoming|outgoing]  — block-filtered requests
                                                             direction=incoming: only to_user_id=me
                                                             direction=outgoing: only from_user_id=me
                                                             missing: both (backward-compat)
  POST   /v1/friend-requests                  — send a friend request (block-aware)
                                               Side effect: writes a Notifications row
                                               (type='friend_request_received') to DynamoDB
                                               so the notifications_publisher Lambda can fan
                                               out to onNotificationForMe (FR-5, story 8.9c).
  POST   /v1/friend-requests/{id}/accept      — accept a pending request
  POST   /v1/friend-requests/{id}/decline     — decline a pending request
  DELETE /v1/friend-requests/{id}             — cancel an outgoing request

All routes require a valid Cognito JWT (enforced by the HTTP API JWT authorizer)
and the x-knotify-edge-secret header (enforced by the @with_edge_secret decorator).

Design notes:
  - User identity comes from the JWT sub claim — never from URL path or request body.
  - RLS GUCs are set on every request via knotify_db.rls_context() so PostgreSQL
    row-level security enforces visibility at the DB layer.
  - The module-level _conn is reused across warm invocations (cold-start friendly).
    rls_context() manages transaction lifetime; GUCs are SET LOCAL and expire
    when the transaction ends so there is no leakage across requests.
  - Block-aware behaviour (Brainstorm B3 / Option B):
      POST /v1/friend-requests: is_blocked() is called INSIDE the same transaction
        as the INSERT so a concurrent block cannot race the request through. If
        blocked → 409 {"error":"blocked"}. UniqueViolation → 409 {"error":"already_pending"}.
      GET /v1/friends: block_filter() fragments embedded in the SELECT cover both
        the user_a and user_b columns so any pair with a block in either direction
        is filtered out.
      GET /v1/friend-requests: block_filter() applied against both from_user_id
        and to_user_id.
  - accept: BEGIN → SELECT FOR UPDATE → if not found → 404. Else UPDATE status +
    INSERT INTO friendships using lex-min/max canonical ordering (same contract as
    knotify_obs.chat_room_id). COMMIT.
  - decline / DELETE friend-request: UPDATE/DELETE; 404 when rowcount == 0.
  - DELETE /v1/friends/{userId}: canonical pair ordering; 200 even when no row.
  - The canonical pair ordering (min/max string compare of UUIDs) matches
    knotify_obs.chat_room_id so phase-8 chat sees consistent pair ordering.

Dependencies (Lambda layers):
  - knotify_obs: init_logger, with_edge_secret, is_blocked, block_filter
  - knotify_db:  get_connection, rls_context
"""

from __future__ import annotations

import datetime
import json
import os
import re
import uuid
from typing import Any

import psycopg2.errors
import knotify_db
from knotify_obs import block_filter, chat_room_id, init_logger, is_blocked, require_profile_complete, with_edge_secret

# ---------------------------------------------------------------------------
# Module-level singletons (cold-start optimization)
# ---------------------------------------------------------------------------

logger = init_logger("knotify_friends")

_DB_SECRET_NAME: str = os.environ.get("DB_SECRET_NAME", "")
_TABLE_CHAT_ROOMS: str = os.environ.get("TABLE_CHAT_ROOMS", "ChatRooms")
_TABLE_NOTIFICATIONS: str = os.environ.get("TABLE_NOTIFICATIONS", "Notifications")


def _new_notification_id() -> str:
    """
    Return a 32-character hex string suitable for use as a notification ID.

    No ULID package is bundled in the knotify_obs / knotify_db layers, so we
    use uuid4().hex.  The SK format is '<ISO8601>#<id>' which preserves
    per-user time ordering for all practical notification volumes.
    """
    return uuid.uuid4().hex

_conn = None
_dynamo = None


def _get_conn():
    """
    Return a (possibly cached) psycopg2 connection.

    Extracted so integration/unit tests can patch at the module level.
    On cold start the connection is None and is created on first call.
    Subsequent warm invocations reuse the same connection.
    """
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
# Deck-view fields returned for each friend in the list
# ---------------------------------------------------------------------------

_DECK_VIEW_COLS = (
    "u.user_id, u.first_name, u.last_name, u.sex, u.age, u.photo_url, "
    "u.username, u.job_title, u.current_residence_city, "
    "u.current_residence_country, u.resident_country_code, "
    "u.chosen_profile_avatar, u.profile_complete_verified"
)

# ---------------------------------------------------------------------------
# SQL statements
# ---------------------------------------------------------------------------

# GET /v1/friends — two CTEs handle user_a and user_b directions separately so
# block_filter() can reference the whitelisted column names directly.
# The UNION deduplicates (a user can appear in only one ordering).
_SELECT_FRIENDS_SQL = (
    "WITH friends_as_a AS ("
    "  SELECT u.user_id, u.first_name, u.last_name, u.sex, u.age, u.photo_url, "
    "         u.username, u.job_title, u.current_residence_city, "
    "         u.current_residence_country, u.resident_country_code, "
    "         u.chosen_profile_avatar, u.profile_complete_verified "
    "  FROM friendships f "
    "  JOIN users u ON u.user_id = f.user_b "
    "  WHERE f.user_a = %s::uuid "
    "    AND {bf_user_b} "
    "    AND u.deleted_at IS NULL"
    "),"
    " friends_as_b AS ("
    "  SELECT u.user_id, u.first_name, u.last_name, u.sex, u.age, u.photo_url, "
    "         u.username, u.job_title, u.current_residence_city, "
    "         u.current_residence_country, u.resident_country_code, "
    "         u.chosen_profile_avatar, u.profile_complete_verified "
    "  FROM friendships f "
    "  JOIN users u ON u.user_id = f.user_a "
    "  WHERE f.user_b = %s::uuid "
    "    AND {bf_user_a} "
    "    AND u.deleted_at IS NULL"
    ") "
    "SELECT * FROM friends_as_a "
    "UNION "
    "SELECT * FROM friends_as_b"
)

# DELETE /v1/friends/{userId}
_DELETE_FRIENDSHIP_SQL = """
DELETE FROM friendships
WHERE (user_a = %s::uuid AND user_b = %s::uuid)
   OR (user_a = %s::uuid AND user_b = %s::uuid)
"""

# GET /v1/friend-requests — block-filter applied to both from_user_id and to_user_id.
# Column names match migration 0005_create_friend_requests.sql:
#   request_id, from_user_id, to_user_id, status, created_at, responded_at.
# Table is aliased `fr` so block_filter() can reference fr.from_user_id /
# fr.to_user_id (correlated subqueries must use the outer alias, not the bare
# table name).
#
# Three variants (FR-1): select the right constant based on the `direction`
# query parameter.  block_filter fragments are always applied to BOTH columns
# regardless of direction so a blocked user's requests are filtered from all
# three views.
_SELECT_FRIEND_REQUESTS_SQL = (
    "SELECT fr.request_id, fr.from_user_id, fr.to_user_id, fr.status, fr.created_at "
    "FROM friend_requests fr "
    "WHERE (fr.from_user_id = %s::uuid OR fr.to_user_id = %s::uuid) "
    "  AND fr.status = 'pending' "
    "  AND {bf_from} "
    "  AND {bf_to}"
)

# FR-1: direction='incoming' — only requests sent TO the caller.
# Parameters: %s::uuid (to_user_id = me), %s %s (bf_from), %s %s (bf_to) → 5 bindings.
_SELECT_FRIEND_REQUESTS_INCOMING_SQL = (
    "SELECT fr.request_id, fr.from_user_id, fr.to_user_id, fr.status, fr.created_at "
    "FROM friend_requests fr "
    "WHERE fr.to_user_id = %s::uuid "
    "  AND fr.status = 'pending' "
    "  AND {bf_from} "
    "  AND {bf_to}"
)

# FR-1: direction='outgoing' — only requests sent BY the caller.
# Parameters: %s::uuid (from_user_id = me), %s %s (bf_from), %s %s (bf_to) → 5 bindings.
_SELECT_FRIEND_REQUESTS_OUTGOING_SQL = (
    "SELECT fr.request_id, fr.from_user_id, fr.to_user_id, fr.status, fr.created_at "
    "FROM friend_requests fr "
    "WHERE fr.from_user_id = %s::uuid "
    "  AND fr.status = 'pending' "
    "  AND {bf_from} "
    "  AND {bf_to}"
)

# POST /v1/friend-requests — INSERT with RETURNING
_INSERT_FRIEND_REQUEST_SQL = """
INSERT INTO friend_requests (from_user_id, to_user_id, status, created_at)
VALUES (%s::uuid, %s::uuid, 'pending', NOW())
RETURNING request_id, from_user_id, to_user_id, status, created_at
"""

# POST /v1/friend-requests/{id}/accept — SELECT FOR UPDATE
_SELECT_REQUEST_FOR_UPDATE_SQL = """
SELECT request_id, from_user_id, to_user_id, status, created_at
FROM friend_requests
WHERE request_id = %s::uuid
  AND status = 'pending'
FOR UPDATE
"""

# Accept: UPDATE status. responded_at is the lifecycle audit column per
# migration 0005 (set when status transitions out of 'pending').
_UPDATE_REQUEST_ACCEPTED_SQL = """
UPDATE friend_requests
SET status = 'accepted', responded_at = NOW()
WHERE request_id = %s::uuid
"""

# Accept: INSERT friendship using lex-min/max canonical ordering
_INSERT_FRIENDSHIP_SQL = """
INSERT INTO friendships (user_a, user_b, created_at)
VALUES (%s::uuid, %s::uuid, NOW())
ON CONFLICT (user_a, user_b) DO NOTHING
"""

# Accept: cancel the acceptor's own reverse-direction pending request to the
# original sender (if any). Prevents a "ghost pending" where the acceptor's
# outgoing request to the sender remains pending after the pair has already
# become friends via the inbound accept. Mirrors the DELETE-outgoing route's
# status semantics (flip to 'cancelled' rather than DELETE — preserves audit
# trail). No-op (0 rows affected) when no reverse row exists.
_UPDATE_REVERSE_PENDING_CANCELLED_SQL = """
UPDATE friend_requests
SET status = 'cancelled', responded_at = NOW()
WHERE from_user_id = %s::uuid
  AND to_user_id   = %s::uuid
  AND status = 'pending'
"""

# POST /v1/friend-requests/{id}/decline
_UPDATE_REQUEST_DECLINED_SQL = """
UPDATE friend_requests
SET status = 'declined', responded_at = NOW()
WHERE request_id = %s::uuid
  AND status = 'pending'
"""

# DELETE /v1/friend-requests/{id} — only the sender (from_user_id) can cancel.
_DELETE_REQUEST_SQL = """
DELETE FROM friend_requests
WHERE request_id = %s::uuid
  AND from_user_id = %s::uuid
"""

# FR-2: reverse-pending check — locks the reverse row so a concurrent reverse POST
# cannot race through and INSERT before we detect it. Parameters bind
# (target_user_id, requester_id) — the REVERSE of what the new row would insert.
_SELECT_REVERSE_PENDING_FOR_UPDATE_SQL = """
SELECT request_id
FROM friend_requests
WHERE from_user_id = %s::uuid
  AND to_user_id   = %s::uuid
  AND status = 'pending'
FOR UPDATE
"""

# FR-3: already-friends check — uses canonical pair ordering (lex-min, lex-max)
# matching _canonical_pair(). Parameters bind (user_a, user_b) in canonical order.
_SELECT_FRIENDSHIP_SQL = """
SELECT 1
FROM friendships
WHERE user_a = %s::uuid
  AND user_b = %s::uuid
"""

# FR-5: requester first-name lookup — single point-lookup run inside the POST
# transaction before COMMIT so no extra round trip after Aurora commits.
# Parameters bind (user_id,).
_SELECT_USER_FIRST_NAME_SQL = """
SELECT first_name
FROM users
WHERE user_id = %s::uuid
"""

# ---------------------------------------------------------------------------
# Pure helper functions (no I/O — fully unit-testable)
# ---------------------------------------------------------------------------


def _get_user_id_and_sex(event: dict) -> tuple[str, str]:
    """Extract (user_id, user_sex) from the HTTP API JWT claims."""
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


def _canonical_pair(id_a: str, id_b: str) -> tuple[str, str]:
    """
    Return (user_a, user_b) in lex-min/max canonical order.

    Matches knotify_obs.chat_room_id's ordering contract so phase-8 chat
    sees consistent pair ordering for both friendships and chat rooms.
    """
    return (min(id_a, id_b), max(id_a, id_b))


# ---------------------------------------------------------------------------
# DynamoDB helpers
# ---------------------------------------------------------------------------


def _set_friendship_active(user_id_a: str, user_id_b: str) -> bool:
    """
    Call DynamoDB UpdateItem to set friendship_active=true on the ChatRooms row.

    Used on friend-request accept (story 8.9b): after the Aurora INSERT INTO
    friendships commits, flip the flag so sendMessage is unblocked.

    ConditionExpression guards on attribute_exists(room_id) — if no chat room
    has ever been created for this pair, the update silently no-ops
    (ConditionalCheckFailedException).  The next createOrGetRoom will set the
    flag to true at row-creation time (story 8.3).

    Returns True when the update succeeded or was a ConditionalCheckFailed no-op.
    Returns False on any other DynamoDB error, which the caller surfaces as
    chat_room_update_pending: true in the HTTP response.  The Aurora commit is
    never rolled back.
    """
    table = os.environ.get("TABLE_CHAT_ROOMS", _TABLE_CHAT_ROOMS)
    room_id = chat_room_id(user_id_a, user_id_b)

    try:
        _get_dynamo().update_item(
            TableName=table,
            Key={"room_id": {"S": room_id}},
            UpdateExpression="SET #fa = :true",
            ConditionExpression="attribute_exists(room_id)",
            ExpressionAttributeNames={"#fa": "friendship_active"},
            ExpressionAttributeValues={":true": {"BOOL": True}},
        )
        return True
    except Exception as exc:
        # Duck-type botocore.exceptions.ClientError via .response attribute so
        # this module does not require a top-level botocore import (boto3 is a
        # layer dependency, not locally installed in test environments).
        error_response = getattr(exc, "response", None)
        if error_response is None:
            raise
        code = error_response.get("Error", {}).get("Code", "")
        if code == "ConditionalCheckFailedException":
            logger.info(
                "accept: chat-room friendship_active no-op (room does not exist yet)",
                extra={"user_id_a": user_id_a, "user_id_b": user_id_b},
            )
            return True
        logger.error(
            "accept: DynamoDB friendship_active update failed",
            extra={
                "user_id_a": user_id_a,
                "user_id_b": user_id_b,
                "dynamo_error_code": code,
            },
        )
        return False


def _clear_friendship_active(user_id_a: str, user_id_b: str) -> bool:
    """
    Call DynamoDB UpdateItem to set friendship_active=false on the ChatRooms row.

    Used on unfriend (story 8.9b): after the Aurora DELETE FROM friendships
    commits, flip the flag so sendMessage returns RoomReadOnly.  The room's
    status stays 'active' — deactivation is the blocks Lambda's job (story 8.9).

    Same attribute_exists(room_id) condition as _set_friendship_active — silent
    no-op when no chat room ever existed.

    Returns True on success or ConditionalCheckFailed no-op.
    Returns False on any other DynamoDB error.
    """
    table = os.environ.get("TABLE_CHAT_ROOMS", _TABLE_CHAT_ROOMS)
    room_id = chat_room_id(user_id_a, user_id_b)

    try:
        _get_dynamo().update_item(
            TableName=table,
            Key={"room_id": {"S": room_id}},
            UpdateExpression="SET #fa = :false",
            ConditionExpression="attribute_exists(room_id)",
            ExpressionAttributeNames={"#fa": "friendship_active"},
            ExpressionAttributeValues={":false": {"BOOL": False}},
        )
        return True
    except Exception as exc:
        error_response = getattr(exc, "response", None)
        if error_response is None:
            raise
        code = error_response.get("Error", {}).get("Code", "")
        if code == "ConditionalCheckFailedException":
            logger.info(
                "unfriend: chat-room friendship_active no-op (room does not exist)",
                extra={"user_id_a": user_id_a, "user_id_b": user_id_b},
            )
            return True
        logger.error(
            "unfriend: DynamoDB friendship_active update failed",
            extra={
                "user_id_a": user_id_a,
                "user_id_b": user_id_b,
                "dynamo_error_code": code,
            },
        )
        return False


def _write_notification_received(
    recipient_id: str,
    requester_id: str,
    request_id: str,
    requester_first_name: str,
) -> bool:
    """
    Write a type='friend_request_received' row to the Notifications DynamoDB table.

    Called AFTER the Aurora transaction in _handle_post_friend_requests commits.

    ## Schema (matches the notifications_publisher consumer expectation)

    PK  user_id                     = recipient_id
    SK  created_at_notification_id  = "<ISO8601>#<uuid_hex>"
        notification_id             = <uuid_hex>
        type                        = 'friend_request_received'
        payload                     = JSON string {"request_id": ..., "from_user_id": ...}
        sender_user_id              = requester_id
        sender_name                 = requester_first_name
        read                        = False
        delivered                   = False
        created_at                  = <ISO8601>

    ## Best-effort semantics

    Returns True on success. Returns False on any DynamoDB error (duck-typed
    via getattr(exc, 'response', None)) without propagating the exception so
    the Aurora commit is never rolled back.  The caller surfaces failure as
    notification_pending: true in the HTTP response body.

    ## ULID note

    Neither knotify_obs nor knotify_db layers include a ULID package, so the
    notification ID is uuid4().hex (32 chars).  _new_notification_id() wraps
    the generator for clarity and future replaceability.
    """
    table = os.environ.get("TABLE_NOTIFICATIONS", _TABLE_NOTIFICATIONS)
    now_iso = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    notif_id = _new_notification_id()
    sk = f"{now_iso}#{notif_id}"

    payload_str = json.dumps(
        {"request_id": request_id, "from_user_id": requester_id}
    )

    try:
        _get_dynamo().put_item(
            TableName=table,
            Item={
                "user_id": {"S": recipient_id},
                "created_at_notification_id": {"S": sk},
                "notification_id": {"S": notif_id},
                "type": {"S": "friend_request_received"},
                "payload": {"S": payload_str},
                "sender_user_id": {"S": requester_id},
                "sender_name": {"S": requester_first_name},
                "read": {"BOOL": False},
                "delivered": {"BOOL": False},
                "created_at": {"S": now_iso},
            },
        )
        return True
    except Exception as exc:
        # Duck-type botocore.exceptions.ClientError via .response attribute so
        # this module does not require a top-level botocore import.
        error_response = getattr(exc, "response", None)
        if error_response is None:
            raise
        code = error_response.get("Error", {}).get("Code", "")
        logger.error(
            "post-friend-request: DynamoDB Notifications PutItem failed",
            extra={
                "recipient_id": recipient_id,
                "requester_id": requester_id,
                "dynamo_error_code": code,
            },
        )
        return False


def _write_notification_accepted(
    original_sender_id: str,
    acceptor_id: str,
    request_id: str,
    acceptor_first_name: str,
) -> bool:
    """
    Write a type='friend_request_accepted' row to the Notifications DynamoDB table.

    Called AFTER the Aurora transaction in _handle_accept_friend_request commits
    and AFTER _set_friendship_active().

    ## Schema (matches the notifications_publisher consumer expectation)

    PK  user_id                     = original_sender_id  (the person notified of acceptance)
    SK  created_at_notification_id  = "<ISO8601>#<uuid_hex>"
        notification_id             = <uuid_hex>
        type                        = 'friend_request_accepted'
        payload                     = JSON string {"request_id": ..., "from_user_id": <acceptor_id>}
        sender_user_id              = acceptor_id
        sender_name                 = acceptor_first_name
        read                        = False
        delivered                   = False
        created_at                  = <ISO8601>

    ## Best-effort semantics

    Returns True on success. Returns False on any DynamoDB error (duck-typed
    via getattr(exc, 'response', None)) without propagating the exception so
    the Aurora commit is never rolled back.  The caller surfaces failure as
    notification_pending: true in the HTTP response body.
    """
    table = os.environ.get("TABLE_NOTIFICATIONS", _TABLE_NOTIFICATIONS)
    now_iso = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    notif_id = _new_notification_id()
    sk = f"{now_iso}#{notif_id}"

    payload_str = json.dumps(
        {"request_id": request_id, "from_user_id": acceptor_id}
    )

    try:
        _get_dynamo().put_item(
            TableName=table,
            Item={
                "user_id": {"S": original_sender_id},
                "created_at_notification_id": {"S": sk},
                "notification_id": {"S": notif_id},
                "type": {"S": "friend_request_accepted"},
                "payload": {"S": payload_str},
                "sender_user_id": {"S": acceptor_id},
                "sender_name": {"S": acceptor_first_name},
                "read": {"BOOL": False},
                "delivered": {"BOOL": False},
                "created_at": {"S": now_iso},
            },
        )
        return True
    except Exception as exc:
        # Duck-type botocore.exceptions.ClientError via .response attribute so
        # this module does not require a top-level botocore import.
        error_response = getattr(exc, "response", None)
        if error_response is None:
            raise
        code = error_response.get("Error", {}).get("Code", "")
        logger.error(
            "accept-friend-request: DynamoDB Notifications PutItem failed",
            extra={
                "original_sender_id": original_sender_id,
                "acceptor_id": acceptor_id,
                "dynamo_error_code": code,
            },
        )
        return False


# ---------------------------------------------------------------------------
# Sub-handlers
# ---------------------------------------------------------------------------


def _handle_get_friends(event: dict, user_id: str, user_sex: str) -> dict:
    """
    GET /v1/friends — return the block-filtered friend list for the caller.

    Two CTEs cover both ordering directions of the friendships table so the
    block_filter() whitelist column references remain literal (no dynamic column
    name generation outside the whitelist).
    """
    bf_user_a = block_filter("f.user_a")
    bf_user_b = block_filter("f.user_b")

    sql = _SELECT_FRIENDS_SQL.format(bf_user_a=bf_user_a, bf_user_b=bf_user_b)

    # Parameters: user_id (user_a CTE), user_id (twice for bf_user_b),
    #             user_id (user_b CTE), user_id (twice for bf_user_a)
    params = (user_id, user_id, user_id, user_id, user_id, user_id)

    conn = _get_conn()
    try:
        with knotify_db.rls_context(conn, user_id, user_sex):
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
                description = cur.description
    except Exception:
        conn.close()
        global _conn
        _conn = None
        raise

    friends = [_row_to_dict(row, description) for row in rows]
    return _json_response(200, {"friends": friends})


def _handle_delete_friend(event: dict, user_id: str, user_sex: str) -> dict:
    """
    DELETE /v1/friends/{userId} — remove a friendship row.

    Uses canonical pair ordering for the DELETE; returns 200 even when no row
    was deleted (idempotent).

    AFTER the Aurora DELETE commits, calls DynamoDB to set friendship_active=false
    on the ChatRooms row (story 8.9b).  ConditionalCheckFailed → no-op (no room
    ever existed).  Other DynamoDB errors → 200 with chat_room_update_pending: true
    (Aurora commit is never rolled back).
    """
    path_params = event.get("pathParameters") or {}
    target_user_id = path_params.get("userId", "").strip()
    if not target_user_id:
        return _json_response(400, {"error": "userId_required"})

    user_a, user_b = _canonical_pair(user_id, target_user_id)

    conn = _get_conn()
    try:
        with knotify_db.rls_context(conn, user_id, user_sex):
            with conn.cursor() as cur:
                cur.execute(
                    _DELETE_FRIENDSHIP_SQL,
                    (user_a, user_b, user_b, user_a),
                )
    except Exception:
        conn.close()
        global _conn
        _conn = None
        raise

    # Aurora transaction has committed at this point.
    # Now call DynamoDB — NEVER roll back Aurora if this fails.
    dynamo_ok = _clear_friendship_active(user_id, target_user_id)

    response_body: dict = {"deleted": True}
    if not dynamo_ok:
        response_body["chat_room_update_pending"] = True

    return _json_response(200, response_body)


def _handle_get_friend_requests(event: dict, user_id: str, user_sex: str) -> dict:
    """
    GET /v1/friend-requests — return block-filtered pending requests for the caller.

    Optional query parameter `direction`:
      - missing / omitted: returns BOTH incoming and outgoing requests (backward-compat).
      - 'incoming': returns only requests where to_user_id = caller.
      - 'outgoing': returns only requests where from_user_id = caller.
      - any other value: 400 {"error": "invalid_direction"}.

    block_filter is always applied to BOTH fr.from_user_id and fr.to_user_id
    regardless of direction, so a blocked user's requests are suppressed in all views.

    Parameter binding counts (bf_from and bf_to each consume 2 bindings):
      - both (no direction): 2 (OR clause) + 2 (bf_from) + 2 (bf_to) = 6
      - incoming / outgoing: 1 (single equality) + 2 (bf_from) + 2 (bf_to) = 5
    """
    qsp = event.get("queryStringParameters") or {}
    direction = qsp.get("direction")

    _VALID_DIRECTIONS = {"incoming", "outgoing"}
    if direction is not None and direction not in _VALID_DIRECTIONS:
        return _json_response(400, {"error": "invalid_direction"})

    bf_from = block_filter("fr.from_user_id")
    bf_to = block_filter("fr.to_user_id")

    if direction == "incoming":
        sql = _SELECT_FRIEND_REQUESTS_INCOMING_SQL.format(bf_from=bf_from, bf_to=bf_to)
        # 1 binding for to_user_id + 2 for bf_from + 2 for bf_to = 5
        params = (user_id, user_id, user_id, user_id, user_id)
    elif direction == "outgoing":
        sql = _SELECT_FRIEND_REQUESTS_OUTGOING_SQL.format(bf_from=bf_from, bf_to=bf_to)
        # 1 binding for from_user_id + 2 for bf_from + 2 for bf_to = 5
        params = (user_id, user_id, user_id, user_id, user_id)
    else:
        # direction is None — both directions (backward-compat)
        sql = _SELECT_FRIEND_REQUESTS_SQL.format(bf_from=bf_from, bf_to=bf_to)
        # 2 bindings for OR clause + 2 for bf_from + 2 for bf_to = 6
        params = (user_id, user_id, user_id, user_id, user_id, user_id)

    conn = _get_conn()
    try:
        with knotify_db.rls_context(conn, user_id, user_sex):
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
                description = cur.description
    except Exception:
        conn.close()
        global _conn
        _conn = None
        raise

    friend_requests = [_row_to_dict(row, description) for row in rows]
    return _json_response(200, {"friend_requests": friend_requests})


def _handle_post_friend_requests(event: dict, user_id: str, user_sex: str) -> dict:
    """
    POST /v1/friend-requests — send a friend request.

    Body: {"toUserId": "<uuid>"}

    ## Transaction semantics

    All pre-flight checks run inside the same rls_context transaction so no
    concurrent mutation can race through between a check passing and the INSERT.

    Precedence order (load-bearing for UX — do not reorder):
      BEGIN (via rls_context) →
        (1) is_blocked(conn, requester, target) — blocked takes first priority so
              a blocker's state is not leaked by a different error code appearing
              earlier →
            if blocked → ROLLBACK → 409 {"error": "blocked"}
        (2) _SELECT_FRIENDSHIP_SQL(canonical_pair) — already-friends check runs
              second so the caller sees "already friends" rather than "you already
              requested" when both are true after an accept-then-request race →
            if row found → ROLLBACK → 409 {"error": "already_friends"}
        (3) _SELECT_REVERSE_PENDING_FOR_UPDATE_SQL(target, requester) — FOR UPDATE
              lock prevents a concurrent reverse POST from racing through →
            if row found → ROLLBACK → 409 {"error": "reverse_pending",
              "request_id": "<id of the reverse row>"}
        (4) INSERT INTO friend_requests →
            if UniqueViolation → ROLLBACK → 409 {"error": "already_pending"}
              (covers the race where two concurrent same-direction POSTs both
               pass the SELECT pre-check; the UNIQUE constraint catches the loser)
        (5) SELECT first_name FROM users WHERE user_id = requester — free point
              lookup in the same transaction before COMMIT; value forwarded to
              the post-commit DynamoDB write.
      COMMIT → 201 with the new request row

    ## Side effects

    After Aurora commits, _write_notification_received() performs a DynamoDB
    PutItem into the Notifications table (TABLE_NOTIFICATIONS env var) addressed
    to the recipient (target_user_id).  This is OUTSIDE the Aurora transaction —
    best-effort semantics:
      - On success: 201 response body contains only the new request row fields.
      - On failure: Aurora commit stands; 201 response body gains
        notification_pending: true so the caller (or a polling fallback) knows
        the real-time push did not fire.
    The notifications_publisher Lambda (story 8.9c) consumes the Notifications
    DynamoDB stream and fans out to the onNotificationForMe AppSync subscription.
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

    target_user_id = (
        body.get("toUserId", "").strip()
        if isinstance(body.get("toUserId"), str)
        else ""
    )
    if not target_user_id:
        return _json_response(400, {"error": "toUserId_required"})

    requester_id = user_id

    conn = _get_conn()
    try:
        with knotify_db.rls_context(conn, requester_id, user_sex):
            # (1) Block check — first so a blocker's state is not leaked by a
            #     different error code appearing earlier.
            if is_blocked(conn, requester_id, target_user_id):
                return _json_response(409, {"error": "blocked"})

            # (2) Already-friends check — second so "already friends" takes
            #     precedence over "you already requested" when both are true
            #     after an accept-then-request race.
            user_a, user_b = _canonical_pair(requester_id, target_user_id)
            with conn.cursor() as cur:
                cur.execute(_SELECT_FRIENDSHIP_SQL, (user_a, user_b))
                friendship_row = cur.fetchone()
            if friendship_row is not None:
                return _json_response(409, {"error": "already_friends"})

            # (3) Reverse-pending check — FOR UPDATE so a concurrent reverse POST
            #     cannot race through and INSERT before we detect it.
            #     Parameters: (target_user_id, requester_id) — the row we are
            #     looking for is target→requester (reverse of what we are about
            #     to insert: requester→target).
            with conn.cursor() as cur:
                cur.execute(
                    _SELECT_REVERSE_PENDING_FOR_UPDATE_SQL,
                    (target_user_id, requester_id),
                )
                reverse_row = cur.fetchone()
            if reverse_row is not None:
                return _json_response(
                    409,
                    {"error": "reverse_pending", "request_id": str(reverse_row[0])},
                )

            # (4) INSERT — UniqueViolation covers same-direction duplicate race.
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        _INSERT_FRIEND_REQUEST_SQL,
                        (requester_id, target_user_id),
                    )
                    row = cur.fetchone()
                    description = cur.description
            except psycopg2.errors.UniqueViolation:
                return _json_response(409, {"error": "already_pending"})

            # (5) Requester first-name lookup — inside the transaction (same
            #     connection, no extra round trip after commit) so the value
            #     is available for the post-commit Notifications PutItem.
            with conn.cursor() as cur:
                cur.execute(_SELECT_USER_FIRST_NAME_SQL, (requester_id,))
                name_row = cur.fetchone()
            requester_first_name: str = name_row[0] if name_row else ""

    except Exception as exc:
        # Re-raise if it's not one of the handled 409 paths.
        # The 409 returns above exit the with-block cleanly via rls_context's
        # commit; only unexpected exceptions fall through here.
        conn.close()
        global _conn
        _conn = None
        raise

    new_request = _row_to_dict(row, description) if row else {}

    # Aurora has committed. Best-effort DynamoDB Notifications write.
    # Never roll back Aurora if this fails — see ## Side effects in the docstring.
    notif_ok = _write_notification_received(
        recipient_id=target_user_id,
        requester_id=requester_id,
        request_id=str(new_request.get("request_id", "")),
        requester_first_name=requester_first_name,
    )

    response_body = dict(new_request)
    if not notif_ok:
        response_body["notification_pending"] = True

    return _json_response(201, response_body)


def _handle_accept_friend_request(
    event: dict, request_id: str, user_id: str, user_sex: str
) -> dict:
    """
    POST /v1/friend-requests/{id}/accept — accept a pending request.

    Transaction semantics:
      BEGIN →
        SELECT ... FOR UPDATE (locks the row, prevents concurrent double-accept) →
        if not found → 404 {"error":"not_found"} (row was deleted by a block)
        UPDATE friend_requests SET status = 'accepted'  (the request being accepted)
        INSERT INTO friendships using lex-min/max canonical ordering
        UPDATE friend_requests SET status = 'cancelled' (reverse-direction pending,
          if any — the acceptor's own outgoing request to the original sender;
          prevents a ghost pending after mutual accept)
        SELECT first_name FROM users WHERE user_id = <acceptor> (same connection,
          no extra round trip after commit — value used for the post-commit
          Notifications PutItem)
      COMMIT → 200

    Partial unique index per migration 0021 means the UPDATE to 'accepted' is
    safe to issue even when a stale `(from, to, accepted)` row exists from a
    prior handshake-then-unfriend cycle. Before 0021, that stale row caused a
    UniqueViolation 500 on the second accept in the same direction.

    AFTER the Aurora transaction commits, calls DynamoDB to set friendship_active=true
    on the ChatRooms row (story 8.9b).  ConditionalCheckFailed → no-op (no room yet;
    the next createOrGetRoom sets the flag at creation time).  Other DynamoDB errors
    → 200 with chat_room_update_pending: true (Aurora commit is never rolled back).

    ## Side effects

    After _set_friendship_active(), writes a type='friend_request_accepted' row to
    the DynamoDB Notifications table addressed to the ORIGINAL SENDER (from_user_id),
    not the acceptor.  The row drives the notifications_publisher Lambda →
    _publishFriendRequestUpdated → onFriendRequestUpdated subscription, which opens
    the RequestAcceptedModal on the sender's device.

    This write is best-effort: if the DynamoDB PutItem fails, the Aurora commit is
    NOT rolled back and the response carries notification_pending: true so the
    frontend can surface a fallback.  The chat_room_update_pending and
    notification_pending flags are independent — either, both, or neither may be set.
    """
    conn = _get_conn()
    from_user_id: str = ""
    to_user_id: str = ""
    acceptor_first_name: str = ""
    try:
        with knotify_db.rls_context(conn, user_id, user_sex):
            with conn.cursor() as cur:
                cur.execute(_SELECT_REQUEST_FOR_UPDATE_SQL, (request_id,))
                row = cur.fetchone()
                if row is None:
                    return _json_response(404, {"error": "not_found"})
                description = cur.description

            request = _row_to_dict(row, description)
            from_user_id = str(request["from_user_id"])
            to_user_id = str(request["to_user_id"])
            user_a, user_b = _canonical_pair(from_user_id, to_user_id)

            with conn.cursor() as cur:
                cur.execute(_UPDATE_REQUEST_ACCEPTED_SQL, (request_id,))
                cur.execute(_INSERT_FRIENDSHIP_SQL, (user_a, user_b))
                # Reverse-direction cleanup: if the acceptor (= user_id) also had
                # a pending outgoing request to the original sender (bidirectional
                # pending — common when both users sent each other requests),
                # cancel it now so the acceptor doesn't see a ghost pending after
                # the pair becomes friends. This runs AFTER the friendships INSERT
                # so a transaction rollback (unlikely, but possible on e.g. an RLS
                # policy failure) leaves no partial state.
                cur.execute(
                    _UPDATE_REVERSE_PENDING_CANCELLED_SQL,
                    (user_id, from_user_id),
                )

            # Acceptor first-name lookup — inside the transaction (same connection,
            # no extra round trip after commit) so the value is available for the
            # post-commit Notifications PutItem.  user_id is the JWT caller = acceptor.
            with conn.cursor() as cur:
                cur.execute(_SELECT_USER_FIRST_NAME_SQL, (user_id,))
                name_row = cur.fetchone()
            acceptor_first_name = name_row[0] if name_row else ""

    except Exception:
        conn.close()
        global _conn
        _conn = None
        raise

    # Aurora transaction has committed at this point.
    # Now call DynamoDB — NEVER roll back Aurora if either of these fails.
    dynamo_ok = _set_friendship_active(from_user_id, to_user_id)

    notif_ok = _write_notification_accepted(
        original_sender_id=from_user_id,
        acceptor_id=user_id,
        request_id=request_id,
        acceptor_first_name=acceptor_first_name,
    )

    response_body: dict = {"accepted": True, "request_id": request_id}
    if not dynamo_ok:
        response_body["chat_room_update_pending"] = True
    if not notif_ok:
        response_body["notification_pending"] = True

    return _json_response(200, response_body)


def _handle_decline_friend_request(
    event: dict, request_id: str, user_id: str, user_sex: str
) -> dict:
    """
    POST /v1/friend-requests/{id}/decline — decline a pending request.

    Returns 404 when the row no longer exists (deleted by a concurrent block).
    """
    conn = _get_conn()
    try:
        with knotify_db.rls_context(conn, user_id, user_sex):
            with conn.cursor() as cur:
                cur.execute(_UPDATE_REQUEST_DECLINED_SQL, (request_id,))
                if cur.rowcount == 0:
                    return _json_response(404, {"error": "not_found"})
    except Exception:
        conn.close()
        global _conn
        _conn = None
        raise

    return _json_response(200, {"declined": True, "request_id": request_id})


def _handle_delete_friend_request(
    event: dict, request_id: str, user_id: str, user_sex: str
) -> dict:
    """
    DELETE /v1/friend-requests/{id} — cancel an outgoing request.

    Only the sender can cancel their own outgoing request (from_user_id = me).
    Returns 404 when no matching row exists.
    """
    conn = _get_conn()
    try:
        with knotify_db.rls_context(conn, user_id, user_sex):
            with conn.cursor() as cur:
                cur.execute(_DELETE_REQUEST_SQL, (request_id, user_id))
                if cur.rowcount == 0:
                    return _json_response(404, {"error": "not_found"})
    except Exception:
        conn.close()
        global _conn
        _conn = None
        raise

    return _json_response(200, {"deleted": True, "request_id": request_id})


# ---------------------------------------------------------------------------
# Route dispatcher
# ---------------------------------------------------------------------------

_PATH_FRIENDS = "/v1/friends"
_PATH_FRIEND_REQUESTS = "/v1/friend-requests"

# Matches /v1/friends/{userId}  (UUID pattern)
_FRIENDS_DELETE_RE = re.compile(
    r"^/v1/friends/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)
# Matches /v1/friend-requests/{id}/accept
_REQUESTS_ACCEPT_RE = re.compile(
    r"^/v1/friend-requests/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})/accept$"
)
# Matches /v1/friend-requests/{id}/decline
_REQUESTS_DECLINE_RE = re.compile(
    r"^/v1/friend-requests/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})/decline$"
)
# Matches /v1/friend-requests/{id}  (DELETE)
_REQUESTS_DELETE_RE = re.compile(
    r"^/v1/friend-requests/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)


def _dispatch(event: dict, user_id: str, user_sex: str) -> dict:
    """Route the request to the appropriate sub-handler."""
    http = event.get("requestContext", {}).get("http", {})
    method: str = http.get("method", "").upper()
    path: str = http.get("path", "")

    if method == "GET" and path == _PATH_FRIENDS:
        return _handle_get_friends(event, user_id, user_sex)

    if method == "DELETE":
        m = _FRIENDS_DELETE_RE.match(path)
        if m:
            return _handle_delete_friend(
                {**event, "pathParameters": {"userId": m.group(1)}},
                user_id,
                user_sex,
            )

    if method == "GET" and path == _PATH_FRIEND_REQUESTS:
        return _handle_get_friend_requests(event, user_id, user_sex)

    if method == "POST" and path == _PATH_FRIEND_REQUESTS:
        return _handle_post_friend_requests(event, user_id, user_sex)

    if method == "POST":
        m = _REQUESTS_ACCEPT_RE.match(path)
        if m:
            return _handle_accept_friend_request(event, m.group(1), user_id, user_sex)

        m = _REQUESTS_DECLINE_RE.match(path)
        if m:
            return _handle_decline_friend_request(event, m.group(1), user_id, user_sex)

    if method == "DELETE":
        m = _REQUESTS_DELETE_RE.match(path)
        if m:
            return _handle_delete_friend_request(event, m.group(1), user_id, user_sex)

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
    knotify-friends Lambda entrypoint.

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
        "friends_request",
        extra={
            "user_id": user_id,
            "method": event.get("requestContext", {}).get("http", {}).get("method"),
            "path": event.get("requestContext", {}).get("http", {}).get("path"),
        },
    )

    return _dispatch(event, user_id, user_sex)
