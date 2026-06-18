"""
knotify-push-tokens Lambda handler.

Implements one HTTP API Gateway v2 route:
  POST /v1/push-tokens — register or refresh a device push notification token

Body: {"platform": <str>, "push_token": <str>, "device_id": <str>, "app_version": <str (optional)>}

Design notes:
  - NOT gated by @require_profile_complete — tokens must be registered at
    first app launch, before onboarding completes. Decorating with
    require_profile_complete would block registration for new users.
  - JWT authorizer enforced by the HTTP API Cognito authorizer (API Gateway level).
  - User identity comes from the JWT sub claim (event.requestContext.authorizer
    .jwt.claims.sub) — never from the request body.
  - Acts as an upsert: DynamoDB PutItem on (user_id PK, device_id SK). A second
    call with the same device_id refreshes push_token, platform, app_version,
    and last_seen in place — no duplicate rows per (user, device).
  - last_seen is written as an ISO 8601 UTC timestamp on every call so the
    stale-token cleanup Lambda (story 8.12) can identify dormant devices.
  - Module-level _dynamo singleton is reused across warm Lambda invocations
    (cold-start optimisation). _get_dynamo() isolates the boto3 import so
    unit tests can patch it without an AWS environment.

Dependencies (Lambda layers):
  - knotify_obs: init_logger
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from knotify_obs import init_logger

# ---------------------------------------------------------------------------
# Module-level singletons (cold-start optimisation)
# ---------------------------------------------------------------------------

logger = init_logger("knotify_push_tokens")

_TABLE_PUSH_TOKENS: str = os.environ.get("TABLE_PUSH_TOKENS", "PushNotificationTokens")

_dynamo = None


def _get_dynamo():
    """Return a (possibly cached) boto3 DynamoDB client."""
    global _dynamo
    if _dynamo is None:
        import boto3  # deferred — not available in local test environments
        _dynamo = boto3.client("dynamodb")
    return _dynamo


# ---------------------------------------------------------------------------
# Pure helper functions
# ---------------------------------------------------------------------------


def _get_user_id(event: dict) -> str:
    """Extract user_id (Cognito sub) from the HTTP API JWT claims."""
    claims: dict = event["requestContext"]["authorizer"]["jwt"]["claims"]
    return claims["sub"]


def _json_response(status_code: int, body: Any) -> dict:
    """Build a Lambda HTTP response dict."""
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _now_iso8601() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Sub-handler
# ---------------------------------------------------------------------------


def _handle_post_push_tokens(event: dict) -> dict:
    """
    POST /v1/push-tokens — register or refresh a device push notification token.

    Reads identity from JWT sub (never from request body).
    Performs a DynamoDB PutItem which acts as an unconditional upsert:
    an existing row for (user_id, device_id) is overwritten with the latest
    push_token, platform, app_version, and last_seen.

    Returns 200 {"registered": true} on success.
    Returns 400 for malformed or missing request body / required fields.
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

    # Validate required fields at the boundary (engineering principles §Defensive Programming)
    platform = body.get("platform", "").strip() if isinstance(body.get("platform"), str) else ""
    if not platform:
        return _json_response(400, {"error": "platform_required"})

    push_token = body.get("push_token", "").strip() if isinstance(body.get("push_token"), str) else ""
    if not push_token:
        return _json_response(400, {"error": "push_token_required"})

    device_id = body.get("device_id", "").strip() if isinstance(body.get("device_id"), str) else ""
    if not device_id:
        return _json_response(400, {"error": "device_id_required"})

    app_version = body.get("app_version", "").strip() if isinstance(body.get("app_version"), str) else ""

    user_id = _get_user_id(event)
    last_seen = _now_iso8601()

    # Build the DynamoDB item. PutItem is an unconditional upsert — any
    # existing row for (user_id, device_id) is overwritten in place.
    item: dict = {
        "user_id":   {"S": user_id},
        "device_id": {"S": device_id},
        "push_token": {"S": push_token},
        "platform":  {"S": platform},
        "last_seen": {"S": last_seen},
    }
    if app_version:
        item["app_version"] = {"S": app_version}

    table = _TABLE_PUSH_TOKENS

    _get_dynamo().put_item(
        TableName=table,
        Item=item,
    )

    logger.info(
        "push_token_registered",
        extra={
            "user_id": user_id,
            "device_id": device_id,
            "platform": platform,
        },
    )

    return _json_response(200, {"registered": True})


# ---------------------------------------------------------------------------
# Route dispatcher
# ---------------------------------------------------------------------------


def _dispatch(event: dict) -> dict:
    """Route the request to the appropriate sub-handler."""
    http = event.get("requestContext", {}).get("http", {})
    method: str = http.get("method", "").upper()
    path: str = http.get("path", "")

    if method == "POST" and path == "/v1/push-tokens":
        return _handle_post_push_tokens(event)

    logger.warning(
        "unmatched_route",
        extra={"method": method, "path": path},
    )
    return _json_response(404, {"error": "not_found"})


# ---------------------------------------------------------------------------
# Lambda entrypoint
# ---------------------------------------------------------------------------


def handler(event: dict, context: object) -> dict:
    """
    knotify-push-tokens Lambda entrypoint.

    NOT decorated with @require_profile_complete — push token registration
    must succeed at first app launch before profile onboarding completes.
    JWT authorizer is enforced by the HTTP API Gateway Cognito authorizer
    (all requests without a valid JWT are rejected at the API level before
    this Lambda is invoked).

    Args:
        event:   HTTP API Gateway v2 Lambda event.
        context: Lambda context object (unused).

    Returns:
        HTTP response dict with statusCode, headers, and body.
    """
    try:
        user_id = _get_user_id(event)
    except (KeyError, TypeError) as exc:
        logger.error(
            "jwt_claims_missing",
            extra={"error": str(exc)},
        )
        return _json_response(401, {"error": "unauthorized"})

    logger.info(
        "push_tokens_request",
        extra={
            "user_id": user_id,
            "method": event.get("requestContext", {}).get("http", {}).get("method"),
            "path": event.get("requestContext", {}).get("http", {}).get("path"),
        },
    )

    return _dispatch(event)
