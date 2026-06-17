"""
knotify-chat-resolver Lambda handler — story 8.0 scaffold.

This is an AppSync Lambda resolver (not an HTTP API Gateway proxy event).
AppSync invokes this Lambda for every resolver field that is mapped to the
chat_resolver data source.  The event shape is:

    {
        "typeName":  "Query" | "Mutation" | "Subscription",
        "fieldName": "<field>",
        "identity":  {
            "sub": "<cognito-sub>",
            "issuer": "...",
            "claims": {
                "sub": "...",
                "custom:profile_complete": "true" | "false",
                ...
            }
        },
        "arguments": { ... },
        "source": null | {...},
        ...
    }

Story 8.0 ships an EMPTY dispatcher that returns a structured
{"errorType": "Unimplemented", "message": "..."} for every (typeName, fieldName)
pair.  Later stories (8.3, 8.4, 8.5, 8.7, 8.8) slot concrete implementations
into _dispatch() without touching this scaffold.

Design decisions recorded here so later stories don't re-debate them:
  - knotify_db.block_filter() is the ONLY acceptable block-exclusion mechanism.
    Hand-rolled NOT EXISTS subqueries are forbidden (hotfix #87 lesson).
  - The @require_profile_complete_appsync decorator reads
    event["identity"]["claims"]["custom:profile_complete"] and returns
    Unauthorized when not "true".  It is applied at the handler entrypoint so
    every mutation and query is protected; subscription connect-path resolvers
    in the pipeline (story 8.6) add an explicit membership check instead.

Dependencies (Lambda layers):
  - knotify_obs: init_logger, require_profile_complete_appsync
  - knotify_db:  get_connection, rls_context, block_filter  (used by later stories)
"""

from __future__ import annotations

import os
from typing import Any

import knotify_db  # noqa: F401 — imported for later stories; unused in 8.0 scaffold
from knotify_obs import init_logger, require_profile_complete_appsync

# ---------------------------------------------------------------------------
# Module-level singletons (cold-start optimization)
# ---------------------------------------------------------------------------

logger = init_logger("knotify_chat_resolver")

_DB_SECRET_NAME: str = os.environ.get("DB_SECRET_NAME", "")

# Module-level DynamoDB client (reused across warm invocations).
# Lazy-initialised in _get_dynamodb(); avoids the import cost on cold start
# until the first real request arrives.
_dynamodb_client = None


def _get_dynamodb():
    """Return a (possibly cached) boto3 DynamoDB client."""
    global _dynamodb_client
    if _dynamodb_client is None:
        import boto3

        _dynamodb_client = boto3.client("dynamodb")
    return _dynamodb_client


# ---------------------------------------------------------------------------
# AppSync resolver dispatcher
#
# The dispatcher is the single entry point for all (typeName, fieldName) pairs
# routed to this Lambda via the AppSync data source configuration.  Story 8.0
# ships an empty implementation that returns Unimplemented for everything.
# Subsequent stories extend this function by adding if-branches for the fields
# they implement.
#
# Return value conventions for AppSync Lambda resolvers:
#   Success  → any serialisable dict / list (AppSync merges it into the
#               GraphQL response data).
#   Error    → {"errorType": "<ErrorCode>", "message": "<human-readable>",
#               "reason": "<machine-code>"}  — AppSync surfaces these as
#               GraphQL errors with extensions.errorType and extensions.reason.
# ---------------------------------------------------------------------------


def _dispatch(event: dict) -> Any:
    """
    Route the AppSync resolver event to the appropriate sub-handler.

    Story 8.0: returns Unimplemented for all fields.
    Later stories add:
        if type_name == "Mutation" and field_name == "sendMessage":
            return _handle_send_message(event)
        ...

    Args:
        event: AppSync Lambda resolver event dict.

    Returns:
        A dict that AppSync merges into the GraphQL response.
    """
    type_name: str = event.get("typeName", "")
    field_name: str = event.get("fieldName", "")

    logger.info(
        "chat_resolver_dispatch",
        extra={"type_name": type_name, "field_name": field_name},
    )

    return {
        "errorType": "Unimplemented",
        "message": f"{type_name}.{field_name} not yet implemented",
    }


# ---------------------------------------------------------------------------
# Lambda entrypoint
# ---------------------------------------------------------------------------


@require_profile_complete_appsync
def handler(event: dict, context: object) -> Any:
    """
    knotify-chat-resolver Lambda entrypoint.

    Validates the caller's profile-completion claim (via
    @require_profile_complete_appsync) and dispatches to the appropriate
    sub-handler based on (typeName, fieldName).

    Args:
        event:   AppSync Lambda resolver event dict.
        context: Lambda context object (unused).

    Returns:
        AppSync resolver response — a serialisable dict or list on success,
        or an error dict with errorType/message on failure.
    """
    user_id: str = (
        event.get("identity", {}).get("sub", "<unknown>")
    )

    logger.info(
        "chat_resolver_invoked",
        extra={
            "user_id": user_id,
            "type_name": event.get("typeName"),
            "field_name": event.get("fieldName"),
        },
    )

    return _dispatch(event)
