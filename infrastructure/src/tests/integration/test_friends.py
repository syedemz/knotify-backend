"""
Integration tests for the knotify-friends Lambda.

Story 6.2 acceptance criteria — tested here (post-apply against dev):

  AC-HAPPY  Happy path:
              - Mint A=Male and B=Female via completed_profile_user.
              - A POST /v1/friend-requests {toUserId: B} → 201/200.
              - B POST /v1/friend-requests/{id}/accept → 200.
              - GET /v1/friends from A shows B in the list.
              - GET /v1/friends from B shows A in the list.
              - A DELETE /v1/friends/{B_id} → 200.
              - Subsequent GET /v1/friends from A returns empty list.

  AC-BLOCK  Block-aware:
              - Mint A and B. A POST /v1/blocks {userId: B} (blocks Lambda, 6.4).
                First seed a friendship directly so the block succeeds.
                After block, A POST /v1/friend-requests {toUserId: B} → 409 BLOCKED.
              - Mint C and D. C POST /v1/friend-requests to D (saves request_id).
                D POST /v1/blocks {userId: C} → this DELETEs the pending request.
                D POST /v1/friend-requests/{stale_id}/accept → 404 not_found.

These tests require:
  - A deployed dev HTTP API + CloudFront stack (phase 5)
  - The friends Lambda deployed and wired (story 6.2)
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
    pytest infrastructure/src/tests/integration/test_friends.py -v -m integration

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


def _friendship_exists(aurora_conn, id_a: str, id_b: str) -> bool:
    """Return True if a friendships row exists for the canonical pair."""
    user_a, user_b = _canonical_pair(id_a, id_b)
    with aurora_conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM friendships WHERE user_a = %s::uuid AND user_b = %s::uuid LIMIT 1",
            (user_a, user_b),
        )
        return cur.fetchone() is not None


def _friend_request_exists(aurora_conn, id_a: str, id_b: str) -> bool:
    """Return True if any pending friend_request row exists between the pair."""
    with aurora_conn.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM friend_requests
            WHERE (from_user_id = %s::uuid AND to_user_id = %s::uuid)
               OR (from_user_id = %s::uuid AND to_user_id = %s::uuid)
            LIMIT 1
            """,
            (id_a, id_b, id_b, id_a),
        )
        return cur.fetchone() is not None


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
# AC-HAPPY: Happy-path friend-request / accept / list / delete flow
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("completed_profile_user", ["Male"], indirect=True)
def test_given_two_users_when_a_sends_request_and_b_accepts_then_friendship_established(
    completed_profile_user,
):
    """
    Happy-path (AC-HAPPY):

      1. A=Male (from fixture), B=Female (minted inline).
      2. A POST /v1/friend-requests {toUserId: B} → assert 201/200.
      3. B POST /v1/friend-requests/{id}/accept → assert 200.
      4. GET /v1/friends from A → B appears in list.
      5. GET /v1/friends from B → A appears in list.
      6. A DELETE /v1/friends/{B} → assert 200.
      7. GET /v1/friends from A → empty list.
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

    b: dict | None = None
    request_id: str | None = None

    try:
        # ------------------------------------------------------------------
        # Mint B=Female
        # ------------------------------------------------------------------
        b = _mint_user(
            cognito_client, integration_client_id, user_pool_id,
            distribution_domain, edge_secret, "Female", http_requests,
        )
        b_id = b["sub"]

        # ------------------------------------------------------------------
        # Step 2: A sends friend-request to B
        # ------------------------------------------------------------------
        post_resp = http_requests.post(
            _api_url(distribution_domain, "/v1/friend-requests"),
            json={"toUserId": b_id},
            headers=_authed_headers(user_a["access_token"], edge_secret),
        )
        assert post_resp.status_code in (200, 201), (
            f"POST /v1/friend-requests returned HTTP {post_resp.status_code}: {post_resp.text!r}"
        )
        resp_body = post_resp.json()
        request_id = resp_body.get("request_id")
        assert request_id, f"Expected 'request_id' in response body, got {resp_body!r}"

        # ------------------------------------------------------------------
        # Step 3: B accepts the request
        # ------------------------------------------------------------------
        accept_resp = http_requests.post(
            _api_url(distribution_domain, f"/v1/friend-requests/{request_id}/accept"),
            headers=_authed_headers(b["access_token"], edge_secret),
        )
        assert accept_resp.status_code == 200, (
            f"POST .../accept returned HTTP {accept_resp.status_code}: {accept_resp.text!r}"
        )

        # ------------------------------------------------------------------
        # Step 4: GET /v1/friends from A → B must appear
        # ------------------------------------------------------------------
        get_a_resp = http_requests.get(
            _api_url(distribution_domain, "/v1/friends"),
            headers=_authed_headers(user_a["access_token"], edge_secret),
        )
        assert get_a_resp.status_code == 200, (
            f"GET /v1/friends (A) returned HTTP {get_a_resp.status_code}"
        )
        a_friends = get_a_resp.json().get("friends", [])
        a_friend_ids = [f.get("user_id") for f in a_friends]
        assert b_id in a_friend_ids, (
            f"B ({b_id!r}) must appear in A's friend list, got {a_friend_ids!r}"
        )

        # ------------------------------------------------------------------
        # Step 5: GET /v1/friends from B → A must appear
        # ------------------------------------------------------------------
        get_b_resp = http_requests.get(
            _api_url(distribution_domain, "/v1/friends"),
            headers=_authed_headers(b["access_token"], edge_secret),
        )
        assert get_b_resp.status_code == 200, (
            f"GET /v1/friends (B) returned HTTP {get_b_resp.status_code}"
        )
        b_friends = get_b_resp.json().get("friends", [])
        b_friend_ids = [f.get("user_id") for f in b_friends]
        assert a_id in b_friend_ids, (
            f"A ({a_id!r}) must appear in B's friend list, got {b_friend_ids!r}"
        )

        # ------------------------------------------------------------------
        # Step 6: A deletes the friendship
        # ------------------------------------------------------------------
        delete_resp = http_requests.delete(
            _api_url(distribution_domain, f"/v1/friends/{b_id}"),
            headers=_authed_headers(user_a["access_token"], edge_secret),
        )
        assert delete_resp.status_code == 200, (
            f"DELETE /v1/friends/{{b_id}} returned HTTP {delete_resp.status_code}"
        )

        # ------------------------------------------------------------------
        # Step 7: GET /v1/friends from A → empty list
        # ------------------------------------------------------------------
        get_a_after = http_requests.get(
            _api_url(distribution_domain, "/v1/friends"),
            headers=_authed_headers(user_a["access_token"], edge_secret),
        )
        assert get_a_after.status_code == 200
        after_friends = get_a_after.json().get("friends", [])
        after_friend_ids = [f.get("user_id") for f in after_friends]
        assert b_id not in after_friend_ids, (
            f"B ({b_id!r}) must no longer appear in A's friend list after DELETE"
        )

    finally:
        import sys
        # ------------------------------------------------------------------
        # Teardown — clean up all rows that may have been inserted.
        # Errors are printed but not re-raised.
        # ------------------------------------------------------------------
        if b is not None:
            b_id_td = b["sub"]

            # Clean up friendship row (may have been deleted by the test, but guard here)
            try:
                user_a_col, user_b_col = _canonical_pair(a_id, b_id_td)
                with aurora_conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM friendships WHERE user_a = %s::uuid AND user_b = %s::uuid",
                        (user_a_col, user_b_col),
                    )
            except Exception as exc:
                print(f"WARN: teardown friendship cleanup failed: {exc}", file=sys.stderr)

            # Clean up friend_request rows
            try:
                with aurora_conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM friend_requests WHERE "
                        "(from_user_id = %s::uuid AND to_user_id = %s::uuid) "
                        "OR (from_user_id = %s::uuid AND to_user_id = %s::uuid)",
                        (a_id, b_id_td, b_id_td, a_id),
                    )
            except Exception as exc:
                print(f"WARN: teardown friend_requests cleanup failed: {exc}", file=sys.stderr)

            _teardown_user(cognito_client, user_pool_id, b["email"], b_id_td, aurora_conn)


# ---------------------------------------------------------------------------
# AC-BLOCK: Block-aware friend-request behavior
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("completed_profile_user", ["Male"], indirect=True)
def test_given_block_when_post_friend_request_returns_409_and_stale_accept_returns_404(
    completed_profile_user,
):
    """
    Block-aware integration test (AC-BLOCK):

    Scenario 1 (A blocks B — then A can't send request to B):
      1. Mint A=Male (fixture), B=Female.
      2. Seed a friendship directly via master Aurora INSERT (so the blocks Lambda
         will accept the POST /v1/blocks — it requires an existing friendship).
      3. A POST /v1/blocks {userId: B} → assert HTTP 200.
      4. A POST /v1/friend-requests {toUserId: B} → assert HTTP 409 {"error":"blocked"}.

    Scenario 2 (D blocks C after C's request — stale accept returns 404):
      5. Mint C=Male, D=Female.
      6. C POST /v1/friend-requests {toUserId: D} → save request_id.
      7. Seed a friendship directly so D can block C.
      8. D POST /v1/blocks {userId: C} → the blocks Lambda DELETEs the pending request.
      9. D POST /v1/friend-requests/{stale_id}/accept → assert HTTP 404 {"error":"not_found"}.

    Teardown must clean up all INSERTed data and unblock any pairs that were blocked.
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
    c: dict | None = None
    d: dict | None = None
    a_blocked_b = False
    c_request_id: str | None = None

    try:
        # ----------------------------------------------------------------
        # Scenario 1: A blocks B, then A cannot send a friend-request to B
        # ----------------------------------------------------------------

        # Mint B=Female
        b = _mint_user(
            cognito_client, integration_client_id, user_pool_id,
            distribution_domain, edge_secret, "Female", http_requests,
        )
        b_id = b["sub"]

        # Seed friendship A↔B directly so blocks Lambda won't reject with 409 not_friends
        user_a_col, user_b_col = _canonical_pair(a_id, b_id)
        with aurora_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO friendships (user_a, user_b, created_at) VALUES (%s::uuid, %s::uuid, NOW())",
                (user_a_col, user_b_col),
            )

        # A blocks B
        block_resp = http_requests.post(
            _api_url(distribution_domain, "/v1/blocks"),
            json={"userId": b_id},
            headers=_authed_headers(user_a["access_token"], edge_secret),
        )
        assert block_resp.status_code == 200, (
            f"POST /v1/blocks (A→B) returned HTTP {block_resp.status_code}: {block_resp.text!r}"
        )
        a_blocked_b = True

        # A tries to send a friend-request to B → must be blocked
        fr_resp = http_requests.post(
            _api_url(distribution_domain, "/v1/friend-requests"),
            json={"toUserId": b_id},
            headers=_authed_headers(user_a["access_token"], edge_secret),
        )
        assert fr_resp.status_code == 409, (
            f"Expected HTTP 409 (blocked), got {fr_resp.status_code}: {fr_resp.text!r}"
        )
        assert fr_resp.json().get("error") == "blocked", (
            f"Expected error='blocked', got {fr_resp.json()!r}"
        )

        # ----------------------------------------------------------------
        # Scenario 2: C sends request to D; D blocks C → stale accept → 404
        # ----------------------------------------------------------------

        # Mint C=Male and D=Female
        c = _mint_user(
            cognito_client, integration_client_id, user_pool_id,
            distribution_domain, edge_secret, "Male", http_requests,
        )
        d = _mint_user(
            cognito_client, integration_client_id, user_pool_id,
            distribution_domain, edge_secret, "Female", http_requests,
        )
        c_id = c["sub"]
        d_id = d["sub"]

        # C sends friend-request to D
        fr_c_resp = http_requests.post(
            _api_url(distribution_domain, "/v1/friend-requests"),
            json={"toUserId": d_id},
            headers=_authed_headers(c["access_token"], edge_secret),
        )
        assert fr_c_resp.status_code in (200, 201), (
            f"C's POST /v1/friend-requests returned HTTP {fr_c_resp.status_code}: "
            f"{fr_c_resp.text!r}"
        )
        c_request_id = fr_c_resp.json().get("request_id")
        assert c_request_id, f"Expected 'request_id' in response, got {fr_c_resp.json()!r}"

        # Seed friendship C↔D so D can block C
        c_user_a, c_user_b = _canonical_pair(c_id, d_id)
        with aurora_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO friendships (user_a, user_b, created_at) VALUES (%s::uuid, %s::uuid, NOW())",
                (c_user_a, c_user_b),
            )

        # D blocks C — this also DELETEs the pending friend_request
        block_d_resp = http_requests.post(
            _api_url(distribution_domain, "/v1/blocks"),
            json={"userId": c_id},
            headers=_authed_headers(d["access_token"], edge_secret),
        )
        assert block_d_resp.status_code == 200, (
            f"D's POST /v1/blocks returned HTTP {block_d_resp.status_code}: "
            f"{block_d_resp.text!r}"
        )

        # D tries to accept C's now-stale request → 404 not_found
        accept_stale_resp = http_requests.post(
            _api_url(distribution_domain, f"/v1/friend-requests/{c_request_id}/accept"),
            headers=_authed_headers(d["access_token"], edge_secret),
        )
        assert accept_stale_resp.status_code == 404, (
            f"Expected HTTP 404 (not_found) for stale accept, got "
            f"{accept_stale_resp.status_code}: {accept_stale_resp.text!r}"
        )
        assert accept_stale_resp.json().get("error") == "not_found", (
            f"Expected error='not_found', got {accept_stale_resp.json()!r}"
        )

    finally:
        # ----------------------------------------------------------------
        # Teardown — clean up all rows seeded or mutated in this test.
        # ----------------------------------------------------------------
        # Unblock A→B if we blocked them (so A can be reused in other tests)
        if a_blocked_b and b is not None:
            b_id_td = b["sub"]
            try:
                http_requests.delete(
                    _api_url(distribution_domain, f"/v1/blocks/{b_id_td}"),
                    headers=_authed_headers(user_a["access_token"], edge_secret),
                )
            except Exception as exc:
                print(f"WARN: teardown unblock A→B failed: {exc}", file=sys.stderr)

            # Also clean Aurora directly in case the unblock request failed
            try:
                with aurora_conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM blocks WHERE blocker_id = %s::uuid AND blocked_id = %s::uuid",
                        (a_id, b_id_td),
                    )
            except Exception as exc:
                print(f"WARN: teardown Aurora blocks cleanup (A→B) failed: {exc}", file=sys.stderr)

        # Clean up friend_requests C→D (may already be gone due to block)
        if c is not None and d is not None:
            c_id_td = c["sub"]
            d_id_td = d["sub"]
            try:
                with aurora_conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM friend_requests WHERE "
                        "(from_user_id = %s::uuid AND to_user_id = %s::uuid) "
                        "OR (from_user_id = %s::uuid AND to_user_id = %s::uuid)",
                        (c_id_td, d_id_td, d_id_td, c_id_td),
                    )
            except Exception as exc:
                print(f"WARN: teardown C↔D friend_requests cleanup failed: {exc}", file=sys.stderr)

            # Unblock D→C
            try:
                http_requests.delete(
                    _api_url(distribution_domain, f"/v1/blocks/{c_id_td}"),
                    headers=_authed_headers(d["access_token"], edge_secret),
                )
            except Exception as exc:
                print(f"WARN: teardown unblock D→C failed: {exc}", file=sys.stderr)

            try:
                with aurora_conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM blocks WHERE blocker_id = %s::uuid AND blocked_id = %s::uuid",
                        (d_id_td, c_id_td),
                    )
            except Exception as exc:
                print(f"WARN: teardown Aurora blocks cleanup (D→C) failed: {exc}", file=sys.stderr)

        # Teardown user B
        if b is not None:
            _teardown_user(cognito_client, user_pool_id, b["email"], b["sub"], aurora_conn)

        # Teardown users C and D
        if c is not None:
            _teardown_user(cognito_client, user_pool_id, c["email"], c["sub"], aurora_conn)
        if d is not None:
            _teardown_user(cognito_client, user_pool_id, d["email"], d["sub"], aurora_conn)


# ---------------------------------------------------------------------------
# FR-1: direction query parameter on GET /v1/friend-requests
# ---------------------------------------------------------------------------

_KNOTIFY_RUN_INTEGRATION = os.environ.get("KNOTIFY_RUN_INTEGRATION", "")


def _check_integration_gate() -> None:
    """Skip unless KNOTIFY_RUN_INTEGRATION=1 is set."""
    if _KNOTIFY_RUN_INTEGRATION != "1":
        pytest.skip(
            "FR-1 direction tests require KNOTIFY_RUN_INTEGRATION=1 "
            "and all required env vars set against a live dev environment."
        )


def _teardown_pair(aurora_conn, a_id: str, b_id: str) -> None:
    """
    Idempotent teardown: delete all friend_requests and friendships rows
    for the given pair. Errors are suppressed.
    """
    import sys

    try:
        with aurora_conn.cursor() as cur:
            cur.execute(
                "DELETE FROM friend_requests WHERE "
                "(from_user_id = %s::uuid AND to_user_id = %s::uuid) "
                "OR (from_user_id = %s::uuid AND to_user_id = %s::uuid)",
                (a_id, b_id, b_id, a_id),
            )
    except Exception as exc:
        print(f"WARN: _teardown_pair friend_requests failed: {exc}", file=sys.stderr)

    try:
        user_a, user_b = _canonical_pair(a_id, b_id)
        with aurora_conn.cursor() as cur:
            cur.execute(
                "DELETE FROM friendships WHERE user_a = %s::uuid AND user_b = %s::uuid",
                (user_a, user_b),
            )
    except Exception as exc:
        print(f"WARN: _teardown_pair friendships failed: {exc}", file=sys.stderr)


@pytest.mark.parametrize("completed_profile_user", ["Male"], indirect=True)
def test_given_a_sent_request_to_b_when_b_queries_direction_incoming_then_sees_only_incoming(
    completed_profile_user,
):
    """
    FR-1 AC: direction='incoming' returns only requests where to_user_id == caller.

    Setup: A (Male) sends a friend request to B (Female).
    B calls GET /v1/friend-requests?direction=incoming.
    Assert: response contains the A→B row; none of the rows have
    from_user_id == B (i.e. no outgoing rows bleed into the result).
    """
    _check_integration_gate()

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

    b: dict | None = None

    try:
        b = _mint_user(
            cognito_client, integration_client_id, user_pool_id,
            distribution_domain, edge_secret, "Female", http_requests,
        )
        b_id = b["sub"]

        # A sends a friend request to B
        post_resp = http_requests.post(
            _api_url(distribution_domain, "/v1/friend-requests"),
            json={"toUserId": b_id},
            headers=_authed_headers(user_a["access_token"], edge_secret),
        )
        assert post_resp.status_code in (200, 201), (
            f"POST /v1/friend-requests returned HTTP {post_resp.status_code}: {post_resp.text!r}"
        )

        # B queries direction=incoming — expects exactly the A→B row and no others
        get_resp = http_requests.get(
            _api_url(distribution_domain, "/v1/friend-requests"),
            params={"direction": "incoming"},
            headers=_authed_headers(b["access_token"], edge_secret),
        )
        assert get_resp.status_code == 200, (
            f"GET /v1/friend-requests?direction=incoming returned HTTP "
            f"{get_resp.status_code}: {get_resp.text!r}"
        )
        body = get_resp.json()
        reqs = body.get("friend_requests", [])

        # At least one request must be present (A→B)
        to_ids = [r.get("to_user_id") for r in reqs]
        assert b_id in to_ids, (
            f"Expected B's incoming request list to contain A's request (to_user_id={b_id!r}), "
            f"got to_user_ids={to_ids!r}"
        )

        # No outgoing rows must appear — none of the rows should have from_user_id == B
        from_ids = [r.get("from_user_id") for r in reqs]
        assert b_id not in from_ids, (
            f"direction=incoming must not return outgoing rows; "
            f"B's own outgoing requests leaked: from_ids={from_ids!r}"
        )

    finally:
        if b is not None:
            _teardown_pair(aurora_conn, a_id, b["sub"])
            _teardown_user(cognito_client, user_pool_id, b["email"], b["sub"], aurora_conn)


@pytest.mark.parametrize("completed_profile_user", ["Male"], indirect=True)
def test_given_a_sent_request_to_b_when_a_queries_direction_outgoing_then_sees_only_outgoing(
    completed_profile_user,
):
    """
    FR-1 AC: direction='outgoing' returns only requests where from_user_id == caller.

    Setup: A (Male) sends a friend request to B (Female).
    A calls GET /v1/friend-requests?direction=outgoing.
    Assert: response contains A's outgoing row; to_user_id of every row != A
    (i.e. no incoming rows bleed into the result).
    """
    _check_integration_gate()

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

    b: dict | None = None

    try:
        b = _mint_user(
            cognito_client, integration_client_id, user_pool_id,
            distribution_domain, edge_secret, "Female", http_requests,
        )
        b_id = b["sub"]

        # A sends a friend request to B
        post_resp = http_requests.post(
            _api_url(distribution_domain, "/v1/friend-requests"),
            json={"toUserId": b_id},
            headers=_authed_headers(user_a["access_token"], edge_secret),
        )
        assert post_resp.status_code in (200, 201), (
            f"POST /v1/friend-requests returned HTTP {post_resp.status_code}: {post_resp.text!r}"
        )

        # A queries direction=outgoing — expects the A→B row and no incoming rows
        get_resp = http_requests.get(
            _api_url(distribution_domain, "/v1/friend-requests"),
            params={"direction": "outgoing"},
            headers=_authed_headers(user_a["access_token"], edge_secret),
        )
        assert get_resp.status_code == 200, (
            f"GET /v1/friend-requests?direction=outgoing returned HTTP "
            f"{get_resp.status_code}: {get_resp.text!r}"
        )
        body = get_resp.json()
        reqs = body.get("friend_requests", [])

        # At least one request must be present (A→B)
        from_ids = [r.get("from_user_id") for r in reqs]
        assert a_id in from_ids, (
            f"Expected A's outgoing list to contain A's own request (from_user_id={a_id!r}), "
            f"got from_ids={from_ids!r}"
        )

        # No incoming rows must appear — none of the rows should have to_user_id == A
        to_ids = [r.get("to_user_id") for r in reqs]
        assert a_id not in to_ids, (
            f"direction=outgoing must not return incoming rows; "
            f"A appeared as a recipient: to_ids={to_ids!r}"
        )

    finally:
        if b is not None:
            _teardown_pair(aurora_conn, a_id, b["sub"])
            _teardown_user(cognito_client, user_pool_id, b["email"], b["sub"], aurora_conn)


@pytest.mark.parametrize("completed_profile_user", ["Male"], indirect=True)
def test_given_requests_in_both_directions_when_no_direction_param_then_both_returned(
    completed_profile_user,
):
    """
    FR-1 AC: missing direction param preserves current behaviour (both directions).

    Setup: A (Male) sends a request to B (Female).  B sends a separate request
    to a third user C (Male) so A's view has no incoming rows — only outgoing.
    A calls GET /v1/friend-requests with NO direction param.
    Assert: A sees his outgoing request (to B). Then B calls without direction
    and sees both her incoming request (from A) and her outgoing request (to C).
    This proves both directions are returned when direction is omitted.
    """
    _check_integration_gate()

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

    b: dict | None = None
    c: dict | None = None

    try:
        b = _mint_user(
            cognito_client, integration_client_id, user_pool_id,
            distribution_domain, edge_secret, "Female", http_requests,
        )
        b_id = b["sub"]

        c = _mint_user(
            cognito_client, integration_client_id, user_pool_id,
            distribution_domain, edge_secret, "Male", http_requests,
        )
        c_id = c["sub"]

        # A → B: outgoing from A, incoming to B
        post_ab = http_requests.post(
            _api_url(distribution_domain, "/v1/friend-requests"),
            json={"toUserId": b_id},
            headers=_authed_headers(user_a["access_token"], edge_secret),
        )
        assert post_ab.status_code in (200, 201), (
            f"A→B POST returned HTTP {post_ab.status_code}: {post_ab.text!r}"
        )

        # B → C: outgoing from B, incoming to C
        post_bc = http_requests.post(
            _api_url(distribution_domain, "/v1/friend-requests"),
            json={"toUserId": c_id},
            headers=_authed_headers(b["access_token"], edge_secret),
        )
        assert post_bc.status_code in (200, 201), (
            f"B→C POST returned HTTP {post_bc.status_code}: {post_bc.text!r}"
        )

        # B calls GET /v1/friend-requests with NO direction param
        # Must see BOTH the incoming A→B request AND the outgoing B→C request
        get_resp = http_requests.get(
            _api_url(distribution_domain, "/v1/friend-requests"),
            headers=_authed_headers(b["access_token"], edge_secret),
        )
        assert get_resp.status_code == 200, (
            f"GET /v1/friend-requests (no direction) returned HTTP "
            f"{get_resp.status_code}: {get_resp.text!r}"
        )
        body = get_resp.json()
        reqs = body.get("friend_requests", [])

        from_ids = [r.get("from_user_id") for r in reqs]
        to_ids = [r.get("to_user_id") for r in reqs]

        # Incoming: A→B — b is the recipient
        assert b_id in to_ids, (
            f"No-direction call must include incoming A→B row (to_user_id={b_id!r}); "
            f"got to_ids={to_ids!r}"
        )
        # Outgoing: B→C — b is the sender
        assert b_id in from_ids, (
            f"No-direction call must include outgoing B→C row (from_user_id={b_id!r}); "
            f"got from_ids={from_ids!r}"
        )

    finally:
        if b is not None and c is not None:
            _teardown_pair(aurora_conn, b_id, c["sub"])
        if b is not None:
            _teardown_pair(aurora_conn, a_id, b["sub"])
            _teardown_user(cognito_client, user_pool_id, b["email"], b["sub"], aurora_conn)
        if c is not None:
            _teardown_user(cognito_client, user_pool_id, c["email"], c["sub"], aurora_conn)


@pytest.mark.parametrize("completed_profile_user", ["Male"], indirect=True)
def test_given_invalid_direction_param_when_get_friend_requests_then_returns_400(
    completed_profile_user,
):
    """
    FR-1 AC: direction='foo' (or any value other than 'incoming'/'outgoing'/missing)
    returns 400 {"error": "invalid_direction"}.

    The endpoint must not silently return the wrong data for an unrecognised
    direction value.
    """
    _check_integration_gate()

    try:
        import boto3
        import requests as http_requests
    except ImportError:
        pytest.skip("boto3 or requests not installed — live AWS environment required")

    distribution_domain = _require_env("DISTRIBUTION_DOMAIN_NAME")
    edge_secret = _require_env("EDGE_SECRET")
    region = _require_env("AWS_REGION")

    user_a = completed_profile_user
    aurora_conn = user_a["_aurora_conn"]  # noqa: F841 — kept for consistency

    # Attempt GET with an invalid direction value
    get_resp = http_requests.get(
        _api_url(distribution_domain, "/v1/friend-requests"),
        params={"direction": "foo"},
        headers=_authed_headers(user_a["access_token"], edge_secret),
    )
    assert get_resp.status_code == 400, (
        f"Expected HTTP 400 for direction='foo', got {get_resp.status_code}: "
        f"{get_resp.text!r}"
    )
    body = get_resp.json()
    assert body.get("error") == "invalid_direction", (
        f"Expected error='invalid_direction', got {body!r}"
    )


# ---------------------------------------------------------------------------
# FR-2: reverse-pending check on POST /v1/friend-requests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("completed_profile_user", ["Female"], indirect=True)
def test_given_a_sent_request_to_b_when_b_sends_to_a_then_409_reverse_pending(
    completed_profile_user,
):
    """
    FR-2 AC: when A has a pending request to B and B tries to send a request
    to A, the POST must return 409 {"error": "reverse_pending",
    "request_id": "<id of the A->B row>"}.

    The frontend uses the returned request_id to offer "they also want to be
    friends — accept their request" UX.

    Setup:
      - user_a = Male (minted inline), user_b = Female (from fixture).
      - A sends a friend request to B → 201, capture request_id.
      - B sends a friend request to A → must return 409 reverse_pending with
        request_id == the A->B row's id.
    """
    _check_integration_gate()

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

    # user_b = Female (from fixture)
    user_b = completed_profile_user
    b_id = user_b["sub"]
    aurora_conn = user_b["_aurora_conn"]

    a: dict | None = None

    try:
        # Mint A=Male inline
        a = _mint_user(
            cognito_client, integration_client_id, user_pool_id,
            distribution_domain, edge_secret, "Male", http_requests,
        )
        a_id = a["sub"]

        # A sends a friend request to B → expect 201
        post_ab = http_requests.post(
            _api_url(distribution_domain, "/v1/friend-requests"),
            json={"toUserId": b_id},
            headers=_authed_headers(a["access_token"], edge_secret),
        )
        assert post_ab.status_code in (200, 201), (
            f"A->B POST /v1/friend-requests returned HTTP {post_ab.status_code}: "
            f"{post_ab.text!r}"
        )
        ab_request_id = post_ab.json().get("request_id")
        assert ab_request_id, (
            f"Expected 'request_id' in A->B response, got {post_ab.json()!r}"
        )

        # B sends a friend request to A → must return 409 reverse_pending
        post_ba = http_requests.post(
            _api_url(distribution_domain, "/v1/friend-requests"),
            json={"toUserId": a_id},
            headers=_authed_headers(user_b["access_token"], edge_secret),
        )
        assert post_ba.status_code == 409, (
            f"Expected HTTP 409 (reverse_pending) when B sends request to A, "
            f"got {post_ba.status_code}: {post_ba.text!r}"
        )
        body = post_ba.json()
        assert body.get("error") == "reverse_pending", (
            f"Expected error='reverse_pending', got {body!r}"
        )
        assert body.get("request_id") == ab_request_id, (
            f"Expected request_id={ab_request_id!r} (the A->B row) in 409 body, "
            f"got request_id={body.get('request_id')!r}"
        )

    finally:
        if a is not None:
            _teardown_pair(aurora_conn, a["sub"], b_id)
            _teardown_user(cognito_client, user_pool_id, a["email"], a["sub"], aurora_conn)


@pytest.mark.parametrize("completed_profile_user", ["Male"], indirect=True)
def test_given_no_reverse_pending_when_post_friend_request_then_201_regression(
    completed_profile_user,
):
    """
    FR-2 regression: when there is no reverse-pending row, the POST must
    succeed with 201 (the reverse-pending check must not block fresh requests).

    This is a focused regression guard — the full happy path is covered by
    test_given_two_users_when_a_sends_request_and_b_accepts_then_friendship_established.
    """
    _check_integration_gate()

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

    b: dict | None = None

    try:
        b = _mint_user(
            cognito_client, integration_client_id, user_pool_id,
            distribution_domain, edge_secret, "Female", http_requests,
        )
        b_id = b["sub"]

        # A sends a friend request to B — no reverse-pending row exists
        post_resp = http_requests.post(
            _api_url(distribution_domain, "/v1/friend-requests"),
            json={"toUserId": b_id},
            headers=_authed_headers(user_a["access_token"], edge_secret),
        )
        assert post_resp.status_code in (200, 201), (
            f"POST /v1/friend-requests (no reverse pending) returned HTTP "
            f"{post_resp.status_code}: {post_resp.text!r}"
        )
        resp_body = post_resp.json()
        assert resp_body.get("request_id"), (
            f"Expected 'request_id' in 201 response, got {resp_body!r}"
        )

    finally:
        if b is not None:
            _teardown_pair(aurora_conn, a_id, b["sub"])
            _teardown_user(cognito_client, user_pool_id, b["email"], b["sub"], aurora_conn)


# ---------------------------------------------------------------------------
# FR-3: already-friends check on POST /v1/friend-requests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("completed_profile_user", ["Male"], indirect=True)
def test_given_already_friends_when_a_sends_request_to_b_then_409_already_friends(
    completed_profile_user,
):
    """
    FR-3 AC: when A and B are already in the friendships table and A tries to
    send a friend request to B, POST must return 409 {"error": "already_friends"}.

    Setup:
      - user_a = Male (from fixture), user_b = Female (minted inline).
      - A sends a request to B → 201, B accepts → 200 (friendship row exists).
      - A sends ANOTHER request to B → must return 409 already_friends.
    """
    _check_integration_gate()

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

    b: dict | None = None

    try:
        b = _mint_user(
            cognito_client, integration_client_id, user_pool_id,
            distribution_domain, edge_secret, "Female", http_requests,
        )
        b_id = b["sub"]

        # A sends a friend request to B → expect 201
        post_ab = http_requests.post(
            _api_url(distribution_domain, "/v1/friend-requests"),
            json={"toUserId": b_id},
            headers=_authed_headers(user_a["access_token"], edge_secret),
        )
        assert post_ab.status_code in (200, 201), (
            f"A->B POST /v1/friend-requests returned HTTP {post_ab.status_code}: "
            f"{post_ab.text!r}"
        )
        ab_request_id = post_ab.json().get("request_id")
        assert ab_request_id, (
            f"Expected 'request_id' in A->B response, got {post_ab.json()!r}"
        )

        # B accepts the request → friendship row now exists
        accept_resp = http_requests.post(
            _api_url(distribution_domain, f"/v1/friend-requests/{ab_request_id}/accept"),
            headers=_authed_headers(b["access_token"], edge_secret),
        )
        assert accept_resp.status_code == 200, (
            f"POST .../accept returned HTTP {accept_resp.status_code}: {accept_resp.text!r}"
        )
        assert _friendship_exists(aurora_conn, a_id, b_id), (
            "friendships row must exist after accept before testing already_friends path"
        )

        # A sends ANOTHER request to B → must return 409 already_friends
        post_ab2 = http_requests.post(
            _api_url(distribution_domain, "/v1/friend-requests"),
            json={"toUserId": b_id},
            headers=_authed_headers(user_a["access_token"], edge_secret),
        )
        assert post_ab2.status_code == 409, (
            f"Expected HTTP 409 (already_friends) when A re-sends request to B after "
            f"accept, got {post_ab2.status_code}: {post_ab2.text!r}"
        )
        body = post_ab2.json()
        assert body.get("error") == "already_friends", (
            f"Expected error='already_friends', got {body!r}"
        )

    finally:
        if b is not None:
            _teardown_pair(aurora_conn, a_id, b["sub"])
            _teardown_user(cognito_client, user_pool_id, b["email"], b["sub"], aurora_conn)


@pytest.mark.parametrize("completed_profile_user", ["Male"], indirect=True)
def test_given_not_yet_friends_when_a_sends_request_to_b_then_201_regression(
    completed_profile_user,
):
    """
    FR-3 regression: when no friendship row exists between A and B, the
    already-friends check must NOT block the request — POST must succeed
    with 201.

    This guards that the new already_friends check does not regress the
    normal happy-path where the two users have never been friends.
    """
    _check_integration_gate()

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

    b: dict | None = None

    try:
        b = _mint_user(
            cognito_client, integration_client_id, user_pool_id,
            distribution_domain, edge_secret, "Female", http_requests,
        )
        b_id = b["sub"]

        # Confirm no friendship row exists before sending (regression guard)
        assert not _friendship_exists(aurora_conn, a_id, b_id), (
            "Precondition failed: friendships row must NOT exist before this test"
        )

        # A sends a friend request to B — no friendship row exists; must succeed
        post_resp = http_requests.post(
            _api_url(distribution_domain, "/v1/friend-requests"),
            json={"toUserId": b_id},
            headers=_authed_headers(user_a["access_token"], edge_secret),
        )
        assert post_resp.status_code in (200, 201), (
            f"POST /v1/friend-requests (not-yet-friends) returned HTTP "
            f"{post_resp.status_code}: {post_resp.text!r}"
        )
        resp_body = post_resp.json()
        assert resp_body.get("request_id"), (
            f"Expected 'request_id' in 201 response, got {resp_body!r}"
        )

    finally:
        if b is not None:
            _teardown_pair(aurora_conn, a_id, b["sub"])
            _teardown_user(cognito_client, user_pool_id, b["email"], b["sub"], aurora_conn)


# ---------------------------------------------------------------------------
# FR-5: Notifications row written on POST /v1/friend-requests
# ---------------------------------------------------------------------------


def _teardown_notifications_row(dynamo_client, table_name: str, user_id: str) -> None:
    """
    Delete all Notifications rows for user_id by scanning the SK prefix.

    Uses a Query on the PK (user_id) then deletes each row individually.
    Errors are suppressed — this is a best-effort teardown.
    """
    import sys

    try:
        resp = dynamo_client.query(
            TableName=table_name,
            KeyConditionExpression="user_id = :uid",
            ExpressionAttributeValues={":uid": {"S": user_id}},
        )
        for item in resp.get("Items", []):
            sk = item.get("created_at_notification_id", {}).get("S", "")
            if not sk:
                continue
            dynamo_client.delete_item(
                TableName=table_name,
                Key={
                    "user_id": {"S": user_id},
                    "created_at_notification_id": {"S": sk},
                },
            )
    except Exception as exc:
        print(
            f"WARN: _teardown_notifications_row(user_id={user_id!r}) failed: {exc}",
            file=sys.stderr,
        )


@pytest.mark.parametrize("completed_profile_user", ["Male"], indirect=True)
def test_given_a_sends_request_to_b_when_post_succeeds_then_notifications_row_written(
    completed_profile_user,
):
    """
    FR-5 AC: after a successful POST /v1/friend-requests, a Notifications row
    must be written in DynamoDB with the correct schema fields.

    Setup: A (Male, from fixture) sends a friend request to B (Female, minted).
    Assert:
      - POST returns 201.
      - DynamoDB Query on Notifications table (PK user_id = B's sub) returns
        at least one row whose 'type' == 'friend_request_received'.
      - That row has sender_user_id == A's sub.
      - That row has sender_name == A's first_name ("Test").
      - That row's payload JSON contains 'request_id' matching the POST response.
      - That row has read == False and delivered == False.

    Does NOT assert on live AppSync subscription (FR-7 scope).

    Gated on KNOTIFY_RUN_INTEGRATION=1.
    """
    _check_integration_gate()

    notifications_table = os.environ.get("DYNAMODB_TABLE_NOTIFICATIONS", "")
    if not notifications_table:
        pytest.skip(
            "DYNAMODB_TABLE_NOTIFICATIONS env var not set — "
            "set it to the Notifications DynamoDB table name and re-run."
        )

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
    dynamo_client = boto3.client("dynamodb", region_name=region)

    user_a = completed_profile_user
    a_id = user_a["sub"]
    # A's first name is "Test" per build_profile_completion_payload in conftest.py.
    a_first_name = "Test"
    aurora_conn = user_a["_aurora_conn"]

    b: dict | None = None

    try:
        b = _mint_user(
            cognito_client, integration_client_id, user_pool_id,
            distribution_domain, edge_secret, "Female", http_requests,
        )
        b_id = b["sub"]

        # A sends friend request to B — POST must succeed with 201.
        post_resp = http_requests.post(
            _api_url(distribution_domain, "/v1/friend-requests"),
            json={"toUserId": b_id},
            headers=_authed_headers(user_a["access_token"], edge_secret),
        )
        assert post_resp.status_code in (200, 201), (
            f"POST /v1/friend-requests returned HTTP {post_resp.status_code}: "
            f"{post_resp.text!r}"
        )
        resp_body = post_resp.json()
        request_id = resp_body.get("request_id")
        assert request_id, f"Expected 'request_id' in response body, got {resp_body!r}"

        # Query Notifications table for recipient B — there should be a row with
        # type='friend_request_received'.  The Lambda writes the row synchronously
        # before returning 201 so no sleep is needed; a DynamoDB PutItem completes
        # well within the HTTP round-trip.
        query_resp = dynamo_client.query(
            TableName=notifications_table,
            KeyConditionExpression="user_id = :uid",
            ExpressionAttributeValues={":uid": {"S": b_id}},
        )
        items = query_resp.get("Items", [])

        # Find the row with type='friend_request_received'
        received_rows = [
            item for item in items
            if item.get("type", {}).get("S") == "friend_request_received"
        ]
        assert received_rows, (
            f"Expected a Notifications row with type='friend_request_received' "
            f"for recipient {b_id!r}, but got items={items!r}"
        )

        row = received_rows[0]

        # sender_user_id must be A's sub.
        actual_sender = row.get("sender_user_id", {}).get("S")
        assert actual_sender == a_id, (
            f"Expected sender_user_id={a_id!r}, got {actual_sender!r}"
        )

        # sender_name must be A's first name.
        actual_sender_name = row.get("sender_name", {}).get("S")
        assert actual_sender_name == a_first_name, (
            f"Expected sender_name={a_first_name!r}, got {actual_sender_name!r}"
        )

        # payload must be a JSON string containing 'request_id'.
        payload_str = row.get("payload", {}).get("S", "")
        try:
            payload_obj = json.loads(payload_str)
        except (json.JSONDecodeError, TypeError) as exc:
            pytest.fail(
                f"Notifications row payload is not valid JSON: {payload_str!r} ({exc})"
            )
        assert payload_obj.get("request_id") == request_id, (
            f"Expected payload.request_id={request_id!r}, "
            f"got payload={payload_obj!r}"
        )

        # read and delivered must both be False.
        actual_read = row.get("read", {}).get("BOOL")
        actual_delivered = row.get("delivered", {}).get("BOOL")
        assert actual_read is False, (
            f"Expected read=False, got {actual_read!r}"
        )
        assert actual_delivered is False, (
            f"Expected delivered=False, got {actual_delivered!r}"
        )

    finally:
        if b is not None:
            b_id_td = b["sub"]
            # Teardown: remove Notifications rows for B (recipient).
            _teardown_notifications_row(dynamo_client, notifications_table, b_id_td)
            _teardown_pair(aurora_conn, a_id, b_id_td)
            _teardown_user(cognito_client, user_pool_id, b["email"], b_id_td, aurora_conn)


@pytest.mark.parametrize("completed_profile_user", ["Male"], indirect=True)
def test_given_existing_pending_request_when_same_direction_duplicate_then_409_already_pending(
    completed_profile_user,
):
    """
    FR-2 regression: same-direction duplicate (A sends to B twice) must still
    return 409 {"error": "already_pending"} via the UniqueViolation path.

    This test verifies that the new reverse-pending check does not interfere
    with the existing UniqueViolation handling.
    """
    _check_integration_gate()

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

    b: dict | None = None

    try:
        b = _mint_user(
            cognito_client, integration_client_id, user_pool_id,
            distribution_domain, edge_secret, "Female", http_requests,
        )
        b_id = b["sub"]

        # First POST — must succeed
        first_resp = http_requests.post(
            _api_url(distribution_domain, "/v1/friend-requests"),
            json={"toUserId": b_id},
            headers=_authed_headers(user_a["access_token"], edge_secret),
        )
        assert first_resp.status_code in (200, 201), (
            f"First POST /v1/friend-requests returned HTTP {first_resp.status_code}: "
            f"{first_resp.text!r}"
        )

        # Second POST (same direction) — must return 409 already_pending
        second_resp = http_requests.post(
            _api_url(distribution_domain, "/v1/friend-requests"),
            json={"toUserId": b_id},
            headers=_authed_headers(user_a["access_token"], edge_secret),
        )
        assert second_resp.status_code == 409, (
            f"Expected HTTP 409 (already_pending) on second same-direction POST, "
            f"got {second_resp.status_code}: {second_resp.text!r}"
        )
        body = second_resp.json()
        assert body.get("error") == "already_pending", (
            f"Expected error='already_pending', got {body!r}"
        )

    finally:
        if b is not None:
            _teardown_pair(aurora_conn, a_id, b["sub"])
            _teardown_user(cognito_client, user_pool_id, b["email"], b["sub"], aurora_conn)


# ---------------------------------------------------------------------------
# FR-6: Notifications row written on POST /v1/friend-requests/{id}/accept
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("completed_profile_user", ["Female"], indirect=True)
def test_given_a_sent_request_to_b_when_b_accepts_then_notifications_row_written_for_a(
    completed_profile_user,
):
    """
    FR-6 AC: after B accepts A's friend request, a Notifications row must be
    written in DynamoDB addressed to A (the original sender) with the correct
    schema fields.

    Setup:
      - A (Male, minted inline) sends a friend request to B (Female, from fixture).
      - B accepts the request via POST /v1/friend-requests/{id}/accept.
    Assert:
      - Accept returns 200.
      - DynamoDB Query on Notifications table (PK user_id = A's sub) returns
        at least one row whose 'type' == 'friend_request_accepted'.
      - That row has sender_user_id == B's sub (the acceptor).
      - That row has sender_name == B's first_name ("Test").
      - That row's payload JSON contains 'request_id' matching the original POST
        response and 'from_user_id' == B's sub.
      - That row has read == False and delivered == False.

    Teardown removes Notifications rows for BOTH A (FR-6 row, recipient of
    acceptance) AND B (FR-5 row, recipient of the original request), because the
    POST /v1/friend-requests step writes B's row before the accept step runs.

    Does NOT assert on live AppSync subscription (FR-7 scope).

    Gated on KNOTIFY_RUN_INTEGRATION=1 and DYNAMODB_TABLE_NOTIFICATIONS.
    """
    _check_integration_gate()

    notifications_table = os.environ.get("DYNAMODB_TABLE_NOTIFICATIONS", "")
    if not notifications_table:
        pytest.skip(
            "DYNAMODB_TABLE_NOTIFICATIONS env var not set — "
            "set it to the Notifications DynamoDB table name and re-run."
        )

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
    dynamo_client = boto3.client("dynamodb", region_name=region)

    # B is the fixture user (Female acceptor); A is minted inline (Male sender).
    user_b = completed_profile_user
    b_id = user_b["sub"]
    # Both _mint_user and completed_profile_user set first_name="Test".
    b_first_name = "Test"
    aurora_conn = user_b["_aurora_conn"]

    a: dict | None = None

    try:
        a = _mint_user(
            cognito_client, integration_client_id, user_pool_id,
            distribution_domain, edge_secret, "Male", http_requests,
        )
        a_id = a["sub"]

        # A sends friend request to B — POST must succeed with 201.
        post_resp = http_requests.post(
            _api_url(distribution_domain, "/v1/friend-requests"),
            json={"toUserId": b_id},
            headers=_authed_headers(a["access_token"], edge_secret),
        )
        assert post_resp.status_code in (200, 201), (
            f"POST /v1/friend-requests returned HTTP {post_resp.status_code}: "
            f"{post_resp.text!r}"
        )
        resp_body = post_resp.json()
        request_id = resp_body.get("request_id")
        assert request_id, f"Expected 'request_id' in POST response body, got {resp_body!r}"

        # B accepts the request — POST /v1/friend-requests/{id}/accept must return 200.
        accept_resp = http_requests.post(
            _api_url(distribution_domain, f"/v1/friend-requests/{request_id}/accept"),
            headers=_authed_headers(user_b["access_token"], edge_secret),
        )
        assert accept_resp.status_code == 200, (
            f"POST /v1/friend-requests/{request_id}/accept returned "
            f"HTTP {accept_resp.status_code}: {accept_resp.text!r}"
        )

        # Query Notifications table for A (the ORIGINAL SENDER) — there should be
        # a row with type='friend_request_accepted'.  The Lambda writes the row
        # synchronously before returning 200 so no sleep is needed.
        query_resp = dynamo_client.query(
            TableName=notifications_table,
            KeyConditionExpression="user_id = :uid",
            ExpressionAttributeValues={":uid": {"S": a_id}},
        )
        items = query_resp.get("Items", [])

        # Find the row with type='friend_request_accepted'
        accepted_rows = [
            item for item in items
            if item.get("type", {}).get("S") == "friend_request_accepted"
        ]
        assert accepted_rows, (
            f"Expected a Notifications row with type='friend_request_accepted' "
            f"for original sender {a_id!r}, but got items={items!r}"
        )

        row = accepted_rows[0]

        # sender_user_id must be B's sub (the acceptor).
        actual_sender = row.get("sender_user_id", {}).get("S")
        assert actual_sender == b_id, (
            f"Expected sender_user_id={b_id!r} (acceptor), got {actual_sender!r}"
        )

        # sender_name must be B's first name.
        actual_sender_name = row.get("sender_name", {}).get("S")
        assert actual_sender_name == b_first_name, (
            f"Expected sender_name={b_first_name!r}, got {actual_sender_name!r}"
        )

        # payload must be a JSON string containing 'request_id' and
        # 'from_user_id' == B's sub (the acceptor).
        payload_str = row.get("payload", {}).get("S", "")
        try:
            payload_obj = json.loads(payload_str)
        except (json.JSONDecodeError, TypeError) as exc:
            pytest.fail(
                f"Notifications row payload is not valid JSON: {payload_str!r} ({exc})"
            )
        assert payload_obj.get("request_id") == request_id, (
            f"Expected payload.request_id={request_id!r}, got payload={payload_obj!r}"
        )
        assert payload_obj.get("from_user_id") == b_id, (
            f"Expected payload.from_user_id={b_id!r} (acceptor), "
            f"got payload={payload_obj!r}"
        )

        # read and delivered must both be False.
        actual_read = row.get("read", {}).get("BOOL")
        actual_delivered = row.get("delivered", {}).get("BOOL")
        assert actual_read is False, (
            f"Expected read=False, got {actual_read!r}"
        )
        assert actual_delivered is False, (
            f"Expected delivered=False, got {actual_delivered!r}"
        )

    finally:
        if a is not None:
            a_id_td = a["sub"]
            # Teardown Notifications rows for BOTH users:
            #   - A's row (type='friend_request_accepted', written by FR-6 accept path)
            #   - B's row (type='friend_request_received', written by FR-5 POST path)
            _teardown_notifications_row(dynamo_client, notifications_table, a_id_td)
            _teardown_notifications_row(dynamo_client, notifications_table, b_id)
            _teardown_pair(aurora_conn, a_id_td, b_id)
            _teardown_user(cognito_client, user_pool_id, a["email"], a_id_td, aurora_conn)
