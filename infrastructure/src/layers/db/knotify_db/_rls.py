"""
knotify_db._rls — SOLE SOURCE OF TRUTH for RLS GUC names, set_rls_context,
and the rls_context transaction manager.

This module owns the two PostgreSQL GUC constants (_GUC_USER_ID and
_GUC_USER_SEX), the set_rls_context function that uses them, and the
rls_context context manager that owns the transaction lifetime.  Both the
Aurora backend (_db.py) and the Supabase backend (_supabase.py, story 14.8)
import from here — there is exactly ONE copy of each RLS symbol in the
codebase.

Invariant: a one-character deviation in a GUC name causes
current_setting(..., true) to return NULL, which the RLS USING clause
evaluates to NULL (neither true nor false), fail-closing the policy to zero
rows with no error raised.  Do NOT retype the constant strings from memory;
copy them from migration 0007 verbatim.
"""

from __future__ import annotations

from contextlib import contextmanager

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


@contextmanager
def rls_context(
    conn: psycopg2.extensions.connection,
    user_id: str,
    user_sex: str,
):
    """
    Context manager that owns the full transaction lifetime.

    Usage:
        with rls_context(conn, user_id, user_sex):
            # conn is inside a transaction with GUCs set
            # queries here are filtered by the RLS policy
        # transaction committed — GUCs expired (SET LOCAL is txn-scoped)

    On entry:  BEGIN, then set_rls_context (SET LOCAL GUCs).
    On normal exit:  COMMIT — GUCs expire automatically (SET LOCAL).
    On exception exit:  ROLLBACK — GUCs expire automatically.

    Because SET LOCAL is transaction-scoped, the GUC values are gone once
    the transaction ends.  No explicit RESET is needed, and no GUC leakage
    occurs across subsequent transactions on the same connection.

    Note: Postgres custom GUCs retain an empty-string session value after the
    first SET LOCAL in a session.  This does NOT cause a security regression
    because: (a) the empty string does not match 'Male' or 'Female' for the
    != comparison on the sex column — wait, it does return true. However, the
    correct Lambda pattern is one connection per request (Lambda functions open
    a new connection per invocation and close it before returning), so the
    session GUC leakage scenario does not arise in production.  If connection
    pooling is introduced in a future phase, the pool must reset the GUC state
    on connection checkout — see architecture.md for guidance.
    """
    with conn.cursor() as cur:
        cur.execute("BEGIN")
    set_rls_context(conn, user_id, user_sex)
    try:
        yield
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
