"""
Unit tests for cognito_pre_token_generation handler.

Story 7.0b — the handler no longer connects to Aurora. Instead it reads
custom:profile_complete from the user's Cognito attributes (delivered in
event["request"]["userAttributes"]["custom:profile_complete"]) and copies
that value into both the ID token and access token claims.

All tests use no real DB or AWS credentials.

Behavior under test:
  - custom:profile_complete = "true"  in userAttributes → both token claims "true"
  - custom:profile_complete = "false" in userAttributes → both token claims "false"
  - custom:profile_complete absent from userAttributes  → both token claims "false"
    (fail-closed default: attribute not yet set means profile is incomplete)
  - Handler always returns the mutated event dict (never raises)
  - No DB connection is opened at any point (no _get_conn call)
"""

from __future__ import annotations

import importlib
import os
import sys
import uuid
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Import helper
# ---------------------------------------------------------------------------

_HANDLER_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
)


def _import_handler():
    """Import the handler module fresh each call to avoid import-level side effects."""
    spec = importlib.util.spec_from_file_location(
        "cognito_pre_token_generation_" + str(id(object())),
        os.path.join(_HANDLER_DIR, "handler.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Event builder
# ---------------------------------------------------------------------------

def _make_v2_event(sub: str, trigger_source: str = "TokenGeneration_Authentication",
                   profile_complete_attr: str | None = None,
                   gender_attr: str | None = None) -> dict:
    """
    Build a minimal V2 PreTokenGeneration event.

    profile_complete_attr — if given, sets custom:profile_complete in
    userAttributes; if None, the attribute is absent (simulates a user
    whose profile_complete attribute has never been set).
    gender_attr — if given, sets the standard Cognito `gender` attribute;
    if None, simulates a user with no gender attribute set (the broken
    state from before this hotfix shipped).
    """
    user_attributes = {
        "sub": sub,
        "email": f"{sub}@example.com",
        "email_verified": "true",
    }
    if profile_complete_attr is not None:
        user_attributes["custom:profile_complete"] = profile_complete_attr
    if gender_attr is not None:
        user_attributes["gender"] = gender_attr

    return {
        "version": "2",
        "triggerSource": trigger_source,
        "region": "eu-central-1",
        "userPoolId": "eu-central-1_TESTPOOL",
        "userName": sub,
        "callerContext": {
            "awsSdkVersion": "aws-sdk-unknown-unknown",
            "clientId": "test-client-id",
        },
        "request": {
            "userAttributes": user_attributes,
            "scopes": [],
        },
        "response": {},
    }


# ---------------------------------------------------------------------------
# Test 1: attribute = "true" → both claims "true"
# ---------------------------------------------------------------------------

def test_given_attribute_true_when_authentication_event_then_both_claims_are_true():
    """
    Given custom:profile_complete = "true" in Cognito userAttributes,
    when TokenGeneration_Authentication fires,
    then both idTokenGeneration and accessTokenGeneration claims are "true".
    """
    mod = _import_handler()
    sub = str(uuid.uuid4())
    event = _make_v2_event(sub, "TokenGeneration_Authentication", profile_complete_attr="true")

    with patch.dict(os.environ, {"DB_SECRET_NAME": "ignored"}):
        result = mod.handler(event, {})

    override = result["response"]["claimsAndScopeOverrideDetails"]
    assert override["idTokenGeneration"]["claimsToAddOrOverride"]["custom:profile_complete"] == "true"
    assert override["accessTokenGeneration"]["claimsToAddOrOverride"]["custom:profile_complete"] == "true"


# ---------------------------------------------------------------------------
# Test 2: attribute = "false" → both claims "false"
# ---------------------------------------------------------------------------

def test_given_attribute_false_when_authentication_event_then_both_claims_are_false():
    """
    Given custom:profile_complete = "false" in Cognito userAttributes,
    when TokenGeneration_Authentication fires,
    then both token claims are "false".
    """
    mod = _import_handler()
    sub = str(uuid.uuid4())
    event = _make_v2_event(sub, "TokenGeneration_Authentication", profile_complete_attr="false")

    with patch.dict(os.environ, {"DB_SECRET_NAME": "ignored"}):
        result = mod.handler(event, {})

    override = result["response"]["claimsAndScopeOverrideDetails"]
    assert override["idTokenGeneration"]["claimsToAddOrOverride"]["custom:profile_complete"] == "false"
    assert override["accessTokenGeneration"]["claimsToAddOrOverride"]["custom:profile_complete"] == "false"


# ---------------------------------------------------------------------------
# Test 3: attribute absent → both claims "false" (fail-closed)
# ---------------------------------------------------------------------------

def test_given_attribute_absent_when_handler_invoked_then_both_claims_default_to_false():
    """
    Given custom:profile_complete is absent from Cognito userAttributes,
    when the handler fires,
    then both token claims default to "false" (fail-closed).

    This is the state for users who signed up before story 7.0b shipped
    and whose profile_complete attribute has never been set.
    """
    mod = _import_handler()
    sub = str(uuid.uuid4())
    event = _make_v2_event(sub, "TokenGeneration_Authentication", profile_complete_attr=None)

    with patch.dict(os.environ, {"DB_SECRET_NAME": "ignored"}):
        result = mod.handler(event, {})

    override = result["response"]["claimsAndScopeOverrideDetails"]
    assert override["idTokenGeneration"]["claimsToAddOrOverride"]["custom:profile_complete"] == "false"
    assert override["accessTokenGeneration"]["claimsToAddOrOverride"]["custom:profile_complete"] == "false"


# ---------------------------------------------------------------------------
# Test 4: RefreshTokens trigger handled identically
# ---------------------------------------------------------------------------

def test_given_attribute_true_when_refresh_tokens_event_then_both_claims_are_true():
    """
    V2 TokenGeneration_RefreshTokens shares the same event shape and handler path.
    """
    mod = _import_handler()
    sub = str(uuid.uuid4())
    event = _make_v2_event(sub, "TokenGeneration_RefreshTokens", profile_complete_attr="true")

    with patch.dict(os.environ, {"DB_SECRET_NAME": "ignored"}):
        result = mod.handler(event, {})

    override = result["response"]["claimsAndScopeOverrideDetails"]
    assert override["idTokenGeneration"]["claimsToAddOrOverride"]["custom:profile_complete"] == "true"
    assert override["accessTokenGeneration"]["claimsToAddOrOverride"]["custom:profile_complete"] == "true"


# ---------------------------------------------------------------------------
# Test 5: No DB connection is ever opened (Aurora-free path)
# ---------------------------------------------------------------------------

def test_given_any_event_when_handler_invoked_then_no_db_connection_opened():
    """
    Story 7.0b: the pre-token-gen handler must NOT connect to Aurora.
    It reads the Cognito attribute directly from the event — no DB roundtrip.

    Verified by asserting that knotify_db.get_connection is never called.
    """
    mod = _import_handler()
    sub = str(uuid.uuid4())
    event = _make_v2_event(sub, "TokenGeneration_Authentication", profile_complete_attr="true")

    mock_get_connection = MagicMock(side_effect=Exception("should not be called"))

    with (
        patch.dict(os.environ, {"DB_SECRET_NAME": "ignored"}),
        patch.object(mod, "_get_conn", mock_get_connection),
    ):
        result = mod.handler(event, {})

    mock_get_connection.assert_not_called()
    # Handler must still succeed
    override = result["response"]["claimsAndScopeOverrideDetails"]
    assert override["accessTokenGeneration"]["claimsToAddOrOverride"]["custom:profile_complete"] == "true"


# ---------------------------------------------------------------------------
# Test 6: Handler always returns the original event dict (identity)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Test 7: gender = "Male" → custom:user_sex = "Male" on both tokens
# ---------------------------------------------------------------------------

def test_given_gender_male_when_handler_invoked_then_user_sex_claim_is_male():
    """
    The standard Cognito `gender` attribute "Male" must flow through as the
    custom:user_sex claim with the canonical "Male" value on both tokens.
    """
    mod = _import_handler()
    sub = str(uuid.uuid4())
    event = _make_v2_event(
        sub,
        "TokenGeneration_Authentication",
        profile_complete_attr="true",
        gender_attr="Male",
    )

    with patch.dict(os.environ, {"DB_SECRET_NAME": "ignored"}):
        result = mod.handler(event, {})

    override = result["response"]["claimsAndScopeOverrideDetails"]
    assert override["idTokenGeneration"]["claimsToAddOrOverride"]["custom:user_sex"] == "Male"
    assert override["accessTokenGeneration"]["claimsToAddOrOverride"]["custom:user_sex"] == "Male"


# ---------------------------------------------------------------------------
# Test 8: gender = "Female" → custom:user_sex = "Female" on both tokens
# ---------------------------------------------------------------------------

def test_given_gender_female_when_handler_invoked_then_user_sex_claim_is_female():
    mod = _import_handler()
    sub = str(uuid.uuid4())
    event = _make_v2_event(
        sub,
        "TokenGeneration_Authentication",
        profile_complete_attr="true",
        gender_attr="Female",
    )

    with patch.dict(os.environ, {"DB_SECRET_NAME": "ignored"}):
        result = mod.handler(event, {})

    override = result["response"]["claimsAndScopeOverrideDetails"]
    assert override["idTokenGeneration"]["claimsToAddOrOverride"]["custom:user_sex"] == "Female"
    assert override["accessTokenGeneration"]["claimsToAddOrOverride"]["custom:user_sex"] == "Female"


# ---------------------------------------------------------------------------
# Test 9: lowercase / single-letter forms fold to canonical
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("male", "Male"),
        ("MALE", "Male"),
        ("m", "Male"),
        ("M", "Male"),
        ("female", "Female"),
        ("F", "Female"),
        ("  Male  ", "Male"),
    ],
)
def test_given_gender_variants_when_handler_invoked_then_user_sex_claim_normalized(raw, expected):
    mod = _import_handler()
    sub = str(uuid.uuid4())
    event = _make_v2_event(
        sub,
        "TokenGeneration_Authentication",
        profile_complete_attr="true",
        gender_attr=raw,
    )

    with patch.dict(os.environ, {"DB_SECRET_NAME": "ignored"}):
        result = mod.handler(event, {})

    override = result["response"]["claimsAndScopeOverrideDetails"]
    assert override["idTokenGeneration"]["claimsToAddOrOverride"]["custom:user_sex"] == expected
    assert override["accessTokenGeneration"]["claimsToAddOrOverride"]["custom:user_sex"] == expected


# ---------------------------------------------------------------------------
# Test 10: gender absent → custom:user_sex claim OMITTED (not set to "")
# ---------------------------------------------------------------------------

def test_given_gender_absent_when_handler_invoked_then_user_sex_claim_omitted():
    """
    If the gender attribute is absent (existing pre-hotfix user, JWT not yet
    refreshed after backfill), the custom:user_sex claim must NOT appear in
    the override block. Emitting "" would poison the GUC and silently disable
    opposite-sex filtering — better to omit and let the consumer log the
    missing case.
    """
    mod = _import_handler()
    sub = str(uuid.uuid4())
    event = _make_v2_event(
        sub,
        "TokenGeneration_Authentication",
        profile_complete_attr="true",
        gender_attr=None,
    )

    with patch.dict(os.environ, {"DB_SECRET_NAME": "ignored"}):
        result = mod.handler(event, {})

    override = result["response"]["claimsAndScopeOverrideDetails"]
    assert "custom:user_sex" not in override["idTokenGeneration"]["claimsToAddOrOverride"]
    assert "custom:user_sex" not in override["accessTokenGeneration"]["claimsToAddOrOverride"]
    # And the profile_complete claim is still present.
    assert override["idTokenGeneration"]["claimsToAddOrOverride"]["custom:profile_complete"] == "true"


# ---------------------------------------------------------------------------
# Test 11: unrecognised gender value → claim OMITTED
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", ["X", "other", "Non-Binary", "true", "1"])
def test_given_unknown_gender_when_handler_invoked_then_user_sex_claim_omitted(raw):
    mod = _import_handler()
    sub = str(uuid.uuid4())
    event = _make_v2_event(
        sub,
        "TokenGeneration_Authentication",
        profile_complete_attr="true",
        gender_attr=raw,
    )

    with patch.dict(os.environ, {"DB_SECRET_NAME": "ignored"}):
        result = mod.handler(event, {})

    override = result["response"]["claimsAndScopeOverrideDetails"]
    assert "custom:user_sex" not in override["idTokenGeneration"]["claimsToAddOrOverride"]
    assert "custom:user_sex" not in override["accessTokenGeneration"]["claimsToAddOrOverride"]


# ---------------------------------------------------------------------------
# Test 12: handler always returns the event dict (identity)
# ---------------------------------------------------------------------------

def test_given_any_event_handler_returns_the_event_dict():
    """
    Cognito requires the PreTokenGeneration handler to return the mutated
    event dict. Verify the returned object is the SAME dict (identity).
    """
    mod = _import_handler()
    sub = str(uuid.uuid4())
    event = _make_v2_event(sub, "TokenGeneration_Authentication", profile_complete_attr="false")

    with patch.dict(os.environ, {"DB_SECRET_NAME": "ignored"}):
        result = mod.handler(event, {})

    assert result is event, "Handler must return the same event dict it received"
