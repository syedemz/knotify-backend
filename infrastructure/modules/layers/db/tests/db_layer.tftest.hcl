# DB layer module tests — all use command = plan (hermetic, no AWS
# credentials required).
#
# TDD note: tests authored before the Terraform resource to drive the
# implementation shape via acceptance criteria (story 3.3).

mock_provider "aws" {}

# ---------------------------------------------------------------------------
# Test 1: Layer resource sets correct compatible_runtimes and
#         compatible_architectures (story 3.3 AC5).
# ---------------------------------------------------------------------------
run "layer_has_correct_runtime_and_architecture" {
  command = plan

  variables {
    environment = "dev"
    zip_path    = "tests/fixtures/dummy_db.zip"
  }

  assert {
    condition     = contains(aws_lambda_layer_version.db.compatible_runtimes, "python3.14")
    error_message = "compatible_runtimes must include python3.14"
  }

  assert {
    condition     = length(aws_lambda_layer_version.db.compatible_runtimes) == 1
    error_message = "compatible_runtimes must contain exactly one entry: python3.14"
  }

  assert {
    condition     = contains(aws_lambda_layer_version.db.compatible_architectures, "arm64")
    error_message = "compatible_architectures must include arm64"
  }

  assert {
    condition     = length(aws_lambda_layer_version.db.compatible_architectures) == 1
    error_message = "compatible_architectures must contain exactly one entry: arm64"
  }
}

# ---------------------------------------------------------------------------
# Test 2: Layer name is scoped by environment (story 3.3 AC5).
# ---------------------------------------------------------------------------
run "layer_name_includes_environment_dev" {
  command = plan

  variables {
    environment = "dev"
    zip_path    = "tests/fixtures/dummy_db.zip"
  }

  assert {
    condition     = aws_lambda_layer_version.db.layer_name == "knotify-dev-db"
    error_message = "Layer name must be knotify-dev-db in dev environment"
  }
}

# ---------------------------------------------------------------------------
# Test 3: Layer name is correctly scoped to prod environment.
# ---------------------------------------------------------------------------
run "layer_name_includes_prod_environment" {
  command = plan

  variables {
    environment = "prod"
    zip_path    = "tests/fixtures/dummy_db.zip"
  }

  assert {
    condition     = aws_lambda_layer_version.db.layer_name == "knotify-prod-db"
    error_message = "Layer name must be knotify-prod-db in prod environment"
  }
}

# ---------------------------------------------------------------------------
# Test 4: layer_arn output is wired to the layer version ARN.
#
# aws_lambda_layer_version.arn is computed (unknown at plan time), so we
# override the resource to supply a deterministic ARN string.
# ---------------------------------------------------------------------------
run "layer_arn_output_wired_to_layer_version" {
  command = plan

  variables {
    environment = "dev"
    zip_path    = "tests/fixtures/dummy_db.zip"
  }

  override_resource {
    target = aws_lambda_layer_version.db
    values = {
      arn = "arn:aws:lambda:eu-central-1:123456789012:layer:knotify-dev-db:1"
    }
    override_during = plan
  }

  assert {
    condition     = output.layer_arn == "arn:aws:lambda:eu-central-1:123456789012:layer:knotify-dev-db:1"
    error_message = "layer_arn output must be wired to aws_lambda_layer_version.db.arn"
  }
}
