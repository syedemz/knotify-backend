"""
Unit tests for the knotify-chat-resolver Lambda handler.

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

Test coverage for story 8.4 (sendMessage — token derivation):
  C. derive_client_request_token — pure function: same inputs produce same hash;
     differing inputs (content diff by one byte, second ±1) produce different hashes.
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


def test_given_mutation_send_message_when_dispatched_then_not_unimplemented() -> None:
    """given Mutation.sendMessage (story 8.4 wired), when _dispatch called, then NOT Unimplemented.

    sendMessage is now routed to _handle_send_message; it will reject the
    caller (Unauthorized — not a room member) rather than returning Unimplemented.
    """
    from unittest.mock import MagicMock

    mod = _import_handler()
    # Inject a mock DDB client so no real AWS call is made.
    # GetItem returns no Item → sender is not a member → Unauthorized.
    mock_ddb = MagicMock()
    mock_ddb.get_item.return_value = {}
    mod._dynamodb_client = mock_ddb

    event = _make_appsync_event("Mutation", "sendMessage")
    event["arguments"] = {"roomId": "room-x", "content": "hello"}
    result = mod._dispatch(event)
    # Must NOT be Unimplemented — the route is now wired.
    assert result.get("errorType") != "Unimplemented", (
        f"sendMessage must be routed but got Unimplemented: {result}"
    )


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


# ---------------------------------------------------------------------------
# Section C — derive_client_request_token (story 8.4)
#
# Determinism proof: same (sender, room, content, second) inputs always yield
# the same SHA-256 hex digest; any single-field difference yields a different
# digest.  This is the sole automated proof of the idempotency property —
# the deployed Lambda's wall clock cannot be pinned, so an integration-test
# replay would be flaky and is intentionally omitted.
# ---------------------------------------------------------------------------


def test_given_same_inputs_when_derive_token_called_twice_then_same_hash() -> None:
    """given identical (sender, room, content, second), when called twice, then identical token."""
    mod = _import_handler()
    token_a = mod.derive_client_request_token(
        sender_id="user-aaa",
        room_id="room-111",
        content="hello world",
        epoch_second=1_718_000_000,
    )
    token_b = mod.derive_client_request_token(
        sender_id="user-aaa",
        room_id="room-111",
        content="hello world",
        epoch_second=1_718_000_000,
    )
    assert token_a == token_b


def test_given_content_differs_by_one_byte_when_derive_token_called_then_different_hashes() -> None:
    """given content differing by one byte, when derive_token called, then different tokens."""
    mod = _import_handler()
    token_original = mod.derive_client_request_token(
        sender_id="user-aaa",
        room_id="room-111",
        content="hello world",
        epoch_second=1_718_000_000,
    )
    token_mutated = mod.derive_client_request_token(
        sender_id="user-aaa",
        room_id="room-111",
        content="hello worle",  # last char differs
        epoch_second=1_718_000_000,
    )
    assert token_original != token_mutated


def test_given_epoch_second_minus_one_when_derive_token_called_then_different_hash() -> None:
    """given epoch_second - 1, when derive_token called, then token differs (clock boundary)."""
    mod = _import_handler()
    base_second = 1_718_000_000
    token_base = mod.derive_client_request_token(
        sender_id="user-aaa",
        room_id="room-111",
        content="hello world",
        epoch_second=base_second,
    )
    token_prev = mod.derive_client_request_token(
        sender_id="user-aaa",
        room_id="room-111",
        content="hello world",
        epoch_second=base_second - 1,
    )
    assert token_base != token_prev


def test_given_epoch_second_plus_one_when_derive_token_called_then_different_hash() -> None:
    """given epoch_second + 1, when derive_token called, then token differs (clock boundary)."""
    mod = _import_handler()
    base_second = 1_718_000_000
    token_base = mod.derive_client_request_token(
        sender_id="user-aaa",
        room_id="room-111",
        content="hello world",
        epoch_second=base_second,
    )
    token_next = mod.derive_client_request_token(
        sender_id="user-aaa",
        room_id="room-111",
        content="hello world",
        epoch_second=base_second + 1,
    )
    assert token_base != token_next


def test_given_different_sender_ids_when_derive_token_called_then_different_hashes() -> None:
    """given different sender_ids, when derive_token called, then tokens differ."""
    mod = _import_handler()
    token_a = mod.derive_client_request_token(
        sender_id="user-aaa",
        room_id="room-111",
        content="hello world",
        epoch_second=1_718_000_000,
    )
    token_b = mod.derive_client_request_token(
        sender_id="user-bbb",
        room_id="room-111",
        content="hello world",
        epoch_second=1_718_000_000,
    )
    assert token_a != token_b


def test_given_different_room_ids_when_derive_token_called_then_different_hashes() -> None:
    """given different room_ids, when derive_token called, then tokens differ."""
    mod = _import_handler()
    token_a = mod.derive_client_request_token(
        sender_id="user-aaa",
        room_id="room-111",
        content="hello world",
        epoch_second=1_718_000_000,
    )
    token_b = mod.derive_client_request_token(
        sender_id="user-aaa",
        room_id="room-222",
        content="hello world",
        epoch_second=1_718_000_000,
    )
    assert token_a != token_b


def test_given_any_inputs_when_derive_token_called_then_result_is_64_char_hex_string() -> None:
    """given valid inputs, when derive_token called, then result is a 64-character hex SHA-256."""
    mod = _import_handler()
    token = mod.derive_client_request_token(
        sender_id="user-aaa",
        room_id="room-111",
        content="test message",
        epoch_second=1_718_000_000,
    )
    assert isinstance(token, str)
    assert len(token) == 64
    assert all(c in "0123456789abcdef" for c in token)
