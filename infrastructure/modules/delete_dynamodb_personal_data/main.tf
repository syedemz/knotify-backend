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
# delete_dynamodb_personal_data Lambda function — story 9.7
#
# Deletes all personal DynamoDB data for a user whose account is being deleted.
# Called from the account-deletion Step Functions state machine inside the
# ParallelCleanup block.
#
# Two tables are cleaned:
#   - Notifications          (PK=user_id, SK=created_at_notification_id)
#   - PushNotificationTokens (PK=user_id, SK=device_id)
#
# For each table the Lambda queries all rows for the user and BatchWriteItem-
# deletes them in chunks of ≤25 (DynamoDB BatchWriteItem limit).
#
# Placement: OUTSIDE the VPC.
# WHY: only touches DynamoDB (Notifications and PushNotificationTokens tables) —
# no Aurora, no AppSync.  Running outside the VPC avoids the ENI cold-start
# penalty and eliminates the hotfix #106 blackhole trap (private subnets without
# NAT cannot reach DynamoDB via service endpoints if no VPC endpoint is
# configured).  Consistent with deactivate_chat_rooms, anonymize_chat_messages,
# write_audit_log, and push_fanout which also run outside the VPC.
# AWSLambdaBasicExecutionRole is sufficient — no ENI attachment needed.
#
# Architecture: ARM64 (Graviton2) — consistent with all other Lambda functions.
# Timeout: 300s — DynamoDB delete is fast; pagination loop is bounded by Lambda
#   timeout.  No continuation-token contract needed (per story 9.7 cross-story
#   integration notes: the Lambda is unlikely to time out given DDB delete speed).
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
  timeout       = 300
  memory_size   = 128

  # No vpc_config — Lambda runs OUTSIDE the VPC (see placement note above).

  environment_variables = {
    # DynamoDB table names — injected at deploy time.
    # Defaults ("Notifications" / "PushNotificationTokens") are safe for local tests.
    TABLE_NOTIFICATIONS            = var.notifications_table_name
    TABLE_PUSH_NOTIFICATION_TOKENS = var.push_notification_tokens_table_name
  }
}
