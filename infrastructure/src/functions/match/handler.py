"""
knotify-match Lambda handler.

Implements two HTTP API Gateway v2 routes (wired in story 7.5):
  POST /v1/match/search   — candidate search with preference-vector ranking
  GET  /v1/match/deck     — swipe-deck with cursor pagination (story 7.2)

All routes require a valid Cognito JWT (enforced by the HTTP API JWT
authorizer) and the x-knotify-edge-secret header (enforced by the
@with_edge_secret decorator).

Design notes:
  - User identity comes from the JWT sub claim — never from URL path or body.
  - The module-level _conn is reused across warm invocations (cold-start
    friendly). rls_context() manages transaction lifetime; GUCs are SET LOCAL
    and expire when the transaction ends so there is no leakage across requests.
  - block_filter() lives in knotify_obs (not knotify_db); stories 7.1 and
    7.2 import from knotify_obs.block_filter.
  - POST /v1/match/search queries users directly (not deck_view), because
    search exposes ad-hoc filter combinations and benefits from the HNSW index
    on users.preference_vector.
  - The explicit u.sex predicate is OMITTED from the search SQL — RLS enforces
    opposite-sex visibility on the users table, so a separate predicate would
    be redundant. A comment in _build_search_sql notes this.
  - Ranking falls back from cosine ORDER BY to created_at DESC when the
    requester's preference_vector is NULL or all zeros (see _is_empty_vector).

Dependencies (Lambda layers):
  - knotify_obs: init_logger, with_edge_secret, require_profile_complete,
                 block_filter
  - knotify_db:  get_connection, rls_context
"""

from __future__ import annotations

import json
import os
from typing import Any

import knotify_db
from knotify_obs import block_filter, init_logger, require_profile_complete, with_edge_secret

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
# Result columns returned for each candidate in the search response.
#
# Only deck-facing fields are returned — keeps the payload lean and mirrors
# the shape used in phase-6 bookmark/friend response objects.
# ---------------------------------------------------------------------------

_SEARCH_RESULT_COLS = (
    "u.user_id, u.username, u.age, u.religion, "
    "u.current_residence_country, u.resident_country_code, "
    "u.current_residence_city, u.job_title, "
    "u.photo_url, u.chosen_profile_avatar, "
    "u.created_at"
)

# ---------------------------------------------------------------------------
# Pure helper functions (no I/O — fully unit-testable)
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


def _row_to_dict(row: tuple, description) -> dict:
    """Convert a psycopg2 row tuple to a dict using cursor description."""
    return {col[0]: val for col, val in zip(description, row)}


def _validate_search_body(body: dict) -> str | None:
    """
    Validate the POST /v1/match/search request body.

    Args:
        body: Parsed JSON dict from the request.

    Returns:
        None if the body is valid.
        An error-key string (e.g. "countries_invalid") if validation fails.
        The returned string names the failing field so callers can build
        structured error responses.
    """
    # countries: must be a list of CHAR(2) codes (exactly 2 uppercase letters)
    countries = body.get("countries")
    if not isinstance(countries, list):
        return "countries must be a list of CHAR(2) codes"
    for code in countries:
        if not isinstance(code, str) or len(code) != 2:
            return "countries must contain CHAR(2) codes only (e.g. 'GB', 'PK')"

    # religion: must be a string
    religion = body.get("religion")
    if not isinstance(religion, str):
        return "religion must be a string"

    # age_min / age_max: must be integers (bool is a subtype of int — exclude it)
    age_min = body.get("age_min")
    age_max = body.get("age_max")
    if not isinstance(age_min, int) or isinstance(age_min, bool):
        return "age_min must be an integer"
    if not isinstance(age_max, int) or isinstance(age_max, bool):
        return "age_max must be an integer"
    if age_min > age_max:
        return "age_min must not be greater than age_max"

    return None


def _is_empty_vector(vec: list[float] | None) -> bool:
    """
    Return True if the vector should be treated as empty/unset.

    A vector is considered empty when:
      - It is None (NULL in Postgres)
      - It is an empty list
      - Every element is zero

    Used to decide whether to use cosine ordering or fall back to created_at DESC.
    """
    if not vec:
        return True
    return all(v == 0.0 for v in vec)


def _build_search_sql(
    *,
    user_id: str,
    user_sex: str,
    countries: list[str],
    religion: str,
    age_min: int,
    age_max: int,
    requester_vector: list[float] | None,
) -> tuple[str, tuple]:
    """
    Build the parameterised SQL SELECT for POST /v1/match/search.

    Returns:
        (sql_string, params_tuple)

    SQL design:
      - Queries users table directly (not deck_view) — benefits from the HNSW
        index on users.preference_vector for the cosine ORDER BY path.
      - Aliases the table as u (required by block_filter("u.user_id")).
      - The explicit u.sex predicate is OMITTED: RLS enforces opposite-sex
        visibility on the users table for the app_user role, so a separate
        WHERE clause is unnecessary and would be redundant.
      - Block filtering via knotify_obs.block_filter("u.user_id"); the caller
        must bind two extra %s params (requester user_id twice) for the
        NOT EXISTS subquery.
      - Ranking: cosine distance (preference_vector <=> requester_vector) when
        the requester has a non-empty vector; falls back to ORDER BY created_at
        DESC when the vector is NULL or all zeros (empty-vector fallback).
      - LIMIT 50 (v1 cap).
    """
    use_cosine = not _is_empty_vector(requester_vector)
    bf = block_filter("u.user_id")

    params: list[Any] = []

    if use_cosine:
        # The ::vector cast lets psycopg2's default list adapter bind the
        # Python list as a pgvector literal without register_vector().
        order_clause = "ORDER BY u.preference_vector <=> %s::vector ASC"
        params.append(str(requester_vector))
    else:
        order_clause = "ORDER BY u.created_at DESC"

    sql = (
        f"SELECT {_SEARCH_RESULT_COLS} "
        f"FROM users u "
        f"WHERE u.deleted_at IS NULL "
        f"  AND u.profile_complete_verified = true "
        # u.sex predicate intentionally omitted: RLS on the users table enforces
        # opposite-sex visibility for the app_user role; adding an explicit
        # predicate would be redundant (and could mask an RLS misconfiguration).
        f"  AND u.religion = %s "
        f"  AND u.resident_country_code = ANY(%s) "
        f"  AND u.age >= %s "
        f"  AND u.age <= %s "
        f"  AND {bf} "
        f"{order_clause} "
        f"LIMIT 50"
    )

    # Parameters must be appended in the order the %s placeholders appear:
    # 1. (optional) requester_vector for the cosine ORDER BY — already appended above
    # 2. religion
    # 3. countries array
    # 4. age_min
    # 5. age_max
    # 6. user_id (first %s for block_filter NOT EXISTS — blocked_id = %s)
    # 7. user_id (second %s for block_filter NOT EXISTS — blocker_id = %s)
    params.extend([
        religion,
        countries,
        age_min,
        age_max,
        user_id,
        user_id,
    ])

    return sql, tuple(params)


# ---------------------------------------------------------------------------
# Sub-handlers
# ---------------------------------------------------------------------------


def _handle_post_match_search(event: dict, user_id: str, user_sex: str) -> dict:
    """
    POST /v1/match/search — candidate search with optional preference-vector ranking.

    Request body (JSON):
        {
            "countries": ["GB", "PK"],   // list of CHAR(2) resident_country_code values
            "religion": "Islam",         // string
            "age_min": 22,               // int
            "age_max": 35                // int
        }

    Steps:
      1. Parse and validate the request body.
      2. Open a DB connection (cached across warm invocations).
      3. Inside rls_context: fetch the requester's preference_vector from users.
      4. Build the search SQL (cosine or fallback ordering based on the vector).
      5. Execute the search and return up to 50 candidates.
    """
    # -- Step 1: parse and validate the body --
    body_raw = event.get("body")
    if not body_raw:
        return _json_response(400, {"error": "missing_body"})

    try:
        body: dict = json.loads(body_raw)
    except (json.JSONDecodeError, TypeError):
        return _json_response(400, {"error": "invalid_json"})

    if not isinstance(body, dict):
        return _json_response(400, {"error": "body_must_be_object"})

    validation_error = _validate_search_body(body)
    if validation_error:
        return _json_response(400, {"error": validation_error})

    countries: list[str] = body["countries"]
    religion: str = body["religion"]
    age_min: int = body["age_min"]
    age_max: int = body["age_max"]

    # -- Step 2: get DB connection --
    conn = _get_conn()

    # -- Steps 3–5: run inside RLS context --
    try:
        with knotify_db.rls_context(conn, user_id, user_sex):
            # Step 3: fetch the requester's preference_vector
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT preference_vector FROM users WHERE user_id = %s::uuid",
                    (user_id,),
                )
                row = cur.fetchone()

            requester_vector: list[float] | None = row[0] if row else None

            # Step 4: build the search SQL
            sql, params = _build_search_sql(
                user_id=user_id,
                user_sex=user_sex,
                countries=countries,
                religion=religion,
                age_min=age_min,
                age_max=age_max,
                requester_vector=requester_vector,
            )

            # Step 5: execute and fetch candidates
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()
                description = cur.description

    except Exception:
        conn.close()
        global _conn
        _conn = None
        raise

    results = [_row_to_dict(row, description) for row in rows]

    logger.info(
        "match_search_complete",
        extra={
            "user_id": user_id,
            "result_count": len(results),
            "vector_empty": _is_empty_vector(requester_vector),
        },
    )

    return _json_response(200, {"results": results})


# ---------------------------------------------------------------------------
# Route dispatcher
# ---------------------------------------------------------------------------

_PATH_MATCH_SEARCH = "/v1/match/search"


def _dispatch(event: dict, user_id: str, user_sex: str) -> dict:
    """
    Route the request to the appropriate sub-handler.

    Routes:
      POST /v1/match/search  → _handle_post_match_search  (story 7.1)
      GET  /v1/match/deck    → _handle_get_match_deck      (story 7.2, not yet wired)

    All unknown routes return 404 {"error": "not_found"}.
    """
    http = event.get("requestContext", {}).get("http", {})
    method: str = http.get("method", "").upper()
    path: str = http.get("path", "")

    if method == "POST" and path == _PATH_MATCH_SEARCH:
        return _handle_post_match_search(event, user_id, user_sex)

    # GET /v1/match/deck is added in story 7.2
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
    knotify-match Lambda entrypoint.

    Validates the edge secret (via @with_edge_secret decorator), checks that
    the caller's profile is complete (via @require_profile_complete), extracts
    the user identity from JWT claims, and dispatches to the appropriate
    sub-handler.

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
