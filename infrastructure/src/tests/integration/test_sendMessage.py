"""
Integration tests for Mutation.sendMessage (story 8.4).

These tests run against the live dev AWS environment: a real DynamoDB (for
ChatRooms, ChatRoomMembership, ChatMessages tables). They drive the handler
in-process with the real DynamoDB client, mirroring the deployed runtime.

Required environment variables (all missing → test is SKIPPED):
  AWS_REGION            — e.g. eu-central-1
  DYNAMODB_ENDPOINT_URL — optional; set to a LocalStack/mock URL for local runs;
                          omit to hit real AWS DynamoDB

Acceptance criteria covered (story 8.4):
  IT-8.4-1  A sends a message in a room A and B share → ChatMessages row appears
            with sender_id=A, content_type=text, delivered_at within 1 second
            of the call.
  IT-8.4-2  A user not in the room attempts sendMessage with the room_id →
            resolver returns Unauthorized.
  IT-8.4-3  room.status == 'deactivated' → sendMessage returns RoomDeactivated.
  IT-8.4-4  room.status == 'active' but room.friendship_active == false →
            sendMessage returns RoomReadOnly.

Run:
    pytest infrastructure/src/tests/integration/test_sendMessage.py -v -m integration

Skip if live env is not configured:
    pytest -m "not integration"
"""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict

import pytest

# ---------------------------------------------------------------------------
# Marker
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------


def _require_env(name: str) -> str:
    """Skip test (not error) when required env var is absent."""
    value = os.environ.get(name)
    if not value:
        pytest.skip(
            f"Required env var {name!r} is not set — "
            "live dev AWS environment not configured."
        )
    return value


# ---------------------------------------------------------------------------
# DynamoDB helper
# ---------------------------------------------------------------------------


def _dynamodb_client():
    """Return a boto3 DynamoDB client, optionally pointed at a local mock."""
    import boto3

    region = _require_env("AWS_REGION")
    endpoint_url = os.environ.get("DYNAMODB_ENDPOINT_URL")
    kwargs: Dict[str, Any] = {"region_name": region}
    if endpoint_url:
        kwargs["endpoint_url"] = endpoint_url
    return boto3.client("dynamodb", **kwargs)


def _compute_room_id(user_a: str, user_b: str) -> str:
    lo, hi = min(user_a, user_b), max(user_a, user_b)
    return hashlib.sha256(f"{lo}:{hi}".encode()).hexdigest()


def _seed_chat_room(
    ddb,
    user_a_id: str,
    user_b_id: str,
    *,
    status: str = "active",
    friendship_active: bool = True,
) -> str:
    """Seed ChatRooms + two ChatRoomMembership rows. Returns room_id."""
    room_id = _compute_room_id(user_a_id, user_b_id)
    ddb.put_item(
        TableName="ChatRooms",
        Item={
            "room_id": {"S": room_id},
            "user_a": {"S": min(user_a_id, user_b_id)},
            "user_b": {"S": max(user_a_id, user_b_id)},
            "status": {"S": status},
            "friendship_active": {"BOOL": friendship_active},
        },
    )
    for uid in (user_a_id, user_b_id):
        ddb.put_item(
            TableName="ChatRoomMembership",
            Item={
                "user_id": {"S": uid},
                "room_id": {"S": room_id},
            },
        )
    return room_id


def _delete_chat_room(ddb, room_id: str) -> None:
    try:
        ddb.delete_item(
            TableName="ChatRooms",
            Key={"room_id": {"S": room_id}},
        )
    except Exception:
        pass


def _delete_membership(ddb, user_id: str, room_id: str) -> None:
    try:
        ddb.delete_item(
            TableName="ChatRoomMembership",
            Key={"user_id": {"S": user_id}, "room_id": {"S": room_id}},
        )
    except Exception:
        pass


def _delete_message(ddb, room_id: str, sk: str) -> None:
    try:
        ddb.delete_item(
            TableName="ChatMessages",
            Key={"room_id": {"S": room_id}, "sk": {"S": sk}},
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Handler loader (in-process, uses real DynamoDB)
# ---------------------------------------------------------------------------


def _load_handler_module():
    """Import the chat_resolver handler with real knotify_obs layer."""
    import importlib
    import importlib.util
    import sys

    handler_dir = os.path.normpath(
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "../../functions/chat_resolver",
        )
    )
    obs_layer_dir = os.path.normpath(
        os.path.join(handler_dir, "../../layers/observability")
    )
    db_layer_dir = os.path.normpath(
        os.path.join(handler_dir, "../../layers/db")
    )

    for d in (handler_dir, obs_layer_dir, db_layer_dir):
        if d not in sys.path:
            sys.path.insert(0, d)

    spec = importlib.util.spec_from_file_location(
        "chat_resolver_handler_it_8_4_" + uuid.uuid4().hex,
        os.path.join(handler_dir, "handler.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_send_message_event(
    sender_id: str,
    room_id: str,
    content: str = "hello",
    content_type: str = "text",
    profile_complete: str = "true",
) -> dict:
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


# ---------------------------------------------------------------------------
# IT-8.4-1: A sends a message → ChatMessages row with correct fields and
#           delivered_at within 1 second
# ---------------------------------------------------------------------------


def test_it_8_4_1_send_message_creates_chat_messages_row() -> None:
    """A sends a message in a room A and B share → ChatMessages row appears with
    sender_id=A, content_type=text, delivered_at within 1 second of the call."""
    _require_env("AWS_REGION")

    user_a_id = str(uuid.uuid4())
    user_b_id = str(uuid.uuid4())
    room_sk: str | None = None

    ddb = _dynamodb_client()

    try:
        room_id = _seed_chat_room(ddb, user_a_id, user_b_id)

        mod = _load_handler_module()
        mod._dynamodb_client = ddb
        # No Aurora needed for sendMessage — suppress DB connection
        from unittest.mock import MagicMock
        mod._conn = MagicMock()

        before_send = datetime.now(tz=timezone.utc)
        event = _make_send_message_event(
            sender_id=user_a_id,
            room_id=room_id,
            content="integration test message",
        )
        result = mod._dispatch(event)

        assert "errorType" not in result, f"Expected success but got: {result}"
        # messageId is the full DynamoDB SK: "<iso>#<ulid>"
        room_sk = result.get("messageId")
        assert room_sk is not None, "Expected 'messageId' in result"

        # Verify ChatMessages row in DynamoDB
        row_resp = ddb.get_item(
            TableName="ChatMessages",
            Key={"room_id": {"S": room_id}, "sk": {"S": room_sk}},
        )
        assert "Item" in row_resp, f"ChatMessages row not found for sk={room_sk!r}"
        item = row_resp["Item"]
        assert item["sender_id"]["S"] == user_a_id, (
            f"sender_id mismatch: expected {user_a_id}, got {item.get('sender_id')}"
        )
        assert item["content_type"]["S"] == "text", (
            f"content_type mismatch: {item.get('content_type')}"
        )

        # delivered_at must be within 1 second of before_send
        delivered_at_str = item["delivered_at"]["S"]
        delivered_at = datetime.fromisoformat(delivered_at_str)
        delta = abs((delivered_at - before_send).total_seconds())
        assert delta < 1.0, (
            f"delivered_at {delivered_at_str!r} is {delta:.3f}s away from send time "
            f"(must be < 1s)"
        )

    finally:
        if room_sk:
            _delete_message(ddb, room_id, room_sk)
        _delete_membership(ddb, user_a_id, room_id)
        _delete_membership(ddb, user_b_id, room_id)
        _delete_chat_room(ddb, room_id)


# ---------------------------------------------------------------------------
# IT-8.4-2: Sender not in the room → Unauthorized
# ---------------------------------------------------------------------------


def test_it_8_4_2_sender_not_in_room_returns_unauthorized() -> None:
    """A user not in the room attempts sendMessage → resolver returns Unauthorized."""
    _require_env("AWS_REGION")

    user_a_id = str(uuid.uuid4())
    user_b_id = str(uuid.uuid4())
    outsider_id = str(uuid.uuid4())  # NOT added to membership

    ddb = _dynamodb_client()

    try:
        room_id = _seed_chat_room(ddb, user_a_id, user_b_id)

        mod = _load_handler_module()
        mod._dynamodb_client = ddb
        from unittest.mock import MagicMock
        mod._conn = MagicMock()

        event = _make_send_message_event(
            sender_id=outsider_id,
            room_id=room_id,
            content="intruder message",
        )
        result = mod._dispatch(event)

        assert result["errorType"] == "Unauthorized", (
            f"Expected Unauthorized for non-member, got: {result}"
        )

    finally:
        _delete_membership(ddb, user_a_id, room_id)
        _delete_membership(ddb, user_b_id, room_id)
        _delete_chat_room(ddb, room_id)


# ---------------------------------------------------------------------------
# IT-8.4-3: room.status == 'deactivated' → RoomDeactivated
# ---------------------------------------------------------------------------


def test_it_8_4_3_deactivated_room_returns_room_deactivated() -> None:
    """room.status == 'deactivated' → sendMessage returns RoomDeactivated."""
    _require_env("AWS_REGION")

    user_a_id = str(uuid.uuid4())
    user_b_id = str(uuid.uuid4())

    ddb = _dynamodb_client()

    try:
        room_id = _seed_chat_room(
            ddb, user_a_id, user_b_id, status="deactivated", friendship_active=True
        )

        mod = _load_handler_module()
        mod._dynamodb_client = ddb
        from unittest.mock import MagicMock
        mod._conn = MagicMock()

        event = _make_send_message_event(
            sender_id=user_a_id,
            room_id=room_id,
            content="message to deactivated room",
        )
        result = mod._dispatch(event)

        assert result["errorType"] == "RoomDeactivated", (
            f"Expected RoomDeactivated for deactivated room, got: {result}"
        )

    finally:
        _delete_membership(ddb, user_a_id, room_id)
        _delete_membership(ddb, user_b_id, room_id)
        _delete_chat_room(ddb, room_id)


# ---------------------------------------------------------------------------
# IT-8.4-4: room.status == 'active' but friendship_active == false → RoomReadOnly
# ---------------------------------------------------------------------------


def test_it_8_4_4_active_room_friendship_inactive_returns_room_read_only() -> None:
    """room.status == 'active' but friendship_active == false → sendMessage returns RoomReadOnly."""
    _require_env("AWS_REGION")

    user_a_id = str(uuid.uuid4())
    user_b_id = str(uuid.uuid4())

    ddb = _dynamodb_client()

    try:
        room_id = _seed_chat_room(
            ddb, user_a_id, user_b_id, status="active", friendship_active=False
        )

        mod = _load_handler_module()
        mod._dynamodb_client = ddb
        from unittest.mock import MagicMock
        mod._conn = MagicMock()

        event = _make_send_message_event(
            sender_id=user_a_id,
            room_id=room_id,
            content="message to read-only room",
        )
        result = mod._dispatch(event)

        assert result["errorType"] == "RoomReadOnly", (
            f"Expected RoomReadOnly for friendship_active=false, got: {result}"
        )

    finally:
        _delete_membership(ddb, user_a_id, room_id)
        _delete_membership(ddb, user_b_id, room_id)
        _delete_chat_room(ddb, room_id)
