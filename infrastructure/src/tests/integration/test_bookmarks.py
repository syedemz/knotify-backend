"""
Integration tests for the knotify-bookmarks Lambda.

Story 6.3 acceptance criteria — tested here (post-apply against dev):

  AC-HAPPY  Happy path:
              - Mint A=Male (from fixture), B1=Female, B2=Female inline.
              - A POST /v1/bookmarks {userId: B1} → 200.
              - A POST /v1/bookmarks {userId: B2} → 200.
              - A GET /v1/bookmarks → returns both B1 and B2 with deck-view fields.
              - A DELETE /v1/bookmarks/{B1_id} → 200.
              - A GET /v1/bookmarks → returns only B2.

  AC-IDEMPOTENT  Idempotent POST:
              - A POST /v1/bookmarks {userId: B2} a second time → 200 (not 409).

  AC-BLOCK  Block-aware:
              - Mint B3=Female. B3 blocks A via POST /v1/blocks (seeds friendship first).
              - A POST /v1/bookmarks {userId: B3} → 409 {"error":"blocked"}.

These tests require:
  - A deployed dev HTTP API + CloudFront stack (phase 5)
  - The bookmarks Lambda deployed and wired (story 6.3)
  - The blocks Lambda deployed and wired (story 6.4)
  - The profile Lambda deployed (the completed_profile_user fixture calls
    PATCH /v1/profile/me)
  - AWS credentials with Cognito, SecretsManager access
  - The .env.test file written by terraform apply (story 5.6)

Required env vars (loaded from .env.test or shell):
  COGNITO_USER_POOL_ID
  COGNITO_INTEGRATION_TEST_CLIENT_ID
  AURORA_HOST, AURORA_PORT, AURORA_DBNAME, AURORA_MASTER_SECRET_ARN
  AWS_REGION
  DISTRIBUTION_DOMAIN_NAME
  EDGE_SECRET

Run:
    pytest infrastructure/src/tests/integration/test_bookmarks.py -v -m integration

Skip without live AWS access:
    pytest -m "not integration"

NOTE (drift advisory):
  Dev infrastructure is currently destroyed (run 27188014855). These tests are
  authored and committed so CI can run them on the next terraform apply. Until
  that apply completes they will be skipped (missing env vars).
  Integration-test path: PATH 2 (deferred to CI on PR merge).
"""

from __future__ import annotations

import json
import os
import uuid

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


def _canonical_pair(id_a: str, id_b: str) -> tuple[str, str]:
    """Return (user_a, user_b) in lex-min/max canonical order."""
    return (min(id_a, id_b), max(id_a, id_b))


def _mint_user(
    cognito_client,
    integration_client_id: str,
    user_pool_id: str,
    distribution_domain: str,
    edge_secret: str,
    sex: str,
    http_requests,
) -> dict:
    """
    Create and fully profile a test user inline (without the fixture machinery).

    Returns a dict with: sub, email, password, access_token.
    """
    run_id = str(uuid.uuid4())
    email = f"knotify-test+{run_id}@example.com"
    password = f"Kn0tify!Test#{run_id[:8]}"
    username = f"test_{uuid.uuid4().hex[:12]}"

    signup_resp = cognito_client.sign_up(
        ClientId=integration_client_id,
        Username=email,
        Password=password,
    )
    sub = signup_resp["UserSub"]
    cognito_client.admin_confirm_sign_up(UserPoolId=user_pool_id, Username=email)

    auth_resp = cognito_client.admin_initiate_auth(
        UserPoolId=user_pool_id,
        ClientId=integration_client_id,
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": email, "PASSWORD": password},
    )
    initial_token = auth_resp["AuthenticationResult"]["AccessToken"]

    patch_resp = http_requests.patch(
        f"https://{distribution_domain}/v1/profile/me",
        json={
            "first_name": "Test",
            "last_name": "User",
            "sex": sex,
            "birthday": "2000-01-01",
            "username": username,
            "religion": "Other",
        },
        headers=_authed_headers(initial_token, edge_secret),
    )
    assert patch_resp.status_code == 200, (
        f"Profile PATCH failed for {sex}: HTTP {patch_resp.status_code} {patch_resp.text!r}"
    )

    # Mint fresh tokens so PreTokenGeneration embeds profile_complete = true
    auth_resp2 = cognito_client.admin_initiate_auth(
        UserPoolId=user_pool_id,
        ClientId=integration_client_id,
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": email, "PASSWORD": password},
    )
    access_token = auth_resp2["AuthenticationResult"]["AccessToken"]

    return {
        "sub": sub,
        "email": email,
        "password": password,
        "access_token": access_token,
    }


def _teardown_user(cognito_client, user_pool_id: str, email: str, sub: str, aurora_conn) -> None:
    """Remove a test user from Cognito and Aurora; errors are suppressed."""
    import sys
    try:
        cognito_client.admin_delete_user(UserPoolId=user_pool_id, Username=email)
    except Exception as exc:
        print(f"WARN: teardown admin_delete_user({email!r}) failed: {exc}", file=sys.stderr)
    try:
        with aurora_conn.cursor() as cur:
            cur.execute("DELETE FROM users WHERE user_id = %s::uuid", (sub,))
    except Exception as exc:
        print(f"WARN: teardown Aurora DELETE({sub!r}) failed: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# AC-HAPPY + AC-IDEMPOTENT: Happy path + idempotent POST
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("completed_profile_user", ["Male"], indirect=True)
def test_given_two_female_users_when_a_bookmarks_both_then_get_returns_both_and_delete_removes_one(
    completed_profile_user,
):
    """
    Happy-path + idempotent POST (AC-HAPPY + AC-IDEMPOTENT):

      1. A=Male (from fixture), B1=Female, B2=Female (minted inline).
      2. A POST /v1/bookmarks {userId: B1} → assert 200.
      3. A POST /v1/bookmarks {userId: B2} → assert 200.
      4. A GET /v1/bookmarks → both B1 and B2 present with deck-view fields.
      5. A POST /v1/bookmarks {userId: B2} again → assert 200 (idempotent, not 409).
      6. A DELETE /v1/bookmarks/{B1_id} → assert 200.
      7. A GET /v1/bookmarks → only B2 present.
    """
    try:
        import boto3
        import requests as http_requests
    except ImportError:
        pytest.skip("boto3 or requests not installed — live AWS environment required")

    distribution_domain = _require_env("DISTRIBUTION_DOMAIN_NAME")
    edge_secret = _require_env("EDGE_SECRET")
    region = _require_env("AWS_REGION")
    user_pool_id = _require_env("COGNITO_USER_POOL_ID")
    integration_client_id = _require_env("COGNITO_INTEGRATION_TEST_CLIENT_ID")

    cognito_client = boto3.client("cognito-idp", region_name=region)

    user_a = completed_profile_user
    a_id = user_a["sub"]
    aurora_conn = user_a["_aurora_conn"]

    b1: dict | None = None
    b2: dict | None = None

    try:
        # ------------------------------------------------------------------
        # Mint B1=Female and B2=Female
        # ------------------------------------------------------------------
        b1 = _mint_user(
            cognito_client, integration_client_id, user_pool_id,
            distribution_domain, edge_secret, "Female", http_requests,
        )
        b1_id = b1["sub"]

        b2 = _mint_user(
            cognito_client, integration_client_id, user_pool_id,
            distribution_domain, edge_secret, "Female", http_requests,
        )
        b2_id = b2["sub"]

        # ------------------------------------------------------------------
        # Step 2: A bookmarks B1
        # ------------------------------------------------------------------
        post_b1 = http_requests.post(
            _api_url(distribution_domain, "/v1/bookmarks"),
            json={"userId": b1_id},
            headers=_authed_headers(user_a["access_token"], edge_secret),
        )
        assert post_b1.status_code == 200, (
            f"POST /v1/bookmarks (B1) returned HTTP {post_b1.status_code}: {post_b1.text!r}"
        )

        # ------------------------------------------------------------------
        # Step 3: A bookmarks B2
        # ------------------------------------------------------------------
        post_b2 = http_requests.post(
            _api_url(distribution_domain, "/v1/bookmarks"),
            json={"userId": b2_id},
            headers=_authed_headers(user_a["access_token"], edge_secret),
        )
        assert post_b2.status_code == 200, (
            f"POST /v1/bookmarks (B2) returned HTTP {post_b2.status_code}: {post_b2.text!r}"
        )

        # ------------------------------------------------------------------
        # Step 4: GET /v1/bookmarks → B1 and B2 both present with deck-view fields
        # ------------------------------------------------------------------
        get_resp = http_requests.get(
            _api_url(distribution_domain, "/v1/bookmarks"),
            headers=_authed_headers(user_a["access_token"], edge_secret),
        )
        assert get_resp.status_code == 200, (
            f"GET /v1/bookmarks returned HTTP {get_resp.status_code}"
        )
        bookmarks = get_resp.json().get("bookmarks", [])
        bookmark_ids = [b.get("user_id") for b in bookmarks]
        assert b1_id in bookmark_ids, (
            f"B1 ({b1_id!r}) must appear in GET /v1/bookmarks, got {bookmark_ids!r}"
        )
        assert b2_id in bookmark_ids, (
            f"B2 ({b2_id!r}) must appear in GET /v1/bookmarks, got {bookmark_ids!r}"
        )
        # Verify deck-view fields are present (not leaking full profile)
        for bmark in bookmarks:
            assert "user_id" in bmark, "deck-view must include user_id"
            assert "first_name" in bmark, "deck-view must include first_name"
            assert "sex" in bmark, "deck-view must include sex"
            assert "email" not in bmark, "deck-view must NOT include email"
            assert "phone_number" not in bmark, "deck-view must NOT include phone_number"

        # ------------------------------------------------------------------
        # Step 5: Second POST for B2 → idempotent 200
        # ------------------------------------------------------------------
        post_b2_again = http_requests.post(
            _api_url(distribution_domain, "/v1/bookmarks"),
            json={"userId": b2_id},
            headers=_authed_headers(user_a["access_token"], edge_secret),
        )
        assert post_b2_again.status_code == 200, (
            f"Second POST /v1/bookmarks (B2) must return 200, "
            f"got {post_b2_again.status_code}: {post_b2_again.text!r}"
        )

        # ------------------------------------------------------------------
        # Step 6: A deletes B1 bookmark
        # ------------------------------------------------------------------
        delete_resp = http_requests.delete(
            _api_url(distribution_domain, f"/v1/bookmarks/{b1_id}"),
            headers=_authed_headers(user_a["access_token"], edge_secret),
        )
        assert delete_resp.status_code == 200, (
            f"DELETE /v1/bookmarks/{{b1_id}} returned HTTP {delete_resp.status_code}"
        )

        # ------------------------------------------------------------------
        # Step 7: GET /v1/bookmarks → only B2 present
        # ------------------------------------------------------------------
        get_after = http_requests.get(
            _api_url(distribution_domain, "/v1/bookmarks"),
            headers=_authed_headers(user_a["access_token"], edge_secret),
        )
        assert get_after.status_code == 200
        bookmarks_after = get_after.json().get("bookmarks", [])
        ids_after = [b.get("user_id") for b in bookmarks_after]
        assert b1_id not in ids_after, (
            f"B1 ({b1_id!r}) must no longer appear in GET /v1/bookmarks after DELETE"
        )
        assert b2_id in ids_after, (
            f"B2 ({b2_id!r}) must still appear in GET /v1/bookmarks"
        )

    finally:
        import sys
        # ------------------------------------------------------------------
        # Teardown — remove bookmark rows and users.
        # ------------------------------------------------------------------
        for target_id in ([b1_id] if b1 else []) + ([b2_id] if b2 else []):
            try:
                with aurora_conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM bookmarks WHERE user_id = %s::uuid AND bookmarked_user_id = %s::uuid",
                        (a_id, target_id),
                    )
            except Exception as exc:
                print(
                    f"WARN: teardown bookmarks cleanup (A→{target_id!r}) failed: {exc}",
                    file=sys.stderr,
                )

        if b1 is not None:
            _teardown_user(cognito_client, user_pool_id, b1["email"], b1["sub"], aurora_conn)
        if b2 is not None:
            _teardown_user(cognito_client, user_pool_id, b2["email"], b2["sub"], aurora_conn)


# ---------------------------------------------------------------------------
# AC-BLOCK: Block-aware bookmark POST
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("completed_profile_user", ["Male"], indirect=True)
def test_given_bookmarked_user_has_blocked_requester_when_post_then_returns_409(
    completed_profile_user,
):
    """
    Block-aware integration test (AC-BLOCK):

      1. Mint A=Male (fixture), B3=Female.
      2. Seed a friendship A↔B3 directly so blocks Lambda accepts the block.
      3. B3 POST /v1/blocks {userId: A} → assert 200 (B3 blocks A).
      4. A POST /v1/bookmarks {userId: B3} → assert 409 {"error":"blocked"}.

    Teardown: unblock B3→A, clean up rows.
    """
    try:
        import boto3
        import requests as http_requests
    except ImportError:
        pytest.skip("boto3 or requests not installed — live AWS environment required")

    import sys

    distribution_domain = _require_env("DISTRIBUTION_DOMAIN_NAME")
    edge_secret = _require_env("EDGE_SECRET")
    region = _require_env("AWS_REGION")
    user_pool_id = _require_env("COGNITO_USER_POOL_ID")
    integration_client_id = _require_env("COGNITO_INTEGRATION_TEST_CLIENT_ID")

    cognito_client = boto3.client("cognito-idp", region_name=region)

    user_a = completed_profile_user
    a_id = user_a["sub"]
    aurora_conn = user_a["_aurora_conn"]

    b3: dict | None = None
    b3_blocked_a = False

    try:
        # ------------------------------------------------------------------
        # Mint B3=Female
        # ------------------------------------------------------------------
        b3 = _mint_user(
            cognito_client, integration_client_id, user_pool_id,
            distribution_domain, edge_secret, "Female", http_requests,
        )
        b3_id = b3["sub"]

        # Seed friendship A↔B3 directly so blocks Lambda accepts the POST /v1/blocks
        user_a_col, user_b_col = _canonical_pair(a_id, b3_id)
        with aurora_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO friendships (user_a, user_b, created_at) "
                "VALUES (%s::uuid, %s::uuid, NOW())",
                (user_a_col, user_b_col),
            )

        # B3 blocks A
        block_resp = http_requests.post(
            _api_url(distribution_domain, "/v1/blocks"),
            json={"userId": a_id},
            headers=_authed_headers(b3["access_token"], edge_secret),
        )
        assert block_resp.status_code == 200, (
            f"B3 POST /v1/blocks (→A) returned HTTP {block_resp.status_code}: "
            f"{block_resp.text!r}"
        )
        b3_blocked_a = True

        # A tries to bookmark B3 → must fail with 409 BLOCKED
        bookmark_resp = http_requests.post(
            _api_url(distribution_domain, "/v1/bookmarks"),
            json={"userId": b3_id},
            headers=_authed_headers(user_a["access_token"], edge_secret),
        )
        assert bookmark_resp.status_code == 409, (
            f"Expected HTTP 409 (blocked) when A bookmarks B3 (who blocked A), "
            f"got {bookmark_resp.status_code}: {bookmark_resp.text!r}"
        )
        assert bookmark_resp.json().get("error") == "blocked", (
            f"Expected error='blocked', got {bookmark_resp.json()!r}"
        )

    finally:
        # ------------------------------------------------------------------
        # Teardown
        # ------------------------------------------------------------------
        # Unblock B3→A
        if b3_blocked_a and b3 is not None:
            b3_id_td = b3["sub"]
            try:
                http_requests.delete(
                    _api_url(distribution_domain, f"/v1/blocks/{a_id}"),
                    headers=_authed_headers(b3["access_token"], edge_secret),
                )
            except Exception as exc:
                print(f"WARN: teardown unblock B3→A failed: {exc}", file=sys.stderr)

            try:
                with aurora_conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM blocks WHERE blocker_id = %s::uuid AND blocked_id = %s::uuid",
                        (b3_id_td, a_id),
                    )
            except Exception as exc:
                print(
                    f"WARN: teardown Aurora blocks cleanup (B3→A) failed: {exc}",
                    file=sys.stderr,
                )

        # Clean up any bookmark rows A→B3 that may have been inserted
        if b3 is not None:
            b3_id_td = b3["sub"]
            try:
                with aurora_conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM bookmarks WHERE user_id = %s::uuid "
                        "AND bookmarked_user_id = %s::uuid",
                        (a_id, b3_id_td),
                    )
            except Exception as exc:
                print(
                    f"WARN: teardown bookmarks cleanup (A→B3) failed: {exc}",
                    file=sys.stderr,
                )

            _teardown_user(cognito_client, user_pool_id, b3["email"], b3["sub"], aurora_conn)
