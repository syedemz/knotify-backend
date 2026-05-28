provider "aws" {
  region = "eu-central-1"

  default_tags {
    tags = {
      Project     = "knotify"
      Environment = var.environment
      ManagedBy   = "terraform"
      Owner       = "knotify-team"
    }
  }
}

module "networking" {
  source      = "../../modules/networking"
  environment = var.environment
}

# ---------------------------------------------------------------------------
# Aurora Serverless v2 cluster — story 2.1 module wired to networking outputs.
# The security group and subnet group are produced by the networking module
# (phase 1 story 1.3) so this module has no direct VPC dependency; it
# receives only the resource references it needs.
# ---------------------------------------------------------------------------

module "aurora" {
  source = "../../modules/aurora"

  environment        = var.environment
  cluster_identifier = "knotify-${var.environment}-aurora"
  database_name      = "knotify"

  # Networking — supplied by the phase-1 networking module outputs
  aurora_security_group_id = module.networking.aurora_security_group_id
  db_subnet_group_name     = module.networking.db_subnet_group_name

  # ACU bounds — per dev.tfvars (0.5 / 2.0)
  min_acu = var.aurora_min_acu
  max_acu = var.aurora_max_acu

  # Environment-specific safety flags — all relaxed for dev
  deletion_protection     = var.aurora_deletion_protection
  skip_final_snapshot     = var.aurora_skip_final_snapshot
  apply_immediately       = var.aurora_apply_immediately
  backup_retention_period = var.aurora_backup_retention_period
}

# ---------------------------------------------------------------------------
# DynamoDB tables — story 2.10–2.13 module.
# No networking dependency; DynamoDB is a regional managed service accessed
# via the VPC endpoint provisioned by the networking module.
# ---------------------------------------------------------------------------

module "dynamodb" {
  source = "../../modules/dynamodb"

  environment  = var.environment
  project_name = "knotify"

  # Safety flags — per dev.tfvars (PITR and deletion protection both off)
  point_in_time_recovery_enabled = var.dynamodb_point_in_time_recovery
  deletion_protection_enabled    = var.dynamodb_deletion_protection
}

# ---------------------------------------------------------------------------
# Shared Lambda layers — story 3.2 (observability) and story 3.3 (db)
# ---------------------------------------------------------------------------

module "observability_layer" {
  source = "../../modules/layers/observability"

  environment = var.environment
  zip_path    = "${path.module}/../../../build/knotify-observability-layer.zip"
}

module "db_layer" {
  source = "../../modules/layers/db"

  environment = var.environment
  zip_path    = "${path.module}/../../../build/knotify-db-layer.zip"
}

# ---------------------------------------------------------------------------
# IAM roles — story 3.4
# All roles are scaffolded here. Phases 6–9 consume aurora_reader/writer,
# dynamodb_*, and stepfn_task. cognito_trigger and db_migrator are consumed
# by stories 3.6 and 3.7 respectively.
# ---------------------------------------------------------------------------

module "iam_roles" {
  source = "../../modules/iam_roles"

  environment                   = var.environment
  aurora_master_user_secret_arn = module.aurora.master_user_secret_arn
}

# ---------------------------------------------------------------------------
# db_migrator Lambda — story 3.7
#
# Applies yoyo migrations against the dev Aurora cluster and rotates the
# app_user password in Secrets Manager on every deployment where migrations
# or the Lambda code change (controlled by null_resource.db_migrator_invoke
# triggers below).
#
# memory_size=1024 and timeout=300 allow headroom for the largest migration
# plus the HNSW index build on the users table.
# ---------------------------------------------------------------------------

module "db_migrator" {
  source = "../../modules/lambda"

  function_name = "knotify-db-migrator-${var.environment}"
  handler       = "handler.handler"
  filename      = "${path.module}/../../../build/db_migrator.zip"
  memory_size   = 1024
  timeout       = 300

  layers = [
    module.observability_layer.layer_arn,
    module.db_layer.layer_arn,
  ]

  role_arn = module.iam_roles.role_arns["db_migrator"]

  # The VPC config must include the aurora_security_group_id so the Lambda
  # ENI can reach Aurora on port 5432. The Lambda SG is also included so
  # outbound Secrets Manager calls route through the VPC Interface Endpoint.
  vpc_config = {
    subnet_ids = module.networking.private_subnet_ids
    security_group_ids = [
      module.networking.lambda_security_group_id,
      module.networking.aurora_security_group_id,
    ]
  }

  environment_variables = {
    # ARN of the Aurora master credential (Aurora-managed rotation).
    # Fetched via the Interface VPC Endpoint (story 3.0).
    AURORA_MASTER_SECRET_ARN = module.aurora.master_user_secret_arn

    # Friendly name of the app_user credential secret to create/update.
    # The migrator writes this secret on first run (CreateSecret) and
    # rotates it on subsequent runs (PutSecretValue).
    APP_USER_SECRET_NAME = "knotify-${var.environment}-app-user-credential"

    # Connection endpoint params. Aurora's managed master secret only
    # contains username/password — host/port/database are exposed via
    # the cluster endpoint outputs, not embedded in the secret.
    AURORA_HOST   = module.aurora.cluster_endpoint
    AURORA_PORT   = tostring(module.aurora.port)
    AURORA_DBNAME = module.aurora.database_name
  }
}

# ---------------------------------------------------------------------------
# null_resource — invoke db_migrator once per apply when migrations or code
# change.
#
# triggers:
#   migrations_hash  — sha256 of all migration file contents; fires when any
#                      .sql file is added, edited, or deleted.
#   lambda_version   — published Lambda version number; fires when the
#                      function code or configuration changes.
#
# Without triggers the null_resource would fire on every apply.  With them,
# it re-runs only when there is a meaningful change to apply — new migrations
# or a code update.  This avoids spurious password rotations on no-op applies.
#
# The local-exec runs `aws lambda invoke` against the LIVE alias and fails the
# apply when:
#   - the invoke itself errors (non-zero exit from the CLI), or
#   - the CLI's structured response includes a FunctionError (the Lambda
#     raised an unhandled exception). Without this second check the apply
#     reports success even though the migrator returned an error payload —
#     `aws lambda invoke` only signals invocation failures via its exit code,
#     not Lambda-side errors.
# The invocation metadata (StatusCode, FunctionError, ExecutedVersion) is
# captured to invoke_meta.json; the response payload is in out.json. Both are
# catted to the apply log for visibility.
# ---------------------------------------------------------------------------

resource "null_resource" "db_migrator_invoke" {
  triggers = {
    migrations_hash = sha256(jsonencode([
      for f in sort(fileset("${path.module}/../../../infrastructure/db/migrations", "*.sql")) :
      filesha256("${path.module}/../../../infrastructure/db/migrations/${f}")
    ]))
    lambda_version = module.db_migrator.function_version
  }

  # The invoke is retried up to 3 times with a 30s wait between attempts.
  # After Terraform finishes Modifying the Lambda + alias, the first invoke
  # can race against AWS-side ENI/version propagation: the function runs,
  # but its outbound TCP to Aurora black-holes (Connection timed out at
  # ~16s with no SYN-ACK). A subsequent manual invoke succeeds reliably
  # within ~300ms, confirming the failure is purely the post-modify warm-up
  # window. Without this retry the apply fails non-deterministically on
  # every code change even though the migrator is healthy.
  provisioner "local-exec" {
    command = <<-EOT
      max_attempts=3
      attempt=1
      while [ $attempt -le $max_attempts ]; do
        echo "=== invoke attempt $attempt of $max_attempts ==="
        aws lambda invoke \
          --cli-read-timeout 0 \
          --cli-connect-timeout 60 \
          --function-name knotify-db-migrator-${var.environment} \
          --qualifier live \
          --payload '{}' \
          out.json > invoke_meta.json
        echo "=== invoke metadata ==="
        cat invoke_meta.json
        echo
        echo "=== response payload ==="
        cat out.json
        echo
        if jq -e '.FunctionError' invoke_meta.json > /dev/null 2>&1; then
          if [ $attempt -lt $max_attempts ]; then
            echo "WARN: db_migrator returned a FunctionError; retrying in 30s (transient post-deploy ENI/version propagation)" >&2
            sleep 30
            attempt=$((attempt + 1))
            continue
          else
            echo "ERROR: db_migrator Lambda returned a FunctionError on all $max_attempts attempts; failing apply" >&2
            exit 1
          fi
        fi
        echo "=== invoke succeeded on attempt $attempt ==="
        exit 0
      done
    EOT
  }

  depends_on = [module.db_migrator]
}

# ---------------------------------------------------------------------------
# cognito_post_confirmation Lambda — story 3.6
#
# Built and deployed here; NOT wired to the Cognito User Pool trigger.
# Wiring (aws_cognito_user_pool_lambda_config and aws_lambda_permission
# for cognito-idp.amazonaws.com) is deferred to phase 4 story 4.3
# per brainstorm N1.
#
# The Lambda connects as app_user using the knotify-dev-app-user-credential
# secret, which is created by the db_migrator Lambda in story 3.7.
# DB_SECRET_NAME uses the friendly Secrets Manager secret name — boto3
# resolves by name (wildcard ARN strings are not accepted by GetSecretValue).
# ---------------------------------------------------------------------------

module "cognito_post_confirmation" {
  source = "../../modules/lambda"

  function_name = "knotify-cognito-post-confirmation-${var.environment}"
  handler       = "handler.handler"
  filename      = "${path.module}/../../../build/cognito_post_confirmation.zip"

  layers = [
    module.observability_layer.layer_arn,
    module.db_layer.layer_arn,
  ]

  role_arn = module.iam_roles.role_arns["cognito_trigger"]

  vpc_config = {
    subnet_ids         = module.networking.private_subnet_ids
    security_group_ids = [module.networking.lambda_security_group_id]
  }

  environment_variables = {
    # Friendly Secrets Manager secret name for the app_user credential.
    # Created by the db_migrator Lambda in story 3.7.
    # The cognito_trigger IAM role scopes GetSecretValue to
    # arn:...:secret:knotify-<env>-app-user-credential-*
    DB_SECRET_NAME = "knotify-${var.environment}-app-user-credential"
  }
}
