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
  - GET /v1/match/deck reads from deck_view (aliased dv). RLS does NOT
    propagate through materialized views, so an explicit opposite-sex WHERE
    using the app.requesting_user_sex GUC is the sole enforcement mechanism
    on the deck path. rls_context() sets that GUC via SET LOCAL.

Dependencies (Lambda layers):
  - knotify_obs: init_logger, with_edge_secret, require_profile_complete,
                 block_filter
  - knotify_db:  get_connection, rls_context
"""

from __future__ import annotations

import json
import os
import urllib.parse
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

# Result columns for GET /v1/match/deck — mirrors search shape, but sourced
# from deck_view (alias dv). sex is included so the handler can log/assert;
# created_at is not in deck_view (not needed; deck orders by user_id).
_DECK_RESULT_COLS = (
    "dv.user_id, dv.username, dv.age, dv.religion, "
    "dv.current_residence_country, dv.resident_country_code, "
    "dv.current_residence_city, dv.job_title, "
    "dv.photo_url, dv.chosen_profile_avatar, "
    "dv.sex"
)

# Number of candidates per deck page.
_DECK_PAGE_SIZE = 20

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

    if use_cosine:
        # The ::vector cast lets psycopg2's default list adapter bind the
        # Python list as a pgvector literal without register_vector().
        order_clause = "ORDER BY u.preference_vector <=> %s::vector ASC"
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

    # Parameters must be in the order the %s placeholders appear in the SQL:
    # 1. religion                                      (WHERE u.religion = %s)
    # 2. countries array            (WHERE u.resident_country_code = ANY(%s))
    # 3. age_min                                          (WHERE u.age >= %s)
    # 4. age_max                                          (WHERE u.age <= %s)
    # 5. user_id   (first %s in block_filter NOT EXISTS — blocked_id = %s)
    # 6. user_id   (second %s in block_filter NOT EXISTS — blocker_id = %s)
    # 7. (cosine path only) requester_vector       (ORDER BY ... <=> %s::vector)
    params: list[Any] = [
        religion,
        countries,
        age_min,
        age_max,
        user_id,
        user_id,
    ]

    if use_cosine:
        params.append(str(requester_vector))

    return sql, tuple(params)


def _parse_deck_filters(raw: str) -> tuple[dict | None, str | None]:
    """
    URL-decode and validate the optional ?filters= query parameter.

    Args:
        raw: The raw (possibly URL-encoded) JSON string from the query string.

    Returns:
        (filters_dict, None)  when the string is valid JSON and passes
                              _validate_search_body.
        (None, error_string)  when JSON parsing fails or validation rejects
                              the content.

    Reuses _validate_search_body so deck and search share the same filter rules.
    """
    try:
        decoded = urllib.parse.unquote(raw)
        body = json.loads(decoded)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None, "filters must be valid url-encoded JSON"

    if not isinstance(body, dict):
        return None, "filters must be a JSON object"

    err = _validate_search_body(body)
    if err:
        return None, err

    return body, None


def _build_deck_sql(
    *,
    user_id: str,
    user_sex: str,
    cursor: str | None = None,
    filters: dict | None = None,
) -> tuple[str, tuple]:
    """
    Build the parameterised SQL SELECT for GET /v1/match/deck.

    Returns:
        (sql_string, params_tuple)

    SQL design:
      - Reads from deck_view aliased as dv (materialized view; already
        filtered to profile_complete_verified = true and deleted_at IS NULL).
      - Explicit opposite-sex WHERE using the app.requesting_user_sex GUC:
          WHERE dv.sex != current_setting('app.requesting_user_sex', true)
        This is mandatory because RLS does NOT propagate through materialized
        views — without this predicate, requester could see same-sex rows.
      - block_filter("dv.user_id") enforces mutual-block exclusion.
      - Optional hard filters (countries / religion / age) from ?filters= param.
      - Optional cursor: WHERE dv.user_id > %s::uuid for keyset pagination.
      - ORDER BY dv.user_id ASC, LIMIT 20.

    Parameter order in the returned tuple:
      1. block_filter first %s  (user_id — blocked_id direction)
      2. block_filter second %s (user_id — blocker_id direction)
      3. (optional) countries array     if filters present
      4. (optional) religion string     if filters present
      5. (optional) age_min int         if filters present
      6. (optional) age_max int         if filters present
      7. (optional) cursor UUID string  if cursor present
    """
    bf = block_filter("dv.user_id")

    params: list[Any] = [user_id, user_id]  # two slots for block_filter NOT EXISTS

    where_clauses = [
        "dv.sex != current_setting('app.requesting_user_sex', true)",
        bf,
    ]

    if filters:
        countries: list[str] = filters["countries"]
        religion: str = filters["religion"]
        age_min: int = filters["age_min"]
        age_max: int = filters["age_max"]

        where_clauses.append("dv.resident_country_code = ANY(%s)")
        params.append(countries)

        where_clauses.append("dv.religion = %s")
        params.append(religion)

        where_clauses.append("dv.age >= %s")
        params.append(age_min)

        where_clauses.append("dv.age <= %s")
        params.append(age_max)

    if cursor is not None:
        where_clauses.append("dv.user_id > %s::uuid")
        params.append(cursor)

    where_sql = "\n  AND ".join(where_clauses)

    sql = (
        f"SELECT {_DECK_RESULT_COLS} "
        f"FROM deck_view dv "
        f"WHERE {where_sql} "
        f"ORDER BY dv.user_id ASC "
        f"LIMIT {_DECK_PAGE_SIZE}"
    )

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


def _handle_get_match_deck(event: dict, user_id: str, user_sex: str) -> dict:
    """
    GET /v1/match/deck — swipe-deck with cursor pagination.

    Query parameters:
        cursor  (optional) — UUID string; returns rows after this user_id.
        filters (optional) — URL-encoded JSON; same structure as search body.
                             When present, hard filters are applied before
                             pagination.

    Steps:
      1. Parse optional query parameters (cursor, filters).
      2. Validate filters if present — reuses _validate_search_body via
         _parse_deck_filters.
      3. Open DB connection (cached across warm invocations).
      4. Inside rls_context: execute deck SQL against deck_view dv.
         The opposite-sex WHERE clause references app.requesting_user_sex GUC,
         which rls_context sets via SET LOCAL — this is the ONLY enforcement
         of opposite-sex visibility on the deck path (RLS does not propagate
         through materialized views).
      5. Build response with next_cursor = last user_id when batch is full,
         or null when fewer than _DECK_PAGE_SIZE rows returned.

    Response shape:
        {"results": [...], "next_cursor": "<uuid-or-null>"}
    """
    # -- Step 1: parse query parameters --
    qsp: dict = event.get("queryStringParameters") or {}
    cursor: str | None = qsp.get("cursor")
    filters_raw: str | None = qsp.get("filters")

    # -- Step 2: validate filters if supplied --
    filters: dict | None = None
    if filters_raw is not None:
        filters, parse_error = _parse_deck_filters(filters_raw)
        if parse_error:
            return _json_response(400, {"error": parse_error})

    # -- Step 3: get DB connection --
    conn = _get_conn()

    # -- Step 4: run inside RLS context --
    sql, params = _build_deck_sql(
        user_id=user_id,
        user_sex=user_sex,
        cursor=cursor,
        filters=filters,
    )

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

    # -- Step 5: build response with next_cursor --
    results = [_row_to_dict(row, description) for row in rows]
    next_cursor: str | None = (
        results[-1]["user_id"] if len(results) >= _DECK_PAGE_SIZE else None
    )

    logger.info(
        "match_deck_complete",
        extra={
            "user_id": user_id,
            "result_count": len(results),
            "cursor": cursor,
            "has_filters": filters is not None,
            "next_cursor": next_cursor,
        },
    )

    return _json_response(200, {"results": results, "next_cursor": next_cursor})


# ---------------------------------------------------------------------------
# Route dispatcher
# ---------------------------------------------------------------------------

_PATH_MATCH_SEARCH = "/v1/match/search"
_PATH_MATCH_DECK = "/v1/match/deck"


def _dispatch(event: dict, user_id: str, user_sex: str) -> dict:
    """
    Route the request to the appropriate sub-handler.

    Routes:
      POST /v1/match/search  → _handle_post_match_search  (story 7.1)
      GET  /v1/match/deck    → _handle_get_match_deck      (story 7.2)

    All unknown routes return 404 {"error": "not_found"}.
    """
    http = event.get("requestContext", {}).get("http", {})
    method: str = http.get("method", "").upper()
    path: str = http.get("path", "")

    if method == "POST" and path == _PATH_MATCH_SEARCH:
        return _handle_post_match_search(event, user_id, user_sex)

    if method == "GET" and path == _PATH_MATCH_DECK:
        return _handle_get_match_deck(event, user_id, user_sex)

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
