"""
Unit tests for knotify_db._rls — shared RLS GUC helpers (story 14.7).

Architecture:
  _rls.py  — sole source of truth for GUC constants, set_rls_context, AND
             the rls_context transaction manager.
  _db.py   — imports all three symbols from _rls and re-exports them so the
             existing public surface of knotify_db._db is preserved.

Tests:
  - _rls._GUC_USER_ID and _GUC_USER_SEX match migration 0007 verbatim.
  - _rls.set_rls_context has the correct signature.
  - _rls.rls_context is callable (and so are the re-exports via _db and
    knotify_db).
  - _db.set_rls_context IS _rls.set_rls_context — identity re-export proof.
  - _db.rls_context IS _rls.rls_context — identity re-export proof.
    (Sole-source-of-truth invariant: exactly one implementation of each
    RLS symbol in the codebase.)

The GUC-name invariant: a one-character deviation causes current_setting(...,
true) to return NULL, which the RLS USING clause evaluates as NULL
(fail-closed: zero rows, no error raised).
"""

import inspect
import unittest


class TestRlsGucConstants(unittest.TestCase):
    """
    Given knotify_db._rls,
    when the module is imported,
    then _GUC_USER_ID and _GUC_USER_SEX match migration 0007 verbatim.
    """

    def test_guc_user_id_exact_string(self):
        """
        _GUC_USER_ID must equal 'app.requesting_user_id' — the exact string
        used in migration 0007 (USING clause, current_setting call).
        Any deviation silently fail-closes the RLS policy to zero rows.
        """
        import knotify_db._rls as rls

        self.assertEqual(
            rls._GUC_USER_ID,
            "app.requesting_user_id",
            f"Expected 'app.requesting_user_id', got '{rls._GUC_USER_ID}'",
        )

    def test_guc_user_sex_exact_string(self):
        """
        _GUC_USER_SEX must equal 'app.requesting_user_sex' — the exact string
        used in migration 0007 (USING clause, current_setting call).
        """
        import knotify_db._rls as rls

        self.assertEqual(
            rls._GUC_USER_SEX,
            "app.requesting_user_sex",
            f"Expected 'app.requesting_user_sex', got '{rls._GUC_USER_SEX}'",
        )


class TestRlsSetRlsContextSignature(unittest.TestCase):
    """
    Given knotify_db._rls.set_rls_context,
    when inspected,
    then its signature is (conn, user_id: str, user_sex: str) -> None.
    """

    def test_set_rls_context_is_callable(self):
        import knotify_db._rls as rls

        self.assertTrue(
            callable(rls.set_rls_context),
            "set_rls_context must be callable",
        )

    def test_set_rls_context_has_three_parameters(self):
        import knotify_db._rls as rls

        sig = inspect.signature(rls.set_rls_context)
        params = list(sig.parameters.keys())
        self.assertEqual(
            len(params),
            3,
            f"Expected 3 parameters (conn, user_id, user_sex), got {params}",
        )

    def test_set_rls_context_first_param_is_conn(self):
        import knotify_db._rls as rls

        sig = inspect.signature(rls.set_rls_context)
        params = list(sig.parameters.keys())
        self.assertEqual(
            params[0],
            "conn",
            f"First param must be 'conn', got '{params[0]}'",
        )

    def test_set_rls_context_second_param_is_user_id(self):
        import knotify_db._rls as rls

        sig = inspect.signature(rls.set_rls_context)
        params = list(sig.parameters.keys())
        self.assertEqual(
            params[1],
            "user_id",
            f"Second param must be 'user_id', got '{params[1]}'",
        )

    def test_set_rls_context_third_param_is_user_sex(self):
        import knotify_db._rls as rls

        sig = inspect.signature(rls.set_rls_context)
        params = list(sig.parameters.keys())
        self.assertEqual(
            params[2],
            "user_sex",
            f"Third param must be 'user_sex', got '{params[2]}'",
        )

    def test_set_rls_context_return_annotation_is_none(self):
        """
        Return annotation is -> None.  With `from __future__ import annotations`
        (PEP 563), inspect.signature returns the annotation as the string 'None'.
        Without that future import, it returns NoneType.  Both indicate -> None.
        """
        import knotify_db._rls as rls

        sig = inspect.signature(rls.set_rls_context)
        annotation = sig.return_annotation
        # PEP 563 (from __future__ import annotations) stores annotations as
        # strings — the string 'None' represents the -> None annotation.
        # Without PEP 563, inspect returns NoneType (type(None)).
        # Both forms are accepted.
        self.assertTrue(
            annotation is type(None) or annotation == "None",
            f"Return annotation must indicate -> None; got {annotation!r}",
        )


class TestRlsContextShape(unittest.TestCase):
    """
    Given knotify_db._rls.rls_context (defined in _rls, re-exported via _db
    and knotify_db),
    when inspected,
    then it is callable (a @contextmanager-decorated generator function).
    """

    def test_rls_context_is_callable_via_rls(self):
        import knotify_db._rls as rls

        self.assertTrue(
            callable(rls.rls_context),
            "knotify_db._rls.rls_context must be callable",
        )

    def test_rls_context_is_callable_via_db(self):
        import knotify_db._db as db

        self.assertTrue(
            callable(db.rls_context),
            "knotify_db._db.rls_context must be callable",
        )

    def test_rls_context_is_callable_via_public_api(self):
        import knotify_db

        self.assertTrue(
            callable(knotify_db.rls_context),
            "knotify_db.rls_context must be callable",
        )


class TestRlsIdentityReExport(unittest.TestCase):
    """
    Sole-source-of-truth proof:

    1. _db.set_rls_context IS _rls.set_rls_context — exactly one
       implementation of set_rls_context (and thus of the GUC name constants).

    2. _db.rls_context IS _rls.rls_context — exactly one implementation of
       the rls_context transaction manager.

    3. knotify_db.rls_context IS knotify_db._db.rls_context — the public
       API re-exports the _db re-export without wrapping.
    """

    def test_db_set_rls_context_is_rls_set_rls_context(self):
        """
        _db.set_rls_context is imported from _rls, not redefined.
        Object identity proves sole-source-of-truth.
        """
        import knotify_db._db as db
        import knotify_db._rls as rls

        self.assertIs(
            db.set_rls_context,
            rls.set_rls_context,
            "knotify_db._db.set_rls_context must be the SAME object as "
            "knotify_db._rls.set_rls_context (identity import, not re-implementation)",
        )

    def test_db_rls_context_is_rls_rls_context(self):
        """
        _db.rls_context is imported from _rls, not redefined.
        Object identity proves sole-source-of-truth.
        """
        import knotify_db._db as db
        import knotify_db._rls as rls

        self.assertIs(
            db.rls_context,
            rls.rls_context,
            "knotify_db._db.rls_context must be the SAME object as "
            "knotify_db._rls.rls_context (identity import, not re-implementation)",
        )

    def test_public_rls_context_is_db_rls_context(self):
        """
        knotify_db.rls_context (public surface) must be the same object as
        knotify_db._db.rls_context — no wrapping, no copy.
        """
        import knotify_db
        import knotify_db._db as db

        self.assertIs(
            knotify_db.rls_context,
            db.rls_context,
            "knotify_db.rls_context must be the SAME object as "
            "knotify_db._db.rls_context",
        )


if __name__ == "__main__":
    unittest.main()
