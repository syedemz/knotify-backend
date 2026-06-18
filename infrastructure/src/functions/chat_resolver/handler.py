"""
knotify-chat-resolver Lambda handler — extended by story 8.3.

This is an AppSync Lambda resolver (not an HTTP API Gateway proxy event).
AppSync invokes this Lambda for every resolver field that is mapped to the
chat_resolver data source.  The event shape is:

    {
        "typeName":  "Query" | "Mutation" | "Subscription",
        "fieldName": "<field>",
        "identity":  {
            "sub": "<cognito-sub>",
            "issuer": "...",
            "claims": {
                "sub": "...",
                "custom:profile_complete": "true" | "false",
                ...
            }
        },
        "arguments": { ... },
        "source": null | {...},
        ...
    }

Story 8.0 ships an EMPTY dispatcher that returns a structured
{"errorType": "Unimplemented", "message": "..."} for every (typeName, fieldName)
pair.  Story 8.3 wires (Mutation, createOrGetRoom) to _handle_create_or_get_room.
Later stories (8.4, 8.5, 8.7, 8.8) slot further concrete implementations
into _dispatch() without touching this scaffold.

Design decisions recorded here so later stories don't re-debate them:
  - knotify_db.block_filter() is the ONLY acceptable block-exclusion mechanism.
    Hand-rolled NOT EXISTS subqueries are forbidden (hotfix #87 lesson).
  - The @require_profile_complete_appsync decorator reads
    event["identity"]["claims"]["custom:profile_complete"] and returns
    Unauthorized when not "true".  It is applied at the handler entrypoint so
    every mutation and query is protected; subscription connect-path resolvers
    in the pipeline (story 8.6) add an explicit membership check instead.
  - Friendship check: inline SQL on the `friendships` table which stores
    canonical pairs (user_a < user_b) per migration 0004. No separate
    knotify_db.is_friend() helper exists; the query is a private helper here.
  - Block check: knotify_obs.is_blocked(conn, user_a, user_b) — bidirectional,
    single SELECT.  Used here; block_filter() is for row-set exclusion in
    list queries (not needed in createOrGetRoom).

Dependencies (Lambda layers):
  - knotify_obs: init_logger, require_profile_complete_appsync, is_blocked,
                 chat_room_id
  - knotify_db:  get_connection
"""

from __future__ import annotations

import os
from typing import Any, Tuple

import knotify_db
from knotify_obs import chat_room_id, init_logger, is_blocked, require_profile_complete_appsync

# ---------------------------------------------------------------------------
# Module-level singletons (cold-start optimization)
# ---------------------------------------------------------------------------

logger = init_logger("knotify_chat_resolver")

_DB_SECRET_NAME: str = os.environ.get("DB_SECRET_NAME", "")

# Module-level DynamoDB client (reused across warm invocations).
# Lazy-initialised in _get_dynamodb(); avoids the import cost on cold start
# until the first real request arrives.
_dynamodb_client = None

# Module-level Aurora connection (reused across warm invocations).
# Lazy-initialised in _get_conn().
_conn = None

# ---------------------------------------------------------------------------
# Reason codes for Unauthorized responses.
#
# Defined as module-level string constants so downstream stories (8.4, 8.5,
# 8.7, 8.8) can import and reuse them without re-typing the literal strings.
# ---------------------------------------------------------------------------

REASON_NOT_FRIENDS: str = "NOT_FRIENDS"
REASON_BLOCKED: str = "BLOCKED"
REASON_SELF_CHAT: str = "SELF_CHAT"
# PROFILE_INCOMPLETE is returned by the @require_profile_complete_appsync
# decorator before this module's logic runs — no constant needed here, but
# documented for completeness.


def _get_dynamodb():
    """Return a (possibly cached) boto3 DynamoDB client."""
    global _dynamodb_client
    if _dynamodb_client is None:
        import boto3

        _dynamodb_client = boto3.client("dynamodb")
    return _dynamodb_client


def _get_conn():
    """Return a (possibly cached) Aurora psycopg2 connection."""
    global _conn
    if _conn is None or _conn.closed:
        _conn = knotify_db.get_connection(_DB_SECRET_NAME)
    return _conn


# ---------------------------------------------------------------------------
# Shared response builders
# ---------------------------------------------------------------------------


def _unauthorized_response(reason: str) -> dict:
    """Return a structured AppSync Unauthorized error dict.

    Args:
        reason: One of the REASON_* constants above.

    Returns:
        {"errorType": "Unauthorized", "reason": <reason>}
    """
    return {"errorType": "Unauthorized", "reason": reason}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _canonical_pair(user_a: str, user_b: str) -> Tuple[str, str]:
    """Return the two user IDs in lexicographic order (lo, hi).

    Mirrors the friendships table CHECK (user_a < user_b) constraint from
    migration 0004, so the pair can be used directly in a SQL WHERE clause.

    Args:
        user_a: UUID string of the first user.
        user_b: UUID string of the second user.

    Returns:
        Tuple (lo, hi) where lo <= hi lexicographically.
    """
    lo = min(user_a, user_b)
    hi = max(user_a, user_b)
    return lo, hi


# ---------------------------------------------------------------------------
# Friendship helper
#
# Friendship check: inline SQL rather than a separate knotify_db helper
# because no is_friend() function exists in knotify_db v1.  The friendships
# table stores canonical pairs (user_a < user_b per migration 0004 CHECK
# constraint), so a single SELECT on the canonical pair covers both directions.
# ---------------------------------------------------------------------------


def _is_friend(conn: Any, user_a: str, user_b: str) -> bool:
    """Return True if a friendship row exists for the canonical pair.

    The friendships table (migration 0004) stores rows with user_a < user_b
    enforced by a CHECK constraint, so there is exactly one row per friendship
    and a single parameterised SELECT on the canonical pair is sufficient.

    Args:
        conn:   An active psycopg2 connection.
        user_a: UUID of first user.
        user_b: UUID of second user.

    Returns:
        True if the friendship exists; False otherwise.
    """
    lo, hi = _canonical_pair(user_a, user_b)
    sql = (
        "SELECT 1 FROM friendships "
        "WHERE user_a = %s AND user_b = %s "
        "LIMIT 1"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (lo, hi))
        return cur.fetchone() is not None


# ---------------------------------------------------------------------------
# createOrGetRoom handler — §5.4.1 flow
# ---------------------------------------------------------------------------


def _handle_create_or_get_room(event: dict) -> dict:
    """
    Implement the §5.4.1 idempotent room-creation flow.

    Steps:
      1. Extract caller and otherUserId from the event; reject SELF_CHAT.
      2. Open Aurora connection; check block (knotify_obs.is_blocked) — reject BLOCKED.
      3. Check friendship (_is_friend) — reject NOT_FRIENDS.
      4. Compute room_id = knotify_obs.chat_room_id(caller, other).
      5. TransactWriteItems: PutItem(ChatRooms) + 2 x PutItem(ChatRoomMembership),
         all conditional on attribute_not_exists(room_id) for ChatRooms and
         attribute_not_exists(user_id) for each ChatRoomMembership row.
      6. On success: return the new room dict.
      7. On TransactionCanceledException whose first reason is ConditionalCheckFailed
         (room already exists): GetItem(ChatRooms, room_id) and return the existing row.

    Args:
        event: AppSync Lambda resolver event dict.

    Returns:
        ChatRoom dict on success, or an Unauthorized error dict on failure.
    """
    caller_id: str = event.get("identity", {}).get("sub", "")
    other_user_id: str = event.get("arguments", {}).get("otherUserId", "")

    # 1. Self-chat guard
    if caller_id == other_user_id:
        return _unauthorized_response(REASON_SELF_CHAT)

    conn = _get_conn()
    try:
        # 2. Block check (bidirectional)
        if is_blocked(conn, caller_id, other_user_id):
            return _unauthorized_response(REASON_BLOCKED)

        # 3. Friendship check
        if not _is_friend(conn, caller_id, other_user_id):
            return _unauthorized_response(REASON_NOT_FRIENDS)
    finally:
        # Release any implicit transaction; connection stays cached for reuse.
        try:
            conn.rollback()
        except Exception:
            pass

    # 4. Deterministic room ID
    room_id: str = chat_room_id(caller_id, other_user_id)
    lo, hi = _canonical_pair(caller_id, other_user_id)

    ddb = _get_dynamodb()

    # 5. Atomic write — ChatRooms row + 2 ChatRoomMembership rows.
    #    All three Put items are conditional so the operation is idempotent:
    #    - ChatRooms: attribute_not_exists(room_id)
    #    - ChatRoomMembership: attribute_not_exists(user_id)  (per-row SK is room_id)
    transact_items = [
        {
            "Put": {
                "TableName": "ChatRooms",
                "Item": {
                    "room_id": {"S": room_id},
                    "user_a": {"S": lo},
                    "user_b": {"S": hi},
                    "status": {"S": "active"},
                    "friendship_active": {"BOOL": True},
                },
                "ConditionExpression": "attribute_not_exists(room_id)",
            }
        },
        {
            "Put": {
                "TableName": "ChatRoomMembership",
                "Item": {
                    "user_id": {"S": caller_id},
                    "room_id": {"S": room_id},
                },
                "ConditionExpression": "attribute_not_exists(user_id)",
            }
        },
        {
            "Put": {
                "TableName": "ChatRoomMembership",
                "Item": {
                    "user_id": {"S": other_user_id},
                    "room_id": {"S": room_id},
                },
                "ConditionExpression": "attribute_not_exists(user_id)",
            }
        },
    ]

    try:
        ddb.transact_write_items(TransactItems=transact_items)
        logger.info(
            "create_or_get_room_created",
            extra={"room_id": room_id, "caller_id": caller_id},
        )
        return {
            "room_id": room_id,
            "user_a": lo,
            "user_b": hi,
            "status": "active",
            "friendship_active": True,
        }
    except Exception as exc:
        # 7. Detect ConditionalCheckFailed — room already exists.
        #    DynamoDB raises TransactionCanceledException with a CancellationReasons
        #    list; at least one entry has Code == "ConditionalCheckFailed".
        if _is_conditional_check_failed(exc):
            logger.info(
                "create_or_get_room_exists",
                extra={"room_id": room_id, "caller_id": caller_id},
            )
            return _fetch_existing_room(ddb, room_id)
        # Any other DynamoDB error propagates — Lambda will return a 500 to AppSync.
        raise


def _is_conditional_check_failed(exc: Exception) -> bool:
    """Return True when exc is a DynamoDB TransactionCanceledException with
    at least one ConditionalCheckFailed cancellation reason.

    Works with both botocore.exceptions.ClientError (production) and
    plain Exception subclasses with a matching response attribute (unit tests).
    """
    response = getattr(exc, "response", None)
    if response is None:
        return False
    error_code = response.get("Error", {}).get("Code", "")
    if error_code != "TransactionCanceledException":
        return False
    reasons = response.get("CancellationReasons", [])
    return any(r.get("Code") == "ConditionalCheckFailed" for r in reasons)


def _fetch_existing_room(ddb: Any, room_id: str) -> dict:
    """GetItem the existing ChatRooms row and return it as a plain dict.

    Args:
        ddb:     Boto3 DynamoDB client.
        room_id: The room's hash key.

    Returns:
        ChatRoom dict decoded from the DynamoDB item.
    """
    response = ddb.get_item(
        TableName="ChatRooms",
        Key={"room_id": {"S": room_id}},
    )
    item = response.get("Item", {})
    return {
        "room_id": item.get("room_id", {}).get("S", room_id),
        "user_a": item.get("user_a", {}).get("S", ""),
        "user_b": item.get("user_b", {}).get("S", ""),
        "status": item.get("status", {}).get("S", ""),
        "friendship_active": item.get("friendship_active", {}).get("BOOL", False),
    }


# ---------------------------------------------------------------------------
# AppSync resolver dispatcher
#
# The single entry point for all (typeName, fieldName) pairs routed to this
# Lambda via the AppSync data source configuration.
#
# Return value conventions for AppSync Lambda resolvers:
#   Success  → any serialisable dict / list (AppSync merges it into the
#               GraphQL response data).
#   Error    → {"errorType": "<ErrorCode>", "message": "<human-readable>",
#               "reason": "<machine-code>"}  — AppSync surfaces these as
#               GraphQL errors with extensions.errorType and extensions.reason.
# ---------------------------------------------------------------------------


def _dispatch(event: dict) -> Any:
    """
    Route the AppSync resolver event to the appropriate sub-handler.

    Wired so far:
        (Mutation, createOrGetRoom) → _handle_create_or_get_room  [story 8.3]

    Later stories add:
        (Mutation, sendMessage)     → _handle_send_message         [story 8.4]
        (Query,    listMyRooms)     → _handle_list_my_rooms        [story 8.5]
        ...

    Args:
        event: AppSync Lambda resolver event dict.

    Returns:
        A dict that AppSync merges into the GraphQL response.
    """
    type_name: str = event.get("typeName", "")
    field_name: str = event.get("fieldName", "")

    logger.info(
        "chat_resolver_dispatch",
        extra={"type_name": type_name, "field_name": field_name},
    )

    if type_name == "Mutation" and field_name == "createOrGetRoom":
        return _handle_create_or_get_room(event)

    return {
        "errorType": "Unimplemented",
        "message": f"{type_name}.{field_name} not yet implemented",
    }


# ---------------------------------------------------------------------------
# Lambda entrypoint
# ---------------------------------------------------------------------------


@require_profile_complete_appsync
def handler(event: dict, context: object) -> Any:
    """
    knotify-chat-resolver Lambda entrypoint.

    Validates the caller's profile-completion claim (via
    @require_profile_complete_appsync) and dispatches to the appropriate
    sub-handler based on (typeName, fieldName).

    Args:
        event:   AppSync Lambda resolver event dict.
        context: Lambda context object (unused).

    Returns:
        AppSync resolver response — a serialisable dict or list on success,
        or an error dict with errorType/message on failure.
    """
    user_id: str = (
        event.get("identity", {}).get("sub", "<unknown>")
    )

    logger.info(
        "chat_resolver_invoked",
        extra={
            "user_id": user_id,
            "type_name": event.get("typeName"),
            "field_name": event.get("fieldName"),
        },
    )

    return _dispatch(event)
