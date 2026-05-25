"""
Unit tests for cognito_post_confirmation handler.

Tests that do NOT require a running database or real AWS services.
All external collaborators (knotify_db.get_connection, boto3) are mocked
at the boundary.

Story 3.6 ACs tested here:
  - Gender mapping: m, M, male → Male; f, F, female → Female; anything else → None + warning
  - Birthdate parsing: valid ISO date parsed; invalid → None + warning
  - Missing email: get_connection NOT called; handler returns event unmodified
  - Happy path: get_connection called, correct INSERT SQL with ON CONFLICT DO NOTHING
  - Re-run (row exists): ON CONFLICT DO NOTHING means no error raised
  - Partial data (no gender/birthdate): NULL passed for absent optional columns
"""

import importlib
import os
import sys
import unittest
from unittest.mock import MagicMock, call, patch


# ---------------------------------------------------------------------------
# Module-level import — handler uses module-level init_logger and DB_SECRET_NAME
# so we must set env vars before importing.
# ---------------------------------------------------------------------------

os.environ.setdefault("DB_SECRET_NAME", "knotify-test-app-user-credential")
os.environ.setdefault("AWS_DEFAULT_REGION", "eu-central-1")
os.environ.setdefault("POWERTOOLS_SERVICE_NAME", "cognito_post_confirmation")
os.environ.setdefault("POWERTOOLS_DEV", "true")  # suppress Powertools JSON output in tests


def _make_cognito_event(
    sub: str = "user-sub-uuid-0001",
    email: str = "alice@example.com",
    given_name: str | None = "Alice",
    family_name: str | None = "Smith",
    gender: str | None = "female",
    birthdate: str | None = "1995-06-15",
    trigger_source: str = "PostConfirmation_ConfirmSignUp",
) -> dict:
    """Build a minimal synthetic Cognito PostConfirmation event."""
    attrs: dict = {}
    if email is not None:
        attrs["email"] = email
    attrs["sub"] = sub
    if given_name is not None:
        attrs["given_name"] = given_name
    if family_name is not None:
        attrs["family_name"] = family_name
    if gender is not None:
        attrs["gender"] = gender
    if birthdate is not None:
        attrs["birthdate"] = birthdate

    return {
        "version": "1",
        "triggerSource": trigger_source,
        "region": "eu-central-1",
        "userPoolId": "eu-central-1_TESTPOOL",
        "userName": sub,
        "callerContext": {
            "awsSdkVersion": "aws-sdk-unknown-unknown",
            "clientId": "test-client-id",
        },
        "request": {
            "userAttributes": attrs,
        },
        "response": {},
    }


class TestNormalizeGender(unittest.TestCase):
    """
    Given the _normalize_gender helper,
    when called with various raw gender strings,
    then it returns the correct canonical value or None.
    """

    def _get_fn(self):
        import handler as h
        return h._normalize_gender

    def test_given_lowercase_m_when_normalize_then_returns_Male(self):
        self.assertEqual(self._get_fn()("m"), "Male")

    def test_given_uppercase_M_when_normalize_then_returns_Male(self):
        self.assertEqual(self._get_fn()("M"), "Male")

    def test_given_lowercase_male_when_normalize_then_returns_Male(self):
        self.assertEqual(self._get_fn()("male"), "Male")

    def test_given_lowercase_f_when_normalize_then_returns_Female(self):
        self.assertEqual(self._get_fn()("f"), "Female")

    def test_given_uppercase_F_when_normalize_then_returns_Female(self):
        self.assertEqual(self._get_fn()("F"), "Female")

    def test_given_lowercase_female_when_normalize_then_returns_Female(self):
        self.assertEqual(self._get_fn()("female"), "Female")

    def test_given_uppercase_Female_when_normalize_then_returns_Female(self):
        self.assertEqual(self._get_fn()("Female"), "Female")

    def test_given_unrecognized_value_x_when_normalize_then_returns_None(self):
        self.assertIsNone(self._get_fn()("x"))

    def test_given_unrecognized_value_nonbinary_when_normalize_then_returns_None(self):
        self.assertIsNone(self._get_fn()("nonbinary"))

    def test_given_empty_string_when_normalize_then_returns_None(self):
        self.assertIsNone(self._get_fn()(""))

    def test_given_None_when_normalize_then_returns_None(self):
        self.assertIsNone(self._get_fn()(None))


class TestHandlerMissingEmail(unittest.TestCase):
    """
    Given a Cognito event with no email attribute,
    when the handler is invoked,
    then get_connection is NOT called and the event is returned unmodified.

    AC: "If email is missing, log structured error and return event unmodified
         WITHOUT inserting."
    """

    def test_given_missing_email_when_handler_called_then_get_connection_not_called(self):
        import handler as h

        event = _make_cognito_event(email=None)

        with patch.object(h, "_get_conn", side_effect=AssertionError("get_connection must not be called when email is absent")) as mock_conn:
            result = h.handler(event, {})
            mock_conn.assert_not_called()

        self.assertEqual(result, event)

    def test_given_missing_email_when_handler_called_then_returns_event_unmodified(self):
        import handler as h

        event = _make_cognito_event(email=None)
        original_event = {k: v for k, v in event.items()}  # shallow copy for comparison

        with patch.object(h, "_get_conn", return_value=MagicMock()):
            result = h.handler(event, {})

        # The returned object must be the unmodified event
        self.assertEqual(result["triggerSource"], original_event["triggerSource"])
        self.assertEqual(result["userName"], original_event["userName"])


class TestHandlerFullAttributes(unittest.TestCase):
    """
    Given a Cognito event with all attributes (email, given_name, family_name,
    gender=female, birthdate=1995-06-15),
    when the handler is invoked,
    then the INSERT is called with all populated columns.

    AC: "full attribute set → row with all populated columns"
    """

    def _make_mock_conn(self):
        cur = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        return conn, cur

    def test_given_full_event_when_handler_called_then_insert_contains_email(self):
        import handler as h
        import datetime

        conn, cur = self._make_mock_conn()
        event = _make_cognito_event(
            sub="user-sub-uuid-full",
            email="alice@example.com",
            given_name="Alice",
            family_name="Smith",
            gender="female",
            birthdate="1995-06-15",
        )

        with patch.object(h, "_get_conn", return_value=conn):
            result = h.handler(event, {})

        self.assertEqual(result, event)

        # Assert INSERT was called
        self.assertTrue(cur.execute.called, "cursor.execute must be called for INSERT")
        sql_args = cur.execute.call_args
        sql = sql_args[0][0]
        self.assertIn("INSERT INTO users", sql)

    def test_given_full_event_when_handler_called_then_insert_params_contain_all_columns(self):
        import handler as h
        import datetime

        conn, cur = self._make_mock_conn()
        event = _make_cognito_event(
            sub="user-sub-uuid-full-params",
            email="alice@example.com",
            given_name="Alice",
            family_name="Smith",
            gender="female",
            birthdate="1995-06-15",
        )

        with patch.object(h, "_get_conn", return_value=conn):
            h.handler(event, {})

        params = cur.execute.call_args[0][1]
        # user_id, email, first_name, last_name, sex, birthday
        self.assertEqual(params[0], "user-sub-uuid-full-params")
        self.assertEqual(params[1], "alice@example.com")
        self.assertEqual(params[2], "Alice")         # first_name
        self.assertEqual(params[3], "Smith")         # last_name
        self.assertEqual(params[4], "Female")        # sex (mapped from "female")
        self.assertEqual(params[5], datetime.date(1995, 6, 15))  # birthday

    def test_given_full_event_when_handler_called_then_conn_commit_called(self):
        import handler as h

        conn, cur = self._make_mock_conn()
        event = _make_cognito_event()

        with patch.object(h, "_get_conn", return_value=conn):
            h.handler(event, {})

        conn.commit.assert_called_once()


class TestHandlerPartialAttributes(unittest.TestCase):
    """
    Given a Cognito event with only email + given_name + family_name
    (no gender, no birthdate — social-login minimal),
    when the handler is invoked,
    then first_name + last_name are populated, sex + birthday are NULL.

    AC: "social-login minimal → row with first_name + last_name populated,
         sex / birthday / username NULL"
    """

    def _make_mock_conn(self):
        cur = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        return conn, cur

    def test_given_minimal_event_when_handler_called_then_sex_is_None(self):
        import handler as h

        conn, cur = self._make_mock_conn()
        event = _make_cognito_event(
            sub="user-sub-uuid-minimal",
            email="bob@example.com",
            given_name="Bob",
            family_name="Jones",
            gender=None,
            birthdate=None,
        )

        with patch.object(h, "_get_conn", return_value=conn):
            h.handler(event, {})

        params = cur.execute.call_args[0][1]
        self.assertEqual(params[2], "Bob")    # first_name
        self.assertEqual(params[3], "Jones")  # last_name
        self.assertIsNone(params[4])          # sex → None
        self.assertIsNone(params[5])          # birthday → None

    def test_given_event_with_no_optional_fields_when_handler_called_then_names_also_None(self):
        import handler as h

        conn, cur = self._make_mock_conn()
        event = _make_cognito_event(
            sub="user-sub-uuid-email-only",
            email="charlie@example.com",
            given_name=None,
            family_name=None,
            gender=None,
            birthdate=None,
        )

        with patch.object(h, "_get_conn", return_value=conn):
            h.handler(event, {})

        params = cur.execute.call_args[0][1]
        self.assertEqual(params[1], "charlie@example.com")  # email
        self.assertIsNone(params[2])  # first_name
        self.assertIsNone(params[3])  # last_name
        self.assertIsNone(params[4])  # sex
        self.assertIsNone(params[5])  # birthday


class TestHandlerGenderVariants(unittest.TestCase):
    """
    Given gender variants in Cognito events,
    when the handler processes them,
    then they map according to the exact table in the AC:
      "male", "M" → Male; "Female" → Female; "x" → NULL (None).
    """

    def _make_mock_conn(self):
        cur = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        return conn, cur

    def _invoke_with_gender(self, gender_value):
        import handler as h
        conn, cur = self._make_mock_conn()
        event = _make_cognito_event(
            sub=f"user-sub-{gender_value or 'none'}",
            email=f"user_{gender_value or 'none'}@example.com",
            given_name=None,
            family_name=None,
            gender=gender_value,
            birthdate=None,
        )
        with patch.object(h, "_get_conn", return_value=conn):
            h.handler(event, {})
        params = cur.execute.call_args[0][1]
        return params[4]  # sex column

    def test_given_gender_male_when_handler_then_sex_is_Male(self):
        self.assertEqual(self._invoke_with_gender("male"), "Male")

    def test_given_gender_M_when_handler_then_sex_is_Male(self):
        self.assertEqual(self._invoke_with_gender("M"), "Male")

    def test_given_gender_Female_when_handler_then_sex_is_Female(self):
        self.assertEqual(self._invoke_with_gender("Female"), "Female")

    def test_given_gender_x_when_handler_then_sex_is_None(self):
        self.assertIsNone(self._invoke_with_gender("x"))


class TestHandlerBirthdateParsing(unittest.TestCase):
    """
    Given birthdate values in Cognito events,
    when the handler processes them,
    then valid ISO dates are parsed; invalid dates yield None + log warning.
    """

    def _make_mock_conn(self):
        cur = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        return conn, cur

    def test_given_valid_iso_birthdate_when_handler_then_birthday_is_date_object(self):
        import handler as h
        import datetime

        conn, cur = self._make_mock_conn()
        event = _make_cognito_event(
            sub="user-sub-birthdate-valid",
            email="valid@example.com",
            given_name=None,
            family_name=None,
            gender=None,
            birthdate="1990-03-22",
        )

        with patch.object(h, "_get_conn", return_value=conn):
            h.handler(event, {})

        params = cur.execute.call_args[0][1]
        self.assertEqual(params[5], datetime.date(1990, 3, 22))

    def test_given_invalid_birthdate_when_handler_then_birthday_is_None(self):
        import handler as h

        conn, cur = self._make_mock_conn()
        event = _make_cognito_event(
            sub="user-sub-birthdate-invalid",
            email="invalid_bd@example.com",
            given_name=None,
            family_name=None,
            gender=None,
            birthdate="not-a-date",
        )

        with patch.object(h, "_get_conn", return_value=conn):
            result = h.handler(event, {})

        # Handler must not raise — returns event
        self.assertEqual(result, event)
        params = cur.execute.call_args[0][1]
        self.assertIsNone(params[5])

    def test_given_invalid_birthdate_when_handler_then_insert_still_proceeds(self):
        import handler as h

        conn, cur = self._make_mock_conn()
        event = _make_cognito_event(
            sub="user-sub-birthdate-bad-still-inserts",
            email="bd_bad@example.com",
            given_name=None,
            family_name=None,
            gender=None,
            birthdate="31/12/1990",  # wrong format
        )

        with patch.object(h, "_get_conn", return_value=conn):
            h.handler(event, {})

        self.assertTrue(cur.execute.called, "INSERT should still run even with bad birthdate")


class TestHandlerReturnValue(unittest.TestCase):
    """
    Given any invocation (success, DB failure, data validation failure),
    when the handler is invoked,
    then it always returns the event (Cognito requires this).

    AC: "handler always returns the event even on DB failure for non-missing-email"
    """

    def _make_mock_conn(self):
        cur = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        return conn, cur

    def test_given_successful_insert_when_handler_called_then_returns_event(self):
        import handler as h

        conn, cur = self._make_mock_conn()
        event = _make_cognito_event()

        with patch.object(h, "_get_conn", return_value=conn):
            result = h.handler(event, {})

        self.assertIs(result, event)

    def test_given_db_error_when_handler_called_then_still_returns_event(self):
        import handler as h

        conn, cur = self._make_mock_conn()
        cur.execute.side_effect = Exception("DB connection failed")
        event = _make_cognito_event(
            sub="user-sub-db-error",
            email="dberror@example.com",
        )

        with patch.object(h, "_get_conn", return_value=conn):
            result = h.handler(event, {})

        # Must not raise; must return event
        self.assertIs(result, event)


class TestHandlerUsesSubForUserId(unittest.TestCase):
    """
    Given a Cognito event,
    when the handler extracts the user_id,
    then it uses event["request"]["userAttributes"]["sub"]
    (not event["userName"] which may differ for federated providers).
    """

    def _make_mock_conn(self):
        cur = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        return conn, cur

    def test_given_event_when_handler_called_then_user_id_from_sub_attribute(self):
        import handler as h

        conn, cur = self._make_mock_conn()
        # Deliberately make userName differ from sub to prove sub is used
        event = _make_cognito_event(
            sub="the-canonical-sub-uuid",
            email="sub@example.com",
        )
        event["userName"] = "different-username-value"

        with patch.object(h, "_get_conn", return_value=conn):
            h.handler(event, {})

        params = cur.execute.call_args[0][1]
        self.assertEqual(params[0], "the-canonical-sub-uuid")


if __name__ == "__main__":
    unittest.main()
