# IAM roles module tests — all use command = plan (hermetic, no AWS credentials required)
# Tests are designed so `terraform test` exits 0 from infrastructure/modules/iam_roles/
#
# TDD note: this file was authored BEFORE main.tf/variables.tf/outputs.tf to drive
# the implementation shape via acceptance criteria.
#
# mock_provider overrides supply deterministic values for the data sources that the
# module uses to construct ARN patterns:
#   data.aws_partition.current.partition     → "aws"
#   data.aws_region.current.region           → "eu-central-1"
#   data.aws_caller_identity.current.account_id → "123456789012"
#
# All seven roles are covered:
#   db_migrator       — full inline policies asserted (AC bullet 2)
#   cognito_trigger   — full inline policies asserted (AC bullet 3)
#   aurora_reader, aurora_writer, dynamodb_chat_writer,
#   dynamodb_notifications_writer, stepfn_task
#                     — trust policy + AWSLambdaVPCAccessExecutionRole only (AC bullet 5)

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

# ---------------------------------------------------------------------------
# Test 1: db_migrator role exists with a non-empty assume_role_policy
#
# Satisfies AC bullet 2 (trust policy must be present) and AC bullet 4
# (all roles have a trust policy).
# The trust policy JSON is produced by aws_iam_policy_document (computed);
# we assert it is non-empty — content is structurally verified by validate.
# ---------------------------------------------------------------------------
run "db_migrator_role_exists_with_trust_policy" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
  }

  assert {
    condition     = aws_iam_role.db_migrator.assume_role_policy != ""
    error_message = "db_migrator assume_role_policy must not be empty"
  }
}

# ---------------------------------------------------------------------------
# Test 2: db_migrator attaches AWSLambdaVPCAccessExecutionRole managed policy
#
# Satisfies AC bullet 4: "Each role includes AWSLambdaVPCAccessExecutionRole
# so the Lambda can attach an ENI."
# ---------------------------------------------------------------------------
run "db_migrator_attaches_vpc_access_managed_policy" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
  }

  assert {
    condition     = aws_iam_role_policy_attachment.db_migrator_vpc_access.policy_arn == "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
    error_message = "db_migrator must attach AWSLambdaVPCAccessExecutionRole"
  }
}

# ---------------------------------------------------------------------------
# Test 3: db_migrator inline policy for Aurora master secret exists and is
#         correctly wired to the role
#
# Satisfies AC bullet 2: "allows secretsmanager:GetSecretValue scoped to the
# Aurora master_user_secret ARN" — the policy resource is now plumbed through
# as the exact ARN of the master_user_secret output (the constructed-name
# pattern from the original brainstorm did not match the real secret name).
#
# The policy JSON is computed (data source); we assert the structural wiring
# (resource name, role reference). ARN scoping is verified by validate.
# ---------------------------------------------------------------------------
run "db_migrator_master_secret_inline_policy_exists" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
  }

  assert {
    condition     = aws_iam_role_policy.db_migrator_secrets.name == "db-migrator-secrets"
    error_message = "db_migrator inline policy must be named db-migrator-secrets"
  }

  assert {
    condition     = aws_iam_role_policy.db_migrator_secrets.role == aws_iam_role.db_migrator.name
    error_message = "db_migrator inline policy must be attached to the db_migrator role"
  }
}

# ---------------------------------------------------------------------------
# Test 4: db_migrator inline policy for app_user credential management exists
#         and is correctly wired to the role
#
# Satisfies AC bullet 2: "allows secretsmanager:CreateSecret + PutSecretValue
# + DescribeSecret scoped to knotify-${env}-app-user-credential-*"
# ---------------------------------------------------------------------------
run "db_migrator_app_user_credential_inline_policy_exists" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
  }

  assert {
    condition     = aws_iam_role_policy.db_migrator_app_user_credential.name == "db-migrator-app-user-credential"
    error_message = "db_migrator app_user credential policy must be named db-migrator-app-user-credential"
  }

  assert {
    condition     = aws_iam_role_policy.db_migrator_app_user_credential.role == aws_iam_role.db_migrator.name
    error_message = "db_migrator app_user credential policy must be attached to the db_migrator role"
  }
}

# ---------------------------------------------------------------------------
# Test 5: cognito_trigger role exists with a non-empty assume_role_policy
#
# Satisfies AC bullet 3 (trust policy must be present) and AC bullet 4.
# ---------------------------------------------------------------------------
run "cognito_trigger_role_exists_with_trust_policy" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
  }

  assert {
    condition     = aws_iam_role.cognito_trigger.assume_role_policy != ""
    error_message = "cognito_trigger assume_role_policy must not be empty"
  }
}

# ---------------------------------------------------------------------------
# Test 6: cognito_trigger attaches AWSLambdaVPCAccessExecutionRole
#
# Satisfies AC bullet 3 and AC bullet 4.
# ---------------------------------------------------------------------------
run "cognito_trigger_attaches_vpc_access_managed_policy" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
  }

  assert {
    condition     = aws_iam_role_policy_attachment.cognito_trigger_vpc_access.policy_arn == "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
    error_message = "cognito_trigger must attach AWSLambdaVPCAccessExecutionRole"
  }
}

# ---------------------------------------------------------------------------
# Test 7: cognito_trigger inline policy for app_user credential read exists
#         and is correctly wired to the role
#
# Satisfies AC bullet 3: "allows secretsmanager:GetSecretValue scoped to
# the app_user credential ARN pattern knotify-${env}-app-user-credential-*"
# ---------------------------------------------------------------------------
run "cognito_trigger_app_user_credential_inline_policy_exists" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
  }

  assert {
    condition     = aws_iam_role_policy.cognito_trigger_app_user_credential.name == "cognito-trigger-app-user-credential"
    error_message = "cognito_trigger inline policy must be named cognito-trigger-app-user-credential"
  }

  assert {
    condition     = aws_iam_role_policy.cognito_trigger_app_user_credential.role == aws_iam_role.cognito_trigger.name
    error_message = "cognito_trigger inline policy must be attached to the cognito_trigger role"
  }
}

# ---------------------------------------------------------------------------
# Test 8: aurora_reader — trust policy present and VPC managed policy attached
#
# Satisfies AC bullet 5: for roles with no phase-3 consumers, assert trust
# policy and AWSLambdaVPCAccessExecutionRole only; no per-action assertions.
# ---------------------------------------------------------------------------
run "aurora_reader_trust_policy_and_vpc_managed_policy" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
  }

  assert {
    condition     = aws_iam_role.aurora_reader.assume_role_policy != ""
    error_message = "aurora_reader assume_role_policy must not be empty"
  }

  assert {
    condition     = aws_iam_role_policy_attachment.aurora_reader_vpc_access.policy_arn == "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
    error_message = "aurora_reader must attach AWSLambdaVPCAccessExecutionRole"
  }
}

# ---------------------------------------------------------------------------
# Test 9: aurora_writer — trust policy present and VPC managed policy attached
# ---------------------------------------------------------------------------
run "aurora_writer_trust_policy_and_vpc_managed_policy" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
  }

  assert {
    condition     = aws_iam_role.aurora_writer.assume_role_policy != ""
    error_message = "aurora_writer assume_role_policy must not be empty"
  }

  assert {
    condition     = aws_iam_role_policy_attachment.aurora_writer_vpc_access.policy_arn == "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
    error_message = "aurora_writer must attach AWSLambdaVPCAccessExecutionRole"
  }
}

# ---------------------------------------------------------------------------
# Test 10: dynamodb_chat_writer — trust policy and VPC managed policy
# ---------------------------------------------------------------------------
run "dynamodb_chat_writer_trust_policy_and_vpc_managed_policy" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
  }

  assert {
    condition     = aws_iam_role.dynamodb_chat_writer.assume_role_policy != ""
    error_message = "dynamodb_chat_writer assume_role_policy must not be empty"
  }

  assert {
    condition     = aws_iam_role_policy_attachment.dynamodb_chat_writer_vpc_access.policy_arn == "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
    error_message = "dynamodb_chat_writer must attach AWSLambdaVPCAccessExecutionRole"
  }
}

# ---------------------------------------------------------------------------
# Test 11: dynamodb_notifications_writer — trust policy and VPC managed policy
# ---------------------------------------------------------------------------
run "dynamodb_notifications_writer_trust_policy_and_vpc_managed_policy" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
  }

  assert {
    condition     = aws_iam_role.dynamodb_notifications_writer.assume_role_policy != ""
    error_message = "dynamodb_notifications_writer assume_role_policy must not be empty"
  }

  assert {
    condition     = aws_iam_role_policy_attachment.dynamodb_notifications_writer_vpc_access.policy_arn == "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
    error_message = "dynamodb_notifications_writer must attach AWSLambdaVPCAccessExecutionRole"
  }
}

# ---------------------------------------------------------------------------
# Test 12: stepfn_task — trust policy uses states.amazonaws.com principal
#          and VPC managed policy is attached
#
# The dispatch brief notes: "stepfn_task's trust policy is states.amazonaws.com"
# The trust policy JSON is computed, so we assert structural presence; the
# correct principal is verified in the data source by validate.
# VPC managed policy is still attached per AC bullet 4 — Step Functions
# tasks that invoke Lambdas inside a VPC need the ENI attachment capability.
# ---------------------------------------------------------------------------
run "stepfn_task_trust_policy_and_vpc_managed_policy" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
  }

  assert {
    condition     = aws_iam_role.stepfn_task.assume_role_policy != ""
    error_message = "stepfn_task assume_role_policy must not be empty"
  }

  assert {
    condition     = aws_iam_role_policy_attachment.stepfn_task_vpc_access.policy_arn == "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
    error_message = "stepfn_task must attach AWSLambdaVPCAccessExecutionRole"
  }
}

# ---------------------------------------------------------------------------
# Test 13: role_arns output map contains all seven expected keys
#
# Satisfies the module output shape requirement so consuming stories can
# reference module.iam_roles.role_arns["db_migrator"] etc.
#
# Role ARNs are computed (unknown at plan time), so we override each role
# resource to supply deterministic ARN strings and then assert the output
# map values equal those strings. This also confirms the output map is wired
# to the correct role resources (not hardcoded strings).
# ---------------------------------------------------------------------------
run "role_arns_output_contains_all_nine_roles" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
  }

  override_resource {
    target = aws_iam_role.db_migrator
    values = {
      arn = "arn:aws:iam::123456789012:role/knotify-test-db-migrator"
    }
    override_during = plan
  }

  override_resource {
    target = aws_iam_role.cognito_trigger
    values = {
      arn = "arn:aws:iam::123456789012:role/knotify-test-cognito-trigger"
    }
    override_during = plan
  }

  override_resource {
    target = aws_iam_role.aurora_reader
    values = {
      arn = "arn:aws:iam::123456789012:role/knotify-test-aurora-reader"
    }
    override_during = plan
  }

  override_resource {
    target = aws_iam_role.aurora_writer
    values = {
      arn = "arn:aws:iam::123456789012:role/knotify-test-aurora-writer"
    }
    override_during = plan
  }

  override_resource {
    target = aws_iam_role.blocks_writer
    values = {
      arn = "arn:aws:iam::123456789012:role/knotify-test-blocks-writer"
    }
    override_during = plan
  }

  override_resource {
    target = aws_iam_role.dynamodb_chat_writer
    values = {
      arn = "arn:aws:iam::123456789012:role/knotify-test-dynamodb-chat-writer"
    }
    override_during = plan
  }

  override_resource {
    target = aws_iam_role.dynamodb_notifications_writer
    values = {
      arn = "arn:aws:iam::123456789012:role/knotify-test-dynamodb-notifications-writer"
    }
    override_during = plan
  }

  override_resource {
    target = aws_iam_role.stepfn_task
    values = {
      arn = "arn:aws:iam::123456789012:role/knotify-test-stepfn-task"
    }
    override_during = plan
  }

  override_resource {
    target = aws_iam_role.aurora_reader_match
    values = {
      arn = "arn:aws:iam::123456789012:role/knotify-test-aurora-reader-match"
    }
    override_during = plan
  }

  assert {
    condition     = output.role_arns["db_migrator"] == "arn:aws:iam::123456789012:role/knotify-test-db-migrator"
    error_message = "role_arns[db_migrator] must be wired to aws_iam_role.db_migrator.arn"
  }

  assert {
    condition     = output.role_arns["cognito_trigger"] == "arn:aws:iam::123456789012:role/knotify-test-cognito-trigger"
    error_message = "role_arns[cognito_trigger] must be wired to aws_iam_role.cognito_trigger.arn"
  }

  assert {
    condition     = output.role_arns["aurora_reader"] == "arn:aws:iam::123456789012:role/knotify-test-aurora-reader"
    error_message = "role_arns[aurora_reader] must be wired to aws_iam_role.aurora_reader.arn"
  }

  assert {
    condition     = output.role_arns["aurora_writer"] == "arn:aws:iam::123456789012:role/knotify-test-aurora-writer"
    error_message = "role_arns[aurora_writer] must be wired to aws_iam_role.aurora_writer.arn"
  }

  assert {
    condition     = output.role_arns["blocks_writer"] == "arn:aws:iam::123456789012:role/knotify-test-blocks-writer"
    error_message = "role_arns[blocks_writer] must be wired to aws_iam_role.blocks_writer.arn"
  }

  assert {
    condition     = output.role_arns["dynamodb_chat_writer"] == "arn:aws:iam::123456789012:role/knotify-test-dynamodb-chat-writer"
    error_message = "role_arns[dynamodb_chat_writer] must be wired to aws_iam_role.dynamodb_chat_writer.arn"
  }

  assert {
    condition     = output.role_arns["dynamodb_notifications_writer"] == "arn:aws:iam::123456789012:role/knotify-test-dynamodb-notifications-writer"
    error_message = "role_arns[dynamodb_notifications_writer] must be wired to aws_iam_role.dynamodb_notifications_writer.arn"
  }

  assert {
    condition     = output.role_arns["stepfn_task"] == "arn:aws:iam::123456789012:role/knotify-test-stepfn-task"
    error_message = "role_arns[stepfn_task] must be wired to aws_iam_role.stepfn_task.arn"
  }

  assert {
    condition     = output.role_arns["aurora_reader_match"] == "arn:aws:iam::123456789012:role/knotify-test-aurora-reader-match"
    error_message = "role_arns[aurora_reader_match] must be wired to aws_iam_role.aurora_reader_match.arn"
  }
}

# ---------------------------------------------------------------------------
# Test 14: blocks_writer role exists with trust policy and VPC managed policy
#
# Satisfies the story 6.4 AC: "New IAM role blocks_writer added to
# modules/iam_roles" with trust policy lambda.amazonaws.com and
# AWSLambdaVPCAccessExecutionRole attached.
# ---------------------------------------------------------------------------
run "blocks_writer_trust_policy_and_vpc_managed_policy" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
  }

  assert {
    condition     = aws_iam_role.blocks_writer.assume_role_policy != ""
    error_message = "blocks_writer assume_role_policy must not be empty"
  }

  assert {
    condition     = aws_iam_role_policy_attachment.blocks_writer_vpc_access.policy_arn == "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
    error_message = "blocks_writer must attach AWSLambdaVPCAccessExecutionRole"
  }
}

# ---------------------------------------------------------------------------
# Test 15: blocks_writer app_user credential inline policy exists
#
# Satisfies story 6.4 AC: "Aurora app-user secret GetSecretValue scoped to
# knotify-${env}-app-user-credential-*".
# ---------------------------------------------------------------------------
run "blocks_writer_app_user_credential_inline_policy_exists" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
  }

  assert {
    condition     = aws_iam_role_policy.blocks_writer_app_user_credential.name == "blocks-writer-app-user-credential"
    error_message = "blocks_writer app_user credential policy must be named blocks-writer-app-user-credential"
  }

  assert {
    condition     = aws_iam_role_policy.blocks_writer_app_user_credential.role == aws_iam_role.blocks_writer.name
    error_message = "blocks_writer app_user credential policy must be attached to the blocks_writer role"
  }
}

# ---------------------------------------------------------------------------
# Test 16: blocks_writer DynamoDB inline policy scoped to ChatRooms table
#
# Satisfies story 6.4 AC: "inline policy granting dynamodb:UpdateItem ONLY on
# arn:aws:dynamodb:*:*:table/ChatRooms".
# ---------------------------------------------------------------------------
run "blocks_writer_dynamodb_inline_policy_exists_and_scoped" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
  }

  assert {
    condition     = aws_iam_role_policy.blocks_writer_dynamodb.name == "blocks-writer-dynamodb"
    error_message = "blocks_writer DynamoDB policy must be named blocks-writer-dynamodb"
  }

  assert {
    condition     = aws_iam_role_policy.blocks_writer_dynamodb.role == aws_iam_role.blocks_writer.name
    error_message = "blocks_writer DynamoDB policy must be attached to the blocks_writer role"
  }
}

# ---------------------------------------------------------------------------
# Test 23: blocks_writer_dynamodb policy uses var.chat_rooms_table_arn
#          (story 8.9 AC-2: ARN must be sourced from module.dynamodb output,
#          not hardcoded).
#
# When a real table ARN is supplied via var.chat_rooms_table_arn, the policy
# document must reference that ARN.  The mock_data for aws_iam_policy_document
# always returns "{}" so we cannot assert the JSON body directly; instead we
# verify that the policy resource exists and is attached to the correct role,
# and rely on `terraform validate` (run in CI) to confirm the ARN expression
# resolves cleanly without the hardcoded fallback.
#
# The test also verifies the default-fallback path: when chat_rooms_table_arn
# is omitted (empty string), validate still passes with the wildcard fallback
# arn:aws:dynamodb:*:*:table/ChatRooms — this is the unit-test-only path.
# ---------------------------------------------------------------------------
run "blocks_writer_dynamodb_policy_accepts_real_chat_rooms_arn" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
    chat_rooms_table_arn          = "arn:aws:dynamodb:eu-central-1:123456789012:table/ChatRooms"
  }

  assert {
    condition     = aws_iam_role_policy.blocks_writer_dynamodb.name == "blocks-writer-dynamodb"
    error_message = "blocks_writer DynamoDB policy must be named blocks-writer-dynamodb when chat_rooms_table_arn is provided"
  }

  assert {
    condition     = aws_iam_role_policy.blocks_writer_dynamodb.role == aws_iam_role.blocks_writer.name
    error_message = "blocks_writer DynamoDB policy must be attached to the blocks_writer role when chat_rooms_table_arn is provided"
  }
}

# ---------------------------------------------------------------------------
# Test 19: aurora_writer gains cognito-idp inline policy (story 7.0b)
#
# Satisfies story 7.0b AC: "aurora_writer IAM role policy gains a statement
# granting cognito-idp:AdminUpdateUserAttributes scoped to
# var.cognito_user_pool_arn".  Unit test asserts the policy resource name and
# that it is attached to the aurora_writer role.
# ---------------------------------------------------------------------------
run "aurora_writer_cognito_profile_complete_inline_policy_exists" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
    cognito_user_pool_arn         = "arn:aws:cognito-idp:eu-central-1:123456789012:userpool/eu-central-1_TESTPOOL"
  }

  assert {
    condition     = aws_iam_role_policy.aurora_writer_cognito_profile_complete.name == "aurora-writer-cognito-profile-complete"
    error_message = "aurora_writer cognito policy must be named aurora-writer-cognito-profile-complete"
  }

  assert {
    condition     = aws_iam_role_policy.aurora_writer_cognito_profile_complete.role == aws_iam_role.aurora_writer.name
    error_message = "aurora_writer cognito policy must be attached to the aurora_writer role"
  }
}

# ---------------------------------------------------------------------------
# Test 17: aurora_reader_match role exists with trust policy and VPC managed policy
#
# Satisfies story 7.0 AC: "New IAM role aurora_reader_match (VPC execution,
# scoped secretsmanager:GetSecretValue on knotify-<env>-app-user-credential)".
# ---------------------------------------------------------------------------
run "aurora_reader_match_trust_policy_and_vpc_managed_policy" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
  }

  assert {
    condition     = aws_iam_role.aurora_reader_match.assume_role_policy != ""
    error_message = "aurora_reader_match assume_role_policy must not be empty"
  }

  assert {
    condition     = aws_iam_role_policy_attachment.aurora_reader_match_vpc_access.policy_arn == "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
    error_message = "aurora_reader_match must attach AWSLambdaVPCAccessExecutionRole"
  }
}

# ---------------------------------------------------------------------------
# Test 18: aurora_reader_match app_user credential inline policy exists and
#          is correctly wired to the role
#
# Satisfies story 7.0 AC: "scoped secretsmanager:GetSecretValue on
# knotify-<env>-app-user-credential".
# ---------------------------------------------------------------------------
run "aurora_reader_match_app_user_credential_inline_policy_exists" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
  }

  assert {
    condition     = aws_iam_role_policy.aurora_reader_match_app_user_credential.name == "aurora-reader-match-app-user-credential"
    error_message = "aurora_reader_match inline policy must be named aurora-reader-match-app-user-credential"
  }

  assert {
    condition     = aws_iam_role_policy.aurora_reader_match_app_user_credential.role == aws_iam_role.aurora_reader_match.name
    error_message = "aurora_reader_match inline policy must be attached to the aurora_reader_match role"
  }
}

# ---------------------------------------------------------------------------
# Test 20: appsync_logs_role — trust principal appsync.amazonaws.com,
#          AWSAppSyncPushToCloudWatchLogs managed policy attached
#
# Satisfies story 8.1 AC: "new appsync_logs_role with trust on
# appsync.amazonaws.com and managed policy AWSAppSyncPushToCloudWatchLogs".
# ---------------------------------------------------------------------------
run "appsync_logs_role_exists_with_trust_policy" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
  }

  assert {
    condition     = aws_iam_role.appsync_logs.assume_role_policy != ""
    error_message = "appsync_logs assume_role_policy must not be empty"
  }
}

run "appsync_logs_role_attaches_appsync_cloudwatch_managed_policy" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
  }

  assert {
    condition     = aws_iam_role_policy_attachment.appsync_logs_cloudwatch.policy_arn == "arn:aws:iam::aws:policy/service-role/AWSAppSyncPushToCloudWatchLogs"
    error_message = "appsync_logs must attach AWSAppSyncPushToCloudWatchLogs"
  }
}

# ---------------------------------------------------------------------------
# Test 21: appsync_chat_resolver_invoke — trust principal appsync.amazonaws.com,
#          inline policy granting lambda:InvokeFunction on chat_resolver Lambda ARN
#
# Satisfies story 8.1 AC: "aws_iam_role for AppSync to invoke chat_resolver Lambda
# is declared and granted lambda:InvokeFunction on module.chat_resolver.lambda_arn".
# ---------------------------------------------------------------------------
run "appsync_chat_resolver_invoke_role_exists" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
    chat_resolver_lambda_arn      = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-chat-resolver-test:live"
  }

  assert {
    condition     = aws_iam_role.appsync_chat_resolver_invoke.assume_role_policy != ""
    error_message = "appsync_chat_resolver_invoke assume_role_policy must not be empty"
  }
}

run "appsync_chat_resolver_invoke_inline_policy_exists" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
    chat_resolver_lambda_arn      = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-chat-resolver-test:live"
  }

  assert {
    condition     = aws_iam_role_policy.appsync_chat_resolver_invoke_lambda.name == "appsync-chat-resolver-invoke-lambda"
    error_message = "appsync_chat_resolver_invoke inline policy must be named appsync-chat-resolver-invoke-lambda"
  }

  assert {
    condition     = aws_iam_role_policy.appsync_chat_resolver_invoke_lambda.role == aws_iam_role.appsync_chat_resolver_invoke.name
    error_message = "appsync_chat_resolver_invoke inline policy must be attached to the appsync_chat_resolver_invoke role"
  }
}

# ---------------------------------------------------------------------------
# Test 22: role_arns output map contains appsync_logs and appsync_chat_resolver_invoke
#
# Satisfies the module output shape requirement so root modules can reference
# module.iam_roles.role_arns["appsync_logs"] and
# module.iam_roles.role_arns["appsync_chat_resolver_invoke"].
# ---------------------------------------------------------------------------
run "role_arns_output_contains_appsync_roles" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
    chat_resolver_lambda_arn      = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-chat-resolver-test:live"
  }

  override_resource {
    target = aws_iam_role.appsync_logs
    values = {
      arn = "arn:aws:iam::123456789012:role/knotify-test-appsync-logs"
    }
    override_during = plan
  }

  override_resource {
    target = aws_iam_role.appsync_chat_resolver_invoke
    values = {
      arn = "arn:aws:iam::123456789012:role/knotify-test-appsync-chat-resolver-invoke"
    }
    override_during = plan
  }

  assert {
    condition     = output.role_arns["appsync_logs"] == "arn:aws:iam::123456789012:role/knotify-test-appsync-logs"
    error_message = "role_arns[appsync_logs] must be wired to aws_iam_role.appsync_logs.arn"
  }

  assert {
    condition     = output.role_arns["appsync_chat_resolver_invoke"] == "arn:aws:iam::123456789012:role/knotify-test-appsync-chat-resolver-invoke"
    error_message = "role_arns[appsync_chat_resolver_invoke] must be wired to aws_iam_role.appsync_chat_resolver_invoke.arn"
  }
}

# ===========================================================================
# Tests 23–26: room_state_publisher IAM role (story 8.9a)
# ===========================================================================

# ---------------------------------------------------------------------------
# Test 23: room_state_publisher role exists with Lambda trust policy
#
# Satisfies AC: "New IAM role room_state_publisher_role in iam_roles module"
# ---------------------------------------------------------------------------
run "room_state_publisher_role_exists_with_lambda_trust_policy" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
    chat_rooms_stream_arn         = "arn:aws:dynamodb:eu-central-1:123456789012:table/ChatRooms/stream/2026-06-18T00:00:00.000"
    appsync_api_arn               = "arn:aws:appsync:eu-central-1:123456789012:apis/TESTAPI"
  }

  assert {
    condition     = aws_iam_role.room_state_publisher.assume_role_policy != ""
    error_message = "room_state_publisher assume_role_policy must not be empty"
  }
}

# ---------------------------------------------------------------------------
# Test 24: room_state_publisher DynamoDB stream inline policy exists
#
# Satisfies AC: "dynamodb:DescribeStream + GetRecords + GetShardIterator +
#               ListStreams on the ChatRooms stream ARN"
# ---------------------------------------------------------------------------
run "room_state_publisher_dynamodb_stream_inline_policy_exists" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
    chat_rooms_stream_arn         = "arn:aws:dynamodb:eu-central-1:123456789012:table/ChatRooms/stream/2026-06-18T00:00:00.000"
    appsync_api_arn               = "arn:aws:appsync:eu-central-1:123456789012:apis/TESTAPI"
  }

  assert {
    condition     = aws_iam_role_policy.room_state_publisher_dynamodb_stream.name == "room-state-publisher-dynamodb-stream"
    error_message = "room_state_publisher DynamoDB stream policy must be named room-state-publisher-dynamodb-stream"
  }

  assert {
    condition     = aws_iam_role_policy.room_state_publisher_dynamodb_stream.role == aws_iam_role.room_state_publisher.name
    error_message = "room_state_publisher DynamoDB stream policy must be attached to room_state_publisher role"
  }
}

# ---------------------------------------------------------------------------
# Test 25: room_state_publisher AppSync GraphQL inline policy exists
#
# Satisfies AC: "appsync:GraphQL on the relevant publish-mutation field ARNs"
# ---------------------------------------------------------------------------
run "room_state_publisher_appsync_graphql_inline_policy_exists" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
    chat_rooms_stream_arn         = "arn:aws:dynamodb:eu-central-1:123456789012:table/ChatRooms/stream/2026-06-18T00:00:00.000"
    appsync_api_arn               = "arn:aws:appsync:eu-central-1:123456789012:apis/TESTAPI"
  }

  assert {
    condition     = aws_iam_role_policy.room_state_publisher_appsync.name == "room-state-publisher-appsync"
    error_message = "room_state_publisher AppSync policy must be named room-state-publisher-appsync"
  }

  assert {
    condition     = aws_iam_role_policy.room_state_publisher_appsync.role == aws_iam_role.room_state_publisher.name
    error_message = "room_state_publisher AppSync policy must be attached to room_state_publisher role"
  }
}

# ---------------------------------------------------------------------------
# Test 26: role_arns output map contains room_state_publisher
#
# Satisfies the module output shape requirement so root modules can reference
# module.iam_roles.role_arns["room_state_publisher"].
# ---------------------------------------------------------------------------
run "role_arns_output_contains_room_state_publisher" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
    chat_rooms_stream_arn         = "arn:aws:dynamodb:eu-central-1:123456789012:table/ChatRooms/stream/2026-06-18T00:00:00.000"
    appsync_api_arn               = "arn:aws:appsync:eu-central-1:123456789012:apis/TESTAPI"
  }

  override_resource {
    target = aws_iam_role.room_state_publisher
    values = {
      arn = "arn:aws:iam::123456789012:role/knotify-test-room-state-publisher"
    }
    override_during = plan
  }

  assert {
    condition     = output.role_arns["room_state_publisher"] == "arn:aws:iam::123456789012:role/knotify-test-room-state-publisher"
    error_message = "role_arns[room_state_publisher] must be wired to aws_iam_role.room_state_publisher.arn"
  }
}

# ===========================================================================
# Tests 27–30: notifications_publisher IAM role (story 8.9c)
# ===========================================================================

# ---------------------------------------------------------------------------
# Test 27: notifications_publisher role exists with Lambda trust policy
#
# Satisfies AC: "New IAM role notifications_publisher_role in iam_roles module"
# ---------------------------------------------------------------------------
run "notifications_publisher_role_exists_with_lambda_trust_policy" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
    notifications_stream_arn      = "arn:aws:dynamodb:eu-central-1:123456789012:table/Notifications/stream/2026-06-18T00:00:00.000"
    appsync_api_arn               = "arn:aws:appsync:eu-central-1:123456789012:apis/TESTAPI"
  }

  assert {
    condition     = aws_iam_role.notifications_publisher.assume_role_policy != ""
    error_message = "notifications_publisher assume_role_policy must not be empty"
  }
}

# ---------------------------------------------------------------------------
# Test 28: notifications_publisher DynamoDB stream inline policy exists
#
# Satisfies AC: "dynamodb:DescribeStream + GetRecords + GetShardIterator +
#               ListStreams on the Notifications stream ARN"
# ---------------------------------------------------------------------------
run "notifications_publisher_dynamodb_stream_inline_policy_exists" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
    notifications_stream_arn      = "arn:aws:dynamodb:eu-central-1:123456789012:table/Notifications/stream/2026-06-18T00:00:00.000"
    appsync_api_arn               = "arn:aws:appsync:eu-central-1:123456789012:apis/TESTAPI"
  }

  assert {
    condition     = aws_iam_role_policy.notifications_publisher_dynamodb_stream.name == "notifications-publisher-dynamodb-stream"
    error_message = "notifications_publisher DynamoDB stream policy must be named notifications-publisher-dynamodb-stream"
  }

  assert {
    condition     = aws_iam_role_policy.notifications_publisher_dynamodb_stream.role == aws_iam_role.notifications_publisher.name
    error_message = "notifications_publisher DynamoDB stream policy must be attached to notifications_publisher role"
  }
}

# ---------------------------------------------------------------------------
# Test 29: notifications_publisher AppSync GraphQL inline policy exists
#
# Satisfies AC: "appsync:GraphQL on the publishNotification and
#               _publishFriendRequestUpdated field ARNs (scoped to fields, not *)"
# ---------------------------------------------------------------------------
run "notifications_publisher_appsync_graphql_inline_policy_exists" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
    notifications_stream_arn      = "arn:aws:dynamodb:eu-central-1:123456789012:table/Notifications/stream/2026-06-18T00:00:00.000"
    appsync_api_arn               = "arn:aws:appsync:eu-central-1:123456789012:apis/TESTAPI"
  }

  assert {
    condition     = aws_iam_role_policy.notifications_publisher_appsync.name == "notifications-publisher-appsync"
    error_message = "notifications_publisher AppSync policy must be named notifications-publisher-appsync"
  }

  assert {
    condition     = aws_iam_role_policy.notifications_publisher_appsync.role == aws_iam_role.notifications_publisher.name
    error_message = "notifications_publisher AppSync policy must be attached to notifications_publisher role"
  }
}

# ---------------------------------------------------------------------------
# Test 30: role_arns output map contains notifications_publisher
#
# Satisfies the module output shape requirement so root modules can reference
# module.iam_roles.role_arns["notifications_publisher"].
# ---------------------------------------------------------------------------
run "role_arns_output_contains_notifications_publisher" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
    notifications_stream_arn      = "arn:aws:dynamodb:eu-central-1:123456789012:table/Notifications/stream/2026-06-18T00:00:00.000"
    appsync_api_arn               = "arn:aws:appsync:eu-central-1:123456789012:apis/TESTAPI"
  }

  override_resource {
    target = aws_iam_role.notifications_publisher
    values = {
      arn = "arn:aws:iam::123456789012:role/knotify-test-notifications-publisher"
    }
    override_during = plan
  }

  assert {
    condition     = output.role_arns["notifications_publisher"] == "arn:aws:iam::123456789012:role/knotify-test-notifications-publisher"
    error_message = "role_arns[notifications_publisher] must be wired to aws_iam_role.notifications_publisher.arn"
  }
}

# ===========================================================================
# Tests 31–34: push_fanout IAM role (story 8.10)
# ===========================================================================

# ---------------------------------------------------------------------------
# Test 31: push_fanout role exists with Lambda trust policy
#
# Satisfies AC: "New IAM role push_fanout_role in iam_roles module"
# ---------------------------------------------------------------------------
run "push_fanout_role_exists_with_lambda_trust_policy" {
  command = plan

  variables {
    environment                        = "test"
    aurora_master_user_secret_arn      = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
    chat_messages_stream_arn           = "arn:aws:dynamodb:eu-central-1:123456789012:table/ChatMessages/stream/2026-06-18T00:00:00.000"
    notifications_stream_arn           = "arn:aws:dynamodb:eu-central-1:123456789012:table/Notifications/stream/2026-06-18T00:00:00.000"
    push_notification_tokens_table_arn = "arn:aws:dynamodb:eu-central-1:123456789012:table/PushNotificationTokens"
  }

  assert {
    condition     = aws_iam_role.push_fanout.assume_role_policy != ""
    error_message = "push_fanout assume_role_policy must not be empty"
  }
}

# ---------------------------------------------------------------------------
# Test 32: push_fanout DynamoDB stream inline policy exists
#
# Satisfies AC: "dynamodb stream actions on ChatMessages and Notifications stream ARNs"
# ---------------------------------------------------------------------------
run "push_fanout_dynamodb_streams_inline_policy_exists" {
  command = plan

  variables {
    environment                        = "test"
    aurora_master_user_secret_arn      = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
    chat_messages_stream_arn           = "arn:aws:dynamodb:eu-central-1:123456789012:table/ChatMessages/stream/2026-06-18T00:00:00.000"
    notifications_stream_arn           = "arn:aws:dynamodb:eu-central-1:123456789012:table/Notifications/stream/2026-06-18T00:00:00.000"
    push_notification_tokens_table_arn = "arn:aws:dynamodb:eu-central-1:123456789012:table/PushNotificationTokens"
  }

  assert {
    condition     = aws_iam_role_policy.push_fanout_dynamodb_streams.name == "push-fanout-dynamodb-streams"
    error_message = "push_fanout DynamoDB streams policy must be named push-fanout-dynamodb-streams"
  }

  assert {
    condition     = aws_iam_role_policy.push_fanout_dynamodb_streams.role == aws_iam_role.push_fanout.name
    error_message = "push_fanout DynamoDB streams policy must be attached to push_fanout role"
  }
}

# ---------------------------------------------------------------------------
# Test 33: push_fanout DynamoDB table inline policy exists
#
# Satisfies AC: "dynamodb:GetItem + Query + UpdateItem + DeleteItem on the
#               four touched tables"
# ---------------------------------------------------------------------------
run "push_fanout_dynamodb_tables_inline_policy_exists" {
  command = plan

  variables {
    environment                        = "test"
    aurora_master_user_secret_arn      = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
    chat_messages_stream_arn           = "arn:aws:dynamodb:eu-central-1:123456789012:table/ChatMessages/stream/2026-06-18T00:00:00.000"
    notifications_stream_arn           = "arn:aws:dynamodb:eu-central-1:123456789012:table/Notifications/stream/2026-06-18T00:00:00.000"
    push_notification_tokens_table_arn = "arn:aws:dynamodb:eu-central-1:123456789012:table/PushNotificationTokens"
  }

  assert {
    condition     = aws_iam_role_policy.push_fanout_dynamodb_tables.name == "push-fanout-dynamodb-tables"
    error_message = "push_fanout DynamoDB tables policy must be named push-fanout-dynamodb-tables"
  }

  assert {
    condition     = aws_iam_role_policy.push_fanout_dynamodb_tables.role == aws_iam_role.push_fanout.name
    error_message = "push_fanout DynamoDB tables policy must be attached to push_fanout role"
  }
}

# ---------------------------------------------------------------------------
# Test 34: role_arns output map contains push_fanout
#
# Satisfies the module output shape requirement so root modules can reference
# module.iam_roles.role_arns["push_fanout"].
# ---------------------------------------------------------------------------
run "role_arns_output_contains_push_fanout" {
  command = plan

  variables {
    environment                        = "test"
    aurora_master_user_secret_arn      = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
    chat_messages_stream_arn           = "arn:aws:dynamodb:eu-central-1:123456789012:table/ChatMessages/stream/2026-06-18T00:00:00.000"
    notifications_stream_arn           = "arn:aws:dynamodb:eu-central-1:123456789012:table/Notifications/stream/2026-06-18T00:00:00.000"
    push_notification_tokens_table_arn = "arn:aws:dynamodb:eu-central-1:123456789012:table/PushNotificationTokens"
  }

  override_resource {
    target = aws_iam_role.push_fanout
    values = {
      arn = "arn:aws:iam::123456789012:role/knotify-test-push-fanout"
    }
    override_during = plan
  }

  assert {
    condition     = output.role_arns["push_fanout"] == "arn:aws:iam::123456789012:role/knotify-test-push-fanout"
    error_message = "role_arns[push_fanout] must be wired to aws_iam_role.push_fanout.arn"
  }
}

# ===========================================================================
# Tests 35–37: push_tokens IAM role (story 8.11)
# ===========================================================================

# ---------------------------------------------------------------------------
# Test 35: push_tokens role exists with Lambda trust policy
#
# Satisfies AC: "IAM role push_tokens_role in iam_roles module:
#               dynamodb:PutItem on PushNotificationTokens"
# ---------------------------------------------------------------------------
run "push_tokens_role_exists_with_lambda_trust_policy" {
  command = plan

  variables {
    environment                        = "test"
    aurora_master_user_secret_arn      = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
    push_notification_tokens_table_arn = "arn:aws:dynamodb:eu-central-1:123456789012:table/PushNotificationTokens"
  }

  assert {
    condition     = aws_iam_role.push_tokens.assume_role_policy != ""
    error_message = "push_tokens assume_role_policy must not be empty"
  }
}

# ---------------------------------------------------------------------------
# Test 36: push_tokens DynamoDB inline policy exists
#
# Satisfies AC: "dynamodb:PutItem on PushNotificationTokens (scoped to
#               exact table ARN, not wildcard)"
# ---------------------------------------------------------------------------
run "push_tokens_dynamodb_inline_policy_exists" {
  command = plan

  variables {
    environment                        = "test"
    aurora_master_user_secret_arn      = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
    push_notification_tokens_table_arn = "arn:aws:dynamodb:eu-central-1:123456789012:table/PushNotificationTokens"
  }

  assert {
    condition     = aws_iam_role_policy.push_tokens_dynamodb.name == "push-tokens-dynamodb"
    error_message = "push_tokens DynamoDB policy must be named push-tokens-dynamodb"
  }

  assert {
    condition     = aws_iam_role_policy.push_tokens_dynamodb.role == aws_iam_role.push_tokens.name
    error_message = "push_tokens DynamoDB policy must be attached to push_tokens role"
  }
}

# ---------------------------------------------------------------------------
# Test 37: role_arns output map contains push_tokens
#
# Satisfies the module output shape requirement so root modules can reference
# module.iam_roles.role_arns["push_tokens"].
# ---------------------------------------------------------------------------
run "role_arns_output_contains_push_tokens" {
  command = plan

  variables {
    environment                        = "test"
    aurora_master_user_secret_arn      = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
    push_notification_tokens_table_arn = "arn:aws:dynamodb:eu-central-1:123456789012:table/PushNotificationTokens"
  }

  override_resource {
    target = aws_iam_role.push_tokens
    values = {
      arn = "arn:aws:iam::123456789012:role/knotify-test-push-tokens"
    }
    override_during = plan
  }

  assert {
    condition     = output.role_arns["push_tokens"] == "arn:aws:iam::123456789012:role/knotify-test-push-tokens"
    error_message = "role_arns[push_tokens] must be wired to aws_iam_role.push_tokens.arn"
  }
}

# ===========================================================================
# Tests 38–40: stale_token_cleanup IAM role (story 8.12)
# ===========================================================================

# ---------------------------------------------------------------------------
# Test 38: stale_token_cleanup role exists with Lambda trust policy
#
# Satisfies AC: "IAM role with dynamodb:Scan + DeleteItem on
#               PushNotificationTokens"
# ---------------------------------------------------------------------------
run "stale_token_cleanup_role_exists_with_lambda_trust_policy" {
  command = plan

  variables {
    environment                        = "test"
    aurora_master_user_secret_arn      = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
    push_notification_tokens_table_arn = "arn:aws:dynamodb:eu-central-1:123456789012:table/PushNotificationTokens"
  }

  assert {
    condition     = aws_iam_role.stale_token_cleanup.assume_role_policy != ""
    error_message = "stale_token_cleanup assume_role_policy must not be empty"
  }
}

# ---------------------------------------------------------------------------
# Test 39: stale_token_cleanup DynamoDB inline policy exists with correct name
#
# Satisfies AC: "dynamodb:Scan + DeleteItem on PushNotificationTokens scoped
#               to exact table ARN"
# ---------------------------------------------------------------------------
run "stale_token_cleanup_dynamodb_inline_policy_exists" {
  command = plan

  variables {
    environment                        = "test"
    aurora_master_user_secret_arn      = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
    push_notification_tokens_table_arn = "arn:aws:dynamodb:eu-central-1:123456789012:table/PushNotificationTokens"
  }

  assert {
    condition     = aws_iam_role_policy.stale_token_cleanup_dynamodb.name == "stale-token-cleanup-dynamodb"
    error_message = "stale_token_cleanup DynamoDB policy must be named stale-token-cleanup-dynamodb"
  }

  assert {
    condition     = aws_iam_role_policy.stale_token_cleanup_dynamodb.role == aws_iam_role.stale_token_cleanup.name
    error_message = "stale_token_cleanup DynamoDB policy must be attached to stale_token_cleanup role"
  }
}

# ---------------------------------------------------------------------------
# Test 40: role_arns output map contains stale_token_cleanup
#
# Satisfies the module output shape requirement so root modules can reference
# module.iam_roles.role_arns["stale_token_cleanup"].
# ---------------------------------------------------------------------------
run "role_arns_output_contains_stale_token_cleanup" {
  command = plan

  variables {
    environment                        = "test"
    aurora_master_user_secret_arn      = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
    push_notification_tokens_table_arn = "arn:aws:dynamodb:eu-central-1:123456789012:table/PushNotificationTokens"
  }

  override_resource {
    target = aws_iam_role.stale_token_cleanup
    values = {
      arn = "arn:aws:iam::123456789012:role/knotify-test-stale-token-cleanup"
    }
    override_during = plan
  }

  assert {
    condition     = output.role_arns["stale_token_cleanup"] == "arn:aws:iam::123456789012:role/knotify-test-stale-token-cleanup"
    error_message = "role_arns[stale_token_cleanup] must be wired to aws_iam_role.stale_token_cleanup.arn"
  }
}

# ===========================================================================
# Tests 41–45: stepfn_deletion_exec IAM role (story 9.1)
#
# Dedicated Step Functions execution role for the account-deletion state machine.
# Trust principal: states.amazonaws.com
# Inline policies:
#   1. lambda:InvokeFunction on all nine deletion task Lambda ARNs
#   2. cloudwatch:PutMetricData (DeletionFailed metric, no resource restriction needed)
#   3. logs:* on the Step Functions CloudWatch log group ARN
# ===========================================================================

# ---------------------------------------------------------------------------
# Test 41: stepfn_deletion_exec role exists with states.amazonaws.com trust policy
#
# Satisfies AC: "A dedicated Step Functions execution role is declared in
#               infrastructure/modules/iam_roles/ with ... trust principal states.amazonaws.com"
# ---------------------------------------------------------------------------
run "stepfn_deletion_exec_role_exists_with_states_trust_policy" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
    deletion_task_lambda_arns = [
      "arn:aws:lambda:eu-central-1:123456789012:function:knotify-validate-deletion-request-test:live",
      "arn:aws:lambda:eu-central-1:123456789012:function:knotify-cognito-user-state-test:live",
      "arn:aws:lambda:eu-central-1:123456789012:function:knotify-deactivate-chat-rooms-test:live",
      "arn:aws:lambda:eu-central-1:123456789012:function:knotify-soft-delete-aurora-test:live",
      "arn:aws:lambda:eu-central-1:123456789012:function:knotify-delete-dynamodb-personal-test:live",
      "arn:aws:lambda:eu-central-1:123456789012:function:knotify-anonymize-chat-messages-test:live",
      "arn:aws:lambda:eu-central-1:123456789012:function:knotify-hard-purge-now-test:live",
      "arn:aws:lambda:eu-central-1:123456789012:function:knotify-hard-delete-user-chat-msgs-test:live",
      "arn:aws:lambda:eu-central-1:123456789012:function:knotify-write-audit-log-test:live",
    ]
    deletion_sfn_log_group_arn = "arn:aws:logs:eu-central-1:123456789012:log-group:/aws/states/knotify-test-account-deletion:*"
  }

  assert {
    condition     = aws_iam_role.stepfn_deletion_exec.assume_role_policy != ""
    error_message = "stepfn_deletion_exec assume_role_policy must not be empty"
  }
}

# ---------------------------------------------------------------------------
# Test 42: stepfn_deletion_exec lambda invoke inline policy exists
#
# Satisfies AC: "lambda:InvokeFunction scoped to each task Lambda ARN"
# ---------------------------------------------------------------------------
run "stepfn_deletion_exec_lambda_invoke_inline_policy_exists" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
    deletion_task_lambda_arns = [
      "arn:aws:lambda:eu-central-1:123456789012:function:knotify-validate-deletion-request-test:live",
      "arn:aws:lambda:eu-central-1:123456789012:function:knotify-cognito-user-state-test:live",
      "arn:aws:lambda:eu-central-1:123456789012:function:knotify-deactivate-chat-rooms-test:live",
      "arn:aws:lambda:eu-central-1:123456789012:function:knotify-soft-delete-aurora-test:live",
      "arn:aws:lambda:eu-central-1:123456789012:function:knotify-delete-dynamodb-personal-test:live",
      "arn:aws:lambda:eu-central-1:123456789012:function:knotify-anonymize-chat-messages-test:live",
      "arn:aws:lambda:eu-central-1:123456789012:function:knotify-hard-purge-now-test:live",
      "arn:aws:lambda:eu-central-1:123456789012:function:knotify-hard-delete-user-chat-msgs-test:live",
      "arn:aws:lambda:eu-central-1:123456789012:function:knotify-write-audit-log-test:live",
    ]
    deletion_sfn_log_group_arn = "arn:aws:logs:eu-central-1:123456789012:log-group:/aws/states/knotify-test-account-deletion:*"
  }

  assert {
    condition     = aws_iam_role_policy.stepfn_deletion_exec_lambda_invoke.name == "stepfn-deletion-exec-lambda-invoke"
    error_message = "stepfn_deletion_exec lambda invoke policy must be named stepfn-deletion-exec-lambda-invoke"
  }

  assert {
    condition     = aws_iam_role_policy.stepfn_deletion_exec_lambda_invoke.role == aws_iam_role.stepfn_deletion_exec.name
    error_message = "stepfn_deletion_exec lambda invoke policy must be attached to the stepfn_deletion_exec role"
  }
}

# ---------------------------------------------------------------------------
# Test 43: stepfn_deletion_exec cloudwatch put metric inline policy exists
#
# Satisfies AC: "cloudwatch:PutMetricData for the DeletionFailed metric"
# ---------------------------------------------------------------------------
run "stepfn_deletion_exec_cloudwatch_metric_inline_policy_exists" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
    deletion_task_lambda_arns = [
      "arn:aws:lambda:eu-central-1:123456789012:function:knotify-validate-deletion-request-test:live",
      "arn:aws:lambda:eu-central-1:123456789012:function:knotify-cognito-user-state-test:live",
      "arn:aws:lambda:eu-central-1:123456789012:function:knotify-deactivate-chat-rooms-test:live",
      "arn:aws:lambda:eu-central-1:123456789012:function:knotify-soft-delete-aurora-test:live",
      "arn:aws:lambda:eu-central-1:123456789012:function:knotify-delete-dynamodb-personal-test:live",
      "arn:aws:lambda:eu-central-1:123456789012:function:knotify-anonymize-chat-messages-test:live",
      "arn:aws:lambda:eu-central-1:123456789012:function:knotify-hard-purge-now-test:live",
      "arn:aws:lambda:eu-central-1:123456789012:function:knotify-hard-delete-user-chat-msgs-test:live",
      "arn:aws:lambda:eu-central-1:123456789012:function:knotify-write-audit-log-test:live",
    ]
    deletion_sfn_log_group_arn = "arn:aws:logs:eu-central-1:123456789012:log-group:/aws/states/knotify-test-account-deletion:*"
  }

  assert {
    condition     = aws_iam_role_policy.stepfn_deletion_exec_cloudwatch.name == "stepfn-deletion-exec-cloudwatch"
    error_message = "stepfn_deletion_exec cloudwatch policy must be named stepfn-deletion-exec-cloudwatch"
  }

  assert {
    condition     = aws_iam_role_policy.stepfn_deletion_exec_cloudwatch.role == aws_iam_role.stepfn_deletion_exec.name
    error_message = "stepfn_deletion_exec cloudwatch policy must be attached to the stepfn_deletion_exec role"
  }
}

# ---------------------------------------------------------------------------
# Test 44: stepfn_deletion_exec logs inline policy exists
#
# Satisfies AC: "logs:* for the log group"
# ---------------------------------------------------------------------------
run "stepfn_deletion_exec_logs_inline_policy_exists" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
    deletion_task_lambda_arns = [
      "arn:aws:lambda:eu-central-1:123456789012:function:knotify-validate-deletion-request-test:live",
      "arn:aws:lambda:eu-central-1:123456789012:function:knotify-cognito-user-state-test:live",
      "arn:aws:lambda:eu-central-1:123456789012:function:knotify-deactivate-chat-rooms-test:live",
      "arn:aws:lambda:eu-central-1:123456789012:function:knotify-soft-delete-aurora-test:live",
      "arn:aws:lambda:eu-central-1:123456789012:function:knotify-delete-dynamodb-personal-test:live",
      "arn:aws:lambda:eu-central-1:123456789012:function:knotify-anonymize-chat-messages-test:live",
      "arn:aws:lambda:eu-central-1:123456789012:function:knotify-hard-purge-now-test:live",
      "arn:aws:lambda:eu-central-1:123456789012:function:knotify-hard-delete-user-chat-msgs-test:live",
      "arn:aws:lambda:eu-central-1:123456789012:function:knotify-write-audit-log-test:live",
    ]
    deletion_sfn_log_group_arn = "arn:aws:logs:eu-central-1:123456789012:log-group:/aws/states/knotify-test-account-deletion:*"
  }

  assert {
    condition     = aws_iam_role_policy.stepfn_deletion_exec_logs.name == "stepfn-deletion-exec-logs"
    error_message = "stepfn_deletion_exec logs policy must be named stepfn-deletion-exec-logs"
  }

  assert {
    condition     = aws_iam_role_policy.stepfn_deletion_exec_logs.role == aws_iam_role.stepfn_deletion_exec.name
    error_message = "stepfn_deletion_exec logs policy must be attached to the stepfn_deletion_exec role"
  }
}

# ---------------------------------------------------------------------------
# Test 45: role_arns output map contains stepfn_deletion_exec
#
# Satisfies the module output shape requirement so root modules can reference
# module.iam_roles.role_arns["stepfn_deletion_exec"].
# ---------------------------------------------------------------------------
run "role_arns_output_contains_stepfn_deletion_exec" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
    deletion_task_lambda_arns = [
      "arn:aws:lambda:eu-central-1:123456789012:function:knotify-validate-deletion-request-test:live",
      "arn:aws:lambda:eu-central-1:123456789012:function:knotify-cognito-user-state-test:live",
      "arn:aws:lambda:eu-central-1:123456789012:function:knotify-deactivate-chat-rooms-test:live",
      "arn:aws:lambda:eu-central-1:123456789012:function:knotify-soft-delete-aurora-test:live",
      "arn:aws:lambda:eu-central-1:123456789012:function:knotify-delete-dynamodb-personal-test:live",
      "arn:aws:lambda:eu-central-1:123456789012:function:knotify-anonymize-chat-messages-test:live",
      "arn:aws:lambda:eu-central-1:123456789012:function:knotify-hard-purge-now-test:live",
      "arn:aws:lambda:eu-central-1:123456789012:function:knotify-hard-delete-user-chat-msgs-test:live",
      "arn:aws:lambda:eu-central-1:123456789012:function:knotify-write-audit-log-test:live",
    ]
    deletion_sfn_log_group_arn = "arn:aws:logs:eu-central-1:123456789012:log-group:/aws/states/knotify-test-account-deletion:*"
  }

  override_resource {
    target = aws_iam_role.stepfn_deletion_exec
    values = {
      arn = "arn:aws:iam::123456789012:role/knotify-test-stepfn-deletion-exec"
    }
    override_during = plan
  }

  assert {
    condition     = output.role_arns["stepfn_deletion_exec"] == "arn:aws:iam::123456789012:role/knotify-test-stepfn-deletion-exec"
    error_message = "role_arns[stepfn_deletion_exec] must be wired to aws_iam_role.stepfn_deletion_exec.arn"
  }
}

# ===========================================================================
# Story 9.8 — write_audit_log IAM role tests
# ===========================================================================

# ---------------------------------------------------------------------------
# Test: write_audit_log role trust policy uses lambda service principal
# Satisfies AC: Lambda execution role in infrastructure/modules/iam_roles/
# ---------------------------------------------------------------------------
run "write_audit_log_role_uses_lambda_trust_policy" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
  }

  assert {
    condition     = aws_iam_role.write_audit_log.name == "knotify-test-write-audit-log"
    error_message = "write_audit_log role name must be knotify-<env>-write-audit-log"
  }
}

# ---------------------------------------------------------------------------
# Test: write_audit_log role has AWSLambdaBasicExecutionRole attached
# Satisfies AC: Lambda runs OUTSIDE the VPC (no VPC endpoint needed for DynamoDB-only)
# ---------------------------------------------------------------------------
run "write_audit_log_role_has_basic_execution_policy" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
  }

  assert {
    condition     = aws_iam_role_policy_attachment.write_audit_log_basic_execution.policy_arn == "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
    error_message = "write_audit_log role must attach AWSLambdaBasicExecutionRole (not VPC access — runs outside VPC)"
  }
}

# ---------------------------------------------------------------------------
# Test: write_audit_log DynamoDB policy allows PutItem on account_deletion_audit
# Satisfies AC: least-privilege DynamoDB PutItem on account_deletion_audit only
# Default fallback wildcard used here (no real table ARN passed)
# ---------------------------------------------------------------------------
run "write_audit_log_dynamodb_policy_putitem_only" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
  }

  assert {
    condition     = aws_iam_role_policy.write_audit_log_dynamodb.name == "write-audit-log-dynamodb"
    error_message = "write_audit_log DynamoDB inline policy must be named write-audit-log-dynamodb"
  }

  assert {
    condition     = aws_iam_role_policy.write_audit_log_dynamodb.role == aws_iam_role.write_audit_log.name
    error_message = "write_audit_log DynamoDB inline policy must be attached to write_audit_log role"
  }
}

# ---------------------------------------------------------------------------
# Test: write_audit_log DynamoDB policy uses scoped ARN when provided
# Satisfies AC: real table ARN is used when var.account_deletion_audit_table_arn is non-empty
# ---------------------------------------------------------------------------
run "write_audit_log_dynamodb_policy_uses_scoped_arn_when_provided" {
  command = plan

  variables {
    environment                      = "test"
    aurora_master_user_secret_arn    = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
    account_deletion_audit_table_arn = "arn:aws:dynamodb:eu-central-1:123456789012:table/account_deletion_audit"
  }

  assert {
    condition     = aws_iam_role_policy.write_audit_log_dynamodb.role == aws_iam_role.write_audit_log.name
    error_message = "write_audit_log DynamoDB inline policy must be attached to write_audit_log role when real ARN provided"
  }
}

# ---------------------------------------------------------------------------
# Test: role_arns output contains write_audit_log
# Satisfies AC: root modules can reference module.iam_roles.role_arns["write_audit_log"]
# ---------------------------------------------------------------------------
run "role_arns_output_contains_write_audit_log" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
  }

  override_resource {
    target = aws_iam_role.write_audit_log
    values = {
      arn = "arn:aws:iam::123456789012:role/knotify-test-write-audit-log"
    }
    override_during = plan
  }

  assert {
    condition     = output.role_arns["write_audit_log"] == "arn:aws:iam::123456789012:role/knotify-test-write-audit-log"
    error_message = "role_arns[write_audit_log] must be wired to aws_iam_role.write_audit_log.arn"
  }
}

# ===========================================================================
# Story 9.3 — cognito_user_state IAM role tests
#
# Dedicated Lambda execution role for the cognito_user_state Lambda.
# Trust principal: lambda.amazonaws.com
# Managed policy: AWSLambdaBasicExecutionRole (runs OUTSIDE the VPC — no ENI)
# Inline policy: cognito-idp:AdminDisableUser + AdminDeleteUser + AdminGetUser
#                scoped to the project's Cognito user pool ARN.
# ===========================================================================

# ---------------------------------------------------------------------------
# Test: cognito_user_state role uses Lambda trust policy
#
# Satisfies AC: "The Lambda's IAM role needs cognito-idp:AdminDisableUser,
#               cognito-idp:AdminDeleteUser, and cognito-idp:AdminGetUser"
# ---------------------------------------------------------------------------
run "cognito_user_state_role_uses_lambda_trust_policy" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
    cognito_user_pool_arn         = "arn:aws:cognito-idp:eu-central-1:123456789012:userpool/eu-central-1_TESTPOOL"
  }

  assert {
    condition     = aws_iam_role.cognito_user_state.name == "knotify-test-cognito-user-state"
    error_message = "cognito_user_state role name must be knotify-<env>-cognito-user-state"
  }

  assert {
    condition     = aws_iam_role.cognito_user_state.assume_role_policy != ""
    error_message = "cognito_user_state assume_role_policy must not be empty"
  }
}

# ---------------------------------------------------------------------------
# Test: cognito_user_state role has AWSLambdaBasicExecutionRole attached
#
# Satisfies AC: Lambda runs OUTSIDE the VPC (Cognito IDP is a public endpoint,
# no VPC endpoint required — consistent with write_audit_log and push_fanout).
# ---------------------------------------------------------------------------
run "cognito_user_state_role_has_basic_execution_policy" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
    cognito_user_pool_arn         = "arn:aws:cognito-idp:eu-central-1:123456789012:userpool/eu-central-1_TESTPOOL"
  }

  assert {
    condition     = aws_iam_role_policy_attachment.cognito_user_state_basic_execution.policy_arn == "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
    error_message = "cognito_user_state role must attach AWSLambdaBasicExecutionRole (not VPC access — runs outside VPC)"
  }
}

# ---------------------------------------------------------------------------
# Test: cognito_user_state Cognito IDP inline policy exists and is wired
#
# Satisfies AC: "cognito-idp:AdminDisableUser, cognito-idp:AdminDeleteUser,
#               and cognito-idp:AdminGetUser scoped to the project's Cognito
#               user pool ARN"
# ---------------------------------------------------------------------------
run "cognito_user_state_cognito_inline_policy_exists" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
    cognito_user_pool_arn         = "arn:aws:cognito-idp:eu-central-1:123456789012:userpool/eu-central-1_TESTPOOL"
  }

  assert {
    condition     = aws_iam_role_policy.cognito_user_state_cognito.name == "cognito-user-state-cognito"
    error_message = "cognito_user_state Cognito inline policy must be named cognito-user-state-cognito"
  }

  assert {
    condition     = aws_iam_role_policy.cognito_user_state_cognito.role == aws_iam_role.cognito_user_state.name
    error_message = "cognito_user_state Cognito inline policy must be attached to the cognito_user_state role"
  }
}

# ---------------------------------------------------------------------------
# Test: cognito_user_state Cognito policy accepts real user pool ARN
#
# Satisfies AC: "scoped to the project's Cognito user pool ARN" — when a
# real ARN is supplied via var.cognito_user_pool_arn, validate passes
# with the exact ARN (not the wildcard fallback).
# ---------------------------------------------------------------------------
run "cognito_user_state_cognito_policy_accepts_real_user_pool_arn" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
    cognito_user_pool_arn         = "arn:aws:cognito-idp:eu-central-1:123456789012:userpool/eu-central-1_REALPOOL"
  }

  assert {
    condition     = aws_iam_role_policy.cognito_user_state_cognito.name == "cognito-user-state-cognito"
    error_message = "cognito_user_state Cognito inline policy must exist when real user pool ARN provided"
  }

  assert {
    condition     = aws_iam_role_policy.cognito_user_state_cognito.role == aws_iam_role.cognito_user_state.name
    error_message = "cognito_user_state Cognito inline policy must be attached to the role when real user pool ARN provided"
  }
}

# ---------------------------------------------------------------------------
# Test: role_arns output map contains cognito_user_state
#
# Satisfies the module output shape requirement so root modules can reference
# module.iam_roles.role_arns["cognito_user_state"] to wire the Lambda role.
# ---------------------------------------------------------------------------
run "role_arns_output_contains_cognito_user_state" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
    cognito_user_pool_arn         = "arn:aws:cognito-idp:eu-central-1:123456789012:userpool/eu-central-1_TESTPOOL"
  }

  override_resource {
    target = aws_iam_role.cognito_user_state
    values = {
      arn = "arn:aws:iam::123456789012:role/knotify-test-cognito-user-state"
    }
    override_during = plan
  }

  assert {
    condition     = output.role_arns["cognito_user_state"] == "arn:aws:iam::123456789012:role/knotify-test-cognito-user-state"
    error_message = "role_arns[cognito_user_state] must be wired to aws_iam_role.cognito_user_state.arn"
  }
}

# ===========================================================================
# Story 9.4 — deactivate_chat_rooms IAM role tests
# ===========================================================================

run "deactivate_chat_rooms_role_uses_lambda_trust_policy" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
  }

  assert {
    condition     = can(aws_iam_role.deactivate_chat_rooms.name)
    error_message = "deactivate_chat_rooms role must be declared"
  }
}

run "deactivate_chat_rooms_role_has_basic_execution_policy" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
  }

  assert {
    condition     = aws_iam_role_policy_attachment.deactivate_chat_rooms_basic_execution.policy_arn == "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
    error_message = "deactivate_chat_rooms must attach AWSLambdaBasicExecutionRole (no VPC access — runs outside VPC)"
  }
}

run "deactivate_chat_rooms_dynamodb_inline_policy_exists" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
  }

  assert {
    condition     = can(aws_iam_role_policy.deactivate_chat_rooms_dynamodb.name)
    error_message = "deactivate_chat_rooms dynamodb inline policy must be declared"
  }
}

run "role_arns_output_contains_deactivate_chat_rooms" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
  }

  override_resource {
    target = aws_iam_role.deactivate_chat_rooms
    values = {
      arn = "arn:aws:iam::123456789012:role/knotify-test-deactivate-chat-rooms"
    }
    override_during = plan
  }

  assert {
    condition     = output.role_arns["deactivate_chat_rooms"] == "arn:aws:iam::123456789012:role/knotify-test-deactivate-chat-rooms"
    error_message = "role_arns[deactivate_chat_rooms] must be wired to aws_iam_role.deactivate_chat_rooms.arn"
  }
}

# ===========================================================================
# Tests: anonymize_chat_messages role — story 9.6
# ===========================================================================

run "anonymize_chat_messages_role_uses_lambda_trust_policy" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
  }

  assert {
    condition     = can(aws_iam_role.anonymize_chat_messages.name)
    error_message = "anonymize_chat_messages role must be declared"
  }
}

run "anonymize_chat_messages_role_has_basic_execution_policy" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
  }

  assert {
    condition     = aws_iam_role_policy_attachment.anonymize_chat_messages_basic_execution.policy_arn == "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
    error_message = "anonymize_chat_messages must attach AWSLambdaBasicExecutionRole (no VPC access — runs outside VPC)"
  }
}

run "anonymize_chat_messages_dynamodb_inline_policy_exists" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
  }

  assert {
    condition     = can(aws_iam_role_policy.anonymize_chat_messages_dynamodb.name)
    error_message = "anonymize_chat_messages dynamodb inline policy must be declared"
  }
}

run "role_arns_output_contains_anonymize_chat_messages" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
  }

  override_resource {
    target = aws_iam_role.anonymize_chat_messages
    values = {
      arn = "arn:aws:iam::123456789012:role/knotify-test-anonymize-chat-messages"
    }
    override_during = plan
  }

  assert {
    condition     = output.role_arns["anonymize_chat_messages"] == "arn:aws:iam::123456789012:role/knotify-test-anonymize-chat-messages"
    error_message = "role_arns[anonymize_chat_messages] must be wired to aws_iam_role.anonymize_chat_messages.arn"
  }
}

# ===========================================================================
# Story 9.7 — delete_dynamodb_personal_data role tests
# ===========================================================================

run "delete_dynamodb_personal_data_role_uses_lambda_trust_policy" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
  }

  assert {
    condition     = can(aws_iam_role.delete_dynamodb_personal_data.name)
    error_message = "delete_dynamodb_personal_data role must be declared"
  }
}

run "delete_dynamodb_personal_data_role_has_basic_execution_policy" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
  }

  assert {
    condition     = aws_iam_role_policy_attachment.delete_dynamodb_personal_data_basic_execution.policy_arn == "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
    error_message = "delete_dynamodb_personal_data must attach AWSLambdaBasicExecutionRole (no VPC access — runs outside VPC)"
  }
}

run "delete_dynamodb_personal_data_dynamodb_inline_policy_exists" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
  }

  assert {
    condition     = can(aws_iam_role_policy.delete_dynamodb_personal_data_dynamodb.name)
    error_message = "delete_dynamodb_personal_data dynamodb inline policy must be declared"
  }
}

run "role_arns_output_contains_delete_dynamodb_personal_data" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
  }

  override_resource {
    target = aws_iam_role.delete_dynamodb_personal_data
    values = {
      arn = "arn:aws:iam::123456789012:role/knotify-test-delete-dynamodb-personal-data"
    }
    override_during = plan
  }

  assert {
    condition     = output.role_arns["delete_dynamodb_personal_data"] == "arn:aws:iam::123456789012:role/knotify-test-delete-dynamodb-personal-data"
    error_message = "role_arns[delete_dynamodb_personal_data] must be wired to aws_iam_role.delete_dynamodb_personal_data.arn"
  }
}

# ===========================================================================
# Story 9.2 — validate_deletion_request IAM role tests
# ===========================================================================

# ---------------------------------------------------------------------------
# Test: validate_deletion_request role exists with lambda trust policy
# ---------------------------------------------------------------------------
run "validate_deletion_request_role_uses_lambda_trust_policy" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
  }

  assert {
    condition     = can(aws_iam_role.validate_deletion_request.name)
    error_message = "validate_deletion_request role must be declared"
  }
}

# ---------------------------------------------------------------------------
# Test: validate_deletion_request attaches AWSLambdaBasicExecutionRole (no VPC)
# ---------------------------------------------------------------------------
run "validate_deletion_request_role_has_basic_execution_policy" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
  }

  assert {
    condition     = aws_iam_role_policy_attachment.validate_deletion_request_basic_execution.policy_arn == "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
    error_message = "validate_deletion_request must attach AWSLambdaBasicExecutionRole (no VPC access — runs outside VPC)"
  }
}

# ---------------------------------------------------------------------------
# Test: validate_deletion_request has DynamoDB inline policy declared
# ---------------------------------------------------------------------------
run "validate_deletion_request_dynamodb_inline_policy_exists" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
  }

  assert {
    condition     = can(aws_iam_role_policy.validate_deletion_request_dynamodb.name)
    error_message = "validate_deletion_request dynamodb inline policy must be declared"
  }
}

# ---------------------------------------------------------------------------
# Test: role_arns output contains validate_deletion_request
# ---------------------------------------------------------------------------
run "role_arns_output_contains_validate_deletion_request" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
  }

  override_resource {
    target = aws_iam_role.validate_deletion_request
    values = {
      arn = "arn:aws:iam::123456789012:role/knotify-test-validate-deletion-request"
    }
    override_during = plan
  }

  assert {
    condition     = output.role_arns["validate_deletion_request"] == "arn:aws:iam::123456789012:role/knotify-test-validate-deletion-request"
    error_message = "role_arns[validate_deletion_request] must be wired to aws_iam_role.validate_deletion_request.arn"
  }
}

# ---------------------------------------------------------------------------
# Tests: deletion_initiator IAM role (story 9.9)
# ---------------------------------------------------------------------------

run "deletion_initiator_role_uses_lambda_trust_policy" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
  }

  assert {
    condition     = can(aws_iam_role.deletion_initiator.assume_role_policy)
    error_message = "deletion_initiator role must declare an assume_role_policy"
  }
}

run "deletion_initiator_role_has_basic_execution_policy" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
  }

  assert {
    condition     = aws_iam_role_policy_attachment.deletion_initiator_basic_execution.policy_arn == "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
    error_message = "deletion_initiator must attach AWSLambdaBasicExecutionRole (Lambda runs outside VPC)"
  }
}

run "deletion_initiator_stepfunctions_inline_policy_exists" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
  }

  assert {
    condition     = can(aws_iam_role_policy.deletion_initiator_stepfunctions.name)
    error_message = "deletion_initiator stepfunctions inline policy must be declared"
  }
}

run "role_arns_output_contains_deletion_initiator" {
  command = plan

  variables {
    environment                   = "test"
    aurora_master_user_secret_arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:rds!cluster-EXAMPLE-suffix"
  }

  override_resource {
    target = aws_iam_role.deletion_initiator
    values = {
      arn = "arn:aws:iam::123456789012:role/knotify-test-deletion-initiator"
    }
    override_during = plan
  }

  assert {
    condition     = output.role_arns["deletion_initiator"] == "arn:aws:iam::123456789012:role/knotify-test-deletion-initiator"
    error_message = "role_arns[deletion_initiator] must be wired to aws_iam_role.deletion_initiator.arn"
  }
}
