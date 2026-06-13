"""
Block-aware SQL helpers for Knotify.

Provides two primitives used by friends, bookmarks, and blocks Lambdas (phase 6)
and later by match (phase 7) and chat (phase 8):

  block_filter(other_user_col)
      Returns a SQL NOT EXISTS fragment that filters rows where a block exists
      between the requesting user and the column identified by other_user_col.
      The column name is validated against a hard-coded whitelist before
      interpolation — user-supplied values are NEVER formatted into the SQL.
      Callers bind two %s parameters (the requesting user's UUID, twice) at
      cur.execute(...) time.

  is_blocked(conn, user_a, user_b)
      Issues a single parameterised SELECT against the blocks table and returns
      True if a block exists between user_a and user_b in either direction.
      The two UUIDs are passed solely via the params tuple — no f-string or
      string concatenation of the UUIDs into the SQL.
"""

from typing import Any

# ---------------------------------------------------------------------------
# Hard-coded whitelist of legal column identifiers.
#
# Only these values may be passed as other_user_col to block_filter.
# Any value not in this set raises ValueError immediately so the column name
# can never be used as a SQL injection vector.
#
# Entries are ALIAS-qualified, not table-qualified. PostgreSQL requires
# correlated subqueries (which block_filter generates) to reference outer
# tables by their visible alias once an alias is present in the FROM clause.
# Using bare table names against an aliased outer query raises
# "invalid reference to FROM-clause entry for table ...". Callers MUST alias
# their FROM clauses to match the prefix used here:
#   friendships     → f
#   friend_requests → fr
#   bookmarks       → bk
#   users           → u   (also used directly by the deck-view JOIN)
# ---------------------------------------------------------------------------
_ALLOWED_COLUMNS = frozenset(
    {
        "f.user_a",
        "f.user_b",
        "fr.from_user_id",
        "fr.to_user_id",
        "bk.bookmarked_user_id",
        "u.user_id",
    }
)


def block_filter(other_user_col: str) -> str:
    """
    Build a SQL fragment that filters rows where a block exists between the
    requesting user and other_user_col.

    The returned fragment is of the form:

        NOT EXISTS (
            SELECT 1 FROM blocks b
            WHERE (b.blocker_id = <other_user_col> AND b.blocked_id = %s)
               OR (b.blocker_id = %s AND b.blocked_id = <other_user_col>)
        )

    The two %s placeholders must be bound by the caller as the requesting
    user's UUID (once for each direction of the block check).

    Args:
        other_user_col: A column reference from the hard-coded whitelist.

    Returns:
        SQL fragment string (safe to embed in a larger SELECT).

    Raises:
        ValueError: If other_user_col is not in the whitelist.
    """
    if other_user_col not in _ALLOWED_COLUMNS:
        raise ValueError(
            f"block_filter: column reference {other_user_col!r} is not in the "
            f"allowed list. Permitted values: {sorted(_ALLOWED_COLUMNS)}"
        )

    return (
        f"NOT EXISTS ("
        f"SELECT 1 FROM blocks b "
        f"WHERE (b.blocker_id = {other_user_col} AND b.blocked_id = %s)"
        f" OR (b.blocker_id = %s AND b.blocked_id = {other_user_col})"
        f")"
    )


def is_blocked(conn: Any, user_a: str, user_b: str) -> bool:
    """
    Return True if a block exists between user_a and user_b in either direction.

    Checks both directions in a single SELECT:
        (blocker_id = user_a AND blocked_id = user_b)
     OR (blocker_id = user_b AND blocked_id = user_a)

    The two user UUIDs are passed exclusively via the params tuple to
    cur.execute — no f-string interpolation or string concatenation of
    the UUID values into SQL.

    Args:
        conn:   An active psycopg2 connection.
        user_a: UUID string of the first user.
        user_b: UUID string of the second user.

    Returns:
        True if any block row exists; False otherwise.
    """
    sql = (
        "SELECT 1 FROM blocks "
        "WHERE (blocker_id = %s AND blocked_id = %s) "
        "   OR (blocker_id = %s AND blocked_id = %s) "
        "LIMIT 1"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (user_a, user_b, user_b, user_a))
        return cur.fetchone() is not None
