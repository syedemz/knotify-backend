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
# chat_resolver Lambda function
#
# The chat_resolver is an AppSync Lambda resolver — it is NOT integrated with
# API Gateway.  AppSync invokes it directly for every resolver field that is
# mapped to the chat_resolver data source.
#
# Placement: inside the VPC (private subnets) so the Lambda can reach Aurora
# on the private endpoint.  DynamoDB is accessed via the VPC endpoint
# provisioned by the networking module.
#
# Architecture: ARM64 (Graviton2) — consistent with all other Lambda functions
# in this project.
#
# Timeout: 30 seconds — AppSync has a 30-second resolver timeout; setting the
# Lambda timeout to the same value ensures timeouts propagate cleanly without
# AWS infrastructure confusion between the two limits.
#
# Layers:
#   - knotify_obs: init_logger, require_profile_complete_appsync
#   - knotify_db:  get_connection, rls_context, block_filter
#
# The module outputs lambda_arn (alias ARN) for consumption by the AppSync
# module in story 8.1 when registering the Lambda data source.
# ---------------------------------------------------------------------------

module "lambda" {
  source = "../lambda"

  function_name = var.function_name
  handler       = "handler.handler"
  filename      = var.filename
  role_arn      = var.role_arn
  layers        = var.layers
  architectures = ["arm64"]
  timeout       = 30

  vpc_config = var.vpc_config

  environment_variables = merge(var.db_env_vars, {
    # app_user credential friendly name — used by knotify_db.get_connection()
    # alongside the DB_SECRET_ARN carried by db_env_vars.
    DB_SECRET_NAME = var.db_secret_name
  })
}
