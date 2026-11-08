"""
knotify_db._db — Aurora connection builder and RLS context manager.

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
      Re-exported from knotify_db._rls — that module is the sole source of
      truth for the GUC name constants and the SET LOCAL implementation.

  rls_context(conn, user_id, user_sex)  [context manager]
      Re-exported from knotify_db._rls — same SSoT guarantee.  Manages the
      full transaction lifetime: BEGIN on entry, set_rls_context on entry,
      COMMIT on normal exit, ROLLBACK on exception exit.
"""

from __future__ import annotations

import json
import os
from typing import Union

import psycopg2
import psycopg2.extras

# RLS symbols — sole source of truth is _rls.py.  Both backends (_db.py for
# Aurora, _supabase.py for Supabase) import from _rls so there is exactly one
# implementation of the sensitive RLS code in the codebase.
from knotify_db._rls import (
    _GUC_USER_ID,
    _GUC_USER_SEX,
    rls_context,
    set_rls_context,
)

# Register UUID adapter once at import time so callers can pass uuid.UUID
# objects without casting to str.
psycopg2.extras.register_uuid()


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


