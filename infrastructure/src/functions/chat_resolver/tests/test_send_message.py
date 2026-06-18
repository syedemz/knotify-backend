"""
Unit tests for the sendMessage resolver (story 8.4).

These tests are fully isolated — they do NOT require a running database,
AWS credentials, or a deployed Lambda. External collaborators (DynamoDB client)
are stubbed at the module level.

Run:
    pytest infrastructure/src/functions/chat_resolver/tests/test_send_message.py -v

Test coverage:
  A. Dispatcher routing — (Mutation, sendMessage) routes to _handle_send_message;
     result is not Unimplemented.
  B. Membership gate — sender not in room → Unauthorized.
  C. RoomDeactivated gate — room.status != 'active' → RoomDeactivated error.
  D. RoomReadOnly gate — room.status == 'active' but friendship_active == false
     → RoomReadOnly error.
  E. sender_id sourced from identity.sub, never from arguments.
  F. Happy path — TransactWriteItems called with exactly 2 items (PutItem ChatMessages
     + UpdateItem ChatRooms); response carries correct fields.
  G. No Notifications row written — TransactWriteItems batch has no Notifications Put.
  H. Message SK shape — SK follows "<iso>#<ULID>" pattern.
  I. ClientRequestToken passed to TransactWriteItems and is a 64-char hex string.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module import helpers (mirrors test_create_or_get_room.py pattern)
# ---------------------------------------------------------------------------

_HANDLER_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
)


def _import_handler(
    *,
    membership_item: dict | None = None,
    room_item: dict | None = None,
    dynamodb_client_override=None,
) -> ModuleType:
    """Import the chat_resolver handler module fresh for each test.

    Patches layer imports and configures the DynamoDB mock's get_item to
    return (membership_item, room_item) in call order.

    Args:
        membership_item:          DynamoDB item dict for ChatRoomMembership GetItem.
                                  None means the item is absent (sender not in room).
        room_item:                DynamoDB item dict for ChatRooms GetItem.
                                  None means the item is absent (room not found).
        dynamodb_client_override: Optional pre-configured DDB mock.  When None,
                                  a default mock is built from membership_item /
                                  room_item.
    """
    if dynamodb_client_override is None:
        mock_ddb = MagicMock()

        # get_item is called twice in _handle_send_message:
        #   call 1 → ChatRoomMembership
        #   call 2 → ChatRooms
        responses = []
        responses.append(
            {"Item": membership_item} if membership_item is not None else {}
        )
        responses.append(
            {"Item": room_item} if room_item is not None else {}
        )
        mock_ddb.get_item.side_effect = responses
        mock_ddb.transact_write_items.return_value = {}
    else:
        mock_ddb = dynamodb_client_override

    mock_knotify_obs = MagicMock()
    mock_knotify_obs.init_logger.return_value = MagicMock()
    mock_knotify_obs.require_profile_complete_appsync = lambda f: f

    spec = importlib.util.spec_from_file_location(
        "chat_resolver_handler_8_4_" + str(id(object())),
        os.path.join(_HANDLER_DIR, "handler.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(
        "sys.modules",
        {
            "knotify_db": MagicMock(),
            "knotify_obs": mock_knotify_obs,
        },
    ):
        spec.loader.exec_module(mod)

    mod._dynamodb_client = mock_ddb
    return mod


def _make_event(
    *,
    sender_id: str = "sender-sub-0001",
    room_id: str = "room-aaaa",
    content: str = "hello world",
    content_type: str = "text",
    profile_complete: str = "true",
    # Intentionally no senderId argument — AC: sender_id never from arguments
) -> dict:
    """Build a minimal AppSync Mutation.sendMessage event."""
    return {
        "typeName": "Mutation",
        "fieldName": "sendMessage",
        "identity": {
            "sub": sender_id,
            "claims": {
                "sub": sender_id,
                "custom:profile_complete": profile_complete,
            },
        },
        "arguments": {
            "roomId": room_id,
            "content": content,
            "contentType": content_type,
        },
        "source": None,
        "request": {"headers": {}},
    }


_ACTIVE_ROOM_ITEM = {
    "room_id": {"S": "room-aaaa"},
    "status": {"S": "active"},
    "friendship_active": {"BOOL": True},
}

_MEMBERSHIP_ITEM = {
    "user_id": {"S": "sender-sub-0001"},
    "room_id": {"S": "room-aaaa"},
}

_CTX = MagicMock()

# ---------------------------------------------------------------------------
# Section A — Dispatcher routing
# ---------------------------------------------------------------------------


def test_given_send_message_event_when_dispatched_then_not_unimplemented() -> None:
    """given (Mutation, sendMessage), when _dispatch called, then NOT Unimplemented (route is wired)."""
    mod = _import_handler(
        membership_item=_MEMBERSHIP_ITEM,
        room_item=_ACTIVE_ROOM_ITEM,
    )
    event = _make_event()
    result = mod._dispatch(event)
    assert result.get("errorType") != "Unimplemented", (
        f"sendMessage must be wired but got Unimplemented: {result}"
    )


# ---------------------------------------------------------------------------
# Section B — Membership gate
# ---------------------------------------------------------------------------


def test_given_sender_not_in_room_when_send_message_then_unauthorized() -> None:
    """given sender absent from ChatRoomMembership, when sendMessage, then Unauthorized."""
    mod = _import_handler(membership_item=None, room_item=_ACTIVE_ROOM_ITEM)
    event = _make_event()
    result = mod._dispatch(event)
    assert result["errorType"] == "Unauthorized", f"Expected Unauthorized: {result}"


# ---------------------------------------------------------------------------
# Section C — RoomDeactivated gate
# ---------------------------------------------------------------------------


def test_given_room_status_deactivated_when_send_message_then_room_deactivated_error() -> None:
    """given room.status == 'deactivated', when sendMessage, then RoomDeactivated error."""
    deactivated_room = {
        "room_id": {"S": "room-aaaa"},
        "status": {"S": "deactivated"},
        "friendship_active": {"BOOL": True},
    }
    mod = _import_handler(
        membership_item=_MEMBERSHIP_ITEM,
        room_item=deactivated_room,
    )
    event = _make_event()
    result = mod._dispatch(event)
    assert result["errorType"] == "RoomDeactivated", (
        f"Expected RoomDeactivated but got: {result}"
    )


def test_given_room_status_unknown_when_send_message_then_room_deactivated_error() -> None:
    """given room.status != 'active' (any non-active value), when sendMessage, then RoomDeactivated."""
    pending_room = {
        "room_id": {"S": "room-aaaa"},
        "status": {"S": "pending"},
        "friendship_active": {"BOOL": True},
    }
    mod = _import_handler(
        membership_item=_MEMBERSHIP_ITEM,
        room_item=pending_room,
    )
    event = _make_event()
    result = mod._dispatch(event)
    assert result["errorType"] == "RoomDeactivated", (
        f"Expected RoomDeactivated for non-active status but got: {result}"
    )


# ---------------------------------------------------------------------------
# Section D — RoomReadOnly gate
# ---------------------------------------------------------------------------


def test_given_room_active_but_friendship_inactive_when_send_message_then_room_read_only() -> None:
    """given room.status == 'active' but friendship_active == false, when sendMessage, then RoomReadOnly."""
    inactive_room = {
        "room_id": {"S": "room-aaaa"},
        "status": {"S": "active"},
        "friendship_active": {"BOOL": False},
    }
    mod = _import_handler(
        membership_item=_MEMBERSHIP_ITEM,
        room_item=inactive_room,
    )
    event = _make_event()
    result = mod._dispatch(event)
    assert result["errorType"] == "RoomReadOnly", (
        f"Expected RoomReadOnly but got: {result}"
    )


# ---------------------------------------------------------------------------
# Section E — sender_id sourced from identity.sub, never from arguments
# ---------------------------------------------------------------------------


def test_given_event_when_send_message_succeeds_then_sender_id_from_identity_sub() -> None:
    """given identity.sub = X, when sendMessage succeeds, then response senderId == X (never from args)."""
    mod = _import_handler(
        membership_item=_MEMBERSHIP_ITEM,
        room_item=_ACTIVE_ROOM_ITEM,
    )
    event = _make_event(sender_id="sender-sub-0001")
    # Confirm no senderId key in arguments (schema integrity — AC explicit rule)
    assert "senderId" not in event["arguments"], (
        "senderId must not appear in the GraphQL arguments (schema stays clean)"
    )
    result = mod._dispatch(event)
    assert "errorType" not in result, f"Expected success but got: {result}"
    assert result.get("senderId") == "sender-sub-0001", (
        f"senderId in result must equal identity.sub, got: {result.get('senderId')}"
    )
    # Also confirm the messageId is present (full SK)
    assert "messageId" in result, f"messageId must be in response: {result}"


# ---------------------------------------------------------------------------
# Section F — Happy path: TransactWriteItems called with correct shape
# ---------------------------------------------------------------------------


def test_given_valid_request_when_send_message_then_transact_write_called_once() -> None:
    """given valid membership + active room, when sendMessage, then TransactWriteItems called once."""
    mod = _import_handler(
        membership_item=_MEMBERSHIP_ITEM,
        room_item=_ACTIVE_ROOM_ITEM,
    )
    event = _make_event()
    mod._dispatch(event)
    ddb = mod._dynamodb_client
    ddb.transact_write_items.assert_called_once()


def test_given_valid_request_when_send_message_then_transact_has_two_operations() -> None:
    """given valid request, when TransactWriteItems called, then batch has exactly 2 items (Put + Update)."""
    mod = _import_handler(
        membership_item=_MEMBERSHIP_ITEM,
        room_item=_ACTIVE_ROOM_ITEM,
    )
    event = _make_event()
    mod._dispatch(event)
    ddb = mod._dynamodb_client
    call_kwargs = ddb.transact_write_items.call_args
    transact_items = (
        call_kwargs[1]["TransactItems"]
        if call_kwargs[1]
        else call_kwargs[0][0]["TransactItems"]
    )
    assert len(transact_items) == 2, (
        f"Expected exactly 2 transact items (PutItem + UpdateItem), got {len(transact_items)}"
    )


def test_given_valid_request_when_send_message_then_batch_has_put_for_chat_messages() -> None:
    """given valid request, when TransactWriteItems called, then batch includes PutItem on ChatMessages."""
    mod = _import_handler(
        membership_item=_MEMBERSHIP_ITEM,
        room_item=_ACTIVE_ROOM_ITEM,
    )
    event = _make_event()
    mod._dispatch(event)
    ddb = mod._dynamodb_client
    call_kwargs = ddb.transact_write_items.call_args
    transact_items = (
        call_kwargs[1]["TransactItems"]
        if call_kwargs[1]
        else call_kwargs[0][0]["TransactItems"]
    )
    put_tables = [
        item["Put"]["TableName"]
        for item in transact_items
        if "Put" in item
    ]
    assert "ChatMessages" in put_tables, (
        f"Expected ChatMessages PutItem in batch, got tables: {put_tables}"
    )


def test_given_valid_request_when_send_message_then_batch_has_update_for_chat_rooms() -> None:
    """given valid request, when TransactWriteItems called, then batch includes UpdateItem on ChatRooms."""
    mod = _import_handler(
        membership_item=_MEMBERSHIP_ITEM,
        room_item=_ACTIVE_ROOM_ITEM,
    )
    event = _make_event()
    mod._dispatch(event)
    ddb = mod._dynamodb_client
    call_kwargs = ddb.transact_write_items.call_args
    transact_items = (
        call_kwargs[1]["TransactItems"]
        if call_kwargs[1]
        else call_kwargs[0][0]["TransactItems"]
    )
    update_tables = [
        item["Update"]["TableName"]
        for item in transact_items
        if "Update" in item
    ]
    assert "ChatRooms" in update_tables, (
        f"Expected ChatRooms UpdateItem in batch, got tables: {update_tables}"
    )


def test_given_valid_request_when_send_message_then_response_has_expected_fields() -> None:
    """given valid request, when sendMessage succeeds, then response has Message-type fields."""
    mod = _import_handler(
        membership_item=_MEMBERSHIP_ITEM,
        room_item=_ACTIVE_ROOM_ITEM,
    )
    event = _make_event(sender_id="sender-sub-0001", room_id="room-aaaa", content="hi")
    result = mod._dispatch(event)
    assert "errorType" not in result, f"Expected success but got: {result}"
    # All fields from the GraphQL Message type (schema.graphql)
    for field in ("roomId", "messageId", "createdAt", "senderId", "content", "contentType", "deliveredAt"):
        assert field in result, f"Expected field '{field}' in result: {result}"
    assert result["roomId"] == "room-aaaa"
    assert result["senderId"] == "sender-sub-0001"
    assert result["content"] == "hi"


# ---------------------------------------------------------------------------
# Section G — No Notifications row written
# ---------------------------------------------------------------------------


def test_given_valid_request_when_send_message_then_no_notifications_put_in_batch() -> None:
    """given valid request (§5.4.2 rule), when TransactWriteItems called, then NO Notifications PutItem."""
    mod = _import_handler(
        membership_item=_MEMBERSHIP_ITEM,
        room_item=_ACTIVE_ROOM_ITEM,
    )
    event = _make_event()
    mod._dispatch(event)
    ddb = mod._dynamodb_client
    call_kwargs = ddb.transact_write_items.call_args
    transact_items = (
        call_kwargs[1]["TransactItems"]
        if call_kwargs[1]
        else call_kwargs[0][0]["TransactItems"]
    )
    put_tables = [
        item["Put"]["TableName"]
        for item in transact_items
        if "Put" in item
    ]
    assert "Notifications" not in put_tables, (
        f"§5.4.2 violation: Notifications PutItem found in chat sendMessage batch: {put_tables}"
    )


# ---------------------------------------------------------------------------
# Section H — Message SK shape: "<iso-timestamp>#<ULID>"
# ---------------------------------------------------------------------------


def test_given_valid_request_when_send_message_then_message_id_matches_iso_hash_ulid_pattern() -> None:
    """given valid request, when sendMessage returns, then messageId has format <iso>#<ULID>."""
    mod = _import_handler(
        membership_item=_MEMBERSHIP_ITEM,
        room_item=_ACTIVE_ROOM_ITEM,
    )
    event = _make_event()
    result = mod._dispatch(event)
    assert "errorType" not in result, f"Expected success but got: {result}"
    # messageId is the full DynamoDB SK: "<iso-timestamp>#<ulid>"
    message_id = result.get("messageId", "")
    # Must contain '#' separating ISO timestamp from ULID
    assert "#" in message_id, f"messageId must contain '#' separator, got: {message_id!r}"
    parts = message_id.split("#", 1)
    iso_part, ulid_part = parts[0], parts[1]
    # ISO part must be parseable as a UTC datetime
    from datetime import datetime, timezone
    dt = datetime.fromisoformat(iso_part)
    assert dt.tzinfo is not None, f"ISO part must be timezone-aware, got: {iso_part!r}"
    # ULID part must be 26 chars, Crockford Base32
    assert len(ulid_part) == 26, f"ULID must be 26 chars, got len={len(ulid_part)}: {ulid_part!r}"
    crockford_chars = set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")
    assert all(c in crockford_chars for c in ulid_part), (
        f"ULID contains non-Crockford chars: {ulid_part!r}"
    )


# ---------------------------------------------------------------------------
# Section I — ClientRequestToken passed to TransactWriteItems
# ---------------------------------------------------------------------------


def test_given_valid_request_when_send_message_then_client_request_token_is_64_char_hex() -> None:
    """given valid request, when TransactWriteItems called, then ClientRequestToken is 64-char hex."""
    mod = _import_handler(
        membership_item=_MEMBERSHIP_ITEM,
        room_item=_ACTIVE_ROOM_ITEM,
    )
    event = _make_event()
    mod._dispatch(event)
    ddb = mod._dynamodb_client
    call_kwargs = ddb.transact_write_items.call_args
    # ClientRequestToken is a keyword argument
    token = (
        call_kwargs[1].get("ClientRequestToken")
        if call_kwargs[1]
        else None
    )
    assert token is not None, "ClientRequestToken must be passed to TransactWriteItems"
    assert isinstance(token, str), f"ClientRequestToken must be a string, got {type(token)}"
    assert len(token) == 64, f"ClientRequestToken must be 64-char hex, got len={len(token)}"
    assert all(c in "0123456789abcdef" for c in token), (
        f"ClientRequestToken must be lowercase hex, got: {token!r}"
    )
