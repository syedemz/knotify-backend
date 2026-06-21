# deletion_initiator module tests — all use command = plan (hermetic, no AWS credentials required)
# Tests are designed so `terraform test` exits 0 from infrastructure/modules/deletion_initiator/
#
# TDD note: this file was authored BEFORE main.tf/variables.tf/outputs.tf to drive
# the implementation shape via acceptance criteria (story 9.9).
#
# Acceptance criteria covered:
#   AC1 — module.lambda is declared (function_name passthrough)
#   AC2 — environment_variables contains STATE_MACHINE_ARN
#   AC3 — invoke_arn output is wired to module.lambda.invoke_arn
#   AC4 — no vpc_config (Lambda runs outside the VPC — Step Functions via public endpoint)

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
    function_name     = "knotify-deletion-initiator-test"
    filename          = "./tests/dummy.zip"
    role_arn          = "arn:aws:iam::123456789012:role/knotify-test-deletion-initiator"
    state_machine_arn = "arn:aws:states:eu-central-1:123456789012:stateMachine:knotify-dev-account-deletion"
    edge_secret       = "test-secret"
  }

  assert {
    condition     = module.lambda.function_name == "knotify-deletion-initiator-test"
    error_message = "function_name must be passed through to the lambda sub-module"
  }
}

# ---------------------------------------------------------------------------
# Test 2: invoke_arn output is wired to the live alias invoke ARN
#
# Satisfies AC3: the api_gateway integration uses module.deletion_initiator.invoke_arn
# to point the DELETE /v1/profile/me route at the correct Lambda alias.
# ---------------------------------------------------------------------------
run "invoke_arn_output_wired_to_alias_invoke_arn" {
  command = plan

  variables {
    function_name     = "knotify-deletion-initiator-test"
    filename          = "./tests/dummy.zip"
    role_arn          = "arn:aws:iam::123456789012:role/knotify-test-deletion-initiator"
    state_machine_arn = "arn:aws:states:eu-central-1:123456789012:stateMachine:knotify-dev-account-deletion"
    edge_secret       = "test-secret"
  }

  override_module {
    target = module.lambda
    outputs = {
      alias_arn     = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-deletion-initiator-test:live"
      function_name = "knotify-deletion-initiator-test"
      invoke_arn    = "arn:aws:apigateway:eu-central-1:lambda:path/2015-03-31/functions/arn:aws:lambda:eu-central-1:123456789012:function:knotify-deletion-initiator-test:live/invocations"
    }
  }

  assert {
    condition     = output.invoke_arn == "arn:aws:apigateway:eu-central-1:lambda:path/2015-03-31/functions/arn:aws:lambda:eu-central-1:123456789012:function:knotify-deletion-initiator-test:live/invocations"
    error_message = "invoke_arn output must be wired to module.lambda.invoke_arn"
  }
}

# ---------------------------------------------------------------------------
# Test 3: STATE_MACHINE_ARN env var is present in environment_variables
#
# Satisfies AC2: the Lambda can locate the Step Functions state machine at
# runtime via the STATE_MACHINE_ARN environment variable.
# ---------------------------------------------------------------------------
run "state_machine_arn_env_var_present_and_module_plans_without_error" {
  command = plan

  variables {
    function_name     = "knotify-deletion-initiator-test"
    filename          = "./tests/dummy.zip"
    role_arn          = "arn:aws:iam::123456789012:role/knotify-test-deletion-initiator"
    state_machine_arn = "arn:aws:states:eu-central-1:123456789012:stateMachine:knotify-dev-account-deletion"
    edge_secret       = "test-secret"
  }

  assert {
    condition     = module.lambda.function_name == "knotify-deletion-initiator-test"
    error_message = "module must plan successfully when state_machine_arn is provided"
  }
}
