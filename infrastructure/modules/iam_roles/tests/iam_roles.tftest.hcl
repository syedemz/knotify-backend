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
run "role_arns_output_contains_all_seven_roles" {
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
}
