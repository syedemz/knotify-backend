"""
End-to-end integration test exercising all four domain Lambdas in a single flow.

Story 6.7 acceptance criteria — tested here (post-apply against dev):

  Flow (A=Male, B=Female — opposite-sex pair so RLS lets them see each other):

  1. Profile read:
       - A GET /v1/profile/me              → 200, own full profile
       - A GET /v1/profiles/{B_id}         → 200, B's deck-view profile
  2. Profile update:
       - A PATCH /v1/profile/me {job_title: "Engineer"} → 200
       - A GET /v1/profile/me              → 200, job_title == "Engineer"
  3. Friend request send/accept:
       - A POST /v1/friend-requests {toUserId: B} → 201/200, save request_id
       - B POST /v1/friend-requests/{id}/accept  → 200
       - A GET /v1/friends                → B present in A's list
       - B GET /v1/friends                → A present in B's list
  4. Bookmark add/list/remove:
       - A POST /v1/bookmarks {userId: B}  → 200
       - A GET /v1/bookmarks               → B present
       - A DELETE /v1/bookmarks/{B_id}    → 200
       - A GET /v1/bookmarks               → B absent
  5. Block + unblock affecting friend-request behavior:
       - (Requires existing friendship from step 3)
       - A POST /v1/blocks {userId: B}     → 200
       - A POST /v1/friend-requests {toUserId: B} → 409 {"error":"blocked"}
       - A DELETE /v1/blocks/{B_id}        → 200
       - A POST /v1/friend-requests {toUserId: B} → 201/200 (succeeds again)

These tests require:
  - A deployed dev HTTP API + CloudFront stack (phase 5)
  - All four domain Lambdas deployed and wired (stories 6.1–6.4)
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
    make test-e2e
    # or directly:
    pytest infrastructure/src/tests/integration/test_domains_e2e.py -v -m integration

Skip without live AWS access:
    pytest -m "not integration"

NOTE (drift advisory):
  Dev infrastructure is currently destroyed. This test is authored and committed
  so CI can run it on the next terraform apply. Until that apply completes it
  will be skipped (missing env vars).
  Integration-test path: PATH 2 (deferred to CI on PR merge).
"""

from __future__ import annotations

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
# End-to-end flow: all four domain Lambdas exercised in sequence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("completed_profile_user", ["Male"], indirect=True)
def test_given_two_opposite_sex_users_when_full_domain_flow_then_all_lambdas_succeed(
    completed_profile_user,
):
    """
    End-to-end flow exercising profile, friends, bookmarks, and blocks
    domain Lambdas in a single test sequence.

    A=Male (from completed_profile_user fixture), B=Female (minted inline).
    Both are opposite-sex so RLS visibility and friend-request eligibility apply.

    Flow:
      1. Profile read (GET /v1/profile/me, GET /v1/profiles/{B_id})
      2. Profile update (PATCH /v1/profile/me with job_title)
      3. Friend request send/accept (POST → accept → GET /v1/friends both sides)
      4. Bookmark add/list/remove (POST → GET → DELETE → GET)
      5. Block + unblock affecting friend-request behavior
         (POST /v1/blocks → POST /v1/friend-requests returns 409 →
          DELETE /v1/blocks → POST /v1/friend-requests succeeds)

    Teardown cleans all seeded data unconditionally.
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

    b: dict | None = None
    # Track state for teardown
    friendship_seeded = False
    a_blocked_b = False
    request_id_first: str | None = None
    request_id_second: str | None = None

    try:
        # ------------------------------------------------------------------
        # Mint B=Female
        # ------------------------------------------------------------------
        b = _mint_user(
            cognito_client, integration_client_id, user_pool_id,
            distribution_domain, edge_secret, "Female", http_requests,
        )
        b_id = b["sub"]

        # ==================================================================
        # Step 1 — Profile read
        # ==================================================================

        # A GET /v1/profile/me → 200, own full profile
        get_me_resp = http_requests.get(
            _api_url(distribution_domain, "/v1/profile/me"),
            headers=_authed_headers(user_a["access_token"], edge_secret),
        )
        assert get_me_resp.status_code == 200, (
            f"GET /v1/profile/me returned HTTP {get_me_resp.status_code}: "
            f"{get_me_resp.text!r}"
        )
        me_body = get_me_resp.json()
        assert me_body.get("user_id") == a_id, (
            f"GET /v1/profile/me user_id mismatch: expected {a_id!r}, got "
            f"{me_body.get('user_id')!r}"
        )
        # Full profile includes email (not deck-view restricted)
        assert "email" in me_body, "GET /v1/profile/me must return email for own profile"

        # A GET /v1/profiles/{B_id} → 200, B's deck-view profile
        get_b_resp = http_requests.get(
            _api_url(distribution_domain, f"/v1/profiles/{b_id}"),
            headers=_authed_headers(user_a["access_token"], edge_secret),
        )
        assert get_b_resp.status_code == 200, (
            f"GET /v1/profiles/{{B_id}} returned HTTP {get_b_resp.status_code}: "
            f"{get_b_resp.text!r}"
        )
        b_profile_body = get_b_resp.json()
        assert b_profile_body.get("user_id") == b_id, (
            f"GET /v1/profiles/{{B_id}} user_id mismatch: expected {b_id!r}, "
            f"got {b_profile_body.get('user_id')!r}"
        )
        # Deck-view must NOT expose email
        assert "email" not in b_profile_body, (
            "GET /v1/profiles/{userId} must NOT include email in deck-view"
        )

        # ==================================================================
        # Step 2 — Profile update (mutable field: job_title)
        # ==================================================================

        patch_update_resp = http_requests.patch(
            _api_url(distribution_domain, "/v1/profile/me"),
            json={"job_title": "Engineer"},
            headers=_authed_headers(user_a["access_token"], edge_secret),
        )
        assert patch_update_resp.status_code == 200, (
            f"PATCH /v1/profile/me (job_title update) returned HTTP "
            f"{patch_update_resp.status_code}: {patch_update_resp.text!r}"
        )

        # Read back to confirm update persisted
        get_me_after_patch = http_requests.get(
            _api_url(distribution_domain, "/v1/profile/me"),
            headers=_authed_headers(user_a["access_token"], edge_secret),
        )
        assert get_me_after_patch.status_code == 200
        assert get_me_after_patch.json().get("job_title") == "Engineer", (
            f"Expected job_title='Engineer' after PATCH, got "
            f"{get_me_after_patch.json().get('job_title')!r}"
        )

        # ==================================================================
        # Step 3 — Friend request send / accept / list
        # ==================================================================

        # A sends friend-request to B
        post_fr_resp = http_requests.post(
            _api_url(distribution_domain, "/v1/friend-requests"),
            json={"toUserId": b_id},
            headers=_authed_headers(user_a["access_token"], edge_secret),
        )
        assert post_fr_resp.status_code in (200, 201), (
            f"POST /v1/friend-requests returned HTTP {post_fr_resp.status_code}: "
            f"{post_fr_resp.text!r}"
        )
        request_id_first = post_fr_resp.json().get("id")
        assert request_id_first, (
            f"Expected 'id' in POST /v1/friend-requests response, got "
            f"{post_fr_resp.json()!r}"
        )

        # B accepts
        accept_resp = http_requests.post(
            _api_url(distribution_domain, f"/v1/friend-requests/{request_id_first}/accept"),
            headers=_authed_headers(b["access_token"], edge_secret),
        )
        assert accept_resp.status_code == 200, (
            f"POST /v1/friend-requests/{{id}}/accept returned HTTP "
            f"{accept_resp.status_code}: {accept_resp.text!r}"
        )
        friendship_seeded = True

        # GET /v1/friends from A → B must be present
        get_friends_a_resp = http_requests.get(
            _api_url(distribution_domain, "/v1/friends"),
            headers=_authed_headers(user_a["access_token"], edge_secret),
        )
        assert get_friends_a_resp.status_code == 200, (
            f"GET /v1/friends (A) returned HTTP {get_friends_a_resp.status_code}"
        )
        a_friends = get_friends_a_resp.json().get("friends", [])
        a_friend_ids = [f.get("user_id") for f in a_friends]
        assert b_id in a_friend_ids, (
            f"B ({b_id!r}) must appear in A's friend list, got {a_friend_ids!r}"
        )

        # GET /v1/friends from B → A must be present
        get_friends_b_resp = http_requests.get(
            _api_url(distribution_domain, "/v1/friends"),
            headers=_authed_headers(b["access_token"], edge_secret),
        )
        assert get_friends_b_resp.status_code == 200, (
            f"GET /v1/friends (B) returned HTTP {get_friends_b_resp.status_code}"
        )
        b_friends = get_friends_b_resp.json().get("friends", [])
        b_friend_ids = [f.get("user_id") for f in b_friends]
        assert a_id in b_friend_ids, (
            f"A ({a_id!r}) must appear in B's friend list, got {b_friend_ids!r}"
        )

        # ==================================================================
        # Step 4 — Bookmark add / list / remove
        # ==================================================================

        # A bookmarks B
        post_bm_resp = http_requests.post(
            _api_url(distribution_domain, "/v1/bookmarks"),
            json={"userId": b_id},
            headers=_authed_headers(user_a["access_token"], edge_secret),
        )
        assert post_bm_resp.status_code == 200, (
            f"POST /v1/bookmarks returned HTTP {post_bm_resp.status_code}: "
            f"{post_bm_resp.text!r}"
        )

        # GET /v1/bookmarks → B must be present
        get_bm_resp = http_requests.get(
            _api_url(distribution_domain, "/v1/bookmarks"),
            headers=_authed_headers(user_a["access_token"], edge_secret),
        )
        assert get_bm_resp.status_code == 200, (
            f"GET /v1/bookmarks returned HTTP {get_bm_resp.status_code}"
        )
        bookmarks = get_bm_resp.json().get("bookmarks", [])
        bookmark_ids = [bm.get("user_id") for bm in bookmarks]
        assert b_id in bookmark_ids, (
            f"B ({b_id!r}) must appear in GET /v1/bookmarks after POST, "
            f"got {bookmark_ids!r}"
        )

        # DELETE /v1/bookmarks/{B_id} → 200
        delete_bm_resp = http_requests.delete(
            _api_url(distribution_domain, f"/v1/bookmarks/{b_id}"),
            headers=_authed_headers(user_a["access_token"], edge_secret),
        )
        assert delete_bm_resp.status_code == 200, (
            f"DELETE /v1/bookmarks/{{B_id}} returned HTTP {delete_bm_resp.status_code}"
        )

        # GET /v1/bookmarks → B must be absent
        get_bm_after_resp = http_requests.get(
            _api_url(distribution_domain, "/v1/bookmarks"),
            headers=_authed_headers(user_a["access_token"], edge_secret),
        )
        assert get_bm_after_resp.status_code == 200
        bookmark_ids_after = [
            bm.get("user_id") for bm in get_bm_after_resp.json().get("bookmarks", [])
        ]
        assert b_id not in bookmark_ids_after, (
            f"B ({b_id!r}) must NOT appear in GET /v1/bookmarks after DELETE, "
            f"got {bookmark_ids_after!r}"
        )

        # ==================================================================
        # Step 5 — Block + unblock affecting friend-request behavior
        #
        # Precondition: A and B are already friends (friendship_seeded = True).
        # POST /v1/blocks requires an existing friendship row.
        # ==================================================================

        # A blocks B (friendship row is consumed — deleted by blocks Lambda)
        block_resp = http_requests.post(
            _api_url(distribution_domain, "/v1/blocks"),
            json={"userId": b_id},
            headers=_authed_headers(user_a["access_token"], edge_secret),
        )
        assert block_resp.status_code == 200, (
            f"POST /v1/blocks (A→B) returned HTTP {block_resp.status_code}: "
            f"{block_resp.text!r}"
        )
        a_blocked_b = True
        # Blocks Lambda deletes the friendship row — mark it gone
        friendship_seeded = False

        # A tries to send a friend-request to B → must return 409 BLOCKED
        fr_blocked_resp = http_requests.post(
            _api_url(distribution_domain, "/v1/friend-requests"),
            json={"toUserId": b_id},
            headers=_authed_headers(user_a["access_token"], edge_secret),
        )
        assert fr_blocked_resp.status_code == 409, (
            f"Expected HTTP 409 (blocked) when A sends friend-request while block "
            f"is active, got {fr_blocked_resp.status_code}: {fr_blocked_resp.text!r}"
        )
        assert fr_blocked_resp.json().get("error") == "blocked", (
            f"Expected error='blocked', got {fr_blocked_resp.json()!r}"
        )

        # A unblocks B
        unblock_resp = http_requests.delete(
            _api_url(distribution_domain, f"/v1/blocks/{b_id}"),
            headers=_authed_headers(user_a["access_token"], edge_secret),
        )
        assert unblock_resp.status_code == 200, (
            f"DELETE /v1/blocks/{{B_id}} returned HTTP {unblock_resp.status_code}: "
            f"{unblock_resp.text!r}"
        )
        a_blocked_b = False

        # A sends a friend-request to B again → must succeed now
        fr_after_unblock_resp = http_requests.post(
            _api_url(distribution_domain, "/v1/friend-requests"),
            json={"toUserId": b_id},
            headers=_authed_headers(user_a["access_token"], edge_secret),
        )
        assert fr_after_unblock_resp.status_code in (200, 201), (
            f"POST /v1/friend-requests after unblock must succeed, "
            f"got HTTP {fr_after_unblock_resp.status_code}: "
            f"{fr_after_unblock_resp.text!r}"
        )
        request_id_second = fr_after_unblock_resp.json().get("id")

    finally:
        import sys
        # ------------------------------------------------------------------
        # Teardown — clean up all rows seeded or mutated in this test.
        # Errors are printed but not re-raised.
        # ------------------------------------------------------------------
        if b is None:
            return

        b_id_td = b["sub"]

        # 5a. If A still has an active block on B, unblock via API then clean DB
        if a_blocked_b:
            try:
                http_requests.delete(
                    _api_url(distribution_domain, f"/v1/blocks/{b_id_td}"),
                    headers=_authed_headers(user_a["access_token"], edge_secret),
                )
            except Exception as exc:
                print(f"WARN: teardown unblock A→B (API) failed: {exc}", file=sys.stderr)
            try:
                with aurora_conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM blocks WHERE blocker_id = %s::uuid "
                        "AND blocked_id = %s::uuid",
                        (a_id, b_id_td),
                    )
            except Exception as exc:
                print(
                    f"WARN: teardown Aurora blocks cleanup (A→B) failed: {exc}",
                    file=sys.stderr,
                )

        # 5b. Clean remaining block rows in either direction (belt-and-suspenders)
        try:
            with aurora_conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM blocks WHERE "
                    "(blocker_id = %s::uuid AND blocked_id = %s::uuid) "
                    "OR (blocker_id = %s::uuid AND blocked_id = %s::uuid)",
                    (a_id, b_id_td, b_id_td, a_id),
                )
        except Exception as exc:
            print(f"WARN: teardown blocks sweep failed: {exc}", file=sys.stderr)

        # 3a. Clean friendship row (may have been removed by the blocks Lambda)
        if friendship_seeded:
            try:
                user_a_col, user_b_col = _canonical_pair(a_id, b_id_td)
                with aurora_conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM friendships WHERE user_a = %s::uuid AND user_b = %s::uuid",
                        (user_a_col, user_b_col),
                    )
            except Exception as exc:
                print(f"WARN: teardown friendship cleanup failed: {exc}", file=sys.stderr)

        # Clean any remaining friendship row unconditionally (belt-and-suspenders)
        try:
            user_a_col, user_b_col = _canonical_pair(a_id, b_id_td)
            with aurora_conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM friendships WHERE user_a = %s::uuid AND user_b = %s::uuid",
                    (user_a_col, user_b_col),
                )
        except Exception as exc:
            print(f"WARN: teardown friendships sweep failed: {exc}", file=sys.stderr)

        # 3b. Clean all friend_request rows between A and B
        try:
            with aurora_conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM friend_requests WHERE "
                    "(requester_id = %s::uuid AND receiver_id = %s::uuid) "
                    "OR (requester_id = %s::uuid AND receiver_id = %s::uuid)",
                    (a_id, b_id_td, b_id_td, a_id),
                )
        except Exception as exc:
            print(f"WARN: teardown friend_requests cleanup failed: {exc}", file=sys.stderr)

        # 4. Clean any leftover bookmark rows
        try:
            with aurora_conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM bookmarks WHERE "
                    "(user_id = %s::uuid AND bookmarked_user_id = %s::uuid) "
                    "OR (user_id = %s::uuid AND bookmarked_user_id = %s::uuid)",
                    (a_id, b_id_td, b_id_td, a_id),
                )
        except Exception as exc:
            print(f"WARN: teardown bookmarks cleanup failed: {exc}", file=sys.stderr)

        # Teardown user B (Cognito + Aurora users row)
        _teardown_user(cognito_client, user_pool_id, b["email"], b_id_td, aurora_conn)
