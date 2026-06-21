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
# deactivate_chat_rooms Lambda function — story 9.4
#
# Deactivates all ChatRooms the deleted user was a member of and removes the
# deleted user's ChatRoomMembership rows.  Called from the account-deletion
# Step Functions state machine after DisableCognitoUser.
#
# Placement: OUTSIDE the VPC.
# WHY: only touches DynamoDB (ChatRooms and ChatRoomMembership tables) — no
# Aurora, no AppSync.  Running outside the VPC avoids the ENI cold-start
# penalty and eliminates the hotfix #106 blackhole trap (private subnets
# without NAT cannot reach DynamoDB via service endpoints if no VPC endpoint
# is configured).  Consistent with write_audit_log, room_state_publisher, and
# push_fanout which also run outside the VPC.
# AWSLambdaBasicExecutionRole is sufficient — no ENI attachment needed.
#
# Architecture: ARM64 (Graviton2) — consistent with all other Lambda functions.
# Timeout: 300s — may need to UpdateItem on many ChatRooms rows for users who
# are members of a large number of rooms, plus BatchWriteItem pagination.
# Memory: 128 MB — minimal boto3 workload, no layers.
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
    # Defaults ("ChatRooms" / "ChatRoomMembership") are safe for local tests.
    TABLE_CHAT_ROOMS            = var.chat_rooms_table_name
    TABLE_CHAT_ROOM_MEMBERSHIP  = var.chat_room_membership_table_name
  }
}
