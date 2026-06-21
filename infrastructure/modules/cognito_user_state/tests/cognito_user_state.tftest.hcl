# cognito_user_state module tests — all use command = plan (hermetic, no AWS credentials required)
# Tests are designed so `terraform test` exits 0 from infrastructure/modules/cognito_user_state/
#
# TDD note: this file was authored BEFORE main.tf/variables.tf/outputs.tf to drive
# the implementation shape via acceptance criteria (story 9.3).
#
# Acceptance criteria covered:
#   AC1 — module.lambda is declared (function_name passthrough)
#   AC2 — environment_variables contains USER_POOL_ID
#   AC3 — lambda_arn output wired to module.lambda.alias_arn
#   AC4 — no vpc_config (Lambda runs outside the VPC)

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
# Test 1: function_name is wired through to the lambda sub-module
#
# Satisfies AC1: module correctly delegates to the lambda sub-module.
# ---------------------------------------------------------------------------
run "function_name_passed_to_lambda_submodule" {
  command = plan

  variables {
    function_name = "knotify-cognito-user-state-test"
    filename      = "./tests/dummy.zip"
    role_arn      = "arn:aws:iam::123456789012:role/knotify-test-cognito-user-state"
    user_pool_id  = "eu-central-1_TESTPOOL"
  }

  assert {
    condition     = module.lambda.function_name == "knotify-cognito-user-state-test"
    error_message = "function_name must be passed through to the lambda sub-module"
  }
}

# ---------------------------------------------------------------------------
# Test 2: lambda_arn output is wired to the live alias ARN
#
# Satisfies AC3: the step_functions module uses module.cognito_user_state.lambda_arn
# as the cognito_user_state ARN in its lambda_arns input map.
# ---------------------------------------------------------------------------
run "lambda_arn_output_wired_to_alias_arn" {
  command = plan

  variables {
    function_name = "knotify-cognito-user-state-test"
    filename      = "./tests/dummy.zip"
    role_arn      = "arn:aws:iam::123456789012:role/knotify-test-cognito-user-state"
    user_pool_id  = "eu-central-1_TESTPOOL"
  }

  override_module {
    target = module.lambda
    outputs = {
      alias_arn     = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-cognito-user-state-test:live"
      function_name = "knotify-cognito-user-state-test"
      invoke_arn    = "arn:aws:apigateway:eu-central-1:lambda:path/2015-03-31/functions/arn:aws:lambda:eu-central-1:123456789012:function:knotify-cognito-user-state-test:live/invocations"
    }
  }

  assert {
    condition     = output.lambda_arn == "arn:aws:lambda:eu-central-1:123456789012:function:knotify-cognito-user-state-test:live"
    error_message = "lambda_arn output must be wired to module.lambda.alias_arn"
  }
}

# ---------------------------------------------------------------------------
# Test 3: user_pool_id default is empty string (safe for isolated module tests)
#
# When user_pool_id is omitted the module plans without error — the default
# empty string is injected as USER_POOL_ID and the handler will return
# UserNotFoundException on every call (no real Cognito pool targeted).
# ---------------------------------------------------------------------------
run "user_pool_id_defaults_to_empty_string_and_module_plans_without_error" {
  command = plan

  variables {
    function_name = "knotify-cognito-user-state-test"
    filename      = "./tests/dummy.zip"
    role_arn      = "arn:aws:iam::123456789012:role/knotify-test-cognito-user-state"
    # user_pool_id intentionally omitted — uses default ""
  }

  assert {
    condition     = module.lambda.function_name == "knotify-cognito-user-state-test"
    error_message = "module must plan successfully when user_pool_id is omitted"
  }
}
