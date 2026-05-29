# Cognito module tests — all use command = plan (hermetic, no AWS credentials required)
# Tests are designed so `terraform test` exits 0 from infrastructure/modules/cognito/
#
# TDD note: this file was authored BEFORE main.tf/variables.tf/outputs.tf to drive
# the implementation shape via acceptance criteria.
#
# mock_provider overrides supply deterministic values for the data sources used
# to construct the user_pool_endpoint output:
#   data.aws_region.current.region → "eu-central-1"
#   data.aws_cognito_user_pool.this.id → supplied via override_resource
#
# Tests covered (acceptance criteria mapping):
#   Test 1  — password policy fields (AC 1)
#   Test 2  — username_attributes = ["email"] and auto_verified_attributes = ["email"] (AC 1)
#   Test 3  — mfa_configuration = "OPTIONAL" (AC 1)
#   Test 4  — account_recovery email-only, no SMS (AC 1)
#   Test 5  — schema attributes: given_name, family_name, gender, birthdate present + properties (AC 2)
#   Test 6  — preferred_username absent from schema (AC 3)
#   Test 7  — advanced_security_mode wired to variable, default "AUDIT" (AC 4)
#   Test 8  — token_validity_units: access=hours, id=hours, refresh=days (AC 5)
#   Test 9  — outputs user_pool_id, user_pool_arn, user_pool_endpoint resolve (AC 6)

mock_provider "aws" {
  mock_data "aws_region" {
    defaults = {
      region = "eu-central-1"
      name   = "eu-central-1"
    }
  }
}

# ---------------------------------------------------------------------------
# Test 1: Password policy meets all requirements
#
# Satisfies AC 1: password_policy minimum_length=12, require_uppercase=true,
# require_lowercase=true, require_numbers=true, require_symbols=true.
# ---------------------------------------------------------------------------
run "password_policy_meets_requirements" {
  command = plan

  variables {
    name        = "knotify-test-user-pool"
    environment = "test"
  }

  assert {
    condition     = aws_cognito_user_pool.this.password_policy[0].minimum_length == 12
    error_message = "password_policy minimum_length must be 12"
  }

  assert {
    condition     = aws_cognito_user_pool.this.password_policy[0].require_uppercase == true
    error_message = "password_policy require_uppercase must be true"
  }

  assert {
    condition     = aws_cognito_user_pool.this.password_policy[0].require_lowercase == true
    error_message = "password_policy require_lowercase must be true"
  }

  assert {
    condition     = aws_cognito_user_pool.this.password_policy[0].require_numbers == true
    error_message = "password_policy require_numbers must be true"
  }

  assert {
    condition     = aws_cognito_user_pool.this.password_policy[0].require_symbols == true
    error_message = "password_policy require_symbols must be true"
  }
}

# ---------------------------------------------------------------------------
# Test 2: username_attributes and auto_verified_attributes are email-only
#
# Satisfies AC 1: username_attributes=["email"],
# auto_verified_attributes=["email"].
# ---------------------------------------------------------------------------
run "email_as_username_and_auto_verified" {
  command = plan

  variables {
    name        = "knotify-test-user-pool"
    environment = "test"
  }

  assert {
    condition     = contains(tolist(aws_cognito_user_pool.this.username_attributes), "email") && length(aws_cognito_user_pool.this.username_attributes) == 1
    error_message = "username_attributes must be [email] only"
  }

  assert {
    condition     = contains(tolist(aws_cognito_user_pool.this.auto_verified_attributes), "email") && length(aws_cognito_user_pool.this.auto_verified_attributes) == 1
    error_message = "auto_verified_attributes must be [email] only"
  }
}

# ---------------------------------------------------------------------------
# Test 3: mfa_configuration is "OPTIONAL"
#
# Satisfies AC 1: mfa_configuration="OPTIONAL".
# MFA enforcement is deferred to phase 11 hardening; OPTIONAL means users
# can opt in but are not forced to enroll.
# ---------------------------------------------------------------------------
run "mfa_configuration_is_optional" {
  command = plan

  variables {
    name        = "knotify-test-user-pool"
    environment = "test"
  }

  assert {
    condition     = aws_cognito_user_pool.this.mfa_configuration == "OPTIONAL"
    error_message = "mfa_configuration must be OPTIONAL"
  }
}

# ---------------------------------------------------------------------------
# Test 4: account_recovery uses email only — no SMS mechanism
#
# Satisfies AC 1: account_recovery_setting with email as the only recovery
# mechanism. SMS is explicitly excluded.
# ---------------------------------------------------------------------------
run "account_recovery_email_only_no_sms" {
  command = plan

  variables {
    name        = "knotify-test-user-pool"
    environment = "test"
  }

  assert {
    condition     = length(aws_cognito_user_pool.this.account_recovery_setting[0].recovery_mechanism) == 1
    error_message = "account_recovery_setting must have exactly one recovery mechanism"
  }

  assert {
    condition = anytrue([
      for m in aws_cognito_user_pool.this.account_recovery_setting[0].recovery_mechanism : m.name == "verified_email"
    ])
    error_message = "account_recovery mechanism must be verified_email"
  }

  assert {
    condition = anytrue([
      for m in aws_cognito_user_pool.this.account_recovery_setting[0].recovery_mechanism : m.priority == 1 && m.name == "verified_email"
    ])
    error_message = "verified_email recovery mechanism must have priority 1"
  }
}

# ---------------------------------------------------------------------------
# Test 5: Schema attributes — given_name, family_name, gender, birthdate
#         all present with correct mutability and required=false
#
# Satisfies AC 2: all four attrs declared for forward-compatibility with
# social IDP federation (schema is immutable post-creation).
# All are mutable=true, required=false (optional in v1 email-only signup).
# ---------------------------------------------------------------------------
run "schema_attributes_present_with_correct_properties" {
  command = plan

  variables {
    name        = "knotify-test-user-pool"
    environment = "test"
  }

  # given_name
  assert {
    condition = anytrue([
      for s in aws_cognito_user_pool.this.schema : s.name == "given_name"
    ])
    error_message = "schema must include given_name attribute"
  }

  assert {
    condition = alltrue([
      for s in aws_cognito_user_pool.this.schema : s.mutable == true if s.name == "given_name"
    ])
    error_message = "given_name must be mutable=true"
  }

  assert {
    condition = alltrue([
      for s in aws_cognito_user_pool.this.schema : s.required == false if s.name == "given_name"
    ])
    error_message = "given_name must be required=false"
  }

  # family_name
  assert {
    condition = anytrue([
      for s in aws_cognito_user_pool.this.schema : s.name == "family_name"
    ])
    error_message = "schema must include family_name attribute"
  }

  assert {
    condition = alltrue([
      for s in aws_cognito_user_pool.this.schema : s.mutable == true if s.name == "family_name"
    ])
    error_message = "family_name must be mutable=true"
  }

  assert {
    condition = alltrue([
      for s in aws_cognito_user_pool.this.schema : s.required == false if s.name == "family_name"
    ])
    error_message = "family_name must be required=false"
  }

  # gender
  assert {
    condition = anytrue([
      for s in aws_cognito_user_pool.this.schema : s.name == "gender"
    ])
    error_message = "schema must include gender attribute"
  }

  assert {
    condition = alltrue([
      for s in aws_cognito_user_pool.this.schema : s.mutable == true if s.name == "gender"
    ])
    error_message = "gender must be mutable=true"
  }

  assert {
    condition = alltrue([
      for s in aws_cognito_user_pool.this.schema : s.required == false if s.name == "gender"
    ])
    error_message = "gender must be required=false"
  }

  # birthdate
  assert {
    condition = anytrue([
      for s in aws_cognito_user_pool.this.schema : s.name == "birthdate"
    ])
    error_message = "schema must include birthdate attribute"
  }

  assert {
    condition = alltrue([
      for s in aws_cognito_user_pool.this.schema : s.mutable == true if s.name == "birthdate"
    ])
    error_message = "birthdate must be mutable=true"
  }

  assert {
    condition = alltrue([
      for s in aws_cognito_user_pool.this.schema : s.required == false if s.name == "birthdate"
    ])
    error_message = "birthdate must be required=false"
  }
}

# ---------------------------------------------------------------------------
# Test 6: preferred_username is NOT in the schema
#
# Satisfies AC 3: preferred_username must not appear as a schema attribute,
# sign-in alias, or auto-verified attribute. The display handle is stored
# in users.username (Aurora) and is set at profile completion (phase 6).
# ---------------------------------------------------------------------------
run "preferred_username_absent_from_schema" {
  command = plan

  variables {
    name        = "knotify-test-user-pool"
    environment = "test"
  }

  assert {
    condition = !anytrue([
      for s in aws_cognito_user_pool.this.schema : s.name == "preferred_username"
    ])
    error_message = "preferred_username must NOT appear in the schema"
  }
}

# ---------------------------------------------------------------------------
# Test 7: advanced_security_mode flows through from variable, default "AUDIT"
#
# Satisfies AC 4: user_pool_add_ons.advanced_security_mode wired to the
# variable from story 4.5; default is "AUDIT" (required by V2 PreTokenGeneration
# per AWS docs — brainstorm B2 resolution).
# ---------------------------------------------------------------------------
run "advanced_security_mode_default_is_audit" {
  command = plan

  variables {
    name        = "knotify-test-user-pool"
    environment = "test"
    # advanced_security_mode intentionally omitted — testing default
  }

  assert {
    condition     = aws_cognito_user_pool.this.user_pool_add_ons[0].advanced_security_mode == "AUDIT"
    error_message = "advanced_security_mode must default to AUDIT"
  }
}

run "advanced_security_mode_override_accepted" {
  command = plan

  variables {
    name                   = "knotify-test-user-pool"
    environment            = "test"
    advanced_security_mode = "ENFORCED"
  }

  assert {
    condition     = aws_cognito_user_pool.this.user_pool_add_ons[0].advanced_security_mode == "ENFORCED"
    error_message = "advanced_security_mode override ENFORCED must be accepted"
  }
}

# ---------------------------------------------------------------------------
# Test 8: Token validity defaults — access=1h, id=1h, refresh=30d
#
# Satisfies AC 5: id_token=1h, access_token=1h, refresh_token=30d.
# aws_cognito_user_pool does not carry a token_validity_units block — that
# block lives on aws_cognito_user_pool_client (added in story 4.2). The module
# declares input variables with the correct defaults and local unit strings
# that story 4.2 will consume. We assert the locals have the correct unit
# strings and that the input variables default to the required numeric values.
# ---------------------------------------------------------------------------
run "token_validity_locals_and_variable_defaults" {
  command = plan

  variables {
    name        = "knotify-test-user-pool"
    environment = "test"
    # access_token_validity, id_token_validity, refresh_token_validity
    # intentionally omitted — testing defaults
  }

  assert {
    condition     = local.access_token_unit == "hours"
    error_message = "access_token unit local must be hours"
  }

  assert {
    condition     = local.id_token_unit == "hours"
    error_message = "id_token unit local must be hours"
  }

  assert {
    condition     = local.refresh_token_unit == "days"
    error_message = "refresh_token unit local must be days"
  }

  assert {
    condition     = var.access_token_validity == 1
    error_message = "access_token_validity variable must default to 1 (hour)"
  }

  assert {
    condition     = var.id_token_validity == 1
    error_message = "id_token_validity variable must default to 1 (hour)"
  }

  assert {
    condition     = var.refresh_token_validity == 30
    error_message = "refresh_token_validity variable must default to 30 (days)"
  }
}

# ---------------------------------------------------------------------------
# Test 9: Outputs — user_pool_id, user_pool_arn, user_pool_endpoint resolve
#
# Satisfies AC 6: all three outputs must be present and correctly wired.
# user_pool_id and user_pool_arn are computed (unknown at plan time), so we
# override aws_cognito_user_pool.this to supply deterministic values and then
# assert the outputs equal those values.
# user_pool_endpoint must be the constructed issuer URL containing the region
# and user pool id.
# ---------------------------------------------------------------------------
run "outputs_user_pool_id_arn_endpoint_resolve" {
  command = plan

  variables {
    name        = "knotify-test-user-pool"
    environment = "test"
  }

  override_resource {
    target = aws_cognito_user_pool.this
    values = {
      id  = "eu-central-1_TestPoolId"
      arn = "arn:aws:cognito-idp:eu-central-1:123456789012:userpool/eu-central-1_TestPoolId"
    }
    override_during = plan
  }

  assert {
    condition     = output.user_pool_id == "eu-central-1_TestPoolId"
    error_message = "user_pool_id output must be wired to aws_cognito_user_pool.this.id"
  }

  assert {
    condition     = output.user_pool_arn == "arn:aws:cognito-idp:eu-central-1:123456789012:userpool/eu-central-1_TestPoolId"
    error_message = "user_pool_arn output must be wired to aws_cognito_user_pool.this.arn"
  }

  assert {
    condition     = output.user_pool_endpoint == "https://cognito-idp.eu-central-1.amazonaws.com/eu-central-1_TestPoolId"
    error_message = "user_pool_endpoint must be https://cognito-idp.<region>.amazonaws.com/<user_pool_id>"
  }
}

# ---------------------------------------------------------------------------
# Story 4.2 tests
#
# Tests 10–13 cover the two app client resources added in story 4.2.
# All use command = plan (hermetic — no AWS credentials required).
#
# Tests 10–11: dev environment — both clients created, correct auth flows,
#              outputs populated.
# Tests 12–13: prod environment — only prod client created (count=0 for
#              integration-test), integration_test_app_client_id == "".
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Test 10: dev env — production app client has SRP-only auth flows
#
# Satisfies AC 1: generate_secret=false, explicit_auth_flows contains
# ALLOW_USER_SRP_AUTH and ALLOW_REFRESH_TOKEN_AUTH, no ADMIN_USER_PASSWORD_AUTH,
# prevent_user_existence_errors="ENABLED".
# Also covers token validity and units (AC 2).
# ---------------------------------------------------------------------------
run "dev_prod_app_client_srp_only_flows_and_token_validity" {
  command = plan

  variables {
    name        = "knotify-dev-user-pool"
    environment = "dev"
  }

  # generate_secret must be false (public client)
  assert {
    condition     = aws_cognito_user_pool_client.app.generate_secret == false
    error_message = "production app client generate_secret must be false"
  }

  # SRP auth flow present
  assert {
    condition     = contains(aws_cognito_user_pool_client.app.explicit_auth_flows, "ALLOW_USER_SRP_AUTH")
    error_message = "production app client must include ALLOW_USER_SRP_AUTH"
  }

  # Refresh token flow present
  assert {
    condition     = contains(aws_cognito_user_pool_client.app.explicit_auth_flows, "ALLOW_REFRESH_TOKEN_AUTH")
    error_message = "production app client must include ALLOW_REFRESH_TOKEN_AUTH"
  }

  # ADMIN_USER_PASSWORD_AUTH must NOT be present on production client
  assert {
    condition     = !contains(aws_cognito_user_pool_client.app.explicit_auth_flows, "ALLOW_ADMIN_USER_PASSWORD_AUTH")
    error_message = "production app client must NOT include ALLOW_ADMIN_USER_PASSWORD_AUTH"
  }

  # USER_PASSWORD_AUTH must NOT be present on production client
  assert {
    condition     = !contains(aws_cognito_user_pool_client.app.explicit_auth_flows, "ALLOW_USER_PASSWORD_AUTH")
    error_message = "production app client must NOT include ALLOW_USER_PASSWORD_AUTH"
  }

  # prevent_user_existence_errors must be ENABLED
  assert {
    condition     = aws_cognito_user_pool_client.app.prevent_user_existence_errors == "ENABLED"
    error_message = "production app client prevent_user_existence_errors must be ENABLED"
  }

  # refresh_token_validity must be 30
  assert {
    condition     = aws_cognito_user_pool_client.app.refresh_token_validity == 30
    error_message = "production app client refresh_token_validity must be 30"
  }

  # access_token_validity must be 60
  assert {
    condition     = aws_cognito_user_pool_client.app.access_token_validity == 60
    error_message = "production app client access_token_validity must be 60"
  }

  # id_token_validity must be 60
  assert {
    condition     = aws_cognito_user_pool_client.app.id_token_validity == 60
    error_message = "production app client id_token_validity must be 60"
  }

  # token_validity_units: access_token=minutes
  assert {
    condition     = aws_cognito_user_pool_client.app.token_validity_units[0].access_token == "minutes"
    error_message = "production app client access_token unit must be minutes"
  }

  # token_validity_units: id_token=minutes
  assert {
    condition     = aws_cognito_user_pool_client.app.token_validity_units[0].id_token == "minutes"
    error_message = "production app client id_token unit must be minutes"
  }

  # token_validity_units: refresh_token=days
  assert {
    condition     = aws_cognito_user_pool_client.app.token_validity_units[0].refresh_token == "days"
    error_message = "production app client refresh_token unit must be days"
  }
}

# ---------------------------------------------------------------------------
# Test 11: dev env — integration-test app client created with correct flows
#          and outputs are correctly populated
#
# Satisfies AC 3: second client exists when environment == "dev",
# has ALLOW_ADMIN_USER_PASSWORD_AUTH, no SRP, generate_secret=false.
# Satisfies AC 4: count = 1 in dev.
# Satisfies AC 5: app_client_id non-empty, integration_test_app_client_id non-empty.
# ---------------------------------------------------------------------------
run "dev_integration_test_client_exists_with_admin_password_auth" {
  command = plan

  variables {
    name        = "knotify-dev-user-pool"
    environment = "dev"
  }

  override_resource {
    target = aws_cognito_user_pool_client.app
    values = {
      id = "dev-app-client-id"
    }
    override_during = plan
  }

  override_resource {
    target = aws_cognito_user_pool_client.integration_test[0]
    values = {
      id = "dev-integration-test-client-id"
    }
    override_during = plan
  }

  # Count must be 1 in dev
  assert {
    condition     = length(aws_cognito_user_pool_client.integration_test) == 1
    error_message = "integration_test client must be created (count=1) in dev environment"
  }

  # generate_secret must be false
  assert {
    condition     = aws_cognito_user_pool_client.integration_test[0].generate_secret == false
    error_message = "integration_test client generate_secret must be false"
  }

  # ADMIN_USER_PASSWORD_AUTH must be present
  assert {
    condition     = contains(aws_cognito_user_pool_client.integration_test[0].explicit_auth_flows, "ALLOW_ADMIN_USER_PASSWORD_AUTH")
    error_message = "integration_test client must include ALLOW_ADMIN_USER_PASSWORD_AUTH"
  }

  # ALLOW_REFRESH_TOKEN_AUTH must be present
  assert {
    condition     = contains(aws_cognito_user_pool_client.integration_test[0].explicit_auth_flows, "ALLOW_REFRESH_TOKEN_AUTH")
    error_message = "integration_test client must include ALLOW_REFRESH_TOKEN_AUTH"
  }

  # SRP must NOT be present on integration-test client
  assert {
    condition     = !contains(aws_cognito_user_pool_client.integration_test[0].explicit_auth_flows, "ALLOW_USER_SRP_AUTH")
    error_message = "integration_test client must NOT include ALLOW_USER_SRP_AUTH"
  }

  # app_client_id output must be non-empty (references prod client)
  assert {
    condition     = output.app_client_id != ""
    error_message = "app_client_id output must be non-empty in dev"
  }

  # integration_test_app_client_id output must be non-empty in dev
  assert {
    condition     = output.integration_test_app_client_id != ""
    error_message = "integration_test_app_client_id output must be non-empty in dev"
  }
}

# ---------------------------------------------------------------------------
# Test 12: prod env — integration-test client NOT created (count=0)
#          and integration_test_app_client_id output is empty string
#
# Satisfies AC 4: count = 0 in prod.
# Satisfies AC 5: integration_test_app_client_id == "" in prod.
# ---------------------------------------------------------------------------
run "prod_integration_test_client_absent_output_empty" {
  command = plan

  variables {
    name        = "knotify-prod-user-pool"
    environment = "prod"
  }

  override_resource {
    target = aws_cognito_user_pool_client.app
    values = {
      id = "prod-app-client-id"
    }
    override_during = plan
  }

  # integration_test resource must not exist in prod
  assert {
    condition     = length(aws_cognito_user_pool_client.integration_test) == 0
    error_message = "integration_test client must NOT be created (count=0) in prod environment"
  }

  # app_client_id output must be non-empty in prod
  assert {
    condition     = output.app_client_id != ""
    error_message = "app_client_id output must be non-empty in prod"
  }

  # integration_test_app_client_id output must be empty string in prod
  assert {
    condition     = output.integration_test_app_client_id == ""
    error_message = "integration_test_app_client_id output must be empty string in prod"
  }
}
