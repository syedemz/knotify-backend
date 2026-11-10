"""
Unit tests for the db_migrator Lambda handler — Supabase path.

Covers all Supabase-specific behaviour introduced in story 14.13:
  - USE_SUPABASE=true causes the handler to fetch DB_SECRET_ARN (not
    AURORA_MASTER_SECRET_ARN) and connect to SUPABASE_HOST:SUPABASE_PORT
    with sslmode="require".
  - Project ref is extracted from the superuser username by splitting on
    '.' and taking the second segment.
  - Malformed usernames (no dot, more than one dot) raise ValueError.
  - The written app_user secret's username is "app_user.<ref>".
  - The written aurora_refresh secret's username is "aurora_refresh.<ref>".
  - The yoyo db_url is well-formed and contains "sslmode=require" as a
    query param.
  - With USE_SUPABASE unset / "false", the Aurora path runs (regression guard).
  - The response shape is identical to the Aurora path.

All AWS calls (boto3 SecretsManager) and psycopg2 are mocked.
No real Supabase or AWS call is made in this suite.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, call, patch
from urllib.parse import urlparse, parse_qs


# ---------------------------------------------------------------------------
# Handler loader (same pattern as test_db_migrator_unit.py)
# ---------------------------------------------------------------------------

def _load_handler():
    """Load db_migrator/handler.py by absolute file path."""
    _here = os.path.dirname(os.path.abspath(__file__))
    handler_path = os.path.normpath(os.path.join(_here, "..", "handler.py"))
    spec = importlib.util.spec_from_file_location("db_migrator_handler_supabase", handler_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_PROJECT_REF = "waulcjgqytiqkwdrkchg"
_SUPERUSER_USERNAME = f"postgres.{_PROJECT_REF}"
_SUPABASE_HOST = "aws-0-eu-central-1.pooler.supabase.com"
_SUPABASE_PORT = "6543"
_SUPABASE_DBNAME = "postgres"
_DB_SECRET_ARN = "arn:aws:secretsmanager:eu-central-1:123:secret:knotify/dev/supabase_credential-OydnRq"
_APP_USER_SECRET_NAME = "knotify-dev-app-user-credential"
_AURORA_REFRESH_SECRET_NAME = "knotify-dev-aurora-refresh-credential"


def _make_supabase_secret() -> str:
    """Return JSON payload for the Supabase superuser secret."""
    return json.dumps({"username": _SUPERUSER_USERNAME, "password": "superpassword"})


def _set_supabase_env(h) -> None:
    """Point the handler's module-level env-var handles at Supabase values."""
    h._DB_SECRET_ARN = _DB_SECRET_ARN
    h._SUPABASE_HOST = _SUPABASE_HOST
    h._SUPABASE_PORT = _SUPABASE_PORT
    h._SUPABASE_DBNAME = _SUPABASE_DBNAME
    h._APP_USER_SECRET_NAME = _APP_USER_SECRET_NAME
    h._AURORA_REFRESH_SECRET_NAME = _AURORA_REFRESH_SECRET_NAME
    h._USE_SUPABASE = True


def _invoke_supabase_handler(h, sm_mock=None):
    """
    Call h.handler({}, None) with the Supabase path fully mocked.
    Returns the handler's return value.
    """
    if sm_mock is None:
        sm_mock = MagicMock()

    sm_mock.get_secret_value.return_value = {"SecretString": _make_supabase_secret()}
    sm_mock.create_secret.return_value = {}

    mock_conn = MagicMock()
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = None  # roles don't exist yet
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    _set_supabase_env(h)

    with patch.object(h, "boto3") as mock_boto3:
        mock_boto3.client.return_value = sm_mock
        with patch.object(h, "psycopg2") as mock_psycopg2:
            mock_psycopg2.connect.return_value = mock_conn
            with patch.object(h, "_apply_migrations") as mock_apply:
                mock_apply.return_value = {"applied_ids": ["0000_init"], "pending_count": 0}
                result = h.handler({}, None)

    return result


# ---------------------------------------------------------------------------
# USE_SUPABASE routing — handler fetches DB_SECRET_ARN, not AURORA_MASTER_SECRET_ARN
# ---------------------------------------------------------------------------

class TestSupabaseSecretFetch(unittest.TestCase):
    """
    Given USE_SUPABASE=true,
    when the handler runs,
    then it fetches DB_SECRET_ARN (the Supabase credential) and NOT
    AURORA_MASTER_SECRET_ARN.
    """

    def test_given_use_supabase_true_handler_fetches_db_secret_arn(self):
        h = _load_handler()
        sm = MagicMock()
        sm.get_secret_value.return_value = {"SecretString": _make_supabase_secret()}
        sm.create_secret.return_value = {}

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        _set_supabase_env(h)

        with patch.object(h, "boto3") as mock_boto3:
            mock_boto3.client.return_value = sm
            with patch.object(h, "psycopg2") as mock_psycopg2:
                mock_psycopg2.connect.return_value = mock_conn
                with patch.object(h, "_apply_migrations") as mock_apply:
                    mock_apply.return_value = {"applied_ids": [], "pending_count": 0}
                    h.handler({}, None)

        # Must have called GetSecretValue with the Supabase ARN
        sm.get_secret_value.assert_called_once_with(SecretId=_DB_SECRET_ARN)

    def test_given_use_supabase_false_handler_fetches_aurora_master_arn(self):
        """Aurora regression guard: USE_SUPABASE=false → old AURORA path."""
        h = _load_handler()
        aurora_arn = "arn:aws:secretsmanager:eu-central-1:123:secret:rds!cluster-xxx"
        h._USE_SUPABASE = False
        h._MASTER_SECRET_ARN = aurora_arn
        h._APP_USER_SECRET_NAME = _APP_USER_SECRET_NAME
        h._AURORA_REFRESH_SECRET_NAME = _AURORA_REFRESH_SECRET_NAME
        h._AURORA_HOST = "db.example.com"
        h._AURORA_PORT = "5432"
        h._AURORA_DBNAME = "knotify"

        sm = MagicMock()
        sm.get_secret_value.return_value = {
            "SecretString": json.dumps({"username": "admin", "password": "secret"})
        }
        sm.create_secret.return_value = {}
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        with patch.object(h, "boto3") as mock_boto3:
            mock_boto3.client.return_value = sm
            with patch.object(h, "psycopg2") as mock_psycopg2:
                mock_psycopg2.connect.return_value = mock_conn
                with patch.object(h, "_apply_migrations") as mock_apply:
                    mock_apply.return_value = {"applied_ids": [], "pending_count": 0}
                    with patch.object(h, "_alter_role_password"):
                        h.handler({}, None)

        sm.get_secret_value.assert_called_once_with(SecretId=aurora_arn)


# ---------------------------------------------------------------------------
# psycopg2.connect call — Supabase path must use SUPABASE_HOST:PORT + sslmode=require
# ---------------------------------------------------------------------------

class TestSupabaseConnection(unittest.TestCase):
    """
    Given USE_SUPABASE=true,
    when the handler connects to the database,
    then psycopg2.connect is called with host=SUPABASE_HOST, port=SUPABASE_PORT
    (as int), and sslmode="require".
    """

    def test_given_use_supabase_true_connect_uses_supabase_host_and_port(self):
        h = _load_handler()
        sm = MagicMock()
        sm.get_secret_value.return_value = {"SecretString": _make_supabase_secret()}
        sm.create_secret.return_value = {}

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        _set_supabase_env(h)

        with patch.object(h, "boto3") as mock_boto3:
            mock_boto3.client.return_value = sm
            with patch.object(h, "psycopg2") as mock_psycopg2:
                mock_psycopg2.connect.return_value = mock_conn
                with patch.object(h, "_apply_migrations") as mock_apply:
                    mock_apply.return_value = {"applied_ids": [], "pending_count": 0}
                    h.handler({}, None)

        connect_kwargs = mock_psycopg2.connect.call_args.kwargs
        self.assertEqual(connect_kwargs["host"], _SUPABASE_HOST)
        self.assertEqual(connect_kwargs["port"], int(_SUPABASE_PORT))
        self.assertEqual(connect_kwargs["sslmode"], "require")

    def test_given_use_supabase_true_connect_uses_dbname_from_env(self):
        h = _load_handler()
        sm = MagicMock()
        sm.get_secret_value.return_value = {"SecretString": _make_supabase_secret()}
        sm.create_secret.return_value = {}

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        _set_supabase_env(h)

        with patch.object(h, "boto3") as mock_boto3:
            mock_boto3.client.return_value = sm
            with patch.object(h, "psycopg2") as mock_psycopg2:
                mock_psycopg2.connect.return_value = mock_conn
                with patch.object(h, "_apply_migrations") as mock_apply:
                    mock_apply.return_value = {"applied_ids": [], "pending_count": 0}
                    h.handler({}, None)

        connect_kwargs = mock_psycopg2.connect.call_args.kwargs
        self.assertEqual(connect_kwargs["dbname"], _SUPABASE_DBNAME)


# ---------------------------------------------------------------------------
# Project ref extraction — _extract_project_ref
# ---------------------------------------------------------------------------

class TestExtractProjectRef(unittest.TestCase):
    """
    Given _extract_project_ref(username),
    when username is "postgres.waulcjgqytiqkwdrkchg",
    then it returns "waulcjgqytiqkwdrkchg".

    When username has no dot, raises ValueError.
    When username has more than one dot, raises ValueError.
    """

    def test_given_valid_username_returns_project_ref(self):
        h = _load_handler()
        ref = h._extract_project_ref(f"postgres.{_PROJECT_REF}")
        self.assertEqual(ref, _PROJECT_REF)

    def test_given_username_with_no_dot_raises_value_error(self):
        h = _load_handler()
        with self.assertRaises(ValueError):
            h._extract_project_ref("postgresNoDotHere")

    def test_given_username_with_two_dots_raises_value_error(self):
        h = _load_handler()
        with self.assertRaises(ValueError):
            h._extract_project_ref("postgres.extra.segment")

    def test_given_username_ref_is_the_segment_after_dot(self):
        """Extra precision: ref is the part after the dot, not before."""
        h = _load_handler()
        ref = h._extract_project_ref("postgres.abcdef123")
        self.assertNotEqual(ref, "postgres")
        self.assertEqual(ref, "abcdef123")


# ---------------------------------------------------------------------------
# app_user secret username is pooler-qualified
# ---------------------------------------------------------------------------

class TestAppUserSecretPoolerUsername(unittest.TestCase):
    """
    Given USE_SUPABASE=true,
    when the handler writes the app_user Secrets Manager secret,
    then the username field is "app_user.<project_ref>" (pooler-qualified).
    """

    def test_app_user_secret_username_is_pooler_qualified(self):
        h = _load_handler()
        sm = MagicMock()
        sm.get_secret_value.return_value = {"SecretString": _make_supabase_secret()}
        sm.create_secret.return_value = {}

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        _set_supabase_env(h)

        with patch.object(h, "boto3") as mock_boto3:
            mock_boto3.client.return_value = sm
            with patch.object(h, "psycopg2") as mock_psycopg2:
                mock_psycopg2.connect.return_value = mock_conn
                with patch.object(h, "_apply_migrations") as mock_apply:
                    mock_apply.return_value = {"applied_ids": [], "pending_count": 0}
                    h.handler({}, None)

        # Find the create_secret or put_secret_value call for the app_user secret
        all_create_calls = sm.create_secret.call_args_list
        app_user_call = None
        for c in all_create_calls:
            args_str = str(c)
            if _APP_USER_SECRET_NAME in args_str:
                app_user_call = c
                break

        self.assertIsNotNone(app_user_call, "Expected a create_secret call for app_user secret")
        secret_string_arg = None
        # SecretString may be in kwargs or positional; search the call representation
        for _, kwargs in [app_user_call]:
            secret_string_arg = kwargs.get("SecretString", None)

        if secret_string_arg is None:
            # Try parsing from string representation
            args_str = str(app_user_call)
            # The call will contain SecretString='{"username": "app_user.waulcjgq...", ...}'
            self.assertIn(f'"app_user.{_PROJECT_REF}"', args_str)
        else:
            payload = json.loads(secret_string_arg)
            self.assertEqual(payload["username"], f"app_user.{_PROJECT_REF}")


# ---------------------------------------------------------------------------
# aurora_refresh secret username is pooler-qualified
# ---------------------------------------------------------------------------

class TestAuroraRefreshSecretPoolerUsername(unittest.TestCase):
    """
    Given USE_SUPABASE=true,
    when the handler writes the aurora_refresh Secrets Manager secret,
    then the username field is "aurora_refresh.<project_ref>" (pooler-qualified).
    """

    def test_aurora_refresh_secret_username_is_pooler_qualified(self):
        h = _load_handler()
        sm = MagicMock()
        sm.get_secret_value.return_value = {"SecretString": _make_supabase_secret()}
        sm.create_secret.return_value = {}

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        _set_supabase_env(h)

        with patch.object(h, "boto3") as mock_boto3:
            mock_boto3.client.return_value = sm
            with patch.object(h, "psycopg2") as mock_psycopg2:
                mock_psycopg2.connect.return_value = mock_conn
                with patch.object(h, "_apply_migrations") as mock_apply:
                    mock_apply.return_value = {"applied_ids": [], "pending_count": 0}
                    h.handler({}, None)

        all_create_calls = sm.create_secret.call_args_list
        refresh_call = None
        for c in all_create_calls:
            args_str = str(c)
            if _AURORA_REFRESH_SECRET_NAME in args_str:
                refresh_call = c
                break

        self.assertIsNotNone(refresh_call, "Expected a create_secret call for aurora_refresh secret")
        args_str = str(refresh_call)
        self.assertIn(f'"aurora_refresh.{_PROJECT_REF}"', args_str)


# ---------------------------------------------------------------------------
# yoyo db_url contains sslmode=require
# ---------------------------------------------------------------------------

class TestYoyoDbUrlSupabase(unittest.TestCase):
    """
    Given USE_SUPABASE=true,
    when the handler calls _apply_migrations,
    then the db_url passed to it includes sslmode=require as a query param.
    """

    def test_yoyo_db_url_contains_sslmode_require(self):
        h = _load_handler()
        sm = MagicMock()
        sm.get_secret_value.return_value = {"SecretString": _make_supabase_secret()}
        sm.create_secret.return_value = {}

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        _set_supabase_env(h)
        captured_urls: list[str] = []

        def capture_apply(db_url, migrations_path):
            captured_urls.append(db_url)
            return {"applied_ids": [], "pending_count": 0}

        with patch.object(h, "boto3") as mock_boto3:
            mock_boto3.client.return_value = sm
            with patch.object(h, "psycopg2") as mock_psycopg2:
                mock_psycopg2.connect.return_value = mock_conn
                with patch.object(h, "_apply_migrations", side_effect=capture_apply):
                    h.handler({}, None)

        self.assertEqual(len(captured_urls), 1, "Expected exactly one _apply_migrations call")
        db_url = captured_urls[0]
        parsed = urlparse(db_url)
        query_params = parse_qs(parsed.query)
        self.assertIn(
            "sslmode", query_params,
            f"Expected 'sslmode' query param in db_url. Got: {db_url}",
        )
        self.assertEqual(
            query_params["sslmode"], ["require"],
            f"Expected sslmode=require. Got: {query_params['sslmode']}",
        )

    def test_yoyo_db_url_uses_supabase_host_and_port(self):
        h = _load_handler()
        sm = MagicMock()
        sm.get_secret_value.return_value = {"SecretString": _make_supabase_secret()}
        sm.create_secret.return_value = {}

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = None
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        _set_supabase_env(h)
        captured_urls: list[str] = []

        def capture_apply(db_url, migrations_path):
            captured_urls.append(db_url)
            return {"applied_ids": [], "pending_count": 0}

        with patch.object(h, "boto3") as mock_boto3:
            mock_boto3.client.return_value = sm
            with patch.object(h, "psycopg2") as mock_psycopg2:
                mock_psycopg2.connect.return_value = mock_conn
                with patch.object(h, "_apply_migrations", side_effect=capture_apply):
                    h.handler({}, None)

        db_url = captured_urls[0]
        parsed = urlparse(db_url)
        self.assertEqual(parsed.hostname, _SUPABASE_HOST)
        self.assertEqual(parsed.port, int(_SUPABASE_PORT))


# ---------------------------------------------------------------------------
# Response shape matches Aurora path
# ---------------------------------------------------------------------------

class TestSupabaseResponseShape(unittest.TestCase):
    """
    Given USE_SUPABASE=true,
    when the handler runs successfully,
    then the response dict has the same five keys as the Aurora path:
    applied_migration_ids, pending_migrations, app_user_secret_action,
    aurora_refresh_secret_action, elapsed_time_seconds.
    """

    def test_supabase_response_has_applied_migration_ids(self):
        h = _load_handler()
        result = _invoke_supabase_handler(h)
        self.assertIn("applied_migration_ids", result)

    def test_supabase_response_has_pending_migrations(self):
        h = _load_handler()
        result = _invoke_supabase_handler(h)
        self.assertIn("pending_migrations", result)

    def test_supabase_response_has_app_user_secret_action(self):
        h = _load_handler()
        result = _invoke_supabase_handler(h)
        self.assertIn("app_user_secret_action", result)
        self.assertIn(result["app_user_secret_action"], ("created", "updated"))

    def test_supabase_response_has_aurora_refresh_secret_action(self):
        h = _load_handler()
        result = _invoke_supabase_handler(h)
        self.assertIn("aurora_refresh_secret_action", result)
        self.assertIn(result["aurora_refresh_secret_action"], ("created", "updated", "skipped"))

    def test_supabase_response_has_elapsed_time_seconds(self):
        h = _load_handler()
        result = _invoke_supabase_handler(h)
        self.assertIn("elapsed_time_seconds", result)
        self.assertIsInstance(result["elapsed_time_seconds"], float)


if __name__ == "__main__":
    unittest.main()
