"""
Profile-completion guard for Knotify Lambda handlers.

The HTTP API JWT authorizer embeds Cognito claims in
event["requestContext"]["authorizer"]["jwt"]["claims"], including
the custom:profile_complete claim that is set by the cognito_pre_token_generation
Lambda (which mirrors the custom:profile_complete Cognito user attribute).

Handlers gated by @require_profile_complete will return 403 before any
business logic runs when the caller's profile is not yet complete.

Public API:
  require_profile_complete  — decorator: returns 403 {"error":"profile_incomplete"}
                               when custom:profile_complete != "true", fail-closed
                               when the claim is absent.

Usage (canonical):

    from knotify_obs import with_edge_secret, require_profile_complete

    @with_edge_secret
    @require_profile_complete
    def handler(event, context):
        ...

Decorator order: @with_edge_secret outermost, @require_profile_complete inside it.
The edge-secret rejection fires first (before any JWT-claims access).
"""

from __future__ import annotations

import functools
import json
from typing import Any, Dict

_CLAIM_NAME = "custom:profile_complete"


def require_profile_complete(handler):
    """
    Decorator for Lambda handlers that gates on the profile-complete claim.

    Reads custom:profile_complete from
        event["requestContext"]["authorizer"]["jwt"]["claims"]

    Returns 403 {"error":"profile_incomplete"} when:
      - The claim value is "false"
      - The claim is absent (fail-closed — treat missing claim as incomplete)

    Allows the request through when the claim value is "true".

    Does not connect to Aurora or any external service.
    """

    @functools.wraps(handler)
    def wrapper(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
        claims: dict = (
            event
            .get("requestContext", {})
            .get("authorizer", {})
            .get("jwt", {})
            .get("claims", {})
        )

        profile_complete_claim = claims.get(_CLAIM_NAME)

        if profile_complete_claim != "true":
            return {
                "statusCode": 403,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"error": "profile_incomplete"}),
            }

        return handler(event, context)

    return wrapper
