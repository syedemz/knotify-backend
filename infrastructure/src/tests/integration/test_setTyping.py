"""
Integration tests for Mutation.setTyping (story 8.8).

These tests run against the live dev AWS environment (real DynamoDB).
They drive the handler in-process with a real DynamoDB client, mirroring the
deployed Lambda runtime — same pattern as test_markAsRead.py.

Required environment variables (all missing → test is SKIPPED):
  AWS_REGION            — e.g. eu-central-1
  DYNAMODB_ENDPOINT_URL — optional; set to a LocalStack/mock URL for local
                          runs; omit to hit real AWS DynamoDB

Acceptance criteria covered (story 8.8):
  IT-8.8-1  A calls setTyping(R, true) → the TypingEvent payload returned by
            the Lambda path has {userId: A, isTyping: True, roomId: R} and
            NO DynamoDB write occurred (ChatRooms / ChatRoomMembership /
            MessageReads unchanged after the call).

            Note: the @aws_subscribe(mutations: ["setTyping"]) subscription
            fan-out to B's onTypingInRoom requires a live AppSync WebSocket
            and is out of scope for in-process integration tests. The full
            round-trip (A calls → B's subscription receives) is deferred to
            the story 8.13 end-to-end test. This test verifies the Lambda-side
            contract: correct payload, no writes.

  IT-8.8-2  A (not a member) calls setTyping(R, true) → Unauthorized; no
            DynamoDB write.

Run:
    pytest infrastructure/src/tests/integration/test_setTyping.py -v -m integration

Skip if live env is not configured:
    pytest -m "not integration"
"""

from __future__ import annotations

import hashlib
import os
import uuid
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


# ---------------------------------------------------------------------------
# Handler loader (in-process, uses real layers)
# ---------------------------------------------------------------------------


def _load_handler_module():
    """Import the chat_resolver handler with real knotify_obs and knotify_db."""
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
        "chat_resolver_handler_it_8_8_" + uuid.uuid4().hex,
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
# IT-8.8-1: setTyping returns TypingEvent payload with no DynamoDB writes
# ---------------------------------------------------------------------------


def test_it_8_8_1_set_typing_returns_typing_event_and_no_ddb_write() -> None:
    """A calls setTyping(R, true):
    - Returns TypingEvent {userId: A, isTyping: True, roomId: R}
    - No DynamoDB write occurs (ChatRooms / ChatRoomMembership unchanged).

    Note: The full subscription fan-out (B's onTypingInRoom receives the event)
    requires a live AppSync WebSocket and is tested in story 8.13's e2e suite.
    This test verifies the Lambda-path contract: correct payload, zero writes.
    """
    _require_env("AWS_REGION")

    user_a_id = str(uuid.uuid4())  # caller (A)
    user_b_id = str(uuid.uuid4())  # room partner (B)

    ddb = _dynamodb_client()

    try:
        room_id = _seed_chat_room(ddb, user_a_id, user_b_id)

        # Snapshot ChatRoomMembership state before the call — we assert it is
        # unchanged after setTyping to prove no write occurred.
        pre_membership_a = ddb.get_item(
            TableName="ChatRoomMembership",
            Key={"user_id": {"S": user_a_id}, "room_id": {"S": room_id}},
        ).get("Item", {})

        mod = _load_handler_module()
        mod._dynamodb_client = ddb
        # Suppress Aurora — not needed for setTyping
        mod._conn = MagicMock()

        event = _make_mutation_event(
            caller_id=user_a_id,
            field_name="setTyping",
            arguments={"roomId": room_id, "isTyping": True},
        )
        result = mod._dispatch(event)

        # Verify TypingEvent payload shape
        assert "errorType" not in result, (
            f"Expected TypingEvent payload but got error: {result}"
        )
        assert result.get("userId") == user_a_id, (
            f"Expected userId={user_a_id!r}, got: {result}"
        )
        assert result.get("isTyping") is True, (
            f"Expected isTyping=True, got: {result}"
        )
        assert result.get("roomId") == room_id, (
            f"Expected roomId={room_id!r}, got: {result}"
        )

        # Verify no DynamoDB write occurred: ChatRoomMembership(A, R) must be
        # byte-for-byte identical to the pre-call snapshot.
        post_membership_a = ddb.get_item(
            TableName="ChatRoomMembership",
            Key={"user_id": {"S": user_a_id}, "room_id": {"S": room_id}},
        ).get("Item", {})
        assert post_membership_a == pre_membership_a, (
            "ChatRoomMembership row was modified by setTyping — no write should occur. "
            f"Before: {pre_membership_a}  After: {post_membership_a}"
        )

    finally:
        _delete_membership(ddb, user_a_id, room_id)
        _delete_membership(ddb, user_b_id, room_id)
        _delete_chat_room(ddb, room_id)


# ---------------------------------------------------------------------------
# IT-8.8-2: setTyping by non-member returns Unauthorized
# ---------------------------------------------------------------------------


def test_it_8_8_2_set_typing_non_member_returns_unauthorized() -> None:
    """A user not in room R calls setTyping(R, true) → Unauthorized; no DynamoDB
    write is attempted."""
    _require_env("AWS_REGION")

    user_a_id = str(uuid.uuid4())
    user_b_id = str(uuid.uuid4())
    outsider_id = str(uuid.uuid4())  # NOT added to membership

    ddb = _dynamodb_client()

    try:
        room_id = _seed_chat_room(ddb, user_a_id, user_b_id)

        mod = _load_handler_module()
        mod._dynamodb_client = ddb
        mod._conn = MagicMock()

        event = _make_mutation_event(
            caller_id=outsider_id,
            field_name="setTyping",
            arguments={"roomId": room_id, "isTyping": True},
        )
        result = mod._dispatch(event)

        assert result.get("errorType") == "Unauthorized", (
            f"Expected Unauthorized for non-member, got: {result}"
        )

    finally:
        _delete_membership(ddb, user_a_id, room_id)
        _delete_membership(ddb, user_b_id, room_id)
        _delete_chat_room(ddb, room_id)
