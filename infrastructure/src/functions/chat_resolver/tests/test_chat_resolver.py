"""
Unit tests for the knotify-chat-resolver Lambda handler (story 8.0 scaffold).

These tests are fully isolated — they do NOT require a running database,
AWS credentials, or a deployed Lambda. External collaborators (DB connection,
DynamoDB client) are stubbed at the module level.

Run:
    pytest infrastructure/src/functions/chat_resolver/tests/test_chat_resolver.py -v

Conventions:
  - Test names follow the pattern: given_<context>_when_<action>_then_<outcome>
  - One behaviour per test.
  - The handler module is imported via importlib to allow layer-import patching
    without physically installing the layers.

Test coverage for story 8.0 (empty dispatcher):
  A. Dispatcher — any (typeName, fieldName) pair not yet implemented returns
     a structured Unimplemented error response (never raises, never returns None).
  B. @require_profile_complete_appsync — the decorator blocks access when the
     custom:profile_complete claim is missing or not "true" and lets requests
     through when it is "true".
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module import helpers
# ---------------------------------------------------------------------------

_HANDLER_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
)

_OBS_DIR = os.path.normpath(
    os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "../../../layers/observability",
    )
)


def _import_handler() -> ModuleType:
    """Import the chat_resolver handler module fresh for each test.

    Patches layer imports so the test suite runs without the layers
    physically installed.  knotify_obs.require_profile_complete_appsync
    is passed through as a no-op decorator so dispatcher tests are not
    gated by the profile-complete check.
    """
    spec = importlib.util.spec_from_file_location(
        "chat_resolver_handler_" + str(id(object())),
        os.path.join(_HANDLER_DIR, "handler.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(
        "sys.modules",
        {
            "knotify_db": MagicMock(),
            "knotify_obs": MagicMock(
                init_logger=MagicMock(return_value=MagicMock()),
                # Pass-through decorator so the dispatcher tests are not gated
                require_profile_complete_appsync=lambda f: f,
            ),
        },
    ):
        spec.loader.exec_module(mod)
    return mod


def _import_profile_complete_appsync() -> ModuleType:
    """Import _profile_complete_appsync directly from the layer source.

    Uses sys.path insertion so the import resolves without pip-installing
    the layer.  This tests the decorator in isolation from the handler.
    """
    layer_pkg_dir = os.path.join(_OBS_DIR, "knotify_obs")
    spec = importlib.util.spec_from_file_location(
        "_profile_complete_appsync_" + str(id(object())),
        os.path.join(layer_pkg_dir, "_profile_complete_appsync.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Helpers — build minimal AppSync Lambda event dicts
# ---------------------------------------------------------------------------


def _make_appsync_event(
    type_name: str = "Query",
    field_name: str = "listMyRooms",
    *,
    profile_complete: str | None = "true",
    user_sub: str = "user-sub-appsync-1234",
) -> dict:
    """Build a minimal AppSync Lambda resolver event.

    AppSync Lambda resolvers receive identity under event["identity"],
    with Cognito claims at event["identity"]["claims"].
    The custom:profile_complete claim is set by the pre-token-gen Lambda.
    """
    claims: dict = {"sub": user_sub}
    if profile_complete is not None:
        claims["custom:profile_complete"] = profile_complete

    return {
        "typeName": type_name,
        "fieldName": field_name,
        "identity": {
            "sub": user_sub,
            "issuer": "https://cognito-idp.eu-central-1.amazonaws.com/eu-central-1_test",
            "username": user_sub,
            "claims": claims,
        },
        "arguments": {},
        "source": None,
        "request": {"headers": {}},
    }


_CONTEXT = MagicMock()

# ---------------------------------------------------------------------------
# Section A — Dispatcher returns Unimplemented for all unknown fields
#
# Story 8.0 ships an empty dispatcher.  Every (typeName, fieldName) pair that
# has not been slotted in by a later story must receive a structured error
# response (not a raised exception, not None).
#
# The required shape:
#   {"errorType": "Unimplemented", "message": "<typeName>.<fieldName> not yet implemented"}
# ---------------------------------------------------------------------------


def test_given_unknown_query_field_when_dispatched_then_returns_unimplemented() -> None:
    """given Query.unknownField, when _dispatch called, then Unimplemented error."""
    mod = _import_handler()
    event = _make_appsync_event("Query", "unknownField")
    result = mod._dispatch(event)
    assert result["errorType"] == "Unimplemented"
    assert "Query.unknownField" in result["message"]


def test_given_mutation_field_when_dispatched_then_returns_unimplemented() -> None:
    """given Mutation.sendMessage, when _dispatch called, then Unimplemented error."""
    mod = _import_handler()
    event = _make_appsync_event("Mutation", "sendMessage")
    result = mod._dispatch(event)
    assert result["errorType"] == "Unimplemented"
    assert "Mutation.sendMessage" in result["message"]


def test_given_subscription_field_when_dispatched_then_returns_unimplemented() -> None:
    """given Subscription.onMessageInRoom, when _dispatch called, then Unimplemented error."""
    mod = _import_handler()
    event = _make_appsync_event("Subscription", "onMessageInRoom")
    result = mod._dispatch(event)
    assert result["errorType"] == "Unimplemented"
    assert "Subscription.onMessageInRoom" in result["message"]


def test_given_arbitrary_type_when_dispatched_then_returns_unimplemented() -> None:
    """given an arbitrary typeName/fieldName, when _dispatch called, then Unimplemented."""
    mod = _import_handler()
    event = _make_appsync_event("SomeType", "someField")
    result = mod._dispatch(event)
    assert result["errorType"] == "Unimplemented"


def test_given_empty_type_name_when_dispatched_then_returns_unimplemented() -> None:
    """given empty typeName, when _dispatch called, then Unimplemented (never raises)."""
    mod = _import_handler()
    event = _make_appsync_event("", "")
    result = mod._dispatch(event)
    assert result["errorType"] == "Unimplemented"


def test_given_dispatch_result_when_checked_then_has_message_key() -> None:
    """given any dispatch result, when examined, then 'message' key is present."""
    mod = _import_handler()
    event = _make_appsync_event("Query", "createOrGetRoom")
    result = mod._dispatch(event)
    assert "message" in result


# ---------------------------------------------------------------------------
# Section B — @require_profile_complete_appsync decorator
#
# AppSync events carry identity.claims, unlike REST handlers that use
# requestContext.authorizer.jwt.claims.  The new decorator must:
#   - Allow requests when custom:profile_complete == "true"
#   - Return Unauthorized when the claim is absent (fail-closed)
#   - Return Unauthorized when the claim is "false"
#   - Return Unauthorized when the claim is any other value
# ---------------------------------------------------------------------------


def test_given_profile_complete_true_when_decorator_applied_then_handler_called() -> None:
    """given profile_complete='true', when decorator applied, then handler is called."""
    mod = _import_profile_complete_appsync()

    called_with = {}

    @mod.require_profile_complete_appsync
    def fake_handler(event, context):
        called_with["event"] = event
        return {"data": "ok"}

    event = _make_appsync_event(profile_complete="true")
    result = fake_handler(event, _CONTEXT)
    assert result == {"data": "ok"}
    assert "event" in called_with


def test_given_profile_complete_false_when_decorator_applied_then_unauthorized() -> None:
    """given profile_complete='false', when decorator applied, then Unauthorized."""
    mod = _import_profile_complete_appsync()

    @mod.require_profile_complete_appsync
    def fake_handler(event, context):
        return {"data": "ok"}

    event = _make_appsync_event(profile_complete="false")
    result = fake_handler(event, _CONTEXT)
    assert result["errorType"] == "Unauthorized"
    assert result.get("reason") == "PROFILE_INCOMPLETE"


def test_given_profile_complete_missing_when_decorator_applied_then_unauthorized() -> None:
    """given claim absent, when decorator applied, then Unauthorized (fail-closed)."""
    mod = _import_profile_complete_appsync()

    @mod.require_profile_complete_appsync
    def fake_handler(event, context):
        return {"data": "ok"}

    event = _make_appsync_event(profile_complete=None)
    result = fake_handler(event, _CONTEXT)
    assert result["errorType"] == "Unauthorized"
    assert result.get("reason") == "PROFILE_INCOMPLETE"


def test_given_profile_complete_arbitrary_value_when_decorator_applied_then_unauthorized() -> None:
    """given claim == 'yes', when decorator applied, then Unauthorized (only 'true' allowed)."""
    mod = _import_profile_complete_appsync()

    @mod.require_profile_complete_appsync
    def fake_handler(event, context):
        return {"data": "ok"}

    event = _make_appsync_event(profile_complete="yes")
    result = fake_handler(event, _CONTEXT)
    assert result["errorType"] == "Unauthorized"


def test_given_no_identity_key_when_decorator_applied_then_unauthorized() -> None:
    """given event with no 'identity' key, when decorator applied, then Unauthorized."""
    mod = _import_profile_complete_appsync()

    @mod.require_profile_complete_appsync
    def fake_handler(event, context):
        return {"data": "ok"}

    result = fake_handler({}, _CONTEXT)
    assert result["errorType"] == "Unauthorized"
