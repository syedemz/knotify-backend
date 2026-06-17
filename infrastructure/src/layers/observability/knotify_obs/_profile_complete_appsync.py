"""
Profile-completion guard for Knotify AppSync Lambda resolvers.

AppSync Lambda resolvers receive Cognito identity under
event["identity"]["claims"], unlike the REST handlers that use
event["requestContext"]["authorizer"]["jwt"]["claims"].

Handlers gated by @require_profile_complete_appsync will return a structured
AppSync error before any business logic runs when the caller's profile is not
yet complete.

Public API:
  require_profile_complete_appsync  — decorator: returns AppSync Unauthorized
                                       error when custom:profile_complete != "true",
                                       fail-closed when the claim is absent.

Usage (canonical for AppSync resolvers):

    from knotify_obs import require_profile_complete_appsync

    @require_profile_complete_appsync
    def handler(event, context):
        ...

Mirrors _profile_complete.py (story 7.0b), which serves REST handlers reading
from event["requestContext"]["authorizer"]["jwt"]["claims"].  The two decorators
exist because AppSync and HTTP API Gateway embed claims at different paths.
"""

from __future__ import annotations

import functools
from typing import Any, Dict

_CLAIM_NAME = "custom:profile_complete"


def require_profile_complete_appsync(handler):
    """
    Decorator for AppSync Lambda resolver handlers that gates on the
    profile-complete claim.

    Reads custom:profile_complete from
        event["identity"]["claims"]

    Returns AppSync-structured Unauthorized error when:
      - The claim value is not "true" (e.g. "false")
      - The claim is absent (fail-closed — treat missing claim as incomplete)
      - The "identity" or "claims" key is absent (fail-closed)

    Allows the request through when the claim value is exactly "true".

    Does not connect to Aurora, DynamoDB, or any external service.
    """

    @functools.wraps(handler)
    def wrapper(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
        claims: dict = (
            event
            .get("identity", {})
            .get("claims", {})
        )

        profile_complete_claim = claims.get(_CLAIM_NAME)

        if profile_complete_claim != "true":
            return {
                "errorType": "Unauthorized",
                "reason": "PROFILE_INCOMPLETE",
            }

        return handler(event, context)

    return wrapper
