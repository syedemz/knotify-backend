"""
E2E signup integration test for the Knotify Cognito + Aurora stack.

Story 4.6 acceptance criteria — tested here:

  AC1  sign_up against the dev User Pool with a unique per-run email
       (knotify-test+<uuid4>@example.com) using boto3 cognito-idp.
       (Handled by the signed_in_user fixture in conftest.py.)
  AC2  admin_confirm_sign_up fires the post-confirmation trigger, which
       inserts a bootstrap users row in Aurora.
       (Handled by the signed_in_user fixture in conftest.py.)
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
       Runs even if the test body raises — handled unconditionally by the
       signed_in_user fixture in conftest.py.

The sign-up / confirm / sign-in / teardown boilerplate is provided by the
`signed_in_user` fixture in conftest.py (extracted in story 5.7 so it is
reusable by the phase-5 edge smoke test without duplication).

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
"""

from __future__ import annotations

import base64
import json

import pytest

pytestmark = pytest.mark.integration


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
    padding_needed = 4 - len(payload_b64) % 4
    if padding_needed != 4:
        payload_b64 += "=" * padding_needed
    payload_bytes = base64.urlsafe_b64decode(payload_b64)
    return json.loads(payload_bytes)


# ---------------------------------------------------------------------------
# E2E signup test
# ---------------------------------------------------------------------------

def test_given_new_signup_when_confirmed_then_users_row_exists_and_profile_complete_claim_is_false(
    signed_in_user,
):
    """
    E2E signup test for story 4.6.

    Given a fresh Cognito signup with a unique per-run email (provided by the
    signed_in_user fixture), when the user is admin-confirmed (firing the
    post-confirmation trigger), then:
      - a users row exists in Aurora with user_id = Cognito sub,
      - profile columns (first_name, last_name, sex, birthday, username)
        are NULL (bootstrap state — AC3),
      - the id_token and access_token both carry
        custom:profile_complete = "false" (AC4 / brainstorm B1).

    Teardown (AC5) is handled unconditionally by the signed_in_user fixture.
    """
    # The fixture already performed sign-up, confirm, sign-in, and opened an
    # Aurora connection as the master user. We only assert behavior here.

    cognito_sub = signed_in_user["sub"]
    test_email = signed_in_user["email"]
    id_token = signed_in_user["id_token"]
    access_token = signed_in_user["access_token"]
    aurora_conn = signed_in_user["_aurora_conn"]

    # ------------------------------------------------------------------
    # AC3 — query Aurora as master and assert the bootstrap users row
    # ------------------------------------------------------------------
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

    db_user_id, db_email, first_name, last_name, sex, birthday, username = row

    assert db_user_id == cognito_sub, (
        f"users.user_id {db_user_id!r} must match Cognito sub {cognito_sub!r}"
    )
    assert db_email == test_email, (
        f"users.email {db_email!r} must match signup email {test_email!r}"
    )

    # AC3 contract: profile columns are NULL in the bootstrap row.
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
    # AC4 — verify custom:profile_complete = "false" on both tokens.
    # The PreTokenGeneration V2 Lambda (story 4.4) embeds the claim on
    # both id_token and access_token (brainstorm B1 two-token contract).
    # ------------------------------------------------------------------
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
