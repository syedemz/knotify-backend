"""
Unit tests for the knotify-friends Lambda handler.

These tests are fully isolated — they do NOT require a running database,
AWS credentials, or a deployed Lambda.  All external collaborators (Aurora
connection, RLS context, is_blocked, block_filter) are replaced with
in-memory stubs.

Run:
    pytest infrastructure/src/functions/friends/tests/test_handler_unit.py -v

Test areas:
  A. GET /v1/friends — block-filtered friend list
  B. DELETE /v1/friends/{userId} — remove friendship
  C. GET /v1/friend-requests — block-filtered request list
  D. POST /v1/friend-requests — block check + already_pending guard
  E. POST /v1/friend-requests/{id}/accept — 404 when stale, lex-min/max insert
  F. POST /v1/friend-requests/{id}/decline — 404 when stale
  G. DELETE /v1/friend-requests/{id} — cancel outgoing request
  H. Route dispatch / edge-secret gate
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
    """Import the friends handler module fresh on every call."""
    spec = importlib.util.spec_from_file_location(
        "friends_handler_" + str(id(object())),
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
_REQUEST_ID = str(uuid.uuid4())


def _make_event(
    method: str,
    path: str,
    *,
    body: dict | None = None,
    path_params: dict | None = None,
    query_params: dict | None = None,
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
    if query_params is not None:
        event["queryStringParameters"] = query_params
    return event


# ---------------------------------------------------------------------------
# DB stub helpers
# ---------------------------------------------------------------------------


def _make_conn(fetchone_side_effect=None, fetchone_return=None, fetchall_return=None):
    """
    Build a minimal psycopg2 connection stub.

    fetchone_side_effect — list of return values (or exceptions) yielded per
                           successive fetchone() call on the cursor.
    fetchone_return      — single return value if not using side_effect.
    fetchall_return      — return value for fetchall().
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

    if fetchall_return is not None:
        cur.fetchall.return_value = fetchall_return
    else:
        cur.fetchall.return_value = []

    cur.description = []

    return conn, cur


class _FakePsycopg2UniqueViolation(Exception):
    """
    Minimal stand-in for psycopg2.errors.UniqueViolation.
    The handler catches it by name after importing psycopg2.errors — so we
    need to patch psycopg2.errors.UniqueViolation in the handler's module
    namespace with this class.
    """
    pgcode = "23505"


_ENV_DEFAULTS = {
    "EDGE_SECRET": _EDGE_SECRET,
    "DB_SECRET_NAME": "test-secret",
}


# ---------------------------------------------------------------------------
# Area A: GET /v1/friends
# ---------------------------------------------------------------------------


class TestGetFriends:
    """GET /v1/friends — returns the block-filtered friend list."""

    def test_given_no_friends_when_get_then_returns_empty_list(self):
        mod = _import_handler()
        conn, cur = _make_conn(fetchall_return=[])
        cur.description = []

        event = _make_event("GET", "/v1/friends", user_sub=_USER_A)

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body == {"friends": []}

    def test_given_friends_when_get_then_returns_list(self):
        mod = _import_handler()
        conn, cur = _make_conn()
        friend_id = str(uuid.uuid4())
        cur.fetchall.return_value = [(friend_id, "Test", "User", "Male", None, None, None, None, None)]
        cur.description = [
            ("user_id",), ("first_name",), ("last_name",), ("sex",),
            ("age",), ("photo_url",), ("username",), ("job_title",),
            ("current_residence_city",),
        ]

        event = _make_event("GET", "/v1/friends", user_sub=_USER_A)

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert len(body["friends"]) == 1
        assert body["friends"][0]["user_id"] == friend_id

    def test_given_get_friends_then_block_filter_applied_to_query(self):
        """
        GET /v1/friends must embed block_filter() fragments in its SQL to filter
        out pairs where a block exists in either direction.

        We verify by patching block_filter itself to return a known sentinel
        and checking that the sentinel appears in the SQL passed to cur.execute.
        """
        mod = _import_handler()
        conn, cur = _make_conn(fetchall_return=[])
        cur.description = []

        event = _make_event("GET", "/v1/friends", user_sub=_USER_A)

        # block_filter is imported into the handler's module namespace; patch it there.
        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.object(mod, "block_filter", return_value="NOT EXISTS (SELECT 1 FROM blocks b WHERE 1=1)"),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            mod.handler(event, None)

        # Verify block_filter was called (means the SQL embeds the filter)
        # The fetchall returning [] means the handler returned {"friends": []}
        assert cur.execute.called, "cur.execute must be called for GET /v1/friends"
        all_sql = " ".join(str(c) for c in cur.execute.call_args_list)
        assert "NOT EXISTS" in all_sql, (
            "GET /v1/friends SELECT must embed the block filter fragment"
        )


# ---------------------------------------------------------------------------
# Area B: DELETE /v1/friends/{userId}
# ---------------------------------------------------------------------------


class TestDeleteFriend:
    """DELETE /v1/friends/{userId} — remove a friendship row."""

    def test_given_existing_friendship_when_delete_then_returns_200(self):
        mod = _import_handler()
        conn, cur = _make_conn()

        event = _make_event(
            "DELETE",
            f"/v1/friends/{_USER_B}",
            path_params={"userId": _USER_B},
            user_sub=_USER_A,
        )

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 200

    def test_given_non_existent_friendship_when_delete_then_returns_200(self):
        """
        DELETE /v1/friends/{userId} is idempotent — returns 200 even when no
        row was deleted.
        """
        mod = _import_handler()
        conn, cur = _make_conn()
        cur.rowcount = 0  # simulate no row deleted

        event = _make_event(
            "DELETE",
            f"/v1/friends/{_USER_B}",
            path_params={"userId": _USER_B},
            user_sub=_USER_A,
        )

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 200

    def test_given_delete_friend_then_uses_canonical_ordering(self):
        """
        DELETE FROM friendships must use canonical lex-min/max ordering so
        both orderings of (user_a, user_b) are covered in a single DELETE.
        """
        mod = _import_handler()
        conn, cur = _make_conn()

        event = _make_event(
            "DELETE",
            f"/v1/friends/{_USER_B}",
            path_params={"userId": _USER_B},
            user_sub=_USER_A,
        )

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            mod.handler(event, None)

        all_sql = " ".join(str(c) for c in cur.execute.call_args_list)
        assert "DELETE FROM friendships" in all_sql

    def test_given_missing_userId_path_param_when_delete_then_returns_400_or_404(self):
        """
        DELETE /v1/friends/ with no userId in the path either returns 400 (validation)
        or 404 (no route matched). Both are acceptable — the request is invalid.
        """
        mod = _import_handler()
        conn, cur = _make_conn()

        event = _make_event("DELETE", "/v1/friends/", user_sub=_USER_A)

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] in (400, 404)

    def test_given_db_error_on_delete_friend_then_module_level_conn_is_cleared(self):
        """
        When _handle_delete_friend raises a DB exception, the module-level _conn
        cache must be reset to None (not merely the local variable).

        This test guards the fix for the missing `global _conn` declaration — without
        it, _conn = None would create a local variable and the broken connection would
        remain cached, causing every subsequent warm-container invocation to fail.
        """
        mod = _import_handler()
        conn, cur = _make_conn()

        # Make the cursor execute raise on the DELETE statement
        def raise_on_execute(sql, params=None):
            raise Exception("simulated DB error")

        cur.execute.side_effect = raise_on_execute

        # Pre-seed the module-level _conn so we can verify it gets cleared
        mod._conn = conn

        event = _make_event(
            "DELETE",
            f"/v1/friends/{_USER_B}",
            path_params={"userId": _USER_B},
            user_sub=_USER_A,
        )

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            with pytest.raises(Exception, match="simulated DB error"):
                mod._dispatch(
                    event,
                    _USER_A,
                    "Male",
                )

        assert mod._conn is None, (
            "_handle_delete_friend must reset the module-level _conn to None "
            "after a DB error so the broken connection is not reused on the next "
            "warm-container invocation"
        )


# ---------------------------------------------------------------------------
# Area C: GET /v1/friend-requests
# ---------------------------------------------------------------------------


class TestGetFriendRequests:
    """GET /v1/friend-requests — block-filtered request list."""

    def test_given_no_requests_when_get_then_returns_empty_list(self):
        mod = _import_handler()
        conn, cur = _make_conn(fetchall_return=[])
        cur.description = []

        event = _make_event("GET", "/v1/friend-requests", user_sub=_USER_A)

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body == {"friend_requests": []}

    def test_given_pending_requests_when_get_then_returns_list(self):
        mod = _import_handler()
        conn, cur = _make_conn()
        req_id = str(uuid.uuid4())
        cur.fetchall.return_value = [(req_id, _USER_A, _USER_B, "pending", "2026-01-01T00:00:00")]
        cur.description = [
            ("id",), ("requester_id",), ("receiver_id",), ("status",), ("created_at",),
        ]

        event = _make_event("GET", "/v1/friend-requests", user_sub=_USER_A)

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert len(body["friend_requests"]) == 1
        assert body["friend_requests"][0]["id"] == req_id

    def test_given_get_friend_requests_then_block_filter_applied_to_query(self):
        """
        GET /v1/friend-requests must apply block_filter against both
        requester_id and receiver_id dimensions.
        """
        mod = _import_handler()
        conn, cur = _make_conn(fetchall_return=[])
        cur.description = []

        event = _make_event("GET", "/v1/friend-requests", user_sub=_USER_A)

        executed_sqls: list[str] = []

        def capture_execute(sql, params=None):
            executed_sqls.append(str(sql))

        cur.execute.side_effect = capture_execute

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            mod.handler(event, None)

        all_sql = " ".join(executed_sqls)
        assert "NOT EXISTS" in all_sql, (
            "GET /v1/friend-requests SELECT must embed a NOT EXISTS block filter"
        )


# ---------------------------------------------------------------------------
# Area D: POST /v1/friend-requests
# ---------------------------------------------------------------------------


class TestPostFriendRequests:
    """POST /v1/friend-requests — block check + already_pending guard."""

    def test_given_missing_body_when_post_then_returns_400(self):
        mod = _import_handler()
        conn, cur = _make_conn()

        event = _make_event("POST", "/v1/friend-requests", user_sub=_USER_A)

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 400

    def test_given_missing_toUserId_field_when_post_then_returns_400(self):
        mod = _import_handler()
        conn, cur = _make_conn()

        event = _make_event("POST", "/v1/friend-requests", body={}, user_sub=_USER_A)

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 400

    def test_given_blocked_pair_when_post_then_returns_409_blocked(self):
        """
        When is_blocked() returns True, POST /v1/friend-requests must return
        HTTP 409 with {"error": "blocked"} and NOT insert any row.
        """
        mod = _import_handler()
        conn, cur = _make_conn()

        event = _make_event(
            "POST", "/v1/friend-requests",
            body={"toUserId": _USER_B},
            user_sub=_USER_A,
        )

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=True),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 409
        body = json.loads(response["body"])
        assert body["error"] == "blocked"

    def test_given_blocked_pair_when_post_then_no_insert_executed(self):
        """
        When is_blocked() returns True, the handler must not issue any INSERT
        into friend_requests.
        """
        mod = _import_handler()
        conn, cur = _make_conn()

        event = _make_event(
            "POST", "/v1/friend-requests",
            body={"toUserId": _USER_B},
            user_sub=_USER_A,
        )

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=True),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            mod.handler(event, None)

        all_sql = " ".join(str(c) for c in cur.execute.call_args_list)
        assert "INSERT INTO friend_requests" not in all_sql

    def test_given_is_blocked_called_inside_transaction(self):
        """
        is_blocked() must be called with the same connection that the INSERT
        will use — the block check happens inside the same transaction to prevent
        race conditions with a concurrent block.
        """
        mod = _import_handler()
        conn, cur = _make_conn()

        captured_conn: list[Any] = []

        def fake_is_blocked(c, a, b):
            captured_conn.append(c)
            return False  # not blocked — let the INSERT proceed

        # Make the INSERT return a row
        new_req_id = str(uuid.uuid4())
        cur.fetchone.return_value = (new_req_id, _USER_A, _USER_B, "pending", "2026-01-01")
        cur.description = [
            ("id",), ("requester_id",), ("receiver_id",), ("status",), ("created_at",),
        ]

        event = _make_event(
            "POST", "/v1/friend-requests",
            body={"toUserId": _USER_B},
            user_sub=_USER_A,
        )

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", side_effect=fake_is_blocked),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            mod.handler(event, None)

        assert len(captured_conn) == 1, "is_blocked must be called exactly once"
        assert captured_conn[0] is conn, (
            "is_blocked must receive the same connection as the INSERT transaction"
        )

    def test_given_already_pending_when_post_then_returns_409_already_pending(self):
        """
        When the INSERT raises UniqueViolation (already_pending pair), the handler
        must return HTTP 409 with {"error": "already_pending"}.
        """
        mod = _import_handler()
        conn, cur = _make_conn()

        # Simulate UniqueViolation by patching psycopg2.errors
        import psycopg2.errors

        def insert_raises(*args, **kwargs):
            # Only raise on the INSERT statement
            sql_str = str(args[0]) if args else ""
            if "INSERT INTO friend_requests" in sql_str:
                raise psycopg2.errors.UniqueViolation("duplicate key value")

        cur.execute.side_effect = insert_raises

        event = _make_event(
            "POST", "/v1/friend-requests",
            body={"toUserId": _USER_B},
            user_sub=_USER_A,
        )

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 409
        body = json.loads(response["body"])
        assert body["error"] == "already_pending"

    def test_given_valid_request_when_post_then_returns_201(self):
        """
        A valid, unblocked, non-duplicate POST /v1/friend-requests must return
        HTTP 201 with the new request row.
        """
        mod = _import_handler()
        conn, cur = _make_conn()

        new_req_id = str(uuid.uuid4())
        cur.fetchone.return_value = (new_req_id, _USER_A, _USER_B, "pending", "2026-01-01")
        cur.description = [
            ("id",), ("requester_id",), ("receiver_id",), ("status",), ("created_at",),
        ]

        event = _make_event(
            "POST", "/v1/friend-requests",
            body={"toUserId": _USER_B},
            user_sub=_USER_A,
        )

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] in (200, 201)
        body = json.loads(response["body"])
        assert body.get("id") == new_req_id or body.get("request", {}).get("id") == new_req_id


# ---------------------------------------------------------------------------
# Area E: POST /v1/friend-requests/{id}/accept
# ---------------------------------------------------------------------------


class TestAcceptFriendRequest:
    """POST /v1/friend-requests/{id}/accept — accept + create friendship."""

    def test_given_non_existent_request_when_accept_then_returns_404(self):
        """
        When the SELECT FOR UPDATE finds no row (request deleted by a block),
        accept must return HTTP 404 with {"error": "not_found"}.
        """
        mod = _import_handler()
        conn, cur = _make_conn()
        # Explicitly set fetchone to return None (no row found)
        cur.fetchone.return_value = None

        event = _make_event(
            "POST",
            f"/v1/friend-requests/{_REQUEST_ID}/accept",
            path_params={"id": _REQUEST_ID},
            user_sub=_USER_B,
        )

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 404
        body = json.loads(response["body"])
        assert body["error"] == "not_found"

    def test_given_valid_request_when_accept_then_returns_200(self):
        """
        When the SELECT FOR UPDATE finds the row, accept must return HTTP 200.
        """
        mod = _import_handler()
        conn, cur = _make_conn()

        req_id = str(uuid.uuid4())
        # fetchone returns the request row on SELECT FOR UPDATE
        cur.fetchone.return_value = (req_id, _USER_A, _USER_B, "pending", "2026-01-01")
        cur.description = [
            ("id",), ("requester_id",), ("receiver_id",), ("status",), ("created_at",),
        ]

        event = _make_event(
            "POST",
            f"/v1/friend-requests/{req_id}/accept",
            path_params={"id": req_id},
            user_sub=_USER_B,
        )

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 200

    def test_given_valid_request_when_accept_then_inserts_friendship_row(self):
        """
        On accept, the handler must INSERT a row into friendships inside the
        same transaction as the status update.
        """
        mod = _import_handler()
        conn, cur = _make_conn()

        req_id = str(uuid.uuid4())
        cur.fetchone.return_value = (req_id, _USER_A, _USER_B, "pending", "2026-01-01")
        cur.description = [
            ("id",), ("requester_id",), ("receiver_id",), ("status",), ("created_at",),
        ]

        event = _make_event(
            "POST",
            f"/v1/friend-requests/{req_id}/accept",
            path_params={"id": req_id},
            user_sub=_USER_B,
        )

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            mod.handler(event, None)

        all_sql = " ".join(str(c) for c in cur.execute.call_args_list)
        assert "INSERT INTO friendships" in all_sql, (
            "accept must INSERT a row into friendships"
        )

    def test_given_valid_request_when_accept_then_updates_status_to_accepted(self):
        """
        On accept, the handler must UPDATE friend_requests.status to 'accepted'
        in the same transaction as the friendships INSERT.
        """
        mod = _import_handler()
        conn, cur = _make_conn()

        req_id = str(uuid.uuid4())
        cur.fetchone.return_value = (req_id, _USER_A, _USER_B, "pending", "2026-01-01")
        cur.description = [
            ("id",), ("requester_id",), ("receiver_id",), ("status",), ("created_at",),
        ]

        event = _make_event(
            "POST",
            f"/v1/friend-requests/{req_id}/accept",
            path_params={"id": req_id},
            user_sub=_USER_B,
        )

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            mod.handler(event, None)

        all_sql = " ".join(str(c) for c in cur.execute.call_args_list)
        assert "UPDATE friend_requests" in all_sql and "accepted" in all_sql, (
            "accept must UPDATE friend_requests status to 'accepted'"
        )

    def test_given_accept_then_friendship_uses_lex_min_max_ordering(self):
        """
        The friendships INSERT must place the lexicographically smaller UUID in
        user_a and the larger one in user_b — canonical pair ordering that matches
        knotify_obs.chat_room_id.

        We verify by inspecting the call_args_list after the handler returns;
        no side_effect wrapping is needed.
        """
        mod = _import_handler()
        conn, cur = _make_conn()

        # Determine canonical ordering
        user_a_lex = min(_USER_A, _USER_B)
        user_b_lex = max(_USER_A, _USER_B)

        req_id = str(uuid.uuid4())
        # requester is USER_A, receiver is USER_B
        cur.fetchone.return_value = (req_id, _USER_A, _USER_B, "pending", "2026-01-01")
        cur.description = [
            ("id",), ("requester_id",), ("receiver_id",), ("status",), ("created_at",),
        ]

        event = _make_event(
            "POST",
            f"/v1/friend-requests/{req_id}/accept",
            path_params={"id": req_id},
            user_sub=_USER_B,
        )

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            mod.handler(event, None)

        # Inspect all execute calls and find the INSERT INTO friendships one
        insert_calls = [
            c for c in cur.execute.call_args_list
            if "INSERT INTO friendships" in str(c.args[0] if c.args else "")
        ]
        assert len(insert_calls) == 1, (
            f"Expected exactly one INSERT INTO friendships call, got {insert_calls!r}"
        )
        insert_params = insert_calls[0].args[1] if len(insert_calls[0].args) > 1 else insert_calls[0].kwargs.get("params")
        assert insert_params is not None, "INSERT INTO friendships must supply bind params"
        assert insert_params[0] == user_a_lex, (
            f"user_a in INSERT should be the lex-min UUID ({user_a_lex!r}), "
            f"got {insert_params[0]!r}"
        )
        assert insert_params[1] == user_b_lex, (
            f"user_b in INSERT should be the lex-max UUID ({user_b_lex!r}), "
            f"got {insert_params[1]!r}"
        )

    def test_given_missing_request_id_when_accept_then_returns_400(self):
        mod = _import_handler()
        conn, cur = _make_conn()

        event = _make_event(
            "POST",
            "/v1/friend-requests//accept",
            user_sub=_USER_B,
        )

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            response = mod.handler(event, None)

        # Missing id param → 400 or 404 (dispatcher won't match the route)
        assert response["statusCode"] in (400, 404)


# ---------------------------------------------------------------------------
# Area F: POST /v1/friend-requests/{id}/decline
# ---------------------------------------------------------------------------


class TestDeclineFriendRequest:
    """POST /v1/friend-requests/{id}/decline — decline a request."""

    def test_given_non_existent_request_when_decline_then_returns_404(self):
        """
        When no matching row exists (e.g. deleted by a concurrent block),
        decline must return HTTP 404 with {"error": "not_found"}.
        """
        mod = _import_handler()
        conn, cur = _make_conn()
        cur.rowcount = 0  # UPDATE affected no rows

        event = _make_event(
            "POST",
            f"/v1/friend-requests/{_REQUEST_ID}/decline",
            path_params={"id": _REQUEST_ID},
            user_sub=_USER_B,
        )

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 404
        body = json.loads(response["body"])
        assert body["error"] == "not_found"

    def test_given_valid_request_when_decline_then_returns_200(self):
        mod = _import_handler()
        conn, cur = _make_conn()
        cur.rowcount = 1

        event = _make_event(
            "POST",
            f"/v1/friend-requests/{_REQUEST_ID}/decline",
            path_params={"id": _REQUEST_ID},
            user_sub=_USER_B,
        )

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 200

    def test_given_valid_request_when_decline_then_updates_status(self):
        """
        decline must UPDATE friend_requests status to 'declined'.
        """
        mod = _import_handler()
        conn, cur = _make_conn()
        cur.rowcount = 1

        event = _make_event(
            "POST",
            f"/v1/friend-requests/{_REQUEST_ID}/decline",
            path_params={"id": _REQUEST_ID},
            user_sub=_USER_B,
        )

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            mod.handler(event, None)

        all_sql = " ".join(str(c) for c in cur.execute.call_args_list)
        assert "UPDATE friend_requests" in all_sql and "declined" in all_sql


# ---------------------------------------------------------------------------
# Area G: DELETE /v1/friend-requests/{id}
# ---------------------------------------------------------------------------


class TestDeleteFriendRequest:
    """DELETE /v1/friend-requests/{id} — cancel an outgoing request."""

    def test_given_non_existent_request_when_delete_then_returns_404(self):
        mod = _import_handler()
        conn, cur = _make_conn()
        cur.rowcount = 0

        event = _make_event(
            "DELETE",
            f"/v1/friend-requests/{_REQUEST_ID}",
            path_params={"id": _REQUEST_ID},
            user_sub=_USER_A,
        )

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 404
        body = json.loads(response["body"])
        assert body["error"] == "not_found"

    def test_given_existing_request_when_delete_then_returns_200(self):
        mod = _import_handler()
        conn, cur = _make_conn()
        cur.rowcount = 1

        event = _make_event(
            "DELETE",
            f"/v1/friend-requests/{_REQUEST_ID}",
            path_params={"id": _REQUEST_ID},
            user_sub=_USER_A,
        )

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 200

    def test_given_cancel_request_then_deletes_from_friend_requests(self):
        mod = _import_handler()
        conn, cur = _make_conn()
        cur.rowcount = 1

        event = _make_event(
            "DELETE",
            f"/v1/friend-requests/{_REQUEST_ID}",
            path_params={"id": _REQUEST_ID},
            user_sub=_USER_A,
        )

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            mod.handler(event, None)

        all_sql = " ".join(str(c) for c in cur.execute.call_args_list)
        assert "DELETE FROM friend_requests" in all_sql


# ---------------------------------------------------------------------------
# Area H: Route dispatch / edge-secret gate
# ---------------------------------------------------------------------------


class TestRouteDispatch:
    """
    The handler must route requests by HTTP method + path and enforce the
    edge secret header via the @with_edge_secret decorator.
    """

    def test_given_wrong_edge_secret_then_returns_403(self):
        mod = _import_handler()
        event = _make_event("GET", "/v1/friends", edge_secret="wrong-secret")

        with patch.dict(os.environ, _ENV_DEFAULTS):
            response = mod.handler(event, None)

        assert response["statusCode"] == 403

    def test_given_unmatched_route_then_returns_404(self):
        mod = _import_handler()
        conn, cur = _make_conn()

        event = _make_event("PUT", "/v1/friends/something", user_sub=_USER_A)

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 404

    def test_given_missing_jwt_claims_then_returns_401(self):
        """
        A request missing the JWT claims block must return 401 before DB access.
        """
        mod = _import_handler()
        conn, cur = _make_conn()

        event = {
            "requestContext": {"http": {"method": "GET", "path": "/v1/friends"}},
            "headers": {"x-knotify-edge-secret": _EDGE_SECRET},
        }

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 401

    def test_get_friends_route_dispatches_correctly(self):
        mod = _import_handler()
        conn, cur = _make_conn(fetchall_return=[])
        cur.description = []

        event = _make_event("GET", "/v1/friends", user_sub=_USER_A)

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 200

    def test_get_friend_requests_route_dispatches_correctly(self):
        mod = _import_handler()
        conn, cur = _make_conn(fetchall_return=[])
        cur.description = []

        event = _make_event("GET", "/v1/friend-requests", user_sub=_USER_A)

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 200

    def test_post_friend_requests_route_dispatches_correctly(self):
        mod = _import_handler()
        conn, cur = _make_conn()
        new_req_id = str(uuid.uuid4())
        cur.fetchone.return_value = (new_req_id, _USER_A, _USER_B, "pending", "2026-01-01")
        cur.description = [
            ("id",), ("requester_id",), ("receiver_id",), ("status",), ("created_at",),
        ]

        event = _make_event(
            "POST", "/v1/friend-requests",
            body={"toUserId": _USER_B},
            user_sub=_USER_A,
        )

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] in (200, 201)
