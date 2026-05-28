"""
Integration tests for the db_migrator Lambda handler.

Requires a running docker-compose Postgres container (`make db-up` first).
Marked @pytest.mark.integration so they are skipped with
`pytest -m "not integration"`.

Story 3.7 AC 9:
  Integration test against local Postgres container mocks boto3
  SecretsManager (master + app_user) and asserts yoyo applies AND
  ALTER ROLE runs with non-empty password.

Test design:
  - The handler's boto3 SecretsManager client is mocked entirely.
  - The handler's psycopg2.connect is NOT mocked — it connects to the
    real local docker-compose Postgres container on localhost:5432.
  - yoyo migrations are applied against the local container so the
    migrator's _apply_migrations path exercises real SQL execution.
  - After the handler runs we verify:
      1. pending_migrations == 0
      2. applied_count > 0 OR 0 (idempotent on repeat runs)
      3. app_user_secret_action in {"created", "updated"}
      4. The app_user role password was actually changed (connect as
         app_user with the new password succeeds).
"""

import importlib.util
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

import pytest
import psycopg2


def _load_handler():
    """Load db_migrator/handler.py by absolute file path (avoids sys.path conflicts)."""
    _here = os.path.dirname(os.path.abspath(__file__))
    handler_path = os.path.normpath(os.path.join(_here, "..", "handler.py"))
    spec = importlib.util.spec_from_file_location("db_migrator_handler", handler_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

# Repo root for resolving paths
_here = os.path.dirname(os.path.abspath(__file__))
_repo_root = os.path.normpath(os.path.join(_here, "..", "..", "..", "..", ".."))
_migrations_dir = os.path.join(_repo_root, "infrastructure", "db", "migrations")


def _master_secret_json(
    username: str = "knotify",
    password: str = "knotify",
) -> str:
    # Aurora-managed master secret only contains username + password.
    # Host/port/dbname come from env vars set by Terraform from the
    # cluster endpoint outputs.
    return json.dumps({
        "username": username,
        "password": password,
    })


def _set_endpoint_env(h, host="localhost", port="5432", dbname="knotify"):
    h._AURORA_HOST = host
    h._AURORA_PORT = port
    h._AURORA_DBNAME = dbname


def _make_sm_mock(
    master_secret: str,
    app_user_secret_name: str = "knotify-dev-app-user-credential",
    create_raises: bool = False,
) -> MagicMock:
    """
    Build a boto3 SecretsManager client mock.

    - get_secret_value returns *master_secret* (the Aurora master credential).
    - create_secret succeeds or raises ResourceExistsException depending on
      *create_raises*.
    - put_secret_value always succeeds.
    """
    sm = MagicMock()
    sm.get_secret_value.return_value = {"SecretString": master_secret}

    if create_raises:
        sm.exceptions.ResourceExistsException = type(
            "ResourceExistsException", (Exception,), {}
        )
        sm.create_secret.side_effect = sm.exceptions.ResourceExistsException("already exists")
    else:
        sm.create_secret.return_value = {}

    sm.put_secret_value.return_value = {}
    return sm


@pytest.mark.integration
class TestDbMigratorIntegration(unittest.TestCase):
    """
    Integration tests for the db_migrator handler.

    setUp: rolls back all migrations so each test starts from a clean slate.
    tearDown: rolls back all migrations again to leave the container clean.

    NOTE: Because rollback leaves the schema empty, we apply migrations
    inside each test (via the handler itself or explicitly) rather than
    relying on a module-scoped fixture.
    """

    _MASTER_ARN = "arn:aws:secretsmanager:eu-central-1:123:secret:master"
    _APP_SECRET_NAME = "knotify-dev-app-user-credential"

    def setUp(self):
        """Roll back all migrations to start from a clean slate."""
        import subprocess
        yoyo_ini = os.path.join(_repo_root, "infrastructure", "db", "yoyo.ini")
        subprocess.run(
            [sys.executable, "-m", "yoyo", "rollback", "--all",
             "--config", yoyo_ini, "--batch"],
            check=True,
        )

    def tearDown(self):
        """Roll back all migrations to leave the container clean."""
        import subprocess
        yoyo_ini = os.path.join(_repo_root, "infrastructure", "db", "yoyo.ini")
        subprocess.run(
            [sys.executable, "-m", "yoyo", "rollback", "--all",
             "--config", yoyo_ini, "--batch"],
            check=True,
        )

    def _run_handler(self, create_raises: bool = False) -> dict:
        """
        Run the db_migrator handler with a mocked boto3 SecretsManager client
        but a REAL psycopg2 connection to the local docker-compose Postgres.

        Returns the handler's structured JSON response dict.
        """
        h = _load_handler()

        sm = _make_sm_mock(
            _master_secret_json(),
            self._APP_SECRET_NAME,
            create_raises=create_raises,
        )

        h._MASTER_SECRET_ARN = self._MASTER_ARN
        h._APP_USER_SECRET_NAME = self._APP_SECRET_NAME
        _set_endpoint_env(h)

        # Patch boto3.client to return our mock but leave psycopg2 real
        # so the ALTER ROLE and yoyo migrations run against the real container.
        with patch.object(h, "boto3") as mock_boto3:
            mock_boto3.client.return_value = sm
            # Override _get_migrations_path to point at the real migrations dir
            with patch.object(h, "_get_migrations_path") as mock_path:
                from pathlib import Path
                mock_path.return_value = Path(_migrations_dir)
                result = h.handler({}, None)

        return result, sm

    def test_given_clean_db_when_handler_runs_then_pending_migrations_zero(self):
        """
        Given a clean database (no schema),
        when the handler runs,
        then all migrations are applied and pending_migrations == 0.
        """
        result, _ = self._run_handler()
        self.assertEqual(
            result["pending_migrations"],
            0,
            f"Expected 0 pending migrations after apply. Got: {result}",
        )

    def test_given_clean_db_when_handler_runs_then_applied_count_positive(self):
        """
        Given a clean database,
        when the handler runs,
        then at least one migration was applied (applied_count > 0).
        """
        result, _ = self._run_handler()
        applied = result["applied_migration_ids"]
        self.assertGreater(
            len(applied),
            0,
            f"Expected applied_count > 0 on a clean DB. Got: {result}",
        )

    def test_given_first_run_when_handler_runs_then_secret_action_is_created(self):
        """
        Given a first run (CreateSecret succeeds),
        when the handler runs,
        then app_user_secret_action == "created".
        """
        result, _ = self._run_handler(create_raises=False)
        self.assertEqual(result["app_user_secret_action"], "created")

    def test_given_subsequent_run_when_handler_runs_then_secret_action_is_updated(self):
        """
        Given a subsequent run (CreateSecret raises ResourceExistsException),
        when the handler runs,
        then app_user_secret_action == "updated".
        """
        result, _ = self._run_handler(create_raises=True)
        self.assertEqual(result["app_user_secret_action"], "updated")

    def test_given_handler_runs_then_alter_role_sets_non_empty_password(self):
        """
        After the handler runs, the app_user role password must have been
        changed to a non-empty value (the put_secret_value mock captures it).

        We verify the password stored in the secret is 32 chars and
        that connecting to Postgres as app_user with that password works
        (the ALTER ROLE must have actually executed against the real container).
        """
        h = _load_handler()

        sm = _make_sm_mock(_master_secret_json(), self._APP_SECRET_NAME)
        h._MASTER_SECRET_ARN = self._MASTER_ARN
        h._APP_USER_SECRET_NAME = self._APP_SECRET_NAME
        _set_endpoint_env(h)

        captured_password: list[str] = []

        original_write = h._write_app_user_secret

        def capturing_write(sm_client, secret_name, password):
            captured_password.append(password)
            return original_write(sm_client, secret_name, password)

        with patch.object(h, "boto3") as mock_boto3:
            mock_boto3.client.return_value = sm
            with patch.object(h, "_get_migrations_path") as mock_path:
                from pathlib import Path
                mock_path.return_value = Path(_migrations_dir)
                with patch.object(h, "_write_app_user_secret", side_effect=capturing_write):
                    h.handler({}, None)

        self.assertEqual(len(captured_password), 1)
        pwd = captured_password[0]
        self.assertEqual(len(pwd), 32, f"Expected 32-char password, got: {pwd!r}")

        # Verify ALTER ROLE actually ran: connect as app_user with the new password
        conn = psycopg2.connect(
            host="localhost",
            port=5432,
            dbname="knotify",
            user="app_user",
            password=pwd,
        )
        conn.close()

    def test_given_idempotent_second_run_when_handler_runs_then_pending_zero(self):
        """
        Given the handler is invoked a second time on an already-migrated DB,
        when the handler runs,
        then pending_migrations == 0 (idempotency check — AC 7).
        """
        # First run: apply migrations
        self._run_handler()
        # Second run: should be idempotent
        result, _ = self._run_handler(create_raises=True)
        self.assertEqual(
            result["pending_migrations"],
            0,
            f"Second run should report 0 pending migrations. Got: {result}",
        )


if __name__ == "__main__":
    unittest.main()
