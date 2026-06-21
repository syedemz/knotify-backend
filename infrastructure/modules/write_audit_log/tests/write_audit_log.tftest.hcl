# write_audit_log module tests — all use command = plan (hermetic, no AWS credentials required)

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

run "function_name_passed_to_lambda_submodule" {
  command = plan

  variables {
    function_name = "knotify-write-audit-log-test"
    filename      = "./tests/dummy.zip"
    role_arn      = "arn:aws:iam::123456789012:role/knotify-test-write-audit-log"
  }

  assert {
    condition     = module.lambda.function_name == "knotify-write-audit-log-test"
    error_message = "function_name must be passed through to the lambda sub-module"
  }
}

run "lambda_arn_output_wired_to_alias_arn" {
  command = plan

  variables {
    function_name = "knotify-write-audit-log-test"
    filename      = "./tests/dummy.zip"
    role_arn      = "arn:aws:iam::123456789012:role/knotify-test-write-audit-log"
  }

  override_module {
    target = module.lambda
    outputs = {
      alias_arn     = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-write-audit-log-test:live"
      function_name = "knotify-write-audit-log-test"
      invoke_arn    = "arn:aws:apigateway:eu-central-1:lambda:path/2015-03-31/functions/arn:aws:lambda:eu-central-1:123456789012:function:knotify-write-audit-log-test:live/invocations"
    }
  }

  assert {
    condition     = output.lambda_arn == "arn:aws:lambda:eu-central-1:123456789012:function:knotify-write-audit-log-test:live"
    error_message = "lambda_arn output must be wired to module.lambda.alias_arn"
  }
}

run "table_audit_default_and_module_plans_without_error" {
  command = plan

  variables {
    function_name = "knotify-write-audit-log-test"
    filename      = "./tests/dummy.zip"
    role_arn      = "arn:aws:iam::123456789012:role/knotify-test-write-audit-log"
    # audit_table_name intentionally omitted — default should apply
  }

  assert {
    condition     = module.lambda.function_name == "knotify-write-audit-log-test"
    error_message = "module must plan successfully when audit_table_name is omitted"
  }
}
