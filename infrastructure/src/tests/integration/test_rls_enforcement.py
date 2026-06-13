"""
Integration tests: RLS session GUC enforcement at the API layer.

Story 6.6 acceptance criteria — tested here (post-apply against dev):

  AC-RLS-HIDE   A Male user calls GET /v1/profiles?username=<existing-male-username>
                and receives HTTP 404 even though the row exists in the DB.
                Explanation: the knotify-profile Lambda sets the requesting user's
                GUCs before querying the users table. With FORCE ROW LEVEL
                SECURITY applied and the users_opposite_sex_only SELECT policy,
                a Male-A requester cannot see Male-B's row (same sex, different
                identity). The handler converts an empty query result to HTTP 404,
                so the caller sees 404 not 403 — the DB enforcement is invisible
                at the API surface.

  AC-RLS-SELF   The same Male user calls GET /v1/profile/me and receives HTTP 200
                with his own row. The identity-exception branch of the policy
                (OR user_id = current_setting('app.requesting_user_id')::uuid)
                lets the requesting user read their own row regardless of sex.

These tests require:
  - A deployed dev HTTP API + CloudFront stack (phase 5)
  - The knotify-profile Lambda deployed and wired (story 6.1)
  - Two Male users with completed profiles (minted via completed_profile_user)
  - AWS credentials with Cognito, SecretsManager access
  - The .env.test file written by terraform apply (story 5.6)

Required env vars (loaded from .env.test or shell):
  COGNITO_USER_POOL_ID
  COGNITO_INTEGRATION_TEST_CLIENT_ID
  AURORA_HOST, AURORA_PORT, AURORA_DBNAME, AURORA_MASTER_SECRET_ARN
  AWS_REGION
  DISTRIBUTION_DOMAIN_NAME
  EDGE_SECRET

Run against the live dev environment:
    pytest infrastructure/src/tests/integration/test_rls_enforcement.py -v -m integration

Skip without live AWS access:
    pytest -m "not integration"

NOTE (drift advisory — 2026-06-10):
  Dev infrastructure is currently destroyed. These tests are authored and
  committed so CI can run them on the next terraform apply. Until that apply
  completes they will be skipped (missing env vars / DISTRIBUTION_DOMAIN_NAME).
  Integration-test path: PATH 2 (deferred to CI on PR merge).
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(
            f"Required env var {name!r} is not set — live dev environment not configured."
        )
    return value


def _api_url(domain: str, path: str) -> str:
    return f"https://{domain}{path}"


def _authed_headers(access_token: str, edge_secret: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "x-knotify-edge-secret": edge_secret,
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# AC-RLS-HIDE
#
# Male-A calls GET /v1/profiles?username=<Male-B's username>.
#
# The profile Lambda sets:
#   app.requesting_user_id  = Male-A's sub
#   app.requesting_user_sex = 'Male'
# before querying. The RLS policy filters Male-B's row (same sex, different
# identity), so the query returns no rows, and the Lambda responds HTTP 404.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("completed_profile_user", ["Male"], indirect=True)
def test_given_male_requester_when_searching_same_sex_username_then_404(
    completed_profile_user,
    request,
):
    """
    A Male user (Male-A) searching for another Male user's username via
    GET /v1/profiles?username=<Male-B> must receive HTTP 404.

    RLS hides same-sex rows; the Lambda converts an empty DB result to 404.
    This test mints a second Male user (Male-B) as the search target, then
    asserts Male-A's request cannot retrieve Male-B's profile.
    """
    try:
        import requests as http_requests
    except ImportError:
        pytest.skip("requests library not installed — live AWS environment required")

    distribution_domain = _require_env("DISTRIBUTION_DOMAIN_NAME")
    edge_secret = _require_env("EDGE_SECRET")

    # Male-A is the parametrized completed_profile_user fixture.
    male_a = completed_profile_user
    male_a_access_token = male_a["access_token"]

    # Mint Male-B as a separate completed user — the RLS target.
    # We use the signed_in_user fixture indirectly via the conftest factory.
    # To avoid a second completed_profile_user fixture request (which would
    # require nested indirect parametrize), we duplicate the profile-completion
    # call inline using the signed_in_user sub-fixture that completed_profile_user
    # already consumed.  Instead, we use a dedicated second fixture call:
    # NOTE — pytest does not allow ad-hoc fixture invocation inside a test.
    # The correct pattern for "two users of the same sex" is two separate test
    # functions, each parametrized, or a helper that performs the PATCH inline.
    #
    # We choose the inline approach: call PATCH /v1/profile/me for Male-B
    # using the second user created by the `signed_in_user` fixture via the
    # `completed_profile_user` fixture's request mechanism.
    #
    # Simplified: both Male-A AND Male-B are produced by back-to-back
    # completed_profile_user fixtures. pytest's indirect parametrize can only
    # produce one instance per test. We therefore produce Male-B by calling
    # the API directly via a signed-in session that is created ad-hoc here.
    #
    # Practical approach: reuse the Aurora master connection exposed by the
    # completed_profile_user fixture to INSERT Male-B's row directly, giving
    # him a known username. We then have Male-A search for that username.

    import uuid as _uuid

    aurora_conn = male_a["_aurora_conn"]
    male_b_id = _uuid.uuid4()
    male_b_username = f"rls_test_male_b_{_uuid.uuid4().hex[:8]}"

    # Insert Male-B directly as master (bypasses RLS INSERT policy check) so
    # we have a known-male target row without spinning up a second Cognito user.
    with aurora_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO users (
                user_id, email, first_name, last_name, sex,
                birthday, username, religion, profile_complete_verified
            ) VALUES (
                %s::uuid, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (user_id) DO NOTHING
            """,
            (
                str(male_b_id),
                f"rls_male_b_{_uuid.uuid4().hex[:8]}@test.invalid",
                "RLS",
                "MaleB",
                "Male",
                "2000-01-01",
                male_b_username,
                "Other",
                True,
            ),
        )

    try:
        # Male-A searches for Male-B's username via the deployed API.
        url = _api_url(distribution_domain, f"/v1/profiles")
        headers = _authed_headers(male_a_access_token, edge_secret)
        response = http_requests.get(
            url,
            params={"username": male_b_username},
            headers=headers,
        )

        assert response.status_code == 404, (
            f"Expected HTTP 404 (RLS hides same-sex row) but got "
            f"HTTP {response.status_code}. "
            f"Male-A (Male) searching for Male-B's username should be blocked by RLS. "
            f"Response body: {response.text!r}"
        )

    finally:
        # Teardown: remove Male-B's synthetic row.
        with aurora_conn.cursor() as cur:
            cur.execute(
                "DELETE FROM users WHERE user_id = %s::uuid",
                (str(male_b_id),),
            )


# ---------------------------------------------------------------------------
# AC-RLS-SELF
#
# Male-A calls GET /v1/profile/me.
#
# The profile Lambda sets:
#   app.requesting_user_id  = Male-A's sub
#   app.requesting_user_sex = 'Male'
# The identity-exception branch (OR user_id = requesting_user_id) ensures the
# requester always sees their own row. The Lambda responds HTTP 200 with the
# profile JSON.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("completed_profile_user", ["Male"], indirect=True)
def test_given_male_requester_when_fetching_own_profile_then_200(
    completed_profile_user,
):
    """
    A Male user calling GET /v1/profile/me must receive HTTP 200 with their
    own profile. The RLS identity-exception branch ensures a user's own row
    is always visible regardless of sex filtering.
    """
    try:
        import requests as http_requests
    except ImportError:
        pytest.skip("requests library not installed — live AWS environment required")

    distribution_domain = _require_env("DISTRIBUTION_DOMAIN_NAME")
    edge_secret = _require_env("EDGE_SECRET")

    male_a = completed_profile_user
    male_a_access_token = male_a["access_token"]
    male_a_sub = male_a["sub"]

    url = _api_url(distribution_domain, "/v1/profile/me")
    headers = _authed_headers(male_a_access_token, edge_secret)

    response = http_requests.get(url, headers=headers)

    assert response.status_code == 200, (
        f"Expected HTTP 200 for GET /v1/profile/me but got "
        f"HTTP {response.status_code}. "
        f"The RLS identity-exception must let a user read their own row. "
        f"Response body: {response.text!r}"
    )

    body = response.json()
    assert body.get("user_id") == male_a_sub or body.get("userId") == male_a_sub, (
        f"Response user_id {body.get('user_id') or body.get('userId')!r} does not "
        f"match expected sub {male_a_sub!r}. The profile endpoint must return the "
        "requesting user's own row."
    )
