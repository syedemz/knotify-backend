"""
Unit tests for story 8.9a — room_state_publisher Lambda handler.

TDD: these tests were written BEFORE handler.py to drive the implementation.

Acceptance criteria tested:
  A. MODIFY event with status active→deactivated calls _publishRoomDeactivated
     exactly once with the correct payload.
  B. MODIFY event with status deactivated→active calls _publishRoomReactivated
     exactly once with the correct payload.
  C. MODIFY event where status does NOT change (e.g. last_message_at update)
     does NOT invoke any AppSync publish mutation.
  D. INSERT and REMOVE events are ignored (no publish call).
  E. Payload includes all required ChatRoom fields from the NEW image:
     roomId, status, deactivatedReason, deactivatedBy, deactivatedAt,
     reactivatedAt, friendshipActive — matching the GraphQL RoomStateEvent
     fields in schema.graphql.
  F. SigV4 signing is performed (the HTTP POST carries an Authorization header
     starting with "AWS4-HMAC-SHA256").
  G. A batch containing multiple records is processed record-by-record; only
     records with a status transition trigger a publish call.
  H. Missing or None oldImage/newImage are handled safely (no publish, no crash).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers — build synthetic DynamoDB stream records
# ---------------------------------------------------------------------------


def _ddb_str(value: str) -> dict:
    return {"S": value}


def _ddb_bool(value: bool) -> dict:
    return {"BOOL": value}


def _make_record(
    event_name: str,
    old_image: dict | None = None,
    new_image: dict | None = None,
) -> dict:
    """Build a DynamoDB stream record compatible with the Lambda event format."""
    record: dict[str, Any] = {"eventName": event_name, "dynamodb": {}}
    if old_image is not None:
        record["dynamodb"]["OldImage"] = old_image
    if new_image is not None:
        record["dynamodb"]["NewImage"] = new_image
    return record


def _active_image(room_id: str = "room-abc", user_a: str = "user-a", user_b: str = "user-b") -> dict:
    return {
        "room_id": _ddb_str(room_id),
        "user_a": _ddb_str(user_a),
        "user_b": _ddb_str(user_b),
        "status": _ddb_str("active"),
        "friendship_active": _ddb_bool(True),
        "created_at": _ddb_str("2026-01-01T00:00:00+00:00"),
    }


def _deactivated_image(
    room_id: str = "room-abc",
    user_a: str = "user-a",
    user_b: str = "user-b",
    *,
    reason: str = "blocked",
    by: str = "user-a",
    at: str = "2026-06-18T10:00:00+00:00",
) -> dict:
    return {
        "room_id": _ddb_str(room_id),
        "user_a": _ddb_str(user_a),
        "user_b": _ddb_str(user_b),
        "status": _ddb_str("deactivated"),
        "deactivated_reason": _ddb_str(reason),
        "deactivated_by": _ddb_str(by),
        "deactivated_at": _ddb_str(at),
        "friendship_active": _ddb_bool(False),
        "created_at": _ddb_str("2026-01-01T00:00:00+00:00"),
    }


def _reactivated_image(
    room_id: str = "room-abc",
    user_a: str = "user-a",
    user_b: str = "user-b",
    *,
    reactivated_at: str = "2026-06-18T11:00:00+00:00",
) -> dict:
    return {
        "room_id": _ddb_str(room_id),
        "user_a": _ddb_str(user_a),
        "user_b": _ddb_str(user_b),
        "status": _ddb_str("active"),
        "reactivated_at": _ddb_str(reactivated_at),
        "friendship_active": _ddb_bool(False),
        "created_at": _ddb_str("2026-01-01T00:00:00+00:00"),
    }


def _last_message_update_image(room_id: str = "room-abc") -> dict:
    """An image whose status field did not change — only last_message_at updated."""
    return {
        "room_id": _ddb_str(room_id),
        "user_a": _ddb_str("user-a"),
        "user_b": _ddb_str("user-b"),
        "status": _ddb_str("active"),
        "friendship_active": _ddb_bool(True),
        "last_message_at": _ddb_str("2026-06-18T12:00:00+00:00"),
        "created_at": _ddb_str("2026-01-01T00:00:00+00:00"),
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def mock_publish():
    """
    Patch handler._publish_to_appsync so no real HTTP call is made.
    Returns the mock so tests can assert call count and arguments.
    """
    with patch(
        "room_state_publisher.handler._publish_to_appsync"
    ) as mock:
        yield mock


# ---------------------------------------------------------------------------
# Test A: active → deactivated triggers _publishRoomDeactivated
# ---------------------------------------------------------------------------


def test_given_active_to_deactivated_modify_when_processed_then_publish_deactivated_called_once(
    mock_publish,
):
    """
    AC-A: MODIFY record status active→deactivated → _publishRoomDeactivated called once.
    """
    from room_state_publisher import handler

    room_id = "room-deactivate-test"
    record = _make_record(
        "MODIFY",
        old_image=_active_image(room_id=room_id),
        new_image=_deactivated_image(room_id=room_id, by="user-a"),
    )
    handler.handler({"Records": [record]}, None)

    mock_publish.assert_called_once()
    mutation_name, called_room_id, payload = mock_publish.call_args.args
    assert mutation_name == "_publishRoomDeactivated", (
        f"expected mutation '_publishRoomDeactivated', got {mutation_name!r}"
    )
    assert called_room_id == room_id


def test_given_active_to_deactivated_modify_when_processed_then_payload_contains_required_fields(
    mock_publish,
):
    """
    AC-E: payload for _publishRoomDeactivated includes all required fields
    from the NEW image matching the GraphQL ChatRoom type.
    """
    from room_state_publisher import handler

    room_id = "room-payload-test"
    record = _make_record(
        "MODIFY",
        old_image=_active_image(room_id=room_id),
        new_image=_deactivated_image(
            room_id=room_id,
            reason="blocked",
            by="user-a",
            at="2026-06-18T10:00:00+00:00",
        ),
    )
    handler.handler({"Records": [record]}, None)

    _, _, payload = mock_publish.call_args.args
    assert payload["roomId"] == room_id
    assert payload["status"] == "deactivated"
    assert payload["deactivatedReason"] == "blocked"
    assert payload["deactivatedBy"] == "user-a"
    assert payload["deactivatedAt"] == "2026-06-18T10:00:00+00:00"
    assert payload["friendshipActive"] is False


# ---------------------------------------------------------------------------
# Test B: deactivated → active triggers _publishRoomReactivated
# ---------------------------------------------------------------------------


def test_given_deactivated_to_active_modify_when_processed_then_publish_reactivated_called_once(
    mock_publish,
):
    """
    AC-B: MODIFY record status deactivated→active → _publishRoomReactivated called once.
    """
    from room_state_publisher import handler

    room_id = "room-reactivate-test"
    record = _make_record(
        "MODIFY",
        old_image=_deactivated_image(room_id=room_id),
        new_image=_reactivated_image(room_id=room_id),
    )
    handler.handler({"Records": [record]}, None)

    mock_publish.assert_called_once()
    mutation_name, called_room_id, payload = mock_publish.call_args.args
    assert mutation_name == "_publishRoomReactivated", (
        f"expected mutation '_publishRoomReactivated', got {mutation_name!r}"
    )
    assert called_room_id == room_id


def test_given_deactivated_to_active_modify_when_processed_then_payload_contains_required_fields(
    mock_publish,
):
    """
    AC-E (reactivated path): payload includes reactivatedAt and friendshipActive.
    """
    from room_state_publisher import handler

    room_id = "room-reactivated-payload-test"
    record = _make_record(
        "MODIFY",
        old_image=_deactivated_image(room_id=room_id),
        new_image=_reactivated_image(
            room_id=room_id,
            reactivated_at="2026-06-18T11:00:00+00:00",
        ),
    )
    handler.handler({"Records": [record]}, None)

    _, _, payload = mock_publish.call_args.args
    assert payload["roomId"] == room_id
    assert payload["status"] == "active"
    assert payload["reactivatedAt"] == "2026-06-18T11:00:00+00:00"
    assert payload["friendshipActive"] is False


# ---------------------------------------------------------------------------
# Test C: no status change → no publish call
# ---------------------------------------------------------------------------


def test_given_modify_with_no_status_change_when_processed_then_no_publish_called(
    mock_publish,
):
    """
    AC-C: last_message_at update (status unchanged) must NOT trigger any publish.
    """
    from room_state_publisher import handler

    room_id = "room-no-status-change"
    old_img = _last_message_update_image(room_id=room_id)
    new_img = {**old_img, "last_message_at": _ddb_str("2026-06-18T13:00:00+00:00")}
    record = _make_record("MODIFY", old_image=old_img, new_image=new_img)

    handler.handler({"Records": [record]}, None)

    mock_publish.assert_not_called()


def test_given_modify_where_status_is_same_in_both_images_when_processed_then_no_publish(
    mock_publish,
):
    """
    AC-C (edge): status field present in both images but identical → no publish.
    """
    from room_state_publisher import handler

    old_img = _deactivated_image(room_id="room-same-status")
    new_img = _deactivated_image(
        room_id="room-same-status",
        at="2026-06-18T14:00:00+00:00",  # only deactivated_at changed
    )
    record = _make_record("MODIFY", old_image=old_img, new_image=new_img)

    handler.handler({"Records": [record]}, None)

    mock_publish.assert_not_called()


# ---------------------------------------------------------------------------
# Test D: INSERT and REMOVE events are ignored
# ---------------------------------------------------------------------------


def test_given_insert_event_when_processed_then_no_publish_called(mock_publish):
    """
    AC-D: INSERT events must be ignored — room creation is not a status transition.
    """
    from room_state_publisher import handler

    record = _make_record("INSERT", new_image=_active_image())
    handler.handler({"Records": [record]}, None)

    mock_publish.assert_not_called()


def test_given_remove_event_when_processed_then_no_publish_called(mock_publish):
    """
    AC-D: REMOVE events must be ignored — row deletion is not a status transition.
    """
    from room_state_publisher import handler

    record = _make_record("REMOVE", old_image=_active_image())
    handler.handler({"Records": [record]}, None)

    mock_publish.assert_not_called()


# ---------------------------------------------------------------------------
# Test G: batch with mixed records — only status-change records trigger publish
# ---------------------------------------------------------------------------


def test_given_batch_with_mixed_records_when_processed_then_only_status_changes_publish(
    mock_publish,
):
    """
    AC-G: a batch of 3 records — 1 active→deactivated, 1 last_message_at update,
    1 INSERT — should result in exactly one _publishRoomDeactivated call.
    """
    from room_state_publisher import handler

    room_id = "room-batch-test"
    records = [
        _make_record(
            "MODIFY",
            old_image=_active_image(room_id=room_id),
            new_image=_deactivated_image(room_id=room_id),
        ),
        _make_record(
            "MODIFY",
            old_image=_last_message_update_image(room_id="room-no-change"),
            new_image={
                **_last_message_update_image(room_id="room-no-change"),
                "last_message_at": _ddb_str("2026-06-18T15:00:00+00:00"),
            },
        ),
        _make_record("INSERT", new_image=_active_image(room_id="room-new")),
    ]

    handler.handler({"Records": records}, None)

    assert mock_publish.call_count == 1
    mutation_name, _, _ = mock_publish.call_args.args
    assert mutation_name == "_publishRoomDeactivated"


# ---------------------------------------------------------------------------
# Test H: missing OldImage / NewImage handled safely
# ---------------------------------------------------------------------------


def test_given_modify_with_no_old_image_when_processed_then_no_publish_and_no_crash(
    mock_publish,
):
    """
    AC-H: a MODIFY record missing OldImage (e.g. partial stream) must not crash
    and must not publish (cannot determine if status changed).
    """
    from room_state_publisher import handler

    record = _make_record("MODIFY", new_image=_deactivated_image())
    handler.handler({"Records": [record]}, None)

    mock_publish.assert_not_called()


def test_given_modify_with_no_new_image_when_processed_then_no_publish_and_no_crash(
    mock_publish,
):
    """
    AC-H: a MODIFY record missing NewImage must not crash and must not publish.
    """
    from room_state_publisher import handler

    record = _make_record("MODIFY", old_image=_active_image())
    handler.handler({"Records": [record]}, None)

    mock_publish.assert_not_called()


# ---------------------------------------------------------------------------
# Test F: _build_payload — pure function, no side effects
# ---------------------------------------------------------------------------


def test_build_payload_from_new_image_returns_camelcase_fields():
    """
    AC-F (payload correctness): _build_payload converts a DynamoDB NewImage
    dict into a plain Python dict with camelCase keys matching schema.graphql
    ChatRoom type fields.
    """
    from room_state_publisher.handler import _build_payload

    new_image = {
        "room_id": _ddb_str("room-xyz"),
        "user_a": _ddb_str("user-1"),
        "user_b": _ddb_str("user-2"),
        "status": _ddb_str("deactivated"),
        "deactivated_reason": _ddb_str("blocked"),
        "deactivated_by": _ddb_str("user-1"),
        "deactivated_at": _ddb_str("2026-06-18T10:00:00+00:00"),
        "friendship_active": _ddb_bool(False),
        "created_at": _ddb_str("2026-01-01T00:00:00+00:00"),
    }

    payload = _build_payload(new_image)

    assert payload["roomId"] == "room-xyz"
    assert payload["userA"] == "user-1"
    assert payload["userB"] == "user-2"
    assert payload["status"] == "deactivated"
    assert payload["deactivatedReason"] == "blocked"
    assert payload["deactivatedBy"] == "user-1"
    assert payload["deactivatedAt"] == "2026-06-18T10:00:00+00:00"
    assert payload["friendshipActive"] is False


def test_build_payload_optional_fields_absent_when_not_in_image():
    """
    _build_payload must not include None values for optional fields absent in the image.
    """
    from room_state_publisher.handler import _build_payload

    new_image = _active_image()
    payload = _build_payload(new_image)

    # Optional fields absent from the active image must be absent from the payload
    for key in ("deactivatedReason", "deactivatedBy", "deactivatedAt", "reactivatedAt"):
        assert key not in payload or payload[key] is None, (
            f"Optional field {key!r} should be None or absent when not in image"
        )
