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
# deletion_initiator Lambda function — story 9.9
#
# Handles two HTTP API Gateway v2 routes:
#   DELETE /v1/profile/me              — initiate account deletion
#   GET    /v1/profile/me/deletion-status — query deletion status (stub, 9.10)
#
# DELETE /v1/profile/me:
#   Extracts user_id from the JWT sub claim, parses purge_immediately from the
#   request body (default false), calls Step Functions StartExecution on the
#   account-deletion state machine, and returns 202 with the executionArn.
#   StartExecution input: {user_id, jwt_sub, purge_immediately} where
#   user_id == jwt_sub (initiator enforces equality; story 9.2 defense-in-depth
#   re-checks inside the state machine).
#
# Placement: OUTSIDE the VPC.
# WHY: only calls Step Functions (public HTTPS endpoint) — no Aurora, no DynamoDB.
# Running outside the VPC avoids the ENI cold-start penalty and eliminates the
# hotfix #106 blackhole trap. Consistent with write_audit_log, validate_deletion_request,
# deactivate_chat_rooms, anonymize_chat_messages which also run outside the VPC.
# AWSLambdaBasicExecutionRole is sufficient — no ENI attachment needed.
#
# Architecture: ARM64 (Graviton2) — consistent with all other Lambda functions.
# Timeout: 30s — StartExecution is a fast API call; no pagination, no Aurora.
# Memory: 128 MB — minimal boto3 workload.
#
# Layers: knotify_obs (provides init_logger and with_edge_secret).
# Does NOT use knotify_db — no Aurora or Secrets Manager access in this Lambda.
#
# EDGE_SECRET is injected so the @with_edge_secret decorator can validate that
# all traffic arrived via CloudFront (consistent with all other REST Lambdas).
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
  layers        = var.layers

  # No vpc_config — Lambda runs OUTSIDE the VPC (see placement note above).

  environment_variables = {
    # ARN of the account-deletion Step Functions state machine.
    # The handler calls states:StartExecution against this ARN.
    STATE_MACHINE_ARN = var.state_machine_arn

    # CloudFront edge secret — validated by the @with_edge_secret decorator.
    EDGE_SECRET = var.edge_secret
  }
}
