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

  environment                = var.environment
  aurora_cluster_resource_id = module.aurora.cluster_resource_id
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
