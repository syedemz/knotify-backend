"""
Integration tests for GET /v1/match/deck (story 7.2).

These tests run against the live dev AWS environment.  They are NOT docker-
compose tests.  They require:
  - A deployed dev Aurora cluster with migrations 0001–0013 applied
  - The deck_view refreshed (SELECT refresh_deck_view() called after seeding)
  - A deployed knotify-match Lambda reachable via the CloudFront distribution
  - All standard integration env vars (see conftest.py)
  - DISTRIBUTION_DOMAIN_NAME and EDGE_SECRET (written by Terraform local_file)

Test coverage (story 7.2 acceptance criteria):
  IT-1  verified-only: deck returns only profile_complete_verified=true users
        (seed one verified + one unverified; assert only the verified one appears).
  IT-2  cursor pagination round-trip: first call with no cursor returns 20 rows
        and a next_cursor; second call with that cursor returns the next 20 rows
        starting after the cursor value.
  IT-3  opposite-sex filter: a Male requester's deck contains zero Male
        candidates even when seeded.

Run:
    pytest infrastructure/src/tests/integration/test_match_deck.py -v -m integration

Skip if live env is not configured:
    pytest -m "not integration"

Design note on refresh:
    deck_view is a materialized view — seeded rows are NOT visible via the deck
    endpoint until refresh_deck_view() is executed.  Each test calls
    SELECT refresh_deck_view() via the master connection before issuing HTTP
    requests.  This mirrors the production trigger path (story 7.4 Lambda) and
    is the correct strategy for deck-specific integration tests.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Marker
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.integration


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
# Master connection helper (shared with test_match_search.py pattern)
# ---------------------------------------------------------------------------


def _master_conn(psycopg2_module, host, port, dbname, username, password):
    """Open a psycopg2 connection as the Aurora master user (bypasses RLS)."""
    conn = psycopg2_module.connect(
        host=host,
        port=port,
        dbname=dbname,
        user=username,
        password=password,
    )
    conn.autocommit = True
    return conn


def _get_creds(boto3_module, region, master_secret_arn):
    sm = boto3_module.client("secretsmanager", region_name=region)
    secret = sm.get_secret_value(SecretId=master_secret_arn)
    return json.loads(secret["SecretString"])


# ---------------------------------------------------------------------------
# DB seed / teardown helpers
# ---------------------------------------------------------------------------


def _seed_user(
    conn,
    *,
    user_id: str,
    email: str,
    sex: str,
    religion: str = "Islam",
    resident_country_code: str = "GB",
    profile_complete_verified: bool = True,
) -> None:
    """
    Insert a synthetic user row directly into Aurora.

    Only the columns that deck_view exposes are required for deck tests.
    Columns not referenced by deck_view queries are left at their defaults.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO users (
                user_id, email, sex, religion,
                resident_country_code,
                profile_complete_verified
            )
            VALUES (
                %s::uuid, %s, %s, %s, %s, %s
            )
            ON CONFLICT (user_id) DO NOTHING
            """,
            (
                user_id, email, sex, religion,
                resident_country_code, profile_complete_verified,
            ),
        )


def _refresh_deck_view(conn) -> None:
    """
    Run SELECT refresh_deck_view() via the master connection.

    The materialized view must be refreshed after seeding so the new rows
    become visible to the deck endpoint.  This mirrors story 7.4's production
    trigger path without requiring the refresh Lambda to be invoked.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT refresh_deck_view()")


def _teardown_users(conn, *user_ids: str) -> None:
    """Delete synthetic test users and their dependent rows."""
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


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------


def _get_match_deck(
    *,
    distribution_domain: str,
    access_token: str,
    edge_secret: str,
    cursor: str | None = None,
    filters: dict | None = None,
) -> tuple[int, dict]:
    """
    GET /v1/match/deck with optional ?cursor= and ?filters= query parameters.

    Returns (status_code, response_body_dict).
    """
    import requests
    import urllib.parse

    url = f"https://{distribution_domain}/v1/match/deck"
    params: dict[str, str] = {}
    if cursor:
        params["cursor"] = cursor
    if filters is not None:
        params["filters"] = urllib.parse.quote(json.dumps(filters))

    headers = {
        "Authorization": f"Bearer {access_token}",
        "x-knotify-edge-secret": edge_secret,
    }
    resp = requests.get(url, params=params, headers=headers, timeout=30)
    try:
        body_json = resp.json()
    except Exception:
        body_json = {"raw": resp.text}
    return resp.status_code, body_json


# ---------------------------------------------------------------------------
# IT-1: Verified-only
#
# Seed one profile_complete_verified=true user and one profile_complete_verified=false
# user, refresh deck_view, then assert the deck only contains the verified user.
# ---------------------------------------------------------------------------


def test_given_verified_and_unverified_users_when_deck_requested_then_only_verified_appears(
    completed_profile_user,
) -> None:
    """
    IT-1 (story 7.2 AC): deck_view's defining query already filters on
    profile_complete_verified = true, so unverified users never appear.

    Seed one verified Female + one unverified Female (opposite sex to the
    Male requester from completed_profile_user).  Refresh the deck_view.
    Assert: verified user appears in results; unverified user absent.
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

    creds = _get_creds(boto3, region, master_secret_arn)
    conn = _master_conn(
        psycopg2,
        host=aurora_host,
        port=aurora_port,
        dbname=aurora_dbname,
        username=creds["username"],
        password=creds["password"],
    )

    verified_id = str(uuid.uuid4())
    unverified_id = str(uuid.uuid4())

    try:
        _seed_user(
            conn,
            user_id=verified_id,
            email=f"deck_it1_verified_{uuid.uuid4().hex[:6]}@test.invalid",
            sex="Female",
            profile_complete_verified=True,
        )
        _seed_user(
            conn,
            user_id=unverified_id,
            email=f"deck_it1_unverified_{uuid.uuid4().hex[:6]}@test.invalid",
            sex="Female",
            profile_complete_verified=False,
        )

        # Refresh the materialized view so the new rows become visible
        _refresh_deck_view(conn)

        status, body = _get_match_deck(
            distribution_domain=distribution_domain,
            access_token=completed_profile_user["access_token"],
            edge_secret=edge_secret,
        )

        assert status == 200, f"Expected 200, got {status}: {body}"
        result_ids = {r["user_id"] for r in body.get("results", [])}

        assert verified_id in result_ids, (
            f"Verified user ({verified_id!r}) must appear in deck. "
            f"Result ids: {result_ids}"
        )
        assert unverified_id not in result_ids, (
            f"Unverified user ({unverified_id!r}) must NOT appear in deck. "
            f"Result ids: {result_ids}"
        )

    finally:
        _teardown_users(conn, verified_id, unverified_id)
        # Refresh again so teardown rows are removed from deck_view
        try:
            _refresh_deck_view(conn)
        except Exception:
            pass
        conn.close()


# ---------------------------------------------------------------------------
# IT-2: Cursor pagination round-trip
#
# Seed 41 opposite-sex verified users.  First call: no cursor → 20 rows +
# next_cursor.  Second call: cursor from first page → next 20 rows starting
# strictly after the cursor value.
# ---------------------------------------------------------------------------


def test_given_41_candidates_when_deck_paginated_then_cursor_round_trip_correct(
    completed_profile_user,
) -> None:
    """
    IT-2 (story 7.2 AC): cursor pagination round-trip.

    Seed 41 female candidates (opposite sex to Male requester).  Refresh.
    First call (no cursor): expect 20 rows + non-null next_cursor.
    Second call (cursor from page 1): expect the next batch of rows all with
    user_id > first-page's last user_id, with no overlap.
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

    creds = _get_creds(boto3, region, master_secret_arn)
    conn = _master_conn(
        psycopg2,
        host=aurora_host,
        port=aurora_port,
        dbname=aurora_dbname,
        username=creds["username"],
        password=creds["password"],
    )

    cand_ids: list[str] = []

    try:
        for i in range(41):
            cid = str(uuid.uuid4())
            cand_ids.append(cid)
            _seed_user(
                conn,
                user_id=cid,
                email=f"deck_it2_{i}_{uuid.uuid4().hex[:6]}@test.invalid",
                sex="Female",
                profile_complete_verified=True,
            )

        _refresh_deck_view(conn)

        # -- Page 1: no cursor --
        status1, body1 = _get_match_deck(
            distribution_domain=distribution_domain,
            access_token=completed_profile_user["access_token"],
            edge_secret=edge_secret,
        )
        assert status1 == 200, f"Page 1 failed: {status1} {body1}"
        results1 = body1.get("results", [])
        next_cursor = body1.get("next_cursor")

        assert len(results1) == 20, (
            f"Page 1 must return 20 rows when > 20 candidates exist. "
            f"Got {len(results1)}"
        )
        assert next_cursor is not None, (
            "next_cursor must be non-null when 20 rows are returned "
            f"(body: {body1})"
        )

        page1_ids = [r["user_id"] for r in results1]
        # The cursor equals the last user_id on page 1
        assert next_cursor == page1_ids[-1], (
            f"next_cursor ({next_cursor!r}) must equal last user_id on page 1 "
            f"({page1_ids[-1]!r})"
        )

        # -- Page 2: cursor from page 1 --
        status2, body2 = _get_match_deck(
            distribution_domain=distribution_domain,
            access_token=completed_profile_user["access_token"],
            edge_secret=edge_secret,
            cursor=next_cursor,
        )
        assert status2 == 200, f"Page 2 failed: {status2} {body2}"
        results2 = body2.get("results", [])

        # All page-2 rows must have user_id > the cursor (strictly after)
        for row in results2:
            assert row["user_id"] > next_cursor, (
                f"Page 2 row user_id {row['user_id']!r} is not strictly > "
                f"cursor {next_cursor!r}"
            )

        # No row on page 2 should overlap page 1
        page1_set = set(page1_ids)
        page2_ids = [r["user_id"] for r in results2]
        overlap = page1_set & set(page2_ids)
        assert not overlap, (
            f"Pages 1 and 2 share user_ids — no pagination overlap allowed. "
            f"Overlap: {overlap}"
        )

        # Page 2 must contain at least 1 row (we seeded 41 total)
        assert len(results2) >= 1, (
            "Page 2 must contain at least 1 row (41 candidates seeded). "
            f"Got {len(results2)}"
        )

    finally:
        _teardown_users(conn, *cand_ids)
        try:
            _refresh_deck_view(conn)
        except Exception:
            pass
        conn.close()


# ---------------------------------------------------------------------------
# IT-3: Opposite-sex filter
#
# A Male requester's deck must contain zero Male candidates even when seeded.
# ---------------------------------------------------------------------------


def test_given_male_requester_when_deck_requested_then_no_male_candidates_returned(
    completed_profile_user,
) -> None:
    """
    IT-3 (story 7.2 AC): opposite-sex filter exercise.

    Seed Male candidates (same sex as the Male requester).  Refresh.
    Assert: the deck contains zero of those seeded Male candidates.

    This verifies that the explicit WHERE dv.sex != current_setting(
    'app.requesting_user_sex', true) predicate in the deck SQL is
    operational — RLS does NOT propagate through materialized views,
    so without this predicate the Male candidates would appear.
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

    creds = _get_creds(boto3, region, master_secret_arn)
    conn = _master_conn(
        psycopg2,
        host=aurora_host,
        port=aurora_port,
        dbname=aurora_dbname,
        username=creds["username"],
        password=creds["password"],
    )

    # Seed 3 Male candidates — same sex as the requester
    male_cand_ids: list[str] = []

    try:
        for i in range(3):
            cid = str(uuid.uuid4())
            male_cand_ids.append(cid)
            _seed_user(
                conn,
                user_id=cid,
                email=f"deck_it3_male_{i}_{uuid.uuid4().hex[:6]}@test.invalid",
                sex="Male",   # same sex as requester — must be filtered out
                profile_complete_verified=True,
            )

        _refresh_deck_view(conn)

        status, body = _get_match_deck(
            distribution_domain=distribution_domain,
            access_token=completed_profile_user["access_token"],
            edge_secret=edge_secret,
        )

        assert status == 200, f"Expected 200, got {status}: {body}"
        result_ids = {r["user_id"] for r in body.get("results", [])}

        for male_id in male_cand_ids:
            assert male_id not in result_ids, (
                f"Male candidate ({male_id!r}) must not appear in a Male "
                f"requester's deck. Result ids: {result_ids}"
            )

    finally:
        _teardown_users(conn, *male_cand_ids)
        try:
            _refresh_deck_view(conn)
        except Exception:
            pass
        conn.close()
