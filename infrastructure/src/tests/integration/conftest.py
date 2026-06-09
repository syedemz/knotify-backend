"""
pytest configuration for top-level E2E integration tests.

These tests run against the live dev AWS environment — they are NOT
docker-compose tests.  They require:
  - A deployed dev Cognito User Pool (stories 4.1–4.5)
  - A deployed dev Aurora cluster with migrations applied (phase 3)
  - AWS credentials in the execution environment with permissions to:
      cognito-idp: sign_up, admin_confirm_sign_up, admin_initiate_auth,
                   admin_delete_user
      secretsmanager: get_secret_value (aurora master secret)
      ec2+VPC: the test runner must be able to reach the Aurora writer
               endpoint, or else the Aurora endpoint must be temporarily
               reachable from the test runner (bastion / VPN / SSM tunnel)

Required environment variables (no defaults — missing values cause an
immediate, clear error at collection time via the fixture in cognito_signup_test.py):

  COGNITO_USER_POOL_ID           — e.g. eu-central-1_abc123
  COGNITO_INTEGRATION_TEST_CLIENT_ID  — the dev-only client (story 4.2)
  AURORA_HOST                    — writer endpoint of the dev cluster
  AURORA_PORT                    — normally 5432
  AURORA_DBNAME                  — normally "knotify"
  AURORA_MASTER_SECRET_ARN       — ARN of the Aurora-managed master secret
  AWS_REGION                     — e.g. eu-central-1

Run with:
    pytest infrastructure/src/tests/integration/ -v -m integration

Skip in any environment that lacks live AWS access:
    pytest -m "not integration"
"""

from __future__ import annotations

import json
import os
import sys
import uuid

import pytest


# ---------------------------------------------------------------------------
# Environment-variable helper (shared across all integration tests)
# ---------------------------------------------------------------------------

def _require_env(name: str) -> str:
    """
    Return the value of environment variable *name*.

    Calls pytest.skip (not raises) if the variable is absent so the test is
    marked SKIPPED rather than ERROR when the live dev environment is not
    configured.
    """
    value = os.environ.get(name)
    if not value:
        pytest.skip(
            f"Required env var {name!r} is not set — "
            "live dev AWS environment not configured.  "
            "Set all required env vars to run E2E tests."
        )
    return value


# ---------------------------------------------------------------------------
# Aurora connection helpers (shared across all integration tests)
# ---------------------------------------------------------------------------

def _get_aurora_master_creds(sm_client, secret_arn: str) -> dict:
    """
    Fetch the Aurora master credential from Secrets Manager.

    Args:
        sm_client:  Initialised boto3 secretsmanager client.
        secret_arn: ARN of the Aurora-managed master secret.

    Returns:
        dict with keys "username" and "password".
    """
    response = sm_client.get_secret_value(SecretId=secret_arn)
    return json.loads(response["SecretString"])


def _aurora_master_conn(
    psycopg2_module,
    host: str,
    port: int,
    dbname: str,
    username: str,
    password: str,
):
    """
    Open a psycopg2 connection to Aurora using master credentials.

    autocommit=True so individual queries and DELETE statements commit
    immediately without requiring explicit transaction management.
    """
    conn = psycopg2_module.connect(
        host=host,
        port=port,
        dbname=dbname,
        user=username,
        password=password,
    )
    conn.autocommit = True
    return conn


# ---------------------------------------------------------------------------
# signed_in_user fixture — story 5.7 (extracted from test_cognito_signup.py)
#
# Performs the full sign-up + confirm + sign-in dance against the live dev
# Cognito User Pool.  Yields a dict containing the user's Cognito sub and
# authentication tokens so any integration test can obtain a valid JWT
# without duplicating the boilerplate.
#
# Teardown (in the fixture's finally block) deletes the synthetic Cognito
# user and the corresponding Aurora users row so repeated runs do not
# accumulate stale test data.
#
# Consumed by:
#   - infrastructure/src/tests/integration/test_cognito_signup.py (refactored)
#   - infrastructure/src/tests/integration/test_edge_smoke.py (new, story 5.7)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="function")
def signed_in_user():
    """
    Yield a dict with Cognito user credentials for a freshly created test user.

    The dict contains:
        sub          — Cognito user sub (UUID string)
        email        — the unique per-run test email
        id_token     — Cognito IdToken JWT
        access_token — Cognito AccessToken JWT
        refresh_token — Cognito RefreshToken

    Teardown removes the user from both Cognito (admin_delete_user) and Aurora
    (DELETE FROM users WHERE user_id = %s).  Runs unconditionally so partial
    test failures never accumulate synthetic users.

    Prerequisites (all resolved via pytest.skip if absent):
        COGNITO_USER_POOL_ID
        COGNITO_INTEGRATION_TEST_CLIENT_ID
        AURORA_HOST
        AURORA_PORT
        AURORA_DBNAME
        AURORA_MASTER_SECRET_ARN
        AWS_REGION
    """
    # ------------------------------------------------------------------
    # Import AWS / DB packages at call time so collection works without them.
    # (boto3 / psycopg2 are only available in a configured AWS environment.)
    # ------------------------------------------------------------------
    try:
        import boto3
    except ImportError:
        pytest.skip("boto3 is not installed — live AWS environment required")

    try:
        import psycopg2
    except ImportError:
        pytest.skip("psycopg2 is not installed — live Aurora environment required")

    # ------------------------------------------------------------------
    # Read required env vars — pytest.skip if any are absent.
    # ------------------------------------------------------------------
    user_pool_id = _require_env("COGNITO_USER_POOL_ID")
    integration_client_id = _require_env("COGNITO_INTEGRATION_TEST_CLIENT_ID")
    aurora_host = _require_env("AURORA_HOST")
    aurora_port = int(_require_env("AURORA_PORT"))
    aurora_dbname = _require_env("AURORA_DBNAME")
    master_secret_arn = _require_env("AURORA_MASTER_SECRET_ARN")
    region = _require_env("AWS_REGION")

    # ------------------------------------------------------------------
    # Synthesise a unique test identity.
    # The knotify-test+<uuid4>@example.com pattern ensures re-runs never
    # collide on Cognito's email-uniqueness constraint.
    # ------------------------------------------------------------------
    test_run_id = str(uuid.uuid4())
    test_email = f"knotify-test+{test_run_id}@example.com"
    test_password = f"Kn0tify!Test#{test_run_id[:8]}"

    cognito_client = boto3.client("cognito-idp", region_name=region)
    sm_client = boto3.client("secretsmanager", region_name=region)

    cognito_sub: str | None = None
    aurora_conn = None

    try:
        # --------------------------------------------------------------
        # Sign up → confirm → sign in
        # --------------------------------------------------------------
        signup_response = cognito_client.sign_up(
            ClientId=integration_client_id,
            Username=test_email,
            Password=test_password,
        )
        cognito_sub = signup_response["UserSub"]

        cognito_client.admin_confirm_sign_up(
            UserPoolId=user_pool_id,
            Username=test_email,
        )

        # Open Aurora connection for use by the consuming test.
        master_creds = _get_aurora_master_creds(sm_client, master_secret_arn)
        aurora_conn = _aurora_master_conn(
            psycopg2,
            host=aurora_host,
            port=aurora_port,
            dbname=aurora_dbname,
            username=master_creds["username"],
            password=master_creds["password"],
        )

        auth_response = cognito_client.admin_initiate_auth(
            UserPoolId=user_pool_id,
            ClientId=integration_client_id,
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={
                "USERNAME": test_email,
                "PASSWORD": test_password,
            },
        )

        auth_result = auth_response["AuthenticationResult"]

        yield {
            "sub": cognito_sub,
            "email": test_email,
            "password": test_password,
            "id_token": auth_result["IdToken"],
            "access_token": auth_result["AccessToken"],
            "refresh_token": auth_result["RefreshToken"],
            # Expose Aurora connection so consuming tests can run DB assertions.
            "_aurora_conn": aurora_conn,
        }

    finally:
        # ------------------------------------------------------------------
        # Teardown — remove the synthetic user from Cognito and Aurora.
        # Errors during teardown are printed to stderr but do not raise so a
        # teardown failure never masks the actual test failure.
        # ------------------------------------------------------------------
        _teardown_user(
            cognito_client=cognito_client,
            sm_client=sm_client,
            psycopg2_module=psycopg2,
            user_pool_id=user_pool_id,
            test_email=test_email,
            cognito_sub=cognito_sub,
            aurora_conn=aurora_conn,
            aurora_host=aurora_host,
            aurora_port=aurora_port,
            aurora_dbname=aurora_dbname,
            master_secret_arn=master_secret_arn,
        )


def _teardown_user(
    *,
    cognito_client,
    sm_client,
    psycopg2_module,
    user_pool_id: str,
    test_email: str,
    cognito_sub: str | None,
    aurora_conn,
    aurora_host: str,
    aurora_port: int,
    aurora_dbname: str,
    master_secret_arn: str,
) -> None:
    """
    Remove the synthetic test user from both Cognito and Aurora.

    Called unconditionally from the signed_in_user fixture's finally block.
    Errors are printed to stderr but never re-raised — a teardown failure must
    not mask the actual test failure.
    """
    # Step 1 — Cognito delete
    try:
        cognito_client.admin_delete_user(
            UserPoolId=user_pool_id,
            Username=test_email,
        )
    except cognito_client.exceptions.UserNotFoundException:
        pass
    except Exception as exc:
        print(
            f"WARN: signed_in_user teardown — admin_delete_user failed: {exc}",
            file=sys.stderr,
        )

    # Step 2 — Aurora DELETE
    if cognito_sub is None:
        return

    fresh_conn_opened = False
    try:
        if aurora_conn is None or aurora_conn.closed:
            master_creds = _get_aurora_master_creds(sm_client, master_secret_arn)
            aurora_conn = _aurora_master_conn(
                psycopg2_module,
                host=aurora_host,
                port=aurora_port,
                dbname=aurora_dbname,
                username=master_creds["username"],
                password=master_creds["password"],
            )
            fresh_conn_opened = True

        with aurora_conn.cursor() as cur:
            cur.execute(
                "DELETE FROM users WHERE user_id = %s::uuid",
                (cognito_sub,),
            )
    except Exception as exc:
        print(
            f"WARN: signed_in_user teardown — Aurora DELETE failed for "
            f"sub={cognito_sub!r}: {exc}",
            file=sys.stderr,
        )
    finally:
        if fresh_conn_opened and aurora_conn is not None:
            try:
                aurora_conn.close()
            except Exception:
                pass
