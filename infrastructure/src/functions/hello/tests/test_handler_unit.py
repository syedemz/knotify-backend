"""
Unit tests for hello/handler.py — story 5.7.

Tests that do NOT require AWS access.  The @with_edge_secret decorator is
exercised at the unit level in infrastructure/src/layers/observability/tests/
test_edge_secret.py (story 5.6). These tests cover:

  (1) happy path: correct EDGE_SECRET → 200 with user sub in body
  (2) wrong EDGE_SECRET → 403 (decorator translates EdgeSecretRequired)
  (3) missing EDGE_SECRET header → 403 (decorator translates EdgeSecretRequired)
  (4) missing EDGE_SECRET env var → 403 (defensive: decorator catches config error)
  (5) handler reads sub from requestContext.authorizer.jwt.claims.sub
"""

from __future__ import annotations

import json
import os
import sys

import pytest

# ---------------------------------------------------------------------------
# Ensure the observability layer source is importable so the decorator can be
# resolved when the handler module is imported. In CI the layer is available at
# runtime; locally it lives at infrastructure/src/layers/observability/.
# ---------------------------------------------------------------------------

_OBS_LAYER_PATH = os.path.join(
    os.path.dirname(__file__),
    "..",  # hello/
    "..",  # functions/
    "..",  # src/
    "layers",
    "observability",
)
if _OBS_LAYER_PATH not in sys.path:
    sys.path.insert(0, os.path.abspath(_OBS_LAYER_PATH))

# Set required env before importing handler (module-level decorator runs at
# import time; EDGE_SECRET must exist for a clean import unless we mock os.environ
# before the first import, which is brittle). We set a known test value here.
_TEST_SECRET = "test-edge-secret-value"


def _load_handler():
    """
    Import (or re-import) the hello handler using importlib spec-from-file so
    the module is isolated from other 'handler' modules collected in the same
    pytest process.  Re-imports on every call so env var changes take effect.
    """
    import importlib.util

    handler_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "handler.py")
    )
    spec = importlib.util.spec_from_file_location("hello_handler", handler_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_event(sub: str = "test-sub-0001", edge_secret: str | None = _TEST_SECRET) -> dict:
    """Build a minimal HTTP API v2 payload format event."""
    headers: dict = {}
    if edge_secret is not None:
        headers["x-knotify-edge-secret"] = edge_secret
    return {
        "headers": headers,
        "requestContext": {
            "authorizer": {
                "jwt": {
                    "claims": {
                        "sub": sub,
                    }
                }
            }
        },
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHelloHandlerUnit:
    """Unit tests for hello/handler.py using @with_edge_secret decorator."""

    def setup_method(self):
        """Set EDGE_SECRET env var to a known value before each test."""
        os.environ["EDGE_SECRET"] = _TEST_SECRET

    def teardown_method(self):
        """Clean up EDGE_SECRET after each test."""
        os.environ.pop("EDGE_SECRET", None)

    def test_given_valid_secret_when_handler_invoked_then_returns_200_with_user_sub(self):
        """
        Given a request with the correct x-knotify-edge-secret header,
        when the handler is invoked,
        then it returns statusCode 200 with ok=True and the user's sub.
        """
        handler_module = _load_handler()
        sub = "abc-123-sub-value"
        event = _make_event(sub=sub, edge_secret=_TEST_SECRET)

        result = handler_module.handler(event, {})

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["ok"] is True
        assert body["user_id"] == sub

    def test_given_wrong_secret_when_handler_invoked_then_returns_403(self):
        """
        Given a request with an incorrect x-knotify-edge-secret header,
        when the handler is invoked,
        then the @with_edge_secret decorator returns statusCode 403.
        """
        handler_module = _load_handler()
        event = _make_event(edge_secret="wrong-secret-value")

        result = handler_module.handler(event, {})

        assert result["statusCode"] == 403
        body = json.loads(result["body"])
        assert "error" in body

    def test_given_missing_secret_header_when_handler_invoked_then_returns_403(self):
        """
        Given a request with no x-knotify-edge-secret header,
        when the handler is invoked,
        then the @with_edge_secret decorator returns statusCode 403.
        """
        handler_module = _load_handler()
        event = _make_event(edge_secret=None)

        result = handler_module.handler(event, {})

        assert result["statusCode"] == 403

    def test_given_missing_edge_secret_env_var_when_handler_invoked_then_returns_403(self):
        """
        Given the EDGE_SECRET env var is not set (Lambda misconfiguration),
        when the handler is invoked,
        then the @with_edge_secret decorator defensively returns statusCode 403.
        """
        os.environ.pop("EDGE_SECRET", None)
        handler_module = _load_handler()
        event = _make_event(edge_secret=_TEST_SECRET)

        result = handler_module.handler(event, {})

        assert result["statusCode"] == 403

    def test_given_valid_request_when_handler_invoked_then_response_has_json_content_type(self):
        """
        Given a valid request,
        when the handler is invoked,
        then the response includes Content-Type: application/json.
        """
        handler_module = _load_handler()
        event = _make_event()

        result = handler_module.handler(event, {})

        assert result["statusCode"] == 200
        assert result.get("headers", {}).get("Content-Type") == "application/json"
