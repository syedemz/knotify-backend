"""
hello — stub Lambda for phase-5 end-to-end edge smoke test.

Validates that:
  - the JWT authorizer (story 5.1) is correctly enforced via HTTP API Gateway
  - the @with_edge_secret decorator (story 5.6) guards the raw execute-api endpoint
  - the full CloudFront → WAF → HTTP API → Lambda path is functional

This Lambda and its route are removed in phase-6 story 6.0 before any
domain Lambda lands.
"""

import json

from knotify_obs import with_edge_secret


@with_edge_secret
def handler(event: dict, context: object) -> dict:
    """
    Return 200 with the authenticated user's Cognito sub.

    The @with_edge_secret decorator handles edge-secret validation before
    this body runs. A missing or wrong x-knotify-edge-secret header causes
    the decorator to return a 403 directly — this body never executes in
    that case.

    The JWT authorizer (HTTP API layer) handles token validation before the
    Lambda is invoked at all. By the time this runs, both checks have passed.
    """
    sub = event["requestContext"]["authorizer"]["jwt"]["claims"]["sub"]
    return {
        "statusCode": 200,
        "body": json.dumps({"ok": True, "user_id": sub}),
        "headers": {"Content-Type": "application/json"},
    }
