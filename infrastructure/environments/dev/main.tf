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

# ---------------------------------------------------------------------------
# us-east-1 provider alias — story 5.0
#
# CloudFront-scoped WAF (story 5.4) and CloudFront viewer certificates
# (story 5.2) must reside in us-east-1 regardless of the environment's
# primary region. This alias is the canonical Terraform pattern for that
# constraint. The alias is a no-op until story 5.2 and story 5.4 reference
# it via `providers = { aws.us_east_1 = aws.us_east_1 }` in their module
# calls. Modules that accept the alias declare
# `configuration_aliases = [aws.us_east_1]` in their required_providers block.
# ---------------------------------------------------------------------------

provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"

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

  # Postgres log retention — dev keeps a short window to limit CloudWatch cost
  postgresql_log_retention_days = var.aurora_postgresql_log_retention_days
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

  # The invoke is retried up to 6 times with a 45s wait between attempts
  # (~4 min total budget). Two distinct transient failure modes are absorbed
  # here:
  #
  #   1. Post-modify Lambda warm-up — after Terraform Modifies the Lambda +
  #      alias, the first invoke can race against AWS-side ENI/version
  #      propagation: outbound TCP to Aurora black-holes (Connection timed
  #      out at ~16s with no SYN-ACK). Resolves within ~30s.
  #
  #   2. Aurora cold boot after a destroy round-trip — when the cluster is
  #      created from scratch in the same apply, RDS reports "available" as
  #      soon as the instance is provisioned, but cross-VPC DNS propagation
  #      to the Lambda's resolver and PostgreSQL accepting connections both
  #      lag the API status by 2-4 min. The cluster waiter doesn't help —
  #      only actual connection attempts prove reachability. Observed
  #      progression: attempt 1 = DNS NXDOMAIN, attempt 3 = Connection
  #      refused, attempt 4+ = success.
  #
  # Without this retry the apply fails non-deterministically on every code
  # change and reliably on every destroy-then-recreate even though the
  # migrator is healthy.
  provisioner "local-exec" {
    command = <<-EOT
      max_attempts=6
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
            echo "WARN: db_migrator returned a FunctionError; retrying in 45s (transient ENI/version propagation or Aurora cold boot)" >&2
            sleep 45
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
# Built and deployed here. The Cognito User Pool trigger wiring and the
# aws_lambda_permission granting cognito-idp.amazonaws.com invoke rights
# live in module.cognito (story 4.3) — see below.
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

# ---------------------------------------------------------------------------
# cognito_pre_token_generation Lambda — story 4.4
#
# Reads profile_complete_verified from Aurora on every token issuance and
# embeds it as custom:profile_complete on both the ID token and the access
# token (brainstorm B1). Uses the shared cognito_trigger IAM role (story 3.4)
# and the same VPC config + layers as cognito_post_confirmation.
#
# DB_SECRET_NAME uses the friendly Secrets Manager secret name — boto3
# resolves by name (wildcard ARN strings are not accepted by GetSecretValue).
# ---------------------------------------------------------------------------

module "cognito_pre_token_generation" {
  source = "../../modules/lambda"

  function_name = "knotify-cognito-pre-token-generation-${var.environment}"
  handler       = "handler.handler"
  filename      = "${path.module}/../../../build/cognito_pre_token_generation.zip"

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

# ---------------------------------------------------------------------------
# Cognito User Pool — story 4.1 / 4.2 / 4.3 / 4.4
#
# module.cognito instantiates the User Pool, both app clients, and wires both
# Lambda triggers (post_confirmation and pre_token_generation) plus their
# aws_lambda_permission resources that grant cognito-idp.amazonaws.com invoke rights.
#
# Both Lambda ARNs use the unqualified function ARN (not the alias ARN) —
# Cognito invokes the function directly without going through an alias.
# ---------------------------------------------------------------------------

module "cognito" {
  source = "../../modules/cognito"

  name        = "knotify-${var.environment}-user-pool"
  environment = var.environment

  # Cognito Advanced Security set to AUDIT minimum to enable V2 PreTokenGeneration
  # (story 4.4 / brainstorm B2). ENFORCED upgrade and MFA enforcement deferred
  # to phase 11. See architecture.md §13 #1.
  advanced_security_mode = var.advanced_security_mode

  # Wire the post-confirmation trigger (story 4.3).
  post_confirmation_lambda_arn = module.cognito_post_confirmation.function_arn

  # Wire the pre-token-generation trigger (story 4.4).
  # V2_0 trigger shape — requires AUDIT Advanced Security Mode (default).
  pre_token_generation_lambda_arn = module.cognito_pre_token_generation.function_arn
}

# ---------------------------------------------------------------------------
# ACM certificate — story 5.2
#
# dev path: domain_name = "" → module produces ZERO resources. The alias
# hand-off is exercised here so the providers block is validated end-to-end
# even though no AWS calls are made. The module will produce real resources
# in prod once prod.tfvars sets domain_name and hosted_zone_id (see
# docs/PROD_CUTOVER.md §4b).
# ---------------------------------------------------------------------------

module "acm" {
  source = "../../modules/acm"

  providers = {
    aws           = aws
    aws.us_east_1 = aws.us_east_1
  }

  domain_name    = var.domain_name
  hosted_zone_id = var.hosted_zone_id
}

# ---------------------------------------------------------------------------
# HTTP API Gateway — story 5.1 module, env wiring deferred to story 5.3
#
# CloudFront (below) needs execute_api_endpoint as its origin, so the API
# Gateway must be instantiated here. See story 5.1 notes: "env-level wiring
# landed in story 5.3 (CloudFront needed an origin)."
#
# Cognito outputs from phase 4 feed the JWT authorizer:
#   user_pool_endpoint       → issuer URL for JWKS validation
#   cognito_audience_client_ids → compact list of both app clients so dev
#                                 integration-test tokens (minted via
#                                 ADMIN_USER_PASSWORD_AUTH) are accepted
# ---------------------------------------------------------------------------

module "api_gateway" {
  source = "../../modules/api_gateway"

  name = "knotify-${var.environment}-api"

  cognito_user_pool_endpoint  = module.cognito.user_pool_endpoint
  cognito_audience_client_ids = compact([module.cognito.app_client_id, module.cognito.integration_test_app_client_id])

  throttling_burst_limit = var.api_gateway_throttling_burst_limit
  throttling_rate_limit  = var.api_gateway_throttling_rate_limit
}

# ---------------------------------------------------------------------------
# CloudFront distribution — story 5.3
#
# Sits in front of the HTTP API Gateway.  The origin receives an injected
# x-knotify-edge-secret header so every Lambda can verify the request
# arrived via CloudFront (not via the raw execute-api endpoint).
#
# dev path: domain_name = "" → cloudfront_default_certificate, no aliases.
#   The ACM module returns certificate_arn = "" on the dev path; the
#   CloudFront module ignores it when domain_name is empty.
#
# replace() strips the "https://" scheme — CloudFront's domain_name field
# on an origin block requires a hostname only.
# ---------------------------------------------------------------------------

module "cloudfront" {
  source = "../../modules/cloudfront"

  api_gateway_domain_name = replace(module.api_gateway.execute_api_endpoint, "https://", "")
  domain_name             = var.domain_name
  acm_certificate_arn     = module.acm.certificate_arn
}

# ---------------------------------------------------------------------------
# WAF web ACL — story 5.4
#
# CLOUDFRONT-scoped WAF must reside in us-east-1 (CloudFront control plane).
# The alias is threaded here from the provider block added in story 5.0.
# The ACL ships with:
#   - AWSManagedRulesCommonRuleSet   (count — monitor; flip to none in phase 11)
#   - AWSManagedRulesKnownBadInputsRuleSet (none — enforce from day one)
#   - AWSManagedRulesSQLiRuleSet      (count — observe; flip to none in phase 11)
#   - Rate-based per-IP               (block at 2000 req/5 min)
# ---------------------------------------------------------------------------

module "waf" {
  source = "../../modules/waf"

  providers = {
    aws.us_east_1 = aws.us_east_1
  }

  environment                 = var.environment
  cloudfront_distribution_arn = module.cloudfront.distribution_arn
}
