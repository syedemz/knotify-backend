"""
knotify_obs — thin observability wrapper for Knotify Lambda functions.

Public API (as specified in story 3.2):
  init_logger(service)            → aws_lambda_powertools.Logger bound to service
  correlation_id_middleware       → callable decorator that injects the HTTP API
                                    request ID as a correlation ID into log records
  verify_cognito_jwt(token, user_pool_id, region, *, audience=None)
                                  → decoded claims dict, or raises on invalid token

Public API (as specified in story 5.6):
  EdgeSecretRequired              → exception raised when edge-secret check fails
  require_edge_secret(event)      → validates x-knotify-edge-secret header;
                                    raises EdgeSecretRequired on mismatch or absence
  with_edge_secret                → decorator: translates EdgeSecretRequired → 403

NOTE: verify_cognito_jwt is rarely used in v1.  The HTTP API Cognito JWT
authorizer (phase 5) handles routine validation natively — business Lambdas
read claims from event.requestContext.authorizer.jwt.claims.  This helper
exists for Lambda authorizers or other niche paths that may emerge in
phase 11 hardening.  See README.md for details.
"""

from knotify_obs._logger import init_logger
from knotify_obs._middleware import correlation_id_middleware
from knotify_obs._jwt import verify_cognito_jwt
from knotify_obs._edge_secret import EdgeSecretRequired, require_edge_secret, with_edge_secret

__all__ = [
    "init_logger",
    "correlation_id_middleware",
    "verify_cognito_jwt",
    "EdgeSecretRequired",
    "require_edge_secret",
    "with_edge_secret",
]
