"""
knotify_db._rls — SOLE SOURCE OF TRUTH for RLS GUC names and set_rls_context.

This module owns the two PostgreSQL GUC constants (_GUC_USER_ID and
_GUC_USER_SEX) and the set_rls_context function that uses them.  Both the
Aurora backend (_db.py) and the Supabase backend (_supabase.py, story 14.8)
import from here — there is exactly ONE copy of set_rls_context and of the
GUC name strings in the codebase.

The rls_context context manager is defined in _db.py (not here) so that the
existing test suite can patch knotify_db._db.set_rls_context and have that
patch intercepted by rls_context's call, which goes through _db's namespace.

Invariant: a one-character deviation in a GUC name causes
current_setting(..., true) to return NULL, which the RLS USING clause
evaluates to NULL (neither true nor false), fail-closing the policy to zero
rows with no error raised.  Do NOT retype the constant strings from memory;
copy them from migration 0007 verbatim.
"""

from __future__ import annotations

import psycopg2.extensions

# GUC names from migration 0007 lines 115-116 — must not be changed.
# Wrong names silently fail-close the RLS policy to zero rows.
_GUC_USER_ID = "app.requesting_user_id"
_GUC_USER_SEX = "app.requesting_user_sex"


def set_rls_context(
    conn: psycopg2.extensions.connection,
    user_id: str,
    user_sex: str,
) -> None:
    """
    Set the two RLS GUCs for the current transaction.

    Must be called inside an open transaction (after BEGIN).  The GUC values
    are transaction-local (SET LOCAL) and expire when the transaction ends.

    Args:
        conn:     An open psycopg2 connection.
        user_id:  UUID string of the requesting user.
        user_sex: 'Male' or 'Female' — the requesting user's sex.
    """
    with conn.cursor() as cur:
        cur.execute(
            f"SET LOCAL {_GUC_USER_ID} = %s",
            (user_id,),
        )
        cur.execute(
            f"SET LOCAL {_GUC_USER_SEX} = %s",
            (user_sex,),
        )
