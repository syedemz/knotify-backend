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
# push_tokens Lambda function
#
# Handles POST /v1/push-tokens — registers or refreshes a device push
# notification token via DynamoDB PutItem (unconditional upsert).
#
# NOT gated by @require_profile_complete: tokens must be registered at first
# app launch before onboarding completes. JWT authorizer enforcement is
# handled at the API Gateway level (HTTP API Cognito authorizer).
#
# Architecture: ARM64 (Graviton2) — consistent with all other Lambda functions.
# Timeout: 10s (default) — a DynamoDB PutItem PK lookup is fast; no Aurora,
# no external HTTP calls.
#
# VPC config: accepts an optional vpc_config variable. In dev and prod the
# Lambda is placed in private subnets with the lambda security group so it
# can reach the DynamoDB VPC endpoint (same pattern as other REST handlers).
# ---------------------------------------------------------------------------

module "lambda" {
  source = "../lambda"

  function_name = var.function_name
  handler       = "handler.handler"
  filename      = var.filename
  role_arn      = var.role_arn
  architectures = ["arm64"]
  layers        = var.layers

  vpc_config = var.vpc_config

  environment_variables = {
    # DynamoDB table name for PushNotificationTokens.
    # Default "PushNotificationTokens" matches the table name in modules/dynamodb/main.tf.
    TABLE_PUSH_TOKENS = var.table_push_tokens_name
  }
}
