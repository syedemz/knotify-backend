"""
Edge-secret guard for Knotify Lambda handlers.

CloudFront injects `x-knotify-edge-secret` on every request before forwarding
to the HTTP API Gateway origin. Handlers call `require_edge_secret(event)` (or
apply the `@with_edge_secret` decorator) to reject traffic that bypassed
CloudFront and hit the raw execute-api endpoint directly.

Public API:
  EdgeSecretRequired        — exception raised on mismatch / missing header
  require_edge_secret(event) — validates header; raises EdgeSecretRequired on failure
  with_edge_secret           — decorator: translates EdgeSecretRequired → 403 response
"""

import functools
import hmac
import json
import os
from typing import Any, Dict


class EdgeSecretRequired(Exception):
    """
    Raised when the x-knotify-edge-secret header is absent, incorrect, or when
    the EDGE_SECRET environment variable is not configured on the Lambda.
    """


def require_edge_secret(event: Dict[str, Any]) -> None:
    """
    Validate that the incoming Lambda event carries the correct edge secret.

    Reads EDGE_SECRET from the environment and compares it constant-time
    against the x-knotify-edge-secret header value. Header keys are
    lower-cased before lookup so the check is case-insensitive (HTTP API
    Gateway can deliver headers in any casing).

    Returns None when the secret matches.

    Raises EdgeSecretRequired when:
    - EDGE_SECRET env var is unset or empty (Lambda misconfiguration)
    - the header is absent from the event
    - the header value does not match EDGE_SECRET
    """
    edge_secret_env = os.environ.get("EDGE_SECRET")
    if not edge_secret_env:
        raise EdgeSecretRequired("EDGE_SECRET env var is not configured")

    headers = {k.lower(): v for k, v in event.get("headers", {}).items()}
    secret_from_header = headers.get("x-knotify-edge-secret")

    if secret_from_header is None:
        raise EdgeSecretRequired("x-knotify-edge-secret header is missing")

    try:
        match = hmac.compare_digest(
            secret_from_header.encode(),
            edge_secret_env.encode(),
        )
    except (TypeError, AttributeError):
        raise EdgeSecretRequired("x-knotify-edge-secret header value is invalid")

    if not match:
        raise EdgeSecretRequired("x-knotify-edge-secret header value does not match")


def with_edge_secret(handler):
    """
    Decorator for Lambda handlers that gates on the edge secret.

    Usage (canonical — apply to the handler function):

        from knotify_obs import with_edge_secret

        @with_edge_secret
        def handler(event, context):
            # secret already validated here
            return {"statusCode": 200, "body": json.dumps({"ok": True})}

    Behaviour:
    - Calls `require_edge_secret(event)` before handing off to the handler.
    - If EdgeSecretRequired is raised, returns a 403 JSON response immediately;
      the handler body is never executed.
    - Any other exception raised inside the handler propagates unchanged.
    """

    @functools.wraps(handler)
    def wrapper(event, context):
        try:
            require_edge_secret(event)
        except EdgeSecretRequired:
            return {
                "statusCode": 403,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({"error": "forbidden"}),
            }
        return handler(event, context)

    return wrapper
