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

  # ---------------------------------------------------------------------------
  # Lambda triggers — story 4.3 (post_confirmation) and story 4.4
  # (pre_token_generation_config, added when 4.4 ships).
  #
  # M3 resolution: wiring lives in this block on the pool itself.
  # No standalone aws_cognito_user_pool_lambda_config resource is used —
  # that resource conflicts with the lambda_config block here and cannot
  # coexist with it.
  # ---------------------------------------------------------------------------

  lambda_config {
    post_confirmation = var.post_confirmation_lambda_arn

    # Story 4.4 — V2 PreTokenGeneration trigger.
    # lambda_version MUST be "V2_0"; the V1 pre_token_generation field is
    # explicitly forbidden (it cannot coexist with V2 and would silently
    # downgrade the trigger shape). V2 requires AUDIT or ENFORCED Advanced
    # Security Mode on the User Pool — that is set via var.advanced_security_mode
    # (default "AUDIT" per brainstorm B2).
    pre_token_generation_config {
      lambda_version = "V2_0"
      lambda_arn     = var.pre_token_generation_lambda_arn
    }
  }
}

# ---------------------------------------------------------------------------
# Lambda invoke permission — story 4.3
#
# Grants cognito-idp.amazonaws.com permission to invoke the post-confirmation
# Lambda. source_arn is scoped to this User Pool's ARN to prevent confused-
# deputy privilege escalation (any other User Pool cannot use this permission
# to invoke the function).
#
# This permission was intentionally deferred from phase 3 story 3.6 because
# the source_arn (User Pool ARN) was not yet known at phase 3 time.
# ---------------------------------------------------------------------------

resource "aws_lambda_permission" "cognito_post_confirmation_invoke" {
  statement_id  = "AllowCognitoInvokePostConfirmation"
  action        = "lambda:InvokeFunction"
  function_name = var.post_confirmation_lambda_arn
  principal     = "cognito-idp.amazonaws.com"
  source_arn    = aws_cognito_user_pool.this.arn
}

# ---------------------------------------------------------------------------
# Lambda invoke permission — story 4.4
#
# Grants cognito-idp.amazonaws.com permission to invoke the pre-token-generation
# Lambda. source_arn is scoped to this User Pool's ARN to prevent confused-
# deputy privilege escalation (any other User Pool cannot use this permission
# to invoke the function).
# ---------------------------------------------------------------------------

resource "aws_lambda_permission" "cognito_pre_token_generation_invoke" {
  statement_id  = "AllowCognitoInvokePreTokenGeneration"
  action        = "lambda:InvokeFunction"
  function_name = var.pre_token_generation_lambda_arn
  principal     = "cognito-idp.amazonaws.com"
  source_arn    = aws_cognito_user_pool.this.arn
}
