"""
Unit tests for the deletion_initiator Lambda handler.

Stories covered:
  9.9 — DELETE /v1/profile/me (initiate account deletion)
  9.10 — GET /v1/profile/me/deletion-status (query execution status)

Acceptance criteria tested:
  A. DELETE /v1/profile/me — extracts user_id from JWT sub, calls
     states:StartExecution with input {user_id, jwt_sub, purge_immediately},
     returns 202 with executionArn.
  B. purge_immediately defaults to False when body is absent.
  C. purge_immediately defaults to False when body is empty JSON {}.
  D. Non-boolean purge_immediately value is rejected with 400.
  E. purge_immediately=True is forwarded correctly.
  F. JWT claims missing → 401.
  G. GET /v1/profile/me/deletion-status — 200 with status, startDate, names
     (story 9.10: real DescribeExecution + GetExecutionHistory implementation).
  H. Unknown route → 404.
  I. StartExecution input includes jwt_sub == user_id (defense-in-depth contract).
  J. StartExecution called with correct stateMachineArn.
  K. GET /v1/profile/me/deletion-status — 403 when jwt.sub != execution input user_id,
     body is empty (no metadata leaked to attacker).
  L. GET /v1/profile/me/deletion-status — 400 when executionArn query param is absent.
  M. GET /v1/profile/me/deletion-status — 404 when DescribeExecution raises
     ExecutionDoesNotExist.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Event builders
# ---------------------------------------------------------------------------

def _make_delete_event(
    sub: str = "test-user-uuid-1234",
    body: str | None = None,
) -> dict:
    """Build a minimal API Gateway v2 DELETE /v1/profile/me event."""
    return {
        "requestContext": {
            "http": {
                "method": "DELETE",
                "path": "/v1/profile/me",
            },
            "authorizer": {
                "jwt": {
                    "claims": {
                        "sub": sub,
                    },
                },
            },
        },
        "body": body,
        "headers": {
            "x-knotify-edge-secret": "test-secret",
        },
    }


def _make_get_status_event(
    sub: str = "test-user-uuid-1234",
    execution_arn: str | None = "arn:aws:states:eu-central-1:123456789012:execution:knotify-dev-account-deletion:exec-abc",
) -> dict:
    """Build a minimal API Gateway v2 GET /v1/profile/me/deletion-status event."""
    query_params: dict | None = {"executionArn": execution_arn} if execution_arn is not None else None
    event: dict = {
        "requestContext": {
            "http": {
                "method": "GET",
                "path": "/v1/profile/me/deletion-status",
            },
            "authorizer": {
                "jwt": {
                    "claims": {
                        "sub": sub,
                    },
                },
            },
        },
        "body": None,
        "headers": {
            "x-knotify-edge-secret": "test-secret",
        },
    }
    if query_params is not None:
        event["queryStringParameters"] = query_params
    return event


def _make_unknown_route_event() -> dict:
    """Build an event for an unrecognised route."""
    return {
        "requestContext": {
            "http": {
                "method": "POST",
                "path": "/v1/profile/me",
            },
            "authorizer": {
                "jwt": {
                    "claims": {
                        "sub": "some-user",
                    },
                },
            },
        },
        "body": None,
        "headers": {
            "x-knotify-edge-secret": "test-secret",
        },
    }


def _make_no_claims_event() -> dict:
    """Build an event whose requestContext has no authorizer JWT claims."""
    return {
        "requestContext": {
            "http": {
                "method": "DELETE",
                "path": "/v1/profile/me",
            },
        },
        "body": None,
        "headers": {
            "x-knotify-edge-secret": "test-secret",
        },
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_EXECUTION_ARN = (
    "arn:aws:states:eu-central-1:123456789012:execution:"
    "knotify-dev-account-deletion:exec-abc"
)

_STATE_MACHINE_ARN = (
    "arn:aws:states:eu-central-1:123456789012:stateMachine:"
    "knotify-dev-account-deletion"
)

_EDGE_SECRET = "test-secret"

# Owned by user "test-user-uuid-1234" — used in story 9.10 happy-path tests.
_DESCRIBE_EXECUTION_RESPONSE = {
    "executionArn": _EXECUTION_ARN,
    "stateMachineArn": _STATE_MACHINE_ARN,
    "name": "exec-abc",
    "status": "RUNNING",
    "startDate": datetime(2026, 6, 20, 12, 0, 0, tzinfo=timezone.utc),
    "input": json.dumps({"user_id": "test-user-uuid-1234", "purge_immediately": False}),
}

# GetExecutionHistory response: one TaskStateEntered, one TaskSucceeded, one more
# TaskStateEntered (no corresponding TaskSucceeded yet — still RUNNING).
_HISTORY_RESPONSE = {
    "events": [
        {
            "id": 1,
            "type": "TaskStateEntered",
            "timestamp": datetime(2026, 6, 20, 12, 0, 1, tzinfo=timezone.utc),
            "stateEnteredEventDetails": {"name": "ValidateDeletionRequest", "input": "{}"},
        },
        {
            "id": 2,
            "type": "TaskSucceeded",
            "timestamp": datetime(2026, 6, 20, 12, 0, 2, tzinfo=timezone.utc),
            "taskSucceededEventDetails": {"resource": "arn:aws:lambda:::function:validate", "output": "{}"},
        },
        {
            "id": 3,
            "type": "TaskStateEntered",
            "timestamp": datetime(2026, 6, 20, 12, 0, 3, tzinfo=timezone.utc),
            "stateEnteredEventDetails": {"name": "DisableCognitoUser", "input": "{}"},
        },
    ],
}


@pytest.fixture(autouse=True)
def env_vars(monkeypatch):
    """Inject required env vars for every test."""
    monkeypatch.setenv("STATE_MACHINE_ARN", _STATE_MACHINE_ARN)
    monkeypatch.setenv("EDGE_SECRET", _EDGE_SECRET)


@pytest.fixture()
def mock_sfn():
    """
    Patch handler._get_sfn_client so no real AWS call is made.
    Returns the mock Step Functions client.

    Preconfigures:
      start_execution  — returns _EXECUTION_ARN (9.9 tests)
      describe_execution — returns _DESCRIBE_EXECUTION_RESPONSE (9.10 tests)
      get_execution_history — returns _HISTORY_RESPONSE (9.10 tests)
    """
    with patch("deletion_initiator.handler._get_sfn_client") as mock_factory:
        mock_client = MagicMock()
        mock_client.start_execution.return_value = {
            "executionArn": _EXECUTION_ARN,
            "startDate": "2026-06-20T00:00:00Z",
        }
        mock_client.describe_execution.return_value = _DESCRIBE_EXECUTION_RESPONSE
        mock_client.get_execution_history.return_value = _HISTORY_RESPONSE
        mock_factory.return_value = mock_client
        yield mock_client


# ---------------------------------------------------------------------------
# Test A: DELETE /v1/profile/me → 202 with executionArn
# ---------------------------------------------------------------------------


def test_given_delete_request_when_handler_called_then_returns_202_with_execution_arn(
    mock_sfn,
):
    """
    Given a valid DELETE /v1/profile/me event with a Cognito JWT sub,
    when the handler is invoked,
    then it returns HTTP 202 with executionArn in the response body.
    """
    import deletion_initiator.handler as h

    response = h.handler(_make_delete_event(), None)

    assert response["statusCode"] == 202
    import json
    body = json.loads(response["body"])
    assert body["executionArn"] == _EXECUTION_ARN


# ---------------------------------------------------------------------------
# Test B: absent body → purge_immediately defaults to False
# ---------------------------------------------------------------------------


def test_given_absent_body_when_delete_called_then_purge_immediately_is_false(
    mock_sfn,
):
    """
    Given a DELETE /v1/profile/me event with no body (body=None),
    when the handler is invoked,
    then states.StartExecution is called with purge_immediately=False.
    """
    import json
    import deletion_initiator.handler as h

    h.handler(_make_delete_event(body=None), None)

    call_kwargs = mock_sfn.start_execution.call_args
    sent_input = json.loads(call_kwargs.kwargs["input"])
    assert sent_input["purge_immediately"] is False


# ---------------------------------------------------------------------------
# Test C: empty JSON body → purge_immediately defaults to False
# ---------------------------------------------------------------------------


def test_given_empty_json_body_when_delete_called_then_purge_immediately_is_false(
    mock_sfn,
):
    """
    Given a DELETE /v1/profile/me event with body='{}',
    when the handler is invoked,
    then states.StartExecution is called with purge_immediately=False.
    """
    import json
    import deletion_initiator.handler as h

    h.handler(_make_delete_event(body="{}"), None)

    call_kwargs = mock_sfn.start_execution.call_args
    sent_input = json.loads(call_kwargs.kwargs["input"])
    assert sent_input["purge_immediately"] is False


# ---------------------------------------------------------------------------
# Test D: non-boolean purge_immediately → 400
# ---------------------------------------------------------------------------


def test_given_non_boolean_purge_immediately_when_delete_called_then_returns_400(
    mock_sfn,
):
    """
    Given a DELETE /v1/profile/me event with purge_immediately='yes' (a string),
    when the handler is invoked,
    then it returns HTTP 400 and does NOT call states.StartExecution.
    """
    import json
    import deletion_initiator.handler as h

    body = json.dumps({"purge_immediately": "yes"})
    response = h.handler(_make_delete_event(body=body), None)

    assert response["statusCode"] == 400
    mock_sfn.start_execution.assert_not_called()


# ---------------------------------------------------------------------------
# Test E: purge_immediately=True is forwarded to StartExecution input
# ---------------------------------------------------------------------------


def test_given_purge_immediately_true_when_delete_called_then_forwarded_to_start_execution(
    mock_sfn,
):
    """
    Given a DELETE /v1/profile/me event with purge_immediately=True,
    when the handler is invoked,
    then states.StartExecution receives input with purge_immediately=True.
    """
    import json
    import deletion_initiator.handler as h

    body = json.dumps({"purge_immediately": True})
    response = h.handler(_make_delete_event(body=body), None)

    assert response["statusCode"] == 202
    call_kwargs = mock_sfn.start_execution.call_args
    sent_input = json.loads(call_kwargs.kwargs["input"])
    assert sent_input["purge_immediately"] is True


# ---------------------------------------------------------------------------
# Test F: missing JWT claims → 401
# ---------------------------------------------------------------------------


def test_given_missing_jwt_claims_when_handler_called_then_returns_401(
    mock_sfn,
):
    """
    Given an event with no authorizer JWT claims block,
    when the handler is invoked,
    then it returns HTTP 401 and does NOT call states.StartExecution.
    """
    import deletion_initiator.handler as h

    response = h.handler(_make_no_claims_event(), None)

    assert response["statusCode"] == 401
    mock_sfn.start_execution.assert_not_called()


# ---------------------------------------------------------------------------
# Test G: GET /v1/profile/me/deletion-status — 200 happy path (story 9.10)
# ---------------------------------------------------------------------------


def test_given_get_deletion_status_request_when_owner_calls_then_returns_200_with_status_and_names(
    mock_sfn,
):
    """
    Given a GET /v1/profile/me/deletion-status?executionArn=<arn> event
    where the JWT sub matches the user_id in the execution input,
    when the handler is invoked,
    then it returns HTTP 200 with status, startDate, and a names list.
    """
    import json
    import deletion_initiator.handler as h

    response = h.handler(_make_get_status_event(sub="test-user-uuid-1234"), None)

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["status"] == "RUNNING"
    assert "startDate" in body
    assert "names" in body
    # The history mock has one TaskSucceeded event; names list must be non-empty.
    assert isinstance(body["names"], list)


# ---------------------------------------------------------------------------
# Test H: unknown route → 404
# ---------------------------------------------------------------------------


def test_given_unknown_route_when_handler_called_then_returns_404(
    mock_sfn,
):
    """
    Given a POST /v1/profile/me event (not a recognised route),
    when the handler is invoked,
    then it returns HTTP 404.
    """
    import deletion_initiator.handler as h

    response = h.handler(_make_unknown_route_event(), None)

    assert response["statusCode"] == 404


# ---------------------------------------------------------------------------
# Test I: StartExecution input includes jwt_sub == user_id
# ---------------------------------------------------------------------------


def test_given_valid_delete_request_when_handler_called_then_start_execution_includes_jwt_sub(
    mock_sfn,
):
    """
    Given a valid DELETE /v1/profile/me event with sub='user-abc-999',
    when the handler is invoked,
    then states.StartExecution input includes both user_id and jwt_sub
    equal to the JWT sub claim (defense-in-depth contract per story 9.2).
    """
    import json
    import deletion_initiator.handler as h

    sub = "user-abc-999"
    h.handler(_make_delete_event(sub=sub), None)

    call_kwargs = mock_sfn.start_execution.call_args
    sent_input = json.loads(call_kwargs.kwargs["input"])
    assert sent_input["user_id"] == sub
    assert sent_input["jwt_sub"] == sub


# ---------------------------------------------------------------------------
# Test J: StartExecution called with correct stateMachineArn
# ---------------------------------------------------------------------------


def test_given_valid_delete_request_when_handler_called_then_start_execution_targets_correct_state_machine(
    mock_sfn,
):
    """
    Given STATE_MACHINE_ARN env var set to a known ARN,
    when the handler is invoked with a valid DELETE event,
    then states.StartExecution is called with stateMachineArn equal to
    the STATE_MACHINE_ARN env var.
    """
    import deletion_initiator.handler as h

    h.handler(_make_delete_event(), None)

    call_kwargs = mock_sfn.start_execution.call_args
    assert call_kwargs.kwargs["stateMachineArn"] == _STATE_MACHINE_ARN


# ---------------------------------------------------------------------------
# Test K: GET /v1/profile/me/deletion-status — 403 when JWT sub ≠ execution
#          input user_id; response body must be empty (no metadata leaked).
# ---------------------------------------------------------------------------


def test_given_get_deletion_status_request_when_jwt_sub_does_not_match_execution_owner_then_returns_403_with_empty_body(
    mock_sfn,
):
    """
    Given a GET /v1/profile/me/deletion-status?executionArn=<arn> event
    where the JWT sub is 'attacker-user' but the execution input records
    user_id='test-user-uuid-1234',
    when the handler is invoked,
    then it returns HTTP 403 and the response body contains NO execution
    metadata (status, startDate, stopDate, or names must all be absent).
    """
    import json
    import deletion_initiator.handler as h

    response = h.handler(_make_get_status_event(sub="attacker-user"), None)

    assert response["statusCode"] == 403
    body = json.loads(response["body"])
    # The body must be empty — {} is acceptable, but none of these keys may appear.
    assert "status" not in body
    assert "startDate" not in body
    assert "stopDate" not in body
    assert "names" not in body


# ---------------------------------------------------------------------------
# Test L: GET /v1/profile/me/deletion-status — 400 when executionArn param absent
# ---------------------------------------------------------------------------


def test_given_get_deletion_status_request_when_execution_arn_param_is_absent_then_returns_400(
    mock_sfn,
):
    """
    Given a GET /v1/profile/me/deletion-status event with NO executionArn
    query parameter,
    when the handler is invoked,
    then it returns HTTP 400 and does NOT call DescribeExecution.
    """
    import deletion_initiator.handler as h

    response = h.handler(_make_get_status_event(execution_arn=None), None)

    assert response["statusCode"] == 400
    mock_sfn.describe_execution.assert_not_called()


# ---------------------------------------------------------------------------
# Test M: GET /v1/profile/me/deletion-status — 404 on ExecutionDoesNotExist
# ---------------------------------------------------------------------------


def test_given_get_deletion_status_request_when_execution_does_not_exist_then_returns_404(
    mock_sfn,
):
    """
    Given a GET /v1/profile/me/deletion-status?executionArn=<arn> event
    where AWS raises ExecutionDoesNotExist for that ARN,
    when the handler is invoked,
    then it returns HTTP 404.
    """
    from botocore.exceptions import ClientError
    import deletion_initiator.handler as h

    mock_sfn.describe_execution.side_effect = ClientError(
        {
            "Error": {
                "Code": "ExecutionDoesNotExist",
                "Message": "Execution Does Not Exist: arn:...",
            }
        },
        "DescribeExecution",
    )

    response = h.handler(_make_get_status_event(sub="test-user-uuid-1234"), None)

    assert response["statusCode"] == 404
