"""
Integration regression sweep — story 6.5.

Verifies that every route wired by stories 6.1–6.4 meets four invariants:

  1. The route exists in the deployed dev HTTP API
     (via boto3 apigatewayv2.get_routes, paginated).

  2. authorization_type is "JWT" and authorizer_id matches the Cognito
     authorizer provisioned by module.api_gateway.

  3. An unauthenticated request (no Authorization header) via the CloudFront
     URL returns HTTP 401 — API Gateway rejects before the Lambda is invoked.

  4. An authenticated request that hits the execute-api URL directly
     (bypassing CloudFront, i.e., no x-knotify-edge-secret header) returns
     HTTP 403 — the @with_edge_secret decorator fires at the Lambda layer.

Routes under test (19 total, enumerated from dev/main.tf):

  6.1 / profile (4 routes):
    GET  /v1/profile/me
    PATCH /v1/profile/me
    GET  /v1/profiles
    GET  /v1/profiles/{userId}

  6.2 / friends (7 routes):
    GET    /v1/friends
    DELETE /v1/friends/{userId}
    GET    /v1/friend-requests
    POST   /v1/friend-requests
    POST   /v1/friend-requests/{id}/accept
    POST   /v1/friend-requests/{id}/decline
    DELETE /v1/friend-requests/{id}

  6.3 / bookmarks (3 routes):
    GET    /v1/bookmarks
    POST   /v1/bookmarks
    DELETE /v1/bookmarks/{userId}

  6.4 / blocks (3 routes):
    GET    /v1/blocks
    POST   /v1/blocks
    DELETE /v1/blocks/{userId}

  7.5 / match (2 routes):
    POST /v1/match/search
    GET  /v1/match/deck

NOTE (drift advisory — 2026-06-10):
  Dev infrastructure is currently destroyed. These tests are authored and
  committed so CI can run them on the next terraform apply. Until that apply
  completes they will be skipped (missing env vars / missing .env.test).

NOTE (terraform plan zero-diff):
  The acceptance criterion "terraform plan shows zero diff" is verified by
  CI's post-apply plan-only job on the next apply, not by this test file.
  No new Terraform resources are introduced in story 6.5.

Required env vars (set by CI from .env.test written by story 5.6's
local_file.integration_test_env, plus the Cognito/Aurora vars already
used by other integration tests):

  AWS_REGION
  EXECUTE_API_ENDPOINT         — https://<api-id>.execute-api.<region>.amazonaws.com
  DISTRIBUTION_DOMAIN_NAME     — CloudFront FQDN
  EDGE_SECRET                  — value of x-knotify-edge-secret

  For invariant 4 (authenticated 403 probe), the signed_in_user fixture
  also requires:
    COGNITO_USER_POOL_ID
    COGNITO_INTEGRATION_TEST_CLIENT_ID
    AURORA_HOST, AURORA_PORT, AURORA_DBNAME, AURORA_MASTER_SECRET_ARN

Run:
    pytest infrastructure/src/tests/integration/test_route_wiring.py -v -m integration

Skip without live AWS access:
    pytest -m "not integration"
"""

from __future__ import annotations

import os
import re
import uuid

import pytest

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Expected route inventory (source-of-truth: dev/main.tf aws_apigatewayv2_route.*)
# Each entry is the route_key string exactly as declared in Terraform.
# ---------------------------------------------------------------------------

_EXPECTED_ROUTE_KEYS: frozenset[str] = frozenset(
    [
        # story 6.1 — profile
        "GET /v1/profile/me",
        "PATCH /v1/profile/me",
        "GET /v1/profiles",
        "GET /v1/profiles/{userId}",
        # story 6.2 — friends
        "GET /v1/friends",
        "DELETE /v1/friends/{userId}",
        "GET /v1/friend-requests",
        "POST /v1/friend-requests",
        "POST /v1/friend-requests/{id}/accept",
        "POST /v1/friend-requests/{id}/decline",
        "DELETE /v1/friend-requests/{id}",
        # story 6.3 — bookmarks
        "GET /v1/bookmarks",
        "POST /v1/bookmarks",
        "DELETE /v1/bookmarks/{userId}",
        # story 6.4 — blocks
        "GET /v1/blocks",
        "POST /v1/blocks",
        "DELETE /v1/blocks/{userId}",
        # story 7.5 — match
        "POST /v1/match/search",
        "GET /v1/match/deck",
    ]
)

# A syntactically valid UUID used to substitute path parameters ({userId}, {id})
# when probing routes. The substitute is never expected to match a real resource —
# we only need the auth gates (401, 403) to fire, which happen before any DB call.
_PROBE_UUID = "00000000-0000-4000-8000-000000000001"


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------


def _require_env(name: str) -> str:
    """
    Return the value of environment variable *name*.

    Calls pytest.skip if absent so the test is marked SKIPPED (not ERROR)
    when the live dev environment is not configured.
    """
    value = os.environ.get(name)
    if not value:
        pytest.skip(
            f"Required env var {name!r} is not set — live dev environment not configured."
        )
    return value


def _parse_api_id_from_endpoint(execute_api_endpoint: str) -> str:
    """
    Extract the API Gateway API ID from EXECUTE_API_ENDPOINT.

    The endpoint format is:
        https://<api-id>.execute-api.<region>.amazonaws.com
    """
    match = re.match(
        r"https://([a-z0-9]+)\.execute-api\.[a-z0-9-]+\.amazonaws\.com",
        execute_api_endpoint,
    )
    if not match:
        pytest.skip(
            f"Cannot parse API ID from EXECUTE_API_ENDPOINT={execute_api_endpoint!r}. "
            "Expected format: https://<id>.execute-api.<region>.amazonaws.com"
        )
    return match.group(1)


def _resolve_probe_path(route_key: str) -> str:
    """
    Convert a route_key (e.g. "POST /v1/friend-requests/{id}/accept") to a
    concrete URL path by substituting all {param} placeholders with a probe UUID.

    The verb prefix is stripped — only the path portion is returned.
    """
    # Strip the HTTP method prefix ("GET /v1/..." → "/v1/...")
    path = route_key.split(" ", 1)[1]
    # Replace every {placeholder} with the probe UUID
    return re.sub(r"\{[^}]+\}", _PROBE_UUID, path)


# ---------------------------------------------------------------------------
# Route enumeration helper (boto3)
# ---------------------------------------------------------------------------


def _get_all_routes(apigw_client, api_id: str) -> list[dict]:
    """
    Return all routes for the given HTTP API, handling pagination.

    Each dict in the returned list is an API Gateway Route object with at
    least the keys: RouteKey, AuthorizationType, AuthorizerId.
    """
    routes: list[dict] = []
    kwargs: dict = {"ApiId": api_id}
    while True:
        resp = apigw_client.get_routes(**kwargs)
        routes.extend(resp.get("Items", []))
        next_token = resp.get("NextToken")
        if not next_token:
            break
        kwargs["NextToken"] = next_token
    return routes


# ---------------------------------------------------------------------------
# Invariant 1 + 2 — route existence and JWT authorization attributes
# ---------------------------------------------------------------------------


def test_all_expected_routes_exist_with_jwt_authorization():
    """
    Given the deployed dev HTTP API,
    when boto3 lists all routes (paginated),
    then every route in _EXPECTED_ROUTE_KEYS must be present, each with
    AuthorizationType=="JWT" and a non-empty AuthorizerId.

    Also verifies that all 17 expected routes share the same AuthorizerId
    value — the single Cognito JWT authorizer provisioned by module.api_gateway.
    """
    try:
        import boto3
    except ImportError:
        pytest.skip("boto3 is not installed — live AWS environment required")

    region = _require_env("AWS_REGION")
    execute_api_endpoint = _require_env("EXECUTE_API_ENDPOINT")
    api_id = _parse_api_id_from_endpoint(execute_api_endpoint)

    apigw_client = boto3.client("apigatewayv2", region_name=region)
    all_routes = _get_all_routes(apigw_client, api_id)

    # Index deployed routes by route_key for O(1) lookup
    deployed: dict[str, dict] = {r["RouteKey"]: r for r in all_routes}

    missing_routes: list[str] = []
    wrong_auth: list[str] = []
    authorizer_ids: set[str] = set()

    for route_key in sorted(_EXPECTED_ROUTE_KEYS):
        if route_key not in deployed:
            missing_routes.append(route_key)
            continue

        route = deployed[route_key]
        auth_type = route.get("AuthorizationType", "")
        authorizer_id = route.get("AuthorizerId", "")

        if auth_type != "JWT" or not authorizer_id:
            wrong_auth.append(
                f"{route_key!r}: AuthorizationType={auth_type!r}, "
                f"AuthorizerId={authorizer_id!r}"
            )
        else:
            authorizer_ids.add(authorizer_id)

    assert not missing_routes, (
        f"The following expected routes are MISSING from the deployed API "
        f"(api_id={api_id!r}):\n"
        + "\n".join(f"  - {r}" for r in missing_routes)
    )
    assert not wrong_auth, (
        "The following routes have incorrect authorization configuration "
        "(expected AuthorizationType='JWT' and a non-empty AuthorizerId):\n"
        + "\n".join(f"  - {msg}" for msg in wrong_auth)
    )
    # All correctly-configured routes must share exactly one authorizer ID —
    # the single Cognito JWT authorizer created by module.api_gateway.
    # Route count: 4 (profile) + 7 (friends) + 3 (bookmarks) + 3 (blocks) + 2 (match) = 19
    assert len(authorizer_ids) == 1, (
        f"Expected exactly one AuthorizerId across all {len(_EXPECTED_ROUTE_KEYS)} routes "
        f"(single Cognito JWT authorizer), but found {len(authorizer_ids)}: {authorizer_ids!r}"
    )


# ---------------------------------------------------------------------------
# Invariant 3 — unauthenticated requests via CloudFront return HTTP 401
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route_key", sorted(_EXPECTED_ROUTE_KEYS))
def test_unauthenticated_request_via_cloudfront_returns_401(route_key: str):
    """
    Given a route wired to the deployed dev HTTP API,
    when an HTTP request is sent via the CloudFront URL WITHOUT an
    Authorization header,
    then the response must be HTTP 401 (API Gateway JWT authorizer rejects
    before the Lambda is invoked).

    Parametrized over all 17 expected routes.
    Path-parameter placeholders ({userId}, {id}) are replaced with a probe UUID.
    """
    try:
        import requests as http_requests
    except ImportError:
        pytest.skip("requests library is not installed — live environment required")

    distribution_domain = _require_env("DISTRIBUTION_DOMAIN_NAME")

    method, raw_path = route_key.split(" ", 1)
    probe_path = _resolve_probe_path(route_key)
    url = f"https://{distribution_domain}{probe_path}"

    # No Authorization header — the JWT authorizer must reject at the API layer.
    response = http_requests.request(method, url, timeout=15)

    assert response.status_code == 401, (
        f"Expected HTTP 401 for unauthenticated {method} {raw_path!r} "
        f"via CloudFront (url={url!r}), "
        f"got HTTP {response.status_code}. Body: {response.text[:200]!r}"
    )


# ---------------------------------------------------------------------------
# Invariant 4 — authenticated requests via execute-api (bypassing CloudFront)
#               return HTTP 403 due to @with_edge_secret rejection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route_key", sorted(_EXPECTED_ROUTE_KEYS))
def test_authenticated_direct_execute_api_request_returns_403(
    route_key: str, signed_in_user
):
    """
    Given a route wired to the deployed dev HTTP API,
    when an HTTP request is sent directly to the execute-api URL (bypassing
    CloudFront) with a valid JWT but WITHOUT the x-knotify-edge-secret header,
    then the response must be HTTP 403 — the @with_edge_secret decorator
    rejects the request at the Lambda layer before any DB call is made.

    Parametrized over all 17 expected routes.
    The signed_in_user fixture provides a valid Cognito JWT.
    Path-parameter placeholders are replaced with a probe UUID.
    """
    try:
        import requests as http_requests
    except ImportError:
        pytest.skip("requests library is not installed — live environment required")

    execute_api_endpoint = _require_env("EXECUTE_API_ENDPOINT")

    method, raw_path = route_key.split(" ", 1)
    probe_path = _resolve_probe_path(route_key)

    # Construct the execute-api URL directly (no CloudFront hostname).
    # execute_api_endpoint already has the trailing base — just append the path.
    base = execute_api_endpoint.rstrip("/")
    url = f"{base}{probe_path}"

    # Valid JWT — API Gateway authorizer accepts it.
    # No x-knotify-edge-secret header — @with_edge_secret must reject.
    headers = {
        "Authorization": f"Bearer {signed_in_user['access_token']}",
        # x-knotify-edge-secret is intentionally omitted
        "Content-Type": "application/json",
    }

    response = http_requests.request(method, url, headers=headers, timeout=15)

    assert response.status_code == 403, (
        f"Expected HTTP 403 for authenticated {method} {raw_path!r} "
        f"hitting execute-api directly (no edge secret), "
        f"got HTTP {response.status_code}. Body: {response.text[:200]!r}"
    )
