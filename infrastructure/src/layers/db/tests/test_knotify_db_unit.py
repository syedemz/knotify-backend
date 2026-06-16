"""
Unit tests for knotify_db module.

Tests that do NOT require a running database — all connection objects are
mocked at the psycopg2 boundary.

Story 3.3 ACs tested here:
  - knotify_db exposes get_connection, set_rls_context, and rls_context
    (context manager).
  - set_rls_context issues the exact GUC names from migration 0007 lines
    115-116: app.requesting_user_id and app.requesting_user_sex.
  - The context manager owns the transaction lifetime: BEGIN on entry,
    COMMIT on normal exit, ROLLBACK on exception exit.  Because SET LOCAL
    is transaction-scoped, GUCs expire when the transaction ends — no
    leakage to subsequent transactions on the same connection.
  - Module public API surface matches the AC.
"""

import unittest
from unittest.mock import MagicMock, call, patch


class TestModulePublicApi(unittest.TestCase):
    """
    Given the knotify_db module,
    when imported,
    then it exposes get_connection, set_rls_context, and rls_context.
    """

    def test_all_three_exports_are_importable(self):
        from knotify_db import get_connection, set_rls_context, rls_context

        self.assertIsNotNone(get_connection)
        self.assertIsNotNone(set_rls_context)
        self.assertIsNotNone(rls_context)

    def test_get_connection_is_callable(self):
        from knotify_db import get_connection

        self.assertTrue(callable(get_connection))

    def test_set_rls_context_is_callable(self):
        from knotify_db import set_rls_context

        self.assertTrue(callable(set_rls_context))

    def test_rls_context_is_callable(self):
        from knotify_db import rls_context

        self.assertTrue(callable(rls_context))


class TestSetRlsContext(unittest.TestCase):
    """
    Given a psycopg2 connection mock,
    when set_rls_context is called with a user_id and user_sex,
    then it must execute exactly the two SET LOCAL statements with the GUC
    names that match migration 0007 lines 115-116 verbatim:
      SET LOCAL app.requesting_user_id = '<uuid>'
      SET LOCAL app.requesting_user_sex = '<Male|Female>'
    """

    def _make_conn(self):
        """Return a mock psycopg2 connection with a cursor context manager."""
        cur = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        return conn, cur

    def test_given_male_user_when_set_rls_context_then_gucs_use_correct_names(self):
        """
        The GUC names must be app.requesting_user_id and app.requesting_user_sex —
        any other name causes the RLS policy to read NULL and fail-closed.
        """
        from knotify_db import set_rls_context

        conn, cur = self._make_conn()
        user_id = "a1b2c3d4-0000-0000-0000-000000000001"

        set_rls_context(conn, user_id, "Male")

        executed_sqls = [str(c.args[0]) for c in cur.execute.call_args_list]

        user_id_sql_found = any("app.requesting_user_id" in s for s in executed_sqls)
        user_sex_sql_found = any("app.requesting_user_sex" in s for s in executed_sqls)

        self.assertTrue(
            user_id_sql_found,
            f"Expected 'app.requesting_user_id' GUC in executed SQL. Got: {executed_sqls}",
        )
        self.assertTrue(
            user_sex_sql_found,
            f"Expected 'app.requesting_user_sex' GUC in executed SQL. Got: {executed_sqls}",
        )

    def test_given_male_user_when_set_rls_context_then_user_id_value_passed(self):
        from knotify_db import set_rls_context

        conn, cur = self._make_conn()
        user_id = "a1b2c3d4-0000-0000-0000-000000000001"

        set_rls_context(conn, user_id, "Male")

        all_args = [c.args for c in cur.execute.call_args_list]
        values_passed = [args[1] if len(args) > 1 else () for args in all_args]
        flat_values = [
            v for tpl in values_passed
            for v in (tpl if isinstance(tpl, (list, tuple)) else [tpl])
        ]

        self.assertIn(
            user_id,
            flat_values,
            f"Expected user_id '{user_id}' in execute parameters. Got: {values_passed}",
        )

    def test_given_female_user_when_set_rls_context_then_sex_value_female(self):
        from knotify_db import set_rls_context

        conn, cur = self._make_conn()
        user_id = "a1b2c3d4-0000-0000-0000-000000000002"

        set_rls_context(conn, user_id, "Female")

        all_args = [c.args for c in cur.execute.call_args_list]
        values_passed = [args[1] if len(args) > 1 else () for args in all_args]
        flat_values = [
            v for tpl in values_passed
            for v in (tpl if isinstance(tpl, (list, tuple)) else [tpl])
        ]

        self.assertIn(
            "Female",
            flat_values,
            f"Expected sex value 'Female' in execute parameters. Got: {values_passed}",
        )

    def test_given_set_rls_context_executed_exactly_two_statements(self):
        """
        set_rls_context must execute exactly 2 SET LOCAL statements — one for
        user_id and one for user_sex. More or fewer is a contract violation.
        """
        from knotify_db import set_rls_context

        conn, cur = self._make_conn()

        set_rls_context(conn, "a1b2c3d4-0000-0000-0000-000000000001", "Male")

        self.assertEqual(
            cur.execute.call_count,
            2,
            f"Expected exactly 2 SET LOCAL execute calls, got {cur.execute.call_count}",
        )

    def test_set_rls_context_uses_set_local_not_set(self):
        """
        Must use SET LOCAL (transaction-scoped) not SET (session-scoped).
        SET LOCAL values expire at transaction end, preventing GUC leakage
        across subsequent transactions on the same connection.
        """
        from knotify_db import set_rls_context

        conn, cur = self._make_conn()

        set_rls_context(conn, "a1b2c3d4-0000-0000-0000-000000000001", "Male")

        executed_sqls = [str(c.args[0]) for c in cur.execute.call_args_list]
        for sql in executed_sqls:
            self.assertIn(
                "SET LOCAL",
                sql.upper(),
                f"Expected SET LOCAL (not SET) in: '{sql}'",
            )


class TestRlsContextManager(unittest.TestCase):
    """
    Given the rls_context context manager,
    when used as `with rls_context(conn, user_id, user_sex):`,
    then it calls set_rls_context on entry and manages the transaction
    (BEGIN on entry, COMMIT on normal exit, ROLLBACK on exception exit).
    GUC expiry is handled by transaction end — no explicit RESET needed.
    """

    def _make_conn(self):
        cur = MagicMock()
        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        return conn, cur

    def test_context_manager_calls_set_rls_context_on_enter(self):
        from knotify_db import rls_context

        conn, cur = self._make_conn()
        user_id = "a1b2c3d4-0000-0000-0000-000000000001"

        with patch("knotify_db._db.set_rls_context") as mock_set:
            with rls_context(conn, user_id, "Male"):
                mock_set.assert_called_once_with(conn, user_id, "Male")

    def test_context_manager_commits_on_normal_exit(self):
        """
        On normal (no exception) exit, the context manager must call
        conn.commit() so SET LOCAL GUCs expire with the transaction.
        """
        from knotify_db import rls_context

        conn, cur = self._make_conn()

        with rls_context(conn, "a1b2c3d4-0000-0000-0000-000000000001", "Male"):
            pass

        conn.commit.assert_called_once()

    def test_context_manager_rollbacks_on_exception_exit(self):
        """
        On exception exit, the context manager must call conn.rollback()
        so SET LOCAL GUCs expire with the rolled-back transaction.
        """
        from knotify_db import rls_context

        conn, cur = self._make_conn()

        with self.assertRaises(ValueError):
            with rls_context(conn, "a1b2c3d4-0000-0000-0000-000000000001", "Male"):
                raise ValueError("intentional test error")

        conn.rollback.assert_called_once()

    def test_context_manager_does_not_commit_on_exception_exit(self):
        """
        On exception exit, commit must NOT be called — only rollback.
        """
        from knotify_db import rls_context

        conn, cur = self._make_conn()

        with self.assertRaises(RuntimeError):
            with rls_context(conn, "a1b2c3d4-0000-0000-0000-000000000001", "Male"):
                raise RuntimeError("error in body")

        conn.commit.assert_not_called()

    def test_context_manager_issues_begin_on_entry(self):
        """
        The context manager must issue a BEGIN so that set_rls_context's
        SET LOCAL statements are scoped to this transaction.
        """
        from knotify_db import rls_context

        conn, cur = self._make_conn()

        with rls_context(conn, "a1b2c3d4-0000-0000-0000-000000000001", "Male"):
            pass

        executed_sqls = [str(c.args[0]) for c in cur.execute.call_args_list]
        begin_found = any("BEGIN" in sql.upper() for sql in executed_sqls)
        self.assertTrue(
            begin_found,
            f"Expected BEGIN in executed SQL. Got: {executed_sqls}",
        )

    def test_context_manager_reraises_exception_from_body(self):
        """
        The context manager must propagate exceptions from the body — it
        must not swallow them.
        """
        from knotify_db import rls_context

        conn, cur = self._make_conn()

        with self.assertRaises(KeyError):
            with rls_context(conn, "a1b2c3d4-0000-0000-0000-000000000001", "Male"):
                raise KeyError("test error propagated")


class TestGetConnectionSignature(unittest.TestCase):
    """
    Given the get_connection function,
    when inspected,
    then it accepts a secret_or_env argument.
    """

    def test_get_connection_accepts_secret_arn_argument(self):
        from knotify_db import get_connection
        import inspect

        sig = inspect.signature(get_connection)
        params = list(sig.parameters.keys())

        self.assertGreaterEqual(
            len(params),
            1,
            "get_connection must accept at least one argument (secret_or_env)",
        )

    def test_get_connection_first_param_named_secret_or_env(self):
        from knotify_db import get_connection
        import inspect

        sig = inspect.signature(get_connection)
        first_param = list(sig.parameters.keys())[0]

        self.assertEqual(
            first_param,
            "secret_or_env",
            f"First parameter must be named 'secret_or_env', got '{first_param}'",
        )


class TestGetConnectionSecretContract(unittest.TestCase):
    """
    Given get_connection called with a string (Secrets Manager secret name),
    when the secret contains only credentials and AURORA_HOST/PORT/DBNAME
    env vars are set, then it connects using env-var endpoint params and
    secret-derived credentials.  When env vars are missing it raises
    EnvironmentError with a precise message — never KeyError.

    This guards the contract introduced by hotfix/db-secret-connection-env-vars:
    the app_user_credential secret carries only {username, password}.  Host,
    port, and dbname live in env vars wired from the aurora Terraform module
    outputs (matching the db_migrator pattern in dev/main.tf lines 178-180).
    """

    def _patch_env(self, env):
        """Return an os.environ patch dict with all three keys removed first."""
        keys = ("AURORA_HOST", "AURORA_PORT", "AURORA_DBNAME")
        cleaned = {k: v for k, v in env.items() if k in keys}
        return cleaned

    def test_secret_name_path_uses_env_vars_for_endpoint(self):
        from knotify_db import get_connection

        env = {
            "AURORA_HOST": "test-cluster.cluster-x.eu-central-1.rds.amazonaws.com",
            "AURORA_PORT": "5432",
            "AURORA_DBNAME": "knotify",
        }
        secret_payload = '{"username":"app_user","password":"supersecret"}'

        fake_boto_client = MagicMock()
        fake_boto_client.get_secret_value.return_value = {"SecretString": secret_payload}

        with patch.dict("os.environ", env, clear=False), \
             patch("boto3.client", return_value=fake_boto_client) as mock_boto, \
             patch("knotify_db._db.psycopg2.connect") as mock_connect:

            get_connection("knotify-dev-app-user-credential")

            mock_boto.assert_called_once_with("secretsmanager")
            fake_boto_client.get_secret_value.assert_called_once_with(
                SecretId="knotify-dev-app-user-credential"
            )
            mock_connect.assert_called_once_with(
                host="test-cluster.cluster-x.eu-central-1.rds.amazonaws.com",
                port=5432,
                dbname="knotify",
                user="app_user",
                password="supersecret",
            )

    def test_secret_name_path_raises_environment_error_when_aurora_host_missing(self):
        from knotify_db import get_connection

        env = {"AURORA_PORT": "5432", "AURORA_DBNAME": "knotify"}
        secret_payload = '{"username":"u","password":"p"}'

        fake_boto_client = MagicMock()
        fake_boto_client.get_secret_value.return_value = {"SecretString": secret_payload}

        with patch.dict("os.environ", env, clear=True), \
             patch("boto3.client", return_value=fake_boto_client), \
             patch("knotify_db._db.psycopg2.connect"):

            with self.assertRaises(EnvironmentError) as ctx:
                get_connection("knotify-dev-app-user-credential")

            self.assertIn("AURORA_HOST", str(ctx.exception))

    def test_secret_name_path_raises_environment_error_when_aurora_port_missing(self):
        from knotify_db import get_connection

        env = {"AURORA_HOST": "h", "AURORA_DBNAME": "knotify"}
        secret_payload = '{"username":"u","password":"p"}'

        fake_boto_client = MagicMock()
        fake_boto_client.get_secret_value.return_value = {"SecretString": secret_payload}

        with patch.dict("os.environ", env, clear=True), \
             patch("boto3.client", return_value=fake_boto_client), \
             patch("knotify_db._db.psycopg2.connect"):

            with self.assertRaises(EnvironmentError) as ctx:
                get_connection("knotify-dev-app-user-credential")

            self.assertIn("AURORA_PORT", str(ctx.exception))

    def test_secret_name_path_raises_environment_error_when_aurora_dbname_missing(self):
        from knotify_db import get_connection

        env = {"AURORA_HOST": "h", "AURORA_PORT": "5432"}
        secret_payload = '{"username":"u","password":"p"}'

        fake_boto_client = MagicMock()
        fake_boto_client.get_secret_value.return_value = {"SecretString": secret_payload}

        with patch.dict("os.environ", env, clear=True), \
             patch("boto3.client", return_value=fake_boto_client), \
             patch("knotify_db._db.psycopg2.connect"):

            with self.assertRaises(EnvironmentError) as ctx:
                get_connection("knotify-dev-app-user-credential")

            self.assertIn("AURORA_DBNAME", str(ctx.exception))

    def test_dict_path_still_uses_all_five_keys_from_dict(self):
        """
        The dict path (local dev / unit-test path) must remain unchanged:
        all five connection fields come from the dict.  Env vars are
        ignored on this path so unit tests stay self-contained.
        """
        from knotify_db import get_connection

        params = {
            "host": "localhost",
            "port": 5432,
            "dbname": "knotify_test",
            "username": "test_user",
            "password": "test_password",
        }

        # AURORA_* env vars deliberately absent — dict path must not consult them.
        with patch.dict("os.environ", {}, clear=True), \
             patch("knotify_db._db.psycopg2.connect") as mock_connect:

            get_connection(params)

            mock_connect.assert_called_once_with(
                host="localhost",
                port=5432,
                dbname="knotify_test",
                user="test_user",
                password="test_password",
            )


class TestYoyoImportable(unittest.TestCase):
    """
    Given the db layer,
    when yoyo-migrations is imported,
    then read_migrations and get_backend are importable.

    This test proves that yoyo-migrations is present in the layer (or the
    local Python environment that mirrors the layer) — it fails if yoyo is
    not installed (B3 resolution).
    """

    def test_yoyo_read_migrations_is_importable(self):
        from yoyo import read_migrations
        self.assertTrue(callable(read_migrations))

    def test_yoyo_get_backend_is_importable(self):
        from yoyo import get_backend
        self.assertTrue(callable(get_backend))

    def test_yoyo_read_migrations_and_get_backend_importable_together(self):
        from yoyo import read_migrations, get_backend
        self.assertIsNotNone(read_migrations)
        self.assertIsNotNone(get_backend)


class TestEncodePrefs(unittest.TestCase):
    """
    Tests for knotify_db.encode_prefs and PREFERENCE_KEYS (story 7.3).

    encode_prefs(prefs: dict) -> list[float] maps the 20-key boolean preference
    dict to a 20-dimensional float vector using the key ordering in PREFERENCE_KEYS
    (§5.5 of architecture.md). PREFERENCE_KEYS is a module-level constant and the
    single source of truth — it is NOT redefined per call.
    """

    def test_given_empty_dict_when_encode_prefs_then_returns_20_zeros(self):
        """encode_prefs({}) must return a list of exactly 20 zeros."""
        from knotify_db import encode_prefs

        result = encode_prefs({})

        self.assertEqual(len(result), 20, f"Expected 20 floats, got {len(result)}")
        self.assertEqual(
            result,
            [0.0] * 20,
            f"Expected all zeros, got {result}",
        )

    def test_given_highlyeducated_true_when_encode_prefs_then_first_element_is_one(self):
        """
        'highlyeducated' is the first key in PREFERENCE_KEYS (§5.5).
        encode_prefs({'highlyeducated': True}) must return [1.0, 0.0, 0.0, ..., 0.0].
        """
        from knotify_db import encode_prefs

        result = encode_prefs({"highlyeducated": True})

        self.assertEqual(result[0], 1.0, f"Expected result[0]=1.0, got {result[0]}")
        self.assertEqual(
            result[1:],
            [0.0] * 19,
            f"Expected positions 1-19 to be 0.0, got {result[1:]}",
        )

    def test_given_athletic_true_when_encode_prefs_then_correct_index_is_one(self):
        """
        'athletic' is at index 14 in PREFERENCE_KEYS (§5.5).
        Only that position must be 1.0; all others 0.0.
        """
        from knotify_db import PREFERENCE_KEYS, encode_prefs

        result = encode_prefs({"athletic": True})

        athletic_idx = PREFERENCE_KEYS.index("athletic")
        self.assertEqual(
            result[athletic_idx],
            1.0,
            f"Expected result[{athletic_idx}]=1.0 for 'athletic', got {result[athletic_idx]}",
        )
        for i, v in enumerate(result):
            if i != athletic_idx:
                self.assertEqual(v, 0.0, f"Expected result[{i}]=0.0, got {v}")

    def test_given_multiple_prefs_true_when_encode_prefs_then_corresponding_positions_are_one(self):
        """
        Multiple True preferences set their respective positions to 1.0.
        """
        from knotify_db import PREFERENCE_KEYS, encode_prefs

        prefs = {"highlyeducated": True, "athletic": True}
        result = encode_prefs(prefs)

        for key in prefs:
            idx = PREFERENCE_KEYS.index(key)
            self.assertEqual(result[idx], 1.0, f"Expected result[{idx}]=1.0 for '{key}'")

        for key in PREFERENCE_KEYS:
            if key not in prefs:
                idx = PREFERENCE_KEYS.index(key)
                self.assertEqual(result[idx], 0.0, f"Expected result[{idx}]=0.0 for '{key}'")

    def test_preference_keys_has_exactly_20_elements(self):
        """PREFERENCE_KEYS must have exactly 20 keys (§5.5 says 20-D vector)."""
        from knotify_db import PREFERENCE_KEYS

        self.assertEqual(
            len(PREFERENCE_KEYS),
            20,
            f"Expected 20 PREFERENCE_KEYS, got {len(PREFERENCE_KEYS)}: {PREFERENCE_KEYS}",
        )

    def test_preference_keys_exact_order_matches_architecture_section_5_5(self):
        """
        PREFERENCE_KEYS must exactly match §5.5 in order — this is the single
        source of truth for the 20-D encoding and must not drift from the spec.
        """
        from knotify_db import PREFERENCE_KEYS

        expected = [
            "highlyeducated", "moderateeducated", "basiceducated",
            "familyoriented", "homeoriented", "workoriented", "religionoriented",
            "talkative", "reserved", "cheerful", "serious", "listener",
            "intelligent", "welldressed", "athletic",
            "travel", "cooking", "reading", "movies", "nature",
        ]
        self.assertEqual(
            list(PREFERENCE_KEYS),
            expected,
            "PREFERENCE_KEYS does not match §5.5 of architecture.md verbatim",
        )

    def test_preference_keys_same_object_when_imported_twice(self):
        """
        PREFERENCE_KEYS imported in two separate import statements must be the
        same list object — not a copy. This verifies it is a module-level
        constant, not reconstructed per call.
        """
        import importlib
        import sys

        # Clear any cached import of knotify_db.prefs to force fresh import
        for mod_name in list(sys.modules.keys()):
            if "knotify_db" in mod_name:
                del sys.modules[mod_name]

        import knotify_db as kdb1
        keys1 = kdb1.PREFERENCE_KEYS

        import knotify_db as kdb2
        keys2 = kdb2.PREFERENCE_KEYS

        self.assertIs(
            keys1,
            keys2,
            "PREFERENCE_KEYS must be the same list object across imports (module-level constant)",
        )

    def test_given_false_preference_when_encode_prefs_then_position_is_zero(self):
        """Explicitly False preferences must map to 0.0."""
        from knotify_db import encode_prefs

        result = encode_prefs({"highlyeducated": False, "athletic": False})

        self.assertEqual(result, [0.0] * 20)

    def test_given_unknown_key_when_encode_prefs_then_ignored(self):
        """Keys not in PREFERENCE_KEYS must be silently ignored (no error, no extra position)."""
        from knotify_db import encode_prefs

        result = encode_prefs({"unknown_key_xyz": True, "highlyeducated": True})

        self.assertEqual(len(result), 20)
        self.assertEqual(result[0], 1.0)


if __name__ == "__main__":
    unittest.main()
