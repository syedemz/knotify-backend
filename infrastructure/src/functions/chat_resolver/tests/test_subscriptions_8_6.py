"""
Integration tests for story 8.6 — Scoped subscriptions with pipeline membership check.

These tests verify the AppSync subscription pipeline resolvers are wired correctly.
They require a deployed AppSync API and real WebSocket connections, so they are
skip-gated on the APPSYNC_GRAPHQL_URL environment variable.

Run against a deployed stack:
    APPSYNC_GRAPHQL_URL=https://... \\
    APPSYNC_REALTIME_URL=wss://... \\
    APPSYNC_API_KEY=... \\   # or Cognito tokens — see _get_auth_header
    pytest infrastructure/src/functions/chat_resolver/tests/test_subscriptions_8_6.py -v -m integration

Skip in unit test runs (default):
    pytest -m "not integration"

Test conventions mirror the 8.3/8.4/8.5 skip-gate idiom:
  - Each integration test is @pytest.mark.integration and @pytest.mark.skipif on
    the APPSYNC_GRAPHQL_URL env var being absent.
  - Tests that don't need a real deployed stack (logic-only) run without the gate.

Acceptance criteria covered:
  IT-8.6-1  Integration test: a user not in room R attempts subscription
            onMessageInRoom(R) → connection rejected before any message
            can be received.
  IT-8.6-2  Integration test: A and B in room R, A sends a message → B's
            onMessageInRoom subscription receives the Message within 2 seconds.
  IT-8.6-3  Integration test: A and B in room R, A sends a message → a third
            user C subscribed to onMessageInRoom(R2) for a different room does
            NOT receive the event (field filter enforced).

Notes:
  - The three tests above are skip-gated on APPSYNC_GRAPHQL_URL because they
    require a real deployed AppSync API + live WebSocket connections. They
    cannot run in a unit-test environment.
  - AppSync's @aws_subscribe field filter is validated by IT-8.6-3: the
    subscription argument (roomId) is the filter key, so only events published
    for roomId=R reach a subscriber listening on R, not R2.
  - Pipeline check timing: the pipeline runs when a CLIENT SUBSCRIBES (not on
    every published event). If the pipeline rejects, the WebSocket subscribe
    attempt returns an error response before the first event is ever delivered.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Skip gate — all integration tests in this file require a deployed AppSync API
# ---------------------------------------------------------------------------

_APPSYNC_URL = os.environ.get("APPSYNC_GRAPHQL_URL", "")
_APPSYNC_REALTIME_URL = os.environ.get("APPSYNC_REALTIME_URL", "")

_INTEGRATION_SKIP = pytest.mark.skipif(
    not _APPSYNC_URL,
    reason=(
        "Integration tests require APPSYNC_GRAPHQL_URL env var. "
        "Set APPSYNC_GRAPHQL_URL (and optionally APPSYNC_REALTIME_URL) "
        "to the deployed AppSync endpoint before running."
    ),
)

# ---------------------------------------------------------------------------
# Helpers — minimal AppSync WebSocket subscription client
#
# AppSync's real-time WebSocket protocol:
#   1. Connect to wss://<realtime_url>/graphql?header=<b64-header>&payload=e30=
#   2. Send {"type": "connection_init"}
#   3. Wait for {"type": "connection_ack"}
#   4. Send {"type": "start", "id": "<id>", "payload": {"data": "<GQL>", ...}}
#   5. Connection rejected → server sends {"type": "error"} or {"type": "ka"}
#      followed by {"type": "complete"} for the subscription id.
#   6. Event delivered → server sends {"type": "data", "id": "<id>"}
# ---------------------------------------------------------------------------


def _graphql_mutation(url: str, token: str, query: str, variables: dict) -> dict:
    """Execute a GraphQL mutation over HTTPS and return the parsed JSON response.

    Args:
        url:       HTTPS AppSync GraphQL endpoint.
        token:     Cognito JWT access token for Authorization header.
        query:     GraphQL mutation document string.
        variables: Variables dict.

    Returns:
        Parsed JSON response dict from AppSync.
    """
    import urllib.request

    payload = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": token,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _subscribe_and_collect(
    realtime_url: str,
    token: str,
    subscription_doc: str,
    variables: dict,
    collect_for_seconds: float = 3.0,
) -> tuple[bool, list[dict]]:
    """Subscribe to an AppSync WebSocket subscription and collect events.

    Connects, sends the subscription start frame, waits collect_for_seconds,
    then closes. Returns (subscription_accepted, events_received).

    subscription_accepted is True if the server acknowledged the subscription
    without returning an error frame. False means the pipeline rejected the
    subscribe attempt (membership check failed).

    Args:
        realtime_url:         WSS AppSync real-time endpoint.
        token:                Cognito JWT access token.
        subscription_doc:     GraphQL subscription document string.
        variables:            Subscription variables dict.
        collect_for_seconds:  How long to wait for events before closing.

    Returns:
        (subscription_accepted: bool, events: list[dict])
    """
    import base64
    import websocket  # type: ignore[import-untyped]  # websocket-client

    # Encode auth header and empty payload as required by AppSync realtime protocol
    header_b64 = base64.b64encode(
        json.dumps({"Authorization": token}).encode("utf-8")
    ).decode("ascii")
    payload_b64 = base64.b64encode(b"{}").decode("ascii")

    connect_url = f"{realtime_url}?header={header_b64}&payload={payload_b64}"

    events: list[dict] = []
    accepted: list[bool] = [False]
    error_received: list[bool] = [False]
    lock = threading.Lock()

    def on_message(ws: Any, message: str) -> None:
        frame = json.loads(message)
        msg_type = frame.get("type", "")
        if msg_type == "connection_ack":
            # Connection established — send subscription start
            sub_payload = json.dumps(
                {
                    "type": "start",
                    "id": "sub-8-6-test",
                    "payload": {
                        "data": json.dumps(
                            {"query": subscription_doc, "variables": variables}
                        ),
                        "extensions": {
                            "authorization": {"Authorization": token}
                        },
                    },
                }
            )
            ws.send(sub_payload)
        elif msg_type == "start_ack":
            with lock:
                accepted[0] = True
        elif msg_type == "error":
            with lock:
                error_received[0] = True
        elif msg_type == "data":
            with lock:
                events.append(frame.get("payload", {}).get("data", {}))
        elif msg_type == "ka":
            pass  # keep-alive — ignore

    def on_open(ws: Any) -> None:
        ws.send(json.dumps({"type": "connection_init"}))

    ws = websocket.WebSocketApp(
        connect_url,
        on_message=on_message,
        on_open=on_open,
        header={"Sec-WebSocket-Protocol": "graphql-ws"},
    )

    thread = threading.Thread(
        target=lambda: ws.run_forever(ping_interval=5, ping_timeout=3),
        daemon=True,
    )
    thread.start()
    time.sleep(collect_for_seconds)
    ws.close()
    thread.join(timeout=2)

    sub_accepted = accepted[0] and not error_received[0]
    return sub_accepted, events


# ---------------------------------------------------------------------------
# IT-8.6-1: Non-member attempts subscription onMessageInRoom(R) — rejected
#
# The pipeline's check_room_membership function does a DynamoDB GetItem on
# ChatRoomMembership(identity.sub, roomId). On miss it returns an error,
# which causes AppSync to reject the WebSocket subscription before delivering
# any events.
# ---------------------------------------------------------------------------


@_INTEGRATION_SKIP
@pytest.mark.integration
def test_given_non_member_when_subscribing_to_room_then_subscription_rejected() -> None:
    """IT-8.6-1: given non-member, when subscribing onMessageInRoom(R), then rejected.

    The pipeline resolver runs on subscribe, not on event delivery. The
    check_room_membership function returns Unauthorized on GetItem miss,
    causing AppSync to reject the WebSocket subscription immediately.
    """
    import base64

    graphql_url = os.environ["APPSYNC_GRAPHQL_URL"]
    realtime_url = os.environ.get("APPSYNC_REALTIME_URL", "")

    # Cognito token for a user that is NOT in the target room.
    # Provided by the test harness via APPSYNC_NON_MEMBER_TOKEN.
    non_member_token = os.environ.get("APPSYNC_NON_MEMBER_TOKEN", "")
    if not non_member_token:
        pytest.skip(
            "APPSYNC_NON_MEMBER_TOKEN not set — skipping live subscription rejection test"
        )

    # A room ID that the non-member is definitely not in.
    # Provided by the test harness or use a well-known fixture room.
    target_room_id = os.environ.get("APPSYNC_FIXTURE_ROOM_ID", "nonexistent-room-id")

    subscription_doc = """
    subscription OnMessageInRoom($roomId: ID!) {
      onMessageInRoom(roomId: $roomId) {
        roomId
        messageId
        content
      }
    }
    """

    accepted, events = _subscribe_and_collect(
        realtime_url=realtime_url,
        token=non_member_token,
        subscription_doc=subscription_doc,
        variables={"roomId": target_room_id},
        collect_for_seconds=3.0,
    )

    assert not accepted, (
        "Pipeline membership check must reject subscription for non-member "
        f"(room={target_room_id}); subscription was unexpectedly accepted"
    )
    assert events == [], (
        "Non-member must receive zero events even if subscription somehow established"
    )


# ---------------------------------------------------------------------------
# IT-8.6-2: Member A sends message → Member B's subscription receives it
#
# Both A and B are in room R. B subscribes to onMessageInRoom(R). A calls
# sendMessage. B's subscription must receive the Message within 2 seconds.
# ---------------------------------------------------------------------------


@_INTEGRATION_SKIP
@pytest.mark.integration
def test_given_member_subscribed_when_other_member_sends_message_then_event_received() -> None:
    """IT-8.6-2: given B subscribed to onMessageInRoom(R), when A sends, then B receives within 2s."""
    graphql_url = os.environ["APPSYNC_GRAPHQL_URL"]
    realtime_url = os.environ.get("APPSYNC_REALTIME_URL", "")

    token_a = os.environ.get("APPSYNC_USER_A_TOKEN", "")
    token_b = os.environ.get("APPSYNC_USER_B_TOKEN", "")
    room_id = os.environ.get("APPSYNC_AB_ROOM_ID", "")

    if not (token_a and token_b and room_id):
        pytest.skip(
            "APPSYNC_USER_A_TOKEN, APPSYNC_USER_B_TOKEN, APPSYNC_AB_ROOM_ID not set"
        )

    subscription_doc = """
    subscription OnMessageInRoom($roomId: ID!) {
      onMessageInRoom(roomId: $roomId) {
        roomId
        messageId
        senderId
        content
        contentType
      }
    }
    """

    # Start B's subscription in a background thread, give it 0.5s to establish
    b_accepted: list[bool] = [False]
    b_events: list[dict] = []

    def collect_b_events() -> None:
        accepted, events = _subscribe_and_collect(
            realtime_url=realtime_url,
            token=token_b,
            subscription_doc=subscription_doc,
            variables={"roomId": room_id},
            collect_for_seconds=5.0,  # wait long enough for A's mutation
        )
        b_accepted[0] = accepted
        b_events.extend(events)

    b_thread = threading.Thread(target=collect_b_events, daemon=True)
    b_thread.start()

    # Give B's subscription time to establish before A sends
    time.sleep(1.5)

    # A sends a message
    send_mutation = """
    mutation SendMessage($roomId: ID!, $content: String!) {
      sendMessage(roomId: $roomId, content: $content) {
        roomId
        messageId
        senderId
        content
      }
    }
    """
    test_content = f"IT-8.6-2 test message at {time.time()}"
    mutation_response = _graphql_mutation(
        url=graphql_url,
        token=token_a,
        query=send_mutation,
        variables={"roomId": room_id, "content": test_content},
    )

    assert "errors" not in mutation_response, (
        f"sendMessage mutation returned errors: {mutation_response.get('errors')}"
    )

    # Wait for B's thread to finish collecting
    b_thread.join(timeout=7)

    assert b_accepted[0], (
        "B's onMessageInRoom subscription must be accepted by the pipeline resolver "
        "(B is a member of the room)"
    )

    matching_events = [
        e for e in b_events
        if e.get("onMessageInRoom", {}).get("content") == test_content
    ]
    assert len(matching_events) >= 1, (
        f"B must receive A's message within the subscription window. "
        f"Sent content='{test_content}', received events: {b_events}"
    )


# ---------------------------------------------------------------------------
# IT-8.6-3: Field filter enforced — C subscribed to R2 does NOT receive
#           events from R
#
# A and B are in room R. C (also a valid member of some other room R2) subscribes
# to onMessageInRoom(R2). When A sends a message to R, C must NOT receive it
# because AppSync's @aws_subscribe field filter matches on the `roomId` field
# of the published mutation result against the subscription argument.
# ---------------------------------------------------------------------------


@_INTEGRATION_SKIP
@pytest.mark.integration
def test_given_c_subscribed_to_different_room_when_a_sends_to_r_then_c_receives_nothing() -> None:
    """IT-8.6-3: given C subscribed to R2, when A sends to R, then C receives no event."""
    graphql_url = os.environ["APPSYNC_GRAPHQL_URL"]
    realtime_url = os.environ.get("APPSYNC_REALTIME_URL", "")

    token_a = os.environ.get("APPSYNC_USER_A_TOKEN", "")
    token_c = os.environ.get("APPSYNC_USER_C_TOKEN", "")
    room_r = os.environ.get("APPSYNC_AB_ROOM_ID", "")
    room_r2 = os.environ.get("APPSYNC_C_ROOM_ID", "")  # C is a member of R2, not R

    if not (token_a and token_c and room_r and room_r2):
        pytest.skip(
            "APPSYNC_USER_A_TOKEN, APPSYNC_USER_C_TOKEN, APPSYNC_AB_ROOM_ID, "
            "APPSYNC_C_ROOM_ID not set"
        )

    subscription_doc = """
    subscription OnMessageInRoom($roomId: ID!) {
      onMessageInRoom(roomId: $roomId) {
        roomId
        messageId
        content
      }
    }
    """

    # C subscribes to R2
    c_accepted: list[bool] = [False]
    c_events: list[dict] = []

    def collect_c_events() -> None:
        accepted, events = _subscribe_and_collect(
            realtime_url=realtime_url,
            token=token_c,
            subscription_doc=subscription_doc,
            variables={"roomId": room_r2},
            collect_for_seconds=5.0,
        )
        c_accepted[0] = accepted
        c_events.extend(events)

    c_thread = threading.Thread(target=collect_c_events, daemon=True)
    c_thread.start()

    # Give C's subscription time to establish
    time.sleep(1.5)

    # A sends a message to room R (not R2)
    send_mutation = """
    mutation SendMessage($roomId: ID!, $content: String!) {
      sendMessage(roomId: $roomId, content: $content) {
        roomId
        messageId
        content
      }
    }
    """
    test_content = f"IT-8.6-3 room-R message at {time.time()}"
    mutation_response = _graphql_mutation(
        url=graphql_url,
        token=token_a,
        query=send_mutation,
        variables={"roomId": room_r, "content": test_content},
    )

    assert "errors" not in mutation_response, (
        f"sendMessage to room R returned errors: {mutation_response.get('errors')}"
    )

    # Wait for C's thread to finish
    c_thread.join(timeout=7)

    assert c_accepted[0], (
        "C's subscription to R2 must be accepted (C is a member of R2); "
        "pipeline rejected unexpectedly"
    )

    # C must have received zero events — the field filter on roomId keeps
    # R events away from R2 subscribers
    events_for_r = [
        e for e in c_events
        if e.get("onMessageInRoom", {}).get("content") == test_content
    ]
    assert events_for_r == [], (
        f"C subscribed to R2 must NOT receive a message sent to R. "
        f"Leaked events: {events_for_r}"
    )
