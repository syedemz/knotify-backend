"""
knotify-profile Lambda handler.

Implements four HTTP API Gateway v2 routes:
  GET  /v1/profile/me              — own full profile (all fields, RLS own-row exception)
  PATCH /v1/profile/me             — update own profile with immutable-field semantics
  GET  /v1/profiles?username=...   — case-insensitive username search, deck-view fields
  GET  /v1/profiles/{userId}       — look up any profile by ID, deck-view fields only

All routes require a valid Cognito JWT (enforced by the HTTP API JWT authorizer)
and the x-knotify-edge-secret header (enforced by the @with_edge_secret decorator).

Design notes:
  - User identity comes from the JWT sub claim — never from URL path or request body.
  - RLS GUCs are set on every request via knotify_db.rls_context() so PostgreSQL
    row-level security enforces opposite-sex visibility at the DB layer.
  - The module-level _conn is reused across warm invocations (cold-start friendly).
    rls_context() manages transaction lifetime; GUCs are SET LOCAL and expire
    when the transaction ends so there is no leakage across requests.
  - PATCH immutable-field semantics:
      1. Fetch the current row inside the same transaction.
      2. Check immutable fields — return 400 BEFORE any UPDATE if violations found.
         This fires BEFORE the DB trigger trg_users_immutable, giving the client a
         clean, structured error instead of a 500 from the DB RAISE.
      3. Apply the UPDATE for non-violating fields.
      4. If all 34 required fields are non-NULL after the update AND
         profile_complete_verified was false before, set profile_complete_verified = true
         in the same transaction.
      5. AFTER the with conn: block commits (outside the transaction), when the flag
         flipped false→true in step 4, call cognito-idp:AdminUpdateUserAttributes to
         set custom:profile_complete = "true" on the Cognito user. Best-effort: on any
         transient failure, log a structured warning and return 200 to the client.
         (See story 7.0b eventual-consistency footnote.)
  - deck_view fields (for GET /v1/profiles): user_id, age, chosen_profile_avatar,
    current_residence_city, current_residence_country, first_name, job_title,
    last_name, photo_url, profile_complete_verified, resident_country_code, sex,
    username. Does NOT include email, phone_number, or family fields.

Dependencies (Lambda layers):
  - knotify_obs: init_logger, with_edge_secret
  - knotify_db:  get_connection, rls_context
  - boto3: cognito-idp client and lambda client (used only on profile-completion flip)
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import boto3
import knotify_db
import psycopg2.extras
from knotify_db import encode_prefs
from knotify_obs import init_logger, with_edge_secret

# ---------------------------------------------------------------------------
# Module-level singletons (cold-start optimization)
# ---------------------------------------------------------------------------

logger = init_logger("knotify_profile")

_DB_SECRET_NAME: str = os.environ.get("DB_SECRET_NAME", "")
_USER_POOL_ID: str = os.environ.get("USER_POOL_ID", "")
_REFRESH_LAMBDA_ARN: str = os.environ.get("REFRESH_LAMBDA_ARN", "")

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
# Constants
# ---------------------------------------------------------------------------

# Immutable-after-set fields per architecture §5.7
_IMMUTABLE_FIELDS = frozenset({"first_name", "last_name", "sex", "birthday", "religion", "subsect"})

# Fields required for profile_complete_verified = true (story 7.0b — widened to 34).
# Must match the CHECK constraint in migration 0012 exactly (flat IS NOT NULL, no
# conditional predicates). marriage_time is NOT required; it gets DEFAULT 'Not Provided'
# via migration 0012 so it is never NULL.
_REQUIRED_FOR_COMPLETION = frozenset({
    # Identity (carried forward from original 5-field check)
    "first_name",
    "last_name",
    "sex",
    "birthday",
    "username",
    # Religion (matching-critical)
    "religion",
    "subsect",
    "religious_level",
    # Residence
    "current_residence_city",
    "current_residence_country",
    "resident_country_code",
    "district",
    # Education
    "education_level",
    "highest_degree",
    "high_school",
    "higher_secondary",
    "college_name",
    # Profession
    "job_title",
    "employer_name",
    "employment_type",
    "office_address",
    "professional_category",
    "salary_range",
    # Family
    "fathers_name",
    "fathers_job",
    "father_retired",
    "mothers_name",
    "mothers_job",
    "mother_retired",
    "family_residence_address",
    # Personal status
    "marital_status",
    "has_children",
    "move_abroad",
    "relation",
})

# Mutable fields accepted by PATCH (excludes identity, system, immutable, and
# read-only computed columns like age)
_MUTABLE_FIELDS = frozenset({
    "username",
    "phone_number",
    "chosen_profile_avatar",
    "photo_url",
    "college_name",
    "current_residence_city",
    "current_residence_country",
    "resident_country_code",
    "district",
    "education_level",
    "employer_name",
    "employment_type",
    "family_residence_address",
    "father_retired",
    "fathers_job",
    "fathers_name",
    "graduation_year",
    "has_children",
    "higher_secondary",
    "higher_secondary_passing_year",
    "highest_degree",
    "high_school",
    "high_school_passing_year",
    "job_title",
    "marital_status",
    "marriage_time",
    "mother_retired",
    "mothers_job",
    "mothers_name",
    "move_abroad",
    "office_address",
    "partners_religious_level",
    "professional_category",
    "relation",
    "religious_level",
    "salary_range",
    "preferences",
})

# All fields accepted by PATCH = mutable + immutable-after-set
_ALL_PATCH_FIELDS = _MUTABLE_FIELDS | _IMMUTABLE_FIELDS

# Deck-view field set — returned by GET /v1/profiles/* (not own profile)
_DECK_VIEW_FIELDS = frozenset({
    "user_id",
    "age",
    "chosen_profile_avatar",
    "current_residence_city",
    "current_residence_country",
    "first_name",
    "job_title",
    "last_name",
    "photo_url",
    "profile_complete_verified",
    "resident_country_code",
    "sex",
    "username",
})

# SQL for the own-profile SELECT (full row, RLS own-row exception applies)
_SELECT_OWN_PROFILE_SQL = """
SELECT
    user_id, first_name, last_name, sex, birthday, religion, subsect,
    username, profile_complete_verified, job_title, photo_url,
    chosen_profile_avatar, current_residence_city, current_residence_country,
    resident_country_code, district, education_level, employer_name,
    employment_type, family_residence_address, father_retired, fathers_job,
    fathers_name, graduation_year, has_children, higher_secondary,
    higher_secondary_passing_year, highest_degree, high_school,
    high_school_passing_year, marital_status, marriage_time, mother_retired,
    mothers_job, mothers_name, move_abroad, office_address,
    partners_religious_level, college_name, professional_category, relation,
    religious_level, salary_range, preferences, age, email, phone_number
FROM users
WHERE user_id = %s::uuid
  AND deleted_at IS NULL
"""

# SQL for the PATCH — fetch current values first (same txn)
_SELECT_FOR_UPDATE_SQL = """
SELECT
    user_id, first_name, last_name, sex, birthday, religion, subsect,
    username, profile_complete_verified, job_title, photo_url,
    chosen_profile_avatar, current_residence_city, current_residence_country,
    resident_country_code, district, education_level, employer_name,
    employment_type, family_residence_address, father_retired, fathers_job,
    fathers_name, graduation_year, has_children, higher_secondary,
    higher_secondary_passing_year, highest_degree, high_school,
    high_school_passing_year, marital_status, marriage_time, mother_retired,
    mothers_job, mothers_name, move_abroad, office_address,
    partners_religious_level, college_name, professional_category, relation,
    religious_level, salary_range, preferences, age, email, phone_number
FROM users
WHERE user_id = %s::uuid
  AND deleted_at IS NULL
FOR UPDATE
"""

# SQL for deck-view lookup by user_id
_SELECT_DECK_BY_ID_SQL = """
SELECT
    user_id, first_name, last_name, sex, age, chosen_profile_avatar,
    current_residence_city, current_residence_country, job_title, last_name,
    photo_url, profile_complete_verified, resident_country_code, username
FROM users
WHERE user_id = %s::uuid
  AND deleted_at IS NULL
  AND profile_complete_verified = true
"""

# SQL for deck-view lookup by username (case-insensitive exact match)
# Uses the partial unique index from migration 0010 for performance.
_SELECT_DECK_BY_USERNAME_SQL = """
SELECT
    user_id, first_name, last_name, sex, age, chosen_profile_avatar,
    current_residence_city, current_residence_country, job_title, last_name,
    photo_url, profile_complete_verified, resident_country_code, username
FROM users
WHERE lower(username) = lower(%s)
  AND deleted_at IS NULL
  AND profile_complete_verified = true
"""

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
    claims: dict = (
        event["requestContext"]["authorizer"]["jwt"]["claims"]
    )
    user_id: str = claims["sub"]
    user_sex: str = claims.get("custom:user_sex", "")
    return user_id, user_sex


def _check_immutable_fields(
    patch_data: dict[str, Any],
    current_row: dict[str, Any] | tuple,
) -> list[str]:
    """
    Return a list of immutable field names whose values the client is trying
    to change (i.e., field is already non-NULL in DB and new value differs).

    Args:
        patch_data:  The validated PATCH request body (field → new value).
        current_row: The current DB row (dict or indexable sequence with
                     string keys if dict, else keyed via description).

    Returns:
        List of field names that are violations. Empty list = no violations.
    """
    if not isinstance(current_row, dict):
        raise TypeError("current_row must be a dict; convert via _row_to_dict() first")

    violations: list[str] = []
    for field in _IMMUTABLE_FIELDS:
        if field not in patch_data:
            continue
        current_val = current_row.get(field)
        if current_val is None:
            continue
        new_val = patch_data[field]
        if str(new_val) == str(current_val):
            continue
        violations.append(field)

    return violations


def _should_set_profile_complete(proposed_row: dict[str, Any]) -> bool:
    """
    Return True iff all 34 required-for-completion fields are non-None in
    proposed_row (the row as it will look after the UPDATE is applied).

    The required set is _REQUIRED_FOR_COMPLETION (34 flat column names).
    No conditional predicates — the frozenset membership check matches the
    DB CHECK constraint in migration 0012 1:1.
    """
    return all(
        proposed_row.get(field) is not None
        for field in _REQUIRED_FOR_COMPLETION
    )


def _row_to_dict(row: tuple, description) -> dict:
    """Convert a psycopg2 row tuple to a dict using the cursor's description."""
    return {col[0]: val for col, val in zip(description, row)}


def _filter_to_deck_view(profile: dict) -> dict:
    """Return a copy of profile containing only deck-view fields."""
    return {k: v for k, v in profile.items() if k in _DECK_VIEW_FIELDS}


def _json_response(status_code: int, body: Any) -> dict:
    """Build a Lambda HTTP response dict."""
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str),
    }


def _write_cognito_profile_complete(user_id: str) -> None:
    """
    Best-effort call to set custom:profile_complete = "true" on the Cognito user.

    Called AFTER the with conn: block has committed the Aurora UPDATE.
    On any failure, logs a structured warning and returns — does NOT raise.
    The caller always returns HTTP 200 regardless of whether this call succeeds.

    Eventual-consistency footnote: a successful PATCH may briefly have
    Aurora=true and Cognito=false until this call lands.  The client must
    refresh the session to pick up the new claim anyway.  A failed call
    means the user stays gated until the next PATCH or a phase-11 backfill
    job sweeps users with Aurora=true / Cognito=false mismatches.
    """
    try:
        client = boto3.client("cognito-idp")
        client.admin_update_user_attributes(
            UserPoolId=_USER_POOL_ID,
            Username=user_id,
            UserAttributes=[{"Name": "custom:profile_complete", "Value": "true"}],
        )
        logger.info(
            "cognito_profile_complete_attribute_set",
            extra={"user_id": user_id},
        )
    except Exception as exc:
        logger.warning(
            "cognito_profile_complete_attribute_write_failed",
            extra={
                "user_id": user_id,
                "error": str(exc),
                "note": "Aurora commit succeeded; Cognito attribute will be set on next PATCH or backfill",
            },
        )


def _invoke_refresh_lambda() -> None:
    """
    Best-effort async invocation of the refresh_deck_view Lambda.

    Called AFTER the with conn: block has committed the Aurora UPDATE, and
    ONLY when profile_complete_verified flips false→true.  If the txn rolls
    back the caller never reaches this function, so no stale refresh is
    triggered.

    InvocationType="Event" means fire-and-forget: the call returns immediately
    once Lambda accepts the invocation; we do not wait for the refresh to complete.

    On any failure (missing ARN, transient AWS error), logs a structured warning
    and returns — does NOT raise.  The EventBridge 15-minute schedule covers
    any missed refresh.

    Post-commit call order: Cognito attribute write first, then lambda.invoke
    (both are best-effort; order is arbitrary but documented here for consistency).
    """
    if not _REFRESH_LAMBDA_ARN:
        logger.warning(
            "refresh_lambda_invoke_skipped",
            extra={"reason": "REFRESH_LAMBDA_ARN not configured"},
        )
        return

    try:
        client = boto3.client("lambda")
        client.invoke(
            FunctionName=_REFRESH_LAMBDA_ARN,
            InvocationType="Event",
            Payload=b"{}",
        )
        logger.info(
            "refresh_lambda_invoked",
            extra={"refresh_lambda_arn": _REFRESH_LAMBDA_ARN},
        )
    except Exception as exc:
        logger.warning(
            "refresh_lambda_invoke_failed",
            extra={
                "refresh_lambda_arn": _REFRESH_LAMBDA_ARN,
                "error": str(exc),
                "note": "Aurora commit succeeded; deck_view will refresh on next scheduled run",
            },
        )


# ---------------------------------------------------------------------------
# Sub-handlers (each handles exactly one route)
# ---------------------------------------------------------------------------

def _handle_get_profile_me(event: dict, user_id: str, user_sex: str) -> dict:
    """
    GET /v1/profile/me — return the full profile for the authenticated user.

    RLS exception: the `OR user_id = current_setting('app.requesting_user_id')::uuid`
    clause in the users policy makes the own row always visible regardless of sex.
    """
    conn = _get_conn()
    try:
        with knotify_db.rls_context(conn, user_id, user_sex):
            with conn.cursor() as cur:
                cur.execute(_SELECT_OWN_PROFILE_SQL, (user_id,))
                row = cur.fetchone()
                if row is None:
                    return _json_response(404, {"error": "not_found"})
                profile = _row_to_dict(row, cur.description)
    except Exception:
        conn.close()
        global _conn
        _conn = None
        raise

    return _json_response(200, profile)


def _handle_patch_profile_me(event: dict, user_id: str, user_sex: str) -> dict:
    """
    PATCH /v1/profile/me — partial update for the authenticated user's profile.

    Semantics:
    1. Parse and validate the request body (accept only known fields).
    2. Inside a transaction, fetch the current row WITH a row-lock.
    3. Check immutable fields — return 400 BEFORE any UPDATE if violations found.
       This fires BEFORE the DB trigger trg_users_immutable.
    4. Build and execute the UPDATE for all valid fields.
    5. If all 34 required-for-completion fields are non-NULL post-update AND
       profile_complete_verified was false, also set profile_complete_verified = true
       in the same transaction.
    6. AFTER the with conn: block commits: if the flag flipped false→true,
       call _write_cognito_profile_complete (best-effort; see its docstring).
    7. Return the updated full profile (200).
    """
    body_raw = event.get("body")
    if not body_raw:
        return _json_response(400, {"error": "missing_body"})

    try:
        patch_data: dict = json.loads(body_raw)
    except (json.JSONDecodeError, TypeError):
        return _json_response(400, {"error": "invalid_json"})

    if not isinstance(patch_data, dict):
        return _json_response(400, {"error": "body_must_be_object"})

    # Filter to only accepted fields
    patch_data = {k: v for k, v in patch_data.items() if k in _ALL_PATCH_FIELDS}

    if not patch_data:
        return _json_response(400, {"error": "no_valid_fields"})

    logger.info("patch_before_get_conn", extra={"user_id": user_id})
    conn = _get_conn()
    logger.info("patch_after_get_conn", extra={"user_id": user_id})
    flag_flipped = False  # tracks whether this PATCH flipped false→true

    try:
        with knotify_db.rls_context(conn, user_id, user_sex):
            logger.info("patch_after_rls_enter", extra={"user_id": user_id})
            with conn.cursor() as cur:
                # Step 1: fetch current row with row-lock
                cur.execute(_SELECT_FOR_UPDATE_SQL, (user_id,))
                row = cur.fetchone()
                logger.info("patch_after_select_for_update", extra={"user_id": user_id, "row_found": row is not None})
                if row is None:
                    return _json_response(404, {"error": "not_found"})
                current = _row_to_dict(row, cur.description)

            # Step 2: immutable-field check (Lambda 400 BEFORE DB trigger)
            violations = _check_immutable_fields(patch_data, current)
            if violations:
                return _json_response(
                    400,
                    {"error": "immutable_field", "fields": sorted(violations)},
                )

            # Step 3: build the proposed post-update row (for completion check)
            proposed = {**current, **patch_data}

            # Step 4: execute the UPDATE
            set_clauses = [f"{col} = %s" for col in patch_data]
            # Wrap dict values (jsonb columns like preferences) in psycopg2.extras.Json
            # so psycopg2 emits valid JSON literals. Without this, psycopg2 raises
            # "can't adapt type 'dict'" at execute time — there is no global Json
            # adapter registered in the knotify_db layer (only register_uuid()).
            set_values = [
                psycopg2.extras.Json(v) if isinstance(v, dict) else v
                for v in patch_data.values()
            ]

            # Story 7.3: when preferences is in the patch body, also write
            # preference_vector in the same UPDATE using an explicit ::vector cast.
            # psycopg2's default list adapter handles the Python list; the ::vector
            # cast coerces it to the pgvector column type — no register_vector() needed.
            if "preferences" in patch_data:
                pref_vector = encode_prefs(patch_data["preferences"])
                set_clauses.append("preference_vector = %s::vector")
                set_values.append(pref_vector)

            # Step 5: flip profile_complete_verified if qualifying
            was_incomplete = not current.get("profile_complete_verified")
            if _should_set_profile_complete(proposed) and was_incomplete:
                set_clauses.append("profile_complete_verified = true")
                flag_flipped = True

            update_sql = (
                f"UPDATE users SET {', '.join(set_clauses)}, updated_at = NOW() "
                f"WHERE user_id = %s::uuid "
                f"RETURNING *"
            )

            logger.info("patch_before_update_execute", extra={"user_id": user_id, "flag_flipped": flag_flipped, "n_set_clauses": len(set_clauses)})
            with conn.cursor() as cur:
                cur.execute(update_sql, [*set_values, user_id])
                logger.info("patch_after_update_execute", extra={"user_id": user_id})
                updated_row = cur.fetchone()
                logger.info("patch_after_update_fetch", extra={"user_id": user_id})
                updated = _row_to_dict(updated_row, cur.description) if updated_row else proposed

    except Exception:
        conn.close()
        global _conn
        _conn = None
        raise
    # with conn: block has committed here (rls_context commits on exit)

    # Step 6: best-effort post-commit calls AFTER Aurora commit, only on flag flip.
    # Order: Cognito attribute write first, then refresh Lambda invoke.
    # Both are best-effort; either failure logs a warning and returns HTTP 200.
    if flag_flipped:
        _write_cognito_profile_complete(user_id)
        _invoke_refresh_lambda()

    return _json_response(200, updated)


def _handle_get_profiles_by_username(event: dict, user_id: str, user_sex: str) -> dict:
    """
    GET /v1/profiles?username=<value> — case-insensitive exact username search.

    Returns the deck-view subset for the single matching opposite-sex user.
    Returns 404 when no match or RLS hides the result (same-sex user).
    """
    qs = event.get("queryStringParameters") or {}
    username = qs.get("username", "").strip()
    if not username:
        return _json_response(400, {"error": "username_required"})

    conn = _get_conn()
    try:
        with knotify_db.rls_context(conn, user_id, user_sex):
            with conn.cursor() as cur:
                cur.execute(_SELECT_DECK_BY_USERNAME_SQL, (username,))
                row = cur.fetchone()
                if row is None:
                    return _json_response(404, {"error": "not_found"})
                profile = _filter_to_deck_view(_row_to_dict(row, cur.description))
    except Exception:
        conn.close()
        global _conn
        _conn = None
        raise

    return _json_response(200, profile)


def _handle_get_profiles_by_id(event: dict, user_id: str, user_sex: str) -> dict:
    """
    GET /v1/profiles/{userId} — look up a profile by user ID.

    Returns deck-view fields only. Returns 404 if not found or RLS hides it.
    """
    path_params = event.get("pathParameters") or {}
    target_user_id = path_params.get("userId", "").strip()
    if not target_user_id:
        return _json_response(400, {"error": "userId_required"})

    conn = _get_conn()
    try:
        with knotify_db.rls_context(conn, user_id, user_sex):
            with conn.cursor() as cur:
                cur.execute(_SELECT_DECK_BY_ID_SQL, (target_user_id,))
                row = cur.fetchone()
                if row is None:
                    return _json_response(404, {"error": "not_found"})
                profile = _filter_to_deck_view(_row_to_dict(row, cur.description))
    except Exception:
        conn.close()
        global _conn
        _conn = None
        raise

    return _json_response(200, profile)


# ---------------------------------------------------------------------------
# Route dispatcher
# ---------------------------------------------------------------------------

_PATH_PROFILE_ME = "/v1/profile/me"
_PATH_PROFILES_PREFIX = "/v1/profiles"
_PROFILE_ID_RE = re.compile(r"^/v1/profiles/([0-9a-fA-F-]{36})$")


def _dispatch(event: dict, user_id: str, user_sex: str) -> dict:
    """
    Route the request to the appropriate sub-handler.

    Routes:
      GET  /v1/profile/me              → _handle_get_profile_me
      PATCH /v1/profile/me             → _handle_patch_profile_me
      GET  /v1/profiles?username=...   → _handle_get_profiles_by_username
      GET  /v1/profiles/{userId}       → _handle_get_profiles_by_id
    """
    http = event.get("requestContext", {}).get("http", {})
    method: str = http.get("method", "").upper()
    path: str = http.get("path", "")

    if method == "GET" and path == _PATH_PROFILE_ME:
        return _handle_get_profile_me(event, user_id, user_sex)

    if method == "PATCH" and path == _PATH_PROFILE_ME:
        return _handle_patch_profile_me(event, user_id, user_sex)

    # GET /v1/profiles?username=...
    if method == "GET" and path == _PATH_PROFILES_PREFIX:
        return _handle_get_profiles_by_username(event, user_id, user_sex)

    # GET /v1/profiles/{userId}
    if method == "GET":
        m = _PROFILE_ID_RE.match(path)
        if m:
            return _handle_get_profiles_by_id(event, user_id, user_sex)

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
    knotify-profile Lambda entrypoint.

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
        "profile_request",
        extra={
            "user_id": user_id,
            "method": event.get("requestContext", {}).get("http", {}).get("method"),
            "path": event.get("requestContext", {}).get("http", {}).get("path"),
        },
    )

    return _dispatch(event, user_id, user_sex)
