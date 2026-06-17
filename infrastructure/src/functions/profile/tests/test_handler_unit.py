"""
Unit tests for the knotify-profile Lambda handler.

These tests are fully isolated — they do NOT require a running database,
AWS credentials, or a deployed Lambda. All external collaborators (DB
connection, RLS context, boto3 Cognito client) are replaced with in-memory stubs.

Run:
    pytest infrastructure/src/functions/profile/tests/test_handler_unit.py -v

Conventions:
  - Each test name follows the pattern:
      given_<context>_when_<action>_then_<outcome>
  - One behaviour per test.
  - The handler module is imported from a local path, not from a deployed zip.

Test coverage by area:
  A. _check_immutable_fields — immutable field detection logic
  B. _should_set_profile_complete — widened 34-field profile-completion check (story 7.0b)
  C. _get_user_id_and_sex — JWT claim extraction helper
  D. Route dispatch — handler routes requests to the correct sub-handler
  E. Cognito write after commit — admin_update_user_attributes called when flag flips (story 7.0b)
  F. Cognito write best-effort — transient failure returns 200 (story 7.0b)
  G. Preference vector write — PATCH with preferences sets preference_vector in same UPDATE (story 7.3)
  H. Refresh Lambda invoke after commit — boto3 lambda.invoke called when flag flips (story 7.4)
  I. Refresh Lambda invoke skipped on txn rollback — no invoke when exception inside with conn: (story 7.4)
  J. Refresh Lambda invoke best-effort — transient failure returns 200 (story 7.4)
"""

from __future__ import annotations

import importlib
import json
import os
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
# Shared fixture — a "complete" profile row with all 34 required fields
#
# Story 7.0b: _REQUIRED_FOR_COMPLETION expanded to 34 fields. Tests that
# exercise the profile-completion flip path must supply all 34 required fields.
# ---------------------------------------------------------------------------

def _complete_profile_row(user_id: str = "user-sub-1234", sex: str = "Male") -> dict:
    """
    Return a dict representing a post-update row with all 34 required fields
    non-None.  This mirrors what Aurora returns after RETURNING * on the UPDATE.
    """
    return {
        "user_id": user_id,
        "email": "test@example.com",
        "phone_number": None,
        "first_name": "Test",
        "last_name": "User",
        "sex": sex,
        "birthday": "2000-01-01",
        "religion": "Islam",
        "subsect": "Sunni",
        "username": "testuser123",
        "profile_complete_verified": False,
        "job_title": "Engineer",
        "photo_url": None,
        "chosen_profile_avatar": None,
        "current_residence_city": "London",
        "current_residence_country": "United Kingdom",
        "resident_country_code": "GB",
        "district": "East London",
        "education_level": "Bachelors",
        "employer_name": "Acme Corp",
        "employment_type": "Full-time",
        "family_residence_address": "123 Main St, London",
        "father_retired": "No",
        "fathers_job": "Engineer",
        "fathers_name": "Father Test",
        "graduation_year": None,
        "has_children": False,
        "higher_secondary": "A-Levels",
        "higher_secondary_passing_year": None,
        "highest_degree": "BSc Computer Science",
        "high_school": "Some High School",
        "high_school_passing_year": None,
        "marital_status": "Single",
        "marriage_time": "Not Provided",
        "mother_retired": "No",
        "mothers_job": "Teacher",
        "mothers_name": "Mother Test",
        "move_abroad": False,
        "office_address": "456 Work St, London",
        "partners_religious_level": None,
        "college_name": "Some University",
        "professional_category": "Technology",
        "relation": "Self",
        "religious_level": "Moderate",
        "salary_range": "50000-70000",
        "preferences": {},
        "preference_vector": None,
        "age": 24,
    }


def _complete_profile_tuple_and_description(user_id: str = "user-sub-1234", sex: str = "Male"):
    """
    Return (tuple, description_list) in the order matching _SELECT_FOR_UPDATE_SQL.

    Column order must match the SELECT column list in handler.py.
    """
    row = _complete_profile_row(user_id=user_id, sex=sex)
    columns = [
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
    tup = tuple(row.get(c) for c in columns)
    description = [(c,) for c in columns]
    return tup, description


# ---------------------------------------------------------------------------
# Area A: _check_immutable_fields
# ---------------------------------------------------------------------------

class TestCheckImmutableFields:
    """Tests for _check_immutable_fields(patch_data, current_row)."""

    def test_given_null_db_value_when_setting_first_time_then_no_violation(self):
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
            "first_name": "Alice",
            "last_name": "Smith",
            "sex": "Female",
        }
        violations = mod._check_immutable_fields(patch_data, current_row)
        assert violations == [], f"Expected no violations but got {violations}"

    def test_given_mix_of_same_and_changed_when_patch_then_only_changed_reported(self):
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
# Area B: _should_set_profile_complete — widened 34-field check (story 7.0b)
# ---------------------------------------------------------------------------

class TestShouldSetProfileComplete:
    """Tests for _should_set_profile_complete(proposed_row) with 34-field frozenset."""

    def test_given_all_34_required_fields_present_then_returns_true(self):
        """All 34 required fields non-None → True."""
        mod = _import_handler()
        proposed = _complete_profile_row()
        assert mod._should_set_profile_complete(proposed) is True

    def test_given_missing_username_then_returns_false(self):
        mod = _import_handler()
        proposed = _complete_profile_row()
        proposed["username"] = None
        assert mod._should_set_profile_complete(proposed) is False

    def test_given_missing_religion_then_returns_false(self):
        mod = _import_handler()
        proposed = _complete_profile_row()
        proposed["religion"] = None
        assert mod._should_set_profile_complete(proposed) is False

    def test_given_missing_job_title_then_returns_false(self):
        mod = _import_handler()
        proposed = _complete_profile_row()
        proposed["job_title"] = None
        assert mod._should_set_profile_complete(proposed) is False

    def test_given_missing_fathers_name_then_returns_false(self):
        mod = _import_handler()
        proposed = _complete_profile_row()
        proposed["fathers_name"] = None
        assert mod._should_set_profile_complete(proposed) is False

    def test_given_missing_marital_status_then_returns_false(self):
        mod = _import_handler()
        proposed = _complete_profile_row()
        proposed["marital_status"] = None
        assert mod._should_set_profile_complete(proposed) is False

    def test_given_missing_relation_then_returns_false(self):
        mod = _import_handler()
        proposed = _complete_profile_row()
        proposed["relation"] = None
        assert mod._should_set_profile_complete(proposed) is False

    def test_given_marriage_time_none_then_still_returns_true(self):
        """
        marriage_time is NOT in _REQUIRED_FOR_COMPLETION (story 7.0b resolution B2).
        A row with marriage_time=None but all other 34 fields set must still return True.
        """
        mod = _import_handler()
        proposed = _complete_profile_row()
        proposed["marriage_time"] = None
        # marriage_time is not required — should still complete
        assert mod._should_set_profile_complete(proposed) is True

    def test_given_all_required_absent_then_returns_false(self):
        mod = _import_handler()
        proposed = {k: None for k in _complete_profile_row().keys()}
        assert mod._should_set_profile_complete(proposed) is False


# ---------------------------------------------------------------------------
# Area C: _get_user_id_and_sex
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
                        }
                    }
                }
            }
        }
        with pytest.raises(KeyError):
            mod._get_user_id_and_sex(event)


# ---------------------------------------------------------------------------
# Area D: PATCH /v1/profile/me — immutable-field 400 path
# ---------------------------------------------------------------------------

class TestPatchProfileMeImmutableField400:
    """PATCH attempts to change an already-set immutable field → 400."""

    def _make_fake_conn(self, current_row_tuple, description):
        fake_conn = MagicMock()
        fake_cur = MagicMock()
        fake_cur.__enter__ = MagicMock(return_value=fake_cur)
        fake_cur.__exit__ = MagicMock(return_value=False)
        fake_cur.fetchone.return_value = current_row_tuple
        fake_cur.description = description
        fake_conn.cursor.return_value = fake_cur
        return fake_conn, fake_cur

    def test_given_immutable_field_already_set_when_changed_in_patch_then_returns_400(self):
        mod = _import_handler()
        tup, desc = _complete_profile_tuple_and_description()
        # sex is already "Male" in the tuple; attempt to change to "Female"
        fake_conn, fake_cur = self._make_fake_conn(tup, desc)

        event = _make_event(
            "PATCH",
            "/v1/profile/me",
            body={"sex": "Female"},
            user_sub="user-sub-1234",
            user_sex="Male",
        )

        with (
            patch.object(mod, "_get_conn", return_value=fake_conn),
            patch.dict(os.environ, {"EDGE_SECRET": _EDGE_SECRET, "DB_SECRET_NAME": "test"}),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert body["error"] == "immutable_field"
        assert "sex" in body["fields"]

        update_dml_calls = [
            call for call in fake_cur.execute.call_args_list
            if "UPDATE users" in str(call)
        ]
        assert update_dml_calls == [], "UPDATE users must not be called on 400 path"

    def test_given_same_immutable_value_resent_when_patch_then_returns_200(self):
        mod = _import_handler()
        tup, desc = _complete_profile_tuple_and_description()
        fake_conn, fake_cur = self._make_fake_conn(tup, desc)
        # RETURNING * returns same row
        fake_cur.fetchone.side_effect = [tup, tup]

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

        assert response["statusCode"] == 200


# ---------------------------------------------------------------------------
# Area E: Route dispatch
# ---------------------------------------------------------------------------

class TestRouteDispatch:
    """Handler routes requests to the correct sub-handler."""

    def test_given_get_profile_me_when_called_then_dispatched_to_get_me_handler(self):
        mod = _import_handler()
        tup, desc = _complete_profile_tuple_and_description()
        fake_conn = MagicMock()
        fake_cur = MagicMock()
        fake_cur.__enter__ = MagicMock(return_value=fake_cur)
        fake_cur.__exit__ = MagicMock(return_value=False)
        fake_cur.fetchone.return_value = tup
        fake_cur.description = desc
        fake_conn.cursor.return_value = fake_cur

        event = _make_event("GET", "/v1/profile/me", user_sub="user-sub-1234", user_sex="Male")

        with (
            patch.object(mod, "_get_conn", return_value=fake_conn),
            patch.dict(os.environ, {"EDGE_SECRET": _EDGE_SECRET, "DB_SECRET_NAME": "test"}),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert "email" in body

    def test_given_get_profiles_by_user_id_then_deck_view_fields_only(self):
        mod = _import_handler()
        other_user_id = str(uuid.uuid4())
        fake_conn = MagicMock()
        fake_cur = MagicMock()
        fake_cur.__enter__ = MagicMock(return_value=fake_cur)
        fake_cur.__exit__ = MagicMock(return_value=False)
        other_row = (
            other_user_id, "Other", "Person", "Female", "1998-03-20", "Other", None,
            "otherperson", True, "Engineer", None, None, "Berlin", "Germany", "DE",
        )
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
        assert "email" not in body
        assert "phone_number" not in body
        assert "family_residence_address" not in body

    def test_given_get_profiles_with_no_row_returned_then_404(self):
        mod = _import_handler()
        fake_conn = MagicMock()
        fake_cur = MagicMock()
        fake_cur.__enter__ = MagicMock(return_value=fake_cur)
        fake_cur.__exit__ = MagicMock(return_value=False)
        fake_cur.fetchone.return_value = None
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
        mod = _import_handler()
        event = _make_event("GET", "/v1/profile/me", edge_secret="wrong-secret")

        with patch.dict(os.environ, {"EDGE_SECRET": _EDGE_SECRET, "DB_SECRET_NAME": "test",
                                     "USER_POOL_ID": "eu-central-1_TESTPOOL"}):
            response = mod.handler(event, None)

        assert response["statusCode"] == 403


# ---------------------------------------------------------------------------
# Area F: Cognito attribute write after commit (story 7.0b)
# ---------------------------------------------------------------------------

class TestCognitoAttributeWriteAfterCommit:
    """
    When PATCH /v1/profile/me flips profile_complete_verified from false → true,
    the handler must call boto3 cognito-idp admin_update_user_attributes
    AFTER the with conn: block commits.
    """

    def _make_fake_conn_for_flip(self, user_id: str, sex: str):
        """
        Build a fake connection where:
          - First fetchone = incomplete row (profile_complete_verified=False)
          - Second fetchone (RETURNING *) = updated row with profile_complete_verified=True
        """
        tup_before, desc = _complete_profile_tuple_and_description(user_id=user_id, sex=sex)
        # mark profile_complete_verified as False in the initial fetch
        tup_list = list(tup_before)
        pvc_idx = [c[0] for c in desc].index("profile_complete_verified")
        tup_list[pvc_idx] = False
        tup_before = tuple(tup_list)

        # RETURNING * row has profile_complete_verified=True
        tup_after = list(tup_before)
        tup_after[pvc_idx] = True
        tup_after = tuple(tup_after)

        fake_conn = MagicMock()
        fake_cur = MagicMock()
        fake_cur.__enter__ = MagicMock(return_value=fake_cur)
        fake_cur.__exit__ = MagicMock(return_value=False)
        fake_cur.fetchone.side_effect = [tup_before, tup_after]
        fake_cur.description = desc
        fake_conn.cursor.return_value = fake_cur
        return fake_conn, desc, pvc_idx

    def test_given_profile_completes_when_patch_then_cognito_admin_update_called(self):
        """
        When PATCH supplies all required fields and flips profile_complete_verified
        from false to true, admin_update_user_attributes must be called with
        custom:profile_complete = "true" on the correct user pool.
        """
        mod = _import_handler()
        user_id = "user-sub-1234"
        fake_conn, desc, _ = self._make_fake_conn_for_flip(user_id, "Male")

        mock_cognito = MagicMock()

        # PATCH body with all 34 required fields (supply a subset that together
        # with the DB row makes it complete; the DB row fixture supplies the rest)
        event = _make_event(
            "PATCH",
            "/v1/profile/me",
            body={"job_title": "Engineer"},
            user_sub=user_id,
            user_sex="Male",
        )

        with (
            patch.object(mod, "_get_conn", return_value=fake_conn),
            patch.object(mod, "_USER_POOL_ID", "eu-central-1_TESTPOOL"),
            patch.dict(os.environ, {
                "EDGE_SECRET": _EDGE_SECRET,
                "DB_SECRET_NAME": "test",
            }),
            patch.object(mod.boto3, "client", return_value=mock_cognito) as mock_b3_client,
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 200
        mock_b3_client.assert_called_once_with("cognito-idp")
        mock_cognito.admin_update_user_attributes.assert_called_once_with(
            UserPoolId="eu-central-1_TESTPOOL",
            Username=user_id,
            UserAttributes=[{"Name": "custom:profile_complete", "Value": "true"}],
        )

    def test_given_profile_already_complete_when_patch_then_cognito_not_called(self):
        """
        When profile_complete_verified is already True in the DB row,
        the flag does not flip and admin_update_user_attributes is NOT called.
        """
        mod = _import_handler()
        tup, desc = _complete_profile_tuple_and_description()
        pvc_idx = [c[0] for c in desc].index("profile_complete_verified")
        # Already True in both before and after
        tup_list = list(tup)
        tup_list[pvc_idx] = True
        tup_true = tuple(tup_list)

        fake_conn = MagicMock()
        fake_cur = MagicMock()
        fake_cur.__enter__ = MagicMock(return_value=fake_cur)
        fake_cur.__exit__ = MagicMock(return_value=False)
        fake_cur.fetchone.side_effect = [tup_true, tup_true]
        fake_cur.description = desc
        fake_conn.cursor.return_value = fake_cur

        event = _make_event(
            "PATCH",
            "/v1/profile/me",
            body={"job_title": "New Title"},
            user_sub="user-sub-1234",
            user_sex="Male",
        )

        with (
            patch.object(mod, "_get_conn", return_value=fake_conn),
            patch.object(mod, "_USER_POOL_ID", "eu-central-1_TESTPOOL"),
            patch.dict(os.environ, {
                "EDGE_SECRET": _EDGE_SECRET,
                "DB_SECRET_NAME": "test",
            }),
            patch.object(mod.boto3, "client") as mock_b3_client,
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 200
        mock_b3_client.assert_not_called()

    def test_given_cognito_write_fails_when_profile_completes_then_returns_200_anyway(self):
        """
        Best-effort: if admin_update_user_attributes raises (transient failure),
        the handler logs a warning and still returns HTTP 200.
        The Aurora commit already succeeded and must not be rolled back.
        """
        mod = _import_handler()
        user_id = "user-sub-1234"
        fake_conn, desc, _ = self._make_fake_conn_for_flip(user_id, "Male")

        mock_cognito = MagicMock()
        mock_cognito.admin_update_user_attributes.side_effect = Exception(
            "Transient Cognito error"
        )

        event = _make_event(
            "PATCH",
            "/v1/profile/me",
            body={"job_title": "Engineer"},
            user_sub=user_id,
            user_sex="Male",
        )

        with (
            patch.object(mod, "_get_conn", return_value=fake_conn),
            patch.object(mod, "_USER_POOL_ID", "eu-central-1_TESTPOOL"),
            patch.dict(os.environ, {
                "EDGE_SECRET": _EDGE_SECRET,
                "DB_SECRET_NAME": "test",
            }),
            patch.object(mod.boto3, "client", return_value=mock_cognito),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 200, (
            f"Expected 200 even on Cognito failure, got {response['statusCode']}"
        )


# ---------------------------------------------------------------------------
# Area G: Preference vector write (story 7.3)
# ---------------------------------------------------------------------------

class TestPreferenceVectorWrite:
    """
    When PATCH /v1/profile/me includes "preferences" in the body, the handler
    must compute preference_vector = encode_prefs(preferences) and include it
    in the same UPDATE statement using the SQL cast `preference_vector = %s::vector`.
    """

    def _make_fake_conn_for_patch(self, user_id: str = "user-sub-1234", sex: str = "Male"):
        """
        Build a fake connection suitable for a simple PATCH that does NOT flip
        profile_complete_verified. Returns (fake_conn, fake_cur, desc).
        """
        tup, desc = _complete_profile_tuple_and_description(user_id=user_id, sex=sex)
        # profile_complete_verified is already True so no flip happens
        pvc_idx = [c[0] for c in desc].index("profile_complete_verified")
        tup_list = list(tup)
        tup_list[pvc_idx] = True
        tup_complete = tuple(tup_list)

        fake_conn = MagicMock()
        fake_cur = MagicMock()
        fake_cur.__enter__ = MagicMock(return_value=fake_cur)
        fake_cur.__exit__ = MagicMock(return_value=False)
        # First fetchone = current row; second = RETURNING * row
        fake_cur.fetchone.side_effect = [tup_complete, tup_complete]
        fake_cur.description = desc
        fake_conn.cursor.return_value = fake_cur
        return fake_conn, fake_cur, desc

    def test_given_preferences_in_patch_body_when_update_executed_then_sql_contains_preference_vector_cast(self):
        """
        When "preferences" is in the PATCH body, the UPDATE SQL must contain
        'preference_vector = %s::vector' — the explicit cast required for psycopg2
        vector binding without pgvector's register_vector() adapter.
        """
        mod = _import_handler()
        fake_conn, fake_cur, _ = self._make_fake_conn_for_patch()

        preferences_payload = {"highlyeducated": True, "athletic": True}
        event = _make_event(
            "PATCH",
            "/v1/profile/me",
            body={"preferences": preferences_payload},
            user_sub="user-sub-1234",
            user_sex="Male",
        )

        with (
            patch.object(mod, "_get_conn", return_value=fake_conn),
            patch.dict(os.environ, {
                "EDGE_SECRET": _EDGE_SECRET,
                "DB_SECRET_NAME": "test",
                "USER_POOL_ID": "eu-central-1_TESTPOOL",
            }),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 200, (
            f"Expected 200, got {response['statusCode']}: {response.get('body')}"
        )

        # Find the UPDATE call in cur.execute calls
        update_calls = [
            call_args
            for call_args in fake_cur.execute.call_args_list
            if "UPDATE users" in str(call_args)
        ]
        assert len(update_calls) == 1, (
            f"Expected exactly one UPDATE users call, got {len(update_calls)}"
        )

        update_sql = str(update_calls[0].args[0])
        assert "preference_vector = %s::vector" in update_sql, (
            f"Expected 'preference_vector = %s::vector' in UPDATE SQL but got:\n{update_sql}"
        )

    def test_given_preferences_in_patch_body_when_update_executed_then_preference_vector_value_is_list(self):
        """
        The value bound for preference_vector must be a list of 20 floats
        (psycopg2 will bind it via its default list adapter, and the ::vector
        cast in SQL coerces it to the pgvector column type).
        """
        mod = _import_handler()
        fake_conn, fake_cur, _ = self._make_fake_conn_for_patch()

        preferences_payload = {"highlyeducated": True}
        event = _make_event(
            "PATCH",
            "/v1/profile/me",
            body={"preferences": preferences_payload},
            user_sub="user-sub-1234",
            user_sex="Male",
        )

        with (
            patch.object(mod, "_get_conn", return_value=fake_conn),
            patch.dict(os.environ, {
                "EDGE_SECRET": _EDGE_SECRET,
                "DB_SECRET_NAME": "test",
                "USER_POOL_ID": "eu-central-1_TESTPOOL",
            }),
        ):
            mod.handler(event, None)

        # Find the UPDATE call and extract its parameter list
        update_calls = [
            call_args
            for call_args in fake_cur.execute.call_args_list
            if "UPDATE users" in str(call_args)
        ]
        assert len(update_calls) == 1

        params = update_calls[0].args[1]  # second arg to execute()
        # params is a list: [<preferences_jsonb>, <preference_vector_list>, <user_id>]
        # Find the preference_vector value — it's a list of 20 floats
        vector_values = [p for p in params if isinstance(p, list) and len(p) == 20]
        assert len(vector_values) == 1, (
            f"Expected exactly one 20-element list in UPDATE params, got params={params}"
        )
        vec = vector_values[0]
        assert vec[0] == 1.0, f"highlyeducated is at index 0, expected 1.0, got {vec[0]}"
        assert all(v == 0.0 for v in vec[1:]), (
            f"All positions except index 0 must be 0.0, got {vec[1:]}"
        )

    def test_given_preferences_in_patch_body_when_update_executed_then_preferences_param_is_json_wrapped_not_raw_dict(self):
        """
        Regression — `preferences` is a jsonb column. psycopg2 has no global
        Json adapter registered (only register_uuid()), so passing a raw dict
        raises `ProgrammingError: can't adapt type 'dict'` at execute time.
        The handler must wrap dict values in psycopg2.extras.Json so they
        serialize to a JSON literal.
        """
        import psycopg2.extras

        mod = _import_handler()
        fake_conn, fake_cur, _ = self._make_fake_conn_for_patch()

        preferences_payload = {"highlyeducated": True, "athletic": True}
        event = _make_event(
            "PATCH",
            "/v1/profile/me",
            body={"preferences": preferences_payload},
            user_sub="user-sub-1234",
            user_sex="Male",
        )

        with (
            patch.object(mod, "_get_conn", return_value=fake_conn),
            patch.dict(os.environ, {
                "EDGE_SECRET": _EDGE_SECRET,
                "DB_SECRET_NAME": "test",
                "USER_POOL_ID": "eu-central-1_TESTPOOL",
            }),
        ):
            mod.handler(event, None)

        update_calls = [
            call_args
            for call_args in fake_cur.execute.call_args_list
            if "UPDATE users" in str(call_args)
        ]
        assert len(update_calls) == 1
        params = list(update_calls[0].args[1])

        raw_dicts = [p for p in params if isinstance(p, dict)]
        assert raw_dicts == [], (
            f"No raw dict may be passed to psycopg2 — preferences must be wrapped "
            f"in psycopg2.extras.Json. Found raw dicts in params: {raw_dicts}"
        )
        json_wrapped = [p for p in params if isinstance(p, psycopg2.extras.Json)]
        assert len(json_wrapped) == 1, (
            f"Expected exactly one psycopg2.extras.Json-wrapped param "
            f"(the preferences dict), got {len(json_wrapped)}: params={params}"
        )
        assert json_wrapped[0].adapted == preferences_payload, (
            f"Wrapped preferences value must equal the input dict, got "
            f"{json_wrapped[0].adapted!r}"
        )

    def test_given_no_preferences_in_patch_body_when_update_executed_then_no_preference_vector_in_sql(self):
        """
        When "preferences" is NOT in the PATCH body, the UPDATE SQL must NOT
        include preference_vector — the handler only writes it alongside preferences.
        """
        mod = _import_handler()
        fake_conn, fake_cur, _ = self._make_fake_conn_for_patch()

        event = _make_event(
            "PATCH",
            "/v1/profile/me",
            body={"job_title": "New Title"},
            user_sub="user-sub-1234",
            user_sex="Male",
        )

        with (
            patch.object(mod, "_get_conn", return_value=fake_conn),
            patch.dict(os.environ, {
                "EDGE_SECRET": _EDGE_SECRET,
                "DB_SECRET_NAME": "test",
                "USER_POOL_ID": "eu-central-1_TESTPOOL",
            }),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 200

        update_calls = [
            call_args
            for call_args in fake_cur.execute.call_args_list
            if "UPDATE users" in str(call_args)
        ]
        assert len(update_calls) == 1
        update_sql = str(update_calls[0].args[0])
        assert "preference_vector" not in update_sql, (
            f"preference_vector must not appear in SQL when preferences not in patch body. "
            f"SQL was:\n{update_sql}"
        )


# ---------------------------------------------------------------------------
# Area H: Refresh Lambda invoke after commit (story 7.4)
# ---------------------------------------------------------------------------


class TestRefreshLambdaInvokeAfterCommit:
    """
    When PATCH /v1/profile/me flips profile_complete_verified from false → true,
    the handler must asynchronously invoke the refresh Lambda (boto3
    lambda.invoke with InvocationType="Event") AFTER the with conn: block commits.

    Both the Cognito attribute write (story 7.0b) and the Lambda invoke (story 7.4)
    fire after the commit; the order is: Cognito write first, then lambda.invoke.
    """

    def _make_fake_conn_for_flip(self, user_id: str, sex: str):
        """
        Build a fake connection where the first fetchone returns an incomplete
        row (profile_complete_verified=False) and the second (RETURNING *) returns
        the updated row with profile_complete_verified=True.
        """
        tup_before, desc = _complete_profile_tuple_and_description(user_id=user_id, sex=sex)
        tup_list = list(tup_before)
        pvc_idx = [c[0] for c in desc].index("profile_complete_verified")
        tup_list[pvc_idx] = False
        tup_before = tuple(tup_list)

        tup_after = list(tup_before)
        tup_after[pvc_idx] = True
        tup_after = tuple(tup_after)

        fake_conn = MagicMock()
        fake_cur = MagicMock()
        fake_cur.__enter__ = MagicMock(return_value=fake_cur)
        fake_cur.__exit__ = MagicMock(return_value=False)
        fake_cur.fetchone.side_effect = [tup_before, tup_after]
        fake_cur.description = desc
        fake_conn.cursor.return_value = fake_cur
        return fake_conn

    def test_given_profile_completes_when_patch_then_lambda_invoke_called_with_event_type(self):
        """
        When PATCH flips profile_complete_verified from false to true, the handler
        must call boto3 lambda.invoke with InvocationType="Event" on the refresh Lambda.
        """
        mod = _import_handler()
        user_id = "user-sub-1234"
        fake_conn = self._make_fake_conn_for_flip(user_id, "Male")

        refresh_lambda_arn = "arn:aws:lambda:eu-central-1:123456789:function:knotify-refresh-deck-view-dev"
        mock_lambda_client = MagicMock()
        mock_cognito_client = MagicMock()

        def _mock_boto3_client(service_name, **kwargs):
            if service_name == "lambda":
                return mock_lambda_client
            if service_name == "cognito-idp":
                return mock_cognito_client
            return MagicMock()

        event = _make_event(
            "PATCH",
            "/v1/profile/me",
            body={"job_title": "Engineer"},
            user_sub=user_id,
            user_sex="Male",
        )

        with (
            patch.object(mod, "_get_conn", return_value=fake_conn),
            patch.object(mod, "_USER_POOL_ID", "eu-central-1_TESTPOOL"),
            patch.object(mod, "_REFRESH_LAMBDA_ARN", refresh_lambda_arn),
            patch.dict(os.environ, {
                "EDGE_SECRET": _EDGE_SECRET,
                "DB_SECRET_NAME": "test",
            }),
            patch.object(mod.boto3, "client", side_effect=_mock_boto3_client),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 200
        mock_lambda_client.invoke.assert_called_once_with(
            FunctionName=refresh_lambda_arn,
            InvocationType="Event",
            Payload=b"{}",
        )

    def test_given_profile_already_complete_when_patch_then_lambda_invoke_not_called(self):
        """
        When profile_complete_verified is already True, the flag does not flip
        and the refresh Lambda must NOT be invoked.
        """
        mod = _import_handler()
        tup, desc = _complete_profile_tuple_and_description()
        pvc_idx = [c[0] for c in desc].index("profile_complete_verified")
        tup_list = list(tup)
        tup_list[pvc_idx] = True
        tup_true = tuple(tup_list)

        fake_conn = MagicMock()
        fake_cur = MagicMock()
        fake_cur.__enter__ = MagicMock(return_value=fake_cur)
        fake_cur.__exit__ = MagicMock(return_value=False)
        fake_cur.fetchone.side_effect = [tup_true, tup_true]
        fake_cur.description = desc
        fake_conn.cursor.return_value = fake_cur

        event = _make_event(
            "PATCH",
            "/v1/profile/me",
            body={"job_title": "New Title"},
            user_sub="user-sub-1234",
            user_sex="Male",
        )

        with (
            patch.object(mod, "_get_conn", return_value=fake_conn),
            patch.object(mod, "_USER_POOL_ID", "eu-central-1_TESTPOOL"),
            patch.dict(os.environ, {
                "EDGE_SECRET": _EDGE_SECRET,
                "DB_SECRET_NAME": "test",
            }),
            patch.object(mod.boto3, "client") as mock_b3_client,
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 200
        # boto3.client should never be called — no flip, no invoke
        mock_b3_client.assert_not_called()


# ---------------------------------------------------------------------------
# Area I: Refresh Lambda invoke skipped on txn rollback (story 7.4)
# ---------------------------------------------------------------------------


class TestRefreshLambdaInvokeSkippedOnRollback:
    """
    When an exception is raised INSIDE the `with conn:` block (transaction
    rolls back), the handler must NOT call the refresh Lambda.

    This verifies that the invoke is issued OUTSIDE the transaction block —
    if the transaction rolls back, the invoke is skipped.
    """

    def test_given_exception_inside_transaction_when_patch_then_lambda_invoke_not_called(self):
        """
        An exception raised inside the `with conn:` block (DB error, etc.) must
        propagate upward WITHOUT triggering the refresh Lambda invoke.

        This is the critical correctness invariant: if the Aurora transaction
        rolls back, we must not invalidate the deck_view with a stale refresh.
        """
        mod = _import_handler()

        # Build a fake connection that raises inside the cursor execute call
        # (simulates a DB error mid-transaction)
        fake_conn = MagicMock()
        fake_cur = MagicMock()
        fake_cur.__enter__ = MagicMock(return_value=fake_cur)
        fake_cur.__exit__ = MagicMock(return_value=False)
        # The SELECT for UPDATE raises an exception to simulate a txn failure
        fake_cur.fetchone.side_effect = Exception("simulated DB error during transaction")
        fake_conn.cursor.return_value = fake_cur

        mock_lambda_client = MagicMock()

        event = _make_event(
            "PATCH",
            "/v1/profile/me",
            body={"job_title": "Engineer"},
            user_sub="user-sub-1234",
            user_sex="Male",
        )

        with (
            patch.object(mod, "_get_conn", return_value=fake_conn),
            patch.dict(os.environ, {
                "EDGE_SECRET": _EDGE_SECRET,
                "DB_SECRET_NAME": "test",
            }),
            patch.object(mod.boto3, "client", return_value=mock_lambda_client),
        ):
            with pytest.raises(Exception, match="simulated DB error during transaction"):
                mod.handler(event, None)

        # The refresh Lambda must NOT have been invoked
        mock_lambda_client.invoke.assert_not_called()


# ---------------------------------------------------------------------------
# Area J: Refresh Lambda invoke best-effort (story 7.4)
# ---------------------------------------------------------------------------


class TestRefreshLambdaInvokeBestEffort:
    """
    If the refresh Lambda invoke raises a transient error, the handler must
    log a warning and return HTTP 200 to the client — the Aurora commit already
    succeeded and must not cause a 5xx to the client.
    """

    def _make_fake_conn_for_flip(self, user_id: str, sex: str):
        tup_before, desc = _complete_profile_tuple_and_description(user_id=user_id, sex=sex)
        tup_list = list(tup_before)
        pvc_idx = [c[0] for c in desc].index("profile_complete_verified")
        tup_list[pvc_idx] = False
        tup_before = tuple(tup_list)

        tup_after = list(tup_before)
        tup_after[pvc_idx] = True
        tup_after = tuple(tup_after)

        fake_conn = MagicMock()
        fake_cur = MagicMock()
        fake_cur.__enter__ = MagicMock(return_value=fake_cur)
        fake_cur.__exit__ = MagicMock(return_value=False)
        fake_cur.fetchone.side_effect = [tup_before, tup_after]
        fake_cur.description = desc
        fake_conn.cursor.return_value = fake_cur
        return fake_conn

    def test_given_lambda_invoke_fails_when_profile_completes_then_returns_200_anyway(self):
        """
        Best-effort: if lambda.invoke raises (transient failure),
        the handler logs a warning and still returns HTTP 200.
        The Aurora commit already succeeded; the deck_view will be refreshed
        on the next scheduled run.
        """
        mod = _import_handler()
        user_id = "user-sub-1234"
        fake_conn = self._make_fake_conn_for_flip(user_id, "Male")

        refresh_lambda_arn = "arn:aws:lambda:eu-central-1:123456789:function:knotify-refresh-deck-view-dev"
        mock_lambda_client = MagicMock()
        mock_lambda_client.invoke.side_effect = Exception("Transient Lambda invoke error")
        mock_cognito_client = MagicMock()

        def _mock_boto3_client(service_name, **kwargs):
            if service_name == "lambda":
                return mock_lambda_client
            if service_name == "cognito-idp":
                return mock_cognito_client
            return MagicMock()

        event = _make_event(
            "PATCH",
            "/v1/profile/me",
            body={"job_title": "Engineer"},
            user_sub=user_id,
            user_sex="Male",
        )

        with (
            patch.object(mod, "_get_conn", return_value=fake_conn),
            patch.object(mod, "_USER_POOL_ID", "eu-central-1_TESTPOOL"),
            patch.object(mod, "_REFRESH_LAMBDA_ARN", refresh_lambda_arn),
            patch.dict(os.environ, {
                "EDGE_SECRET": _EDGE_SECRET,
                "DB_SECRET_NAME": "test",
            }),
            patch.object(mod.boto3, "client", side_effect=_mock_boto3_client),
        ):
            response = mod.handler(event, None)

        assert response["statusCode"] == 200, (
            f"Expected 200 even on Lambda invoke failure, got {response['statusCode']}"
        )
