terraform {
  required_version = ">= 1.9.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.20"
    }
  }
}

# ---------------------------------------------------------------------------
# Data sources
# ---------------------------------------------------------------------------

# Used to construct user_pool_endpoint without hardcoding the region.
# Phase 3 story 3.1 already resolved the deprecated .name → .region attribute;
# we reuse that pattern here.
data "aws_region" "current" {}

# ---------------------------------------------------------------------------
# Locals
#
# token_validity_* locals centralise the unit strings alongside the numeric
# validity variables so story 4.2's app client resources can reference both
# consistently. They are also the target of plan-mode test 8, which asserts
# the correct unit strings are in place before any app client resource exists.
# ---------------------------------------------------------------------------

locals {
  access_token_unit  = "hours"
  id_token_unit      = "hours"
  refresh_token_unit = "days"
}

# ---------------------------------------------------------------------------
# Cognito User Pool
#
# Schema attributes are IMMUTABLE after creation. All four profile attributes
# (given_name, family_name, gender, birthdate) are declared now for
# forward-compatibility with social IDP federation (Google, Apple) even though
# v1 uses email-only signup and leaves them NULL. Changing an attribute later
# requires a User Pool rebuild, forcing all existing users to re-sign-up.
# See story 4.1 notes and brainstorm M2/Md5 resolution.
#
# preferred_username is intentionally absent — see architecture.md §13 item 27.
# The user-supplied display handle is stored in users.username (Aurora) and
# set at profile completion (phase 6) with no Cognito coupling.
# ---------------------------------------------------------------------------

resource "aws_cognito_user_pool" "this" {
  name = var.name

  # Email is the sole sign-in identifier. Phone number sign-in is not supported.
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  # Password requirements per security policy.
  password_policy {
    minimum_length                   = 12
    require_uppercase                = true
    require_lowercase                = true
    require_numbers                  = true
    require_symbols                  = true
    temporary_password_validity_days = 7
  }

  # OPTIONAL: users may set up MFA but are not required to.
  # MFA enforcement is deferred to phase 11 hardening per architecture.md §13 #1.
  mfa_configuration = "OPTIONAL"

  # Email-only account recovery. SMS is explicitly excluded.
  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  # ---------------------------------------------------------------------------
  # Schema attributes
  #
  # Standard attributes (given_name, family_name, gender, birthdate) are
  # declared as optional (required=false) and mutable so they can be populated
  # post-signup via the profile completion flow (phase 6) or by social IDP
  # federation in a later phase. In v1, email-only signup leaves all four NULL.
  # ---------------------------------------------------------------------------

  schema {
    name                     = "given_name"
    attribute_data_type      = "String"
    mutable                  = true
    required                 = false
    developer_only_attribute = false
  }

  schema {
    name                     = "family_name"
    attribute_data_type      = "String"
    mutable                  = true
    required                 = false
    developer_only_attribute = false
  }

  schema {
    name                     = "gender"
    attribute_data_type      = "String"
    mutable                  = true
    required                 = false
    developer_only_attribute = false
  }

  schema {
    name                     = "birthdate"
    attribute_data_type      = "String"
    mutable                  = true
    required                 = false
    developer_only_attribute = false
  }

  # ---------------------------------------------------------------------------
  # Advanced Security Mode
  #
  # Wired to var.advanced_security_mode (default "AUDIT").
  # AUDIT is the minimum required by the V2 PreTokenGeneration trigger (story 4.4)
  # per AWS documentation. Brainstorm B2 resolution: bumped from original OFF plan.
  # Upgrade to ENFORCED in phase 11 hardening.
  # ---------------------------------------------------------------------------

  user_pool_add_ons {
    advanced_security_mode = var.advanced_security_mode
  }
}
