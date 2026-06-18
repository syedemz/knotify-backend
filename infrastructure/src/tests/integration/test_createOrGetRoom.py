"""
Integration tests for Mutation.createOrGetRoom (story 8.3).

These tests run against the live dev AWS environment: a real Aurora cluster
(for friendships / blocks tables) and a real DynamoDB (for ChatRooms and
ChatRoomMembership tables). They do NOT invoke the deployed AppSync Lambda
directly — they drive the handler in-process with real external dependencies,
mirroring the deployed runtime.

Required environment variables (all missing → test is SKIPPED):
  AURORA_HOST                         — dev Aurora writer endpoint
  AURORA_PORT                         — normally 5432
  AURORA_DBNAME                       — normally "knotify"
  AURORA_MASTER_SECRET_ARN            — ARN of the Aurora master secret
  AWS_REGION                          — e.g. eu-central-1
  DYNAMODB_ENDPOINT_URL               — optional; set to a LocalStack/mock URL in
                                        local runs; omit to hit real AWS DynamoDB

Acceptance criteria covered (story 8.3):
  IT-8.3-1  A and B are friends, A calls createOrGetRoom(B) twice → same room_id,
            exactly one ChatRooms row, exactly two ChatRoomMembership rows.
  IT-8.3-2  A and C are not friends → returns Unauthorized with reason NOT_FRIENDS.
  IT-8.3-3  A has blocked B → returns Unauthorized with reason BLOCKED.
  IT-8.3-4  Caller without custom:profile_complete claim → returns Unauthorized
            with reason PROFILE_INCOMPLETE (decorator gate, tested via handler entry).

Run:
    pytest infrastructure/src/tests/integration/test_createOrGetRoom.py -v -m integration

Skip if live env is not configured:
    pytest -m "not integration"
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
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
# Aurora + DynamoDB helpers
# ---------------------------------------------------------------------------


def _aurora_conn():
    """Return a psycopg2 master connection to dev Aurora.

    Uses AURORA_MASTER_SECRET_ARN to fetch credentials, then connects
    to AURORA_HOST:AURORA_PORT/AURORA_DBNAME.
    """
    import boto3
    import psycopg2

    region = _require_env("AWS_REGION")
    secret_arn = _require_env("AURORA_MASTER_SECRET_ARN")
    host = _require_env("AURORA_HOST")
    port = int(_require_env("AURORA_PORT"))
    dbname = _require_env("AURORA_DBNAME")

    sm = boto3.client("secretsmanager", region_name=region)
    secret_value = sm.get_secret_value(SecretId=secret_arn)
    creds = json.loads(secret_value["SecretString"])

    return psycopg2.connect(
        host=host,
        port=port,
        dbname=dbname,
        user=creds["username"],
        password=creds["password"],
    )


def _dynamodb_client():
    """Return a boto3 DynamoDB client, optionally pointed at a local mock."""
    import boto3

    region = _require_env("AWS_REGION")
    endpoint_url = os.environ.get("DYNAMODB_ENDPOINT_URL")
    kwargs: Dict[str, Any] = {"region_name": region}
    if endpoint_url:
        kwargs["endpoint_url"] = endpoint_url
    return boto3.client("dynamodb", **kwargs)


def _seed_friendship(conn, user_a_id: str, user_b_id: str) -> None:
    """Insert a canonical friendship row (user_a < user_b).

    Uses the master connection so RLS does not interfere.
    """
    lo = min(user_a_id, user_b_id)
    hi = max(user_a_id, user_b_id)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO friendships (user_a, user_b) VALUES (%s, %s) "
            "ON CONFLICT DO NOTHING",
            (lo, hi),
        )
    conn.commit()


def _seed_block(conn, blocker_id: str, blocked_id: str) -> None:
    """Insert a block row."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO blocks (blocker_id, blocked_id) VALUES (%s, %s) "
            "ON CONFLICT DO NOTHING",
            (blocker_id, blocked_id),
        )
    conn.commit()


def _seed_user(conn, user_id: str, email: str) -> None:
    """Insert a minimal user row to satisfy FK constraints."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO users (user_id, email, username, sex, age,
                               current_residence_country, resident_country_code,
                               marital_status, religion)
            VALUES (%s, %s, %s, 'Male', 25, 'United Kingdom', 'GB', 'single', 'Islam')
            ON CONFLICT (user_id) DO NOTHING
            """,
            (user_id, email, f"ituser_{user_id[:8]}"),
        )
    conn.commit()


def _delete_friendship(conn, user_a_id: str, user_b_id: str) -> None:
    lo, hi = min(user_a_id, user_b_id), max(user_a_id, user_b_id)
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM friendships WHERE user_a = %s AND user_b = %s",
            (lo, hi),
        )
    conn.commit()


def _delete_block(conn, blocker_id: str, blocked_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM blocks WHERE blocker_id = %s AND blocked_id = %s",
            (blocker_id, blocked_id),
        )
    conn.commit()


def _delete_user(conn, user_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM users WHERE user_id = %s", (user_id,))
    conn.commit()


def _delete_chat_room(ddb, room_id: str) -> None:
    """Remove ChatRooms and ChatRoomMembership rows seeded by a test."""
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


def _compute_room_id(user_a: str, user_b: str) -> str:
    lo, hi = min(user_a, user_b), max(user_a, user_b)
    return hashlib.sha256(f"{lo}:{hi}".encode()).hexdigest()


# ---------------------------------------------------------------------------
# Handler loader (in-process, uses real layers)
# ---------------------------------------------------------------------------


def _load_handler_module():
    """Import the chat_resolver handler with real knotify_obs and knotify_db.

    Requires the layers to be installed or on sys.path.  The Aurora and
    DynamoDB clients are injected via module-level singletons after import.
    """
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
        "chat_resolver_handler_it_" + str(uuid.uuid4().hex),
        os.path.join(handler_dir, "handler.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_event(
    caller_id: str,
    other_user_id: str,
    profile_complete: str = "true",
) -> dict:
    return {
        "typeName": "Mutation",
        "fieldName": "createOrGetRoom",
        "identity": {
            "sub": caller_id,
            "claims": {
                "sub": caller_id,
                "custom:profile_complete": profile_complete,
            },
        },
        "arguments": {"otherUserId": other_user_id},
        "source": None,
        "request": {"headers": {}},
    }


# ---------------------------------------------------------------------------
# IT-8.3-1: A and B are friends → same room_id, 1 ChatRooms row, 2 membership rows
# ---------------------------------------------------------------------------


def test_it_8_3_1_friends_create_room_idempotent() -> None:
    """A and B are friends; A calls createOrGetRoom(B) twice → same room_id,
    exactly one ChatRooms row, exactly two ChatRoomMembership rows."""
    _require_env("AURORA_HOST")
    _require_env("AURORA_MASTER_SECRET_ARN")
    _require_env("AWS_REGION")

    user_a_id = str(uuid.uuid4())
    user_b_id = str(uuid.uuid4())
    expected_room_id = _compute_room_id(user_a_id, user_b_id)

    aurora = _aurora_conn()
    ddb = _dynamodb_client()

    try:
        # Seed users + friendship
        _seed_user(aurora, user_a_id, f"it_8_3_1_a_{user_a_id[:8]}@test.invalid")
        _seed_user(aurora, user_b_id, f"it_8_3_1_b_{user_b_id[:8]}@test.invalid")
        _seed_friendship(aurora, user_a_id, user_b_id)

        mod = _load_handler_module()
        # Inject real DDB client
        mod._dynamodb_client = ddb
        # Reset Aurora connection to pick up fresh credentials
        mod._conn = None
        mod._DB_SECRET_NAME = os.environ.get("DB_APP_SECRET_NAME", "")

        event = _make_event(user_a_id, user_b_id)

        # First call — creates the room
        result1 = mod._dispatch(event)
        assert "errorType" not in result1, f"First call failed: {result1}"
        assert result1["room_id"] == expected_room_id

        # Second call — room already exists; idempotent
        result2 = mod._dispatch(event)
        assert "errorType" not in result2, f"Second call failed: {result2}"
        assert result2["room_id"] == expected_room_id

        # Exactly one ChatRooms row
        get_room = ddb.get_item(
            TableName="ChatRooms",
            Key={"room_id": {"S": expected_room_id}},
        )
        assert "Item" in get_room, "ChatRooms row not found"

        # Exactly two ChatRoomMembership rows (one per participant)
        for uid in (user_a_id, user_b_id):
            membership = ddb.get_item(
                TableName="ChatRoomMembership",
                Key={"user_id": {"S": uid}, "room_id": {"S": expected_room_id}},
            )
            assert "Item" in membership, (
                f"ChatRoomMembership row for user {uid} not found"
            )

    finally:
        # Cleanup DynamoDB
        _delete_chat_room(ddb, expected_room_id)
        _delete_membership(ddb, user_a_id, expected_room_id)
        _delete_membership(ddb, user_b_id, expected_room_id)
        # Cleanup Aurora
        _delete_friendship(aurora, user_a_id, user_b_id)
        _delete_user(aurora, user_a_id)
        _delete_user(aurora, user_b_id)
        aurora.close()


# ---------------------------------------------------------------------------
# IT-8.3-2: A and C are not friends → NOT_FRIENDS
# ---------------------------------------------------------------------------


def test_it_8_3_2_not_friends_returns_not_friends_unauthorized() -> None:
    """A and C are not friends; createOrGetRoom returns Unauthorized NOT_FRIENDS."""
    _require_env("AURORA_HOST")
    _require_env("AURORA_MASTER_SECRET_ARN")
    _require_env("AWS_REGION")

    user_a_id = str(uuid.uuid4())
    user_c_id = str(uuid.uuid4())

    aurora = _aurora_conn()

    try:
        _seed_user(aurora, user_a_id, f"it_8_3_2_a_{user_a_id[:8]}@test.invalid")
        _seed_user(aurora, user_c_id, f"it_8_3_2_c_{user_c_id[:8]}@test.invalid")
        # No friendship inserted

        mod = _load_handler_module()
        mod._dynamodb_client = _dynamodb_client()
        mod._conn = None
        mod._DB_SECRET_NAME = os.environ.get("DB_APP_SECRET_NAME", "")

        event = _make_event(user_a_id, user_c_id)
        result = mod._dispatch(event)

        assert result["errorType"] == "Unauthorized", f"Expected Unauthorized, got: {result}"
        assert result["reason"] == "NOT_FRIENDS", f"Expected NOT_FRIENDS, got reason: {result.get('reason')}"

    finally:
        _delete_user(aurora, user_a_id)
        _delete_user(aurora, user_c_id)
        aurora.close()


# ---------------------------------------------------------------------------
# IT-8.3-3: A has blocked B → BLOCKED
# ---------------------------------------------------------------------------


def test_it_8_3_3_blocked_returns_blocked_unauthorized() -> None:
    """A has blocked B; createOrGetRoom returns Unauthorized BLOCKED."""
    _require_env("AURORA_HOST")
    _require_env("AURORA_MASTER_SECRET_ARN")
    _require_env("AWS_REGION")

    user_a_id = str(uuid.uuid4())
    user_b_id = str(uuid.uuid4())

    aurora = _aurora_conn()

    try:
        _seed_user(aurora, user_a_id, f"it_8_3_3_a_{user_a_id[:8]}@test.invalid")
        _seed_user(aurora, user_b_id, f"it_8_3_3_b_{user_b_id[:8]}@test.invalid")
        # Friendship exists but A has blocked B
        _seed_friendship(aurora, user_a_id, user_b_id)
        _seed_block(aurora, user_a_id, user_b_id)

        mod = _load_handler_module()
        mod._dynamodb_client = _dynamodb_client()
        mod._conn = None
        mod._DB_SECRET_NAME = os.environ.get("DB_APP_SECRET_NAME", "")

        event = _make_event(user_a_id, user_b_id)
        result = mod._dispatch(event)

        assert result["errorType"] == "Unauthorized", f"Expected Unauthorized, got: {result}"
        assert result["reason"] == "BLOCKED", f"Expected BLOCKED, got reason: {result.get('reason')}"

    finally:
        _delete_block(aurora, user_a_id, user_b_id)
        _delete_friendship(aurora, user_a_id, user_b_id)
        _delete_user(aurora, user_a_id)
        _delete_user(aurora, user_b_id)
        aurora.close()


# ---------------------------------------------------------------------------
# IT-8.3-4: Caller without profile_complete → PROFILE_INCOMPLETE (decorator gate)
# ---------------------------------------------------------------------------


def test_it_8_3_4_profile_incomplete_returns_unauthorized() -> None:
    """Caller without custom:profile_complete='true' → PROFILE_INCOMPLETE Unauthorized.

    This tests the @require_profile_complete_appsync decorator via the full
    handler() entrypoint, not _dispatch().  No database connections required.
    """
    # No env vars required — this is a decorator gate test, no real deps needed.
    mod = _load_handler_module()

    # Inject no-op DDB and a closed connection so the decorator runs first.
    from unittest.mock import MagicMock
    mod._dynamodb_client = MagicMock()
    mod._conn = MagicMock()

    # Event with no custom:profile_complete claim (claim absent)
    event_no_claim = {
        "typeName": "Mutation",
        "fieldName": "createOrGetRoom",
        "identity": {
            "sub": "some-user-id",
            "claims": {
                "sub": "some-user-id",
                # custom:profile_complete intentionally absent
            },
        },
        "arguments": {"otherUserId": "other-user-id"},
        "source": None,
        "request": {"headers": {}},
    }
    result = mod.handler(event_no_claim, {})
    assert result["errorType"] == "Unauthorized", (
        f"Expected Unauthorized for absent claim, got: {result}"
    )
    assert result.get("reason") == "PROFILE_INCOMPLETE", (
        f"Expected PROFILE_INCOMPLETE reason, got: {result.get('reason')}"
    )

    # Event with profile_complete = "false"
    event_false_claim = dict(event_no_claim)
    event_false_claim["identity"] = {
        "sub": "some-user-id",
        "claims": {
            "sub": "some-user-id",
            "custom:profile_complete": "false",
        },
    }
    result2 = mod.handler(event_false_claim, {})
    assert result2["errorType"] == "Unauthorized"
    assert result2.get("reason") == "PROFILE_INCOMPLETE"
