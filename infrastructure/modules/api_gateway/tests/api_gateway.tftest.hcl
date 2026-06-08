# API Gateway module tests — all use command = plan (hermetic, no AWS credentials required)
# Tests are designed so `terraform test` exits 0 from infrastructure/modules/api_gateway/
#
# TDD note: this file was authored BEFORE main.tf/variables.tf/outputs.tf to drive
# the implementation shape via acceptance criteria.
#
# Tests covered (acceptance criteria mapping):
#   Test 1  — protocol_type=HTTP (AC 1)
#   Test 2  — throttling burst_limit and rate_limit pass through from variables (AC 1)
#   Test 3  — throttling dev defaults: burst=10, rate=25 (AC 1)
#   Test 4  — authorizer type=JWT (AC 2)
#   Test 5  — authorizer issuer URL wired to cognito_user_pool_endpoint variable (AC 2)
#   Test 6  — audience with two client ids: compact keeps both (AC 2)
#   Test 7  — audience with empty integration-test id: compact drops it, only prod client remains (AC 2)
#   Test 8  — log group retention_in_days=7 (AC 3)
#   Test 9  — access_log_settings format contains all seven $context.* fields (AC 3)
#   Test 10 — outputs api_id, api_arn, execute_api_endpoint, authorizer_id, default_stage_arn,
#             access_log_group_name all resolve (AC 4)

mock_provider "aws" {}

# ---------------------------------------------------------------------------
# File-level variable defaults
#
# A sentinel Cognito User Pool endpoint URL is supplied so every run block
# that does not exercise issuer-wiring does not need to set it individually.
# The integration-test client id is supplied as non-empty here; tests that
# exercise the compact-drops-empty-string behaviour set it to "" inline.
# ---------------------------------------------------------------------------
variables {
  cognito_user_pool_endpoint  = "https://cognito-idp.eu-central-1.amazonaws.com/eu-central-1_SentinelPoolId"
  cognito_audience_client_ids = ["sentinel-app-client-id", "sentinel-integration-test-client-id"]
  name                        = "knotify-test-api"
}

# ---------------------------------------------------------------------------
# Test 1: protocol_type is HTTP
#
# Satisfies AC 1: aws_apigatewayv2_api must be created with protocol_type="HTTP".
# ---------------------------------------------------------------------------
run "api_protocol_type_is_http" {
  command = plan

  variables {
    name = "knotify-test-api"
  }

  assert {
    condition     = aws_apigatewayv2_api.this.protocol_type == "HTTP"
    error_message = "aws_apigatewayv2_api protocol_type must be HTTP"
  }
}

# ---------------------------------------------------------------------------
# Test 2: throttling burst_limit and rate_limit pass through from variables
#
# Satisfies AC 1: throttling values are configurable via variables and wired
# to the default stage default_route_settings.
# ---------------------------------------------------------------------------
run "throttling_values_pass_through_from_variables" {
  command = plan

  variables {
    name                   = "knotify-test-api"
    throttling_burst_limit = 200
    throttling_rate_limit  = 500
  }

  assert {
    condition     = aws_apigatewayv2_stage.default.default_route_settings[0].throttling_burst_limit == 200
    error_message = "throttling_burst_limit must be wired to default_route_settings"
  }

  assert {
    condition     = aws_apigatewayv2_stage.default.default_route_settings[0].throttling_rate_limit == 500
    error_message = "throttling_rate_limit must be wired to default_route_settings"
  }
}

# ---------------------------------------------------------------------------
# Test 3: throttling dev defaults are burst=10, rate=25
#
# Satisfies AC 1: variable defaults match the brainstorm Mn4 adjustment for dev
# (10/25 instead of the original higher defaults).
# ---------------------------------------------------------------------------
run "throttling_dev_defaults_burst_10_rate_25" {
  command = plan

  variables {
    name = "knotify-test-api"
    # throttling_burst_limit and throttling_rate_limit intentionally omitted
  }

  assert {
    condition     = aws_apigatewayv2_stage.default.default_route_settings[0].throttling_burst_limit == 10
    error_message = "throttling_burst_limit default must be 10"
  }

  assert {
    condition     = aws_apigatewayv2_stage.default.default_route_settings[0].throttling_rate_limit == 25
    error_message = "throttling_rate_limit default must be 25"
  }
}

# ---------------------------------------------------------------------------
# Test 4: authorizer type is JWT
#
# Satisfies AC 2: aws_apigatewayv2_authorizer must have authorizer_type="JWT".
# ---------------------------------------------------------------------------
run "authorizer_type_is_jwt" {
  command = plan

  variables {
    name = "knotify-test-api"
  }

  assert {
    condition     = aws_apigatewayv2_authorizer.cognito_jwt.authorizer_type == "JWT"
    error_message = "authorizer_type must be JWT"
  }
}

# ---------------------------------------------------------------------------
# Test 5: authorizer issuer URL is wired to the cognito_user_pool_endpoint variable
#
# Satisfies AC 2: jwt_configuration.issuer must equal var.cognito_user_pool_endpoint.
# ---------------------------------------------------------------------------
run "authorizer_issuer_equals_cognito_endpoint_variable" {
  command = plan

  variables {
    name                       = "knotify-test-api"
    cognito_user_pool_endpoint = "https://cognito-idp.eu-central-1.amazonaws.com/eu-central-1_TestPoolId"
  }

  assert {
    condition     = aws_apigatewayv2_authorizer.cognito_jwt.jwt_configuration[0].issuer == "https://cognito-idp.eu-central-1.amazonaws.com/eu-central-1_TestPoolId"
    error_message = "authorizer jwt_configuration.issuer must equal var.cognito_user_pool_endpoint"
  }
}

# ---------------------------------------------------------------------------
# Test 6: audience with two client ids — compact keeps both
#
# Satisfies AC 2 (brainstorm M1): when both the production app client id and the
# integration-test app client id are non-empty strings, compact keeps both so
# integration tests that mint tokens via the dev-only ADMIN_USER_PASSWORD_AUTH
# client can still pass JWT authorizer validation.
# ---------------------------------------------------------------------------
run "audience_compact_keeps_both_ids_when_both_non_empty" {
  command = plan

  variables {
    name                        = "knotify-test-api"
    cognito_audience_client_ids = ["prod-app-client-id", "dev-integration-test-client-id"]
  }

  assert {
    condition     = length(aws_apigatewayv2_authorizer.cognito_jwt.jwt_configuration[0].audience) == 2
    error_message = "audience must contain 2 entries when both client ids are non-empty"
  }

  assert {
    condition     = contains(tolist(aws_apigatewayv2_authorizer.cognito_jwt.jwt_configuration[0].audience), "prod-app-client-id")
    error_message = "audience must contain the production app client id"
  }

  assert {
    condition     = contains(tolist(aws_apigatewayv2_authorizer.cognito_jwt.jwt_configuration[0].audience), "dev-integration-test-client-id")
    error_message = "audience must contain the integration-test client id"
  }
}

# ---------------------------------------------------------------------------
# Test 7: audience with empty integration-test id — compact drops it
#
# Satisfies AC 2 (brainstorm M1): in prod, the integration-test client output is
# "" (the cognito module's try() returns "" when count=0). compact() must drop
# the empty string so the prod JWT authorizer only accepts the production client.
# ---------------------------------------------------------------------------
run "audience_compact_drops_empty_integration_test_id" {
  command = plan

  variables {
    name                        = "knotify-test-api"
    cognito_audience_client_ids = ["prod-app-client-id-only"]
  }

  assert {
    condition     = length(aws_apigatewayv2_authorizer.cognito_jwt.jwt_configuration[0].audience) == 1
    error_message = "audience must contain exactly 1 entry when integration-test id is absent"
  }

  assert {
    condition     = contains(tolist(aws_apigatewayv2_authorizer.cognito_jwt.jwt_configuration[0].audience), "prod-app-client-id-only")
    error_message = "audience must contain only the production app client id"
  }
}

# ---------------------------------------------------------------------------
# Test 8: CloudWatch log group retention is 7 days
#
# Satisfies AC 3: aws_cloudwatch_log_group must have retention_in_days=7,
# matching the project-wide log retention convention from architecture.md §10.6.
# The retention is overridable but defaults to 7.
# ---------------------------------------------------------------------------
run "access_log_group_retention_defaults_to_7_days" {
  command = plan

  variables {
    name = "knotify-test-api"
    # access_log_retention_days intentionally omitted — testing default
  }

  assert {
    condition     = aws_cloudwatch_log_group.access_logs.retention_in_days == 7
    error_message = "access log group retention_in_days must default to 7"
  }
}

# ---------------------------------------------------------------------------
# Test 9: access_log_settings.format contains all seven $context.* fields
#
# Satisfies AC 3 (brainstorm Tb2): the format must be a JSON string with the
# seven $context.* template fields as values. The format is produced by
# jsonencode() in HCL so the $context.* placeholders are preserved verbatim
# (they're evaluated at request time by API Gateway, not by Terraform).
#
# Fields required: requestId, status, routeKey, integrationLatency,
# authLatency (-> $context.authorizer.latency), sourceIp, userAgent.
#
# Strategy: override the log group ARN with a deterministic value so
# destination_arn is known at plan time. Then assert the format attribute
# of the stage directly.
# ---------------------------------------------------------------------------
run "access_log_format_contains_all_seven_context_fields" {
  command = plan

  variables {
    name = "knotify-test-api"
  }

  override_resource {
    target = aws_cloudwatch_log_group.access_logs
    values = {
      arn = "arn:aws:logs:eu-central-1:123456789012:log-group:/knotify/test-api/access-logs"
    }
    override_during = plan
  }

  assert {
    condition     = strcontains(aws_apigatewayv2_stage.default.access_log_settings[0].format, "$context.requestId")
    error_message = "access log format must contain $context.requestId"
  }

  assert {
    condition     = strcontains(aws_apigatewayv2_stage.default.access_log_settings[0].format, "$context.status")
    error_message = "access log format must contain $context.status"
  }

  assert {
    condition     = strcontains(aws_apigatewayv2_stage.default.access_log_settings[0].format, "$context.routeKey")
    error_message = "access log format must contain $context.routeKey"
  }

  assert {
    condition     = strcontains(aws_apigatewayv2_stage.default.access_log_settings[0].format, "$context.integrationLatency")
    error_message = "access log format must contain $context.integrationLatency"
  }

  assert {
    condition     = strcontains(aws_apigatewayv2_stage.default.access_log_settings[0].format, "$context.authorizer.latency")
    error_message = "access log format must contain $context.authorizer.latency (authLatency field)"
  }

  assert {
    condition     = strcontains(aws_apigatewayv2_stage.default.access_log_settings[0].format, "$context.identity.sourceIp")
    error_message = "access log format must contain $context.identity.sourceIp"
  }

  assert {
    condition     = strcontains(aws_apigatewayv2_stage.default.access_log_settings[0].format, "$context.identity.userAgent")
    error_message = "access log format must contain $context.identity.userAgent"
  }
}

# ---------------------------------------------------------------------------
# Test 10: All six required outputs resolve
#
# Satisfies AC 4: api_id, api_arn, execute_api_endpoint, authorizer_id,
# default_stage_arn, access_log_group_name must all be produced by the module.
# Resources are overridden to supply deterministic computed-attribute values
# so the outputs can be evaluated at plan time.
# ---------------------------------------------------------------------------
run "all_six_outputs_resolve" {
  command = plan

  variables {
    name = "knotify-test-api"
  }

  override_resource {
    target = aws_apigatewayv2_api.this
    values = {
      id           = "test-api-id-abc123"
      arn          = "arn:aws:apigateway:eu-central-1::/apis/test-api-id-abc123"
      api_endpoint = "https://test-api-id-abc123.execute-api.eu-central-1.amazonaws.com"
    }
    override_during = plan
  }

  override_resource {
    target = aws_apigatewayv2_authorizer.cognito_jwt
    values = {
      id = "auth-id-xyz789"
    }
    override_during = plan
  }

  override_resource {
    target = aws_apigatewayv2_stage.default
    values = {
      arn = "arn:aws:apigateway:eu-central-1::/apis/test-api-id-abc123/stages/$default"
    }
    override_during = plan
  }

  override_resource {
    target = aws_cloudwatch_log_group.access_logs
    values = {
      # The log group name is computed as /knotify/<var.name>/access-logs.
      # With var.name="knotify-test-api" the actual name is /knotify/knotify-test-api/access-logs.
      name = "/knotify/knotify-test-api/access-logs"
      arn  = "arn:aws:logs:eu-central-1:123456789012:log-group:/knotify/knotify-test-api/access-logs"
    }
    override_during = plan
  }

  assert {
    condition     = output.api_id == "test-api-id-abc123"
    error_message = "api_id output must be wired to aws_apigatewayv2_api.this.id"
  }

  assert {
    condition     = output.api_arn == "arn:aws:apigateway:eu-central-1::/apis/test-api-id-abc123"
    error_message = "api_arn output must be wired to aws_apigatewayv2_api.this.arn"
  }

  assert {
    condition     = output.execute_api_endpoint == "https://test-api-id-abc123.execute-api.eu-central-1.amazonaws.com"
    error_message = "execute_api_endpoint output must be wired to aws_apigatewayv2_api.this.api_endpoint"
  }

  assert {
    condition     = output.authorizer_id == "auth-id-xyz789"
    error_message = "authorizer_id output must be wired to aws_apigatewayv2_authorizer.cognito_jwt.id"
  }

  assert {
    condition     = output.default_stage_arn == "arn:aws:apigateway:eu-central-1::/apis/test-api-id-abc123/stages/$default"
    error_message = "default_stage_arn output must be wired to aws_apigatewayv2_stage.default.arn"
  }

  assert {
    condition     = output.access_log_group_name == "/knotify/knotify-test-api/access-logs"
    error_message = "access_log_group_name output must be wired to aws_cloudwatch_log_group.access_logs.name"
  }
}
