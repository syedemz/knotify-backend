"""
Unit tests for the knotify-bookmarks Lambda handler.

These tests are fully isolated — they do NOT require a running database,
AWS credentials, or a deployed Lambda.  All external collaborators (Aurora
connection, RLS context, is_blocked, block_filter) are replaced with
in-memory stubs.

Run:
    pytest infrastructure/src/functions/bookmarks/tests/test_handler_unit.py -v

Test areas:
  A. GET /v1/bookmarks — block-filtered bookmarks list
  B. POST /v1/bookmarks — block check + idempotent upsert
  C. DELETE /v1/bookmarks/{userId} — idempotent remove
  D. Route dispatch / edge-secret gate
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import uuid
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module import helper
# ---------------------------------------------------------------------------

_HANDLER_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
)


def _import_handler():
    """Import the bookmarks handler module fresh on every call."""
    spec = importlib.util.spec_from_file_location(
        "bookmarks_handler_" + str(id(object())),
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

_ENV_DEFAULTS = {
    "EDGE_SECRET": _EDGE_SECRET,
    "DB_SECRET_NAME": "test-secret",
}


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
                        # story 7.0b: bookmarks endpoint requires completed profile
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


def _make_conn(fetchone_return=None, fetchall_return=None):
    """Build a minimal psycopg2 connection stub."""
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value = cur

    cur.fetchone.return_value = fetchone_return
    cur.fetchall.return_value = fetchall_return if fetchall_return is not None else []
    cur.description = []

    return conn, cur


# ---------------------------------------------------------------------------
# Area A: GET /v1/bookmarks
# ---------------------------------------------------------------------------


class TestGetBookmarks:
    """GET /v1/bookmarks — returns the block-filtered bookmark list."""

    def test_given_no_bookmarks_when_get_then_returns_empty_list(self):
        mod = _import_handler()
        conn, cur = _make_conn(fetchall_return=[])

        event = _make_event("GET", "/v1/bookmarks", user_sub=_USER_A)

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert body == {"bookmarks": []}

    def test_given_bookmarked_users_when_get_then_returns_list(self):
        mod = _import_handler()
        conn, cur = _make_conn()
        bookmarked_id = str(uuid.uuid4())
        cur.fetchall.return_value = [
            (bookmarked_id, "Test", "User", "Female", None, None, None, None, None)
        ]
        cur.description = [
            ("user_id",), ("first_name",), ("last_name",), ("sex",),
            ("age",), ("photo_url",), ("username",), ("job_title",),
            ("current_residence_city",),
        ]

        event = _make_event("GET", "/v1/bookmarks", user_sub=_USER_A)

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert len(body["bookmarks"]) == 1
        assert body["bookmarks"][0]["user_id"] == bookmarked_id

    def test_given_get_bookmarks_then_block_filter_applied_to_query(self):
        """
        GET /v1/bookmarks must embed block_filter("bk.bookmarked_user_id")
        in its SQL so bookmarked users who later blocked the requester (or vice versa)
        are silently excluded.
        """
        mod = _import_handler()
        conn, cur = _make_conn(fetchall_return=[])

        event = _make_event("GET", "/v1/bookmarks", user_sub=_USER_A)

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.object(
                mod,
                "block_filter",
                return_value="NOT EXISTS (SELECT 1 FROM blocks b WHERE 1=1)",
            ),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            mod.handler(event, None)

        assert cur.execute.called, "cur.execute must be called for GET /v1/bookmarks"
        all_sql = " ".join(str(c) for c in cur.execute.call_args_list)
        assert "NOT EXISTS" in all_sql, (
            "GET /v1/bookmarks SELECT must embed the block filter fragment"
        )

    def test_given_db_error_on_get_bookmarks_then_module_level_conn_is_cleared(self):
        """
        When a DB error occurs inside _handle_get_bookmarks, the module-level
        _conn cache must be reset to None so the broken connection is not reused
        on subsequent warm-container invocations.
        """
        mod = _import_handler()
        conn, cur = _make_conn(fetchall_return=[])

        def raise_on_execute(sql, params=None):
            raise Exception("simulated DB error")

        cur.execute.side_effect = raise_on_execute
        mod._conn = conn

        event = _make_event("GET", "/v1/bookmarks", user_sub=_USER_A)

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            with pytest.raises(Exception, match="simulated DB error"):
                mod._dispatch(event, _USER_A, "Male")

        assert mod._conn is None, (
            "_handle_get_bookmarks must reset module-level _conn to None after a DB error"
        )


# ---------------------------------------------------------------------------
# Area B: POST /v1/bookmarks
# ---------------------------------------------------------------------------


class TestPostBookmarks:
    """POST /v1/bookmarks — block check + idempotent upsert."""

    def test_given_missing_body_when_post_then_returns_400(self):
        mod = _import_handler()
        conn, cur = _make_conn()

        event = _make_event("POST", "/v1/bookmarks", user_sub=_USER_A)

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 400

    def test_given_missing_userId_field_when_post_then_returns_400(self):
        mod = _import_handler()
        conn, cur = _make_conn()

        event = _make_event("POST", "/v1/bookmarks", body={}, user_sub=_USER_A)

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 400

    def test_given_blocked_pair_when_post_then_returns_409_blocked(self):
        """
        When is_blocked() returns True, POST /v1/bookmarks must return
        HTTP 409 with {"error": "blocked"} and NOT insert any row.
        """
        mod = _import_handler()
        conn, cur = _make_conn()

        event = _make_event(
            "POST", "/v1/bookmarks",
            body={"userId": _USER_B},
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
        into bookmarks.
        """
        mod = _import_handler()
        conn, cur = _make_conn()

        event = _make_event(
            "POST", "/v1/bookmarks",
            body={"userId": _USER_B},
            user_sub=_USER_A,
        )

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=True),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            mod.handler(event, None)

        all_sql = " ".join(str(c) for c in cur.execute.call_args_list)
        assert "INSERT INTO bookmarks" not in all_sql

    def test_given_is_blocked_called_inside_transaction(self):
        """
        is_blocked() must be called with the same connection as the INSERT so
        the block check and INSERT share the same transaction (prevents races).
        """
        mod = _import_handler()
        conn, cur = _make_conn()

        captured_conn: list = []

        def fake_is_blocked(c, a, b):
            captured_conn.append(c)
            return False

        # INSERT ... ON CONFLICT DO NOTHING RETURNING * — simulate empty RETURNING
        cur.fetchone.return_value = None

        event = _make_event(
            "POST", "/v1/bookmarks",
            body={"userId": _USER_B},
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

    def test_given_new_bookmark_when_post_then_returns_200(self):
        """
        A valid, unblocked POST /v1/bookmarks must return HTTP 200 (not 201 —
        POST is idempotent; 200 on both first and second create).
        """
        mod = _import_handler()
        conn, cur = _make_conn()
        # Simulate INSERT RETURNING * with a new row
        cur.fetchone.return_value = (_USER_A, _USER_B, "2026-01-01T00:00:00")
        cur.description = [("user_id",), ("bookmarked_user_id",), ("created_at",)]

        event = _make_event(
            "POST", "/v1/bookmarks",
            body={"userId": _USER_B},
            user_sub=_USER_A,
        )

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 200

    def test_given_duplicate_bookmark_when_post_then_returns_200_not_409(self):
        """
        A second POST with the same userId must return HTTP 200 (idempotent ON
        CONFLICT DO NOTHING — empty RETURNING is treated as success).
        """
        mod = _import_handler()
        conn, cur = _make_conn()
        # Simulate ON CONFLICT DO NOTHING — RETURNING * yields nothing
        cur.fetchone.return_value = None

        event = _make_event(
            "POST", "/v1/bookmarks",
            body={"userId": _USER_B},
            user_sub=_USER_A,
        )

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        # Should indicate success (bookmarked: true or similar)
        assert body.get("bookmarked") is True or "bookmarked_user_id" in body

    def test_given_post_bookmark_then_uses_on_conflict_do_nothing(self):
        """
        The INSERT statement must use ON CONFLICT (user_id, bookmarked_user_id)
        DO NOTHING RETURNING * — idempotent semantics per PRD.
        """
        mod = _import_handler()
        conn, cur = _make_conn()
        cur.fetchone.return_value = None

        executed_sqls: list[str] = []

        def capture_execute(sql, params=None):
            executed_sqls.append(str(sql))

        cur.execute.side_effect = capture_execute

        event = _make_event(
            "POST", "/v1/bookmarks",
            body={"userId": _USER_B},
            user_sub=_USER_A,
        )

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            mod.handler(event, None)

        all_sql = " ".join(executed_sqls)
        assert "ON CONFLICT" in all_sql and "DO NOTHING" in all_sql, (
            "POST /v1/bookmarks must use ON CONFLICT ... DO NOTHING for idempotency"
        )

    def test_given_db_error_on_post_bookmarks_then_module_level_conn_is_cleared(self):
        """
        When a DB error occurs inside _handle_post_bookmark, the module-level
        _conn cache must be reset to None.
        """
        mod = _import_handler()
        conn, cur = _make_conn()

        def raise_on_execute(sql, params=None):
            raise Exception("simulated DB error")

        cur.execute.side_effect = raise_on_execute
        mod._conn = conn

        event = _make_event(
            "POST", "/v1/bookmarks",
            body={"userId": _USER_B},
            user_sub=_USER_A,
        )

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            with pytest.raises(Exception, match="simulated DB error"):
                mod._dispatch(event, _USER_A, "Male")

        assert mod._conn is None, (
            "_handle_post_bookmark must reset module-level _conn to None after a DB error"
        )


# ---------------------------------------------------------------------------
# Area C: DELETE /v1/bookmarks/{userId}
# ---------------------------------------------------------------------------


class TestDeleteBookmark:
    """DELETE /v1/bookmarks/{userId} — idempotent remove."""

    def test_given_existing_bookmark_when_delete_then_returns_200(self):
        mod = _import_handler()
        conn, cur = _make_conn()
        cur.rowcount = 1

        event = _make_event(
            "DELETE",
            f"/v1/bookmarks/{_USER_B}",
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

    def test_given_non_existent_bookmark_when_delete_then_returns_200(self):
        """
        DELETE /v1/bookmarks/{userId} is idempotent — returns 200 even when
        no row was deleted (rowcount == 0).
        """
        mod = _import_handler()
        conn, cur = _make_conn()
        cur.rowcount = 0

        event = _make_event(
            "DELETE",
            f"/v1/bookmarks/{_USER_B}",
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

    def test_given_delete_bookmark_then_deletes_from_bookmarks_table(self):
        """
        DELETE must execute a DELETE FROM bookmarks using user_id (caller) and
        bookmarked_user_id (path param).
        """
        mod = _import_handler()
        conn, cur = _make_conn()

        executed_sqls: list[str] = []

        def capture_execute(sql, params=None):
            executed_sqls.append(str(sql))

        cur.execute.side_effect = capture_execute

        event = _make_event(
            "DELETE",
            f"/v1/bookmarks/{_USER_B}",
            path_params={"userId": _USER_B},
            user_sub=_USER_A,
        )

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            mod.handler(event, None)

        all_sql = " ".join(executed_sqls)
        assert "DELETE FROM bookmarks" in all_sql, (
            "DELETE /v1/bookmarks/{userId} must DELETE FROM bookmarks"
        )

    def test_given_missing_userId_path_param_when_delete_then_returns_400_or_404(self):
        """
        DELETE /v1/bookmarks/ with no userId in the path returns 400 or 404.
        """
        mod = _import_handler()
        conn, cur = _make_conn()

        event = _make_event("DELETE", "/v1/bookmarks/", user_sub=_USER_A)

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] in (400, 404)

    def test_given_db_error_on_delete_bookmark_then_module_level_conn_is_cleared(self):
        """
        When _handle_delete_bookmark raises a DB exception, the module-level _conn
        cache must be reset to None so the broken connection is not reused on
        subsequent warm-container invocations.
        """
        mod = _import_handler()
        conn, cur = _make_conn()

        def raise_on_execute(sql, params=None):
            raise Exception("simulated DB error")

        cur.execute.side_effect = raise_on_execute
        mod._conn = conn

        event = _make_event(
            "DELETE",
            f"/v1/bookmarks/{_USER_B}",
            path_params={"userId": _USER_B},
            user_sub=_USER_A,
        )

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            with pytest.raises(Exception, match="simulated DB error"):
                mod._dispatch(event, _USER_A, "Male")

        assert mod._conn is None, (
            "_handle_delete_bookmark must reset module-level _conn to None after a DB error"
        )


# ---------------------------------------------------------------------------
# Area D: Route dispatch / edge-secret gate
# ---------------------------------------------------------------------------


class TestRouteDispatch:
    """
    The handler must route requests by HTTP method + path and enforce the
    edge secret header via the @with_edge_secret decorator.
    """

    def test_given_wrong_edge_secret_then_returns_403(self):
        mod = _import_handler()
        event = _make_event("GET", "/v1/bookmarks", edge_secret="wrong-secret")

        with patch.dict(os.environ, _ENV_DEFAULTS):
            response = mod.handler(event, None)

        assert response["statusCode"] == 403

    def test_given_unmatched_route_then_returns_404(self):
        mod = _import_handler()
        conn, cur = _make_conn()

        event = _make_event("PUT", "/v1/bookmarks/something", user_sub=_USER_A)

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
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

        event = {
            "requestContext": {"http": {"method": "GET", "path": "/v1/bookmarks"}},
            "headers": {"x-knotify-edge-secret": _EDGE_SECRET},
        }

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 403
        body = json.loads(response["body"])
        assert body["error"] == "profile_incomplete"

    def test_get_bookmarks_route_dispatches_correctly(self):
        mod = _import_handler()
        conn, cur = _make_conn(fetchall_return=[])

        event = _make_event("GET", "/v1/bookmarks", user_sub=_USER_A)

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 200

    def test_post_bookmarks_route_dispatches_correctly(self):
        mod = _import_handler()
        conn, cur = _make_conn()
        cur.fetchone.return_value = (_USER_A, _USER_B, "2026-01-01T00:00:00")
        cur.description = [("user_id",), ("bookmarked_user_id",), ("created_at",)]

        event = _make_event(
            "POST", "/v1/bookmarks",
            body={"userId": _USER_B},
            user_sub=_USER_A,
        )

        with (
            patch.object(mod, "_get_conn", return_value=conn),
            patch.object(mod, "is_blocked", return_value=False),
            patch.dict(os.environ, _ENV_DEFAULTS),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 200

    def test_delete_bookmark_route_dispatches_correctly(self):
        mod = _import_handler()
        conn, cur = _make_conn()
        cur.rowcount = 1

        event = _make_event(
            "DELETE",
            f"/v1/bookmarks/{_USER_B}",
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
