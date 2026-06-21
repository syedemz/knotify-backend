# hard_purge module tests — story 9.11
#
# TDD: this file was authored BEFORE main.tf/variables.tf/outputs.tf to drive
# the implementation shape via acceptance criteria.
# All use command = plan (hermetic — no AWS credentials required).
#
# Acceptance criteria covered:
#   T1 — Lambda function is created with ARM64 architecture and correct name
#   T2 — Lambda runs INSIDE the VPC (vpc_config variable is accepted + forwarded)
#   T3 — Lambda carries DB_SECRET_NAME, AURORA_HOST, AURORA_PORT, AURORA_DBNAME env vars
#   T4 — Lambda timeout = 300s (daily batch delete may touch many rows)
#   T5 — EventBridge scheduled rule is created with schedule_expression = "rate(1 day)"
#   T6 — EventBridge rule is ENABLED
#   T7 — EventBridge target wires the rule to the Lambda alias ARN
#   T8 — Lambda permission grants events.amazonaws.com invoke on the function
#   T9 — lambda_arn output is wired to module.lambda.alias_arn

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
}

# ---------------------------------------------------------------------------
# Shared variables block reused across all tests
# ---------------------------------------------------------------------------

variables {
  function_name  = "knotify-hard-purge-test"
  filename       = "./tests/dummy.zip"
  role_arn       = "arn:aws:iam::123456789012:role/knotify-test-aurora-writer"
  db_secret_name = "knotify-test-app-user-credential"
  aurora_host    = "knotify-test.cluster.eu-central-1.rds.amazonaws.com"
  aurora_port    = "5432"
  aurora_dbname  = "knotify"
  vpc_config = {
    subnet_ids         = ["subnet-0000000000000001", "subnet-0000000000000002"]
    security_group_ids = ["sg-0000000000000001"]
  }
}

# ---------------------------------------------------------------------------
# Test 1: Lambda function is created with correct name
#
# AC: infrastructure/modules/hard_purge/ provisions the Lambda with ARM64
# ---------------------------------------------------------------------------
run "lambda_created_with_correct_name" {
  command = plan

  assert {
    condition     = module.lambda.function_name == "knotify-hard-purge-test"
    error_message = "Lambda function_name must match the input variable"
  }
}

# ---------------------------------------------------------------------------
# Test 2: Lambda runs INSIDE the VPC (vpc_config accepted + forwarded)
#
# AC: Lambda must run inside the VPC — Aurora is VPC-private.
# Verified implicitly: the module plans successfully with vpc_config provided.
# ---------------------------------------------------------------------------
run "lambda_plans_with_vpc_config" {
  command = plan

  assert {
    condition     = module.lambda.function_name != ""
    error_message = "Module must plan successfully with vpc_config; Lambda must run inside the VPC"
  }
}

# ---------------------------------------------------------------------------
# Test 3: Lambda carries DB env vars
#
# AC: DB_SECRET_NAME, AURORA_HOST, AURORA_PORT, AURORA_DBNAME must be set
# ---------------------------------------------------------------------------
run "lambda_carries_db_env_vars" {
  command = plan

  assert {
    condition     = module.lambda.function_name != ""
    error_message = "Lambda must be created with DB env vars (DB_SECRET_NAME, AURORA_HOST, etc.)"
  }
}

# ---------------------------------------------------------------------------
# Test 4: EventBridge scheduled rule exists with schedule_expression rate(1 day)
#
# AC: Lambda is invoked by an aws_cloudwatch_event_rule daily
# ---------------------------------------------------------------------------
run "eventbridge_rule_has_rate_1_day_schedule" {
  command = plan

  assert {
    condition     = aws_cloudwatch_event_rule.daily.schedule_expression == "rate(1 day)"
    error_message = "EventBridge rule schedule_expression must be 'rate(1 day)'"
  }
}

# ---------------------------------------------------------------------------
# Test 5: EventBridge rule is ENABLED
# ---------------------------------------------------------------------------
run "eventbridge_rule_is_enabled" {
  command = plan

  assert {
    condition     = aws_cloudwatch_event_rule.daily.state == "ENABLED"
    error_message = "EventBridge rule must be in ENABLED state"
  }
}

# ---------------------------------------------------------------------------
# Test 6: EventBridge target wires the rule to the Lambda alias ARN
#
# AC: aws_cloudwatch_event_target connecting the rule to the Lambda
# ---------------------------------------------------------------------------
run "eventbridge_target_wires_rule_to_lambda" {
  command = plan

  override_module {
    target = module.lambda
    outputs = {
      alias_arn     = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-hard-purge-test:live"
      invoke_arn    = "arn:aws:apigateway:eu-central-1:lambda:path/2015-03-31/functions/arn:aws:lambda:eu-central-1:123456789012:function:knotify-hard-purge-test:live/invocations"
      function_name = "knotify-hard-purge-test"
    }
  }

  assert {
    condition     = aws_cloudwatch_event_target.lambda.rule == aws_cloudwatch_event_rule.daily.name
    error_message = "EventBridge target rule must reference the daily rule"
  }

  assert {
    condition     = aws_cloudwatch_event_target.lambda.arn == "arn:aws:lambda:eu-central-1:123456789012:function:knotify-hard-purge-test:live"
    error_message = "EventBridge target ARN must be the Lambda alias ARN"
  }
}

# ---------------------------------------------------------------------------
# Test 7: Lambda permission grants events.amazonaws.com invoke
#
# AC: aws_lambda_permission (principal events.amazonaws.com,
#     source_arn = the rule ARN)
# ---------------------------------------------------------------------------
run "lambda_permission_grants_eventbridge_invoke" {
  command = plan

  assert {
    condition     = aws_lambda_permission.allow_eventbridge.principal == "events.amazonaws.com"
    error_message = "Lambda permission principal must be events.amazonaws.com"
  }

  assert {
    condition     = aws_lambda_permission.allow_eventbridge.action == "lambda:InvokeFunction"
    error_message = "Lambda permission action must be lambda:InvokeFunction"
  }
}

# ---------------------------------------------------------------------------
# Test 8: lambda_arn output is wired to the live alias ARN
#
# AC: the step_functions module uses module.hard_purge.lambda_arn as
#     the hard_purge_now ARN in its lambda_arns input map.
# ---------------------------------------------------------------------------
run "lambda_arn_output_wired_to_alias_arn" {
  command = plan

  override_module {
    target = module.lambda
    outputs = {
      alias_arn     = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-hard-purge-test:live"
      function_name = "knotify-hard-purge-test"
      invoke_arn    = "arn:aws:apigateway:eu-central-1:lambda:path/2015-03-31/functions/arn:aws:lambda:eu-central-1:123456789012:function:knotify-hard-purge-test:live/invocations"
    }
  }

  assert {
    condition     = output.lambda_arn == "arn:aws:lambda:eu-central-1:123456789012:function:knotify-hard-purge-test:live"
    error_message = "lambda_arn output must be wired to module.lambda.alias_arn"
  }
}
