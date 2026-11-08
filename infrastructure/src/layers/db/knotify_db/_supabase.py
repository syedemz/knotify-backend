"""
knotify_db._supabase — Supabase connection builder.

This module is the Supabase-backend counterpart to _db.py.  It provides the
same public contract as _db.get_connection: accepts either a connection-param
dict (local/unit-test path) or a Secrets Manager ARN/name (Lambda runtime
path), and returns a live psycopg2 connection.

Key difference from _db.py: psycopg2.connect is always called with
sslmode="require" because Supabase enforces TLS at the transaction-mode
pooler.  Plaintext connections are rejected before reaching the database.

RLS SSoT invariant:
    set_rls_context, rls_context, _GUC_USER_ID, and _GUC_USER_SEX are
    IMPORTED from knotify_db._rls — they are NOT redefined here.  Both
    backends (_db.py for Aurora, this module for Supabase) share a single
    implementation of the RLS code.  A one-character deviation in a GUC
    constant silently fail-closes the RLS policy to zero rows with no error
    raised — the sole-source-of-truth property is non-negotiable.

Public surface:
    get_connection(secret_or_env)
        Returns a psycopg2 connection.  secret_or_env is either:
          - a dict with keys host, port, dbname, username, password
            (used directly — local dev / unit test path; all five fields
            live in the dict for self-contained test fixtures)
          - a string (secret ARN or name) — fetched from AWS Secrets Manager
            via boto3.  The secret payload is JSON containing only the
            credential pair {"username": ..., "password": ...}.  The
            connection endpoint params (host, port, dbname) are read from
            the environment variables SUPABASE_HOST, SUPABASE_PORT,
            SUPABASE_DBNAME — wired by Terraform from the supabase module
            outputs via local.db_env_vars in dev/main.tf.

    set_rls_context   — re-exported from knotify_db._rls (SSoT).
    rls_context       — re-exported from knotify_db._rls (SSoT).
    _GUC_USER_ID      — re-exported from knotify_db._rls (SSoT).
    _GUC_USER_SEX     — re-exported from knotify_db._rls (SSoT).
"""

from __future__ import annotations

import json
import os
from typing import Union

import psycopg2
import psycopg2.extras

# RLS symbols — sole source of truth is _rls.py.  Both backends import from
# _rls so there is exactly one implementation of the sensitive RLS code.
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
    Return a psycopg2 connection to the Supabase database.

    Args:
        secret_or_env: Either a dict with all five connection fields
            (host, port, dbname, username, password) — used directly for
            local dev / unit tests — or a string secret ARN/name fetched
            from AWS Secrets Manager (Lambda runtime path).

    Dict path: expects keys host, port, dbname, username, password.
        All five values are used directly with no environment variable access.

    Secrets Manager path: the secret JSON contains only the credential
    pair {"username": ..., "password": ...}.  Connection endpoint params are
    read from environment variables SUPABASE_HOST, SUPABASE_PORT,
    SUPABASE_DBNAME — wired by Terraform via local.db_env_vars in dev/main.tf
    when use_supabase=true.  A missing env var raises EnvironmentError with a
    precise message naming the missing variable so misconfigured Lambdas fail
    loudly at startup rather than producing a KeyError deep inside
    psycopg2.connect.

    In both cases psycopg2.connect is called with sslmode="require".
    Supabase enforces TLS at the transaction-mode pooler — plaintext
    connections are rejected before reaching the database.
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
            host = os.environ["SUPABASE_HOST"]
            port = os.environ["SUPABASE_PORT"]
            dbname = os.environ["SUPABASE_DBNAME"]
        except KeyError as exc:
            raise EnvironmentError(
                f"knotify_db._supabase.get_connection: missing environment variable {exc.args[0]}. "
                "When called with a Secrets Manager secret name, SUPABASE_HOST, SUPABASE_PORT, "
                "and SUPABASE_DBNAME must all be set on the Lambda (wired from the supabase "
                "Terraform module outputs via local.db_env_vars)."
            ) from exc

        username = creds["username"]
        password = creds["password"]

    # sslmode="require" is non-negotiable: Supabase enforces TLS at the
    # transaction-mode pooler.  Plaintext connections fail before the
    # PostgreSQL handshake occurs.
    return psycopg2.connect(
        host=host,
        port=int(port),
        dbname=dbname,
        user=username,
        password=password,
        sslmode="require",
    )
