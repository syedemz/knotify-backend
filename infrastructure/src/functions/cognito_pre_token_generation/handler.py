"""
cognito_pre_token_generation — Lambda handler for Cognito PreTokenGeneration V2 trigger.

Triggered on every token issuance (sign-in and refresh). Reads the
custom:profile_complete Cognito user attribute from the V2 event's
userAttributes dict and copies it into both the ID token and the access token
as the custom:profile_complete claim.

Story 7.0b design (Finding 6 / re-brainstorm resolution):
  - The handler does NOT connect to Aurora.  The custom:profile_complete Cognito
    attribute is maintained by the profile PATCH handler via
    cognito-idp:AdminUpdateUserAttributes after a successful profile-completion
    flip in Aurora.  Reading the attribute here (from event.request.userAttributes)
    costs zero DB roundtrips on the auth path.
  - Default when the attribute is absent: "false" (fail-closed).  Users who
    signed up before story 7.0b shipped have no custom:profile_complete attribute
    yet.  They remain gated until their next successful PATCH /v1/profile/me
    call that completes the profile.

Story 4.4 design decisions (unchanged):
  - B1: claim embedded on BOTH idTokenGeneration and accessTokenGeneration so
    all API surfaces (REST, AppSync) can gate on profile completion without
    decoding the ID token.
  - B2: V2 trigger shape (pre_token_generation_config lambda_version = "V2_0")
    used throughout.  V2 requires AUDIT or ENFORCED Advanced Security Mode.
  - Handler never raises — an unhandled exception blocks Cognito login entirely.

Dependencies:
  - knotify_obs layer: init_logger
  (knotify_db is no longer needed; DB_SECRET_NAME env var is kept so Terraform
   modules do not require a simultaneous IaC change, but it is not used.)
"""

from __future__ import annotations

import os

from knotify_obs import init_logger

# ---------------------------------------------------------------------------
# Module-level singletons (cold-start optimization)
# ---------------------------------------------------------------------------

logger = init_logger("cognito_pre_token_generation")

# Kept for Terraform compatibility; not used by this handler.
_DB_SECRET_NAME: str = os.environ.get("DB_SECRET_NAME", "")

# Name of the Cognito custom attribute that carries the profile-completion flag.
_ATTR_NAME = "custom:profile_complete"

# Claim name written to both ID token and access token.
_CLAIM_NAME = "custom:profile_complete"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_conn():
    """
    Stub retained so unit tests can assert it is never called.

    Story 7.0b: the handler no longer connects to Aurora.  If a future story
    needs Aurora on the token-gen path, restore this function and re-add
    knotify_db to the import list.
    """
    import knotify_db  # noqa: F401 — only imported if this path is called
    return knotify_db.get_connection(_DB_SECRET_NAME)


def _read_profile_complete(event: dict) -> bool:
    """
    Return the profile-completion state from the V2 event's userAttributes.

    Reads custom:profile_complete from event["request"]["userAttributes"].
    Returns True only when the attribute value is exactly the string "true".
    Returns False (fail-closed) when the attribute is absent or any other value.

    No external calls are made.
    """
    user_attributes: dict = (
        event.get("request", {}).get("userAttributes", {})
    )
    attr_value = user_attributes.get(_ATTR_NAME)

    if attr_value is None:
        logger.info(
            "profile_complete_attribute_absent",
            extra={"defaulting_to": "false"},
        )

    return attr_value == "true"


def _build_claims_override(profile_complete: bool) -> dict:
    """
    Build the V2 claimsAndScopeOverrideDetails response dict.

    Sets custom:profile_complete on BOTH idTokenGeneration and
    accessTokenGeneration (B1 resolution).  The claim value is a string
    "true" or "false" — Cognito custom claims are always strings.
    """
    claim_value = "true" if profile_complete else "false"
    return {
        "claimsAndScopeOverrideDetails": {
            "idTokenGeneration": {
                "claimsToAddOrOverride": {
                    _CLAIM_NAME: claim_value,
                },
            },
            "accessTokenGeneration": {
                "claimsToAddOrOverride": {
                    _CLAIM_NAME: claim_value,
                },
            },
        }
    }


# ---------------------------------------------------------------------------
# Lambda entrypoint
# ---------------------------------------------------------------------------

def handler(event: dict, context: object) -> dict:
    """
    Cognito PreTokenGeneration V2 Lambda entrypoint.

    Copies custom:profile_complete from the user's Cognito attributes into
    both the ID token and the access token as a custom JWT claim.

    Always returns the mutated event — Cognito requires the trigger to return
    the event object (possibly with a modified response block).  Never raises:
    an unhandled exception in PreTokenGeneration blocks the user's login.

    Args:
        event:   Cognito V2 PreTokenGeneration event dict.
        context: Lambda context object (unused).

    Returns:
        The input event mutated with response.claimsAndScopeOverrideDetails set.
    """
    try:
        profile_complete = _read_profile_complete(event)
    except Exception as exc:
        # Defensive catch: _read_profile_complete has no I/O today, but
        # future changes must not accidentally break login.
        logger.error(
            "pre_token_generation_attribute_read_error",
            extra={
                "error": str(exc),
                "trigger_source": event.get("triggerSource"),
            },
        )
        profile_complete = False  # safe default — fail-closed

    event["response"].update(_build_claims_override(profile_complete))
    return event
