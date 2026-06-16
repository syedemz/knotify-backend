"""
Unit tests for require_profile_complete decorator.

Story 7.0b AC: knotify_obs exposes a @require_profile_complete decorator that:
  - Reads custom:profile_complete from event["requestContext"]["authorizer"]["jwt"]["claims"]
  - Returns 403 {"error":"profile_incomplete"} when the claim is "false"
  - Returns 403 {"error":"profile_incomplete"} when the claim is absent (fail-closed)
  - Allows the request through when the claim is "true"

Three branches covered:
  - Claim present and "true"  → handler is called, returns its response
  - Claim present and "false" → 403 profile_incomplete (short-circuit)
  - Claim absent              → 403 profile_incomplete (fail-closed)
"""

from __future__ import annotations

import json
import unittest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_event(profile_complete_claim=None):
    """
    Build a minimal HTTP API v2 event with JWT claims.

    When profile_complete_claim is None, the claim is absent entirely.
    """
    claims = {"sub": "user-uuid-1234"}
    if profile_complete_claim is not None:
        claims["custom:profile_complete"] = profile_complete_claim

    return {
        "requestContext": {
            "authorizer": {
                "jwt": {
                    "claims": claims,
                }
            }
        }
    }


class TestRequireProfileCompleteDecorator(unittest.TestCase):
    """Tests for the @require_profile_complete decorator."""

    # -----------------------------------------------------------------------
    # Branch 1: claim = "true" → handler passes through
    # -----------------------------------------------------------------------

    def test_given_claim_true_when_decorated_handler_called_then_handler_executes(self):
        """
        Given custom:profile_complete = "true" in JWT claims,
        when the decorated handler is called,
        then the inner handler is executed and its response returned.
        """
        from knotify_obs import require_profile_complete

        @require_profile_complete
        def inner(event, context):
            return {"statusCode": 200, "body": "ok"}

        event = _make_event("true")
        result = inner(event, None)

        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(result["body"], "ok")

    # -----------------------------------------------------------------------
    # Branch 2: claim = "false" → 403 profile_incomplete
    # -----------------------------------------------------------------------

    def test_given_claim_false_when_decorated_handler_called_then_returns_403_profile_incomplete(self):
        """
        Given custom:profile_complete = "false" in JWT claims,
        when the decorated handler is called,
        then 403 {"error":"profile_incomplete"} is returned without calling handler.
        """
        from knotify_obs import require_profile_complete

        handler_called = []

        @require_profile_complete
        def inner(event, context):
            handler_called.append(True)
            return {"statusCode": 200, "body": "ok"}

        event = _make_event("false")
        result = inner(event, None)

        self.assertEqual(result["statusCode"], 403)
        body = json.loads(result["body"])
        self.assertEqual(body["error"], "profile_incomplete")
        self.assertEqual(handler_called, [], "Inner handler must NOT be called when profile incomplete")

    # -----------------------------------------------------------------------
    # Branch 3: claim absent → 403 profile_incomplete (fail-closed)
    # -----------------------------------------------------------------------

    def test_given_claim_absent_when_decorated_handler_called_then_returns_403_profile_incomplete(self):
        """
        Given custom:profile_complete is absent from JWT claims,
        when the decorated handler is called,
        then 403 {"error":"profile_incomplete"} is returned (fail-closed).
        """
        from knotify_obs import require_profile_complete

        handler_called = []

        @require_profile_complete
        def inner(event, context):
            handler_called.append(True)
            return {"statusCode": 200, "body": "ok"}

        event = _make_event(None)  # claim absent
        result = inner(event, None)

        self.assertEqual(result["statusCode"], 403)
        body = json.loads(result["body"])
        self.assertEqual(body["error"], "profile_incomplete")
        self.assertEqual(handler_called, [], "Inner handler must NOT be called when claim absent")

    # -----------------------------------------------------------------------
    # Decorator preserves the inner handler's function name (__wrapped__)
    # -----------------------------------------------------------------------

    def test_given_decorated_handler_when_inspecting_name_then_original_name_preserved(self):
        """
        The decorator uses functools.wraps so the function name and docstring
        of the original handler are preserved — important for test introspection
        and Lambda metrics.
        """
        from knotify_obs import require_profile_complete

        @require_profile_complete
        def my_handler(event, context):
            """My handler docstring."""
            return {}

        self.assertEqual(my_handler.__name__, "my_handler")


if __name__ == "__main__":
    unittest.main()
