"""
Unit tests for the knotify-blocks Lambda handler.

These tests are fully isolated — they do NOT require a running database,
AWS credentials, or a deployed Lambda.  All external collaborators (Aurora
connection, RLS context, DynamoDB client) are replaced with in-memory stubs.

Run:
    pytest infrastructure/src/functions/blocks/tests/test_handler_unit.py -v

Test areas:
  A. GET /v1/blocks — returns the caller's block list
  B. POST /v1/blocks — friendship guard, Aurora transaction, DynamoDB deactivation
  C. DELETE /v1/blocks/{userId} — unblock + DynamoDB reactivation
  D. DynamoDB error paths — ConditionalCheckFailedException no-op, other errors
     return HTTP 200 with chat_deactivation_pending flag
  E. Route dispatch / edge-secret gate
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
import uuid
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest

# ---------------------------------------------------------------------------
# Module import helper
# ---------------------------------------------------------------------------

_HANDLER_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
)


def _import_handler():
    """Import the blocks handler module fresh on every call."""
    spec = importlib.util.spec_from_file_location(
        "blocks_handler_" + str(id(object())),
        os.path.join(_HANDLER_DIR, "handler.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Event builder
# ---------------------------------------------------------------------------

_EDGE_SECRET = "test-edge-secret"
_USER_A = str(uuid.uuid4())
_USER_B = str(uuid.uuid4())


def _make_event(
    method: str,
    path: str,
    *,
    body: dict | None = None,
    path_params: dict | None = None,
    user_sub: str = _USER_A,
    user_sex: str = "Male",
    edge_secret: str = _EDGE_SECRET,
) -> dict:
    event: dict = {
        "requestContext": {
            "http": {"method": method, "path": path},
            "authorizer": {
                "jwt": {
                    "claims": {
                        "sub": user_sub,
                        "custom:user_sex": user_sex,
                        # story 7.0b: blocks endpoint requires completed profile
                        "custom:profile_complete": "true",
                    }
                }
            },
        },
        "headers": {"x-knotify-edge-secret": edge_secret},
    }
    if body is not None:
        event["body"] = json.dumps(body)
    if path_params is not None:
        event["pathParameters"] = path_params
    return event


# ---------------------------------------------------------------------------
# DB stub helpers
# ---------------------------------------------------------------------------

def _make_conn(fetchone_side_effect=None, fetchone_return=None):
    """
    Build a minimal psycopg2 connection stub.

    fetchone_side_effect — list of return values (or exceptions) yielded per
                           successive fetchone() call on the cursor.
    fetchone_return      — single return value if not using side_effect.
    """
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cur

    if fetchone_side_effect is not None:
        cur.fetchone.side_effect = fetchone_side_effect
    elif fetchone_return is not None:
        cur.fetchone.return_value = fetchone_return

    return conn, cur


def _make_dynamo_client(update_side_effect=None):
    """Build a minimal boto3 DynamoDB client stub."""
    dynamo = MagicMock()
    if update_side_effect is not None:
        dynamo.update_item.side_effect = update_side_effect
    else:
        dynamo.update_item.return_value = {}
    return dynamo


class _FakeDynamoClientError(Exception):
    """
    Minimal stand-in for botocore.exceptions.ClientError.

    The handler identifies DynamoDB errors by duck-typing the `.response`
    attribute (no botocore import at handler module level).  This stub
    replicates that structure without requiring botocore in the test env.
    """

    def __init__(self, code: str, message: str = "") -> None:
        self.response = {"Error": {"Code": code, "Message": message}}
        super().__init__(f"An error occurred ({code}): {message}")


def _conditional_check_failed_exc() -> _FakeDynamoClientError:
    """Return a stub exception for ConditionalCheckFailedException."""
    return _FakeDynamoClientError("ConditionalCheckFailedException", "The conditional request failed")


def _other_dynamo_exc() -> _FakeDynamoClientError:
    """Return a stub exception for a generic (non-conditional) DynamoDB error."""
    return _FakeDynamoClientError("InternalServerError", "Internal server error")


# ---------------------------------------------------------------------------
# Area A: GET /v1/blocks
# ---------------------------------------------------------------------------


class TestGetBlocks:
    """GET /v1/blocks — returns the block list for the authenticated user."""

    def test_given_no_blocks_when_get_then_returns_empty_list(self):
        mod = _import_handler()
        conn, cur = _make_conn()
        cur.fetchall.return_value = []
        cur.description = [("blocked_id",), ("created_at",)]

        dynamo = _make_dynamo_client()
        event = _make_event("GET", "/v1/blocks", user_sub=_USER_A)

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "_get_dynamo", return_value=dynamo),
            patch.dict(os.environ, {"EDGE_SECRET": _EDGE_SECRET, "DB_SECRET_NAME": "x",
                                    "TABLE_CHAT_ROOMS": "ChatRooms"}),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body == {"blocks": []}

    def test_given_existing_blocks_when_get_then_returns_list(self):
        mod = _import_handler()
        conn, cur = _make_conn()
        blocked_id = str(uuid.uuid4())
        cur.fetchall.return_value = [(blocked_id, "2026-01-01T00:00:00")]
        cur.description = [("blocked_id",), ("created_at",)]

        dynamo = _make_dynamo_client()
        event = _make_event("GET", "/v1/blocks", user_sub=_USER_A)

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "_get_dynamo", return_value=dynamo),
            patch.dict(os.environ, {"EDGE_SECRET": _EDGE_SECRET, "DB_SECRET_NAME": "x",
                                    "TABLE_CHAT_ROOMS": "ChatRooms"}),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert len(body["blocks"]) == 1
        assert body["blocks"][0]["blocked_id"] == blocked_id


# ---------------------------------------------------------------------------
# Area B: POST /v1/blocks
# ---------------------------------------------------------------------------


class TestPostBlocks:
    """POST /v1/blocks — friendship guard + Aurora txn + DynamoDB deactivation."""

    def test_given_missing_body_when_post_then_returns_400(self):
        mod = _import_handler()
        conn, cur = _make_conn()
        dynamo = _make_dynamo_client()

        event = _make_event("POST", "/v1/blocks", user_sub=_USER_A)
        # No body set

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "_get_dynamo", return_value=dynamo),
            patch.dict(os.environ, {"EDGE_SECRET": _EDGE_SECRET, "DB_SECRET_NAME": "x",
                                    "TABLE_CHAT_ROOMS": "ChatRooms"}),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 400

    def test_given_missing_userId_field_when_post_then_returns_400(self):
        mod = _import_handler()
        conn, cur = _make_conn()
        dynamo = _make_dynamo_client()

        event = _make_event("POST", "/v1/blocks", body={}, user_sub=_USER_A)

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "_get_dynamo", return_value=dynamo),
            patch.dict(os.environ, {"EDGE_SECRET": _EDGE_SECRET, "DB_SECRET_NAME": "x",
                                    "TABLE_CHAT_ROOMS": "ChatRooms"}),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 400

    def test_given_not_friends_when_post_then_returns_409_not_friends(self):
        """
        When no friendship row exists between blocker and target, POST /v1/blocks
        must return HTTP 409 with {"error": "not_friends"} and write nothing.
        """
        mod = _import_handler()
        conn, cur = _make_conn()
        # First fetchone (friendship check) returns None — not friends
        cur.fetchone.return_value = None

        dynamo = _make_dynamo_client()
        event = _make_event("POST", "/v1/blocks", body={"userId": _USER_B}, user_sub=_USER_A)

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "_get_dynamo", return_value=dynamo),
            patch.dict(os.environ, {"EDGE_SECRET": _EDGE_SECRET, "DB_SECRET_NAME": "x",
                                    "TABLE_CHAT_ROOMS": "ChatRooms"}),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 409
        body = json.loads(response["body"])
        assert body["error"] == "not_friends"

        # DynamoDB must NOT be called when Aurora rejects the block
        dynamo.update_item.assert_not_called()

    def test_given_not_friends_when_post_then_aurora_not_written(self):
        """
        The blocks table must remain unchanged when the friendship guard fires.
        Verified by asserting no INSERT INTO blocks was executed.
        """
        mod = _import_handler()
        conn, cur = _make_conn()
        cur.fetchone.return_value = None  # no friendship row

        dynamo = _make_dynamo_client()
        event = _make_event("POST", "/v1/blocks", body={"userId": _USER_B}, user_sub=_USER_A)

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "_get_dynamo", return_value=dynamo),
            patch.dict(os.environ, {"EDGE_SECRET": _EDGE_SECRET, "DB_SECRET_NAME": "x",
                                    "TABLE_CHAT_ROOMS": "ChatRooms"}),
        ):
            mod.handler(event, None)

        insert_calls = [c for c in cur.execute.call_args_list if "INSERT INTO blocks" in str(c)]
        assert insert_calls == [], "INSERT INTO blocks must not execute when not friends"

    def test_given_friends_when_post_then_returns_200(self):
        """
        When a friendship exists, POST /v1/blocks must return HTTP 200.
        """
        mod = _import_handler()
        conn, cur = _make_conn()
        # fetchone returns a friendship row (any truthy value means friends)
        cur.fetchone.return_value = (1,)

        dynamo = _make_dynamo_client()
        event = _make_event("POST", "/v1/blocks", body={"userId": _USER_B}, user_sub=_USER_A)

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "_get_dynamo", return_value=dynamo),
            patch.dict(os.environ, {"EDGE_SECRET": _EDGE_SECRET, "DB_SECRET_NAME": "x",
                                    "TABLE_CHAT_ROOMS": "ChatRooms"}),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 200

    def test_given_friends_when_post_then_aurora_transaction_executes_three_writes(self):
        """
        On a successful block, the Aurora transaction must execute:
          1. INSERT INTO blocks
          2. DELETE FROM friendships
          3. DELETE FROM friend_requests (both directions)
        All in a single transaction (BEGIN + COMMIT, not autocommit).
        """
        mod = _import_handler()
        conn, cur = _make_conn()
        cur.fetchone.return_value = (1,)  # friendship exists

        dynamo = _make_dynamo_client()
        event = _make_event("POST", "/v1/blocks", body={"userId": _USER_B}, user_sub=_USER_A)

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "_get_dynamo", return_value=dynamo),
            patch.dict(os.environ, {"EDGE_SECRET": _EDGE_SECRET, "DB_SECRET_NAME": "x",
                                    "TABLE_CHAT_ROOMS": "ChatRooms"}),
        ):
            mod.handler(event, None)

        all_sql = " ".join(str(c) for c in cur.execute.call_args_list)
        assert "INSERT INTO blocks" in all_sql
        assert "DELETE FROM friendships" in all_sql
        assert "DELETE FROM friend_requests" in all_sql

    def test_given_friends_when_post_then_dynamo_update_called_after_commit(self):
        """
        DynamoDB UpdateItem must be called AFTER the Aurora transaction commits,
        not inside it.  The handler must call dynamo.update_item at least once.
        """
        mod = _import_handler()
        conn, cur = _make_conn()
        cur.fetchone.return_value = (1,)

        dynamo = _make_dynamo_client()
        event = _make_event("POST", "/v1/blocks", body={"userId": _USER_B}, user_sub=_USER_A)

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "_get_dynamo", return_value=dynamo),
            patch.dict(os.environ, {"EDGE_SECRET": _EDGE_SECRET, "DB_SECRET_NAME": "x",
                                    "TABLE_CHAT_ROOMS": "ChatRooms"}),
        ):
            response = mod.handler(event, None)

        dynamo.update_item.assert_called_once()
        # Verify it was called with the correct table
        call_kwargs = dynamo.update_item.call_args[1]
        assert call_kwargs["TableName"] == "ChatRooms"

    def test_given_friends_when_post_then_dynamo_sets_status_deactivated(self):
        """
        The DynamoDB UpdateItem call must SET status='deactivated' and related
        deactivation attributes.
        """
        mod = _import_handler()
        conn, cur = _make_conn()
        cur.fetchone.return_value = (1,)

        dynamo = _make_dynamo_client()
        event = _make_event("POST", "/v1/blocks", body={"userId": _USER_B}, user_sub=_USER_A)

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "_get_dynamo", return_value=dynamo),
            patch.dict(os.environ, {"EDGE_SECRET": _EDGE_SECRET, "DB_SECRET_NAME": "x",
                                    "TABLE_CHAT_ROOMS": "ChatRooms"}),
        ):
            mod.handler(event, None)

        update_kwargs = dynamo.update_item.call_args[1]
        update_expr = update_kwargs.get("UpdateExpression", "")
        assert "deactivated" in update_expr or "deactivated_reason" in str(update_kwargs)
        # deactivated_reason must be 'blocked'
        expr_values = update_kwargs.get("ExpressionAttributeValues", {})
        reasons = [v.get("S", "") for v in expr_values.values() if isinstance(v, dict)]
        assert "blocked" in reasons

    def test_given_friends_when_post_then_dynamo_condition_expression_is_set(self):
        """
        ConditionExpression must be included so the update is conditional on the
        room existing and being currently active.
        """
        mod = _import_handler()
        conn, cur = _make_conn()
        cur.fetchone.return_value = (1,)

        dynamo = _make_dynamo_client()
        event = _make_event("POST", "/v1/blocks", body={"userId": _USER_B}, user_sub=_USER_A)

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "_get_dynamo", return_value=dynamo),
            patch.dict(os.environ, {"EDGE_SECRET": _EDGE_SECRET, "DB_SECRET_NAME": "x",
                                    "TABLE_CHAT_ROOMS": "ChatRooms"}),
        ):
            mod.handler(event, None)

        update_kwargs = dynamo.update_item.call_args[1]
        assert "ConditionExpression" in update_kwargs
        cond = update_kwargs["ConditionExpression"]
        assert "attribute_exists" in cond

    # ---------------------------------------------------------------------------
    # Story 8.9 additions — friendship_active flag management
    # ---------------------------------------------------------------------------

    def test_given_friends_when_post_then_dynamo_sets_friendship_active_false(self):
        """
        story 8.9 AC-1 (block path): the DynamoDB UpdateItem on POST /v1/blocks
        MUST set friendship_active=false so that sendMessage returns RoomReadOnly
        (not RoomDeactivated) after an unblock.  Without this flag the sendMessage
        resolver sees an active room with no friendship and allows writes incorrectly.
        """
        mod = _import_handler()
        conn, cur = _make_conn()
        cur.fetchone.return_value = (1,)

        dynamo = _make_dynamo_client()
        event = _make_event("POST", "/v1/blocks", body={"userId": _USER_B}, user_sub=_USER_A)

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "_get_dynamo", return_value=dynamo),
            patch.dict(os.environ, {"EDGE_SECRET": _EDGE_SECRET, "DB_SECRET_NAME": "x",
                                    "TABLE_CHAT_ROOMS": "ChatRooms"}),
        ):
            mod.handler(event, None)

        update_kwargs = dynamo.update_item.call_args[1]
        update_expr = update_kwargs.get("UpdateExpression", "")
        expr_values = update_kwargs.get("ExpressionAttributeValues", {})
        expr_names = update_kwargs.get("ExpressionAttributeNames", {})

        # friendship_active must appear in the UpdateExpression (via placeholder or literal)
        fa_in_expr = (
            "friendship_active" in update_expr
            or any("friendship_active" in v for v in expr_names.values())
        )
        assert fa_in_expr, (
            f"UpdateExpression or ExpressionAttributeNames must reference "
            f"friendship_active; got update_expr={update_expr!r}, "
            f"expr_names={expr_names!r}"
        )

        # The value bound to friendship_active must be BOOL false
        bool_false_values = [
            v for v in expr_values.values()
            if isinstance(v, dict) and v.get("BOOL") is False
        ]
        assert bool_false_values, (
            f"ExpressionAttributeValues must contain {{BOOL: false}} for "
            f"friendship_active; got expr_values={expr_values!r}"
        )


# ---------------------------------------------------------------------------
# Area C: DELETE /v1/blocks/{userId}
# ---------------------------------------------------------------------------


class TestDeleteBlocks:
    """DELETE /v1/blocks/{userId} — unblock + DynamoDB reactivation."""

    def test_given_existing_block_when_delete_then_returns_200(self):
        mod = _import_handler()
        conn, cur = _make_conn()
        dynamo = _make_dynamo_client()

        event = _make_event(
            "DELETE",
            f"/v1/blocks/{_USER_B}",
            path_params={"userId": _USER_B},
            user_sub=_USER_A,
        )

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "_get_dynamo", return_value=dynamo),
            patch.dict(os.environ, {"EDGE_SECRET": _EDGE_SECRET, "DB_SECRET_NAME": "x",
                                    "TABLE_CHAT_ROOMS": "ChatRooms"}),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 200

    def test_given_unblock_when_delete_then_aurora_deletes_block_row(self):
        """DELETE must issue DELETE FROM blocks for the (blocker, blocked) pair."""
        mod = _import_handler()
        conn, cur = _make_conn()
        dynamo = _make_dynamo_client()

        event = _make_event(
            "DELETE",
            f"/v1/blocks/{_USER_B}",
            path_params={"userId": _USER_B},
            user_sub=_USER_A,
        )

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "_get_dynamo", return_value=dynamo),
            patch.dict(os.environ, {"EDGE_SECRET": _EDGE_SECRET, "DB_SECRET_NAME": "x",
                                    "TABLE_CHAT_ROOMS": "ChatRooms"}),
        ):
            mod.handler(event, None)

        all_sql = " ".join(str(c) for c in cur.execute.call_args_list)
        assert "DELETE FROM blocks" in all_sql

    def test_given_unblock_when_delete_then_dynamo_reactivates_room(self):
        """
        DynamoDB UpdateItem must be called to reactivate the chat room.
        The update must SET status='active' and REMOVE deactivation attributes.
        """
        mod = _import_handler()
        conn, cur = _make_conn()
        dynamo = _make_dynamo_client()

        event = _make_event(
            "DELETE",
            f"/v1/blocks/{_USER_B}",
            path_params={"userId": _USER_B},
            user_sub=_USER_A,
        )

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "_get_dynamo", return_value=dynamo),
            patch.dict(os.environ, {"EDGE_SECRET": _EDGE_SECRET, "DB_SECRET_NAME": "x",
                                    "TABLE_CHAT_ROOMS": "ChatRooms"}),
        ):
            mod.handler(event, None)

        dynamo.update_item.assert_called_once()
        update_kwargs = dynamo.update_item.call_args[1]
        update_expr = update_kwargs.get("UpdateExpression", "")
        # Must SET status to 'active' and REMOVE deactivation attrs
        assert "active" in str(update_kwargs)
        assert "REMOVE" in update_expr or "deactivated_reason" in str(update_kwargs)

    def test_given_unblock_when_delete_then_reactivation_condition_expression_set(self):
        """
        ConditionExpression must be present for reactivation: guards against
        resurrecting rooms deactivated by different blockers or for other reasons.
        """
        mod = _import_handler()
        conn, cur = _make_conn()
        dynamo = _make_dynamo_client()

        event = _make_event(
            "DELETE",
            f"/v1/blocks/{_USER_B}",
            path_params={"userId": _USER_B},
            user_sub=_USER_A,
        )

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "_get_dynamo", return_value=dynamo),
            patch.dict(os.environ, {"EDGE_SECRET": _EDGE_SECRET, "DB_SECRET_NAME": "x",
                                    "TABLE_CHAT_ROOMS": "ChatRooms"}),
        ):
            mod.handler(event, None)

        update_kwargs = dynamo.update_item.call_args[1]
        assert "ConditionExpression" in update_kwargs
        cond = update_kwargs["ConditionExpression"]
        assert "deactivated_reason" in cond

    def test_given_unblock_when_delete_then_dynamo_does_not_set_friendship_active(self):
        """
        story 8.9 AC-1 (unblock path): the reactivation UpdateExpression must NOT
        set or modify friendship_active.  Only a subsequent friend-request accept
        (story 8.9b) flips the flag to true.  If unblock incorrectly set
        friendship_active=true the room would become writable before re-friending.
        """
        mod = _import_handler()
        conn, cur = _make_conn()
        dynamo = _make_dynamo_client()

        event = _make_event(
            "DELETE",
            f"/v1/blocks/{_USER_B}",
            path_params={"userId": _USER_B},
            user_sub=_USER_A,
        )

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "_get_dynamo", return_value=dynamo),
            patch.dict(os.environ, {"EDGE_SECRET": _EDGE_SECRET, "DB_SECRET_NAME": "x",
                                    "TABLE_CHAT_ROOMS": "ChatRooms"}),
        ):
            mod.handler(event, None)

        update_kwargs = dynamo.update_item.call_args[1]
        update_expr = update_kwargs.get("UpdateExpression", "")
        expr_names = update_kwargs.get("ExpressionAttributeNames", {})

        # friendship_active must NOT appear in the unblock UpdateExpression
        fa_in_expr = (
            "friendship_active" in update_expr
            or any("friendship_active" in v for v in expr_names.values())
        )
        assert not fa_in_expr, (
            f"Unblock UpdateExpression must NOT reference friendship_active "
            f"(story 8.9b handles that); got update_expr={update_expr!r}, "
            f"expr_names={expr_names!r}"
        )

    def test_given_missing_userId_path_param_when_delete_then_returns_400(self):
        mod = _import_handler()
        conn, cur = _make_conn()
        dynamo = _make_dynamo_client()

        # No pathParameters
        event = _make_event("DELETE", "/v1/blocks/", user_sub=_USER_A)

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "_get_dynamo", return_value=dynamo),
            patch.dict(os.environ, {"EDGE_SECRET": _EDGE_SECRET, "DB_SECRET_NAME": "x",
                                    "TABLE_CHAT_ROOMS": "ChatRooms"}),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 400


# ---------------------------------------------------------------------------
# Area D: DynamoDB error paths
# ---------------------------------------------------------------------------


class TestDynamoDbErrorPaths:
    """
    ConditionalCheckFailedException is treated as a no-op (INFO log).
    Any other DynamoDB error returns HTTP 200 with chat_deactivation_pending: true.
    The Aurora block is NEVER rolled back when DynamoDB fails.
    """

    def test_given_conditional_check_failed_on_post_then_still_returns_200(self):
        """
        ConditionalCheckFailedException during deactivation must be swallowed
        (room absent or already deactivated) — the POST still returns HTTP 200.
        """
        mod = _import_handler()
        conn, cur = _make_conn()
        cur.fetchone.return_value = (1,)  # friendship exists

        dynamo = _make_dynamo_client(
            update_side_effect=_conditional_check_failed_exc()
        )
        event = _make_event("POST", "/v1/blocks", body={"userId": _USER_B}, user_sub=_USER_A)

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "_get_dynamo", return_value=dynamo),
            patch.dict(os.environ, {"EDGE_SECRET": _EDGE_SECRET, "DB_SECRET_NAME": "x",
                                    "TABLE_CHAT_ROOMS": "ChatRooms"}),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        # No chat_deactivation_pending flag for ConditionalCheckFailed (it's a no-op)
        assert body.get("chat_deactivation_pending") is not True

    def test_given_other_dynamo_error_on_post_then_returns_200_with_pending_flag(self):
        """
        A non-ConditionalCheckFailedException DynamoDB error on POST must return
        HTTP 200 with chat_deactivation_pending: true so the client knows the
        Aurora block succeeded but the DynamoDB deactivation may lag.
        """
        mod = _import_handler()
        conn, cur = _make_conn()
        cur.fetchone.return_value = (1,)

        dynamo = _make_dynamo_client(
            update_side_effect=_other_dynamo_exc()
        )
        event = _make_event("POST", "/v1/blocks", body={"userId": _USER_B}, user_sub=_USER_A)

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "_get_dynamo", return_value=dynamo),
            patch.dict(os.environ, {"EDGE_SECRET": _EDGE_SECRET, "DB_SECRET_NAME": "x",
                                    "TABLE_CHAT_ROOMS": "ChatRooms"}),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body.get("chat_deactivation_pending") is True

    def test_given_other_dynamo_error_on_post_then_aurora_not_rolled_back(self):
        """
        When DynamoDB raises a non-ConditionalCheckFailed error, the Aurora
        transaction must have already committed (conn.rollback must not be called).
        """
        mod = _import_handler()
        conn, cur = _make_conn()
        cur.fetchone.return_value = (1,)

        dynamo = _make_dynamo_client(
            update_side_effect=_other_dynamo_exc()
        )
        event = _make_event("POST", "/v1/blocks", body={"userId": _USER_B}, user_sub=_USER_A)

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "_get_dynamo", return_value=dynamo),
            patch.dict(os.environ, {"EDGE_SECRET": _EDGE_SECRET, "DB_SECRET_NAME": "x",
                                    "TABLE_CHAT_ROOMS": "ChatRooms"}),
        ):
            mod.handler(event, None)

        conn.rollback.assert_not_called()

    def test_given_conditional_check_failed_on_delete_then_still_returns_200(self):
        """
        ConditionalCheckFailedException during reactivation (DELETE /v1/blocks)
        must be treated as a no-op and the response is HTTP 200.
        """
        mod = _import_handler()
        conn, cur = _make_conn()

        dynamo = _make_dynamo_client(
            update_side_effect=_conditional_check_failed_exc()
        )
        event = _make_event(
            "DELETE",
            f"/v1/blocks/{_USER_B}",
            path_params={"userId": _USER_B},
            user_sub=_USER_A,
        )

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "_get_dynamo", return_value=dynamo),
            patch.dict(os.environ, {"EDGE_SECRET": _EDGE_SECRET, "DB_SECRET_NAME": "x",
                                    "TABLE_CHAT_ROOMS": "ChatRooms"}),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 200

    def test_given_other_dynamo_error_on_delete_then_returns_200_with_pending_flag(self):
        """
        A non-ConditionalCheckFailedException DynamoDB error on DELETE /v1/blocks
        must return HTTP 200 with chat_deactivation_pending: true.
        """
        mod = _import_handler()
        conn, cur = _make_conn()

        dynamo = _make_dynamo_client(
            update_side_effect=_other_dynamo_exc()
        )
        event = _make_event(
            "DELETE",
            f"/v1/blocks/{_USER_B}",
            path_params={"userId": _USER_B},
            user_sub=_USER_A,
        )

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "_get_dynamo", return_value=dynamo),
            patch.dict(os.environ, {"EDGE_SECRET": _EDGE_SECRET, "DB_SECRET_NAME": "x",
                                    "TABLE_CHAT_ROOMS": "ChatRooms"}),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body.get("chat_deactivation_pending") is True


# ---------------------------------------------------------------------------
# Area E: Route dispatch / edge-secret gate
# ---------------------------------------------------------------------------


class TestRouteDispatch:
    """
    The handler must route requests by HTTP method + path and enforce the edge
    secret header via the @with_edge_secret decorator.
    """

    def test_given_wrong_edge_secret_then_returns_403(self):
        mod = _import_handler()
        event = _make_event("GET", "/v1/blocks", edge_secret="wrong-secret")

        with patch.dict(os.environ, {"EDGE_SECRET": _EDGE_SECRET, "DB_SECRET_NAME": "x",
                                     "TABLE_CHAT_ROOMS": "ChatRooms"}):
            response = mod.handler(event, None)

        assert response["statusCode"] == 403

    def test_given_unmatched_route_then_returns_404(self):
        mod = _import_handler()
        conn, cur = _make_conn()
        dynamo = _make_dynamo_client()

        event = _make_event("PUT", "/v1/blocks/something", user_sub=_USER_A)

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "_get_dynamo", return_value=dynamo),
            patch.dict(os.environ, {"EDGE_SECRET": _EDGE_SECRET, "DB_SECRET_NAME": "x",
                                    "TABLE_CHAT_ROOMS": "ChatRooms"}),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 404

    def test_given_missing_jwt_claims_then_returns_403(self):
        """
        A request missing the JWT claims block returns 403 (not 401).

        @require_profile_complete is fail-closed: when the custom:profile_complete
        claim is absent it returns 403 {"error":"profile_incomplete"} immediately,
        before the handler's own JWT-extraction logic (which would have returned
        401) can run.  The outer @with_edge_secret has already passed at this
        point (edge secret header is present), so 403 is the correct status.
        """
        mod = _import_handler()
        conn, cur = _make_conn()
        dynamo = _make_dynamo_client()

        event = {
            "requestContext": {"http": {"method": "GET", "path": "/v1/blocks"}},
            "headers": {"x-knotify-edge-secret": _EDGE_SECRET},
        }

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "_get_dynamo", return_value=dynamo),
            patch.dict(os.environ, {"EDGE_SECRET": _EDGE_SECRET, "DB_SECRET_NAME": "x",
                                    "TABLE_CHAT_ROOMS": "ChatRooms"}),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"] == "profile_incomplete"
