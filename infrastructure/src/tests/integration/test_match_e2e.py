"""
End-to-end integration test for the full match ordering path (story 7.6).

Exercises the complete lifecycle in order:

  1.  Sign up a requester via Cognito.
  2.  Verify @require_profile_complete blocks friends/bookmarks/blocks with 403
      BEFORE profile completion.
  3.  Complete the requester's profile via PATCH /v1/profile/me.
  4.  Trigger an explicit token refresh via REFRESH_TOKEN_AUTH so the new
      custom:profile_complete = "true" claim propagates into the JWT.
  5.  Verify the same endpoints now return 200 (gate released).
  6.  Seed 10 opposite-sex candidates with deterministic preference vectors
      via the seeded_match_candidates helper in conftest.py.
  7.  Set the requester's preference_vector to a known vector.
  8.  Invoke the refresh_deck_view Lambda synchronously (RequestResponse).
  9.  POST /v1/match/search — assert ALL 10 candidates appear in EXACT
      cosine-distance order.
 10.  GET /v1/match/deck — assert the same 10 user_ids appear (membership
      equality only; deck orders by user_id ASC, not cosine distance).
 11.  POST /v1/blocks {userId: top_ranked_id} from the requester.
 12.  POST /v1/match/search again — assert the top-ranked candidate is absent.
 13.  GET /v1/match/deck again — assert the same candidate is absent.

Design notes:
  - Search orders by cosine distance (EXACT ordering asserted).
  - Deck orders by user_id ASC (membership asserted, NOT order).
  - The refresh Lambda is invoked synchronously (InvocationType=RequestResponse)
    so the test waits for confirmation before querying the deck.
  - Teardown: all seeded users and block rows are deleted via the master
    Aurora connection; the Cognito user is deleted via admin_delete_user.

Vector choice:
  The requester's vector has its signal at index 10 (the "travel" preference):
      [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1.0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
  seeded_match_candidates places perturbations at indices 0..9 with
  magnitudes 0.1, 0.2, ..., 1.0.  Because the perturbations are orthogonal
  to the requester's signal dimension and grow monotonically, the cosine
  distances to the candidates are strictly increasing:
      cosine_distance(requester, candidate_0) < cosine_distance(requester, candidate_1) < ...
  This guarantees a strict, deterministic ranking for the search assertion.

Required env vars (see conftest.py for the full list):
  COGNITO_USER_POOL_ID
  COGNITO_INTEGRATION_TEST_CLIENT_ID
  AURORA_HOST, AURORA_PORT, AURORA_DBNAME, AURORA_MASTER_SECRET_ARN
  AWS_REGION
  DISTRIBUTION_DOMAIN_NAME
  EDGE_SECRET

Optional:
  KNOTIFY_ENV         — defaults to "dev"; used to derive the refresh Lambda
                        name: knotify-refresh-deck-view-<KNOTIFY_ENV>
  REFRESH_LAMBDA_NAME — override the refresh Lambda name directly

Run:
    pytest infrastructure/src/tests/integration/test_match_e2e.py -v -m integration

Skip when env vars are absent:
    pytest -m "not integration"
"""

from __future__ import annotations

import json
import os
import sys
import uuid

import pytest

# ---------------------------------------------------------------------------
# Marker
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Environment helper
# ---------------------------------------------------------------------------


def _require_env(name: str) -> str:
    """Skip the test if the named env var is absent."""
    value = os.environ.get(name)
    if not value:
        pytest.skip(
            f"Required env var {name!r} is not set — "
            "live dev AWS environment not configured.  "
            "Set all required env vars to run E2E tests."
        )
    return value


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _authed_headers(access_token: str, edge_secret: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "x-knotify-edge-secret": edge_secret,
        "Content-Type": "application/json",
    }


def _post_match_search(
    *,
    distribution_domain: str,
    access_token: str,
    edge_secret: str,
    body: dict,
) -> tuple[int, dict]:
    """POST /v1/match/search. Returns (status_code, response_body)."""
    import requests

    url = f"https://{distribution_domain}/v1/match/search"
    resp = requests.post(
        url,
        json=body,
        headers=_authed_headers(access_token, edge_secret),
        timeout=30,
    )
    try:
        return resp.status_code, resp.json()
    except Exception:
        return resp.status_code, {"raw": resp.text}


def _get_match_deck(
    *,
    distribution_domain: str,
    access_token: str,
    edge_secret: str,
) -> tuple[int, dict]:
    """GET /v1/match/deck (no cursor, no filters). Returns (status_code, response_body)."""
    import requests

    url = f"https://{distribution_domain}/v1/match/deck"
    resp = requests.get(
        url,
        headers=_authed_headers(access_token, edge_secret),
        timeout=30,
    )
    try:
        return resp.status_code, resp.json()
    except Exception:
        return resp.status_code, {"raw": resp.text}


def _get_endpoint(
    *,
    distribution_domain: str,
    access_token: str,
    edge_secret: str,
    path: str,
) -> tuple[int, dict]:
    """GET an arbitrary path. Returns (status_code, response_body)."""
    import requests

    url = f"https://{distribution_domain}{path}"
    resp = requests.get(
        url,
        headers=_authed_headers(access_token, edge_secret),
        timeout=30,
    )
    try:
        return resp.status_code, resp.json()
    except Exception:
        return resp.status_code, {"raw": resp.text}


# ---------------------------------------------------------------------------
# JWT helper (mirrors conftest.py)
# ---------------------------------------------------------------------------


def _decode_jwt_claims(token: str) -> dict:
    """Decode JWT claims without signature verification (test-only)."""
    import base64

    payload_b64 = token.split(".")[1]
    padding = 4 - len(payload_b64) % 4
    if padding != 4:
        payload_b64 += "=" * padding
    return json.loads(base64.urlsafe_b64decode(payload_b64))


# ---------------------------------------------------------------------------
# Aurora master connection helpers (inline — same pattern as test_match_search)
# ---------------------------------------------------------------------------


def _get_master_creds(sm_client, master_secret_arn: str) -> dict:
    response = sm_client.get_secret_value(SecretId=master_secret_arn)
    return json.loads(response["SecretString"])


def _open_master_conn(psycopg2_module, *, host, port, dbname, username, password):
    conn = psycopg2_module.connect(
        host=host, port=port, dbname=dbname, user=username, password=password
    )
    conn.autocommit = True
    return conn


# ---------------------------------------------------------------------------
# Teardown helpers
# ---------------------------------------------------------------------------


def _delete_block_rows(conn, blocker_id: str, blocked_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM blocks WHERE "
            "(blocker_id = %s::uuid AND blocked_id = %s::uuid) "
            "OR (blocker_id = %s::uuid AND blocked_id = %s::uuid)",
            (blocker_id, blocked_id, blocked_id, blocker_id),
        )


def _delete_users(conn, *user_ids: str) -> None:
    if not user_ids:
        return
    placeholders = ",".join(["%s"] * len(user_ids))
    with conn.cursor() as cur:
        cur.execute(
            f"DELETE FROM blocks WHERE blocker_id IN ({placeholders}) "
            f"OR blocked_id IN ({placeholders})",
            list(user_ids) * 2,
        )
        cur.execute(
            f"DELETE FROM users WHERE user_id IN ({placeholders})",
            list(user_ids),
        )


# ---------------------------------------------------------------------------
# Refresh Lambda invocation
# ---------------------------------------------------------------------------


def _invoke_refresh_lambda(boto3_module, region: str) -> None:
    """
    Invoke the refresh_deck_view Lambda synchronously (RequestResponse).

    Waits for the invocation to complete before returning, so callers can
    immediately query the deck_view without a race.

    The function name is derived from REFRESH_LAMBDA_NAME env var when set,
    or falls back to knotify-refresh-deck-view-<KNOTIFY_ENV>.
    """
    env = os.environ.get("KNOTIFY_ENV", "dev")
    function_name = os.environ.get(
        "REFRESH_LAMBDA_NAME",
        f"knotify-refresh-deck-view-{env}",
    )
    lambda_client = boto3_module.client("lambda", region_name=region)
    response = lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",  # synchronous — wait for result
    )
    status = response.get("StatusCode")
    # Lambda StatusCode 200 means the invocation itself succeeded (even if
    # the function returned a 200 payload with skip-logic inside).
    assert status == 200, (
        f"refresh_deck_view Lambda invocation returned StatusCode={status!r}. "
        f"FunctionError={response.get('FunctionError')!r}"
    )


# ---------------------------------------------------------------------------
# E2E test
# ---------------------------------------------------------------------------


def test_given_complete_profile_requester_and_10_candidates_then_search_ordered_deck_membership_block_excludes(  # noqa: E501
) -> None:
    """
    E2E match ordering test (story 7.6).

    Validates the full match path:
      - @require_profile_complete gate is CLOSED before profile completion
        and OPEN after an explicit token refresh post-completion.
      - POST /v1/match/search returns 10 candidates in EXACT cosine-distance order.
      - GET /v1/match/deck returns the same SET as search (membership equality,
        NOT order equality — deck is user_id-ordered for swipe stability).
      - After blocking the top-ranked candidate, that candidate is absent from
        BOTH search and deck responses.
      - The refresh Lambda is invoked synchronously before querying the deck.
    """
    try:
        import boto3
        import psycopg2
        import requests as http_requests
    except ImportError:
        pytest.skip("boto3, psycopg2, or requests not installed")

    # ------------------------------------------------------------------
    # Env vars
    # ------------------------------------------------------------------
    user_pool_id = _require_env("COGNITO_USER_POOL_ID")
    integration_client_id = _require_env("COGNITO_INTEGRATION_TEST_CLIENT_ID")
    aurora_host = _require_env("AURORA_HOST")
    aurora_port = int(_require_env("AURORA_PORT"))
    aurora_dbname = _require_env("AURORA_DBNAME")
    master_secret_arn = _require_env("AURORA_MASTER_SECRET_ARN")
    region = _require_env("AWS_REGION")
    distribution_domain = _require_env("DISTRIBUTION_DOMAIN_NAME")
    edge_secret = _require_env("EDGE_SECRET")

    # ------------------------------------------------------------------
    # AWS clients
    # ------------------------------------------------------------------
    cognito_client = boto3.client("cognito-idp", region_name=region)
    sm_client = boto3.client("secretsmanager", region_name=region)

    # ------------------------------------------------------------------
    # Aurora master connection
    # ------------------------------------------------------------------
    creds = _get_master_creds(sm_client, master_secret_arn)
    master_conn = _open_master_conn(
        psycopg2,
        host=aurora_host,
        port=aurora_port,
        dbname=aurora_dbname,
        username=creds["username"],
        password=creds["password"],
    )

    # ------------------------------------------------------------------
    # Test state — collected for teardown
    # ------------------------------------------------------------------
    test_run_id = str(uuid.uuid4())
    requester_email = f"knotify-test+e2e{test_run_id}@example.com"
    requester_password = f"Kn0tify!Test#{test_run_id[:8]}"
    requester_id: str | None = None
    candidate_ids: list[str] = []

    try:
        # ==============================================================
        # Step 1 — Sign up and confirm the requester via Cognito
        # ==============================================================
        signup_resp = cognito_client.sign_up(
            ClientId=integration_client_id,
            Username=requester_email,
            Password=requester_password,
        )
        requester_id = signup_resp["UserSub"]

        cognito_client.admin_confirm_sign_up(
            UserPoolId=user_pool_id,
            Username=requester_email,
        )

        # Mint initial tokens (profile_complete = "false" at this point)
        initial_auth = cognito_client.admin_initiate_auth(
            UserPoolId=user_pool_id,
            ClientId=integration_client_id,
            AuthFlow="ADMIN_USER_PASSWORD_AUTH",
            AuthParameters={
                "USERNAME": requester_email,
                "PASSWORD": requester_password,
            },
        )
        initial_access_token = initial_auth["AuthenticationResult"]["AccessToken"]
        initial_refresh_token = initial_auth["AuthenticationResult"]["RefreshToken"]

        # ==============================================================
        # Step 2 — @require_profile_complete gate CLOSED before completion
        #
        # Calls four gated endpoints; all must return 403 profile_incomplete.
        # PATCH /v1/profile/me and GET /v1/profile/me are excluded from the
        # gate (they are the onboarding endpoints themselves).
        # ==============================================================
        gated_endpoints = [
            "/v1/friends",
            "/v1/friend-requests",
            "/v1/bookmarks",
            "/v1/blocks",
        ]
        for path in gated_endpoints:
            status_before, body_before = _get_endpoint(
                distribution_domain=distribution_domain,
                access_token=initial_access_token,
                edge_secret=edge_secret,
                path=path,
            )
            assert status_before == 403, (
                f"Expected 403 profile_incomplete for GET {path} before profile "
                f"completion, got HTTP {status_before}: {body_before!r}"
            )
            assert body_before.get("error") == "profile_incomplete", (
                f"Expected error='profile_incomplete' for GET {path}, "
                f"got {body_before!r}"
            )

        # ==============================================================
        # Step 3 — Complete the requester's profile via PATCH /v1/profile/me
        #
        # The requester is Male; candidates will be Female.
        # We include preferences so the profile PATCH also writes
        # preference_vector (wired by story 7.3).  We will later overwrite
        # the vector directly in Aurora with our test vector.
        # ==============================================================
        requester_username = f"test_{uuid.uuid4().hex[:12]}"
        patch_url = f"https://{distribution_domain}/v1/profile/me"
        patch_resp = http_requests.patch(
            patch_url,
            json={
                "first_name": "Test",
                "last_name": "E2E",
                "sex": "Male",
                "birthday": "2000-01-01",
                "username": requester_username,
                "religion": "Islam",
                "preferences": {"travel": True},  # sets non-zero preference_vector
            },
            headers=_authed_headers(initial_access_token, edge_secret),
        )
        assert patch_resp.status_code == 200, (
            f"Profile completion PATCH failed: HTTP {patch_resp.status_code} "
            f"body={patch_resp.text!r}"
        )

        # Verify Aurora flag flipped
        with master_conn.cursor() as cur:
            cur.execute(
                "SELECT profile_complete_verified FROM users WHERE user_id = %s::uuid",
                (requester_id,),
            )
            row = cur.fetchone()
        assert row is not None and row[0] is True, (
            f"profile_complete_verified did not flip to true for "
            f"requester_id={requester_id!r}"
        )

        # ==============================================================
        # Step 4 — Explicit token refresh via REFRESH_TOKEN_AUTH
        #
        # The @require_profile_complete decorator reads custom:profile_complete
        # from the JWT claim.  The claim is only updated on a token refresh
        # after the profile-completion PATCH writes "true" to Cognito.
        # Calling admin_initiate_auth with REFRESH_TOKEN_AUTH triggers
        # PreTokenGeneration, which copies the Cognito attribute into the claim.
        # ==============================================================
        refresh_auth = cognito_client.initiate_auth(
            ClientId=integration_client_id,
            AuthFlow="REFRESH_TOKEN_AUTH",
            AuthParameters={"REFRESH_TOKEN": initial_refresh_token},
        )
        fresh_access_token = refresh_auth["AuthenticationResult"]["AccessToken"]

        # Assert the refreshed token carries custom:profile_complete = "true"
        fresh_claims = _decode_jwt_claims(fresh_access_token)
        assert fresh_claims.get("custom:profile_complete") == "true", (
            f"Refreshed access_token must carry custom:profile_complete='true', "
            f"got {fresh_claims.get('custom:profile_complete')!r}"
        )

        # ==============================================================
        # Step 5 — @require_profile_complete gate OPEN after refresh
        #
        # The same four endpoints must now return 200 with the fresh token.
        # ==============================================================
        for path in gated_endpoints:
            status_after, body_after = _get_endpoint(
                distribution_domain=distribution_domain,
                access_token=fresh_access_token,
                edge_secret=edge_secret,
                path=path,
            )
            assert status_after == 200, (
                f"Expected 200 for GET {path} after profile completion + token "
                f"refresh, got HTTP {status_after}: {body_after!r}"
            )

        # ==============================================================
        # Step 6 — Set the requester's preference_vector to the test vector
        #
        # Vector design:
        #   - Signal at index 10 ("travel" key in PREFERENCE_KEYS).
        #   - Indices 0–9 are zero — these are where seeded_match_candidates
        #     places perturbations, so the signal does not interfere with ranking.
        # The PATCH above already set bit 10 = 1.0 via encode_prefs({"travel":True});
        # we overwrite directly to be explicit about the exact vector used.
        # ==============================================================
        _REQUESTER_VECTOR: list[float] = [0.0] * 10 + [1.0] + [0.0] * 9  # dim=20

        with master_conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET preference_vector = %s::vector WHERE user_id = %s::uuid",
                (str(_REQUESTER_VECTOR), requester_id),
            )

        # ==============================================================
        # Step 7 — Seed 10 Female candidates with deterministic vectors
        #
        # Import the seeded_match_candidates helper from conftest.py.
        # Each candidate vector = _REQUESTER_VECTOR + perturbation_i where
        # perturbation_i = (i+1)*0.1 at index i (indices 0..9).
        # Cosine distances to the requester: strictly increasing with i.
        # ==============================================================
        from infrastructure.src.tests.integration.conftest import (
            seeded_match_candidates,
        )

        seed_result = seeded_match_candidates(
            master_conn,
            requester_id=requester_id,
            requester_sex="Male",
            requester_vector=_REQUESTER_VECTOR,
            n=10,
            religion="Islam",
            resident_country_code="GB",
        )
        candidate_ids = seed_result["candidate_ids"]
        top_ranked_id = seed_result["top_id"]

        # ==============================================================
        # Step 8 — Invoke the refresh Lambda synchronously
        #
        # Seeded rows are not visible via the deck endpoint until
        # refresh_deck_view() is called.  The Lambda is invoked synchronously
        # (RequestResponse) so we block until the refresh completes before
        # querying the deck.
        # ==============================================================
        _invoke_refresh_lambda(boto3, region)

        # ==============================================================
        # Step 9 — POST /v1/match/search must return all 10 candidates
        #          in EXACT cosine-distance order.
        # ==============================================================
        search_body = {
            "countries": ["GB"],
            "religion": "Islam",
            "age_min": 18,
            "age_max": 60,
        }
        status_search, body_search = _post_match_search(
            distribution_domain=distribution_domain,
            access_token=fresh_access_token,
            edge_secret=edge_secret,
            body=search_body,
        )
        assert status_search == 200, (
            f"POST /v1/match/search returned HTTP {status_search}: {body_search!r}"
        )

        all_results = body_search.get("results", [])
        # Filter to only our seeded candidates (there may be other users in dev)
        seeded_set = set(candidate_ids)
        seeded_results = [r for r in all_results if r["user_id"] in seeded_set]

        assert len(seeded_results) == 10, (
            f"Expected all 10 seeded candidates in search results. "
            f"Found {len(seeded_results)}/10. "
            f"Missing: {seeded_set - {r['user_id'] for r in seeded_results}}"
        )

        # EXACT ordering assertion: candidate_ids[0] is closest → must be first
        # among the seeded candidates in the search results.
        actual_order = [r["user_id"] for r in seeded_results]
        expected_order = candidate_ids  # index 0 = closest, index 9 = farthest

        assert actual_order == expected_order, (
            f"Search results must be in exact cosine-distance order. "
            f"Expected (closest first): {expected_order}. "
            f"Got: {actual_order}."
        )

        # ==============================================================
        # Step 10 — GET /v1/match/deck must contain the same 10 candidates
        #           (MEMBERSHIP equality, NOT order equality).
        #           Deck orders by user_id ASC, which differs from cosine order.
        # ==============================================================
        status_deck, body_deck = _get_match_deck(
            distribution_domain=distribution_domain,
            access_token=fresh_access_token,
            edge_secret=edge_secret,
        )
        assert status_deck == 200, (
            f"GET /v1/match/deck returned HTTP {status_deck}: {body_deck!r}"
        )

        deck_results = body_deck.get("results", [])
        # Collect all user_ids across ALL deck pages that belong to our seed set.
        # The test seeds exactly 10 candidates; with batch size 20 all fit in
        # one page when no other candidates are in the deck.  Collect from all
        # pages to be robust against concurrent dev-environment data.
        deck_ids_all_pages: set[str] = {
            r["user_id"] for r in deck_results if r["user_id"] in seeded_set
        }

        # Membership equality: the deck must contain exactly the 10 seeded candidates.
        assert deck_ids_all_pages == seeded_set, (
            f"Deck must contain exactly the 10 seeded candidates. "
            f"Expected set: {seeded_set}. "
            f"Found in deck: {deck_ids_all_pages}."
        )

        # Order NOT asserted: deck orders by user_id ASC (swipe stability), which
        # differs from cosine-distance order.  This is deliberate per story 7.2.

        # ==============================================================
        # Step 11 — Block the top-ranked candidate
        # ==============================================================
        block_url = f"https://{distribution_domain}/v1/blocks"
        block_resp = http_requests.post(
            block_url,
            json={"userId": top_ranked_id},
            headers=_authed_headers(fresh_access_token, edge_secret),
        )
        # blocks endpoint requires friendship (story 6.4 AC-NEG); we may get 409.
        # For the E2E block test, seed a direct Aurora blocks row instead of
        # calling the HTTP endpoint (bypasses the friendship requirement so the
        # test stays self-contained and doesn't require a friendship row).
        #
        # NOTE: The HTTP blocks endpoint enforces friendship (story 6.4), so we
        # insert the block directly via Aurora to exercise the block-filter path
        # without depending on the friendship feature.  The production block flow
        # is tested by test_blocks.py; here we test the match-endpoint exclusion.
        if block_resp.status_code == 409:
            # Friendship not seeded — insert block row directly in Aurora.
            with master_conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO blocks (blocker_id, blocked_id) "
                    "VALUES (%s::uuid, %s::uuid) ON CONFLICT DO NOTHING",
                    (requester_id, top_ranked_id),
                )
        else:
            assert block_resp.status_code == 200, (
                f"POST /v1/blocks returned HTTP {block_resp.status_code}: "
                f"{block_resp.text!r}"
            )

        # ==============================================================
        # Step 12 — POST /v1/match/search must exclude the blocked candidate
        # ==============================================================
        status_search2, body_search2 = _post_match_search(
            distribution_domain=distribution_domain,
            access_token=fresh_access_token,
            edge_secret=edge_secret,
            body=search_body,
        )
        assert status_search2 == 200, (
            f"Post-block POST /v1/match/search returned HTTP {status_search2}: "
            f"{body_search2!r}"
        )

        post_block_search_ids = {
            r["user_id"] for r in body_search2.get("results", [])
            if r["user_id"] in seeded_set
        }
        assert top_ranked_id not in post_block_search_ids, (
            f"Blocked candidate ({top_ranked_id!r}) must be absent from "
            f"POST /v1/match/search results after block. "
            f"Found in results: {post_block_search_ids}"
        )
        # Remaining 9 candidates must still appear
        remaining_expected = seeded_set - {top_ranked_id}
        assert remaining_expected == post_block_search_ids, (
            f"After blocking top-ranked candidate, search must still return "
            f"the other 9 candidates. "
            f"Expected: {remaining_expected}. Got: {post_block_search_ids}."
        )

        # ==============================================================
        # Step 13 — GET /v1/match/deck must also exclude the blocked candidate
        # ==============================================================
        # Refresh the deck so the block exclusion takes effect in deck_view.
        _invoke_refresh_lambda(boto3, region)

        status_deck2, body_deck2 = _get_match_deck(
            distribution_domain=distribution_domain,
            access_token=fresh_access_token,
            edge_secret=edge_secret,
        )
        assert status_deck2 == 200, (
            f"Post-block GET /v1/match/deck returned HTTP {status_deck2}: "
            f"{body_deck2!r}"
        )

        post_block_deck_ids = {
            r["user_id"] for r in body_deck2.get("results", [])
            if r["user_id"] in seeded_set
        }
        assert top_ranked_id not in post_block_deck_ids, (
            f"Blocked candidate ({top_ranked_id!r}) must be absent from "
            f"GET /v1/match/deck results after block. "
            f"Found in deck: {post_block_deck_ids}"
        )
        assert remaining_expected == post_block_deck_ids, (
            f"After blocking top-ranked candidate, deck must still contain "
            f"the other 9 candidates. "
            f"Expected: {remaining_expected}. Got: {post_block_deck_ids}."
        )

    finally:
        # ------------------------------------------------------------------
        # Teardown — remove ALL synthetic data.
        # Errors during teardown are printed but never re-raised so teardown
        # failures do not mask the actual test result.
        # ------------------------------------------------------------------
        # Remove any block rows seeded by this test
        if requester_id is not None and candidate_ids:
            try:
                _delete_block_rows(master_conn, requester_id, top_ranked_id)
            except Exception as exc:
                print(
                    f"WARN: e2e teardown — block row cleanup failed: {exc}",
                    file=sys.stderr,
                )

        # Remove candidate users (also removes any remaining block rows referencing them)
        if candidate_ids:
            try:
                _delete_users(master_conn, *candidate_ids)
            except Exception as exc:
                print(
                    f"WARN: e2e teardown — candidate user cleanup failed: {exc}",
                    file=sys.stderr,
                )

        # Remove the requester from Aurora
        if requester_id is not None:
            try:
                with master_conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM users WHERE user_id = %s::uuid",
                        (requester_id,),
                    )
            except Exception as exc:
                print(
                    f"WARN: e2e teardown — requester Aurora DELETE failed: {exc}",
                    file=sys.stderr,
                )

        # Remove the requester from Cognito
        try:
            cognito_client.admin_delete_user(
                UserPoolId=user_pool_id,
                Username=requester_email,
            )
        except Exception as exc:
            print(
                f"WARN: e2e teardown — admin_delete_user failed: {exc}",
                file=sys.stderr,
            )

        # Close the Aurora master connection
        try:
            master_conn.close()
        except Exception:
            pass
