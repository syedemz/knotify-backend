"""
Integration tests for POST /v1/match/search (story 7.1).

These tests run against the live dev AWS environment.  They are NOT docker-
compose tests.  They require:
  - A deployed dev Aurora cluster with migrations 0001–0013 applied
  - A deployed knotify-match Lambda reachable via the CloudFront distribution
  - All standard integration env vars (see conftest.py)
  - DISTRIBUTION_DOMAIN_NAME and EDGE_SECRET (written by Terraform local_file)

Test coverage (story 7.1 acceptance criteria):
  IT-1  Cosine ranking: seed 5 candidates with known preference vectors;
        the requester whose vector is closest to candidate C gets C in position 1.
  IT-2  Block filtering: seed candidate C; requester blocks C; the search no
        longer returns C.
  IT-3  Empty-vector fallback: a requester whose preference_vector is NULL gets
        results ordered by created_at DESC (deterministic, not random).

Run:
    pytest infrastructure/src/tests/integration/test_match_search.py -v -m integration

Skip if live env is not configured:
    pytest -m "not integration"
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Marker
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers imported from the shared conftest (signed_in_user, etc.)
# ---------------------------------------------------------------------------

# conftest.py in this directory provides signed_in_user, completed_profile_user,
# _require_env, and the Aurora master connection helpers.


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------


def _require_env(name: str) -> str:
    """Skip test if env var is missing."""
    value = os.environ.get(name)
    if not value:
        pytest.skip(
            f"Required env var {name!r} is not set — "
            "live dev AWS environment not configured."
        )
    return value


# ---------------------------------------------------------------------------
# DB seed helpers (direct Aurora master connection, bypasses Lambda + RLS)
# ---------------------------------------------------------------------------


def _seed_user(
    conn,
    *,
    user_id: str,
    email: str,
    sex: str,
    religion: str,
    age: int | None = None,
    resident_country_code: str = "GB",
    preference_vector: list[float] | None = None,
    profile_complete_verified: bool = True,
    created_at: datetime | None = None,
) -> None:
    """
    Insert a synthetic user row directly into Aurora via the master connection.

    preference_vector is bound using the explicit %s::vector cast so psycopg2
    does not need the pgvector type adapter.
    """
    ts = created_at or datetime.now(tz=timezone.utc)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO users (
                user_id, email, sex, religion,
                resident_country_code,
                profile_complete_verified,
                created_at,
                preference_vector
            )
            VALUES (
                %s::uuid, %s, %s, %s,
                %s,
                %s,
                %s,
                %s::vector
            )
            ON CONFLICT (user_id) DO NOTHING
            """,
            (
                user_id,
                email,
                sex,
                religion,
                resident_country_code,
                profile_complete_verified,
                ts,
                str(preference_vector) if preference_vector is not None else None,
            ),
        )


def _teardown_users(conn, *user_ids: str) -> None:
    """Delete synthetic test users and their dependent rows (blocks)."""
    if not user_ids:
        return
    placeholders = ",".join(["%s"] * len(user_ids))
    with conn.cursor() as cur:
        cur.execute(
            f"DELETE FROM blocks WHERE blocker_id IN ({placeholders}) OR blocked_id IN ({placeholders})",
            list(user_ids) * 2,
        )
        cur.execute(
            f"DELETE FROM users WHERE user_id IN ({placeholders})",
            list(user_ids),
        )


def _seed_block(conn, *, blocker_id: str, blocked_id: str) -> None:
    """Insert a blocks row so the search filters out the blocked user."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO blocks (blocker_id, blocked_id) VALUES (%s::uuid, %s::uuid) "
            "ON CONFLICT DO NOTHING",
            (blocker_id, blocked_id),
        )


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------


def _post_match_search(
    *,
    distribution_domain: str,
    access_token: str,
    edge_secret: str,
    body: dict,
) -> tuple[int, dict]:
    """
    POST /v1/match/search.

    Returns (status_code, response_body_dict).
    """
    import requests

    url = f"https://{distribution_domain}/v1/match/search"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "x-knotify-edge-secret": edge_secret,
        "Content-Type": "application/json",
    }
    resp = requests.post(url, json=body, headers=headers, timeout=30)
    try:
        body_json = resp.json()
    except Exception:
        body_json = {"raw": resp.text}
    return resp.status_code, body_json


# ---------------------------------------------------------------------------
# IT-1: Cosine ranking
#
# Seed 5 female candidates with known preference vectors.
# Male requester whose preference_vector is identical to candidate C gets C
# in position 1 (cosine distance = 0 → ranked first).
# ---------------------------------------------------------------------------


def test_given_five_candidates_when_search_with_matching_vector_then_closest_is_first(
    completed_profile_user,
) -> None:
    """
    IT-1: seed 5 female candidates with known preference vectors;
    the requester whose vector is closest to candidate C gets C in position 1.
    """
    try:
        import requests
        import psycopg2
        import boto3
    except ImportError:
        pytest.skip("requests/psycopg2/boto3 not installed")

    distribution_domain = _require_env("DISTRIBUTION_DOMAIN_NAME")
    edge_secret = _require_env("EDGE_SECRET")
    aurora_host = _require_env("AURORA_HOST")
    aurora_port = int(_require_env("AURORA_PORT"))
    aurora_dbname = _require_env("AURORA_DBNAME")
    master_secret_arn = _require_env("AURORA_MASTER_SECRET_ARN")
    region = _require_env("AWS_REGION")

    sm_client = boto3.client("secretsmanager", region_name=region)
    from infrastructure.src.tests.integration.conftest import (
        _get_aurora_master_creds,
        _aurora_master_conn,
    )
    creds = _get_aurora_master_creds(sm_client, master_secret_arn)
    master_conn = _aurora_master_conn(
        psycopg2,
        host=aurora_host,
        port=aurora_port,
        dbname=aurora_dbname,
        username=creds["username"],
        password=creds["password"],
    )

    # Requester is Male (from completed_profile_user). Set their
    # preference_vector to match candidate C exactly.
    # vector C: index 0 = 1.0, rest = 0.0
    vector_c = [1.0] + [0.0] * 19

    # Write the requester's preference_vector directly in Aurora (bypasses
    # Lambda immutable-field guard — we're seeding test state).
    requester_id = completed_profile_user["sub"]
    with master_conn.cursor() as cur:
        cur.execute(
            "UPDATE users SET preference_vector = %s::vector WHERE user_id = %s::uuid",
            (str(vector_c), requester_id),
        )

    # Seed 5 Female candidates with distinct vectors
    cand_ids: list[str] = []
    vectors = [
        [0.0, 1.0] + [0.0] * 18,   # cand A — far from C
        [0.0, 0.0, 1.0] + [0.0] * 17,  # cand B — far
        vector_c,                   # cand C — exact match (distance 0)
        [0.0, 0.0, 0.0, 1.0] + [0.0] * 16,  # cand D
        [0.0, 0.0, 0.0, 0.0, 1.0] + [0.0] * 15,  # cand E
    ]

    try:
        for i, vec in enumerate(vectors):
            cid = str(uuid.uuid4())
            cand_ids.append(cid)
            _seed_user(
                master_conn,
                user_id=cid,
                email=f"it1_cand_{i}_{uuid.uuid4().hex[:6]}@test.invalid",
                sex="Female",  # opposite to Male requester
                religion="Islam",
                resident_country_code="GB",
                preference_vector=vec,
                profile_complete_verified=True,
            )

        cand_c_id = cand_ids[2]  # the exact-match candidate

        status, body = _post_match_search(
            distribution_domain=distribution_domain,
            access_token=completed_profile_user["access_token"],
            edge_secret=edge_secret,
            body={
                "countries": ["GB"],
                "religion": "Islam",
                "age_min": 18,
                "age_max": 60,
            },
        )

        assert status == 200, f"Expected 200, got {status}: {body}"
        results = body.get("results", [])
        assert len(results) >= 1, "Expected at least one result"
        assert results[0]["user_id"] == cand_c_id, (
            f"Expected candidate C ({cand_c_id!r}) in position 1, "
            f"got {results[0].get('user_id')!r}. Full results: {results}"
        )

    finally:
        _teardown_users(master_conn, requester_id, *cand_ids)
        # Reset the requester's preference_vector
        try:
            with master_conn.cursor() as cur:
                cur.execute(
                    "UPDATE users SET preference_vector = NULL WHERE user_id = %s::uuid",
                    (requester_id,),
                )
        except Exception:
            pass
        master_conn.close()


# ---------------------------------------------------------------------------
# IT-2: Block filtering
#
# Seed candidate C; requester blocks C; the search no longer returns C.
# ---------------------------------------------------------------------------


def test_given_requester_blocks_candidate_when_search_then_blocked_candidate_absent(
    completed_profile_user,
) -> None:
    """
    IT-2: seed candidate C; requester blocks C;
    POST /v1/match/search must not return C.
    """
    try:
        import requests
        import psycopg2
        import boto3
    except ImportError:
        pytest.skip("requests/psycopg2/boto3 not installed")

    distribution_domain = _require_env("DISTRIBUTION_DOMAIN_NAME")
    edge_secret = _require_env("EDGE_SECRET")
    aurora_host = _require_env("AURORA_HOST")
    aurora_port = int(_require_env("AURORA_PORT"))
    aurora_dbname = _require_env("AURORA_DBNAME")
    master_secret_arn = _require_env("AURORA_MASTER_SECRET_ARN")
    region = _require_env("AWS_REGION")

    sm_client = boto3.client("secretsmanager", region_name=region)
    from infrastructure.src.tests.integration.conftest import (
        _get_aurora_master_creds,
        _aurora_master_conn,
    )
    creds = _get_aurora_master_creds(sm_client, master_secret_arn)
    master_conn = _aurora_master_conn(
        psycopg2,
        host=aurora_host,
        port=aurora_port,
        dbname=aurora_dbname,
        username=creds["username"],
        password=creds["password"],
    )

    requester_id = completed_profile_user["sub"]
    cand_c_id = str(uuid.uuid4())

    try:
        # Seed candidate C (Female, same religion/country as requester)
        _seed_user(
            master_conn,
            user_id=cand_c_id,
            email=f"it2_cand_c_{uuid.uuid4().hex[:6]}@test.invalid",
            sex="Female",
            religion="Islam",
            resident_country_code="GB",
            profile_complete_verified=True,
        )

        # Verify C appears in search results before blocking
        status, body = _post_match_search(
            distribution_domain=distribution_domain,
            access_token=completed_profile_user["access_token"],
            edge_secret=edge_secret,
            body={
                "countries": ["GB"],
                "religion": "Islam",
                "age_min": 18,
                "age_max": 60,
            },
        )
        assert status == 200, f"Pre-block search failed: {status} {body}"
        pre_block_ids = {r["user_id"] for r in body.get("results", [])}
        assert cand_c_id in pre_block_ids, (
            f"Candidate C ({cand_c_id!r}) should appear before block. "
            f"Got ids: {pre_block_ids}"
        )

        # Requester blocks candidate C
        _seed_block(master_conn, blocker_id=requester_id, blocked_id=cand_c_id)

        # Search again — C must be absent
        status2, body2 = _post_match_search(
            distribution_domain=distribution_domain,
            access_token=completed_profile_user["access_token"],
            edge_secret=edge_secret,
            body={
                "countries": ["GB"],
                "religion": "Islam",
                "age_min": 18,
                "age_max": 60,
            },
        )
        assert status2 == 200, f"Post-block search failed: {status2} {body2}"
        post_block_ids = {r["user_id"] for r in body2.get("results", [])}
        assert cand_c_id not in post_block_ids, (
            f"Blocked candidate C ({cand_c_id!r}) must not appear in search results. "
            f"Got ids: {post_block_ids}"
        )

    finally:
        _teardown_users(master_conn, cand_c_id)
        master_conn.close()


# ---------------------------------------------------------------------------
# IT-3: Empty-vector fallback
#
# A requester whose preference_vector is NULL gets results ordered by
# created_at DESC (deterministic, not random).
# ---------------------------------------------------------------------------


def test_given_requester_null_vector_when_search_then_results_ordered_by_created_at_desc(
    completed_profile_user,
) -> None:
    """
    IT-3 (empty-vector fallback): a requester whose preference_vector is NULL
    gets results ordered by created_at DESC, not random.
    """
    try:
        import requests
        import psycopg2
        import boto3
    except ImportError:
        pytest.skip("requests/psycopg2/boto3 not installed")

    distribution_domain = _require_env("DISTRIBUTION_DOMAIN_NAME")
    edge_secret = _require_env("EDGE_SECRET")
    aurora_host = _require_env("AURORA_HOST")
    aurora_port = int(_require_env("AURORA_PORT"))
    aurora_dbname = _require_env("AURORA_DBNAME")
    master_secret_arn = _require_env("AURORA_MASTER_SECRET_ARN")
    region = _require_env("AWS_REGION")

    sm_client = boto3.client("secretsmanager", region_name=region)
    from infrastructure.src.tests.integration.conftest import (
        _get_aurora_master_creds,
        _aurora_master_conn,
    )
    creds = _get_aurora_master_creds(sm_client, master_secret_arn)
    master_conn = _aurora_master_conn(
        psycopg2,
        host=aurora_host,
        port=aurora_port,
        dbname=aurora_dbname,
        username=creds["username"],
        password=creds["password"],
    )

    requester_id = completed_profile_user["sub"]

    # Ensure the requester's preference_vector is NULL
    with master_conn.cursor() as cur:
        cur.execute(
            "UPDATE users SET preference_vector = NULL WHERE user_id = %s::uuid",
            (requester_id,),
        )

    # Seed 3 Female candidates with deterministic, spread-out created_at values
    now = datetime.now(tz=timezone.utc)
    cand_ids: list[str] = []
    created_ats: list[datetime] = [
        now - timedelta(seconds=300),  # oldest
        now - timedelta(seconds=200),  # middle
        now - timedelta(seconds=100),  # newest
    ]

    try:
        for i, ts in enumerate(created_ats):
            cid = str(uuid.uuid4())
            cand_ids.append(cid)
            _seed_user(
                master_conn,
                user_id=cid,
                email=f"it3_cand_{i}_{uuid.uuid4().hex[:6]}@test.invalid",
                sex="Female",
                religion="Islam",
                resident_country_code="GB",
                profile_complete_verified=True,
                created_at=ts,
            )

        status, body = _post_match_search(
            distribution_domain=distribution_domain,
            access_token=completed_profile_user["access_token"],
            edge_secret=edge_secret,
            body={
                "countries": ["GB"],
                "religion": "Islam",
                "age_min": 18,
                "age_max": 60,
            },
        )

        assert status == 200, f"Expected 200, got {status}: {body}"
        results = body.get("results", [])

        # Extract only the seeded candidates (filter by our known IDs)
        seeded = [r for r in results if r["user_id"] in cand_ids]
        assert len(seeded) == 3, (
            f"Expected all 3 seeded candidates in results. "
            f"Found: {[r['user_id'] for r in seeded]}"
        )

        # They should be ordered newest-first: cand_ids[2], [1], [0]
        expected_order = [cand_ids[2], cand_ids[1], cand_ids[0]]
        actual_order = [r["user_id"] for r in seeded]
        assert actual_order == expected_order, (
            f"Empty-vector fallback must order by created_at DESC. "
            f"Expected {expected_order}, got {actual_order}"
        )

    finally:
        _teardown_users(master_conn, *cand_ids)
        master_conn.close()
