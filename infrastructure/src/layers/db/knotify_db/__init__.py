"""
knotify_db — shared Aurora-access layer for Knotify Lambda functions.

Public API (story 3.3):
  get_connection(secret_or_env)
      Returns a psycopg2 connection.  Pass either a dict of connection params
      (local/test path) or a Secrets Manager ARN/name (Lambda runtime path).

  set_rls_context(conn, user_id, user_sex)
      Issues two SET LOCAL statements — app.requesting_user_id and
      app.requesting_user_sex — so the RLS policy in migration 0007 filters
      rows correctly.  GUC names match migration 0007 lines 115-116 verbatim.

  rls_context(conn, user_id, user_sex)  [context manager]
      Calls set_rls_context on entry; resets both GUCs via RESET statements
      on exit (normal or exception) to prevent leakage across connection reuse.
"""

from knotify_db._db import get_connection, set_rls_context, rls_context

__all__ = [
    "get_connection",
    "set_rls_context",
    "rls_context",
]
