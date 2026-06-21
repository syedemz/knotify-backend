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
# cognito_user_state Lambda function — story 9.3
#
# Dispatches to Cognito IDP AdminDisableUser or AdminDeleteUser depending on
# the mode input field ("disable" | "delete").  Called twice by the
# account-deletion Step Functions state machine:
#   DisableCognitoUser  (mode=disable) — early in workflow
#   DeleteCognitoUser   (mode=delete)  — late in workflow
#
# Placement: OUTSIDE the VPC.
# WHY: only calls Cognito IDP which is a public AWS HTTPS endpoint — no Aurora,
# no DynamoDB.  Running outside the VPC avoids the ENI cold-start penalty and
# eliminates the hotfix #106 blackhole trap (private subnets without NAT
# cannot reach Cognito if no VPC endpoint is configured).
# AWSLambdaBasicExecutionRole is sufficient — no ENI attachment needed.
#
# Architecture: ARM64 (Graviton2) — consistent with all other Lambda functions.
# Timeout: 30s — Cognito AdminDisableUser / AdminDeleteUser are fast API calls;
# 30s is generous headroom for retries.
# Memory: 128 MB — minimal boto3 workload, no layers.
# No layers: this Lambda only needs boto3 (bundled in the Lambda runtime).
#   It does NOT connect to Aurora or DynamoDB and does NOT need knotify_db
#   or knotify_obs.
# ---------------------------------------------------------------------------

module "lambda" {
  source = "../lambda"

  function_name = var.function_name
  handler       = "handler.handler"
  filename      = var.filename
  role_arn      = var.role_arn
  architectures = ["arm64"]
  timeout       = 30
  memory_size   = 128

  # No vpc_config — Lambda runs OUTSIDE the VPC (see placement note above).

  environment_variables = {
    # Cognito User Pool ID — injected at deploy time from module.cognito.user_pool_id.
    # Default empty string causes UserNotFoundException on all calls (safe for tests).
    USER_POOL_ID = var.user_pool_id
  }
}
