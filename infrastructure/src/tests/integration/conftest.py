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
# Profile-completion payload helper (shared across integration tests)
#
# Story 7.0b widened the profile_complete_verified CHECK constraint from 5 to
# 34 required fields (migration 0012). Any PATCH /v1/profile/me that aims to
# flip profile_complete_verified=true (and thereby trigger the Cognito
# custom:profile_complete attribute write) must populate all 34 fields.
# This helper is the single source of truth for the 34-field payload used by
# every test that needs a completed-profile user.
# ---------------------------------------------------------------------------

def build_profile_completion_payload(*, sex: str, username: str) -> dict:
    """
    Return a 34-field PATCH body sufficient to flip profile_complete_verified
    to true under the post-7.0b CHECK constraint.

    Field values are intentionally trivial — these tests don't assert on
    field semantics, only on completion gating. Booleans match the column
    type (BOOLEAN in Aurora); the *_retired columns are TEXT.
    """
    return {
        # Identity (immutable-after-set)
        "first_name": "Test",
        "last_name": "User",
        "sex": sex,
        "birthday": "2000-01-01",
        "username": username,
        # Religion
        "religion": "Other",
        "subsect": "Other",
        "religious_level": "Practicing",
        # Residence
        "current_residence_city": "Berlin",
        "current_residence_country": "Germany",
        "resident_country_code": "DE",
        "district": "Mitte",
        # Education
        "education_level": "Graduate",
        "highest_degree": "MSc",
        "high_school": "Test High School",
        "higher_secondary": "Test Higher Secondary",
        "college_name": "Test College",
        # Profession
        "job_title": "Engineer",
        "employer_name": "TestCo",
        "employment_type": "Full-time",
        "office_address": "123 Test St",
        "professional_category": "Technology",
        "salary_range": "100-150k",
        # Family
        "fathers_name": "Father Test",
        "fathers_job": "Retired",
        "father_retired": "Yes",
        "mothers_name": "Mother Test",
        "mothers_job": "Retired",
        "mother_retired": "Yes",
        "family_residence_address": "456 Family Rd",
        # Personal status
        "marital_status": "Single",
        "has_children": False,
        "move_abroad": False,
        "relation": "Self",
    }


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


# ---------------------------------------------------------------------------
# completed_profile_user fixture — story 6.1
#
# Parametrized factory built on top of signed_in_user. Performs a full
# profile-completion PATCH via the dev API, then mints a FRESH Cognito token
# pair (because PreTokenGeneration only fires at login/refresh, not at PATCH).
#
# Three-step protocol (Brainstorm NEW-6):
#   Step 1 — consume signed_in_user for the initial token pair
#             (custom:profile_complete = "false" at this point)
#   Step 2 — PATCH /v1/profile/me with the completion payload using the
#             initial access_token; assert HTTP 200 and verify via master
#             Aurora connection that profile_complete_verified flipped.
#   Step 3 — call admin_initiate_auth AGAIN to mint a fresh token pair;
#             assert custom:profile_complete = "true" on both id_token and
#             access_token. Yield the fresh pair, NOT the initial tokens.
#
# The teardown chain is fully delegated to signed_in_user — the fixture is
# "layered on top" and adds no new teardown state.
#
# Additional required env vars beyond what signed_in_user needs:
#   DISTRIBUTION_DOMAIN_NAME — CloudFront FQDN for the dev API
#   EDGE_SECRET              — value of the x-knotify-edge-secret header
#
# Both are written by the Terraform local_file resource in story 5.6.
#
# Consumed by:
#   - infrastructure/src/tests/integration/test_profile.py (story 6.1)
#   - infrastructure/src/tests/integration/test_blocks.py  (story 6.4)
#   - ... subsequent phase-6 domain Lambda tests
# ---------------------------------------------------------------------------


def _decode_jwt_claims(token: str) -> dict:
    """
    Decode the claims from a JWT's payload section WITHOUT signature verification.

    Only for integration test assertion purposes — the token was just minted by
    Cognito and delivered over HTTPS; we do not need to re-verify the signature
    inside the test runner.
    """
    import base64
    payload_b64 = token.split(".")[1]
    # Add padding if needed
    padding = 4 - len(payload_b64) % 4
    if padding != 4:
        payload_b64 += "=" * padding
    return json.loads(base64.urlsafe_b64decode(payload_b64))


@pytest.fixture(scope="function")
def completed_profile_user(request, signed_in_user):
    """
    Yield a dict with credentials for a fully-profiled test user.

    Usage (parametrize via indirect):
        @pytest.mark.parametrize("completed_profile_user", ["Male"], indirect=True)
        def test_something(completed_profile_user): ...

    Or call the fixture factory with the sex parameter via
    pytest.fixture(params=["Male", "Female"]).

    The fixture accepts a sex parameter via request.param (default "Male").
    Callers that need a specific sex must parametrize or pass via indirect.

    Yields a dict with ALL keys from signed_in_user PLUS:
        first_name        — "Test"
        last_name         — "User"
        sex               — the requested sex ("Male" or "Female")
        birthday          — "2000-01-01"
        username          — f"test_{uuid4().hex[:12]}"
        religion          — "Other"
        id_token          — FRESH post-PATCH Cognito ID token
        access_token      — FRESH post-PATCH Cognito access token
        refresh_token     — FRESH post-PATCH Cognito refresh token

    The initial tokens from signed_in_user are discarded (Cognito tokens are
    immutable; PreTokenGeneration fires only at login/refresh, not at PATCH).
    """
    # The sex parameter is supplied via request.param when the fixture is
    # invoked indirectly; default to "Male" for non-parametrized usage.
    sex: str = getattr(request, "param", "Male")

    try:
        import boto3
        import requests as http_requests
    except ImportError:
        pytest.skip("boto3 or requests not installed — live AWS environment required")

    distribution_domain = _require_env("DISTRIBUTION_DOMAIN_NAME")
    edge_secret = _require_env("EDGE_SECRET")
    user_pool_id = _require_env("COGNITO_USER_POOL_ID")
    integration_client_id = _require_env("COGNITO_INTEGRATION_TEST_CLIENT_ID")
    region = _require_env("AWS_REGION")

    cognito_client = boto3.client("cognito-idp", region_name=region)
    aurora_conn = signed_in_user["_aurora_conn"]

    # ------------------------------------------------------------------
    # Step 1: initial token pair already in signed_in_user
    # (custom:profile_complete = "false" at this point)
    # ------------------------------------------------------------------
    initial_access_token = signed_in_user["access_token"]
    user_id = signed_in_user["sub"]
    username = f"test_{uuid.uuid4().hex[:12]}"

    # ------------------------------------------------------------------
    # Step 2: PATCH /v1/profile/me with the completion payload
    # ------------------------------------------------------------------
    patch_url = f"https://{distribution_domain}/v1/profile/me"
    completion_payload = build_profile_completion_payload(sex=sex, username=username)
    headers = {
        "Authorization": f"Bearer {initial_access_token}",
        "x-knotify-edge-secret": edge_secret,
        "Content-Type": "application/json",
    }

    patch_response = http_requests.patch(patch_url, json=completion_payload, headers=headers)
    assert patch_response.status_code == 200, (
        f"completed_profile_user PATCH failed: HTTP {patch_response.status_code} "
        f"body={patch_response.text!r}"
    )

    # Verify profile_complete_verified flipped to true via master Aurora connection
    with aurora_conn.cursor() as cur:
        cur.execute(
            "SELECT profile_complete_verified FROM users WHERE user_id = %s::uuid",
            (user_id,),
        )
        row = cur.fetchone()
    assert row is not None, f"users row not found for sub={user_id!r}"
    assert row[0] is True, (
        f"profile_complete_verified is {row[0]!r}, expected True "
        f"after PATCH /v1/profile/me for user_id={user_id!r}"
    )

    # ------------------------------------------------------------------
    # Step 3: mint fresh tokens (PreTokenGeneration runs at this login)
    # ------------------------------------------------------------------
    auth_response = cognito_client.admin_initiate_auth(
        UserPoolId=user_pool_id,
        ClientId=integration_client_id,
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={
            "USERNAME": signed_in_user["email"],
            "PASSWORD": signed_in_user["password"],
        },
    )
    fresh_auth = auth_response["AuthenticationResult"]

    # Assert both tokens carry custom:profile_complete = "true"
    id_claims = _decode_jwt_claims(fresh_auth["IdToken"])
    access_claims = _decode_jwt_claims(fresh_auth["AccessToken"])
    assert id_claims.get("custom:profile_complete") == "true", (
        f"id_token custom:profile_complete is {id_claims.get('custom:profile_complete')!r}, "
        "expected 'true' after profile completion"
    )
    assert access_claims.get("custom:profile_complete") == "true", (
        f"access_token custom:profile_complete is {access_claims.get('custom:profile_complete')!r}, "
        "expected 'true' after profile completion"
    )

    # Yield the complete dict — teardown is handled by signed_in_user
    yield {
        **signed_in_user,
        # Profile fields
        "first_name": "Test",
        "last_name": "User",
        "sex": sex,
        "birthday": "2000-01-01",
        "username": username,
        "religion": "Other",
        # Fresh post-PATCH tokens (initial tokens discarded — Cognito tokens are immutable)
        "id_token": fresh_auth["IdToken"],
        "access_token": fresh_auth["AccessToken"],
        "refresh_token": fresh_auth["RefreshToken"],
    }


# ---------------------------------------------------------------------------
# app_user_conn fixture — story 6.6 (Brainstorm N4 + Mi2)
#
# Opens a psycopg2 connection to Aurora as the `app_user` role (NOT master).
# RLS only fires against non-superuser, non-BYPASSRLS roles under FORCE ROW
# LEVEL SECURITY; any direct-DB test that needs the policy to apply must use
# this fixture rather than the master connection exposed by signed_in_user.
#
# Skip-gate (Brainstorm Mi2):
#   If `secretsmanager.GetSecretValue` raises ResourceNotFoundException, the
#   fixture calls pytest.skip(...) so the test is a no-op on a freshly-
#   destroyed dev environment (the secret is created by the db_migrator Lambda
#   on the first phase-6 apply — see migration 0007 header). Once the secret
#   materialises the fixture connects normally.
#
# Credential source:
#   Secret name: knotify-${env}-app-user-credential (env defaults to "dev").
#   Secret payload: {"username": "app_user", "password": "<random>"}
#
# Required env vars:
#   AURORA_HOST, AURORA_PORT, AURORA_DBNAME, AWS_REGION
#   APP_USER_SECRET_NAME (optional override; default: knotify-dev-app-user-credential)
#
# Yields a psycopg2 connection object. Teardown closes it.
#
# Note: the live tests in test_rls_enforcement.py drive the deployed HTTP API
# and therefore exercise RLS via the Lambda's internal connection. They do NOT
# use app_user_conn directly. The fixture exists here for future RLS unit
# tests that need a direct DB connection under the app_user role.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="function")
def app_user_conn():
    """
    Yield a psycopg2 connection to Aurora as app_user (RLS-enforced role).

    Skips the test if the app_user credential secret does not exist yet
    (ResourceNotFoundException means the db_migrator Lambda has not run).
    """
    try:
        import boto3
    except ImportError:
        pytest.skip("boto3 is not installed — live AWS environment required")

    try:
        import psycopg2 as _psycopg2
    except ImportError:
        pytest.skip("psycopg2 is not installed — live Aurora environment required")

    aurora_host = _require_env("AURORA_HOST")
    aurora_port = int(_require_env("AURORA_PORT"))
    aurora_dbname = _require_env("AURORA_DBNAME")
    region = _require_env("AWS_REGION")

    env = os.environ.get("KNOTIFY_ENV", "dev")
    secret_name = os.environ.get(
        "APP_USER_SECRET_NAME",
        f"knotify-{env}-app-user-credential",
    )

    sm_client = boto3.client("secretsmanager", region_name=region)

    try:
        response = sm_client.get_secret_value(SecretId=secret_name)
    except sm_client.exceptions.ResourceNotFoundException:
        pytest.skip(
            f"app_user credential not yet materialized — "
            f"first phase-6 apply has not run db_migrator "
            f"(secret {secret_name!r} does not exist)"
        )

    creds = json.loads(response["SecretString"])
    app_user_password = creds["password"]

    conn = _psycopg2.connect(
        host=aurora_host,
        port=aurora_port,
        dbname=aurora_dbname,
        user="app_user",
        password=app_user_password,
    )

    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# seeded_match_candidates fixture — story 7.6
#
# Creates n synthetic candidate users whose preference_vectors are the
# requester's vector plus a deterministic perturbation with monotonically-
# growing L2 norm.  This produces a strict, predictable cosine-distance
# ranking when combined with the requester's own vector.
#
# Perturbation strategy (one non-zero component per candidate, at position i):
#   perturbation_i = [0, 0, ..., (i+1)*0.1, ..., 0]   (component at index i)
#
# Because each perturbation moves the candidate away from the requester's
# direction in a unique dimension, the cosine distances are strictly ordered
# (candidate 0 is closest, candidate n-1 is farthest) when the requester's
# vector is non-zero and its non-zero components do not overlap with the
# perturbation positions.
#
# Seeded users:
#   - All have profile_complete_verified = true
#   - All are the opposite sex to the requester
#   - All share the same resident_country_code and religion as the requester
#     (so they appear in a filtered search without extra filter wrangling)
#
# Usage:
#   def test_something(seeded_match_candidates):
#       result = seeded_match_candidates(
#           conn=master_conn,
#           requester_id="...",
#           requester_sex="Male",
#           requester_vector=[1.0, 0.0, ...],
#           n=10,
#       )
#       # result["candidate_ids"]: list[str] — in expected cosine-ranking order
#       # result["top_id"]: str — the candidate closest to the requester (rank 1)
#
# Teardown: the caller is responsible for deleting the seeded users (done
# inside the test's finally block via the master connection, same pattern as
# test_match_search.py and test_match_deck.py).
#
# Rationale for a fixture rather than a plain helper: it follows the existing
# conftest.py pattern for shared reusable test infrastructure; keeps the
# test file focused on assertions.
# ---------------------------------------------------------------------------


def seeded_match_candidates(
    conn,
    *,
    requester_id: str,
    requester_sex: str,
    requester_vector: list[float],
    n: int = 10,
    religion: str = "Islam",
    resident_country_code: str = "GB",
) -> dict:
    """
    Seed n candidate users with deterministic preference vectors.

    Each candidate's preference_vector is:
        requester_vector + perturbation_i
    where perturbation_i has a single non-zero component at position i
    with magnitude (i+1)*0.1, giving a strictly increasing L2 distance
    from the requester vector.

    Args:
        conn:                  psycopg2 connection with autocommit=True
                               (master credentials, bypasses RLS).
        requester_id:          user_id of the requester (used only to derive
                               opposite sex and to avoid collision).
        requester_sex:         "Male" or "Female"; candidates will be the
                               opposite sex.
        requester_vector:      The requester's 20-D preference_vector
                               (list of 20 floats). Must be non-zero.
        n:                     Number of candidates to seed (default 10).
        religion:              religion value for all seeded candidates.
        resident_country_code: CHAR(2) code for all seeded candidates.

    Returns:
        dict with:
            "candidate_ids":   list[str] — user_ids in expected cosine-
                               ranking order (index 0 = closest to requester).
            "top_id":          str — the top-ranked candidate's user_id.

    Note:
        The perturbation positions start at index 0 in the 20-D space.
        The requester's vector must have its non-zero components outside
        positions 0..(n-1) to guarantee a strict ordering.  For n=10 the
        requester vector should have its signal at index 10 or higher.
        (See test_match_e2e.py for the concrete vector choice.)
    """
    import uuid

    opposite_sex = "Female" if requester_sex == "Male" else "Male"
    candidate_ids: list[str] = []
    dim = len(requester_vector)

    with conn.cursor() as cur:
        for i in range(n):
            # Build the perturbation vector: single non-zero component at
            # position i with magnitude (i+1)*0.1.  Adding this to the
            # requester vector moves the candidate away from the requester's
            # direction; the further from the requester direction, the lower
            # the cosine similarity → higher cosine distance → lower rank.
            perturbation = [0.0] * dim
            perturbation[i] = (i + 1) * 0.1
            candidate_vector = [
                requester_vector[d] + perturbation[d] for d in range(dim)
            ]

            cid = str(uuid.uuid4())
            candidate_ids.append(cid)

            cur.execute(
                """
                INSERT INTO users (
                    user_id, email, sex, religion,
                    resident_country_code,
                    profile_complete_verified,
                    preference_vector
                )
                VALUES (
                    %s::uuid, %s, %s, %s,
                    %s,
                    %s,
                    %s::vector
                )
                ON CONFLICT (user_id) DO NOTHING
                """,
                (
                    cid,
                    f"e2e_cand_{i}_{cid[:8]}@test.invalid",
                    opposite_sex,
                    religion,
                    resident_country_code,
                    True,
                    str(candidate_vector),
                ),
            )

    return {
        "candidate_ids": candidate_ids,
        "top_id": candidate_ids[0],
    }
