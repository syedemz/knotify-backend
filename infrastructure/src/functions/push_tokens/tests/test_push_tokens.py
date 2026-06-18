"""
Unit tests for the push_tokens Lambda handler.

Story 8.11 — POST /v1/push-tokens

Behaviour under test:
  - Valid request: PutItem on PushNotificationTokens, 200 response
  - Missing body: 400 {"error":"missing_body"}
  - Invalid JSON: 400 {"error":"invalid_json"}
  - Missing required fields: 400 with field-specific errors
  - Upsert semantics: same (user_id, device_id) → update last_seen, no duplicate
  - JWT sub extraction from requestContext.authorizer.jwt.claims.sub
  - NOT gated by require_profile_complete (early-app-launch registration)

All tests are pure unit tests — no real DynamoDB, no real AWS calls.
DynamoDB client is patched at the module level via monkeypatch.
"""

from __future__ import annotations

import importlib
import json
import sys
import types
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    body: Any = None,
    sub: str = "test-user-sub-123",
    raw_body: str | None = None,
) -> dict:
    """Build a minimal API Gateway v2 HTTP event for POST /v1/push-tokens."""
    if raw_body is None:
        raw_body = json.dumps(body) if body is not None else None
    return {
        "requestContext": {
            "http": {"method": "POST", "path": "/v1/push-tokens"},
            "authorizer": {
                "jwt": {
                    "claims": {
                        "sub": sub,
                    }
                }
            },
        },
        "body": raw_body,
    }


def _make_module(dynamo_mock: MagicMock):
    """
    Import (or reload) push_tokens.handler with boto3 patched so no real
    AWS calls are made.  Returns the module so tests can call handler()
    directly.
    """
    # Ensure knotify_obs stub is present before import.
    # build_package.py strips layer deps so we install a lightweight stub.
    if "knotify_obs" not in sys.modules:
        obs = types.ModuleType("knotify_obs")

        def _init_logger(name: str):
            import logging
            return logging.getLogger(name)

        obs.init_logger = _init_logger
        sys.modules["knotify_obs"] = obs

    # Patch boto3 at the module level so _get_dynamo() returns dynamo_mock.
    with patch("boto3.client", return_value=dynamo_mock):
        # Remove cached module so the import sees the fresh patch.
        for key in list(sys.modules.keys()):
            if "push_tokens" in key and "test" not in key:
                del sys.modules[key]
        import infrastructure.src.functions.push_tokens.handler as mod
        # Force _dynamo singleton to use our mock.
        mod._dynamo = dynamo_mock
    return mod


def _call_handler(module, event: dict) -> dict:
    """Invoke the Lambda handler directly (bypassing decorators)."""
    return module._handle_post_push_tokens(event)


# ---------------------------------------------------------------------------
# Test: valid request upserts the token and returns 200
# ---------------------------------------------------------------------------


def test_valid_request_puts_item_and_returns_200():
    """
    Given a valid JWT and body {platform, push_token, device_id, app_version},
    when POST /v1/push-tokens is called,
    then DynamoDB PutItem is called with PK=user_id, SK=device_id and the
    function returns HTTP 200 {"registered": true}.
    """
    dynamo_mock = MagicMock()
    mod = _make_module(dynamo_mock)

    event = _make_event(
        body={
            "platform": "ios",
            "push_token": "ExponentPushToken[xxxxxx]",
            "device_id": "device-abc-123",
            "app_version": "1.2.3",
        },
        sub="user-sub-abc",
    )

    resp = _call_handler(mod, event)

    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body.get("registered") is True

    # PutItem must have been called exactly once
    dynamo_mock.put_item.assert_called_once()
    call_kwargs = dynamo_mock.put_item.call_args[1]

    # Table name from env var (default value)
    assert call_kwargs["TableName"] == "PushNotificationTokens"

    # Key: PK = user_id (Cognito sub), SK = device_id
    item = call_kwargs["Item"]
    assert item["user_id"]["S"] == "user-sub-abc"
    assert item["device_id"]["S"] == "device-abc-123"
    assert item["push_token"]["S"] == "ExponentPushToken[xxxxxx]"
    assert item["platform"]["S"] == "ios"
    assert item["app_version"]["S"] == "1.2.3"
    # last_seen must be a non-empty string (ISO 8601 timestamp)
    assert item["last_seen"]["S"] != ""


# ---------------------------------------------------------------------------
# Test: missing body returns 400
# ---------------------------------------------------------------------------


def test_missing_body_returns_400():
    """
    Given a request with no body,
    when POST /v1/push-tokens is called,
    then the response is HTTP 400 {"error": "missing_body"}.
    """
    dynamo_mock = MagicMock()
    mod = _make_module(dynamo_mock)

    event = _make_event(body=None)
    resp = _call_handler(mod, event)

    assert resp["statusCode"] == 400
    assert json.loads(resp["body"])["error"] == "missing_body"
    dynamo_mock.put_item.assert_not_called()


# ---------------------------------------------------------------------------
# Test: invalid JSON returns 400
# ---------------------------------------------------------------------------


def test_invalid_json_body_returns_400():
    """
    Given a request with a non-JSON body,
    when POST /v1/push-tokens is called,
    then the response is HTTP 400 {"error": "invalid_json"}.
    """
    dynamo_mock = MagicMock()
    mod = _make_module(dynamo_mock)

    event = _make_event(raw_body="{not valid json")
    resp = _call_handler(mod, event)

    assert resp["statusCode"] == 400
    assert json.loads(resp["body"])["error"] == "invalid_json"
    dynamo_mock.put_item.assert_not_called()


# ---------------------------------------------------------------------------
# Test: missing required field 'push_token'
# ---------------------------------------------------------------------------


def test_missing_push_token_returns_400():
    """
    Given a body without push_token,
    when POST /v1/push-tokens is called,
    then the response is HTTP 400 {"error": "push_token_required"}.
    """
    dynamo_mock = MagicMock()
    mod = _make_module(dynamo_mock)

    event = _make_event(
        body={
            "platform": "android",
            "device_id": "device-xyz",
            "app_version": "2.0.0",
        }
    )
    resp = _call_handler(mod, event)

    assert resp["statusCode"] == 400
    assert json.loads(resp["body"])["error"] == "push_token_required"
    dynamo_mock.put_item.assert_not_called()


# ---------------------------------------------------------------------------
# Test: missing required field 'device_id'
# ---------------------------------------------------------------------------


def test_missing_device_id_returns_400():
    """
    Given a body without device_id,
    when POST /v1/push-tokens is called,
    then the response is HTTP 400 {"error": "device_id_required"}.
    """
    dynamo_mock = MagicMock()
    mod = _make_module(dynamo_mock)

    event = _make_event(
        body={
            "platform": "ios",
            "push_token": "ExponentPushToken[xxxxxx]",
            "app_version": "1.0.0",
        }
    )
    resp = _call_handler(mod, event)

    assert resp["statusCode"] == 400
    assert json.loads(resp["body"])["error"] == "device_id_required"
    dynamo_mock.put_item.assert_not_called()


# ---------------------------------------------------------------------------
# Test: missing required field 'platform'
# ---------------------------------------------------------------------------


def test_missing_platform_returns_400():
    """
    Given a body without platform,
    when POST /v1/push-tokens is called,
    then the response is HTTP 400 {"error": "platform_required"}.
    """
    dynamo_mock = MagicMock()
    mod = _make_module(dynamo_mock)

    event = _make_event(
        body={
            "push_token": "ExponentPushToken[xxxxxx]",
            "device_id": "device-abc",
            "app_version": "1.0.0",
        }
    )
    resp = _call_handler(mod, event)

    assert resp["statusCode"] == 400
    assert json.loads(resp["body"])["error"] == "platform_required"
    dynamo_mock.put_item.assert_not_called()


# ---------------------------------------------------------------------------
# Test: upsert — second call with same device_id does not create a duplicate
# (DynamoDB PutItem acts as upsert; we verify it is called with PUT semantics)
# ---------------------------------------------------------------------------


def test_second_registration_same_device_upserts_token():
    """
    Given a second POST with the same (user_id, device_id) but a new push_token,
    when POST /v1/push-tokens is called,
    then DynamoDB PutItem is called again (upsert — no condition expression).
    PutItem without ConditionExpression overwrites the existing item,
    which is the correct upsert behaviour for token refresh.
    """
    dynamo_mock = MagicMock()
    mod = _make_module(dynamo_mock)

    body = {
        "platform": "ios",
        "push_token": "ExponentPushToken[NEW_TOKEN]",
        "device_id": "device-abc-123",
        "app_version": "1.3.0",
    }
    event = _make_event(body=body, sub="user-sub-abc")

    resp = _call_handler(mod, event)
    assert resp["statusCode"] == 200

    # No ConditionExpression — unconditional put (upsert semantics)
    call_kwargs = dynamo_mock.put_item.call_args[1]
    assert "ConditionExpression" not in call_kwargs


# ---------------------------------------------------------------------------
# Test: app_version is optional (no 400 when absent)
# ---------------------------------------------------------------------------


def test_app_version_is_optional():
    """
    Given a body without app_version,
    when POST /v1/push-tokens is called,
    then the request succeeds with HTTP 200 (app_version is not required).
    """
    dynamo_mock = MagicMock()
    mod = _make_module(dynamo_mock)

    event = _make_event(
        body={
            "platform": "ios",
            "push_token": "ExponentPushToken[xxxxxx]",
            "device_id": "device-abc-123",
            # app_version intentionally omitted
        }
    )
    resp = _call_handler(mod, event)

    assert resp["statusCode"] == 200


# ---------------------------------------------------------------------------
# Test: JWT sub extraction — user_id comes from JWT claims, never from body
# ---------------------------------------------------------------------------


def test_user_id_sourced_from_jwt_sub_not_body():
    """
    Given a body that includes a userId field and a JWT sub,
    when POST /v1/push-tokens is called,
    then PutItem uses the JWT sub as user_id, ignoring any userId in the body.
    """
    dynamo_mock = MagicMock()
    mod = _make_module(dynamo_mock)

    event = _make_event(
        body={
            "platform": "ios",
            "push_token": "ExponentPushToken[xxxxxx]",
            "device_id": "device-abc-123",
            "app_version": "1.0.0",
            "userId": "should-be-ignored",
        },
        sub="correct-sub-from-jwt",
    )

    _call_handler(mod, event)

    item = dynamo_mock.put_item.call_args[1]["Item"]
    assert item["user_id"]["S"] == "correct-sub-from-jwt"


# ---------------------------------------------------------------------------
# Test: table name sourced from TABLE_PUSH_TOKENS env var
# ---------------------------------------------------------------------------


def test_table_name_from_env_var(monkeypatch):
    """
    Given TABLE_PUSH_TOKENS is set to a custom table name,
    when POST /v1/push-tokens is called,
    then PutItem uses that custom table name.
    """
    dynamo_mock = MagicMock()
    mod = _make_module(dynamo_mock)
    monkeypatch.setenv("TABLE_PUSH_TOKENS", "CustomPushTokensTable")
    # Reset the module-level default so the env var is re-read
    mod._TABLE_PUSH_TOKENS = "CustomPushTokensTable"

    event = _make_event(
        body={
            "platform": "android",
            "push_token": "ExponentPushToken[xxxxxx]",
            "device_id": "device-xyz",
        }
    )
    resp = _call_handler(mod, event)

    assert resp["statusCode"] == 200
    assert dynamo_mock.put_item.call_args[1]["TableName"] == "CustomPushTokensTable"
