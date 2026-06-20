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
# CloudWatch log group — Step Functions execution logs
#
# 7-day retention is consistent with every other knotify log group and keeps
# costs proportional to the workload. The group is created explicitly so the
# retention policy is enforced and the group survives a terraform destroy of
# the state machine.
#
# Name pattern: /aws/states/knotify-<env>-account-deletion
# The :* suffix required by Step Functions logging is appended in the
# log_destination reference below and in the outputs.tf log_group_arn output.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "account_deletion_sfn" {
  name              = "/aws/states/knotify-${var.environment}-account-deletion"
  retention_in_days = 7
}

# ---------------------------------------------------------------------------
# Step Functions Standard state machine — account deletion workflow
#
# The ASL definition is expressed as a templated JSON file so Lambda ARNs can
# be injected at plan time via templatefile(). This keeps the definition
# readable and avoids brittle string interpolation inside a Terraform string.
#
# Type: STANDARD (not EXPRESS) — the deletion workflow takes up to 15 minutes
# per branch and requires at-least-once execution guarantees with full
# CloudWatch execution history. STANDARD is appropriate here.
#
# Workflow branches (wired in the ASL template):
#   purge_immediately=false — soft-delete branch:
#     DisableCognitoUser(disable) → DeactivateChatRooms →
#     Parallel{SoftDeleteAurora, DeleteDynamoDBPersonalData,
#              AnonymizeChatMessages (loop)} →
#     DeleteCognitoUser(delete) → WriteAuditLog
#
#   purge_immediately=true — purge-immediately branch:
#     DisableCognitoUser(disable) → DeactivateChatRooms →
#     Parallel{SoftDeleteAurora→HardPurgeNow, DeleteDynamoDBPersonalData,
#              HardDeleteUserChatMessages (loop)} →
#     DeleteCognitoUser(delete) → WriteAuditLog
#
# Global Catch: every Task routes to RecordFailureAuditLog → EmitDeletionFailedMetric
# → DeletionFailed (Fail state) on any error.
#
# Key wiring decisions (per story 9.1 acceptance criteria):
#   - ValidateDeletionRequest uses ResultPath: "$.validation" so the original
#     input (user_id, purge_immediately) survives unchanged for downstream states.
#   - DeactivateChatRooms uses ResultPath: "$.deactivate_chat_rooms" so its
#     room_ids output is available to AnonymizeChatMessages and
#     HardDeleteUserChatMessages via an explicit Parameters block.
#   - DisableCognitoUser and DeleteCognitoUser each inject mode via Parameters
#     (task-local constant, not derived from state).
#   - The CloudWatch DeletionFailed metric is emitted via an aws-sdk integration
#     state (EmitDeletionFailedMetric) in the failure path, which reuses the
#     execution role's cloudwatch:PutMetricData permission.
# ---------------------------------------------------------------------------

resource "aws_sfn_state_machine" "account_deletion" {
  name     = "knotify-${var.environment}-account-deletion"
  role_arn = var.execution_role_arn
  type     = "STANDARD"

  definition = templatefile("${path.module}/account_deletion.asl.json.tftpl", {
    validate_deletion_request_arn  = var.lambda_arns.validate_deletion_request
    cognito_user_state_arn         = var.lambda_arns.cognito_user_state
    deactivate_chat_rooms_arn      = var.lambda_arns.deactivate_chat_rooms
    soft_delete_aurora_arn         = var.lambda_arns.soft_delete_aurora
    delete_dynamodb_personal_arn   = var.lambda_arns.delete_dynamodb_personal
    anonymize_chat_messages_arn    = var.lambda_arns.anonymize_chat_messages
    hard_purge_now_arn             = var.lambda_arns.hard_purge_now
    hard_delete_user_chat_msgs_arn = var.lambda_arns.hard_delete_user_chat_msgs
    write_audit_log_arn            = var.lambda_arns.write_audit_log
    environment                    = var.environment
  })

  logging_configuration {
    level                  = "ALL"
    include_execution_data = true
    log_destination        = "${aws_cloudwatch_log_group.account_deletion_sfn.arn}:*"
  }

  tracing_configuration {
    enabled = false
  }
}
