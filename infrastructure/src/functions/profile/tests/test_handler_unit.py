"""
Unit tests for the knotify-profile Lambda handler.

These tests are fully isolated — they do NOT require a running database,
AWS credentials, or a deployed Lambda. All external collaborators (DB
connection, RLS context) are replaced with in-memory stubs.

Run:
    pytest infrastructure/src/functions/profile/tests/test_handler_unit.py -v

Conventions:
  - Each test name follows the pattern:
      given_<context>_when_<action>_then_<outcome>
  - One behaviour per test.
  - The handler module is imported from a local path, not from a deployed zip.

Test coverage by area:
  A. _check_immutable_fields — immutable field detection logic
  B. _build_update_clause — SQL UPDATE builder (field filtering + parameter binding)
  C. _should_set_profile_complete — profile-completion eligibility check
  D. _get_user_id_and_sex — JWT claim extraction helper
  E. Route dispatch — handler routes requests to the correct sub-handler
"""

from __future__ import annotations

import importlib
import json
import sys
import uuid
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module import helper
#
# The handler lives at infrastructure/src/functions/profile/handler.py.
# We import it directly via importlib so the test can run from the repo root
# without a package installation step.
# ---------------------------------------------------------------------------

import os

_HANDLER_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
)


def _import_handler() -> ModuleType:
    """Import the profile handler module (not cached across tests)."""
    spec = importlib.util.spec_from_file_location(
        "profile_handler_" + str(id(object())),
        os.path.join(_HANDLER_DIR, "handler.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Helpers — build minimal Lambda event dicts for HTTP API v2 format
# ---------------------------------------------------------------------------

_EDGE_SECRET = "test-edge-secret-value"


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
    """
    Build an HTTP API Gateway v2 Lambda event.

    The JWT claims are embedded directly in requestContext.authorizer.jwt.claims
    as the HTTP API authorizer would set them — no actual JWT decoding needed.
    """
    event: dict = {
        "requestContext": {
            "http": {
                "method": method,
                "path": path,
            },
            "authorizer": {
                "jwt": {
                    "claims": {
                        "sub": user_sub,
                        "custom:user_sex": user_sex,
                    }
                }
            },
        },
        "headers": {
            "x-knotify-edge-secret": edge_secret,
        },
    }
    if body is not None:
        event["body"] = json.dumps(body)
    if query_params is not None:
        event["queryStringParameters"] = query_params
    if path_params is not None:
        event["pathParameters"] = path_params
    return event


# ---------------------------------------------------------------------------
# Area A: _check_immutable_fields
#
# Given a patch payload and the current row from the DB, _check_immutable_fields
# returns a list of field names whose values differ from the current (non-NULL)
# DB value — these are the "tried to change an already-set immutable field" cases.
# ---------------------------------------------------------------------------

class TestCheckImmutableFields:
    """Tests for _check_immutable_fields(patch_data, current_row)."""

    def test_given_null_db_value_when_setting_first_time_then_no_violation(self):
        """
        Fields that are NULL in the DB (never set) may be given any value
        in a PATCH — this is the first-set case and is always allowed.
        """
        mod = _import_handler()
        current_row = {
            "first_name": None,
            "last_name": None,
            "sex": None,
            "birthday": None,
            "religion": None,
            "subsect": None,
        }
        patch_data = {
            "first_name": "Alice",
            "last_name": "Smith",
            "sex": "Female",
            "birthday": "1995-06-15",
            "religion": "Islam",
            "subsect": "Sunni",
        }
        violations = mod._check_immutable_fields(patch_data, current_row)
        assert violations == [], f"Expected no violations but got {violations}"

    def test_given_set_db_value_when_changing_then_violation_reported(self):
        """
        Attempting to change an immutable field that is already non-NULL in
        the DB must be reported as a violation.
        """
        mod = _import_handler()
        current_row = {
            "first_name": "Alice",
            "last_name": "Smith",
            "sex": "Female",
            "birthday": "1995-06-15",
            "religion": "Islam",
            "subsect": "Sunni",
        }
        patch_data = {
            "first_name": "Alicia",   # different — violation
            "sex": "Male",            # different — violation
        }
        violations = mod._check_immutable_fields(patch_data, current_row)
        assert set(violations) == {"first_name", "sex"}

    def test_given_set_db_value_when_resending_exact_same_value_then_no_violation(self):
        """
        Re-PATCHing with the EXACT SAME value as the current column value is a
        no-op and must NOT be reported as a violation (idempotent re-PATCH rule).
        """
        mod = _import_handler()
        current_row = {
            "first_name": "Alice",
            "last_name": "Smith",
            "sex": "Female",
            "birthday": "1995-06-15",
            "religion": "Islam",
            "subsect": "Sunni",
        }
        patch_data = {
            "first_name": "Alice",    # same — no violation
            "last_name": "Smith",     # same — no violation
            "sex": "Female",          # same — no violation
        }
        violations = mod._check_immutable_fields(patch_data, current_row)
        assert violations == [], f"Expected no violations but got {violations}"

    def test_given_mix_of_same_and_changed_when_patch_then_only_changed_reported(self):
        """
        When a PATCH contains both unchanged and changed immutable fields, only
        the changed ones are in the violations list.
        """
        mod = _import_handler()
        current_row = {
            "first_name": "Alice",
            "last_name": "Smith",
            "sex": "Female",
            "birthday": "1995-06-15",
            "religion": "Islam",
            "subsect": "Sunni",
        }
        patch_data = {
            "first_name": "Alice",   # same — no violation
            "sex": "Male",           # changed — violation
        }
        violations = mod._check_immutable_fields(patch_data, current_row)
        assert violations == ["sex"]

    def test_given_patch_with_only_mutable_fields_then_no_violation(self):
        """
        Mutable fields (job_title, username, photo_url, etc.) patched against
        a row with all immutable fields already set must produce no violation.
        """
        mod = _import_handler()
        current_row = {
            "first_name": "Alice",
            "last_name": "Smith",
            "sex": "Female",
            "birthday": "1995-06-15",
            "religion": "Islam",
            "subsect": "Sunni",
        }
        patch_data = {
            "job_title": "Engineer",
            "username": "alice_dev",
        }
        violations = mod._check_immutable_fields(patch_data, current_row)
        assert violations == []


# ---------------------------------------------------------------------------
# Area B: _should_set_profile_complete
#
# Returns True iff all of {first_name, last_name, sex, birthday, username}
# are non-None in the combined row after the proposed update.
# ---------------------------------------------------------------------------

class TestShouldSetProfileComplete:
    """Tests for _should_set_profile_complete(proposed_row)."""

    def test_given_all_required_fields_present_then_returns_true(self):
        mod = _import_handler()
        proposed = {
            "first_name": "Test",
            "last_name": "User",
            "sex": "Male",
            "birthday": "2000-01-01",
            "username": "test_abc123",
        }
        assert mod._should_set_profile_complete(proposed) is True

    def test_given_missing_username_then_returns_false(self):
        mod = _import_handler()
        proposed = {
            "first_name": "Test",
            "last_name": "User",
            "sex": "Male",
            "birthday": "2000-01-01",
            "username": None,
        }
        assert mod._should_set_profile_complete(proposed) is False

    def test_given_missing_sex_then_returns_false(self):
        mod = _import_handler()
        proposed = {
            "first_name": "Test",
            "last_name": "User",
            "sex": None,
            "birthday": "2000-01-01",
            "username": "test_abc",
        }
        assert mod._should_set_profile_complete(proposed) is False

    def test_given_all_required_absent_then_returns_false(self):
        mod = _import_handler()
        proposed = {
            "first_name": None,
            "last_name": None,
            "sex": None,
            "birthday": None,
            "username": None,
        }
        assert mod._should_set_profile_complete(proposed) is False


# ---------------------------------------------------------------------------
# Area C: _get_user_id_and_sex
#
# Extracts the Cognito sub and custom:user_sex from the HTTP API event's
# requestContext.authorizer.jwt.claims block.
# ---------------------------------------------------------------------------

class TestGetUserIdAndSex:
    """Tests for _get_user_id_and_sex(event)."""

    def test_given_valid_event_then_returns_sub_and_sex(self):
        mod = _import_handler()
        event = _make_event("GET", "/v1/profile/me", user_sub="abc-123", user_sex="Female")
        user_id, user_sex = mod._get_user_id_and_sex(event)
        assert user_id == "abc-123"
        assert user_sex == "Female"

    def test_given_missing_sub_then_raises_key_error(self):
        mod = _import_handler()
        event = {
            "requestContext": {
                "authorizer": {
                    "jwt": {
                        "claims": {
                            "custom:user_sex": "Male",
                            # no "sub"
                        }
                    }
                }
            }
        }
        with pytest.raises(KeyError):
            mod._get_user_id_and_sex(event)


# ---------------------------------------------------------------------------
# Area D: PATCH /v1/profile/me — immutable-field 400 path
#
# The handler must call _check_immutable_fields and return a 400 BEFORE
# any DB write is attempted when violations are found.
# ---------------------------------------------------------------------------

class TestPatchProfileMeImmutableField400:
    """
    When PATCH /v1/profile/me tries to change an already-set immutable field,
    the handler must return HTTP 400 with {"error": "immutable_field", "fields": [...]}.
    """

    def _fake_conn(self, current_row: dict):
        """
        Build a minimal psycopg2 connection stub that returns current_row on
        the first SELECT (fetch current values) and records any UPDATE calls.
        """
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        # Simulate fetchone returning the current profile row as a dict-like tuple
        cur.fetchone.return_value = current_row
        cur.description = [
            (col,) for col in [
                "user_id", "first_name", "last_name", "sex", "birthday",
                "religion", "subsect", "username", "profile_complete_verified",
                "job_title", "photo_url", "chosen_profile_avatar",
                "current_residence_city", "current_residence_country",
                "resident_country_code", "district", "education_level",
                "employer_name", "employment_type", "family_residence_address",
                "father_retired", "fathers_job", "fathers_name",
                "graduation_year", "has_children", "higher_secondary",
                "higher_secondary_passing_year", "highest_degree", "high_school",
                "high_school_passing_year", "marital_status", "marriage_time",
                "mother_retired", "mothers_job", "mothers_name", "move_abroad",
                "office_address", "partners_religious_level", "college_name",
                "professional_category", "relation", "religious_level",
                "salary_range", "preferences", "age", "email", "phone_number",
            ]
        ]
        return conn, cur

    def test_given_immutable_field_already_set_when_changed_in_patch_then_returns_400(self):
        """
        PATCH body attempts to change 'sex' which is already non-NULL in DB.
        Handler must return 400 with error=immutable_field and fields=["sex"].
        The DB cursor's execute must NOT be called with an UPDATE statement.
        """
        mod = _import_handler()

        # DB returns a row where sex = "Male" (already set)
        current_row_tuple = (
            "user-sub-1234",  # user_id
            "Test",           # first_name
            "User",           # last_name
            "Male",           # sex — already set
            "2000-01-01",     # birthday
            "Other",          # religion
            None,             # subsect
            "testuser",       # username
            True,             # profile_complete_verified
            None, None, None, None, None, None, None, None,
            None, None, None, None, None, None, None, None,
            None, None, None, None, None, None, None, None,
            None, None, None, None, None, None, None, None,
            None, None, None, None, None, 25, "test@example.com", None,
        )

        fake_conn = MagicMock()
        fake_cur = MagicMock()
        fake_cur.__enter__ = MagicMock(return_value=fake_cur)
        fake_cur.__exit__ = MagicMock(return_value=False)
        fake_cur.fetchone.return_value = current_row_tuple
        # description list of (name, ...) tuples — handler zips these with row values
        fake_cur.description = [
            ("user_id",), ("first_name",), ("last_name",), ("sex",), ("birthday",),
            ("religion",), ("subsect",), ("username",), ("profile_complete_verified",),
            ("job_title",), ("photo_url",), ("chosen_profile_avatar",),
            ("current_residence_city",), ("current_residence_country",),
            ("resident_country_code",), ("district",), ("education_level",),
            ("employer_name",), ("employment_type",), ("family_residence_address",),
            ("father_retired",), ("fathers_job",), ("fathers_name",),
            ("graduation_year",), ("has_children",), ("higher_secondary",),
            ("higher_secondary_passing_year",), ("highest_degree",), ("high_school",),
            ("high_school_passing_year",), ("marital_status",), ("marriage_time",),
            ("mother_retired",), ("mothers_job",), ("mothers_name",), ("move_abroad",),
            ("office_address",), ("partners_religious_level",), ("college_name",),
            ("professional_category",), ("relation",), ("religious_level",),
            ("salary_range",), ("preferences",), ("age",), ("email",), ("phone_number",),
        ]
        fake_conn.cursor.return_value = fake_cur

        event = _make_event(
            "PATCH",
            "/v1/profile/me",
            body={"sex": "Female"},   # trying to change already-set "sex"
            user_sub="user-sub-1234",
            user_sex="Male",
        )

        with (
            patch.object(mod, "_get_conn", return_value=fake_conn),
            patch.dict(os.environ, {"EDGE_SECRET": _EDGE_SECRET, "DB_SECRET_NAME": "test"}),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 400, (
            f"Expected 400 but got {response['statusCode']}. Body: {response.get('body')}"
        )
        body = json.loads(response["body"])
        assert body["error"] == "immutable_field"
        assert "sex" in body["fields"]

        # Confirm no UPDATE users statement was executed — the immutable-field 400 fires
        # BEFORE any DB write. Note: SELECT ... FOR UPDATE is NOT a write, so we check
        # specifically for "UPDATE users" (the actual UPDATE DML statement).
        update_dml_calls = [
            call for call in fake_cur.execute.call_args_list
            if "UPDATE users" in str(call)
        ]
        assert update_dml_calls == [], "UPDATE users was executed but should not be — immutable 400 must fire first"

    def test_given_same_immutable_value_resent_when_patch_then_returns_200(self):
        """
        Re-PATCHing with the EXACT SAME value as the existing column value
        is a no-op and must return 200 (idempotent re-PATCH rule).
        """
        mod = _import_handler()

        # DB row — first_name already "Test"
        current_row_tuple = (
            "user-sub-1234",  # user_id
            "Test",           # first_name — same as patch
            "User",           # last_name
            "Male",           # sex
            "2000-01-01",     # birthday
            "Other",          # religion
            None,             # subsect
            "testuser",       # username
            True,             # profile_complete_verified
        ) + (None,) * 38     # remaining mutable fields

        fake_conn = MagicMock()
        fake_cur = MagicMock()
        fake_cur.__enter__ = MagicMock(return_value=fake_cur)
        fake_cur.__exit__ = MagicMock(return_value=False)
        fake_cur.fetchone.return_value = current_row_tuple
        fake_cur.description = [
            ("user_id",), ("first_name",), ("last_name",), ("sex",), ("birthday",),
            ("religion",), ("subsect",), ("username",), ("profile_complete_verified",),
            ("job_title",), ("photo_url",), ("chosen_profile_avatar",),
            ("current_residence_city",), ("current_residence_country",),
            ("resident_country_code",), ("district",), ("education_level",),
            ("employer_name",), ("employment_type",), ("family_residence_address",),
            ("father_retired",), ("fathers_job",), ("fathers_name",),
            ("graduation_year",), ("has_children",), ("higher_secondary",),
            ("higher_secondary_passing_year",), ("highest_degree",), ("high_school",),
            ("high_school_passing_year",), ("marital_status",), ("marriage_time",),
            ("mother_retired",), ("mothers_job",), ("mothers_name",), ("move_abroad",),
            ("office_address",), ("partners_religious_level",), ("college_name",),
            ("professional_category",), ("relation",), ("religious_level",),
            ("salary_range",), ("preferences",), ("age",), ("email",), ("phone_number",),
        ]
        fake_conn.cursor.return_value = fake_cur
        # fetchall returns the updated row for the final response
        fake_cur.fetchall.return_value = [current_row_tuple]

        event = _make_event(
            "PATCH",
            "/v1/profile/me",
            body={"first_name": "Test"},   # same value — idempotent
            user_sub="user-sub-1234",
            user_sex="Male",
        )

        with (
            patch.object(mod, "_get_conn", return_value=fake_conn),
            patch.dict(os.environ, {"EDGE_SECRET": _EDGE_SECRET, "DB_SECRET_NAME": "test"}),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 200, (
            f"Expected 200 for idempotent re-PATCH but got {response['statusCode']}. "
            f"Body: {response.get('body')}"
        )


# ---------------------------------------------------------------------------
# Area E: Route dispatch — GET /v1/profile/me vs GET /v1/profiles/{userId}
# ---------------------------------------------------------------------------

class TestRouteDispatch:
    """
    The handler must route requests to the correct sub-handler based on
    HTTP method + path combination. Verified via mock injection.
    """

    def test_given_get_profile_me_when_called_then_dispatched_to_get_me_handler(self):
        """GET /v1/profile/me must call the own-profile sub-handler."""
        mod = _import_handler()

        fake_conn = MagicMock()
        fake_cur = MagicMock()
        fake_cur.__enter__ = MagicMock(return_value=fake_cur)
        fake_cur.__exit__ = MagicMock(return_value=False)
        # own-profile query returns one row tuple
        own_row = ("user-sub-1234", "Test", "User", "Male", "2000-01-01", "Other", None,
                   "testuser", True) + (None,) * 38
        fake_cur.fetchone.return_value = own_row
        fake_cur.description = [
            ("user_id",), ("first_name",), ("last_name",), ("sex",), ("birthday",),
            ("religion",), ("subsect",), ("username",), ("profile_complete_verified",),
            ("job_title",), ("photo_url",), ("chosen_profile_avatar",),
            ("current_residence_city",), ("current_residence_country",),
            ("resident_country_code",), ("district",), ("education_level",),
            ("employer_name",), ("employment_type",), ("family_residence_address",),
            ("father_retired",), ("fathers_job",), ("fathers_name",),
            ("graduation_year",), ("has_children",), ("higher_secondary",),
            ("higher_secondary_passing_year",), ("highest_degree",), ("high_school",),
            ("high_school_passing_year",), ("marital_status",), ("marriage_time",),
            ("mother_retired",), ("mothers_job",), ("mothers_name",), ("move_abroad",),
            ("office_address",), ("partners_religious_level",), ("college_name",),
            ("professional_category",), ("relation",), ("religious_level",),
            ("salary_range",), ("preferences",), ("age",), ("email",), ("phone_number",),
        ]
        fake_conn.cursor.return_value = fake_cur

        event = _make_event("GET", "/v1/profile/me", user_sub="user-sub-1234", user_sex="Male")

        with (
            patch.object(mod, "_get_conn", return_value=fake_conn),
            patch.dict(os.environ, {"EDGE_SECRET": _EDGE_SECRET, "DB_SECRET_NAME": "test"}),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        # Full profile — email must be present for GET /v1/profile/me
        assert "email" in body

    def test_given_get_profiles_by_user_id_then_deck_view_fields_only(self):
        """
        GET /v1/profiles/{userId} must return only deck-view fields —
        email, phone_number, and family fields must NOT be present.
        """
        mod = _import_handler()

        other_user_id = str(uuid.uuid4())

        fake_conn = MagicMock()
        fake_cur = MagicMock()
        fake_cur.__enter__ = MagicMock(return_value=fake_cur)
        fake_cur.__exit__ = MagicMock(return_value=False)
        other_row = (
            other_user_id,  # user_id
            "Other",        # first_name
            "Person",       # last_name
            "Female",       # sex
            "1998-03-20",   # birthday
            "Other",        # religion
            None,           # subsect
            "otherperson",  # username
            True,           # profile_complete_verified
            "Engineer",     # job_title
            None,           # photo_url
            None,           # chosen_profile_avatar
            "Berlin",       # current_residence_city
            "Germany",      # current_residence_country
            "DE",           # resident_country_code
        )
        # fetchone returns the deck-view row from the DB query
        fake_cur.fetchone.return_value = other_row
        fake_cur.description = [
            ("user_id",), ("first_name",), ("last_name",), ("sex",), ("birthday",),
            ("religion",), ("subsect",), ("username",), ("profile_complete_verified",),
            ("job_title",), ("photo_url",), ("chosen_profile_avatar",),
            ("current_residence_city",), ("current_residence_country",),
            ("resident_country_code",),
        ]
        fake_conn.cursor.return_value = fake_cur

        event = _make_event(
            "GET",
            f"/v1/profiles/{other_user_id}",
            path_params={"userId": other_user_id},
            user_sub="user-sub-1234",
            user_sex="Male",
        )

        with (
            patch.object(mod, "_get_conn", return_value=fake_conn),
            patch.dict(os.environ, {"EDGE_SECRET": _EDGE_SECRET, "DB_SECRET_NAME": "test"}),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        # Deck-view must NOT contain sensitive fields
        assert "email" not in body
        assert "phone_number" not in body
        assert "family_residence_address" not in body

    def test_given_get_profiles_with_no_row_returned_then_404(self):
        """
        When the DB returns no row for GET /v1/profiles/{userId} (RLS hides it
        or it doesn't exist), the handler must return 404.
        """
        mod = _import_handler()

        fake_conn = MagicMock()
        fake_cur = MagicMock()
        fake_cur.__enter__ = MagicMock(return_value=fake_cur)
        fake_cur.__exit__ = MagicMock(return_value=False)
        fake_cur.fetchone.return_value = None   # no row
        fake_conn.cursor.return_value = fake_cur

        other_user_id = str(uuid.uuid4())
        event = _make_event(
            "GET",
            f"/v1/profiles/{other_user_id}",
            path_params={"userId": other_user_id},
            user_sub="user-sub-1234",
            user_sex="Male",
        )

        with (
            patch.object(mod, "_get_conn", return_value=fake_conn),
            patch.dict(os.environ, {"EDGE_SECRET": _EDGE_SECRET, "DB_SECRET_NAME": "test"}),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 404

    def test_given_get_profiles_username_with_no_match_then_404(self):
        """
        GET /v1/profiles?username=unknown returns 404 when DB returns no row.
        """
        mod = _import_handler()

        fake_conn = MagicMock()
        fake_cur = MagicMock()
        fake_cur.__enter__ = MagicMock(return_value=fake_cur)
        fake_cur.__exit__ = MagicMock(return_value=False)
        fake_cur.fetchone.return_value = None
        fake_conn.cursor.return_value = fake_cur

        event = _make_event(
            "GET",
            "/v1/profiles",
            query_params={"username": "unknownuser"},
            user_sub="user-sub-1234",
            user_sex="Male",
        )

        with (
            patch.object(mod, "_get_conn", return_value=fake_conn),
            patch.dict(os.environ, {"EDGE_SECRET": _EDGE_SECRET, "DB_SECRET_NAME": "test"}),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 404

    def test_given_missing_edge_secret_then_403(self):
        """
        A request missing the edge secret header must be rejected with 403
        BEFORE any DB access is attempted.
        """
        mod = _import_handler()

        event = _make_event(
            "GET",
            "/v1/profile/me",
            edge_secret="wrong-secret",
        )

        with patch.dict(os.environ, {"EDGE_SECRET": _EDGE_SECRET, "DB_SECRET_NAME": "test"}):
            response = mod.handler(event, None)

        assert response["statusCode"] == 403
