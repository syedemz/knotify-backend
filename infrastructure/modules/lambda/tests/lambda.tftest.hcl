# Lambda module tests — all use command = plan (hermetic, no AWS credentials required)
# Tests are designed so `terraform test` exits 0 from infrastructure/modules/lambda/
#
# TDD note: this file was authored BEFORE main.tf/variables.tf/outputs.tf to drive
# the implementation shape via acceptance criteria.

mock_provider "aws" {}

# Shared variable block reused across every run that needs only the required vars.
# Inline variables {} blocks override on a per-run basis.

# ---------------------------------------------------------------------------
# Test 1: log group is created with retention_in_days=7 and the correct name.
#
# Satisfies AC 1: aws_cloudwatch_log_group with retention_in_days=7 and
# name "/aws/lambda/${var.function_name}".
# ---------------------------------------------------------------------------
run "log_group_retention_and_name" {
  command = plan

  variables {
    function_name = "knotify-test-fn"
    handler       = "handler.handler"
    role_arn      = "arn:aws:iam::123456789012:role/dummy-role"
    filename      = "tests/fixtures/dummy.zip"
  }

  assert {
    condition     = aws_cloudwatch_log_group.this.retention_in_days == 7
    error_message = "Log group retention_in_days must be 7"
  }

  assert {
    condition     = aws_cloudwatch_log_group.this.name == "/aws/lambda/knotify-test-fn"
    error_message = "Log group name must be /aws/lambda/<function_name>"
  }
}

# ---------------------------------------------------------------------------
# Test 2: Lambda function sets publish=true and has the correct defaults.
#
# Satisfies AC 2: publish=true.
# Satisfies AC 5: default runtime "python3.14", architectures ["arm64"],
# timeout 10, memory_size 512.
# ---------------------------------------------------------------------------
run "function_publish_true_and_defaults" {
  command = plan

  variables {
    function_name = "knotify-test-fn"
    handler       = "handler.handler"
    role_arn      = "arn:aws:iam::123456789012:role/dummy-role"
    filename      = "tests/fixtures/dummy.zip"
    # runtime, architectures, timeout, memory_size intentionally omitted — testing defaults
  }

  assert {
    condition     = aws_lambda_function.this.publish == true
    error_message = "Lambda function must have publish=true"
  }

  assert {
    condition     = aws_lambda_function.this.runtime == "python3.14"
    error_message = "Default runtime must be python3.14"
  }

  assert {
    condition     = contains(tolist(aws_lambda_function.this.architectures), "arm64") && length(aws_lambda_function.this.architectures) == 1
    error_message = "Default architectures must be [arm64]"
  }

  assert {
    condition     = aws_lambda_function.this.timeout == 10
    error_message = "Default timeout must be 10 seconds"
  }

  assert {
    condition     = aws_lambda_function.this.memory_size == 512
    error_message = "Default memory_size must be 512 MB"
  }
}

# ---------------------------------------------------------------------------
# Test 3: alias "live" points at aws_lambda_function.this.version.
#
# Satisfies AC 2: alias function_version references aws_lambda_function.this.version.
# The alias name must be "live".
#
# aws_lambda_function.this.version is a computed (unknown at plan time) attribute.
# We override the function resource to supply a deterministic version string so
# the cross-resource reference equality is evaluable at plan time, mirroring
# the pattern used in the aurora and networking tftest files.
# ---------------------------------------------------------------------------
run "alias_live_points_at_function_version" {
  command = plan

  variables {
    function_name = "knotify-test-fn"
    handler       = "handler.handler"
    role_arn      = "arn:aws:iam::123456789012:role/dummy-role"
    filename      = "tests/fixtures/dummy.zip"
  }

  override_resource {
    target = aws_lambda_function.this
    values = {
      version = "1"
    }
    override_during = plan
  }

  assert {
    condition     = aws_lambda_alias.live.name == "live"
    error_message = "Alias must be named live"
  }

  assert {
    condition     = aws_lambda_alias.live.function_version == aws_lambda_function.this.version
    error_message = "Alias function_version must reference aws_lambda_function.this.version"
  }
}

# ---------------------------------------------------------------------------
# Test 4: Module-level env var defaults are merged into the function environment.
#
# Satisfies AC 4: POWERTOOLS_SERVICE_NAME defaults to var.function_name and
# LOG_LEVEL defaults to "INFO" when the consumer provides no overrides.
# ---------------------------------------------------------------------------
run "env_var_defaults_injected_when_no_consumer_overrides" {
  command = plan

  variables {
    function_name         = "knotify-test-fn"
    handler               = "handler.handler"
    role_arn              = "arn:aws:iam::123456789012:role/dummy-role"
    filename              = "tests/fixtures/dummy.zip"
    environment_variables = {}
  }

  assert {
    condition     = aws_lambda_function.this.environment[0].variables["POWERTOOLS_SERVICE_NAME"] == "knotify-test-fn"
    error_message = "POWERTOOLS_SERVICE_NAME must default to var.function_name"
  }

  assert {
    condition     = aws_lambda_function.this.environment[0].variables["LOG_LEVEL"] == "INFO"
    error_message = "LOG_LEVEL must default to INFO when consumer provides no override"
  }
}

# ---------------------------------------------------------------------------
# Test 5: Consumer override wins for LOG_LEVEL; POWERTOOLS_SERVICE_NAME is
# still sourced from the module default.
#
# Satisfies AC 4: "Consumer overrides win via Terraform's merge() right-side
# precedence." Verifies that supplying LOG_LEVEL="DEBUG" overrides the default
# while POWERTOOLS_SERVICE_NAME stays at its module default value.
# ---------------------------------------------------------------------------
run "consumer_override_wins_for_log_level" {
  command = plan

  variables {
    function_name = "knotify-test-fn"
    handler       = "handler.handler"
    role_arn      = "arn:aws:iam::123456789012:role/dummy-role"
    filename      = "tests/fixtures/dummy.zip"
    environment_variables = {
      LOG_LEVEL = "DEBUG"
    }
  }

  assert {
    condition     = aws_lambda_function.this.environment[0].variables["LOG_LEVEL"] == "DEBUG"
    error_message = "Consumer-supplied LOG_LEVEL=DEBUG must override the module default of INFO"
  }

  assert {
    condition     = aws_lambda_function.this.environment[0].variables["POWERTOOLS_SERVICE_NAME"] == "knotify-test-fn"
    error_message = "POWERTOOLS_SERVICE_NAME must remain at module default (var.function_name) when not overridden"
  }
}

# ---------------------------------------------------------------------------
# Test 6: Consumer can supply additional env vars alongside the module defaults.
#
# Satisfies AC 4 edge case: a consumer-provided var that is NOT one of the
# module defaults must appear in the merged environment alongside the defaults.
# ---------------------------------------------------------------------------
run "consumer_extra_env_vars_merged_with_defaults" {
  command = plan

  variables {
    function_name = "knotify-test-fn"
    handler       = "handler.handler"
    role_arn      = "arn:aws:iam::123456789012:role/dummy-role"
    filename      = "tests/fixtures/dummy.zip"
    environment_variables = {
      DB_SECRET_ARN = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:knotify-dev-app-user-credential-XXXXXX"
    }
  }

  assert {
    condition     = aws_lambda_function.this.environment[0].variables["DB_SECRET_ARN"] != ""
    error_message = "Consumer-supplied DB_SECRET_ARN must appear in the merged environment"
  }

  assert {
    condition     = aws_lambda_function.this.environment[0].variables["POWERTOOLS_SERVICE_NAME"] == "knotify-test-fn"
    error_message = "Module default POWERTOOLS_SERVICE_NAME must still be present when consumer adds extra vars"
  }

  assert {
    condition     = aws_lambda_function.this.environment[0].variables["LOG_LEVEL"] == "INFO"
    error_message = "Module default LOG_LEVEL must still be present when consumer adds extra vars"
  }
}

# ---------------------------------------------------------------------------
# Test 7: VPC config is wired when vpc_config is supplied.
#
# Satisfies AC 1: vpc_config object with subnet_ids + security_group_ids is
# accepted and forwarded to the Lambda function.
#
# subnet_ids and security_group_ids are lists of computed IDs — we supply
# deterministic strings to assert the wiring is present.
# ---------------------------------------------------------------------------
run "vpc_config_wired_to_function" {
  command = plan

  variables {
    function_name = "knotify-test-fn"
    handler       = "handler.handler"
    role_arn      = "arn:aws:iam::123456789012:role/dummy-role"
    filename      = "tests/fixtures/dummy.zip"
    vpc_config = {
      subnet_ids         = ["subnet-aaaa0001", "subnet-aaaa0002"]
      security_group_ids = ["sg-lambda-mock-id"]
    }
  }

  assert {
    condition     = length(aws_lambda_function.this.vpc_config[0].subnet_ids) == 2
    error_message = "VPC config subnet_ids must contain 2 entries"
  }

  assert {
    condition     = contains(tolist(aws_lambda_function.this.vpc_config[0].subnet_ids), "subnet-aaaa0001")
    error_message = "VPC config subnet_ids must include subnet-aaaa0001"
  }

  assert {
    condition     = contains(tolist(aws_lambda_function.this.vpc_config[0].security_group_ids), "sg-lambda-mock-id")
    error_message = "VPC config security_group_ids must include sg-lambda-mock-id"
  }
}

# ---------------------------------------------------------------------------
# Test 8: Non-default runtime and architectures are accepted.
#
# Satisfies AC 5: runtime and architectures are overridable inputs.
# ---------------------------------------------------------------------------
run "runtime_and_architectures_overridable" {
  command = plan

  variables {
    function_name = "knotify-test-fn"
    handler       = "handler.handler"
    role_arn      = "arn:aws:iam::123456789012:role/dummy-role"
    filename      = "tests/fixtures/dummy.zip"
    runtime       = "python3.12"
    architectures = ["x86_64"]
  }

  assert {
    condition     = aws_lambda_function.this.runtime == "python3.12"
    error_message = "Override runtime must be accepted"
  }

  assert {
    condition     = contains(tolist(aws_lambda_function.this.architectures), "x86_64") && length(aws_lambda_function.this.architectures) == 1
    error_message = "Override architectures must be accepted"
  }
}

# ---------------------------------------------------------------------------
# Test 9: Non-default timeout and memory_size are accepted.
#
# Satisfies AC 5: timeout and memory_size are overridable inputs.
# ---------------------------------------------------------------------------
run "timeout_and_memory_size_overridable" {
  command = plan

  variables {
    function_name = "knotify-test-fn"
    handler       = "handler.handler"
    role_arn      = "arn:aws:iam::123456789012:role/dummy-role"
    filename      = "tests/fixtures/dummy.zip"
    timeout       = 300
    memory_size   = 1024
  }

  assert {
    condition     = aws_lambda_function.this.timeout == 300
    error_message = "Override timeout must be accepted"
  }

  assert {
    condition     = aws_lambda_function.this.memory_size == 1024
    error_message = "Override memory_size must be accepted"
  }
}

# ---------------------------------------------------------------------------
# Test 10: Function handler and role_arn are wired correctly.
#
# Satisfies AC 1: function_name, handler, role_arn are required inputs that
# wire through to aws_lambda_function.
# ---------------------------------------------------------------------------
run "function_handler_and_role_wired" {
  command = plan

  variables {
    function_name = "knotify-test-fn"
    handler       = "handler.handler"
    role_arn      = "arn:aws:iam::123456789012:role/dummy-role"
    filename      = "tests/fixtures/dummy.zip"
  }

  assert {
    condition     = aws_lambda_function.this.function_name == "knotify-test-fn"
    error_message = "function_name must be wired to the Lambda function"
  }

  assert {
    condition     = aws_lambda_function.this.handler == "handler.handler"
    error_message = "handler must be wired to the Lambda function"
  }

  assert {
    condition     = aws_lambda_function.this.role == "arn:aws:iam::123456789012:role/dummy-role"
    error_message = "role_arn must be wired to the Lambda function"
  }
}
