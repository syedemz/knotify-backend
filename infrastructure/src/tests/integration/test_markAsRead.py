"""
Integration tests for Mutation.markAsRead (story 8.7).

These tests run against the live dev AWS environment (real DynamoDB).
They drive the handler in-process with a real DynamoDB client, mirroring the
deployed Lambda runtime — same pattern as test_chatQueries.py and
test_sendMessage.py.

Required environment variables (all missing → test is SKIPPED):
  AWS_REGION            — e.g. eu-central-1
  DYNAMODB_ENDPOINT_URL — optional; set to a LocalStack/mock URL for local runs;
                          omit to hit real AWS DynamoDB

Acceptance criteria covered (story 8.7):
  IT-8.7-1  A sends message m1, B calls markAsRead(R, m1) →
            MessageReads has B's row with last_read_message_id=m1 and
            ChatRoomMembership(B, R) also has last_read_message_id=m1 (cached).
  IT-8.7-2  A user not in the room attempts markAsRead → Unauthorized.

Note on IT-8.7-1 subscription assertion:
  The AC also states "A's onReadReceipt subscription receives the event within
  2 seconds".  This assertion requires a live AppSync WebSocket connection and
  is out of scope for in-process integration tests (it would need the e2e test
  in story 8.13).  This test verifies the DynamoDB-side writes that are the
  precondition for the subscription to fire.

Run:
    pytest infrastructure/src/tests/integration/test_markAsRead.py -v -m integration

Skip if live env is not configured:
    pytest -m "not integration"
"""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict
from unittest.mock import MagicMock

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


def _seed_chat_room(ddb, user_a_id: str, user_b_id: str) -> str:
    """Seed ChatRooms + two ChatRoomMembership rows. Returns room_id."""
    room_id = _compute_room_id(user_a_id, user_b_id)
    ddb.put_item(
        TableName="ChatRooms",
        Item={
            "room_id": {"S": room_id},
            "user_a": {"S": min(user_a_id, user_b_id)},
            "user_b": {"S": max(user_a_id, user_b_id)},
            "status": {"S": "active"},
            "friendship_active": {"BOOL": True},
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


def _delete_message_read(ddb, room_id: str, user_id: str) -> None:
    try:
        ddb.delete_item(
            TableName="MessageReads",
            Key={"room_id": {"S": room_id}, "user_id": {"S": user_id}},
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
        "chat_resolver_handler_it_8_7_" + uuid.uuid4().hex,
        os.path.join(handler_dir, "handler.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_mutation_event(
    caller_id: str,
    field_name: str,
    arguments: dict,
    profile_complete: str = "true",
) -> dict:
    return {
        "typeName": "Mutation",
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
# IT-8.7-1: markAsRead writes MessageReads row and caches on membership
# ---------------------------------------------------------------------------


def test_it_8_7_1_mark_as_read_writes_message_reads_and_caches_on_membership() -> None:
    """A sends message m1, B calls markAsRead(R, m1):
    - MessageReads has B's row with last_read_message_id=m1
    - ChatRoomMembership(B, R) has last_read_message_id=m1 (cached copy)
    """
    _require_env("AWS_REGION")

    user_a_id = str(uuid.uuid4())  # sender (A)
    user_b_id = str(uuid.uuid4())  # reader (B)

    # A simulated message SK (the resolver does not actually write a ChatMessages
    # row here — it only marks up to this ID as read)
    message_id = f"{datetime.now(tz=timezone.utc).isoformat()}#TESTULID0001"

    ddb = _dynamodb_client()

    try:
        room_id = _seed_chat_room(ddb, user_a_id, user_b_id)

        mod = _load_handler_module()
        mod._dynamodb_client = ddb
        # Suppress Aurora — not needed for markAsRead
        mod._conn = MagicMock()

        event = _make_mutation_event(
            caller_id=user_b_id,
            field_name="markAsRead",
            arguments={"roomId": room_id, "lastMessageId": message_id},
        )
        result = mod._dispatch(event)

        assert "errorType" not in result, f"Expected success but got error: {result}"
        assert result.get("roomId") == room_id
        assert result.get("userId") == user_b_id
        assert result.get("lastReadMessageId") == message_id
        assert result.get("lastReadAt"), "Expected lastReadAt to be set"

        # Verify MessageReads row written with exact attribute names from dynamodb/main.tf
        # PK=room_id (S), SK=user_id (S)
        mr_resp = ddb.get_item(
            TableName="MessageReads",
            Key={
                "room_id": {"S": room_id},
                "user_id": {"S": user_b_id},
            },
        )
        assert "Item" in mr_resp, (
            f"MessageReads row not found for (room_id={room_id!r}, user_id={user_b_id!r})"
        )
        mr_item = mr_resp["Item"]
        assert mr_item.get("last_read_message_id", {}).get("S") == message_id, (
            f"Expected last_read_message_id={message_id!r} in MessageReads, "
            f"got: {mr_item}"
        )
        assert mr_item.get("last_read_at", {}).get("S"), (
            "Expected last_read_at to be set in MessageReads"
        )

        # Verify ChatRoomMembership(B, R) has cached last_read_message_id
        # PK=user_id (S), SK=room_id (S)
        mbr_resp = ddb.get_item(
            TableName="ChatRoomMembership",
            Key={
                "user_id": {"S": user_b_id},
                "room_id": {"S": room_id},
            },
        )
        assert "Item" in mbr_resp, (
            f"ChatRoomMembership row not found for (user_id={user_b_id!r}, room_id={room_id!r})"
        )
        mbr_item = mbr_resp["Item"]
        assert mbr_item.get("last_read_message_id", {}).get("S") == message_id, (
            f"Expected last_read_message_id={message_id!r} cached on ChatRoomMembership, "
            f"got: {mbr_item}"
        )
        assert mbr_item.get("last_read_at", {}).get("S"), (
            "Expected last_read_at cached on ChatRoomMembership"
        )

    finally:
        _delete_message_read(ddb, room_id, user_b_id)
        _delete_membership(ddb, user_a_id, room_id)
        _delete_membership(ddb, user_b_id, room_id)
        _delete_chat_room(ddb, room_id)


# ---------------------------------------------------------------------------
# IT-8.7-2: markAsRead by non-member returns Unauthorized
# ---------------------------------------------------------------------------


def test_it_8_7_2_mark_as_read_non_member_returns_unauthorized() -> None:
    """A user not in the room attempts markAsRead → Unauthorized error returned;
    no MessageReads row is written."""
    _require_env("AWS_REGION")

    user_a_id = str(uuid.uuid4())
    user_b_id = str(uuid.uuid4())
    outsider_id = str(uuid.uuid4())  # NOT added to membership

    message_id = f"{datetime.now(tz=timezone.utc).isoformat()}#TESTULID0002"

    ddb = _dynamodb_client()

    try:
        room_id = _seed_chat_room(ddb, user_a_id, user_b_id)

        mod = _load_handler_module()
        mod._dynamodb_client = ddb
        mod._conn = MagicMock()

        event = _make_mutation_event(
            caller_id=outsider_id,
            field_name="markAsRead",
            arguments={"roomId": room_id, "lastMessageId": message_id},
        )
        result = mod._dispatch(event)

        assert result.get("errorType") == "Unauthorized", (
            f"Expected Unauthorized for non-member, got: {result}"
        )

        # Confirm no MessageReads row was written for the outsider
        mr_resp = ddb.get_item(
            TableName="MessageReads",
            Key={
                "room_id": {"S": room_id},
                "user_id": {"S": outsider_id},
            },
        )
        assert "Item" not in mr_resp, (
            "MessageReads row must NOT have been written for a non-member"
        )

    finally:
        _delete_membership(ddb, user_a_id, room_id)
        _delete_membership(ddb, user_b_id, room_id)
        _delete_chat_room(ddb, room_id)
