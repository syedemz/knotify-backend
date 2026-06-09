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
# HTTP API Gateway
#
# Creates an HTTP API (not REST API). The HTTP API flavor is the correct choice
# for this project: it natively supports JWT authorizers, has lower per-request
# cost, and forwards the Lambda payload format v2. REST API (v1) is not used.
#
# CORS: intentionally NOT configured. The v1 mobile React Native client uses
# native HTTP libraries and does not send CORS preflight requests. See README.md
# for the rationale and the flag for future Expo Web support.
# ---------------------------------------------------------------------------

resource "aws_apigatewayv2_api" "this" {
  name          = var.name
  protocol_type = "HTTP"
}

# ---------------------------------------------------------------------------
# CloudWatch access-log group
#
# retention_in_days=7 matches the project-wide convention (architecture.md §10.6).
# The group is created before the stage so the destination ARN is known at plan
# time for wiring into access_log_settings.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "access_logs" {
  name              = "/knotify/${var.name}/access-logs"
  retention_in_days = var.access_log_retention_days
}

# ---------------------------------------------------------------------------
# Default stage ($default)
#
# The $default stage is the standard single-stage HTTP API deployment target.
# auto_deploy = true so every API change is immediately deployed without a
# manual deployment step. Throttling limits are configurable via variables
# with conservative dev defaults (brainstorm Mn4: burst=10, rate=25).
#
# Access log format: JSON produced by jsonencode() with $context.* placeholders.
# The placeholders are template strings evaluated by API Gateway at request time,
# NOT by Terraform. jsonencode() is used to guarantee valid JSON in CloudWatch
# Logs Insights from day one (brainstorm Tb2).
# ---------------------------------------------------------------------------

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.this.id
  name        = "$default"
  auto_deploy = true

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.access_logs.arn
    format = jsonencode({
      requestId          = "$context.requestId"
      status             = "$context.status"
      routeKey           = "$context.routeKey"
      integrationLatency = "$context.integrationLatency"
      authLatency        = "$context.authorizer.latency"
      sourceIp           = "$context.identity.sourceIp"
      userAgent          = "$context.identity.userAgent"
    })
  }

  default_route_settings {
    throttling_burst_limit = var.throttling_burst_limit
    throttling_rate_limit  = var.throttling_rate_limit
  }
}

# ---------------------------------------------------------------------------
# JWT authorizer — Cognito User Pool
#
# HTTP API allows exactly one authorizer per route. This JWT authorizer uses
# Cognito's built-in JWKS validation (no custom Lambda needed). The issuer URL
# and audience are wired from Cognito module outputs at the env level.
#
# audience is built at the call site with:
#   compact([module.cognito.app_client_id, module.cognito.integration_test_app_client_id])
# In dev: both IDs are non-empty → audience has two entries.
# In prod: integration_test_app_client_id is "" → compact drops it → audience has one entry.
# This allows phase-4.6-style integration tests (ADMIN_USER_PASSWORD_AUTH) to
# mint tokens that pass authorizer validation in dev (brainstorm M1).
# ---------------------------------------------------------------------------

resource "aws_apigatewayv2_authorizer" "cognito_jwt" {
  api_id           = aws_apigatewayv2_api.this.id
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]
  name             = "${var.name}-cognito-jwt"

  jwt_configuration {
    issuer   = var.cognito_user_pool_endpoint
    audience = var.cognito_audience_client_ids
  }
}
