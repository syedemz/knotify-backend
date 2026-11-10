"""
Unit tests for the conditional backend dispatch in knotify_db/__init__.py
(story 14.9, phase 14).

The dispatch happens at MODULE IMPORT TIME: __init__.py reads
os.environ.get("USE_SUPABASE") once and binds get_connection to either
_db.get_connection (Aurora) or _supabase.get_connection (Supabase).

Because the dispatch is import-time, each test manipulates os.environ
BEFORE calling importlib.reload(knotify_db).  After reload, the test
inspects knotify_db.<name> via the module object — NEVER via a local
`from knotify_db import ...` at the top of this file, which would capture
a stale reference.

Env-state contract tested:
  - USE_SUPABASE absent (not in os.environ)  → Aurora path
  - USE_SUPABASE=""                           → Aurora path
  - USE_SUPABASE="false"                      → Aurora path
  - USE_SUPABASE="True"  (capital T)          → Aurora path  (case-sensitive)
  - USE_SUPABASE="TRUE"  (all caps)           → Aurora path  (case-sensitive)
  - USE_SUPABASE="true"  (exact lowercase)    → Supabase path
  RLS helpers (set_rls_context, rls_context) always come from _rls
  regardless of USE_SUPABASE.
  __all__ is exactly the five-name list, unchanged by either dispatch path.

Reload hygiene:
  Each test uses patch.dict(os.environ, ..., clear=False) as a context
  manager, which auto-restores os.environ to its original state on exit.
  importlib.reload(knotify_db) is called inside the context manager, so the
  newly-reloaded module sees the patched env.  After the context manager
  exits the env is restored, but the module in sys.modules still reflects
  the last reload; that is acceptable because the NEXT test will reload
  again inside its own patch.dict context.
"""

import importlib
import os
import unittest
from unittest.mock import patch

import knotify_db
import knotify_db._db
import knotify_db._rls
import knotify_db._supabase


class TestDispatchAuroraPath(unittest.TestCase):
    """
    Given USE_SUPABASE is absent, empty-string, "false", "True", or "TRUE",
    when knotify_db is (re-)imported,
    then get_connection must be the same object as knotify_db._db.get_connection.
    """

    def test_given_use_supabase_unset_when_imported_then_uses_aurora_get_connection(self):
        """
        Given USE_SUPABASE is NOT in os.environ at all,
        when knotify_db is reloaded,
        then knotify_db.get_connection IS knotify_db._db.get_connection.
        """
        env_without_flag = {k: v for k, v in os.environ.items() if k != "USE_SUPABASE"}

        with patch.dict(os.environ, env_without_flag, clear=True):
            importlib.reload(knotify_db)
            self.assertIs(
                knotify_db.get_connection,
                knotify_db._db.get_connection,
                "With USE_SUPABASE unset, get_connection must be the Aurora implementation "
                "(_db.get_connection).",
            )

    def test_given_use_supabase_false_string_when_imported_then_uses_aurora_get_connection(self):
        """
        Given USE_SUPABASE="false",
        when knotify_db is reloaded,
        then knotify_db.get_connection IS knotify_db._db.get_connection.
        """
        with patch.dict(os.environ, {"USE_SUPABASE": "false"}, clear=False):
            importlib.reload(knotify_db)
            self.assertIs(
                knotify_db.get_connection,
                knotify_db._db.get_connection,
                "With USE_SUPABASE='false', get_connection must be the Aurora implementation.",
            )

    def test_given_use_supabase_empty_string_when_imported_then_uses_aurora_get_connection(self):
        """
        Given USE_SUPABASE="" (empty string),
        when knotify_db is reloaded,
        then knotify_db.get_connection IS knotify_db._db.get_connection.
        """
        with patch.dict(os.environ, {"USE_SUPABASE": ""}, clear=False):
            importlib.reload(knotify_db)
            self.assertIs(
                knotify_db.get_connection,
                knotify_db._db.get_connection,
                "With USE_SUPABASE='', get_connection must be the Aurora implementation.",
            )

    def test_given_use_supabase_capital_T_true_when_imported_then_uses_aurora_path(self):
        """
        Given USE_SUPABASE="True" (Python-style boolean string, capital T),
        when knotify_db is reloaded,
        then knotify_db.get_connection IS knotify_db._db.get_connection.

        This proves the case-sensitive comparison — only the exact lowercase
        string "true" (emitted by Terraform locals) enables the Supabase path.
        A hand-typed "True" must NOT silently activate Supabase.
        """
        with patch.dict(os.environ, {"USE_SUPABASE": "True"}, clear=False):
            importlib.reload(knotify_db)
            self.assertIs(
                knotify_db.get_connection,
                knotify_db._db.get_connection,
                "With USE_SUPABASE='True' (capital T), get_connection must be Aurora. "
                "The comparison is case-sensitive: only 'true' (Terraform-emitted) activates Supabase.",
            )

    def test_given_use_supabase_all_caps_TRUE_when_imported_then_uses_aurora_path(self):
        """
        Given USE_SUPABASE="TRUE" (all caps),
        when knotify_db is reloaded,
        then knotify_db.get_connection IS knotify_db._db.get_connection.
        """
        with patch.dict(os.environ, {"USE_SUPABASE": "TRUE"}, clear=False):
            importlib.reload(knotify_db)
            self.assertIs(
                knotify_db.get_connection,
                knotify_db._db.get_connection,
                "With USE_SUPABASE='TRUE' (all caps), get_connection must be Aurora. "
                "Only exact 'true' (lowercase) activates Supabase.",
            )


class TestDispatchSupabasePath(unittest.TestCase):
    """
    Given USE_SUPABASE="true" (exact lowercase match),
    when knotify_db is (re-)imported,
    then get_connection must be the same object as knotify_db._supabase.get_connection.
    """

    def test_given_use_supabase_lowercase_true_when_imported_then_uses_supabase_get_connection(self):
        """
        Given USE_SUPABASE="true" (exact Terraform-emitted string),
        when knotify_db is reloaded,
        then knotify_db.get_connection IS knotify_db._supabase.get_connection.
        """
        with patch.dict(os.environ, {"USE_SUPABASE": "true"}, clear=False):
            importlib.reload(knotify_db)
            self.assertIs(
                knotify_db.get_connection,
                knotify_db._supabase.get_connection,
                "With USE_SUPABASE='true', get_connection must be the Supabase implementation "
                "(_supabase.get_connection).",
            )


class TestRlsHelpersAlwaysFromRls(unittest.TestCase):
    """
    Given any value of USE_SUPABASE,
    when knotify_db is (re-)imported,
    then set_rls_context and rls_context must always be the same objects as
    knotify_db._rls.set_rls_context and knotify_db._rls.rls_context.

    This is the sole-source-of-truth invariant from story 14.7: there is
    exactly ONE implementation of the RLS GUC code, regardless of which
    database backend is active.  A wrong GUC name silently fail-closes the
    RLS policy to zero rows with no error raised.
    """

    def test_given_supabase_true_when_reloaded_then_set_rls_context_is_from_rls(self):
        """
        Given USE_SUPABASE="true",
        when knotify_db is reloaded,
        then knotify_db.set_rls_context IS knotify_db._rls.set_rls_context.
        """
        with patch.dict(os.environ, {"USE_SUPABASE": "true"}, clear=False):
            importlib.reload(knotify_db)
            self.assertIs(
                knotify_db.set_rls_context,
                knotify_db._rls.set_rls_context,
                "With USE_SUPABASE='true', set_rls_context must still come from _rls (SSoT).",
            )

    def test_given_supabase_true_when_reloaded_then_rls_context_is_from_rls(self):
        """
        Given USE_SUPABASE="true",
        when knotify_db is reloaded,
        then knotify_db.rls_context IS knotify_db._rls.rls_context.
        """
        with patch.dict(os.environ, {"USE_SUPABASE": "true"}, clear=False):
            importlib.reload(knotify_db)
            self.assertIs(
                knotify_db.rls_context,
                knotify_db._rls.rls_context,
                "With USE_SUPABASE='true', rls_context must still come from _rls (SSoT).",
            )

    def test_given_use_supabase_false_when_reloaded_then_set_rls_context_is_from_rls(self):
        """
        Given USE_SUPABASE="false",
        when knotify_db is reloaded,
        then knotify_db.set_rls_context IS knotify_db._rls.set_rls_context.
        """
        with patch.dict(os.environ, {"USE_SUPABASE": "false"}, clear=False):
            importlib.reload(knotify_db)
            self.assertIs(
                knotify_db.set_rls_context,
                knotify_db._rls.set_rls_context,
                "With USE_SUPABASE='false', set_rls_context must come from _rls (SSoT).",
            )

    def test_given_use_supabase_false_when_reloaded_then_rls_context_is_from_rls(self):
        """
        Given USE_SUPABASE="false",
        when knotify_db is reloaded,
        then knotify_db.rls_context IS knotify_db._rls.rls_context.
        """
        with patch.dict(os.environ, {"USE_SUPABASE": "false"}, clear=False):
            importlib.reload(knotify_db)
            self.assertIs(
                knotify_db.rls_context,
                knotify_db._rls.rls_context,
                "With USE_SUPABASE='false', rls_context must come from _rls (SSoT).",
            )


class TestAllUnchanged(unittest.TestCase):
    """
    Given knotify_db after reload under either dispatch path,
    when __all__ is inspected,
    then it must be exactly the five-name list in the original order:
    ['get_connection', 'set_rls_context', 'rls_context', 'PREFERENCE_KEYS', 'encode_prefs'].

    This is a regression guard: the public API surface must not change
    regardless of which backend is selected.
    """

    _EXPECTED_ALL = [
        "get_connection",
        "set_rls_context",
        "rls_context",
        "PREFERENCE_KEYS",
        "encode_prefs",
    ]

    def test_given_use_supabase_false_then_all_has_exact_five_names(self):
        """
        Given USE_SUPABASE="false",
        when knotify_db is reloaded,
        then __all__ equals the canonical five-element list in order.
        """
        with patch.dict(os.environ, {"USE_SUPABASE": "false"}, clear=False):
            importlib.reload(knotify_db)
            self.assertEqual(
                knotify_db.__all__,
                self._EXPECTED_ALL,
                f"__all__ must be {self._EXPECTED_ALL!r}; got {knotify_db.__all__!r}",
            )

    def test_given_use_supabase_true_then_all_has_exact_five_names(self):
        """
        Given USE_SUPABASE="true",
        when knotify_db is reloaded,
        then __all__ equals the canonical five-element list in order.
        """
        with patch.dict(os.environ, {"USE_SUPABASE": "true"}, clear=False):
            importlib.reload(knotify_db)
            self.assertEqual(
                knotify_db.__all__,
                self._EXPECTED_ALL,
                f"__all__ must be {self._EXPECTED_ALL!r}; got {knotify_db.__all__!r}",
            )


class TestEnvRestoreAfterSuite(unittest.TestCase):
    """
    Final teardown guard: after all dispatch tests complete, knotify_db is
    reloaded once more into a neutral state (no USE_SUPABASE override), so
    that any subsequently-run test file (test_knotify_db_unit.py, etc.) sees
    the Aurora path — which is what those files expect.

    patch.dict(os.environ, ..., clear=False) already auto-restores os.environ
    after each test, but the sys.modules entry for knotify_db will reflect the
    LAST reload performed above.  This class reloads once more without any
    USE_SUPABASE override so the module is left in its default-Aurora state.
    """

    def test_zz_final_reload_to_aurora_state(self):
        """
        Given no USE_SUPABASE override,
        when knotify_db is reloaded as the last action in this test file,
        then knotify_db.get_connection IS knotify_db._db.get_connection.

        Named with 'zz_' prefix so pytest sorts it last within this class
        and within the file.
        """
        env_without_flag = {k: v for k, v in os.environ.items() if k != "USE_SUPABASE"}

        with patch.dict(os.environ, env_without_flag, clear=True):
            importlib.reload(knotify_db)
            self.assertIs(
                knotify_db.get_connection,
                knotify_db._db.get_connection,
                "Final reload (no USE_SUPABASE) must leave knotify_db on the Aurora path.",
            )


if __name__ == "__main__":
    unittest.main()
