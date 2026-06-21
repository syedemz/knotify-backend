"""
Unit tests for story 9.9 — deletion_initiator Lambda handler.

TDD: these tests were written BEFORE handler.py to drive the implementation.

Acceptance criteria tested:
  A. DELETE /v1/profile/me — extracts user_id from JWT sub, calls
     states:StartExecution with input {user_id, jwt_sub, purge_immediately},
     returns 202 with executionArn.
  B. purge_immediately defaults to False when body is absent.
  C. purge_immediately defaults to False when body is empty JSON {}.
  D. Non-boolean purge_immediately value is rejected with 400.
  E. purge_immediately=True is forwarded correctly.
  F. JWT claims missing → 401.
  G. GET /v1/profile/me/deletion-status → 501 (stub for story 9.10).
  H. Unknown route → 404.
  I. StartExecution input includes jwt_sub == user_id (defense-in-depth contract).
  J. Response body contains executionArn key with a non-empty string value.
"""

from __future__ import annotations

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


def _make_get_status_event(sub: str = "test-user-uuid-1234") -> dict:
    """Build a minimal API Gateway v2 GET /v1/profile/me/deletion-status event."""
    return {
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
    """
    with patch("deletion_initiator.handler._get_sfn_client") as mock_factory:
        mock_client = MagicMock()
        mock_client.start_execution.return_value = {
            "executionArn": _EXECUTION_ARN,
            "startDate": "2026-06-20T00:00:00Z",
        }
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
# Test G: GET /v1/profile/me/deletion-status → 501 (stub for story 9.10)
# ---------------------------------------------------------------------------


def test_given_get_deletion_status_request_when_handler_called_then_returns_501(
    mock_sfn,
):
    """
    Given a GET /v1/profile/me/deletion-status event,
    when the handler is invoked,
    then it returns HTTP 501 (not yet implemented — story 9.10).
    """
    import deletion_initiator.handler as h

    response = h.handler(_make_get_status_event(), None)

    assert response["statusCode"] == 501


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
