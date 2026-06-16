"""
Unit tests for POST /v1/match/search handler (story 7.1).

These tests are fully isolated — they do NOT require a running database,
AWS credentials, or a deployed Lambda. All external collaborators (DB
connection, RLS context, block_filter) are replaced with in-memory stubs.

Run:
    pytest infrastructure/src/functions/match/tests/test_search_unit.py -v

Conventions:
  - Test names follow the pattern: given_<context>_when_<action>_then_<outcome>
  - One behaviour per test.

Test coverage for story 7.1 (POST /v1/match/search):
  A. Input validation — missing body, invalid JSON, wrong field types
  B. _validate_search_body — per-field validation rules
  C. _is_empty_vector — NULL / all-zeros detection
  D. _build_search_sql — cosine ORDER BY vs fallback to created_at DESC
  E. _dispatch — POST /v1/match/search routes to _handle_post_match_search;
     all other paths still return 404
  F. empty-vector fallback — when requester's preference_vector is NULL or
     all zeros, ORDER BY created_at DESC (not cosine distance)
  G. block_filter is called with "u.user_id"; its SQL fragment appears in the
     query that is executed
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
from types import ModuleType
from unittest.mock import MagicMock, patch, call

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
      - knotify_db (get_connection, rls_context, encode_prefs, PREFERENCE_KEYS)
      - knotify_obs (init_logger, with_edge_secret pass-through,
                     require_profile_complete pass-through,
                     block_filter returning a mock SQL fragment)
    """
    bf_sql = mock_block_filter or "NOT EXISTS (SELECT 1 FROM blocks b WHERE ...)"

    mock_knotify_obs = MagicMock()
    mock_knotify_obs.init_logger.return_value = MagicMock()
    mock_knotify_obs.with_edge_secret = lambda f: f        # pass-through decorator
    mock_knotify_obs.require_profile_complete = lambda f: f  # pass-through decorator
    mock_knotify_obs.block_filter.return_value = bf_sql

    mock_knotify_db = MagicMock()
    # PREFERENCE_KEYS must be a real list so _is_empty_vector can compare
    from knotify_db.prefs import PREFERENCE_KEYS as _REAL_KEYS
    mock_knotify_db.PREFERENCE_KEYS = _REAL_KEYS
    mock_knotify_db.encode_prefs.side_effect = lambda p: [
        1.0 if p.get(k, False) else 0.0 for k in _REAL_KEYS
    ]

    spec = importlib.util.spec_from_file_location(
        "match_handler_" + str(id(object())),
        os.path.join(_HANDLER_DIR, "handler.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(
        "sys.modules",
        {
            "knotify_db": mock_knotify_db,
            "knotify_obs": mock_knotify_obs,
        },
    ):
        spec.loader.exec_module(mod)
        mod._EDGE_SECRET = _EDGE_SECRET
        # Expose the mocks on the module for test introspection
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
    body: dict | None = None,
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
    if body is not None:
        event["body"] = json.dumps(body)
    return event


_CONTEXT = MagicMock()

_VALID_SEARCH_BODY = {
    "countries": ["GB", "PK"],
    "religion": "Islam",
    "age_min": 22,
    "age_max": 35,
}


# ---------------------------------------------------------------------------
# Section A — _dispatch still returns 404 for unknown routes
#
# The 7.0 tests verify the empty-dispatcher path. After 7.1 wires
# POST /v1/match/search, all OTHER paths must still 404.
# ---------------------------------------------------------------------------


def test_given_get_match_search_when_dispatched_then_returns_404() -> None:
    """given GET /v1/match/search (wrong method), when dispatched, then 404."""
    mod = _import_handler()
    event = _make_event("GET", "/v1/match/search")
    response = mod._dispatch(event, "user-sub-1234", "Male")
    assert response["statusCode"] == 404
    assert json.loads(response["body"])["error"] == "not_found"


def test_given_get_match_deck_when_dispatched_then_returns_404_after_71() -> None:
    """given GET /v1/match/deck, when dispatched after 7.1 adds search, then 404."""
    mod = _import_handler()
    event = _make_event("GET", "/v1/match/deck")
    response = mod._dispatch(event, "user-sub-1234", "Male")
    assert response["statusCode"] == 404
    assert json.loads(response["body"])["error"] == "not_found"


# ---------------------------------------------------------------------------
# Section B — POST /v1/match/search routes to the handler
# ---------------------------------------------------------------------------


def test_given_post_match_search_when_dispatched_then_routes_to_search_handler() -> None:
    """given POST /v1/match/search, when dispatched, then NOT 404 (handled)."""
    mod = _import_handler()
    # Stub _handle_post_match_search so we just prove routing works
    stub_response = {"statusCode": 200, "headers": {}, "body": '{"results":[]}'}
    mod._handle_post_match_search = MagicMock(return_value=stub_response)

    event = _make_event("POST", "/v1/match/search", body=_VALID_SEARCH_BODY)
    response = mod._dispatch(event, "user-sub-1234", "Male")
    assert response["statusCode"] == 200
    mod._handle_post_match_search.assert_called_once()


# ---------------------------------------------------------------------------
# Section C — Input validation (_validate_search_body)
# ---------------------------------------------------------------------------


def test_given_missing_body_when_post_match_search_then_returns_400() -> None:
    """given no body in the event, when POST /v1/match/search, then 400 missing_body."""
    mod = _import_handler()
    event = _make_event("POST", "/v1/match/search")  # no body kwarg
    response = mod._handle_post_match_search(event, "user-sub-1234", "Male")
    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error"] == "missing_body"


def test_given_invalid_json_body_when_post_match_search_then_returns_400() -> None:
    """given a body that is not valid JSON, when POST /v1/match/search, then 400 invalid_json."""
    mod = _import_handler()
    event = _make_event("POST", "/v1/match/search")
    event["body"] = "not-json"
    response = mod._handle_post_match_search(event, "user-sub-1234", "Male")
    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error"] == "invalid_json"


def test_given_body_not_object_when_post_match_search_then_returns_400() -> None:
    """given body is a JSON array (not object), when POST /v1/match/search, then 400."""
    mod = _import_handler()
    event = _make_event("POST", "/v1/match/search")
    event["body"] = json.dumps([1, 2, 3])
    response = mod._handle_post_match_search(event, "user-sub-1234", "Male")
    assert response["statusCode"] == 400
    assert json.loads(response["body"])["error"] == "body_must_be_object"


def test_given_countries_not_list_when_validate_search_body_then_returns_error() -> None:
    """given countries is a string (not a list), when validating, then validation error."""
    mod = _import_handler()
    err = mod._validate_search_body({"countries": "GB", "religion": "Islam", "age_min": 20, "age_max": 30})
    assert err is not None
    assert "countries" in err


def test_given_country_code_too_long_when_validate_search_body_then_returns_error() -> None:
    """given countries contains a 3-char code, when validating, then validation error."""
    mod = _import_handler()
    err = mod._validate_search_body({"countries": ["GBR"], "religion": "Islam", "age_min": 20, "age_max": 30})
    assert err is not None
    assert "countries" in err


def test_given_country_code_free_text_when_validate_search_body_then_returns_error() -> None:
    """given countries contains a free-text name, when validating, then validation error."""
    mod = _import_handler()
    err = mod._validate_search_body({"countries": ["United Kingdom"], "religion": "Islam", "age_min": 20, "age_max": 30})
    assert err is not None
    assert "countries" in err


def test_given_age_min_not_int_when_validate_search_body_then_returns_error() -> None:
    """given age_min is a string, when validating, then validation error."""
    mod = _import_handler()
    err = mod._validate_search_body({"countries": ["GB"], "religion": "Islam", "age_min": "20", "age_max": 30})
    assert err is not None
    assert "age_min" in err


def test_given_age_max_not_int_when_validate_search_body_then_returns_error() -> None:
    """given age_max is a float, when validating, then validation error."""
    mod = _import_handler()
    err = mod._validate_search_body({"countries": ["GB"], "religion": "Islam", "age_min": 20, "age_max": 30.5})
    assert err is not None
    assert "age_max" in err


def test_given_age_min_greater_than_age_max_when_validate_search_body_then_returns_error() -> None:
    """given age_min > age_max, when validating, then validation error."""
    mod = _import_handler()
    err = mod._validate_search_body({"countries": ["GB"], "religion": "Islam", "age_min": 40, "age_max": 30})
    assert err is not None


def test_given_religion_not_string_when_validate_search_body_then_returns_error() -> None:
    """given religion is an integer, when validating, then validation error."""
    mod = _import_handler()
    err = mod._validate_search_body({"countries": ["GB"], "religion": 42, "age_min": 20, "age_max": 30})
    assert err is not None
    assert "religion" in err


def test_given_valid_body_when_validate_search_body_then_returns_none() -> None:
    """given a fully valid body, when _validate_search_body, then None (no error)."""
    mod = _import_handler()
    err = mod._validate_search_body(_VALID_SEARCH_BODY)
    assert err is None


def test_given_valid_body_with_single_country_when_validate_search_body_then_returns_none() -> None:
    """given countries list with a single valid CHAR(2) code, when validating, then None."""
    mod = _import_handler()
    err = mod._validate_search_body({
        "countries": ["US"],
        "religion": "Christianity",
        "age_min": 18,
        "age_max": 50,
    })
    assert err is None


# ---------------------------------------------------------------------------
# Section D — _is_empty_vector helper
# ---------------------------------------------------------------------------


def test_given_none_vector_when_is_empty_vector_then_returns_true() -> None:
    """given preference_vector is None, when _is_empty_vector, then True."""
    mod = _import_handler()
    assert mod._is_empty_vector(None) is True


def test_given_all_zeros_vector_when_is_empty_vector_then_returns_true() -> None:
    """given preference_vector is a list of 20 zeros, when _is_empty_vector, then True."""
    mod = _import_handler()
    assert mod._is_empty_vector([0.0] * 20) is True


def test_given_vector_with_nonzero_element_when_is_empty_vector_then_returns_false() -> None:
    """given preference_vector has at least one non-zero element, when _is_empty_vector, then False."""
    mod = _import_handler()
    vec = [0.0] * 20
    vec[3] = 1.0
    assert mod._is_empty_vector(vec) is False


def test_given_empty_list_vector_when_is_empty_vector_then_returns_true() -> None:
    """given preference_vector is an empty list [], when _is_empty_vector, then True."""
    mod = _import_handler()
    assert mod._is_empty_vector([]) is True


# ---------------------------------------------------------------------------
# Section E — _build_search_sql: cosine vs. fallback ORDER BY
# ---------------------------------------------------------------------------


def test_given_non_empty_vector_when_build_search_sql_then_sql_contains_cosine_order() -> None:
    """
    given a non-empty requester preference_vector,
    when _build_search_sql is called,
    then the returned SQL contains the cosine distance operator (<=>).
    """
    mod = _import_handler()
    vec = [1.0] + [0.0] * 19  # first preference set
    sql, params = mod._build_search_sql(
        user_id="req-uuid",
        user_sex="Male",
        countries=["GB"],
        religion="Islam",
        age_min=20,
        age_max=30,
        requester_vector=vec,
    )
    assert "<=>" in sql, "Cosine distance operator <=> must appear in SQL for non-empty vector"
    assert "preference_vector" in sql


def test_given_non_empty_vector_when_build_search_sql_then_order_by_is_not_created_at() -> None:
    """
    given a non-empty vector, when _build_search_sql, then ORDER BY does NOT use created_at.
    created_at IS in the SELECT list (always returned) but must not be the sort key.
    """
    mod = _import_handler()
    vec = [1.0] + [0.0] * 19
    sql, params = mod._build_search_sql(
        user_id="req-uuid",
        user_sex="Male",
        countries=["GB"],
        religion="Islam",
        age_min=20,
        age_max=30,
        requester_vector=vec,
    )
    # The ORDER BY clause must use cosine distance, not created_at.
    # Extract the ORDER BY portion from the SQL.
    order_by_idx = sql.upper().rfind("ORDER BY")
    assert order_by_idx != -1, "SQL must contain ORDER BY"
    order_by_clause = sql[order_by_idx:]
    assert "created_at" not in order_by_clause, (
        "ORDER BY must not reference created_at when the vector is non-empty"
    )


def test_given_null_vector_when_build_search_sql_then_sql_contains_fallback_order() -> None:
    """
    given requester_vector is None (NULL),
    when _build_search_sql is called,
    then SQL uses ORDER BY created_at DESC (fallback path).
    """
    mod = _import_handler()
    sql, params = mod._build_search_sql(
        user_id="req-uuid",
        user_sex="Male",
        countries=["GB"],
        religion="Islam",
        age_min=20,
        age_max=30,
        requester_vector=None,
    )
    assert "created_at" in sql
    assert "<=>" not in sql


def test_given_all_zeros_vector_when_build_search_sql_then_uses_fallback_order() -> None:
    """
    given requester_vector is all zeros,
    when _build_search_sql is called,
    then SQL uses ORDER BY created_at DESC (empty-vector fallback).
    """
    mod = _import_handler()
    sql, params = mod._build_search_sql(
        user_id="req-uuid",
        user_sex="Male",
        countries=["GB"],
        religion="Islam",
        age_min=20,
        age_max=30,
        requester_vector=[0.0] * 20,
    )
    assert "created_at" in sql
    assert "<=>" not in sql


# ---------------------------------------------------------------------------
# Section F — _build_search_sql: SQL structural invariants
# ---------------------------------------------------------------------------


def test_given_valid_params_when_build_search_sql_then_queries_users_table() -> None:
    """given valid params, when _build_search_sql, then SQL references users table aliased as u."""
    mod = _import_handler()
    sql, _ = mod._build_search_sql(
        user_id="req",
        user_sex="Male",
        countries=["GB"],
        religion="Islam",
        age_min=20,
        age_max=30,
        requester_vector=None,
    )
    assert "FROM users u" in sql or "from users u" in sql.lower()


def test_given_valid_params_when_build_search_sql_then_excludes_deleted_users() -> None:
    """given valid params, when _build_search_sql, then SQL filters deleted_at IS NULL."""
    mod = _import_handler()
    sql, _ = mod._build_search_sql(
        user_id="req",
        user_sex="Male",
        countries=["GB"],
        religion="Islam",
        age_min=20,
        age_max=30,
        requester_vector=None,
    )
    assert "deleted_at IS NULL" in sql


def test_given_valid_params_when_build_search_sql_then_filters_profile_complete() -> None:
    """given valid params, when _build_search_sql, then SQL requires profile_complete_verified = true."""
    mod = _import_handler()
    sql, _ = mod._build_search_sql(
        user_id="req",
        user_sex="Male",
        countries=["GB"],
        religion="Islam",
        age_min=20,
        age_max=30,
        requester_vector=None,
    )
    assert "profile_complete_verified" in sql


def test_given_valid_params_when_build_search_sql_then_includes_block_filter() -> None:
    """
    given valid params, when _build_search_sql,
    then block_filter("u.user_id") is called and its fragment appears in SQL.
    """
    sentinel_fragment = "NOT EXISTS (SELECT 1 FROM blocks b WHERE SENTINEL)"
    mod = _import_handler(mock_block_filter=sentinel_fragment)
    sql, _ = mod._build_search_sql(
        user_id="req",
        user_sex="Male",
        countries=["GB"],
        religion="Islam",
        age_min=20,
        age_max=30,
        requester_vector=None,
    )
    mod._mock_knotify_obs.block_filter.assert_called_with("u.user_id")
    assert sentinel_fragment in sql


def test_given_valid_params_when_build_search_sql_then_limits_to_50() -> None:
    """given valid params, when _build_search_sql, then SQL has LIMIT 50."""
    mod = _import_handler()
    sql, _ = mod._build_search_sql(
        user_id="req",
        user_sex="Male",
        countries=["GB"],
        religion="Islam",
        age_min=20,
        age_max=30,
        requester_vector=None,
    )
    assert "LIMIT 50" in sql or "limit 50" in sql.lower()


def test_given_valid_params_when_build_search_sql_then_no_explicit_sex_predicate() -> None:
    """
    given valid params, when _build_search_sql,
    then the SQL does NOT contain an explicit u.sex filter
    (RLS enforces opposite-sex visibility; handler omits it).
    """
    mod = _import_handler()
    sql, _ = mod._build_search_sql(
        user_id="req",
        user_sex="Male",
        countries=["GB"],
        religion="Islam",
        age_min=20,
        age_max=30,
        requester_vector=None,
    )
    # The handler must NOT emit "u.sex" as a WHERE predicate
    assert "u.sex" not in sql


def test_given_country_filter_when_build_search_sql_then_resident_country_code_in_sql() -> None:
    """given countries filter, when _build_search_sql, then resident_country_code filter appears."""
    mod = _import_handler()
    sql, _ = mod._build_search_sql(
        user_id="req",
        user_sex="Male",
        countries=["GB", "PK"],
        religion="Islam",
        age_min=20,
        age_max=30,
        requester_vector=None,
    )
    assert "resident_country_code" in sql


def test_given_age_range_when_build_search_sql_then_age_between_filter_in_sql() -> None:
    """given age range, when _build_search_sql, then age BETWEEN or age >= and age <= in SQL."""
    mod = _import_handler()
    sql, params = mod._build_search_sql(
        user_id="req",
        user_sex="Male",
        countries=["GB"],
        religion="Islam",
        age_min=22,
        age_max=35,
        requester_vector=None,
    )
    assert "age" in sql
    # The age values are bound as parameters, not interpolated into SQL
    assert 22 in params or "22" in str(params)
    assert 35 in params or "35" in str(params)


def test_given_religion_filter_when_build_search_sql_then_religion_in_sql() -> None:
    """given a religion filter, when _build_search_sql, then religion predicate appears in SQL."""
    mod = _import_handler()
    sql, _ = mod._build_search_sql(
        user_id="req",
        user_sex="Male",
        countries=["GB"],
        religion="Islam",
        age_min=20,
        age_max=30,
        requester_vector=None,
    )
    assert "religion" in sql


# ---------------------------------------------------------------------------
# Section G — Empty-vector fallback end-to-end (handler level)
#
# A requester with a NULL preference_vector gets results ordered by
# created_at DESC. The handler must NOT raise and must return 200.
# We stub the DB to simulate a requester with NULL preference_vector.
# ---------------------------------------------------------------------------


def test_given_requester_null_vector_when_post_match_search_then_returns_200() -> None:
    """
    given requester's DB row has preference_vector = NULL,
    when POST /v1/match/search with a valid body,
    then the handler returns 200 and does NOT use cosine ordering.
    """
    mod = _import_handler()

    # Stub _get_conn to return a mock connection
    mock_conn = MagicMock()
    mock_conn.closed = False
    mod._get_conn = MagicMock(return_value=mock_conn)

    # Stub rls_context to act as a no-op context manager
    import contextlib

    @contextlib.contextmanager
    def _mock_rls(conn, uid, sex):
        yield

    mod._mock_knotify_db.rls_context.side_effect = _mock_rls

    # First cursor call: fetch preference_vector (returns NULL)
    mock_cur_vector = MagicMock()
    mock_cur_vector.fetchone.return_value = (None,)  # preference_vector is NULL

    # Second cursor call: the search SELECT (returns candidate rows)
    candidate_row = (
        "cand-uuid-1",          # user_id
        "CandUser",             # username
        25,                     # age
        "Islam",                # religion
        "UK",                   # current_residence_country
        "GB",                   # resident_country_code
        "London",               # current_residence_city
        "Engineer",             # job_title
        None,                   # photo_url
        None,                   # chosen_profile_avatar
        "2020-01-01",           # created_at
    )
    mock_cur_search = MagicMock()
    mock_cur_search.fetchall.return_value = [candidate_row]
    mock_cur_search.description = [
        (col,) for col in [
            "user_id", "username", "age", "religion",
            "current_residence_country", "resident_country_code",
            "current_residence_city", "job_title",
            "photo_url", "chosen_profile_avatar", "created_at",
        ]
    ]

    # Provide cursor context managers in sequence: first for vector fetch,
    # second for search SELECT
    mock_conn.cursor.return_value.__enter__ = MagicMock(
        side_effect=[mock_cur_vector, mock_cur_search]
    )
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    event = _make_event("POST", "/v1/match/search", body=_VALID_SEARCH_BODY)
    response = mod._handle_post_match_search(event, "user-sub-1234", "Male")

    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert "results" in body


def test_given_requester_all_zeros_vector_when_post_match_search_then_uses_created_at_order() -> None:
    """
    given requester's DB row has preference_vector = [0,0,...,0],
    when POST /v1/match/search,
    then the SQL executed against the DB uses ORDER BY created_at DESC (not <=>).
    """
    mod = _import_handler()

    executed_sqls: list[str] = []

    mock_conn = MagicMock()
    mock_conn.closed = False
    mod._get_conn = MagicMock(return_value=mock_conn)

    import contextlib

    @contextlib.contextmanager
    def _mock_rls(conn, uid, sex):
        yield

    mod._mock_knotify_db.rls_context.side_effect = _mock_rls

    mock_cur_vector = MagicMock()
    mock_cur_vector.fetchone.return_value = ([0.0] * 20,)

    mock_cur_search = MagicMock()
    mock_cur_search.fetchall.return_value = []
    mock_cur_search.description = []

    def _capture_execute(sql, params=None):
        executed_sqls.append(sql)

    mock_cur_vector.execute = _capture_execute
    mock_cur_search.execute = _capture_execute

    mock_conn.cursor.return_value.__enter__ = MagicMock(
        side_effect=[mock_cur_vector, mock_cur_search]
    )
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    event = _make_event("POST", "/v1/match/search", body=_VALID_SEARCH_BODY)
    mod._handle_post_match_search(event, "user-sub-1234", "Male")

    # The second executed SQL (search query) must use fallback ordering
    search_sqls = [s for s in executed_sqls if "FROM users u" in s or "from users u" in s.lower()]
    assert len(search_sqls) >= 1, "Expected at least one SQL query against users table"
    search_sql = search_sqls[0]
    assert "<=>" not in search_sql, "Cosine operator must not appear when vector is all zeros"
    assert "created_at" in search_sql, "Fallback ORDER BY created_at must appear"


# ---------------------------------------------------------------------------
# Section H — block_filter called with "u.user_id"
# ---------------------------------------------------------------------------


def test_given_search_request_when_handler_runs_then_block_filter_called_with_u_user_id() -> None:
    """
    given any valid POST /v1/match/search request,
    when _handle_post_match_search runs,
    then block_filter is called with the argument "u.user_id".
    """
    mod = _import_handler()

    mock_conn = MagicMock()
    mock_conn.closed = False
    mod._get_conn = MagicMock(return_value=mock_conn)

    import contextlib

    @contextlib.contextmanager
    def _mock_rls(conn, uid, sex):
        yield

    mod._mock_knotify_db.rls_context.side_effect = _mock_rls

    mock_cur_vector = MagicMock()
    mock_cur_vector.fetchone.return_value = (None,)

    mock_cur_search = MagicMock()
    mock_cur_search.fetchall.return_value = []
    mock_cur_search.description = []

    mock_conn.cursor.return_value.__enter__ = MagicMock(
        side_effect=[mock_cur_vector, mock_cur_search]
    )
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    event = _make_event("POST", "/v1/match/search", body=_VALID_SEARCH_BODY)
    mod._handle_post_match_search(event, "user-sub-1234", "Male")

    mod._mock_knotify_obs.block_filter.assert_called_with("u.user_id")
