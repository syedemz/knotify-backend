terraform {
  required_version = ">= 1.9.0"
  required_providers {
    aws = {
      source = "hashicorp/aws"
      # python3.14 runtime support requires provider >= 6.20.0.
      # The project-wide constraint is raised here; other modules will be
      # updated in the same commit so the root lock file stays consistent.
      version = "~> 6.20"
    }
  }
}

# ---------------------------------------------------------------------------
# Locals
# ---------------------------------------------------------------------------

locals {
  # Module-level defaults merged with consumer-supplied vars.
  # merge() gives right-side precedence, so consumer values override defaults.
  merged_env_vars = merge(
    {
      POWERTOOLS_SERVICE_NAME = var.function_name
      LOG_LEVEL               = "INFO"
    },
    var.environment_variables
  )
}

# ---------------------------------------------------------------------------
# CloudWatch Log Group
#
# Created BEFORE the function (enforced by depends_on below) so that the
# explicit group with retention_in_days=7 is in place before the first
# invocation. Without this, AWS auto-creates a "Never expire" group and a
# subsequent apply of the explicit group fails with ResourceAlreadyExists
# (brainstorm M9).
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "this" {
  name              = "/aws/lambda/${var.function_name}"
  retention_in_days = 7
}

# ---------------------------------------------------------------------------
# Lambda function
#
# publish = true is required so that the "live" alias can point at a concrete
# numbered version rather than $LATEST. Without publish=true the alias has no
# version to reference and apply fails (brainstorm M8).
#
# depends_on = [aws_cloudwatch_log_group.this] ensures the log group is
# created in the same apply pass before Lambda initialises and potentially
# emits its first log event (brainstorm M9).
# ---------------------------------------------------------------------------

resource "aws_lambda_function" "this" {
  function_name = var.function_name
  handler       = var.handler
  runtime       = var.runtime
  architectures = var.architectures
  layers        = var.layers
  role          = var.role_arn
  filename      = var.filename
  memory_size   = var.memory_size
  timeout       = var.timeout

  # Without source_code_hash, Terraform only detects metadata changes
  # (env vars, layers, etc.) and never re-uploads the zip when its
  # contents change. That silently keeps the old code in place across
  # deploys. filebase64sha256 is computed at plan time and matches the
  # AWS-reported CodeSha256 so the function only redeploys when the
  # bundled bytes actually change.
  source_code_hash = filebase64sha256(var.filename)

  # publish = true creates a numbered version on every deployment, enabling
  # the "live" alias to reference a stable, immutable version ARN.
  publish = true

  environment {
    variables = local.merged_env_vars
  }

  dynamic "vpc_config" {
    for_each = var.vpc_config != null ? [var.vpc_config] : []
    content {
      subnet_ids         = vpc_config.value.subnet_ids
      security_group_ids = vpc_config.value.security_group_ids
    }
  }

  # Log group must exist before Lambda can emit to it (brainstorm M9).
  depends_on = [aws_cloudwatch_log_group.this]
}

# ---------------------------------------------------------------------------
# Lambda alias "live"
#
# Points at the latest published version of the function. Consuming code
# invokes the alias ARN rather than the function ARN so that a blue/green
# deploy (shifting alias weight) is possible in a future phase without
# changing the caller's endpoint.
# ---------------------------------------------------------------------------

resource "aws_lambda_alias" "live" {
  name             = "live"
  function_name    = aws_lambda_function.this.function_name
  function_version = aws_lambda_function.this.version
}
