"""
E2E signup integration test for the Knotify Cognito + Aurora stack.

Story 4.6 acceptance criteria — tested here:

  AC1  sign_up against the dev User Pool with a unique per-run email
       (knotify-test+<uuid4>@example.com) using boto3 cognito-idp.
  AC2  admin_confirm_sign_up fires the post-confirmation trigger, which
       inserts a bootstrap users row in Aurora.
  AC3  Query Aurora as master and assert a users row exists with user_id
       matching the Cognito sub.  All profile columns (first_name,
       last_name, sex, birthday, username) are NULL — that is the
       expected bootstrap state.  The test does NOT fail on NULLs.
  AC4  admin_initiate_auth (AuthFlow=ADMIN_USER_PASSWORD_AUTH) against the
       dev-only integration-test app client (story 4.2, NOT the production
       SRP client).  Decode both the returned id_token and access_token
       and assert custom:profile_complete = "false" on both, exercising
       the PreTokenGeneration Lambda (story 4.4 / brainstorm B1).
  AC5  Teardown: admin_delete_user (Cognito) and DELETE FROM users WHERE
       user_id = '<sub>' (Aurora master credential via Secrets Manager).
       Runs even if the test body raises — prevents accumulation.

Token decoding: base64-url-decode the middle segment of the JWT (no
signature verification — we trust the dev environment for this assertion).

Required environment variables (all mandatory — missing vars produce an
explicit pytest.skip so the test is clearly marked as skipped rather than
erroring out with a confusing traceback):

  COGNITO_USER_POOL_ID               — e.g. eu-central-1_abc123
  COGNITO_INTEGRATION_TEST_CLIENT_ID — dev-only client (story 4.2)
  AURORA_HOST                        — writer endpoint of the dev cluster
  AURORA_PORT                        — 5432
  AURORA_DBNAME                      — knotify
  AURORA_MASTER_SECRET_ARN           — ARN of the Aurora-managed master secret
  AWS_REGION                         — e.g. eu-central-1

Run:
    pytest infrastructure/src/tests/integration/ -v -m integration

Skip in any environment that lacks live AWS access:
    pytest -m "not integration"

Test is marked @pytest.mark.integration per story 4.6 notes.

boto3 and psycopg2 are imported inside the test function body so that pytest
can collect (and deselect with -m "not integration") this file even when those
packages are not installed in the local test environment.  They are only
available in a configured AWS/dev environment, not in the docker-compose unit-
test environment.
"""

from __future__ import annotations

import base64
import json
import os
import uuid

import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Environment-variable helper
# ---------------------------------------------------------------------------

def _require_env(name: str) -> str:
    """
    Return the value of environment variable *name*.

    Calls pytest.skip (not raises) if the variable is absent so the test
    is marked SKIPPED rather than ERROR when the live dev environment is
    not configured.  This preserves the "pytest -m integration" contract —
    the test is simply not runnable in that context, which is expected.
    """
    value = os.environ.get(name)
    if not value:
        pytest.skip(
            f"Required env var {name!r} is not set — "
            "live dev AWS environment not configured.  "
            "Set all COGNITO_* / AURORA_* vars to run E2E tests."
        )
    return value


# ---------------------------------------------------------------------------
# JWT payload decoder
# ---------------------------------------------------------------------------

def _decode_jwt_payload(token: str) -> dict:
    """
    Decode the payload segment of a JWT without verifying the signature.

    The middle segment of a JWT is base64url-encoded JSON.  We trust the
    dev Cognito issuer for this assertion; full signature verification is
    not needed and would require fetching the JWKS endpoint.

    Args:
        token: A dot-separated JWT string (header.payload.signature).

    Returns:
        dict of claims decoded from the payload segment.

    Raises:
        ValueError: if *token* is not a three-segment dot-delimited string.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError(
            f"Expected a 3-segment JWT, got {len(parts)} segments: {token[:40]!r}…"
        )
    payload_b64 = parts[1]
    # base64url may omit padding; restore it before decoding.
    padding_needed = 4 - len(payload_b64) % 4
    if padding_needed != 4:
        payload_b64 += "=" * padding_needed
    payload_bytes = base64.urlsafe_b64decode(payload_b64)
    return json.loads(payload_bytes)


# ---------------------------------------------------------------------------
# Aurora master-credential helper
# ---------------------------------------------------------------------------

def _get_aurora_master_creds(sm_client, secret_arn: str) -> dict:
    """
    Fetch the Aurora master credential from Secrets Manager.

    The Aurora-managed secret contains ONLY `username` and `password`;
    connection endpoint params come from the AURORA_* environment variables.

    Args:
        sm_client:  Initialised boto3 secretsmanager client.
        secret_arn: ARN of the Aurora-managed master secret.

    Returns:
        dict with keys "username" and "password".
    """
    response = sm_client.get_secret_value(SecretId=secret_arn)
    return json.loads(response["SecretString"])


# ---------------------------------------------------------------------------
# Aurora connection helper (master credential, bypasses RLS)
# ---------------------------------------------------------------------------

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

    Args:
        psycopg2_module: The psycopg2 module (passed in to avoid module-level
                         import; callers import it before use).
        host:     Aurora writer endpoint.
        port:     Aurora port (5432).
        dbname:   Database name ("knotify").
        username: Master username from Secrets Manager.
        password: Master password from Secrets Manager.

    Returns:
        An open psycopg2 connection with autocommit=True.
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
# E2E signup test
# ---------------------------------------------------------------------------

def test_given_new_signup_when_confirmed_then_users_row_exists_and_profile_complete_claim_is_false():
    """
    E2E signup test for story 4.6.

    Given a fresh Cognito signup with a unique per-run email,
    when the user is admin-confirmed (firing the post-confirmation trigger),
    then:
      - a users row exists in Aurora with user_id = Cognito sub,
      - profile columns (first_name, last_name, sex, birthday, username)
        are NULL (bootstrap state — AC3),
      - admin_initiate_auth against the dev-only integration-test client
        returns id_token and access_token both carrying
        custom:profile_complete = "false" (AC4 / brainstorm B1).

    Teardown removes both the Cognito user and the Aurora row (AC5 /
    brainstorm Md4), even if the test body raises.
    """
    # ------------------------------------------------------------------
    # Import AWS / DB packages at call time so collection works without them
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
    # Read required env vars — pytest.skip if any are absent
    # ------------------------------------------------------------------
    user_pool_id = _require_env("COGNITO_USER_POOL_ID")
    integration_client_id = _require_env("COGNITO_INTEGRATION_TEST_CLIENT_ID")
    aurora_host = _require_env("AURORA_HOST")
    aurora_port = int(_require_env("AURORA_PORT"))
    aurora_dbname = _require_env("AURORA_DBNAME")
    master_secret_arn = _require_env("AURORA_MASTER_SECRET_ARN")
    region = _require_env("AWS_REGION")

    # ------------------------------------------------------------------
    # Synthesise a unique test identity
    # The email uses the knotify-test+<uuid4>@example.com pattern from AC1
    # so re-runs never collide on Cognito's email-uniqueness rule and any
    # outstanding rows from partial prior runs do not block re-runs.
    # ------------------------------------------------------------------
    test_run_id = str(uuid.uuid4())
    test_email = f"knotify-test+{test_run_id}@example.com"
    # Passwords must satisfy the pool's policy: ≥12 chars, upper, lower,
    # digits, symbols.
    test_password = f"Kn0tify!Test#{test_run_id[:8]}"

    cognito_client = boto3.client("cognito-idp", region_name=region)
    sm_client = boto3.client("secretsmanager", region_name=region)

    # sub is populated after sign_up and used for both Aurora teardown and
    # token assertions.
    cognito_sub: str | None = None

    # aurora_conn is opened after confirming the user so the post-confirm
    # trigger has had time to insert the users row.
    aurora_conn = None

    try:
        # ------------------------------------------------------------------
        # AC1 — sign_up against the dev User Pool
        # ------------------------------------------------------------------
        signup_response = cognito_client.sign_up(
            ClientId=integration_client_id,
            Username=test_email,
            Password=test_password,
        )
        cognito_sub = signup_response["UserSub"]

        # ------------------------------------------------------------------
        # AC2 — admin_confirm_sign_up fires the post-confirmation trigger.
        # This is a synchronous call; Cognito invokes the Lambda before
        # returning.  By the time we reach the next line the Lambda has run
        # and the users row has been inserted (or the trigger failed — in
        # which case the assertion below surfaces it).
        # ------------------------------------------------------------------
        cognito_client.admin_confirm_sign_up(
            UserPoolId=user_pool_id,
            Username=test_email,
        )

        # ------------------------------------------------------------------
        # AC3 — query Aurora as master and assert the bootstrap users row
        # ------------------------------------------------------------------
        master_creds = _get_aurora_master_creds(sm_client, master_secret_arn)
        aurora_conn = _aurora_master_conn(
            psycopg2,
            host=aurora_host,
            port=aurora_port,
            dbname=aurora_dbname,
            username=master_creds["username"],
            password=master_creds["password"],
        )

        with aurora_conn.cursor() as cur:
            cur.execute(
                """
                SELECT user_id::text, email,
                       first_name, last_name, sex, birthday, username
                  FROM users
                 WHERE user_id = %s::uuid
                """,
                (cognito_sub,),
            )
            row = cur.fetchone()

        assert row is not None, (
            f"Expected a users row for Cognito sub {cognito_sub!r} "
            f"(email {test_email!r}) but found none.  "
            "The post-confirmation trigger may not have inserted the row — "
            "check CloudWatch logs for knotify-cognito-post-confirmation-dev."
        )

        # Unpack all columns
        db_user_id, db_email, first_name, last_name, sex, birthday, username = row

        assert db_user_id == cognito_sub, (
            f"users.user_id {db_user_id!r} must match Cognito sub {cognito_sub!r}"
        )
        assert db_email == test_email, (
            f"users.email {db_email!r} must match signup email {test_email!r}"
        )

        # AC3 contract: profile columns are NULL in the bootstrap row.
        # The test does NOT fail on NULLs — that IS the expected state.
        # Assertion messages document intent so a future reader understands
        # these are NOT bugs; they are the expected bootstrap state.
        assert first_name is None, (
            f"users.first_name should be NULL for email-only bootstrap row, "
            f"got {first_name!r}.  The post-confirmation Lambda only sets "
            "first_name when the Cognito trigger event includes given_name; "
            "email-only signup leaves it NULL by design."
        )
        assert last_name is None, (
            f"users.last_name should be NULL for email-only bootstrap row, got {last_name!r}."
        )
        assert sex is None, (
            f"users.sex should be NULL for email-only bootstrap row, got {sex!r}."
        )
        assert birthday is None, (
            f"users.birthday should be NULL for email-only bootstrap row, got {birthday!r}."
        )
        assert username is None, (
            f"users.username should be NULL for bootstrap row, got {username!r}.  "
            "Username is set at profile completion (phase 6), not at signup."
        )

        # ------------------------------------------------------------------
        # AC4 — admin_initiate_auth against the dev-only integration-test
        # client (NOT the production SRP client).
        # This fires the PreTokenGeneration V2 Lambda (story 4.4), which
        # reads profile_complete_verified from Aurora and embeds the claim.
        # Brainstorm B1: claim MUST appear on BOTH id_token AND access_token.
        # ------------------------------------------------------------------
        auth_response = cognito_client.admin_initiate_auth(
            UserPoolId=user_pool_id,
            ClientId=integration_client_id,
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={
                "USERNAME": test_email,
                "PASSWORD": test_password,
            },
        )

        id_token = auth_response["AuthenticationResult"]["IdToken"]
        access_token = auth_response["AuthenticationResult"]["AccessToken"]

        id_payload = _decode_jwt_payload(id_token)
        access_payload = _decode_jwt_payload(access_token)

        assert id_payload.get("custom:profile_complete") == "false", (
            f"id_token must carry custom:profile_complete = 'false' for a newly "
            f"confirmed user whose profile_complete_verified = false in Aurora.  "
            f"Got {id_payload.get('custom:profile_complete')!r}.  "
            "Check the PreTokenGeneration Lambda "
            "(knotify-cognito-pre-token-generation-dev) is wired and returning "
            "the claim on idTokenGeneration (brainstorm B1)."
        )
        assert access_payload.get("custom:profile_complete") == "false", (
            f"access_token must carry custom:profile_complete = 'false' for a newly "
            f"confirmed user whose profile_complete_verified = false in Aurora.  "
            f"Got {access_payload.get('custom:profile_complete')!r}.  "
            "Check the PreTokenGeneration Lambda is writing the claim to "
            "accessTokenGeneration — the HTTP API authorizer (phase 5) reads the "
            "access token, not the id token.  Both MUST carry the claim "
            "(brainstorm B1 two-token contract)."
        )

    finally:
        # ------------------------------------------------------------------
        # AC5 — Teardown: remove the Cognito user and the Aurora row.
        # Runs unconditionally so partial test failures do not accumulate
        # synthetic users across repeated runs (brainstorm Md4 resolution).
        # ------------------------------------------------------------------
        _teardown(
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


# ---------------------------------------------------------------------------
# Teardown helper
# ---------------------------------------------------------------------------

def _teardown(
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

    Called unconditionally from the test's `finally` block.  Errors during
    teardown are logged to stderr but do not raise — a teardown failure must
    not mask the actual test failure.

    Steps:
      1. admin_delete_user — removes the Cognito user regardless of
         confirmation state (handles the case where sign_up succeeded but
         admin_confirm_sign_up failed or was not yet called).
      2. DELETE FROM users WHERE user_id = '<sub>' — removes the Aurora
         bootstrap row using the master credential.  Opens a fresh
         connection if aurora_conn is None (e.g., sign_up succeeded but
         the Aurora connection was never opened because confirm failed
         before we got there).

    Args:
        cognito_client:   boto3 cognito-idp client.
        sm_client:        boto3 secretsmanager client.
        psycopg2_module:  The psycopg2 module.
        user_pool_id:     Dev User Pool ID.
        test_email:       The username used at sign_up time.
        cognito_sub:      Cognito sub UUID (None if sign_up itself failed).
        aurora_conn:      Open psycopg2 connection or None.
        aurora_host:      Aurora writer endpoint.
        aurora_port:      Aurora port.
        aurora_dbname:    Database name.
        master_secret_arn: ARN of the Aurora-managed master secret.
    """
    import sys

    # Step 1 — Cognito delete
    try:
        cognito_client.admin_delete_user(
            UserPoolId=user_pool_id,
            Username=test_email,
        )
    except cognito_client.exceptions.UserNotFoundException:
        # sign_up never completed; nothing to delete.
        pass
    except Exception as exc:
        print(
            f"WARN: cognito_signup_test teardown — admin_delete_user failed: {exc}",
            file=sys.stderr,
        )

    # Step 2 — Aurora DELETE
    if cognito_sub is None:
        # sign_up failed before we got a sub; no Aurora row to clean up.
        return

    fresh_conn_opened = False
    try:
        if aurora_conn is None or aurora_conn.closed:
            # The main test body didn't open a connection (e.g., confirm
            # failed before we reached the Aurora query block).  Open a
            # fresh connection for the cleanup DELETE.
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
            f"WARN: cognito_signup_test teardown — Aurora DELETE failed for "
            f"sub={cognito_sub!r}: {exc}",
            file=sys.stderr,
        )
    finally:
        if fresh_conn_opened and aurora_conn is not None:
            try:
                aurora_conn.close()
            except Exception:
                pass
