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


if __name__ == "__main__":
    unittest.main()
