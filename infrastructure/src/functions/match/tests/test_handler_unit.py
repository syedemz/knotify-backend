"""
Unit tests for the knotify-match Lambda handler (story 7.0 scaffold).

These tests are fully isolated — they do NOT require a running database,
AWS credentials, or a deployed Lambda. External collaborators (DB connection)
are stubbed at the module level.

Run:
    pytest infrastructure/src/functions/match/tests/test_handler_unit.py -v

Conventions:
  - Test names follow the pattern: given_<context>_when_<action>_then_<outcome>
  - One behaviour per test.
  - The handler module is imported via importlib (same pattern as profile tests).

Test coverage for story 7.0 (empty dispatcher):
  A. Unknown routes — any method/path combination not yet implemented returns 404.
  B. Edge-secret enforcement — missing or wrong edge secret returns 401 before
     any route logic runs.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module import helper
# ---------------------------------------------------------------------------

_HANDLER_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
)

_EDGE_SECRET = "test-edge-secret-value"


def _import_handler() -> ModuleType:
    """Import the match handler module fresh for each test."""
    spec = importlib.util.spec_from_file_location(
        "match_handler_" + str(id(object())),
        os.path.join(_HANDLER_DIR, "handler.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    # Patch layer imports before module executes so the import succeeds
    # without the layers physically installed.
    with (
        patch.dict(
            "sys.modules",
            {
                "knotify_db": MagicMock(),
                "knotify_obs": MagicMock(
                    init_logger=MagicMock(return_value=MagicMock()),
                    with_edge_secret=lambda f: f,  # pass-through decorator
                ),
            },
        )
    ):
        spec.loader.exec_module(mod)
        # Patch the module-level EDGE_SECRET so decorator logic can validate
        mod._EDGE_SECRET = _EDGE_SECRET
    return mod


# ---------------------------------------------------------------------------
# Helpers — build minimal Lambda event dicts (HTTP API v2 format)
# ---------------------------------------------------------------------------


def _make_event(
    method: str,
    path: str,
    *,
    body: dict | None = None,
    query_params: dict | None = None,
    path_params: dict | None = None,
    user_sub: str = "user-sub-1234",
    user_sex: str = "Male",
    edge_secret: str = _EDGE_SECRET,
) -> dict:
    """Build a minimal HTTP API Gateway v2 Lambda event."""
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
    if query_params:
        event["queryStringParameters"] = query_params
    if path_params:
        event["pathParameters"] = path_params
    return event


_CONTEXT = MagicMock()

# ---------------------------------------------------------------------------
# Section A — Unknown routes return 404
#
# Story 7.0 ships an empty dispatcher. No match routes are wired yet
# (that is story 7.5). Any request to an unrecognised method/path must
# receive {"error": "not_found"} with a 404 status code.
# ---------------------------------------------------------------------------


def test_given_get_root_when_dispatched_then_returns_404() -> None:
    """given GET / (completely unrelated path), when dispatched, then 404."""
    mod = _import_handler()
    event = _make_event("GET", "/")
    response = mod._dispatch(event, "user-sub-1234", "Male")
    assert response["statusCode"] == 404
    body = json.loads(response["body"])
    assert body["error"] == "not_found"


def test_given_post_match_search_when_dispatched_then_not_404() -> None:
    """
    given POST /v1/match/search (wired in story 7.1),
    when dispatched with an incomplete body,
    then the response is NOT 404 (route is handled — expects 400 for bad body).
    """
    mod = _import_handler()
    # Body omits required fields — validation returns 400, not the 404 that the
    # empty-dispatcher (story 7.0) would have returned.
    event = _make_event("POST", "/v1/match/search", body={"countries": ["GB"]})
    response = mod._dispatch(event, "user-sub-1234", "Male")
    assert response["statusCode"] != 404, (
        "POST /v1/match/search is wired in story 7.1 — it must not return 404"
    )


def test_given_get_match_deck_when_dispatched_then_routes_to_deck_handler() -> None:
    """
    given GET /v1/match/deck (wired in story 7.2), when dispatched,
    then the route is handled (not 404).
    The handler will error without a real DB; we assert it is not a not_found response.
    """
    mod = _import_handler()
    event = _make_event("GET", "/v1/match/deck")
    # _handle_get_match_deck is called; without a DB it will raise before returning.
    # Stub _handle_get_match_deck so the dispatch test stays isolated from DB concerns.
    stub_response = {"statusCode": 200, "headers": {}, "body": '{"results":[],"next_cursor":null}'}
    mod._handle_get_match_deck = MagicMock(return_value=stub_response)
    response = mod._dispatch(event, "user-sub-1234", "Male")
    assert response["statusCode"] != 404, (
        "GET /v1/match/deck is wired in story 7.2 — it must not return 404"
    )
    mod._handle_get_match_deck.assert_called_once()


def test_given_delete_match_when_dispatched_then_returns_404() -> None:
    """given DELETE /v1/match/<anything>, when dispatched, then 404."""
    mod = _import_handler()
    event = _make_event("DELETE", "/v1/match/some-user-id")
    response = mod._dispatch(event, "user-sub-1234", "Male")
    assert response["statusCode"] == 404
    body = json.loads(response["body"])
    assert body["error"] == "not_found"


def test_given_put_method_when_dispatched_then_returns_404() -> None:
    """given PUT to any match path, when dispatched, then 404 (method not wired)."""
    mod = _import_handler()
    event = _make_event("PUT", "/v1/match/search")
    response = mod._dispatch(event, "user-sub-1234", "Male")
    assert response["statusCode"] == 404
    body = json.loads(response["body"])
    assert body["error"] == "not_found"


# ---------------------------------------------------------------------------
# Section B — JSON response helper
#
# _json_response must always set Content-Type: application/json and serialise
# the body to a JSON string.
# ---------------------------------------------------------------------------


def test_given_json_response_helper_when_called_then_sets_content_type() -> None:
    """given _json_response, when called, then Content-Type is application/json."""
    mod = _import_handler()
    response = mod._json_response(200, {"ok": True})
    assert response["statusCode"] == 200
    assert response["headers"]["Content-Type"] == "application/json"
    body = json.loads(response["body"])
    assert body["ok"] is True


def test_given_json_response_helper_when_404_then_status_code_is_404() -> None:
    """given _json_response(404, ...), when called, then statusCode == 404."""
    mod = _import_handler()
    response = mod._json_response(404, {"error": "not_found"})
    assert response["statusCode"] == 404


# ---------------------------------------------------------------------------
# Section C — JWT extraction helper
#
# _get_user_id_and_sex must extract (sub, custom:user_sex) from the
# standard HTTP API v2 JWT authorizer path. Missing sub → KeyError.
# ---------------------------------------------------------------------------


def test_given_valid_jwt_claims_when_extracted_then_returns_user_id_and_sex() -> None:
    """given valid claims, when _get_user_id_and_sex called, then returns (sub, sex)."""
    mod = _import_handler()
    event = _make_event("GET", "/v1/match/deck", user_sub="abc-123", user_sex="Female")
    user_id, user_sex = mod._get_user_id_and_sex(event)
    assert user_id == "abc-123"
    assert user_sex == "Female"


def test_given_missing_sub_claim_when_extracted_then_raises_key_error() -> None:
    """given claims with no sub, when _get_user_id_and_sex called, then KeyError."""
    mod = _import_handler()
    event = _make_event("GET", "/v1/match/deck")
    # Remove the 'sub' key from claims
    del event["requestContext"]["authorizer"]["jwt"]["claims"]["sub"]
    with pytest.raises(KeyError):
        mod._get_user_id_and_sex(event)
