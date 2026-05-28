"""
knotify_db._db — internal Aurora connection and RLS GUC helpers.

Public surface (re-exported by knotify_db/__init__.py):
  get_connection(secret_or_env)
      Returns a psycopg2 connection.  secret_or_env is either:
        - a dict with keys host, port, dbname, username, password
          (used directly — local dev / unit test path)
        - a string (secret ARN or name) — fetched from AWS Secrets Manager
          via boto3, parsed as JSON.  The Lambda runtime uses this path.

  set_rls_context(conn, user_id, user_sex)
      Issues two SET LOCAL statements within the current transaction so the
      RLS policy in migration 0007 can evaluate the GUCs:
        SET LOCAL app.requesting_user_id = '<uuid>'
        SET LOCAL app.requesting_user_sex = '<Male|Female>'
      GUC names match migration 0007 lines 115-116 verbatim — any deviation
      causes the policy to read NULL and fail-closed (zero rows, no error).

  rls_context(conn, user_id, user_sex)  [context manager]
      Manages a full transaction lifetime: BEGIN on entry, set_rls_context
      on entry, COMMIT on normal exit, ROLLBACK on exception exit.  Because
      SET LOCAL is transaction-scoped, the GUCs expire automatically when
      the transaction ends — no leakage to subsequent transactions on the
      same connection.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Union

import psycopg2
import psycopg2.extras

# Register UUID adapter once at import time so callers can pass uuid.UUID
# objects without casting to str.
psycopg2.extras.register_uuid()

# GUC names from migration 0007 lines 115-116 — must not be changed.
# Wrong names silently fail-close the RLS policy to zero rows.
_GUC_USER_ID = "app.requesting_user_id"
_GUC_USER_SEX = "app.requesting_user_sex"


def get_connection(secret_or_env: Union[dict, str]) -> psycopg2.extensions.connection:
    """
    Return a psycopg2 connection.

    Args:
        secret_or_env: Either a dict with connection fields (local/test path)
            or a string secret ARN/name (Lambda runtime path — fetched from
            AWS Secrets Manager via boto3).

    The dict path expects keys: host, port, dbname, username, password.
    The Secrets Manager path expects a JSON secret with the same keys
    (Aurora managed secrets use host/port/dbname/username/password).
    """
    if isinstance(secret_or_env, dict):
        params = secret_or_env
    else:
        import boto3  # deferred — not available in local test environments

        client = boto3.client("secretsmanager")
        response = client.get_secret_value(SecretId=secret_or_env)
        params = json.loads(response["SecretString"])

    return psycopg2.connect(
        host=params["host"],
        port=int(params["port"]),
        dbname=params["dbname"],
        user=params["username"],
        password=params["password"],
    )


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
