"""
knotify-bookmarks Lambda handler.

Implements three HTTP API Gateway v2 routes:
  GET    /v1/bookmarks              — block-filtered bookmark list
  POST   /v1/bookmarks              — bookmark a user (idempotent, block-aware)
  DELETE /v1/bookmarks/{userId}     — remove a bookmark (idempotent)

All routes require a valid Cognito JWT (enforced by the HTTP API JWT authorizer)
and the x-knotify-edge-secret header (enforced by the @with_edge_secret decorator).

Design notes:
  - User identity comes from the JWT sub claim — never from URL path or request body.
  - RLS GUCs are set on every request via knotify_db.rls_context() so PostgreSQL
    row-level security enforces visibility at the DB layer.
  - The module-level _conn is reused across warm invocations (cold-start friendly).
    rls_context() manages transaction lifetime; GUCs are SET LOCAL and expire
    when the transaction ends so there is no leakage across requests.
  - Block-aware behaviour (PRD story 6.3 / Brainstorm B3 Option B):
      POST /v1/bookmarks: is_blocked() is called INSIDE the same transaction as the
        INSERT so a concurrent block cannot race the request through. If blocked →
        409 {"error":"blocked"}.
      GET /v1/bookmarks: block_filter("bookmarks.bookmarked_user_id") embedded in
        the SELECT silently filters out pairs where a block exists in either direction.
  - POST idempotency: INSERT ... ON CONFLICT (user_id, bookmarked_user_id)
    DO NOTHING RETURNING *. Empty RETURNING (conflict hit) is treated as success
    (HTTP 200) — not 409.
  - DELETE is idempotent: returns HTTP 200 even when no row was deleted.

Dependencies (Lambda layers):
  - knotify_obs: init_logger, with_edge_secret, is_blocked, block_filter
  - knotify_db:  get_connection, rls_context
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import knotify_db
from knotify_obs import block_filter, init_logger, is_blocked, with_edge_secret

# ---------------------------------------------------------------------------
# Module-level singletons (cold-start optimization)
# ---------------------------------------------------------------------------

logger = init_logger("knotify_bookmarks")

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
# Deck-view fields returned for each bookmarked user in the list
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

# GET /v1/bookmarks — join bookmarks → users with block filter on bookmarked_user_id.
# block_filter("bk.bookmarked_user_id") is in the _blocks.py whitelist; the alias
# prefix is mandatory because Postgres correlated subqueries must reference the
# outer FROM alias, not the bare table name.
_SELECT_BOOKMARKS_SQL = (
    "SELECT {cols} "
    "FROM bookmarks bk "
    "JOIN users u ON u.user_id = bk.bookmarked_user_id "
    "WHERE bk.user_id = %s::uuid "
    "  AND {{bf_bookmarked}} "
    "  AND u.deleted_at IS NULL"
).format(cols=_DECK_VIEW_COLS)

# POST /v1/bookmarks — idempotent upsert using the composite PK as the conflict target.
_INSERT_BOOKMARK_SQL = """
INSERT INTO bookmarks (user_id, bookmarked_user_id, created_at)
VALUES (%s::uuid, %s::uuid, NOW())
ON CONFLICT (user_id, bookmarked_user_id) DO NOTHING
RETURNING user_id, bookmarked_user_id, created_at
"""

# DELETE /v1/bookmarks/{userId}
_DELETE_BOOKMARK_SQL = """
DELETE FROM bookmarks
WHERE user_id = %s::uuid
  AND bookmarked_user_id = %s::uuid
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


# ---------------------------------------------------------------------------
# Sub-handlers
# ---------------------------------------------------------------------------


def _handle_get_bookmarks(event: dict, user_id: str, user_sex: str) -> dict:
    """
    GET /v1/bookmarks — return the block-filtered bookmark list for the caller.

    Joins bookmarks → users and applies block_filter on bookmarked_user_id so
    any bookmarked user who has since blocked the requester (or vice versa) is
    silently excluded from the response.
    """
    bf_bookmarked = block_filter("bk.bookmarked_user_id")
    sql = _SELECT_BOOKMARKS_SQL.format(bf_bookmarked=bf_bookmarked)

    # Parameters: user_id (WHERE bk.user_id = %s), then user_id twice for
    # block_filter's two %s bind parameters.
    params = (user_id, user_id, user_id)

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

    bookmarks = [_row_to_dict(row, description) for row in rows]
    return _json_response(200, {"bookmarks": bookmarks})


def _handle_post_bookmark(event: dict, user_id: str, user_sex: str) -> dict:
    """
    POST /v1/bookmarks — bookmark a user (idempotent, block-aware).

    Body: {"userId": "<uuid>"}

    Transaction semantics:
      BEGIN (via rls_context) →
        is_blocked(conn, requester, target) — inside the same transaction so a
          concurrent block cannot race the request through →
        if blocked → ROLLBACK (implicit via exception path) → 409 {"error":"blocked"}
        INSERT INTO bookmarks ON CONFLICT DO NOTHING RETURNING *
        if RETURNING is empty (conflict) → treat as success → 200 {"bookmarked": true}
        if RETURNING has a row → 200 with the new row fields
      COMMIT → 200
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
        body.get("userId", "").strip()
        if isinstance(body.get("userId"), str)
        else ""
    )
    if not target_user_id:
        return _json_response(400, {"error": "userId_required"})

    conn = _get_conn()
    try:
        with knotify_db.rls_context(conn, user_id, user_sex):
            # Block check inside the same transaction
            if is_blocked(conn, user_id, target_user_id):
                return _json_response(409, {"error": "blocked"})

            with conn.cursor() as cur:
                cur.execute(_INSERT_BOOKMARK_SQL, (user_id, target_user_id))
                row = cur.fetchone()
                description = cur.description
    except Exception:
        conn.close()
        global _conn
        _conn = None
        raise

    if row is not None:
        return _json_response(200, _row_to_dict(row, description))
    # ON CONFLICT DO NOTHING — empty RETURNING is treated as success
    return _json_response(200, {"bookmarked": True})


def _handle_delete_bookmark(event: dict, user_id: str, user_sex: str) -> dict:
    """
    DELETE /v1/bookmarks/{userId} — remove a bookmark row.

    Idempotent — returns HTTP 200 even when no row was deleted.
    """
    path_params = event.get("pathParameters") or {}
    target_user_id = path_params.get("userId", "").strip()
    if not target_user_id:
        return _json_response(400, {"error": "userId_required"})

    conn = _get_conn()
    try:
        with knotify_db.rls_context(conn, user_id, user_sex):
            with conn.cursor() as cur:
                cur.execute(_DELETE_BOOKMARK_SQL, (user_id, target_user_id))
    except Exception:
        conn.close()
        global _conn
        _conn = None
        raise

    return _json_response(200, {"deleted": True})


# ---------------------------------------------------------------------------
# Route dispatcher
# ---------------------------------------------------------------------------

_PATH_BOOKMARKS = "/v1/bookmarks"

# Matches /v1/bookmarks/{userId}  (UUID pattern)
_BOOKMARKS_DELETE_RE = re.compile(
    r"^/v1/bookmarks/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)


def _dispatch(event: dict, user_id: str, user_sex: str) -> dict:
    """Route the request to the appropriate sub-handler."""
    http = event.get("requestContext", {}).get("http", {})
    method: str = http.get("method", "").upper()
    path: str = http.get("path", "")

    if method == "GET" and path == _PATH_BOOKMARKS:
        return _handle_get_bookmarks(event, user_id, user_sex)

    if method == "POST" and path == _PATH_BOOKMARKS:
        return _handle_post_bookmark(event, user_id, user_sex)

    if method == "DELETE":
        m = _BOOKMARKS_DELETE_RE.match(path)
        if m:
            return _handle_delete_bookmark(
                {**event, "pathParameters": {"userId": m.group(1)}},
                user_id,
                user_sex,
            )

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
    knotify-bookmarks Lambda entrypoint.

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
        "bookmarks_request",
        extra={
            "user_id": user_id,
            "method": event.get("requestContext", {}).get("http", {}).get("method"),
            "path": event.get("requestContext", {}).get("http", {}).get("path"),
        },
    )

    return _dispatch(event, user_id, user_sex)
