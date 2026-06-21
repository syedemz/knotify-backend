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
# write_audit_log Lambda function — story 9.8
#
# Writes audit records to account_deletion_audit on every account-deletion
# workflow event: deletion_initiated, deletion_completed, deletion_failed.
# Called by the Step Functions state machine at the end of each branch and
# from the global Catch handler.
#
# Placement: OUTSIDE the VPC.
# WHY: only touches DynamoDB (account_deletion_audit) via the regional public
# endpoint — no Aurora, no AppSync.  Running outside the VPC avoids the ENI
# cold-start penalty and eliminates the hotfix #106 blackhole trap.
# AWSLambdaBasicExecutionRole is sufficient — no ENI attachment needed.
#
# Architecture: ARM64 (Graviton2) — consistent with all other Lambda functions.
# Timeout: 30s — a DynamoDB PutItem is fast; no pagination-heavy workload.
# Memory: 128 MB — minimal boto3 workload, no layers required.
# No layers: only needs boto3 (bundled in the Lambda runtime) and Python stdlib.
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
    # Default ("account_deletion_audit") matches the table name in modules/dynamodb/main.tf.
    TABLE_AUDIT = var.audit_table_name
  }
}
