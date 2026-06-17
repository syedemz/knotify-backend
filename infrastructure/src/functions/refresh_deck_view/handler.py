"""
knotify-refresh-deck-view Lambda handler.

Refreshes the deck_view materialized view by executing refresh_deck_view()
as the dedicated aurora_refresh Postgres role.  The refresh role has EXECUTE
on refresh_deck_view() but is NOT the RLS-bearing app_user — keeping the
app_user surface minimal and isolating failure modes.

Invocation sources:
  - CloudWatch EventBridge rule (every 15 minutes, both dev and prod)
  - Async boto3 lambda.invoke from the profile Lambda (InvocationType="Event")
    immediately after a profile_complete_verified false→true flip commits

Advisory lock:
  The handler acquires pg_try_advisory_lock(DECK_VIEW_LOCK_KEY) before
  refreshing.  If the lock is already held (concurrent invocation), the
  handler logs a skip and returns 200 — a concurrent refresh will cover
  any pending user.  The advisory lock is released after the refresh.
  The lock is session-scoped; it is released automatically when the
  connection closes, but we release it explicitly for clarity.

Instrumentation:
  On each actual refresh, the handler INSERTs one row into refresh_log
  (refreshed_at timestamptz NOT NULL DEFAULT now()).  Skip paths do NOT
  insert.  The advisory-lock integration test counts rows to assert exactly
  one concurrent invocation did the work.

Dependencies (Lambda layers):
  - knotify_obs: init_logger
  - knotify_db:  get_connection
"""

from __future__ import annotations

import json
import os

import knotify_db
from knotify_obs import init_logger

# ---------------------------------------------------------------------------
# Module-level singletons
# ---------------------------------------------------------------------------

logger = init_logger("knotify_refresh_deck_view")

_DB_SECRET_NAME: str = os.environ.get("DB_SECRET_NAME", "")

# Advisory lock key — a stable, project-specific integer that uniquely
# identifies the deck_view refresh lock across all sessions.  The value is
# arbitrary but must be consistent across deployments.  We use a well-known
# constant derived from the string "knotify_deck_refresh" to avoid collision
# with other advisory locks in the same database instance.
#
# Python: int.from_bytes(b"knotify_dk", "big") & 0x7FFFFFFF = 1801812843
# (Postgres pg_try_advisory_lock takes a bigint; fits in 32-bit positive range.)
DECK_VIEW_LOCK_KEY: int = 1801812843

# Module-level connection (reused across warm invocations)
_conn = None


def _get_conn():
    """
    Return a (possibly cached) psycopg2 connection using the aurora-refresh
    secret (NOT the app-user secret).

    Extracted so tests can patch at the module level without installing
    psycopg2 in the test environment.
    """
    global _conn
    if _conn is None or _conn.closed:
        _conn = knotify_db.get_connection(_DB_SECRET_NAME)
    return _conn


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def _try_refresh(conn) -> bool:
    """
    Attempt to acquire the advisory lock and refresh the materialized view.

    Returns:
        True  — lock was acquired and refresh executed (log row inserted).
        False — lock was held by another session; refresh skipped.

    The advisory lock is always released on the True path before returning.
    On the False path there is nothing to release (lock was never held).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_try_advisory_lock(%s)", (DECK_VIEW_LOCK_KEY,)
        )
        (lock_acquired,) = cur.fetchone()

    if not lock_acquired:
        logger.info(
            "refresh_skipped",
            extra={"reason": "advisory lock already held by another invocation"},
        )
        return False

    try:
        with conn.cursor() as cur:
            # Execute the materialized view refresh via the dedicated function.
            # refresh_deck_view() is defined in migration 0009 as:
            #   REFRESH MATERIALIZED VIEW CONCURRENTLY deck_view;
            cur.execute("SELECT refresh_deck_view()")

        # Insert one instrumentation row per actual refresh.
        # The advisory-lock integration test counts these rows.
        with conn.cursor() as cur:
            cur.execute("INSERT INTO refresh_log (refreshed_at) VALUES (DEFAULT)")

        conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_unlock(%s)", (DECK_VIEW_LOCK_KEY,)
            )
        conn.commit()
    except Exception:
        # On any error: release the lock (best-effort) and re-raise.
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_unlock(%s)", (DECK_VIEW_LOCK_KEY,)
                )
            conn.commit()
        except Exception:
            pass
        raise

    return True


# ---------------------------------------------------------------------------
# Lambda entrypoint
# ---------------------------------------------------------------------------


def handler(event: dict, context: object) -> dict:
    """
    refresh_deck_view Lambda entrypoint.

    Connects to Aurora as aurora_refresh, acquires the advisory lock, refreshes
    deck_view, and inserts a refresh_log row.  If the lock is already held,
    returns 200 with status="skipped".

    This function is invoked by:
      - EventBridge CloudWatch rule every 15 minutes
      - Profile Lambda (InvocationType="Event") after profile_complete_verified
        flips false→true

    Args:
        event:   Lambda event dict (unused — no payload expected).
        context: Lambda context object (unused).

    Returns:
        dict with statusCode and body indicating "refreshed" or "skipped".
    """
    conn = _get_conn()

    try:
        refreshed = _try_refresh(conn)
    except Exception as exc:
        # On unexpected error, reset the cached connection and re-raise so
        # Lambda records an error and the EventBridge retry policy applies.
        conn.close()
        global _conn
        _conn = None
        logger.error(
            "refresh_failed",
            extra={"error": str(exc)},
        )
        raise

    if refreshed:
        logger.info("refresh_completed", extra={"lock_key": DECK_VIEW_LOCK_KEY})
        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"status": "refreshed"}),
        }

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"status": "skipped"}),
    }
