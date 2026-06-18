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

Public API (as specified in story 6.0b):
  chat_room_id(user_a, user_b)    → stable, symmetric SHA-256 room ID derived from
                                    the lexicographically ordered pair of user UUIDs
  block_filter(other_user_col)    → SQL NOT EXISTS fragment for filtering blocked pairs;
                                    other_user_col must be in the hard-coded whitelist
  is_blocked(conn, user_a, user_b) → True if a block exists between the pair in either
                                    direction; uses parameter binding only

Public API (as specified in story 7.0b):
  require_profile_complete        → decorator: returns 403 {"error":"profile_incomplete"}
                                    when custom:profile_complete JWT claim != "true";
                                    fail-closed when claim is absent

Public API (as specified in story 8.0):
  require_profile_complete_appsync → decorator: returns AppSync Unauthorized error
                                     when custom:profile_complete != "true";
                                     reads from event["identity"]["claims"] (AppSync path,
                                     not event["requestContext"]["authorizer"]["jwt"]["claims"]);
                                     fail-closed when claim is absent

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
from knotify_obs._chat_room_id import chat_room_id
from knotify_obs._blocks import block_filter, is_blocked
from knotify_obs._profile_complete import require_profile_complete
from knotify_obs._profile_complete_appsync import require_profile_complete_appsync

__all__ = [
    "init_logger",
    "correlation_id_middleware",
    "verify_cognito_jwt",
    "EdgeSecretRequired",
    "require_edge_secret",
    "with_edge_secret",
    "chat_room_id",
    "block_filter",
    "is_blocked",
    "require_profile_complete",
    "require_profile_complete_appsync",
]
