"""
knotify-deletion-initiator Lambda handler.

Implements two HTTP API Gateway v2 routes:
  DELETE /v1/profile/me              — initiate account deletion workflow
  GET    /v1/profile/me/deletion-status  — query execution status (story 9.10)

DELETE /v1/profile/me:
  Extracts user_id from the JWT sub claim, parses an optional purge_immediately
  boolean from the request body (default False), calls Step Functions
  StartExecution on the account-deletion state machine, and returns 202 with the
  executionArn. The StartExecution input includes both user_id and jwt_sub set to
  the JWT sub (defense-in-depth contract for the ValidateDeletionRequest step in
  story 9.2 which re-checks this equality).

GET /v1/profile/me/deletion-status?executionArn=<arn>:
  Calls DescribeExecution, validates that the execution's input.user_id matches
  the JWT sub (returns 403 with an empty body on mismatch — no metadata leaked),
  then calls GetExecutionHistory to collect the names of completed steps, and
  returns 200 with {status, startDate, stopDate?, names}.

  Error cases:
    400 — executionArn query parameter is absent.
    403 — JWT sub does not match execution input.user_id (empty body).
    404 — AWS raises ExecutionDoesNotExist for the supplied ARN.

Design notes:
  - Both routes require a valid Cognito JWT (enforced by the HTTP API JWT
    authorizer at the API Gateway level).
  - The x-knotify-edge-secret header is validated by the @with_edge_secret
    decorator to ensure all traffic arrived via CloudFront (consistent with
    all other REST Lambda handlers in this project).
  - This Lambda runs OUTSIDE the VPC: Step Functions and Cognito JWT validation
    use public HTTPS endpoints; no Aurora or DynamoDB access needed.
  - The handler is structured as a route dispatcher; each route is a single
    focused function with no shared mutable state.

Dependencies (Lambda layers):
  - knotify_obs: init_logger, with_edge_secret
  - boto3: Step Functions client (states)

Environment variables:
  STATE_MACHINE_ARN — ARN of the account-deletion Step Functions state machine.
                      Injected by the deletion_initiator TF module.
  EDGE_SECRET       — CloudFront edge secret for @with_edge_secret validation.
                      Injected by the deletion_initiator TF module.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError
from knotify_obs import init_logger, with_edge_secret

# ---------------------------------------------------------------------------
# Module-level singletons (cold-start optimization)
# ---------------------------------------------------------------------------

logger = init_logger("knotify_deletion_initiator")

_STATE_MACHINE_ARN: str = os.environ.get("STATE_MACHINE_ARN", "")

# Module-level Step Functions client (reused across warm invocations).
_sfn_client = None


def _get_sfn_client():
    """
    Return a (possibly cached) boto3 Step Functions client.

    Extracted as a factory function so unit tests can patch at the module level
    without rebuilding the module. Warm invocations reuse the same client.
    """
    global _sfn_client
    if _sfn_client is None:
        _sfn_client = boto3.client("stepfunctions")
    return _sfn_client


# ---------------------------------------------------------------------------
# Pure helper functions (no I/O — fully unit-testable)
# ---------------------------------------------------------------------------

def _get_user_id(event: dict) -> str:
    """
    Extract user_id from the HTTP API JWT claims.

    The HTTP API JWT authorizer places decoded claims at:
        event["requestContext"]["authorizer"]["jwt"]["claims"]

    Raises KeyError if "sub" is absent (misconfigured authorizer).
    """
    claims: dict = event["requestContext"]["authorizer"]["jwt"]["claims"]
    return claims["sub"]


def _parse_purge_immediately(event: dict) -> bool | None:
    """
    Parse the purge_immediately flag from the request body.

    Returns:
        True / False — the parsed boolean value.
        None         — if the body contains a non-boolean value for
                       purge_immediately (caller should return 400).

    If the body is absent, null, or empty JSON, defaults to False.
    """
    body_raw = event.get("body")
    if not body_raw:
        return False

    try:
        body = json.loads(body_raw)
    except (json.JSONDecodeError, TypeError):
        # Unparseable body: treat as absent (no purge_immediately flag).
        return False

    if not isinstance(body, dict):
        return False

    if "purge_immediately" not in body:
        return False

    value = body["purge_immediately"]
    if not isinstance(value, bool):
        # Reject non-boolean values (e.g., strings, integers).
        return None

    return value


def _json_response(status_code: int, body: object) -> dict:
    """Build a Lambda HTTP response dict."""
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


# ---------------------------------------------------------------------------
# Sub-handlers (each handles exactly one route)
# ---------------------------------------------------------------------------

def _handle_delete_profile_me(event: dict, user_id: str) -> dict:
    """
    DELETE /v1/profile/me — initiate account deletion.

    Calls Step Functions StartExecution with:
        {user_id, jwt_sub, purge_immediately}
    where user_id == jwt_sub (both set to the JWT sub claim).

    Returns 202 with {executionArn: <arn>}.
    """
    purge_immediately = _parse_purge_immediately(event)
    if purge_immediately is None:
        return _json_response(
            400,
            {"error": "invalid_purge_immediately", "message": "purge_immediately must be a boolean"},
        )

    sfn = _get_sfn_client()
    execution_input = json.dumps({
        "user_id": user_id,
        "jwt_sub": user_id,
        "purge_immediately": purge_immediately,
    })

    # Use a unique name so duplicate DELETE requests within 90 days are
    # idempotent at the Step Functions level (same execution name = same ARN
    # returned without starting a new execution).
    execution_name = f"delete-{user_id[:32]}-{uuid.uuid4().hex[:8]}"

    response = sfn.start_execution(
        stateMachineArn=_STATE_MACHINE_ARN,
        name=execution_name,
        input=execution_input,
    )

    execution_arn: str = response["executionArn"]

    logger.info(
        "deletion_execution_started",
        extra={
            "user_id": user_id,
            "execution_arn": execution_arn,
            "purge_immediately": purge_immediately,
        },
    )

    return _json_response(202, {"executionArn": execution_arn})


def _serialize_dt(dt: datetime | None) -> str | None:
    """
    Serialize a datetime to an ISO 8601 string (UTC, with Z suffix).

    Returns None if dt is None (stopDate is absent on RUNNING executions).
    """
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _collect_completed_step_names(sfn: object, execution_arn: str) -> list[str]:
    """
    Call GetExecutionHistory (paginated via nextToken) and return the names of
    states whose tasks completed successfully (TaskSucceeded events).

    GetExecutionHistory does not include a state name directly on the
    TaskSucceeded event; instead we track the most recent TaskStateEntered
    name and emit it when the following TaskSucceeded appears.

    This approach is O(events) and correct for Standard Workflows where
    events are sequential per branch.
    """
    names: list[str] = []
    pending_state_name: str | None = None
    next_token: str | None = None

    while True:
        kwargs: dict = {
            "executionArn": execution_arn,
            "includeExecutionData": False,
        }
        if next_token is not None:
            kwargs["nextToken"] = next_token

        response: dict = sfn.get_execution_history(**kwargs)  # type: ignore[attr-defined]

        for event in response.get("events", []):
            event_type: str = event.get("type", "")
            if event_type == "TaskStateEntered":
                details = event.get("stateEnteredEventDetails", {})
                pending_state_name = details.get("name")
            elif event_type == "TaskSucceeded":
                if pending_state_name is not None:
                    names.append(pending_state_name)
                    pending_state_name = None

        next_token = response.get("nextToken")
        if next_token is None:
            break

    return names


def _handle_get_deletion_status(event: dict, user_id: str) -> dict:
    """
    GET /v1/profile/me/deletion-status?executionArn=<arn>

    1. Validate executionArn query parameter is present (400 if absent).
    2. Call DescribeExecution (404 if ExecutionDoesNotExist).
    3. Parse execution.input, assert input.user_id == jwt.sub (403, empty body, if not).
    4. Call GetExecutionHistory, collect completed step names.
    5. Return 200 with {status, startDate, stopDate?, names}.
    """
    query_params: dict = event.get("queryStringParameters") or {}
    execution_arn: str | None = query_params.get("executionArn")

    if not execution_arn:
        logger.warning(
            "deletion_status_missing_execution_arn",
            extra={"user_id": user_id},
        )
        return _json_response(400, {"error": "missing_execution_arn"})

    sfn = _get_sfn_client()

    try:
        describe_result = sfn.describe_execution(executionArn=execution_arn)
    except ClientError as exc:
        error_code = exc.response["Error"]["Code"]
        if error_code == "ExecutionDoesNotExist":
            logger.warning(
                "deletion_status_execution_not_found",
                extra={"user_id": user_id, "execution_arn": execution_arn},
            )
            return _json_response(404, {"error": "execution_not_found"})
        raise

    # Authorization: the execution input must record the same user_id as the JWT sub.
    # Parse the input JSON and compare; a mismatch returns 403 with NO metadata.
    try:
        execution_input: dict = json.loads(describe_result.get("input", "{}"))
        execution_owner: str = execution_input.get("user_id", "")
    except (json.JSONDecodeError, TypeError):
        execution_owner = ""

    if execution_owner != user_id:
        logger.warning(
            "deletion_status_authorization_mismatch",
            extra={
                "jwt_user_id": user_id,
                # Do NOT log execution_owner — that would leak the real owner's ID
                # to an attacker who is probing for valid ARNs via log correlation.
            },
        )
        # Return 403 with an empty body — no metadata of any kind.
        return {
            "statusCode": 403,
            "headers": {"Content-Type": "application/json"},
            "body": "{}",
        }

    # Collect completed step names from execution history.
    completed_names: list[str] = _collect_completed_step_names(sfn, execution_arn)

    status: str = describe_result.get("status", "UNKNOWN")
    start_date: str | None = _serialize_dt(describe_result.get("startDate"))
    stop_date: str | None = _serialize_dt(describe_result.get("stopDate"))

    response_body: dict = {
        "status": status,
        "startDate": start_date,
        "names": completed_names,
    }
    if stop_date is not None:
        response_body["stopDate"] = stop_date

    logger.info(
        "deletion_status_fetched",
        extra={
            "user_id": user_id,
            "execution_arn": execution_arn,
            "status": status,
            "completed_steps_count": len(completed_names),
        },
    )

    return _json_response(200, response_body)


# ---------------------------------------------------------------------------
# Route dispatcher
# ---------------------------------------------------------------------------

_PATH_PROFILE_ME = "/v1/profile/me"
_PATH_DELETION_STATUS = "/v1/profile/me/deletion-status"


def _dispatch(event: dict, user_id: str) -> dict:
    """
    Route the request to the appropriate sub-handler.

    Routes:
      DELETE /v1/profile/me              → _handle_delete_profile_me
      GET    /v1/profile/me/deletion-status → _handle_get_deletion_status
    """
    http = event.get("requestContext", {}).get("http", {})
    method: str = http.get("method", "").upper()
    path: str = http.get("path", "")

    if method == "DELETE" and path == _PATH_PROFILE_ME:
        return _handle_delete_profile_me(event, user_id)

    if method == "GET" and path == _PATH_DELETION_STATUS:
        return _handle_get_deletion_status(event, user_id)

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
    knotify-deletion-initiator Lambda entrypoint.

    Validates the edge secret (via @with_edge_secret decorator), extracts the
    user identity from JWT claims, and dispatches to the appropriate sub-handler.

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
        "deletion_initiator_request",
        extra={
            "user_id": user_id,
            "method": event.get("requestContext", {}).get("http", {}).get("method"),
            "path": event.get("requestContext", {}).get("http", {}).get("path"),
        },
    )

    return _dispatch(event, user_id)
