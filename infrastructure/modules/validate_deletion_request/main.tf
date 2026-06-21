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
# validate_deletion_request Lambda function — story 9.2
#
# First task in the account-deletion Step Functions state machine.
# Validates the deletion request before any irreversible cleanup steps run:
#   1. Checks input.user_id == input.jwt_sub (defense-in-depth; UserIdMismatch)
#   2. Queries account_deletion_audit for an in-progress deletion (DeletionInProgress)
#   3. Writes a "deletion_initiated" audit record (direct PutItem — not via write_audit_log)
#
# State machine wiring (story 9.1): ResultPath: '$.validation' — the original
# input fields (user_id, purge_immediately) survive unchanged past this task.
#
# Placement: OUTSIDE the VPC.
# WHY: only touches DynamoDB (account_deletion_audit) via the regional public
# endpoint — no Aurora, no AppSync.  Running outside the VPC avoids the ENI
# cold-start penalty and eliminates the hotfix #106 blackhole trap.
# Consistent with write_audit_log, deactivate_chat_rooms, anonymize_chat_messages,
# and delete_dynamodb_personal_data which also run outside the VPC.
# AWSLambdaBasicExecutionRole is sufficient — no ENI attachment needed.
#
# Architecture: ARM64 (Graviton2) — consistent with all other Lambda functions.
# Timeout: 30s — DynamoDB Query + PutItem is fast; no pagination-heavy workload.
# Memory: 128 MB — minimal boto3 workload, no layers required.
# No layers: only needs boto3 (bundled in the Lambda runtime) and Python stdlib.
#   Does NOT connect to Aurora and does NOT need knotify_db or knotify_obs.
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
    # DynamoDB table name — injected at deploy time.
    # Default ("account_deletion_audit") is safe for local tests.
    TABLE_AUDIT = var.audit_table_name
  }
}
