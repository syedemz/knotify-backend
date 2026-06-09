"""
End-to-end edge smoke test for phase 5 — story 5.7.

Validates the full CloudFront → WAF → HTTP API Gateway → hello Lambda path:

  (a) GET <distribution_domain_name>/v1/_internal/hello with Authorization
      header returns 200 with the user's sub in the body.

  (b) The same request to <distribution_domain_name>/v1/_internal/hello
      WITHOUT the Authorization header returns 401 (built-in JWT authorizer
      rejects the request before it reaches Lambda).

  (c) GET <execute_api_endpoint>/v1/_internal/hello (bypassing CloudFront)
      WITH a valid Authorization header but NO x-knotify-edge-secret header
      returns 403 (@with_edge_secret decorator rejects the request).

  (d) GET <execute_api_endpoint>/v1/_internal/hello WITH the correct
      x-knotify-edge-secret header value (read from .env.test) returns 200
      — confirms the helper accepts the right secret, not just rejects all
      direct-API traffic.

Endpoint values are loaded from infrastructure/src/tests/integration/.env.test
which is written by the `local_file.integration_test_env` Terraform resource
(story 5.6) after `terraform apply` in dev.

Required environment variables (sourced from .env.test):
  EXECUTE_API_ENDPOINT      — raw HTTP API execute-api URL
  DISTRIBUTION_DOMAIN_NAME  — CloudFront distribution hostname (d*.cloudfront.net)
  EDGE_SECRET               — value of the x-knotify-edge-secret header

Additional env vars required by the `signed_in_user` fixture (from conftest.py):
  COGNITO_USER_POOL_ID
  COGNITO_INTEGRATION_TEST_CLIENT_ID
  AURORA_HOST, AURORA_PORT, AURORA_DBNAME, AURORA_MASTER_SECRET_ARN
  AWS_REGION

Run:
    pytest infrastructure/src/tests/integration/ -v -m integration

Skip without live AWS:
    pytest -m "not integration"
"""

from __future__ import annotations

import os
import pathlib

import pytest

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Load .env.test into os.environ if the file exists.
#
# The file is written by `terraform apply` via the local_file resource in
# story 5.6. Each line is `KEY=VALUE`; blank lines and # comments are ignored.
# We parse it manually (no python-dotenv dependency required) and inject into
# os.environ so _require_env() in conftest.py picks up the values.
# ---------------------------------------------------------------------------

_ENV_TEST_PATH = pathlib.Path(__file__).parent / ".env.test"

if _ENV_TEST_PATH.exists():
    with _ENV_TEST_PATH.open() as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#"):
                continue
            if "=" in _line:
                _key, _, _val = _line.partition("=")
                os.environ.setdefault(_key.strip(), _val.strip())


# ---------------------------------------------------------------------------
# Local helper: skip if a required env var is absent (mirrors conftest helper)
# ---------------------------------------------------------------------------

def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(
            f"Required env var {name!r} is not set — "
            "run `terraform apply` in dev and source .env.test, "
            "or set EXECUTE_API_ENDPOINT / DISTRIBUTION_DOMAIN_NAME / EDGE_SECRET manually."
        )
    return value


# ---------------------------------------------------------------------------
# Edge smoke tests
# ---------------------------------------------------------------------------


class TestEdgeSmoke:
    """
    End-to-end smoke tests verifying the phase-5 edge stack.

    These tests require the dev CloudFront + WAF + HTTP API Gateway + hello
    Lambda to be deployed (terraform apply in dev).  They are always skipped
    when .env.test is absent or the required env vars are not set.
    """

    def test_a_given_valid_jwt_via_cloudfront_when_get_hello_then_returns_200_with_sub(
        self, signed_in_user
    ):
        """
        AC (a): GET via CloudFront with valid Authorization header → 200 with sub.

        Exercises the full path: CloudFront → WAF → HTTP API JWT authorizer →
        hello Lambda. The @with_edge_secret decorator passes because CloudFront
        injects the correct x-knotify-edge-secret origin header automatically.
        """
        try:
            import requests
        except ImportError:
            pytest.skip("requests is not installed")

        distribution_domain = _require_env("DISTRIBUTION_DOMAIN_NAME")
        id_token = signed_in_user["id_token"]
        expected_sub = signed_in_user["sub"]

        url = f"https://{distribution_domain}/v1/_internal/hello"
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {id_token}"},
            timeout=30,
        )

        assert response.status_code == 200, (
            f"Expected 200 from {url} with valid JWT. "
            f"Got {response.status_code}: {response.text[:200]}"
        )
        body = response.json()
        assert body.get("ok") is True, f"Expected ok=True in body, got: {body}"
        assert body.get("user_id") == expected_sub, (
            f"Expected user_id={expected_sub!r} in body, got {body.get('user_id')!r}"
        )

    def test_b_given_no_jwt_via_cloudfront_when_get_hello_then_returns_401(
        self, signed_in_user
    ):
        """
        AC (b): GET via CloudFront WITHOUT Authorization header → 401.

        The built-in HTTP API JWT authorizer rejects the request before it
        reaches Lambda (no invocation occurs).
        """
        try:
            import requests
        except ImportError:
            pytest.skip("requests is not installed")

        distribution_domain = _require_env("DISTRIBUTION_DOMAIN_NAME")

        url = f"https://{distribution_domain}/v1/_internal/hello"
        response = requests.get(url, timeout=30)

        assert response.status_code == 401, (
            f"Expected 401 (JWT authorizer rejection) from {url} with no Authorization. "
            f"Got {response.status_code}: {response.text[:200]}"
        )

    def test_c_given_valid_jwt_bypass_cloudfront_no_edge_secret_then_returns_403(
        self, signed_in_user
    ):
        """
        AC (c): GET direct to execute-api with valid JWT but no edge-secret → 403.

        Bypasses CloudFront entirely. The JWT authorizer passes (valid token)
        but the @with_edge_secret decorator detects the missing
        x-knotify-edge-secret header and returns 403 before the handler body runs.
        """
        try:
            import requests
        except ImportError:
            pytest.skip("requests is not installed")

        execute_api_endpoint = _require_env("EXECUTE_API_ENDPOINT")
        id_token = signed_in_user["id_token"]

        url = f"{execute_api_endpoint}/v1/_internal/hello"
        response = requests.get(
            url,
            headers={"Authorization": f"Bearer {id_token}"},
            timeout=30,
        )

        assert response.status_code == 403, (
            f"Expected 403 (@with_edge_secret rejection) from direct execute-api "
            f"{url} when x-knotify-edge-secret is absent. "
            f"Got {response.status_code}: {response.text[:200]}"
        )

    def test_d_given_valid_jwt_and_correct_edge_secret_direct_api_then_returns_200(
        self, signed_in_user
    ):
        """
        AC (d): GET direct to execute-api with valid JWT AND correct edge-secret → 200.

        Confirms the @with_edge_secret helper accepts the right secret (not just
        rejects everything). Direct access with the correct secret should succeed
        identically to the CloudFront path.
        """
        try:
            import requests
        except ImportError:
            pytest.skip("requests is not installed")

        execute_api_endpoint = _require_env("EXECUTE_API_ENDPOINT")
        edge_secret = _require_env("EDGE_SECRET")
        id_token = signed_in_user["id_token"]
        expected_sub = signed_in_user["sub"]

        url = f"{execute_api_endpoint}/v1/_internal/hello"
        response = requests.get(
            url,
            headers={
                "Authorization": f"Bearer {id_token}",
                "x-knotify-edge-secret": edge_secret,
            },
            timeout=30,
        )

        assert response.status_code == 200, (
            f"Expected 200 from direct execute-api {url} with correct "
            f"x-knotify-edge-secret. "
            f"Got {response.status_code}: {response.text[:200]}"
        )
        body = response.json()
        assert body.get("ok") is True, f"Expected ok=True in body, got: {body}"
        assert body.get("user_id") == expected_sub, (
            f"Expected user_id={expected_sub!r} in body, got {body.get('user_id')!r}"
        )
