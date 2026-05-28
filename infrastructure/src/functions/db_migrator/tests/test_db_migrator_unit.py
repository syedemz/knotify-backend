"""
Unit tests for the db_migrator Lambda handler.

All AWS calls (boto3 SecretsManager) are mocked at the module boundary.
yoyo backend is mocked to avoid needing a real database.

Import strategy: this test file loads db_migrator/handler.py via
conftest._load_handler() to avoid sys.path conflicts with the
cognito_post_confirmation handler (both are named handler.py).

Story 3.7 ACs covered:
  - Handler reads AURORA_MASTER_SECRET_ARN env var
  - Handler fetches master secret via boto3 and parses the JSON
  - Handler builds a DB URL and calls yoyo apply
  - Handler generates a 32-char random password
  - Handler writes app_user credential to Secrets Manager:
      CreateSecret on first run (secret_action = "created")
      PutSecretValue on subsequent runs (secret_action = "updated")
  - Handler runs ALTER ROLE with the generated password
  - Handler returns structured JSON (applied migration ids, pending_count,
    app_user_secret_action, elapsed_time_seconds)
  - M-new-3: exception-based CreateSecret/PutSecretValue idempotency
  - M-new-3: both code paths (created / updated) are exercised
"""

import importlib.util
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, call, patch

import pytest


# ---------------------------------------------------------------------------
# Handler loader — loads db_migrator/handler.py by absolute path to avoid
# sys.path conflicts with cognito_post_confirmation/handler.py (both are
# named handler.py and live on sys.path when pytest runs infrastructure/src/).
# ---------------------------------------------------------------------------

def _load_handler():
    """Load db_migrator/handler.py by absolute file path."""
    _here = os.path.dirname(os.path.abspath(__file__))
    handler_path = os.path.normpath(os.path.join(_here, "..", "handler.py"))
    spec = importlib.util.spec_from_file_location("db_migrator_handler", handler_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_master_secret(
    host: str = "db.example.com",
    port: int = 5432,
    dbname: str = "knotify",
    username: str = "admin",
    password: str = "secret",
) -> str:
    return json.dumps({
        "host": host,
        "port": port,
        "dbname": dbname,
        "username": username,
        "password": password,
    })


def _make_env(
    master_arn: str = "arn:aws:secretsmanager:eu-central-1:123:secret:master",
    app_user_secret_name: str = "knotify-dev-app-user-credential",
) -> dict:
    return {
        "AURORA_MASTER_SECRET_ARN": master_arn,
        "APP_USER_SECRET_NAME": app_user_secret_name,
    }


# ---------------------------------------------------------------------------
# Structured response shape
# ---------------------------------------------------------------------------

class TestHandlerResponseShape(unittest.TestCase):
    """
    Given a Lambda invocation with valid environment and mocked AWS calls,
    when the handler is invoked,
    then the response is a dict with the required keys.
    """

    def _invoke_handler(self, sm_mock, master_arn: str, app_secret_name: str):
        sm_mock.get_secret_value.return_value = {
            "SecretString": _make_master_secret()
        }
        # CreateSecret succeeds on first run
        sm_mock.create_secret.return_value = {}

        h = _load_handler()
        mock_conn = MagicMock()

        with patch.object(h, "boto3") as mock_boto3:
            mock_boto3.client.return_value = sm_mock
            with patch.object(h, "psycopg2") as mock_psycopg2:
                mock_psycopg2.connect.return_value = mock_conn
                with patch.object(h, "_apply_migrations") as mock_apply:
                    mock_apply.return_value = {"applied_ids": ["0001_init"], "pending_count": 0}
                    with patch.object(h, "_alter_role_password") as mock_alter:
                        mock_alter.return_value = None
                        h._MASTER_SECRET_ARN = master_arn
                        h._APP_USER_SECRET_NAME = app_secret_name
                        result = h.handler({}, None)
        return result

    def test_response_contains_applied_migration_ids(self):
        sm = MagicMock()
        result = self._invoke_handler(sm, "arn:master", "knotify-dev-app-user-credential")
        self.assertIn("applied_migration_ids", result)

    def test_response_contains_pending_count(self):
        sm = MagicMock()
        result = self._invoke_handler(sm, "arn:master", "knotify-dev-app-user-credential")
        self.assertIn("pending_migrations", result)

    def test_response_contains_app_user_secret_action(self):
        sm = MagicMock()
        result = self._invoke_handler(sm, "arn:master", "knotify-dev-app-user-credential")
        self.assertIn("app_user_secret_action", result)

    def test_response_contains_elapsed_time(self):
        sm = MagicMock()
        result = self._invoke_handler(sm, "arn:master", "knotify-dev-app-user-credential")
        self.assertIn("elapsed_time_seconds", result)


# ---------------------------------------------------------------------------
# Password generation
# ---------------------------------------------------------------------------

class TestPasswordGeneration(unittest.TestCase):
    """
    Given the _generate_password helper,
    when called,
    then it returns a cryptographically-random 32-char string.
    """

    def test_password_is_32_chars(self):
        h = _load_handler()
        pwd = h._generate_password()
        self.assertEqual(len(pwd), 32, f"Expected 32 chars, got {len(pwd)}: {pwd!r}")

    def test_password_is_non_empty_string(self):
        h = _load_handler()
        pwd = h._generate_password()
        self.assertIsInstance(pwd, str)
        self.assertTrue(pwd)

    def test_two_consecutive_passwords_are_different(self):
        h = _load_handler()
        pwd1 = h._generate_password()
        pwd2 = h._generate_password()
        self.assertNotEqual(
            pwd1, pwd2,
            "Two consecutive random passwords should not be identical",
        )


# ---------------------------------------------------------------------------
# Secrets Manager idempotency (M-new-3)
# ---------------------------------------------------------------------------

class TestSecretsManagerIdempotency(unittest.TestCase):
    """
    Given the _write_app_user_secret helper,
    when CreateSecret raises ResourceExistsException,
    then PutSecretValue is called and secret_action = "updated".
    When CreateSecret succeeds,
    then secret_action = "created" and PutSecretValue is NOT called.
    """

    def test_given_first_run_when_create_succeeds_then_action_is_created(self):
        h = _load_handler()
        sm = MagicMock()
        sm.create_secret.return_value = {}

        action = h._write_app_user_secret(
            sm, "knotify-dev-app-user-credential", "random_password_32chars_______xx"
        )

        sm.create_secret.assert_called_once()
        sm.put_secret_value.assert_not_called()
        self.assertEqual(action, "created")

    def test_given_subsequent_run_when_create_raises_resource_exists_then_action_is_updated(self):
        h = _load_handler()
        sm = MagicMock()
        # Simulate ResourceExistsException
        sm.exceptions.ResourceExistsException = type(
            "ResourceExistsException", (Exception,), {}
        )
        sm.create_secret.side_effect = sm.exceptions.ResourceExistsException("exists")

        action = h._write_app_user_secret(
            sm, "knotify-dev-app-user-credential", "random_password_32chars_______xx"
        )

        sm.create_secret.assert_called_once()
        sm.put_secret_value.assert_called_once()
        self.assertEqual(action, "updated")

    def test_given_first_run_secret_name_passed_to_create_secret(self):
        h = _load_handler()
        sm = MagicMock()
        sm.create_secret.return_value = {}

        h._write_app_user_secret(
            sm, "knotify-dev-app-user-credential", "mypassword_32_chars_xxxxxxxxxxxxx"
        )

        call_kwargs = sm.create_secret.call_args
        all_args = str(call_kwargs)
        self.assertIn("knotify-dev-app-user-credential", all_args)

    def test_given_subsequent_run_put_secret_value_called_with_new_password(self):
        h = _load_handler()
        sm = MagicMock()
        sm.exceptions.ResourceExistsException = type(
            "ResourceExistsException", (Exception,), {}
        )
        sm.create_secret.side_effect = sm.exceptions.ResourceExistsException("exists")

        password = "newpassword_32chars_xxxxxxxxxxxx"
        h._write_app_user_secret(sm, "knotify-dev-app-user-credential", password)

        put_args = str(sm.put_secret_value.call_args)
        self.assertIn(password, put_args)


# ---------------------------------------------------------------------------
# ALTER ROLE invocation
# ---------------------------------------------------------------------------

class TestAlterRolePassword(unittest.TestCase):
    """
    Given the _alter_role_password helper,
    when called with a connection and password,
    then it executes ALTER ROLE app_user WITH PASSWORD '<password>'.
    """

    def test_alter_role_executes_correct_sql(self):
        h = _load_handler()

        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        password = "testpassword_32chars_xxxxxxxxxxx"
        h._alter_role_password(conn, password)

        executed_sqls = [str(c.args[0]) for c in cur.execute.call_args_list]
        alter_found = any("ALTER ROLE" in sql.upper() for sql in executed_sqls)
        self.assertTrue(alter_found, f"Expected ALTER ROLE in executed SQL. Got: {executed_sqls}")

    def test_alter_role_uses_app_user(self):
        h = _load_handler()

        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        h._alter_role_password(conn, "somepassword_32chars_xxxxxxxxxxx")

        executed_sqls = [str(c.args[0]) for c in cur.execute.call_args_list]
        app_user_found = any("app_user" in sql.lower() for sql in executed_sqls)
        self.assertTrue(
            app_user_found,
            f"Expected 'app_user' in ALTER ROLE SQL. Got: {executed_sqls}",
        )

    def test_alter_role_commits(self):
        h = _load_handler()

        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        h._alter_role_password(conn, "somepassword_32chars_xxxxxxxxxxx")

        conn.commit.assert_called_once()


# ---------------------------------------------------------------------------
# AURORA_MASTER_SECRET_ARN env var
# ---------------------------------------------------------------------------

class TestMasterSecretEnvVar(unittest.TestCase):
    """
    Given the handler reads AURORA_MASTER_SECRET_ARN from the environment,
    when the env var is set,
    then the handler calls boto3 GetSecretValue with that ARN.
    """

    def test_handler_calls_get_secret_value_with_master_arn(self):
        master_arn = "arn:aws:secretsmanager:eu-central-1:123456789:secret:rds!cluster-xxx"
        app_secret_name = "knotify-dev-app-user-credential"

        h = _load_handler()
        sm = MagicMock()
        sm.get_secret_value.return_value = {"SecretString": _make_master_secret()}
        sm.create_secret.return_value = {}

        mock_conn = MagicMock()

        with patch.object(h, "boto3") as mock_boto3:
            mock_boto3.client.return_value = sm
            with patch.object(h, "psycopg2") as mock_psycopg2:
                mock_psycopg2.connect.return_value = mock_conn
                with patch.object(h, "_apply_migrations") as mock_apply:
                    mock_apply.return_value = {"applied_ids": [], "pending_count": 0}
                    with patch.object(h, "_alter_role_password"):
                        h._MASTER_SECRET_ARN = master_arn
                        h._APP_USER_SECRET_NAME = app_secret_name
                        h.handler({}, None)

        sm.get_secret_value.assert_called_with(SecretId=master_arn)


# ---------------------------------------------------------------------------
# Migrations path resolution
# ---------------------------------------------------------------------------

class TestMigrationsPath(unittest.TestCase):
    """
    Given the _get_migrations_path helper,
    when called,
    then it returns the 'migrations' subdirectory relative to handler.py's
    __file__ — this is where build_package.py places the bundled .sql files.
    """

    def test_migrations_path_points_to_migrations_subdir(self):
        h = _load_handler()
        path = h._get_migrations_path()
        self.assertTrue(
            str(path).endswith("migrations"),
            f"Expected path ending in 'migrations', got: {path}",
        )

    def test_migrations_path_is_relative_to_handler_file(self):
        h = _load_handler()
        import os as _os
        handler_dir = _os.path.dirname(_os.path.abspath(h.__file__))
        expected = _os.path.join(handler_dir, "migrations")
        actual = str(h._get_migrations_path())
        self.assertEqual(_os.path.normpath(actual), _os.path.normpath(expected))


if __name__ == "__main__":
    unittest.main()
