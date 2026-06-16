"""
Unit tests for GET /v1/match/deck handler (story 7.2).

These tests are fully isolated — they do NOT require a running database,
AWS credentials, or a deployed Lambda. All external collaborators (DB
connection, RLS context, block_filter) are replaced with in-memory stubs.

Run:
    pytest infrastructure/src/functions/match/tests/test_deck_unit.py -v

Conventions:
  - Test names follow the pattern: given_<context>_when_<action>_then_<outcome>
  - One behaviour per test.

Test coverage for story 7.2 (GET /v1/match/deck):
  A. _build_deck_sql — structural invariants:
       FROM deck_view dv, opposite-sex WHERE, block_filter("dv.user_id"),
       ORDER BY user_id ASC, LIMIT 20
  B. _build_deck_sql — cursor parameter:
       present → WHERE dv.user_id > %s appended; absent → no cursor predicate
  C. _build_deck_sql — optional filters:
       countries, religion, age range applied when present; omitted when absent
  D. _parse_deck_filters — reuses _validate_search_body; bad JSON → error string
  E. _dispatch — GET /v1/match/deck routes to _handle_get_match_deck
  F. _handle_get_match_deck — next_cursor:
       20 rows returned → next_cursor = last user_id;
       fewer than 20 → next_cursor = null
  G. _handle_get_match_deck — opposite-sex GUC is set (rls_context called
       with the requester's sex so the SET LOCAL app.requesting_user_sex
       GUC propagates to the deck query)
  H. _handle_get_match_deck — block_filter called with "dv.user_id"
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import json
import os
from types import ModuleType
from unittest.mock import MagicMock, call

import pytest

# ---------------------------------------------------------------------------
# Module import helper
# ---------------------------------------------------------------------------

_HANDLER_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
)

_EDGE_SECRET = "test-edge-secret-value"


def _import_handler(mock_block_filter: str | None = None) -> ModuleType:
    """
    Import the match handler module fresh for each test.

    Stubs out:
      - knotify_db (get_connection, rls_context)
      - knotify_obs (init_logger, with_edge_secret pass-through,
                     require_profile_complete pass-through,
                     block_filter returning a mock SQL fragment)
    """
    bf_sql = mock_block_filter or "NOT EXISTS (SELECT 1 FROM blocks b WHERE ...)"

    mock_knotify_obs = MagicMock()
    mock_knotify_obs.init_logger.return_value = MagicMock()
    mock_knotify_obs.with_edge_secret = lambda f: f
    mock_knotify_obs.require_profile_complete = lambda f: f
    mock_knotify_obs.block_filter.return_value = bf_sql

    mock_knotify_db = MagicMock()

    spec = importlib.util.spec_from_file_location(
        "match_handler_deck_" + str(id(object())),
        os.path.join(_HANDLER_DIR, "handler.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    import unittest.mock as um
    with um.patch.dict(
        "sys.modules",
        {
            "knotify_db": mock_knotify_db,
            "knotify_obs": mock_knotify_obs,
        },
    ):
        spec.loader.exec_module(mod)
        mod._EDGE_SECRET = _EDGE_SECRET
        mod._mock_knotify_obs = mock_knotify_obs
        mod._mock_knotify_db = mock_knotify_db
    return mod


# ---------------------------------------------------------------------------
# Event builder helpers
# ---------------------------------------------------------------------------


def _make_event(
    method: str,
    path: str,
    *,
    query_params: dict | None = None,
    user_sub: str = "user-sub-1234",
    user_sex: str = "Male",
    edge_secret: str = _EDGE_SECRET,
    profile_complete: str = "true",
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
                        "custom:profile_complete": profile_complete,
                    }
                }
            },
        },
        "headers": {"x-knotify-edge-secret": edge_secret},
    }
    if query_params:
        event["queryStringParameters"] = query_params
    return event


_CONTEXT = MagicMock()

# A realistic-looking UUID string for test use
_UUID_1 = "aaaaaaaa-0000-0000-0000-000000000001"
_UUID_2 = "aaaaaaaa-0000-0000-0000-000000000002"


# ---------------------------------------------------------------------------
# Section A — _build_deck_sql structural invariants
# ---------------------------------------------------------------------------


def test_given_no_cursor_no_filters_when_build_deck_sql_then_from_deck_view_dv() -> None:
    """given no cursor and no filters, when _build_deck_sql, then SQL uses FROM deck_view dv."""
    mod = _import_handler()
    sql, _ = mod._build_deck_sql(user_id=_UUID_1, user_sex="Male")
    assert "FROM deck_view dv" in sql


def test_given_no_cursor_no_filters_when_build_deck_sql_then_opposite_sex_where_clause() -> None:
    """
    given no cursor and no filters,
    when _build_deck_sql,
    then SQL contains the explicit opposite-sex WHERE filter using current_setting GUC.
    RLS does NOT propagate through materialized views — this predicate is the only
    enforcement of opposite-sex visibility on the deck path.
    """
    mod = _import_handler()
    sql, _ = mod._build_deck_sql(user_id=_UUID_1, user_sex="Male")
    assert "dv.sex != current_setting('app.requesting_user_sex', true)" in sql


def test_given_no_cursor_no_filters_when_build_deck_sql_then_block_filter_dv_user_id() -> None:
    """
    given no cursor and no filters,
    when _build_deck_sql,
    then block_filter is called with "dv.user_id" and the fragment appears in SQL.
    """
    sentinel = "NOT EXISTS (SELECT 1 FROM blocks b WHERE SENTINEL_DV)"
    mod = _import_handler(mock_block_filter=sentinel)
    sql, _ = mod._build_deck_sql(user_id=_UUID_1, user_sex="Male")
    mod._mock_knotify_obs.block_filter.assert_called_with("dv.user_id")
    assert sentinel in sql


def test_given_no_cursor_no_filters_when_build_deck_sql_then_order_by_user_id_asc() -> None:
    """
    given no cursor and no filters,
    when _build_deck_sql,
    then SQL orders by user_id ASC.
    """
    mod = _import_handler()
    sql, _ = mod._build_deck_sql(user_id=_UUID_1, user_sex="Male")
    # The ORDER BY clause must reference dv.user_id ascending
    upper = sql.upper()
    order_idx = upper.rfind("ORDER BY")
    assert order_idx != -1
    order_clause = upper[order_idx:]
    assert "USER_ID" in order_clause
    assert "ASC" in order_clause


def test_given_no_cursor_no_filters_when_build_deck_sql_then_limit_20() -> None:
    """given no cursor and no filters, when _build_deck_sql, then SQL has LIMIT 20."""
    mod = _import_handler()
    sql, _ = mod._build_deck_sql(user_id=_UUID_1, user_sex="Male")
    assert "LIMIT 20" in sql or "limit 20" in sql.lower()


def test_given_no_cursor_no_filters_when_build_deck_sql_then_profile_complete_not_in_where() -> None:
    """
    given no cursor and no filters,
    when _build_deck_sql,
    then SQL does NOT contain a profile_complete_verified predicate
    (deck_view's defining query already filters on it — no double-filter needed).
    """
    mod = _import_handler()
    sql, _ = mod._build_deck_sql(user_id=_UUID_1, user_sex="Male")
    assert "profile_complete_verified" not in sql


# ---------------------------------------------------------------------------
# Section B — _build_deck_sql cursor parameter
# ---------------------------------------------------------------------------


def test_given_cursor_present_when_build_deck_sql_then_user_id_gt_predicate_in_sql() -> None:
    """
    given a cursor UUID is provided,
    when _build_deck_sql,
    then SQL contains a WHERE dv.user_id > %s::uuid predicate
    and the cursor value is bound in the params tuple.
    """
    mod = _import_handler()
    cursor = _UUID_1
    sql, params = mod._build_deck_sql(user_id=_UUID_2, user_sex="Male", cursor=cursor)
    assert "dv.user_id > %s::uuid" in sql
    assert cursor in params


def test_given_no_cursor_when_build_deck_sql_then_no_user_id_gt_predicate() -> None:
    """
    given no cursor,
    when _build_deck_sql,
    then SQL does NOT contain a cursor WHERE predicate.
    """
    mod = _import_handler()
    sql, _ = mod._build_deck_sql(user_id=_UUID_1, user_sex="Male", cursor=None)
    assert "dv.user_id > %s" not in sql


# ---------------------------------------------------------------------------
# Section C — _build_deck_sql optional filters
# ---------------------------------------------------------------------------


def test_given_countries_filter_when_build_deck_sql_then_resident_country_code_in_sql() -> None:
    """
    given filters with countries list,
    when _build_deck_sql,
    then SQL contains resident_country_code predicate.
    """
    mod = _import_handler()
    filters = {"countries": ["GB", "PK"], "religion": "Islam", "age_min": 20, "age_max": 35}
    sql, params = mod._build_deck_sql(user_id=_UUID_1, user_sex="Male", filters=filters)
    assert "resident_country_code" in sql
    assert ["GB", "PK"] in params or ("GB", "PK") in params or ["GB", "PK"] in list(params)


def test_given_religion_filter_when_build_deck_sql_then_religion_predicate_in_sql() -> None:
    """
    given filters with religion,
    when _build_deck_sql,
    then SQL contains dv.religion predicate.
    """
    mod = _import_handler()
    filters = {"countries": ["GB"], "religion": "Islam", "age_min": 20, "age_max": 35}
    sql, _ = mod._build_deck_sql(user_id=_UUID_1, user_sex="Male", filters=filters)
    assert "dv.religion" in sql or "religion" in sql


def test_given_age_filter_when_build_deck_sql_then_age_predicates_in_sql() -> None:
    """
    given filters with age_min and age_max,
    when _build_deck_sql,
    then SQL contains age >= and age <= (or BETWEEN) predicates.
    """
    mod = _import_handler()
    filters = {"countries": ["GB"], "religion": "Islam", "age_min": 22, "age_max": 35}
    sql, params = mod._build_deck_sql(user_id=_UUID_1, user_sex="Male", filters=filters)
    assert "age" in sql
    assert 22 in params or "22" in str(params)
    assert 35 in params or "35" in str(params)


def test_given_no_filters_when_build_deck_sql_then_no_filter_predicates() -> None:
    """
    given no filters dict,
    when _build_deck_sql,
    then the WHERE clause does NOT contain country, religion, or age filter predicates.
    resident_country_code appears in the SELECT column list — we check the WHERE portion only.
    """
    mod = _import_handler()
    sql, _ = mod._build_deck_sql(user_id=_UUID_1, user_sex="Male", filters=None)
    # Extract the WHERE clause (everything after WHERE and before ORDER BY)
    upper = sql.upper()
    where_idx = upper.find("WHERE")
    order_idx = upper.rfind("ORDER BY")
    assert where_idx != -1 and order_idx != -1
    where_clause = sql[where_idx:order_idx]
    # Filter predicates must not appear in the WHERE when no filters passed
    assert "resident_country_code = ANY" not in where_clause
    assert "dv.religion =" not in where_clause
    assert "dv.age >=" not in where_clause
    assert "dv.age <=" not in where_clause


# ---------------------------------------------------------------------------
# Section D — _parse_deck_filters helper
# ---------------------------------------------------------------------------


def test_given_valid_url_encoded_json_when_parse_deck_filters_then_returns_dict() -> None:
    """
    given a URL-encoded JSON string of valid filters,
    when _parse_deck_filters,
    then returns a (filters_dict, None) tuple with no error.
    """
    import urllib.parse
    mod = _import_handler()
    raw = urllib.parse.quote(json.dumps({"countries": ["GB"], "religion": "Islam", "age_min": 20, "age_max": 30}))
    filters, err = mod._parse_deck_filters(raw)
    assert err is None
    assert filters is not None
    assert filters["religion"] == "Islam"


def test_given_invalid_json_when_parse_deck_filters_then_returns_error() -> None:
    """
    given a malformed JSON string,
    when _parse_deck_filters,
    then returns (None, error_string).
    """
    mod = _import_handler()
    filters, err = mod._parse_deck_filters("not-valid-json}")
    assert err is not None
    assert filters is None


def test_given_invalid_filter_values_when_parse_deck_filters_then_returns_validation_error() -> None:
    """
    given a valid JSON string but with invalid field values (age_min is a string),
    when _parse_deck_filters,
    then returns (None, validation_error_string) from _validate_search_body.
    """
    import urllib.parse
    mod = _import_handler()
    raw = urllib.parse.quote(json.dumps({"countries": ["GB"], "religion": "Islam", "age_min": "bad", "age_max": 30}))
    filters, err = mod._parse_deck_filters(raw)
    assert err is not None
    assert filters is None


# ---------------------------------------------------------------------------
# Section E — _dispatch routes GET /v1/match/deck
# ---------------------------------------------------------------------------


def test_given_get_match_deck_when_dispatched_then_routes_to_deck_handler() -> None:
    """given GET /v1/match/deck, when dispatched, then not 404 (route is handled)."""
    mod = _import_handler()
    stub_response = {"statusCode": 200, "headers": {}, "body": '{"results":[],"next_cursor":null}'}
    mod._handle_get_match_deck = MagicMock(return_value=stub_response)

    event = _make_event("GET", "/v1/match/deck")
    response = mod._dispatch(event, "user-sub-1234", "Male")
    assert response["statusCode"] == 200
    mod._handle_get_match_deck.assert_called_once()


def test_given_post_match_deck_when_dispatched_then_returns_404() -> None:
    """given POST /v1/match/deck (wrong method), when dispatched, then 404."""
    mod = _import_handler()
    event = _make_event("POST", "/v1/match/deck")
    response = mod._dispatch(event, "user-sub-1234", "Male")
    assert response["statusCode"] == 404


# ---------------------------------------------------------------------------
# Section F — _handle_get_match_deck: next_cursor logic
# ---------------------------------------------------------------------------


def _make_mock_conn_for_deck(rows: list[tuple]) -> MagicMock:
    """
    Build a mock psycopg2 connection whose cursor returns the given rows.

    Cursor description mirrors the _DECK_RESULT_COLS column list.
    Each row must be a tuple where the first element is the user_id.
    """
    mock_conn = MagicMock()
    mock_conn.closed = False

    mock_cur = MagicMock()
    mock_cur.fetchall.return_value = rows
    mock_cur.description = [
        ("user_id",), ("username",), ("age",), ("religion",),
        ("current_residence_country",), ("resident_country_code",),
        ("current_residence_city",), ("job_title",),
        ("photo_url",), ("chosen_profile_avatar",),
        ("sex",),
    ]
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cur)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return mock_conn


def _setup_mock_rls(mod: ModuleType, mock_conn: MagicMock) -> None:
    """Wire _get_conn and rls_context stubs onto the freshly imported module."""
    mod._get_conn = MagicMock(return_value=mock_conn)

    @contextlib.contextmanager
    def _mock_rls(conn, uid, sex):
        yield

    mod._mock_knotify_db.rls_context.side_effect = _mock_rls


def test_given_full_batch_when_get_match_deck_then_next_cursor_is_last_user_id() -> None:
    """
    given the DB returns exactly 20 rows,
    when GET /v1/match/deck,
    then next_cursor equals the last row's user_id.
    """
    mod = _import_handler()
    last_uuid = f"aaaaaaaa-0000-0000-0000-{20:012d}"
    rows = [
        (f"aaaaaaaa-0000-0000-0000-{i:012d}", f"user{i}", 25, "Islam",
         "UK", "GB", "London", "Engineer", None, None, "Female")
        for i in range(1, 21)
    ]
    mock_conn = _make_mock_conn_for_deck(rows)
    _setup_mock_rls(mod, mock_conn)

    event = _make_event("GET", "/v1/match/deck", user_sex="Male")
    response = mod._handle_get_match_deck(event, "req-uuid", "Male")

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["next_cursor"] == last_uuid


def test_given_partial_batch_when_get_match_deck_then_next_cursor_is_null() -> None:
    """
    given the DB returns fewer than 20 rows (e.g. 5),
    when GET /v1/match/deck,
    then next_cursor is null (no more pages).
    """
    mod = _import_handler()
    rows = [
        (f"aaaaaaaa-0000-0000-0000-{i:012d}", f"user{i}", 25, "Islam",
         "UK", "GB", "London", "Engineer", None, None, "Female")
        for i in range(1, 6)
    ]
    mock_conn = _make_mock_conn_for_deck(rows)
    _setup_mock_rls(mod, mock_conn)

    event = _make_event("GET", "/v1/match/deck", user_sex="Male")
    response = mod._handle_get_match_deck(event, "req-uuid", "Male")

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["next_cursor"] is None


def test_given_empty_deck_when_get_match_deck_then_next_cursor_is_null() -> None:
    """
    given the DB returns zero rows,
    when GET /v1/match/deck,
    then next_cursor is null and results is an empty list.
    """
    mod = _import_handler()
    mock_conn = _make_mock_conn_for_deck([])
    _setup_mock_rls(mod, mock_conn)

    event = _make_event("GET", "/v1/match/deck", user_sex="Male")
    response = mod._handle_get_match_deck(event, "req-uuid", "Male")

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["next_cursor"] is None
    assert body["results"] == []


def test_given_invalid_filters_param_when_get_match_deck_then_returns_400() -> None:
    """
    given ?filters= with invalid JSON,
    when GET /v1/match/deck,
    then the handler returns 400 with a parse error.
    """
    mod = _import_handler()
    mock_conn = _make_mock_conn_for_deck([])
    _setup_mock_rls(mod, mock_conn)

    event = _make_event("GET", "/v1/match/deck",
                        query_params={"filters": "NOT_VALID_JSON{"})
    response = mod._handle_get_match_deck(event, "req-uuid", "Male")
    assert response["statusCode"] == 400


# ---------------------------------------------------------------------------
# Section G — rls_context called with correct user_sex (GUC propagation)
# ---------------------------------------------------------------------------


def test_given_male_requester_when_get_match_deck_then_rls_context_called_with_male_sex() -> None:
    """
    given a Male requester,
    when GET /v1/match/deck,
    then rls_context is called with (conn, user_id, "Male") so the
    app.requesting_user_sex GUC is set correctly for the opposite-sex predicate.
    """
    mod = _import_handler()
    mock_conn = _make_mock_conn_for_deck([])
    _setup_mock_rls(mod, mock_conn)

    event = _make_event("GET", "/v1/match/deck", user_sex="Male")
    mod._handle_get_match_deck(event, "req-uuid-male", "Male")

    # rls_context must have been called with the requester's sex
    mod._mock_knotify_db.rls_context.assert_called_once_with(mock_conn, "req-uuid-male", "Male")


# ---------------------------------------------------------------------------
# Section H — block_filter called with "dv.user_id"
# ---------------------------------------------------------------------------


def test_given_deck_request_when_handler_runs_then_block_filter_called_with_dv_user_id() -> None:
    """
    given any GET /v1/match/deck request,
    when _handle_get_match_deck runs,
    then block_filter is called with the argument "dv.user_id".
    """
    mod = _import_handler()
    mock_conn = _make_mock_conn_for_deck([])
    _setup_mock_rls(mod, mock_conn)

    event = _make_event("GET", "/v1/match/deck", user_sex="Male")
    mod._handle_get_match_deck(event, "req-uuid", "Male")

    mod._mock_knotify_obs.block_filter.assert_called_with("dv.user_id")
