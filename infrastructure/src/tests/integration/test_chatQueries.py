"""
Integration tests for Query.listMyRooms and Query.messagesByChatRoom (story 8.5).

These tests run against the live dev AWS environment: a real DynamoDB for the
chat tables.  They drive the handler in-process with a real DynamoDB client,
mirroring the deployed Lambda runtime — same pattern as test_createOrGetRoom.py
and test_sendMessage.py.

Required environment variables (all missing → test is SKIPPED):
  AWS_REGION            — e.g. eu-central-1
  DYNAMODB_ENDPOINT_URL — optional; set to a LocalStack/mock URL for local runs;
                          omit to hit real AWS DynamoDB

Acceptance criteria covered (story 8.5):
  IT-8.5-1  messagesByChatRoom returns the latest 20 messages by default.
  IT-8.5-2  passing createdAtGt to messagesByChatRoom fetches only newer messages.
  IT-8.5-3  listMyRooms for a user with two rooms returns both, in last_message_at
            descending order.
  IT-8.5-4  messagesByChatRoom for a user not in the room returns Unauthorized.

Run:
    pytest infrastructure/src/tests/integration/test_chatQueries.py -v -m integration

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
# DynamoDB helpers
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
    last_message_at: str | None = None,
) -> str:
    """Seed ChatRooms + two ChatRoomMembership rows. Returns room_id."""
    room_id = _compute_room_id(user_a_id, user_b_id)
    item: Dict[str, Any] = {
        "room_id": {"S": room_id},
        "user_a": {"S": min(user_a_id, user_b_id)},
        "user_b": {"S": max(user_a_id, user_b_id)},
        "status": {"S": status},
        "friendship_active": {"BOOL": friendship_active},
    }
    if last_message_at is not None:
        item["last_message_at"] = {"S": last_message_at}
    ddb.put_item(TableName="ChatRooms", Item=item)
    for uid in (user_a_id, user_b_id):
        ddb.put_item(
            TableName="ChatRoomMembership",
            Item={
                "user_id": {"S": uid},
                "room_id": {"S": room_id},
            },
        )
    return room_id


def _seed_message(
    ddb,
    room_id: str,
    sender_id: str,
    content: str,
    sk: str,
) -> None:
    """Seed a single ChatMessages row with the provided SK."""
    # Parse the ISO timestamp prefix from the SK (format: "<iso>#<ulid>")
    delivered_at = sk.split("#")[0] if "#" in sk else sk
    ddb.put_item(
        TableName="ChatMessages",
        Item={
            "room_id": {"S": room_id},
            "sk": {"S": sk},
            "sender_id": {"S": sender_id},
            "content": {"S": content},
            "content_type": {"S": "text"},
            "delivered_at": {"S": delivered_at},
        },
    )


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
# Handler loader (in-process, uses real layers)
# ---------------------------------------------------------------------------


def _load_handler_module():
    """Import the chat_resolver handler with real knotify_obs and knotify_db layers."""
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
        "chat_resolver_handler_it_8_5_" + uuid.uuid4().hex,
        os.path.join(handler_dir, "handler.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_query_event(
    caller_id: str,
    field_name: str,
    arguments: dict,
    profile_complete: str = "true",
) -> dict:
    return {
        "typeName": "Query",
        "fieldName": field_name,
        "identity": {
            "sub": caller_id,
            "claims": {
                "sub": caller_id,
                "custom:profile_complete": profile_complete,
            },
        },
        "arguments": arguments,
        "source": None,
        "request": {"headers": {}},
    }


# ---------------------------------------------------------------------------
# IT-8.5-1: messagesByChatRoom returns the latest 20 messages by default
# ---------------------------------------------------------------------------


def test_it_8_5_1_messages_by_chat_room_default_limit_20() -> None:
    """messagesByChatRoom returns at most 20 messages by default (no limit arg)."""
    _require_env("AWS_REGION")

    user_a_id = str(uuid.uuid4())
    user_b_id = str(uuid.uuid4())
    seeded_sks: list[str] = []

    ddb = _dynamodb_client()

    try:
        room_id = _seed_chat_room(ddb, user_a_id, user_b_id)

        # Seed 25 messages — handler must return only 20 (default limit)
        base_ts = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        for i in range(25):
            ts = (base_ts + timedelta(minutes=i)).isoformat()
            sk = f"{ts}#ULID{i:04d}"
            seeded_sks.append(sk)
            _seed_message(ddb, room_id, user_a_id, f"msg {i}", sk)

        mod = _load_handler_module()
        mod._dynamodb_client = ddb
        # Suppress Aurora connection — not needed for query resolvers
        from unittest.mock import MagicMock
        mod._conn = MagicMock()

        event = _make_query_event(
            caller_id=user_a_id,
            field_name="messagesByChatRoom",
            arguments={"roomId": room_id},
        )
        result = mod._dispatch(event)

        assert "errorType" not in result, f"Expected success but got: {result}"
        assert "items" in result, f"Expected 'items' key, got: {result.keys()}"
        # Default limit is 20; DynamoDB returns newest-first (ScanIndexForward=False)
        assert len(result["items"]) == 20, (
            f"Expected 20 messages (default limit), got {len(result['items'])}"
        )
        # Since ScanIndexForward=False, the first returned item should be the
        # most recent message (highest SK). All returned items must be messages.
        for msg in result["items"]:
            assert "messageId" in msg, f"Message missing 'messageId': {msg}"
            assert "content" in msg, f"Message missing 'content': {msg}"

    finally:
        for sk in seeded_sks:
            _delete_message(ddb, room_id, sk)
        _delete_membership(ddb, user_a_id, room_id)
        _delete_membership(ddb, user_b_id, room_id)
        _delete_chat_room(ddb, room_id)


# ---------------------------------------------------------------------------
# IT-8.5-2: passing createdAtGt fetches only newer messages
# ---------------------------------------------------------------------------


def test_it_8_5_2_messages_by_chat_room_created_at_gt_filters_older_messages() -> None:
    """passing createdAtGt to messagesByChatRoom fetches only newer messages."""
    _require_env("AWS_REGION")

    user_a_id = str(uuid.uuid4())
    user_b_id = str(uuid.uuid4())
    seeded_sks: list[str] = []

    ddb = _dynamodb_client()

    try:
        room_id = _seed_chat_room(ddb, user_a_id, user_b_id)

        # Seed 5 old messages (before the cutoff) and 3 new messages (after)
        base_ts = datetime(2024, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
        cutoff_ts = (base_ts + timedelta(hours=5)).isoformat()

        # Old messages: t+0h through t+4h (before cutoff at t+5h)
        for i in range(5):
            ts = (base_ts + timedelta(hours=i)).isoformat()
            sk = f"{ts}#OLDULID{i:04d}"
            seeded_sks.append(sk)
            _seed_message(ddb, room_id, user_a_id, f"old msg {i}", sk)

        # New messages: t+6h through t+8h (after cutoff at t+5h)
        new_sks: list[str] = []
        for i in range(3):
            ts = (base_ts + timedelta(hours=6 + i)).isoformat()
            sk = f"{ts}#NEWULID{i:04d}"
            seeded_sks.append(sk)
            new_sks.append(sk)
            _seed_message(ddb, room_id, user_b_id, f"new msg {i}", sk)

        mod = _load_handler_module()
        mod._dynamodb_client = ddb
        from unittest.mock import MagicMock
        mod._conn = MagicMock()

        event = _make_query_event(
            caller_id=user_a_id,
            field_name="messagesByChatRoom",
            arguments={"roomId": room_id, "createdAtGt": cutoff_ts},
        )
        result = mod._dispatch(event)

        assert "errorType" not in result, f"Expected success but got: {result}"
        items = result.get("items", [])
        # Only the 3 messages newer than cutoff_ts should be returned
        assert len(items) == 3, (
            f"Expected 3 messages after createdAtGt={cutoff_ts!r}, got {len(items)}"
        )
        # All returned message IDs must be in the new_sks set
        returned_ids = {msg["messageId"] for msg in items}
        assert returned_ids == set(new_sks), (
            f"Returned message IDs {returned_ids} do not match expected {set(new_sks)}"
        )

    finally:
        for sk in seeded_sks:
            _delete_message(ddb, room_id, sk)
        _delete_membership(ddb, user_a_id, room_id)
        _delete_membership(ddb, user_b_id, room_id)
        _delete_chat_room(ddb, room_id)


# ---------------------------------------------------------------------------
# IT-8.5-3: listMyRooms returns both rooms in last_message_at descending order
# ---------------------------------------------------------------------------


def test_it_8_5_3_list_my_rooms_returns_both_rooms_sorted_descending() -> None:
    """listMyRooms for a user with two rooms returns both, in last_message_at
    descending order (most-recently-messaged room first)."""
    _require_env("AWS_REGION")

    user_id = str(uuid.uuid4())
    other_user_1 = str(uuid.uuid4())
    other_user_2 = str(uuid.uuid4())

    ddb = _dynamodb_client()

    try:
        # Room 1 — older last_message_at
        room_1_id = _seed_chat_room(
            ddb,
            user_id,
            other_user_1,
            last_message_at="2024-01-01T10:00:00+00:00",
        )
        # Room 2 — newer last_message_at
        room_2_id = _seed_chat_room(
            ddb,
            user_id,
            other_user_2,
            last_message_at="2024-01-02T10:00:00+00:00",
        )

        mod = _load_handler_module()
        mod._dynamodb_client = ddb
        from unittest.mock import MagicMock
        mod._conn = MagicMock()

        event = _make_query_event(
            caller_id=user_id,
            field_name="listMyRooms",
            arguments={},
        )
        result = mod._dispatch(event)

        assert isinstance(result, list), f"Expected list, got: {type(result)}"
        assert len(result) == 2, f"Expected 2 rooms, got {len(result)}"

        room_ids = [r["roomId"] for r in result]
        # Room 2 (newer last_message_at) must come first
        assert room_ids[0] == room_2_id, (
            f"Expected newer room ({room_2_id}) first, got {room_ids[0]!r}"
        )
        assert room_ids[1] == room_1_id, (
            f"Expected older room ({room_1_id}) second, got {room_ids[1]!r}"
        )

    finally:
        for uid in (user_id, other_user_1):
            _delete_membership(ddb, uid, room_1_id)
        for uid in (user_id, other_user_2):
            _delete_membership(ddb, uid, room_2_id)
        _delete_chat_room(ddb, room_1_id)
        _delete_chat_room(ddb, room_2_id)


# ---------------------------------------------------------------------------
# IT-8.5-4: messagesByChatRoom for a non-member returns Unauthorized
# ---------------------------------------------------------------------------


def test_it_8_5_4_messages_by_chat_room_non_member_returns_unauthorized() -> None:
    """messagesByChatRoom for a user not in the room returns Unauthorized."""
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

        event = _make_query_event(
            caller_id=outsider_id,
            field_name="messagesByChatRoom",
            arguments={"roomId": room_id},
        )
        result = mod._dispatch(event)

        assert result.get("errorType") == "Unauthorized", (
            f"Expected Unauthorized for non-member, got: {result}"
        )

    finally:
        _delete_membership(ddb, user_a_id, room_id)
        _delete_membership(ddb, user_b_id, room_id)
        _delete_chat_room(ddb, room_id)
