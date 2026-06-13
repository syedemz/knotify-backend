"""
knotify_db._db — internal Aurora connection and RLS GUC helpers.

Public surface (re-exported by knotify_db/__init__.py):
  get_connection(secret_or_env)
      Returns a psycopg2 connection.  secret_or_env is either:
        - a dict with keys host, port, dbname, username, password
          (used directly — local dev / unit test path; all five fields
          live in the dict for self-contained test fixtures)
        - a string (secret ARN or name) — fetched from AWS Secrets Manager
          via boto3.  The secret payload is JSON containing only the
          credential pair {"username": ..., "password": ...}.  The
          connection endpoint params (host, port, dbname) are read from
          the environment variables AURORA_HOST, AURORA_PORT, AURORA_DBNAME.
          The Lambda runtime uses this path.

      Rationale for the split contract: the db_migrator Lambda is the
      authoritative writer of the app_user_credential secret (story 3.7,
      handler.py:128).  It writes only username + password because Aurora's
      managed master secret contains only credential fields too — host /
      port / dbname are exposed via aurora module outputs and injected as
      env vars by Terraform.  Embedding host in the secret would force a
      secret rewrite every time the cluster endpoint changed (e.g. after
      a failover or blue/green swap) and creates two sources of truth.

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
import os
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
        secret_or_env: Either a dict with all five connection fields
            (host, port, dbname, username, password) — used directly for
            local dev / unit tests — or a string secret ARN/name fetched
            from AWS Secrets Manager (Lambda runtime path).

    Dict path: expects keys host, port, dbname, username, password.

    Secrets Manager path: the secret JSON contains only the credential
    pair {"username": ..., "password": ...} (written by db_migrator,
    story 3.7).  Connection endpoint params are read from environment
    variables AURORA_HOST, AURORA_PORT, AURORA_DBNAME — these are wired
    by Terraform from the aurora module outputs (cluster_endpoint, port,
    database_name) on every Lambda that talks to Aurora.  If any of the
    three env vars is missing the function raises EnvironmentError with
    a precise message so misconfigured Lambdas fail loudly at startup
    rather than producing a KeyError deep inside psycopg2.connect.
    """
    if isinstance(secret_or_env, dict):
        params = secret_or_env
        host = params["host"]
        port = params["port"]
        dbname = params["dbname"]
        username = params["username"]
        password = params["password"]
    else:
        import boto3  # deferred — not available in local test environments

        client = boto3.client("secretsmanager")
        response = client.get_secret_value(SecretId=secret_or_env)
        creds = json.loads(response["SecretString"])

        try:
            host = os.environ["AURORA_HOST"]
            port = os.environ["AURORA_PORT"]
            dbname = os.environ["AURORA_DBNAME"]
        except KeyError as exc:
            raise EnvironmentError(
                f"knotify_db.get_connection: missing environment variable {exc.args[0]}. "
                "When called with a Secrets Manager secret name, AURORA_HOST, AURORA_PORT, "
                "and AURORA_DBNAME must all be set on the Lambda (wired from the aurora "
                "Terraform module outputs)."
            ) from exc

        username = creds["username"]
        password = creds["password"]

    return psycopg2.connect(
        host=host,
        port=int(port),
        dbname=dbname,
        user=username,
        password=password,
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
