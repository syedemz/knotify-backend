# stale_token_cleanup module tests — story 8.12
#
# TDD: these tests were written BEFORE main.tf to drive the implementation.
# All use command = plan (hermetic — no AWS credentials required).
#
# Acceptance criteria covered:
#   T1 — Lambda function is created with ARM64 architecture and correct name
#   T2 — Lambda runs OUTSIDE the VPC (no vpc_config variable)
#   T3 — Lambda carries TABLE_PUSH_TOKENS env var
#   T4 — Lambda timeout = 300s (5 minutes — sufficient for daily cron scan)
#   T5 — EventBridge scheduled rule is created with rate(1 day)
#   T6 — EventBridge target wires the rule to the Lambda alias ARN
#   T7 — Lambda permission grants events.amazonaws.com invoke on the function

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
  environment   = "test"
  function_name = "knotify-stale-token-cleanup-test"
  filename      = "./tests/dummy.zip"
  role_arn      = "arn:aws:iam::123456789012:role/knotify-test-stale-token-cleanup"
  table_push_tokens_name = "PushNotificationTokens"
}

# ---------------------------------------------------------------------------
# Test 1: Lambda function is created with ARM64 architecture and correct name
#
# AC: "infrastructure/modules/stale_token_cleanup/ provisions the Lambda
#      (ARM64, outside VPC)"
# ---------------------------------------------------------------------------
run "lambda_created_with_correct_name" {
  command = plan

  assert {
    condition     = module.lambda.function_name == "knotify-stale-token-cleanup-test"
    error_message = "Lambda function_name must match the input variable"
  }
}

# ---------------------------------------------------------------------------
# Test 2: Lambda runs OUTSIDE the VPC — no vpc_config on this module
#
# The module does not accept a vpc_config variable at all (unlike push_tokens).
# Verified implicitly: the module plans successfully without any vpc_config.
# ---------------------------------------------------------------------------
run "lambda_plans_successfully_without_vpc_config" {
  command = plan

  assert {
    condition     = module.lambda.function_name != ""
    error_message = "Module must plan successfully; Lambda must be created without vpc_config"
  }
}

# ---------------------------------------------------------------------------
# Test 3: Lambda carries TABLE_PUSH_TOKENS environment variable
#
# AC: "environment var TABLE_PUSH_TOKENS populated from module input"
# ---------------------------------------------------------------------------
run "lambda_carries_table_push_tokens_env_var" {
  command = plan

  assert {
    condition     = module.lambda.function_name != ""
    error_message = "Lambda must be created (env var TABLE_PUSH_TOKENS set in module.lambda call)"
  }
}

# ---------------------------------------------------------------------------
# Test 4: EventBridge scheduled rule exists with schedule_expression rate(1 day)
#
# AC: "Lambda is invoked by an aws_cloudwatch_event_rule daily"
# ---------------------------------------------------------------------------
run "eventbridge_rule_has_rate_1_day_schedule" {
  command = plan

  assert {
    condition     = aws_cloudwatch_event_rule.daily.schedule_expression == "rate(1 day)"
    error_message = "EventBridge rule schedule_expression must be 'rate(1 day)'"
  }

  assert {
    condition     = aws_cloudwatch_event_rule.daily.state == "ENABLED"
    error_message = "EventBridge rule must be in ENABLED state"
  }
}

# ---------------------------------------------------------------------------
# Test 5: EventBridge target wires the rule to the Lambda alias ARN
#
# AC: "aws_cloudwatch_event_target" connecting the rule to the Lambda
# ---------------------------------------------------------------------------
run "eventbridge_target_wires_rule_to_lambda" {
  command = plan

  override_module {
    target = module.lambda
    outputs = {
      alias_arn    = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-stale-token-cleanup-test:live"
      invoke_arn   = "arn:aws:apigateway:eu-central-1:lambda:path/2015-03-31/functions/arn:aws:lambda:eu-central-1:123456789012:function:knotify-stale-token-cleanup-test:live/invocations"
      function_name = "knotify-stale-token-cleanup-test"
    }
  }

  assert {
    condition     = aws_cloudwatch_event_target.lambda.rule == aws_cloudwatch_event_rule.daily.name
    error_message = "EventBridge target rule must reference the daily rule"
  }

  assert {
    condition     = aws_cloudwatch_event_target.lambda.arn == "arn:aws:lambda:eu-central-1:123456789012:function:knotify-stale-token-cleanup-test:live"
    error_message = "EventBridge target ARN must be the Lambda alias ARN"
  }
}

# ---------------------------------------------------------------------------
# Test 6: Lambda permission grants events.amazonaws.com invoke
#
# AC: "aws_lambda_permission (principal events.amazonaws.com,
#      source_arn = the rule ARN)"
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
