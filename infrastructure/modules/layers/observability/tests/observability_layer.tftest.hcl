# Observability layer module tests — all use command = plan (hermetic, no AWS
# credentials required).
#
# TDD note: tests authored before the Terraform resource to drive the
# implementation shape via acceptance criteria (story 3.2).

mock_provider "aws" {}

# ---------------------------------------------------------------------------
# Test 1: Layer resource sets correct compatible_runtimes and
#         compatible_architectures (story 3.2 AC3).
# ---------------------------------------------------------------------------
run "layer_has_correct_runtime_and_architecture" {
  command = plan

  variables {
    environment = "dev"
    zip_path    = "tests/fixtures/dummy_obs.zip"
  }

  assert {
    condition     = contains(aws_lambda_layer_version.observability.compatible_runtimes, "python3.14")
    error_message = "compatible_runtimes must include python3.14"
  }

  assert {
    condition     = length(aws_lambda_layer_version.observability.compatible_runtimes) == 1
    error_message = "compatible_runtimes must contain exactly one entry: python3.14"
  }

  assert {
    condition     = contains(aws_lambda_layer_version.observability.compatible_architectures, "arm64")
    error_message = "compatible_architectures must include arm64"
  }

  assert {
    condition     = length(aws_lambda_layer_version.observability.compatible_architectures) == 1
    error_message = "compatible_architectures must contain exactly one entry: arm64"
  }
}

# ---------------------------------------------------------------------------
# Test 2: Layer name is scoped by environment (story 3.2 AC3).
# ---------------------------------------------------------------------------
run "layer_name_includes_environment" {
  command = plan

  variables {
    environment = "dev"
    zip_path    = "tests/fixtures/dummy_obs.zip"
  }

  assert {
    condition     = aws_lambda_layer_version.observability.layer_name == "knotify-dev-observability"
    error_message = "Layer name must be knotify-<environment>-observability"
  }
}

# ---------------------------------------------------------------------------
# Test 3: Layer name is correctly scoped to prod environment.
# ---------------------------------------------------------------------------
run "layer_name_includes_prod_environment" {
  command = plan

  variables {
    environment = "prod"
    zip_path    = "tests/fixtures/dummy_obs.zip"
  }

  assert {
    condition     = aws_lambda_layer_version.observability.layer_name == "knotify-prod-observability"
    error_message = "Layer name must be knotify-prod-observability in prod environment"
  }
}

# ---------------------------------------------------------------------------
# Test 4: layer_arn output is wired to the layer version ARN.
#
# aws_lambda_layer_version.arn is computed (unknown at plan time), so we
# override the resource to supply a deterministic ARN string and verify
# the output wires through correctly — same pattern as the lambda tests.
# ---------------------------------------------------------------------------
run "layer_arn_output_wired_to_layer_version" {
  command = plan

  variables {
    environment = "dev"
    zip_path    = "tests/fixtures/dummy_obs.zip"
  }

  override_resource {
    target = aws_lambda_layer_version.observability
    values = {
      arn = "arn:aws:lambda:eu-central-1:123456789012:layer:knotify-dev-observability:1"
    }
    override_during = plan
  }

  assert {
    condition     = output.layer_arn == "arn:aws:lambda:eu-central-1:123456789012:layer:knotify-dev-observability:1"
    error_message = "layer_arn output must be wired to aws_lambda_layer_version.observability.arn"
  }
}
