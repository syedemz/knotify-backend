"""
knotify-match Lambda handler — story 7.0 scaffold.

This is an empty dispatcher that returns 404 for every route. Routes are
wired in story 7.5 after stories 7.1 (search) and 7.2 (deck) land.

Implements two HTTP API Gateway v2 routes (to be connected in 7.5):
  POST /v1/match/search   — candidate search with preference-vector ranking
  GET  /v1/match/deck     — swipe-deck with cursor pagination

All routes require a valid Cognito JWT (enforced by the HTTP API JWT
authorizer) and the x-knotify-edge-secret header (enforced by the
@with_edge_secret decorator).

Design notes:
  - User identity comes from the JWT sub claim — never from URL path or body.
  - The module-level _conn is reused across warm invocations (cold-start
    friendly). Stories 7.1 and 7.2 will populate the sub-handlers.
  - block_filter() lives in knotify_obs (not knotify_db); stories 7.1 and
    7.2 import from knotify_obs.block_filter.

Dependencies (Lambda layers):
  - knotify_obs: init_logger, with_edge_secret
  - knotify_db:  get_connection, rls_context
"""

from __future__ import annotations

import json
import os
from typing import Any

import knotify_db
from knotify_obs import init_logger, with_edge_secret

# ---------------------------------------------------------------------------
# Module-level singletons (cold-start optimization)
# ---------------------------------------------------------------------------

logger = init_logger("knotify_match")

_DB_SECRET_NAME: str = os.environ.get("DB_SECRET_NAME", "")

# Module-level connection (reused across warm invocations)
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
# Pure helper functions (no DB access, no I/O — fully unit-testable)
# ---------------------------------------------------------------------------


def _get_user_id_and_sex(event: dict) -> tuple[str, str]:
    """
    Extract (user_id, user_sex) from the HTTP API JWT claims.

    The HTTP API JWT authorizer places decoded claims at:
        event["requestContext"]["authorizer"]["jwt"]["claims"]

    Raises KeyError if "sub" is absent (misconfigured authorizer).
    """
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


# ---------------------------------------------------------------------------
# Route dispatcher
#
# Story 7.0: empty — all paths return 404.
# Story 7.1 will add POST /v1/match/search.
# Story 7.2 will add GET /v1/match/deck.
# Story 7.5 wires both routes in the HTTP API.
# ---------------------------------------------------------------------------


def _dispatch(event: dict, user_id: str, user_sex: str) -> dict:
    """
    Route the request to the appropriate sub-handler.

    Routes (populated in later stories):
      POST /v1/match/search  → _handle_post_match_search  (story 7.1)
      GET  /v1/match/deck    → _handle_get_match_deck      (story 7.2)

    All unknown routes return 404 {"error": "not_found"}.
    """
    http = event.get("requestContext", {}).get("http", {})
    method: str = http.get("method", "").upper()
    path: str = http.get("path", "")

    # No routes wired yet — 7.1 and 7.2 will add branches here.
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
    knotify-match Lambda entrypoint.

    Validates the edge secret (via @with_edge_secret decorator), extracts the
    user identity from JWT claims, and dispatches to the appropriate sub-handler.

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
        "match_request",
        extra={
            "user_id": user_id,
            "method": event.get("requestContext", {}).get("http", {}).get("method"),
            "path": event.get("requestContext", {}).get("http", {}).get("path"),
        },
    )

    return _dispatch(event, user_id, user_sex)
