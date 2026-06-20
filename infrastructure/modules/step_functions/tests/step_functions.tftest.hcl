# Step Functions module tests — all use command = plan (hermetic, no AWS credentials required)
# Tests are designed so `terraform test` exits 0 from infrastructure/modules/step_functions/
#
# TDD note: this file was authored BEFORE main.tf/variables.tf/outputs.tf to drive
# the implementation shape via acceptance criteria (story 9.1).
#
# mock_provider overrides supply deterministic values for the data sources that the
# module uses to construct ARN patterns and log group names.
#
# Acceptance criteria covered:
#   AC1  — aws_sfn_state_machine declared with type = STANDARD
#   AC2  — logging_configuration block present with level = ALL
#   AC3  — CloudWatch log group declared with retention_in_days = 7
#   AC4  — module output state_machine_arn wired to the state machine ARN
#   AC5  — definition body is non-empty (templated JSON rendered at plan time)

mock_provider "aws" {
  mock_data "aws_partition" {
    defaults = {
      partition  = "aws"
      dns_suffix = "amazonaws.com"
    }
  }
  mock_data "aws_region" {
    defaults = {
      region = "eu-central-1"
      name   = "eu-central-1"
    }
  }
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "123456789012"
      arn        = "arn:aws:iam::123456789012:root"
      user_id    = "AIDACKCEVSQ6C2EXAMPLE"
    }
  }
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{}"
    }
  }
}

# Shared stub variable block used by all tests.
# Each run block must supply variables independently — terraform test
# does not support a module-level variable default block.

# ---------------------------------------------------------------------------
# Test 1: state machine resource is type STANDARD
#
# Satisfies AC: "aws_sfn_state_machine of type STANDARD"
# ---------------------------------------------------------------------------
run "state_machine_type_is_standard" {
  command = plan

  variables {
    environment        = "test"
    execution_role_arn = "arn:aws:iam::123456789012:role/knotify-test-stepfn-deletion-exec"
    lambda_arns = {
      validate_deletion_request  = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-validate-deletion-request-test:live"
      cognito_user_state         = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-cognito-user-state-test:live"
      deactivate_chat_rooms      = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-deactivate-chat-rooms-test:live"
      soft_delete_aurora         = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-soft-delete-aurora-test:live"
      delete_dynamodb_personal   = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-delete-dynamodb-personal-test:live"
      anonymize_chat_messages    = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-anonymize-chat-messages-test:live"
      hard_purge_now             = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-hard-purge-now-test:live"
      hard_delete_user_chat_msgs = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-hard-delete-user-chat-msgs-test:live"
      write_audit_log            = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-write-audit-log-test:live"
    }
  }

  assert {
    condition     = aws_sfn_state_machine.account_deletion.type == "STANDARD"
    error_message = "state machine type must be STANDARD"
  }
}

# ---------------------------------------------------------------------------
# Test 2: state machine logging_configuration level is ALL
#
# Satisfies AC: "logging_configuration enabled at ALL"
# ---------------------------------------------------------------------------
run "state_machine_logging_level_is_all" {
  command = plan

  variables {
    environment        = "test"
    execution_role_arn = "arn:aws:iam::123456789012:role/knotify-test-stepfn-deletion-exec"
    lambda_arns = {
      validate_deletion_request  = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-validate-deletion-request-test:live"
      cognito_user_state         = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-cognito-user-state-test:live"
      deactivate_chat_rooms      = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-deactivate-chat-rooms-test:live"
      soft_delete_aurora         = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-soft-delete-aurora-test:live"
      delete_dynamodb_personal   = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-delete-dynamodb-personal-test:live"
      anonymize_chat_messages    = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-anonymize-chat-messages-test:live"
      hard_purge_now             = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-hard-purge-now-test:live"
      hard_delete_user_chat_msgs = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-hard-delete-user-chat-msgs-test:live"
      write_audit_log            = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-write-audit-log-test:live"
    }
  }

  assert {
    condition     = aws_sfn_state_machine.account_deletion.logging_configuration[0].level == "ALL"
    error_message = "state machine logging level must be ALL"
  }
}

# ---------------------------------------------------------------------------
# Test 3: CloudWatch log group for state machine has 7-day retention
#
# Satisfies AC: "7-day retention CloudWatch log group"
# ---------------------------------------------------------------------------
run "state_machine_log_group_has_7_day_retention" {
  command = plan

  variables {
    environment        = "test"
    execution_role_arn = "arn:aws:iam::123456789012:role/knotify-test-stepfn-deletion-exec"
    lambda_arns = {
      validate_deletion_request  = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-validate-deletion-request-test:live"
      cognito_user_state         = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-cognito-user-state-test:live"
      deactivate_chat_rooms      = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-deactivate-chat-rooms-test:live"
      soft_delete_aurora         = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-soft-delete-aurora-test:live"
      delete_dynamodb_personal   = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-delete-dynamodb-personal-test:live"
      anonymize_chat_messages    = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-anonymize-chat-messages-test:live"
      hard_purge_now             = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-hard-purge-now-test:live"
      hard_delete_user_chat_msgs = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-hard-delete-user-chat-msgs-test:live"
      write_audit_log            = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-write-audit-log-test:live"
    }
  }

  assert {
    condition     = aws_cloudwatch_log_group.account_deletion_sfn.retention_in_days == 7
    error_message = "state machine CloudWatch log group retention_in_days must be 7"
  }
}

# ---------------------------------------------------------------------------
# Test 4: state_machine_arn output is wired to the state machine ARN
#
# Satisfies AC: "Module output: state_machine_arn"
# ---------------------------------------------------------------------------
run "state_machine_arn_output_is_wired" {
  command = plan

  variables {
    environment        = "test"
    execution_role_arn = "arn:aws:iam::123456789012:role/knotify-test-stepfn-deletion-exec"
    lambda_arns = {
      validate_deletion_request  = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-validate-deletion-request-test:live"
      cognito_user_state         = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-cognito-user-state-test:live"
      deactivate_chat_rooms      = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-deactivate-chat-rooms-test:live"
      soft_delete_aurora         = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-soft-delete-aurora-test:live"
      delete_dynamodb_personal   = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-delete-dynamodb-personal-test:live"
      anonymize_chat_messages    = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-anonymize-chat-messages-test:live"
      hard_purge_now             = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-hard-purge-now-test:live"
      hard_delete_user_chat_msgs = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-hard-delete-user-chat-msgs-test:live"
      write_audit_log            = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-write-audit-log-test:live"
    }
  }

  override_resource {
    target = aws_sfn_state_machine.account_deletion
    values = {
      arn = "arn:aws:states:eu-central-1:123456789012:stateMachine:knotify-test-account-deletion"
    }
    override_during = plan
  }

  assert {
    condition     = output.state_machine_arn == "arn:aws:states:eu-central-1:123456789012:stateMachine:knotify-test-account-deletion"
    error_message = "state_machine_arn output must be wired to aws_sfn_state_machine.account_deletion.arn"
  }
}

# ---------------------------------------------------------------------------
# Test 5: state machine definition is non-empty (templated JSON rendered)
#
# Satisfies AC: "definition expressed as a templated JSON file"
# The definition body is rendered by templatefile() and contains the Lambda ARNs.
# We assert it is a non-empty string — structural correctness is validated
# by terraform validate and end-to-end correctness by story 9.13.
# ---------------------------------------------------------------------------
run "state_machine_definition_is_non_empty" {
  command = plan

  variables {
    environment        = "test"
    execution_role_arn = "arn:aws:iam::123456789012:role/knotify-test-stepfn-deletion-exec"
    lambda_arns = {
      validate_deletion_request  = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-validate-deletion-request-test:live"
      cognito_user_state         = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-cognito-user-state-test:live"
      deactivate_chat_rooms      = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-deactivate-chat-rooms-test:live"
      soft_delete_aurora         = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-soft-delete-aurora-test:live"
      delete_dynamodb_personal   = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-delete-dynamodb-personal-test:live"
      anonymize_chat_messages    = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-anonymize-chat-messages-test:live"
      hard_purge_now             = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-hard-purge-now-test:live"
      hard_delete_user_chat_msgs = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-hard-delete-user-chat-msgs-test:live"
      write_audit_log            = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-write-audit-log-test:live"
    }
  }

  assert {
    condition     = aws_sfn_state_machine.account_deletion.definition != ""
    error_message = "state machine definition must be non-empty (templatefile rendered)"
  }
}

# ---------------------------------------------------------------------------
# Test 6: logging_configuration destination_config is wired to the log group ARN
#
# Satisfies AC: "logging_configuration enabled at ALL with ... CloudWatch log group"
# The log_destination must reference the log group ARN with :* suffix.
# We override the log group resource to supply a known ARN so that the
# log_destination (which is computed from the log group ARN) becomes known
# at plan time and can be asserted.
# ---------------------------------------------------------------------------
run "state_machine_logging_destination_references_log_group" {
  command = plan

  variables {
    environment        = "test"
    execution_role_arn = "arn:aws:iam::123456789012:role/knotify-test-stepfn-deletion-exec"
    lambda_arns = {
      validate_deletion_request  = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-validate-deletion-request-test:live"
      cognito_user_state         = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-cognito-user-state-test:live"
      deactivate_chat_rooms      = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-deactivate-chat-rooms-test:live"
      soft_delete_aurora         = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-soft-delete-aurora-test:live"
      delete_dynamodb_personal   = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-delete-dynamodb-personal-test:live"
      anonymize_chat_messages    = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-anonymize-chat-messages-test:live"
      hard_purge_now             = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-hard-purge-now-test:live"
      hard_delete_user_chat_msgs = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-hard-delete-user-chat-msgs-test:live"
      write_audit_log            = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-write-audit-log-test:live"
    }
  }

  override_resource {
    target = aws_cloudwatch_log_group.account_deletion_sfn
    values = {
      arn = "arn:aws:logs:eu-central-1:123456789012:log-group:/aws/states/knotify-test-account-deletion"
    }
    override_during = plan
  }

  assert {
    condition     = aws_sfn_state_machine.account_deletion.logging_configuration[0].log_destination == "arn:aws:logs:eu-central-1:123456789012:log-group:/aws/states/knotify-test-account-deletion:*"
    error_message = "logging_configuration log_destination must be the log group ARN with :* suffix"
  }
}
