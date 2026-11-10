# Supabase module tests — all use command = plan (hermetic, no AWS credentials required)
# Tests are designed so `terraform test` exits 0 from infrastructure/modules/supabase/

mock_provider "aws" {}

# ---------------------------------------------------------------------------
# Test 1: Exactly one secret is planned with the correct name shape
# Satisfies AC: name equals "knotify/<env>/supabase_credential"
# ---------------------------------------------------------------------------

run "secret_name_matches_env_shape" {
  command = plan

  variables {
    environment = "dev"
    username    = "postgres.test-ref"
  }

  assert {
    condition     = aws_secretsmanager_secret.credential.name == "knotify/dev/supabase_credential"
    error_message = "Secret name must be knotify/dev/supabase_credential when environment=dev"
  }
}

# ---------------------------------------------------------------------------
# Test 2: Initial secret version password is empty and username matches input
# Satisfies AC: initial version deserialises to {password: "", username: var.username}
# ---------------------------------------------------------------------------

run "initial_version_placeholder_payload" {
  command = plan

  variables {
    environment = "dev"
    username    = "postgres.test-ref"
  }

  assert {
    condition     = jsondecode(aws_secretsmanager_secret_version.initial.secret_string).password == ""
    error_message = "Initial secret version must have password set to empty string"
  }

  assert {
    condition     = jsondecode(aws_secretsmanager_secret_version.initial.secret_string).username == "postgres.test-ref"
    error_message = "Initial secret version username must equal var.username"
  }
}

# ---------------------------------------------------------------------------
# Test 3: secret_arn output is non-empty / known at plan time
# Satisfies AC: secret_arn output is non-empty
# override_resource injects a known mock ARN so the condition is evaluable at
# plan time (same technique used in the aurora module test 8 / test 9).
# ---------------------------------------------------------------------------

run "secret_arn_output_is_known" {
  command = plan

  variables {
    environment = "dev"
    username    = "postgres.test-ref"
  }

  override_resource {
    target          = aws_secretsmanager_secret.credential
    override_during = plan
    values = {
      arn = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:knotify/dev/supabase_credential-AbCdEf"
    }
  }

  assert {
    condition     = output.secret_arn != ""
    error_message = "secret_arn output must be non-empty"
  }
}

# ---------------------------------------------------------------------------
# Test 4: secret_name output matches the resource name
# Satisfies AC: secret_name output is non-empty and consistent with the secret name
# ---------------------------------------------------------------------------

run "secret_name_output_matches_resource" {
  command = plan

  variables {
    environment = "dev"
    username    = "postgres.test-ref"
  }

  assert {
    condition     = output.secret_name == "knotify/dev/supabase_credential"
    error_message = "secret_name output must equal knotify/dev/supabase_credential when environment=dev"
  }
}
