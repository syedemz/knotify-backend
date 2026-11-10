"""
knotify_db — shared database-access layer for Knotify Lambda functions.
Phase 14, story 14.9: conditional backend dispatch.

Backend selection (import-time, based on USE_SUPABASE env var):
  - os.environ.get("USE_SUPABASE") == "true"  → import get_connection from _supabase
  - anything else (unset, "", "false", "True", "FALSE", …) → import get_connection from _db

The comparison is CASE-SENSITIVE and matches EXACTLY the string "true" (lowercase).
Rationale: Terraform locals emit lowercase "true" / "false" for boolean variables.
Matching only the exact Terraform-emitted string "true" ensures that a hand-typed
"True" or "TRUE" in a manually-set env var does NOT silently activate the Supabase
path.  Mistakes that look like booleans must fail to the safe Aurora default rather
than routing to the wrong backend.

RLS helpers (set_rls_context, rls_context) are ALWAYS imported from _rls
regardless of which backend is active.  This is the sole-source-of-truth invariant
from story 14.7: there is exactly ONE implementation of the RLS GUC code in the
codebase.  A one-character deviation in a GUC constant silently fail-closes the
RLS policy to zero rows with no error raised.

Public API (story 3.3):
  get_connection(secret_or_env)
      Returns a psycopg2 connection.  Pass either a dict of connection params
      (local/test path) or a Secrets Manager ARN/name (Lambda runtime path).
      Routes to _db or _supabase depending on USE_SUPABASE.

  set_rls_context(conn, user_id, user_sex)
      Issues two SET LOCAL statements — app.requesting_user_id and
      app.requesting_user_sex — so the RLS policy in migration 0007 filters
      rows correctly.  GUC names match migration 0007 lines 115-116 verbatim.
      Always from _rls (sole source of truth).

  rls_context(conn, user_id, user_sex)  [context manager]
      Calls set_rls_context on entry; manages the full transaction lifetime.
      Always from _rls (sole source of truth).

Public API (story 7.3):
  PREFERENCE_KEYS : list[str]
      Ordered 20-key list from §5.5 of architecture.md — the single source of
      truth for the 20-D preference vector encoding.

  encode_prefs(prefs: dict) -> list[float]
      Map a preferences JSONB dict to a 20-dimensional float vector using
      PREFERENCE_KEYS ordering.  1.0 for truthy keys, 0.0 otherwise.
"""

import os

# --- Backend dispatch (case-sensitive, import-time) -------------------------
# Only the exact lowercase string "true" activates Supabase.  This matches
# the string Terraform emits for `use_supabase = true` in tfvars.
if os.environ.get("USE_SUPABASE") == "true":
    from knotify_db._supabase import get_connection
else:
    from knotify_db._db import get_connection

# --- RLS helpers — ALWAYS from _rls, no backend branching ------------------
# Sole-source-of-truth invariant (story 14.7): both _db.py and _supabase.py
# also import from _rls; __init__.py does the same directly so callers who
# import from knotify_db receive the identical objects regardless of backend.
from knotify_db._rls import set_rls_context, rls_context

# --- Preference-vector helpers (story 7.3) — unchanged ---------------------
from knotify_db.prefs import PREFERENCE_KEYS, encode_prefs

__all__ = [
    "get_connection",
    "set_rls_context",
    "rls_context",
    "PREFERENCE_KEYS",
    "encode_prefs",
]
