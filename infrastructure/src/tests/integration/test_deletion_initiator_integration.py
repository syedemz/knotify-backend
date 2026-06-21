"""
Integration tests for story 9.9 — deletion_initiator Lambda and
DELETE /v1/profile/me route.

Acceptance criteria tested (API-level only — full workflow assertions live in 9.13):

  AC-1  DELETE /v1/profile/me through CloudFront returns HTTP 202 with a
        non-empty executionArn in the response body.

  AC-2  A Step Functions execution exists for the returned executionArn.
        Status may be RUNNING, SUCCEEDED, or FAILED — only existence is
        asserted here. Do NOT poll for SUCCEEDED in this story.

  AC-3  WAF / CloudFront does not block a bodyless DELETE request to this
        route. The AC-1 test issues a bodyless DELETE (no Content-Type, no body)
        through CloudFront — if the WAF blocked it we would get 403/400, not 202.
        Existence of a valid 202 response is the confirmation.

Route: DELETE /v1/profile/me
Lambda: knotify-deletion-initiator (outside VPC, states:StartExecution)
State machine: knotify-{env}-account-deletion

Required env vars (all skip-gated — tests are skipped, not failed, when absent):
  DISTRIBUTION_DOMAIN_NAME — CloudFront HTTPS distribution domain (no scheme)
  EDGE_SECRET              — value of x-knotify-edge-secret header
  AWS_REGION               — e.g. eu-central-1
  STATE_MACHINE_ARN        — ARN of the account-deletion state machine
    (written to .env.test by terraform apply in dev; can be set manually)

  Plus the signed_in_user fixture prerequisites:
    COGNITO_USER_POOL_ID
    COGNITO_INTEGRATION_TEST_CLIENT_ID
    AURORA_HOST, AURORA_PORT, AURORA_DBNAME, AURORA_MASTER_SECRET_ARN

Run:
    pytest infrastructure/src/tests/integration/test_deletion_initiator_integration.py -v -m integration

Skip without live AWS:
    pytest -m "not integration"

Design notes:
  - The test does NOT assert SUCCEEDED status on the state machine execution.
    The state machine invokes ValidateDeletionRequest which writes an audit row
    and checks for existing in-progress deletions — those side effects are
    tested in the full E2E story 9.13.
  - The signed_in_user teardown deletes the Cognito user and Aurora row. The
    state machine execution may continue running after the test completes; this
    is acceptable — Step Functions executions are isolated to the test account
    and will eventually complete (SUCCEEDED or FAILED) without user impact.
  - The test issues a bodyless DELETE to confirm the WAF does not block it
    (AC-3). A DELETE with a JSON body (purge_immediately=false explicit) is
    NOT tested here — body handling is covered by unit tests in
    infrastructure/src/functions/deletion_initiator/tests/.
"""

from __future__ import annotations

import json
import os
import re

import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------

def _require_env(name: str) -> str:
    """
    Return env var *name*. Calls pytest.skip if absent (not error)
    so the test is marked SKIPPED when live AWS is not configured.
    """
    value = os.environ.get(name)
    if not value:
        pytest.skip(
            f"Required env var {name!r} not set — live dev environment not configured."
        )
    return value


def _is_valid_execution_arn(arn: str) -> bool:
    """
    Return True if *arn* looks like a valid Step Functions execution ARN.

    Expected format:
        arn:aws:states:<region>:<account>:execution:<machine-name>:<execution-name>
    """
    pattern = re.compile(
        r"^arn:aws:states:[a-z0-9-]+:\d{12}:execution:[a-zA-Z0-9_-]+:[a-zA-Z0-9_-]+$"
    )
    return bool(pattern.match(arn))


# ---------------------------------------------------------------------------
# Test AC-1 + AC-3: bodyless DELETE via CloudFront returns 202 with executionArn
#
# Covers:
#   AC-1: 202 response with a non-empty, valid executionArn
#   AC-3: WAF / CloudFront did not block the bodyless DELETE request
#         (if it had, we would see 403 or 400, not 202)
# ---------------------------------------------------------------------------

def test_given_signed_in_user_when_bodyless_delete_via_cloudfront_then_202_with_execution_arn(
    signed_in_user,
):
    """
    Given a signed-in user,
    when DELETE /v1/profile/me is called through CloudFront WITHOUT a body,
    then:
      - the response status is HTTP 202 (not 400/403/500)
      - the response body contains an 'executionArn' key
      - the executionArn matches the expected Step Functions ARN format
      - (implicitly) the WAF/CloudFront did not block the bodyless DELETE
        request (AC-3 — if blocked we would have received 403/400).
    """
    try:
        import requests as http_requests
    except ImportError:
        pytest.skip("requests library not installed — live environment required")

    distribution_domain = _require_env("DISTRIBUTION_DOMAIN_NAME")
    edge_secret = _require_env("EDGE_SECRET")

    url = f"https://{distribution_domain}/v1/profile/me"
    headers = {
        "Authorization": f"Bearer {signed_in_user['access_token']}",
        "x-knotify-edge-secret": edge_secret,
        # No Content-Type. No body. Bodyless DELETE — verifies WAF does not block it.
    }

    response = http_requests.delete(url, headers=headers, timeout=20)

    assert response.status_code == 202, (
        f"Expected HTTP 202 from DELETE /v1/profile/me (bodyless) via CloudFront, "
        f"got HTTP {response.status_code}. "
        f"Body: {response.text[:400]!r}. "
        "If status is 403/400, the WAF may be blocking the bodyless DELETE."
    )

    try:
        body = response.json()
    except (json.JSONDecodeError, ValueError) as exc:
        pytest.fail(
            f"DELETE /v1/profile/me returned HTTP 202 but body is not valid JSON. "
            f"Body: {response.text[:400]!r}. Error: {exc}"
        )

    assert "executionArn" in body, (
        f"Expected 'executionArn' key in 202 response body, got: {body!r}"
    )

    execution_arn: str = body["executionArn"]
    assert execution_arn, "executionArn must be a non-empty string"
    assert _is_valid_execution_arn(execution_arn), (
        f"executionArn {execution_arn!r} does not match the expected Step Functions "
        f"execution ARN format "
        f"(arn:aws:states:<region>:<account>:execution:<machine>:<name>)"
    )


# ---------------------------------------------------------------------------
# Test AC-2: the execution exists in Step Functions ListExecutions
# ---------------------------------------------------------------------------

def test_given_deletion_initiated_when_list_executions_then_execution_exists(
    signed_in_user,
):
    """
    Given a signed-in user who calls DELETE /v1/profile/me via CloudFront,
    when ListExecutions is called on the account-deletion state machine,
    then an execution exists for the returned executionArn.
    The execution status may be RUNNING, SUCCEEDED, or FAILED — only
    existence is asserted (full workflow assertions live in story 9.13).
    """
    try:
        import boto3
        import requests as http_requests
    except ImportError:
        pytest.skip("boto3 or requests not installed — live environment required")

    distribution_domain = _require_env("DISTRIBUTION_DOMAIN_NAME")
    edge_secret = _require_env("EDGE_SECRET")
    state_machine_arn = _require_env("STATE_MACHINE_ARN")
    region = _require_env("AWS_REGION")

    # Step 1: initiate deletion
    url = f"https://{distribution_domain}/v1/profile/me"
    headers = {
        "Authorization": f"Bearer {signed_in_user['access_token']}",
        "x-knotify-edge-secret": edge_secret,
    }

    response = http_requests.delete(url, headers=headers, timeout=20)

    assert response.status_code == 202, (
        f"Expected HTTP 202 from DELETE /v1/profile/me, "
        f"got HTTP {response.status_code}. Body: {response.text[:400]!r}"
    )

    execution_arn: str = response.json()["executionArn"]
    assert execution_arn, "executionArn must not be empty"

    # Step 2: assert the execution exists in Step Functions.
    # Use DescribeExecution (single call) rather than ListExecutions (paginated)
    # since we have the exact ARN from the response.
    sfn_client = boto3.client("stepfunctions", region_name=region)
    describe_response = sfn_client.describe_execution(executionArn=execution_arn)

    # The execution must exist (DescribeExecution would raise
    # ExecutionDoesNotExist if it didn't). Assert the ARN matches.
    assert describe_response["executionArn"] == execution_arn, (
        f"DescribeExecution returned ARN {describe_response['executionArn']!r}, "
        f"expected {execution_arn!r}"
    )

    # Status may be RUNNING, SUCCEEDED, or FAILED. We do NOT assert SUCCEEDED
    # here — full workflow assertions are in story 9.13.
    actual_status = describe_response["status"]
    assert actual_status in {"RUNNING", "SUCCEEDED", "FAILED"}, (
        f"Unexpected execution status {actual_status!r} for execution "
        f"{execution_arn!r}. Expected one of RUNNING, SUCCEEDED, FAILED."
    )
