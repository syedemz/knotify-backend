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
# hard_delete_user_chat_messages Lambda function — story 9.12
#
# Hard-deletes (DeleteItem) all ChatMessages rows sent by the deleted user
# across all rooms they were a member of.  Called from the purge_immediately
# branch of the account-deletion Step Functions state machine inside
# PurgeImmediately_ParallelCleanup (after DeactivateChatRooms has already
# removed the user's ChatRoomMembership rows).
#
# This Lambda replaces AnonymizeChatMessages in the purge_immediately branch.
# The soft-delete branch continues to use AnonymizeChatMessages (story 9.6).
#
# Accepts a continuation token {room_ids, current_room_index, last_evaluated_key}
# and returns {has_more, room_ids, current_room_index, last_evaluated_key}.
# Step Functions iterates via a Choice → Task → Choice loop on the has_more flag.
#
# Placement: OUTSIDE the VPC.
# WHY: only touches DynamoDB (ChatMessages table) — no Aurora, no AppSync.
# Running outside the VPC avoids the ENI cold-start penalty and the hotfix #106
# blackhole trap (private subnets without NAT cannot reach DynamoDB service
# endpoints).  Consistent with anonymize_chat_messages (story 9.6),
# write_audit_log, deactivate_chat_rooms, and push_fanout.
# AWSLambdaBasicExecutionRole is sufficient — no ENI attachment needed.
#
# Architecture: ARM64 (Graviton2) — consistent with all other Lambda functions.
# Timeout: 900s (15 minutes) — in-Lambda paginated work is bounded by this limit;
# the continuation token survives across invocations for users with large message
# volumes.
# Memory: 128 MB — minimal boto3 workload; no layers required.
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
  timeout       = 900
  memory_size   = 128

  # No vpc_config — Lambda runs OUTSIDE the VPC (see placement note above).

  environment_variables = {
    # DynamoDB table name — injected at deploy time.
    # Default ("ChatMessages") is safe for local tests.
    TABLE_CHAT_MESSAGES = var.chat_messages_table_name
  }
}
