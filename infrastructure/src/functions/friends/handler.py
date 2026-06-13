"""
knotify-friends Lambda handler.

Implements seven HTTP API Gateway v2 routes:
  GET    /v1/friends                          — block-filtered friend list
  DELETE /v1/friends/{userId}                 — remove a friendship
  GET    /v1/friend-requests                  — block-filtered incoming/outgoing requests
  POST   /v1/friend-requests                  — send a friend request (block-aware)
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

import json
import os
import re
from typing import Any

import psycopg2.errors
import knotify_db
from knotify_obs import block_filter, init_logger, is_blocked, with_edge_secret

# ---------------------------------------------------------------------------
# Module-level singletons (cold-start optimization)
# ---------------------------------------------------------------------------

logger = init_logger("knotify_friends")

_DB_SECRET_NAME: str = os.environ.get("DB_SECRET_NAME", "")

_conn = None


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
_SELECT_FRIEND_REQUESTS_SQL = (
    "SELECT fr.request_id, fr.from_user_id, fr.to_user_id, fr.status, fr.created_at "
    "FROM friend_requests fr "
    "WHERE (fr.from_user_id = %s::uuid OR fr.to_user_id = %s::uuid) "
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

    return _json_response(200, {"deleted": True})


def _handle_get_friend_requests(event: dict, user_id: str, user_sex: str) -> dict:
    """
    GET /v1/friend-requests — return block-filtered pending requests for the caller.

    Shows both incoming (to_user_id = me) and outgoing (from_user_id = me) requests.
    Block filter applied against both from_user_id and to_user_id.
    """
    bf_from = block_filter("fr.from_user_id")
    bf_to = block_filter("fr.to_user_id")

    sql = _SELECT_FRIEND_REQUESTS_SQL.format(bf_from=bf_from, bf_to=bf_to)

    # Parameters:
    #   %s::uuid (from_user_id = me)
    #   %s::uuid (to_user_id   = me)
    #   %s, %s for bf_from (other_user = fr.from_user_id; bind me twice)
    #   %s, %s for bf_to   (other_user = fr.to_user_id;   bind me twice)
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

    requests = [_row_to_dict(row, description) for row in rows]
    return _json_response(200, {"friend_requests": requests})


def _handle_post_friend_requests(event: dict, user_id: str, user_sex: str) -> dict:
    """
    POST /v1/friend-requests — send a friend request.

    Body: {"toUserId": "<uuid>"}

    Transaction semantics (non-negotiable per story 6.2 PRD):
      BEGIN (via rls_context) →
        is_blocked(conn, requester, target) — inside the same transaction so a
          concurrent block cannot race the request through →
        if blocked → ROLLBACK (implicit via exception path) → 409 {"error":"blocked"}
        INSERT INTO friend_requests →
        if UniqueViolation → ROLLBACK → 409 {"error":"already_pending"}
      COMMIT → 201 with the new request row
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
            # Block check inside the same transaction
            if is_blocked(conn, requester_id, target_user_id):
                return _json_response(409, {"error": "blocked"})

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

    except Exception as exc:
        # Re-raise if it's not one of the handled 409 paths.
        # The 409 returns above exit the with-block cleanly via rls_context's
        # commit; only unexpected exceptions fall through here.
        conn.close()
        global _conn
        _conn = None
        raise

    new_request = _row_to_dict(row, description) if row else {}
    return _json_response(201, new_request)


def _handle_accept_friend_request(
    event: dict, request_id: str, user_id: str, user_sex: str
) -> dict:
    """
    POST /v1/friend-requests/{id}/accept — accept a pending request.

    Transaction semantics:
      BEGIN →
        SELECT ... FOR UPDATE (locks the row, prevents concurrent double-accept) →
        if not found → 404 {"error":"not_found"} (row was deleted by a block)
        UPDATE friend_requests SET status = 'accepted'
        INSERT INTO friendships using lex-min/max canonical ordering
      COMMIT → 200
    """
    conn = _get_conn()
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

    except Exception:
        conn.close()
        global _conn
        _conn = None
        raise

    return _json_response(200, {"accepted": True, "request_id": request_id})


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
def handler(event: dict, context: object) -> dict:
    """
    knotify-friends Lambda entrypoint.

    Validates the edge secret (via @with_edge_secret), extracts user identity
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
