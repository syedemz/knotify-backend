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
#
# PROD NOTE: apply is gated per docs/PROD_CUTOVER.md and the deploy.yml gate
# from phase 1. This file is authored for `terraform plan`; `terraform apply`
# against prod requires manual approval.
# ---------------------------------------------------------------------------

module "aurora" {
  source = "../../modules/aurora"

  environment        = var.environment
  cluster_identifier = "knotify-${var.environment}-aurora"
  database_name      = "knotify"

  # Networking — supplied by the phase-1 networking module outputs
  aurora_security_group_id = module.networking.aurora_security_group_id
  db_subnet_group_name     = module.networking.db_subnet_group_name

  # ACU bounds — per prod.tfvars (1.0 / 8.0)
  min_acu = var.aurora_min_acu
  max_acu = var.aurora_max_acu

  # Environment-specific safety flags — all hardened for prod
  deletion_protection     = var.aurora_deletion_protection
  skip_final_snapshot     = var.aurora_skip_final_snapshot
  apply_immediately       = var.aurora_apply_immediately
  backup_retention_period = var.aurora_backup_retention_period

  # Postgres log retention — prod keeps a week for incident forensics
  postgresql_log_retention_days = var.aurora_postgresql_log_retention_days
}

# ---------------------------------------------------------------------------
# DynamoDB tables — story 2.10–2.13 module.
# No networking dependency; DynamoDB is a regional managed service accessed
# via the VPC endpoint provisioned by the networking module.
#
# PROD NOTE: apply is gated per docs/PROD_CUTOVER.md. Authored + planned only.
# ---------------------------------------------------------------------------

module "dynamodb" {
  source = "../../modules/dynamodb"

  environment  = var.environment
  project_name = "knotify"

  # Safety flags — per prod.tfvars (PITR and deletion protection both on)
  point_in_time_recovery_enabled = var.dynamodb_point_in_time_recovery
  deletion_protection_enabled    = var.dynamodb_deletion_protection
}

# ---------------------------------------------------------------------------
# Shared Lambda layers — story 3.2 (observability) and story 3.3 (db)
#
# PROD NOTE: authored for `terraform plan`; apply gated per PROD_CUTOVER.md.
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
#
# PROD NOTE: authored for `terraform plan`; apply gated per PROD_CUTOVER.md.
# ---------------------------------------------------------------------------

module "iam_roles" {
  source = "../../modules/iam_roles"

  environment                   = var.environment
  aurora_master_user_secret_arn = module.aurora.master_user_secret_arn
}

# ---------------------------------------------------------------------------
# db_migrator Lambda — story 3.7
#
# PROD NOTE: authored for `terraform plan`; apply gated per PROD_CUTOVER.md
# via the DEPLOY_PROD=true gate in deploy.yml.
#
# AT-FLIP BEHAVIOR (N-new-3): when DEPLOY_PROD flips to true for the first
# time, the next apply-prod run will:
#   1. Create the db_migrator Lambda (and its IAM role, VPC config, etc.)
#   2. Immediately invoke it via the null_resource.db_migrator_invoke
#      local-exec below — this applies all yoyo migrations against prod
#      Aurora AND generates + stores the initial app_user credential.
# This is the desired prod cutover behavior: schema migration and credential
# bootstrap are atomic with the first apply.  Do NOT split the null_resource
# into a separate gated step — the migrator must run on the same apply that
# creates it.
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

  vpc_config = {
    subnet_ids = module.networking.private_subnet_ids
    security_group_ids = [
      module.networking.lambda_security_group_id,
      module.networking.aurora_security_group_id,
    ]
  }

  environment_variables = {
    AURORA_MASTER_SECRET_ARN = module.aurora.master_user_secret_arn
    APP_USER_SECRET_NAME     = "knotify-${var.environment}-app-user-credential"

    # Connection endpoint params. Aurora's managed master secret only
    # contains username/password — host/port/database are exposed via
    # the cluster endpoint outputs, not embedded in the secret.
    AURORA_HOST   = module.aurora.cluster_endpoint
    AURORA_PORT   = tostring(module.aurora.port)
    AURORA_DBNAME = module.aurora.database_name
  }
}

resource "null_resource" "db_migrator_invoke" {
  triggers = {
    migrations_hash = sha256(jsonencode([
      for f in sort(fileset("${path.module}/../../../infrastructure/db/migrations", "*.sql")) :
      filesha256("${path.module}/../../../infrastructure/db/migrations/${f}")
    ]))
    lambda_version = module.db_migrator.function_version
  }

  # See dev/main.tf for the rationale on the retry loop — absorbs both the
  # post-modify Lambda warm-up (ENI/version propagation) and Aurora cold
  # boot after a destroy round-trip. ~4 min total budget.
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
# PROD NOTE: authored for `terraform plan`; apply gated per PROD_CUTOVER.md.
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
    DB_SECRET_NAME = "knotify-${var.environment}-app-user-credential"

    # Aurora connection endpoint params — secret carries only username +
    # password (db_migrator writer pattern); host/port/dbname come from
    # aurora module outputs.
    AURORA_HOST   = module.aurora.cluster_endpoint
    AURORA_PORT   = tostring(module.aurora.port)
    AURORA_DBNAME = module.aurora.database_name
  }
}

# ---------------------------------------------------------------------------
# cognito_pre_token_generation Lambda — story 4.4
#
# PROD NOTE: authored for `terraform plan`; apply gated per PROD_CUTOVER.md.
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
    DB_SECRET_NAME = "knotify-${var.environment}-app-user-credential"

    # Aurora connection endpoint params — see cognito_post_confirmation
    # above for rationale.
    AURORA_HOST   = module.aurora.cluster_endpoint
    AURORA_PORT   = tostring(module.aurora.port)
    AURORA_DBNAME = module.aurora.database_name
  }
}

# ---------------------------------------------------------------------------
# Cognito User Pool — story 4.1 / 4.2 / 4.3 / 4.4
#
# PROD NOTE: authored for `terraform plan`; apply gated per PROD_CUTOVER.md.
#
# Cognito Advanced Security set to AUDIT minimum to enable V2 PreTokenGeneration
# (story 4.4 / brainstorm B2). ENFORCED upgrade and MFA enforcement deferred
# to phase 11. See architecture.md §13 #1.
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
# prod path: domain_name = "" (module default) until the prod cutover
# documented in docs/PROD_CUTOVER.md §4b. Once domain_name and hosted_zone_id
# are set in prod.tfvars, this module creates the cert, DNS validation records,
# and the aws_acm_certificate_validation wait resource in us-east-1.
# The alias hand-off is exercised here so the providers block is validated
# end-to-end even in the current zero-resource state.
#
# PROD NOTE: authored for `terraform plan`; cert apply requires setting
# domain_name + hosted_zone_id in prod.tfvars per PROD_CUTOVER.md §4b.
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
# PROD NOTE: authored for `terraform plan`; apply gated per PROD_CUTOVER.md.
# ---------------------------------------------------------------------------

module "api_gateway" {
  source = "../../modules/api_gateway"

  name = "knotify-${var.environment}-api"

  cognito_user_pool_endpoint = module.cognito.user_pool_endpoint

  # prod: integration_test_app_client_id is "" → compact() drops it,
  # leaving only the production app client in the audience list.
  cognito_audience_client_ids = compact([module.cognito.app_client_id, module.cognito.integration_test_app_client_id])

  throttling_burst_limit = var.api_gateway_throttling_burst_limit
  throttling_rate_limit  = var.api_gateway_throttling_rate_limit
}

# ---------------------------------------------------------------------------
# WAF web ACL — story 5.4
#
# PROD NOTE: authored for `terraform plan`; apply gated per PROD_CUTOVER.md.
#
# CLOUDFRONT-scoped WAF must reside in us-east-1 (CloudFront control plane).
# Declared BEFORE the CloudFront distribution so the distribution can attach
# the ACL via its web_acl_id field (WAFv2 AssociateWebACL does not accept
# CloudFront resource ARNs, so the attachment must happen on the distribution
# side, not via aws_wafv2_web_acl_association).
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

  environment = var.environment
}

# ---------------------------------------------------------------------------
# CloudFront distribution — story 5.3
#
# PROD NOTE: authored for `terraform plan`; apply gated per PROD_CUTOVER.md.
#
# prod path: domain_name is set in prod.tfvars once the prod cutover begins
# (docs/PROD_CUTOVER.md §4c). Until then domain_name = "" (module default)
# and cloudfront_default_certificate is used, matching the dev behaviour.
#
# web_acl_id accepts the WAFv2 ARN directly — supported path for
# CloudFront-scoped WAFs.
# ---------------------------------------------------------------------------

module "cloudfront" {
  source = "../../modules/cloudfront"

  api_gateway_domain_name = replace(module.api_gateway.execute_api_endpoint, "https://", "")
  domain_name             = var.domain_name
  acm_certificate_arn     = module.acm.certificate_arn
  web_acl_id              = module.waf.web_acl_arn
}

# ---------------------------------------------------------------------------
# Route 53 A-alias record — story 5.5
#
# prod path: domain_name and hosted_zone_id default to "" (module defaults)
# until the prod cutover documented in docs/PROD_CUTOVER.md §4d. Once both
# are set in prod.tfvars, this module creates one A-alias record pointing
# the custom domain at the CloudFront distribution.
#
# cloudfront_hosted_zone_id is the CloudFront global hosted zone ID — the
# same well-known constant (Z2FDTNDATAQYW2) in every AWS account.
#
# PROD NOTE: authored for `terraform plan`; alias record apply requires
# setting domain_name + hosted_zone_id in prod.tfvars per PROD_CUTOVER.md §4d.
# ---------------------------------------------------------------------------

module "route53" {
  source = "../../modules/route53"

  domain_name                         = var.domain_name
  hosted_zone_id                      = var.hosted_zone_id
  cloudfront_distribution_domain_name = module.cloudfront.distribution_domain_name
  cloudfront_hosted_zone_id           = "Z2FDTNDATAQYW2"
}

# ---------------------------------------------------------------------------
# Integration test environment file — story 5.6
#
# PROD NOTE: authored for completeness so `terraform plan` succeeds; the file
# would be written at the path relative to the prod environment directory.
# In practice, integration tests run against dev only; this resource is
# included for consistency so plan output is clean on both envs.
#
# Same path resolution as dev: path.root = infrastructure/environments/prod;
# ../../src/tests/integration/.env.test resolves to the repo-relative path.
# ---------------------------------------------------------------------------

resource "local_file" "integration_test_env" {
  filename        = "${path.root}/../../src/tests/integration/.env.test"
  file_permission = "0600"
  content = join("\n", [
    "EXECUTE_API_ENDPOINT=${module.api_gateway.execute_api_endpoint}",
    "DISTRIBUTION_DOMAIN_NAME=${module.cloudfront.distribution_domain_name}",
    "EDGE_SECRET=${module.cloudfront.edge_secret}",
    "",
  ])
}

# ---------------------------------------------------------------------------
# knotify-profile Lambda — story 6.1
#
# PROD NOTE: authored for `terraform plan`; apply gated per PROD_CUTOVER.md.
# Mirrors the dev wiring exactly. Four routes (GET/PATCH /v1/profile/me,
# GET /v1/profiles, GET /v1/profiles/{userId}) with JWT authorization.
# ---------------------------------------------------------------------------

module "profile" {
  source = "../../modules/lambda"

  function_name = "knotify-profile-${var.environment}"
  handler       = "handler.handler"
  filename      = "${path.module}/../../../build/profile.zip"

  layers = [
    module.observability_layer.layer_arn,
    module.db_layer.layer_arn,
  ]

  role_arn = module.iam_roles.role_arns["aurora_writer"]

  vpc_config = {
    subnet_ids         = module.networking.private_subnet_ids
    security_group_ids = [module.networking.lambda_security_group_id]
  }

  environment_variables = {
    DB_SECRET_NAME = "knotify-${var.environment}-app-user-credential"
    EDGE_SECRET    = module.cloudfront.edge_secret

    # Aurora connection endpoint params — secret carries only username +
    # password (db_migrator writer pattern); host/port/dbname come from
    # aurora module outputs.
    AURORA_HOST   = module.aurora.cluster_endpoint
    AURORA_PORT   = tostring(module.aurora.port)
    AURORA_DBNAME = module.aurora.database_name
  }
}

resource "aws_apigatewayv2_integration" "profile" {
  api_id                 = module.api_gateway.api_id
  integration_type       = "AWS_PROXY"
  integration_uri        = module.profile.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "get_profile_me" {
  api_id             = module.api_gateway.api_id
  route_key          = "GET /v1/profile/me"
  target             = "integrations/${aws_apigatewayv2_integration.profile.id}"
  authorization_type = "JWT"
  authorizer_id      = module.api_gateway.authorizer_id
}

resource "aws_apigatewayv2_route" "patch_profile_me" {
  api_id             = module.api_gateway.api_id
  route_key          = "PATCH /v1/profile/me"
  target             = "integrations/${aws_apigatewayv2_integration.profile.id}"
  authorization_type = "JWT"
  authorizer_id      = module.api_gateway.authorizer_id
}

resource "aws_apigatewayv2_route" "get_profiles" {
  api_id             = module.api_gateway.api_id
  route_key          = "GET /v1/profiles"
  target             = "integrations/${aws_apigatewayv2_integration.profile.id}"
  authorization_type = "JWT"
  authorizer_id      = module.api_gateway.authorizer_id
}

resource "aws_apigatewayv2_route" "get_profiles_by_id" {
  api_id             = module.api_gateway.api_id
  route_key          = "GET /v1/profiles/{userId}"
  target             = "integrations/${aws_apigatewayv2_integration.profile.id}"
  authorization_type = "JWT"
  authorizer_id      = module.api_gateway.authorizer_id
}

resource "aws_lambda_permission" "profile_api_gateway" {
  statement_id  = "AllowProfileAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.profile.function_name
  qualifier     = "live"
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${module.api_gateway.api_execution_arn}/*/*/v1/profile*"
}

# ---------------------------------------------------------------------------
# knotify-blocks Lambda — story 6.4
#
# PROD NOTE: authored for `terraform plan`; apply gated per PROD_CUTOVER.md.
# Mirrors the dev wiring exactly. Three routes (GET/POST /v1/blocks,
# DELETE /v1/blocks/{userId}) with JWT authorization.
# ---------------------------------------------------------------------------

module "blocks" {
  source = "../../modules/lambda"

  function_name = "knotify-blocks-${var.environment}"
  handler       = "handler.handler"
  filename      = "${path.module}/../../../build/blocks.zip"

  layers = [
    module.observability_layer.layer_arn,
    module.db_layer.layer_arn,
  ]

  role_arn = module.iam_roles.role_arns["blocks_writer"]

  vpc_config = {
    subnet_ids         = module.networking.private_subnet_ids
    security_group_ids = [module.networking.lambda_security_group_id]
  }

  environment_variables = {
    DB_SECRET_NAME   = "knotify-${var.environment}-app-user-credential"
    EDGE_SECRET      = module.cloudfront.edge_secret
    TABLE_CHAT_ROOMS = module.dynamodb.chat_rooms_table_name

    # Aurora connection endpoint params — see profile Lambda above for
    # rationale.
    AURORA_HOST   = module.aurora.cluster_endpoint
    AURORA_PORT   = tostring(module.aurora.port)
    AURORA_DBNAME = module.aurora.database_name
  }
}

resource "aws_apigatewayv2_integration" "blocks" {
  api_id                 = module.api_gateway.api_id
  integration_type       = "AWS_PROXY"
  integration_uri        = module.blocks.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "get_blocks" {
  api_id             = module.api_gateway.api_id
  route_key          = "GET /v1/blocks"
  target             = "integrations/${aws_apigatewayv2_integration.blocks.id}"
  authorization_type = "JWT"
  authorizer_id      = module.api_gateway.authorizer_id
}

resource "aws_apigatewayv2_route" "post_blocks" {
  api_id             = module.api_gateway.api_id
  route_key          = "POST /v1/blocks"
  target             = "integrations/${aws_apigatewayv2_integration.blocks.id}"
  authorization_type = "JWT"
  authorizer_id      = module.api_gateway.authorizer_id
}

resource "aws_apigatewayv2_route" "delete_block" {
  api_id             = module.api_gateway.api_id
  route_key          = "DELETE /v1/blocks/{userId}"
  target             = "integrations/${aws_apigatewayv2_integration.blocks.id}"
  authorization_type = "JWT"
  authorizer_id      = module.api_gateway.authorizer_id
}

resource "aws_lambda_permission" "blocks_api_gateway" {
  statement_id  = "AllowBlocksAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.blocks.function_name
  qualifier     = "live"
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${module.api_gateway.api_execution_arn}/*/*/v1/blocks*"
}

# ---------------------------------------------------------------------------
# knotify-friends Lambda — story 6.2
#
# PROD NOTE: authored for `terraform plan`; apply gated per PROD_CUTOVER.md.
# Mirrors the dev wiring exactly. Seven routes (GET/DELETE /v1/friends,
# GET/POST /v1/friend-requests, POST accept/decline, DELETE request) with
# JWT authorization.
# ---------------------------------------------------------------------------

module "friends" {
  source = "../../modules/lambda"

  function_name = "knotify-friends-${var.environment}"
  handler       = "handler.handler"
  filename      = "${path.module}/../../../build/friends.zip"

  layers = [
    module.observability_layer.layer_arn,
    module.db_layer.layer_arn,
  ]

  role_arn = module.iam_roles.role_arns["aurora_writer"]

  vpc_config = {
    subnet_ids         = module.networking.private_subnet_ids
    security_group_ids = [module.networking.lambda_security_group_id]
  }

  environment_variables = {
    DB_SECRET_NAME = "knotify-${var.environment}-app-user-credential"
    EDGE_SECRET    = module.cloudfront.edge_secret

    # Aurora connection endpoint params — see profile Lambda above for
    # rationale.
    AURORA_HOST   = module.aurora.cluster_endpoint
    AURORA_PORT   = tostring(module.aurora.port)
    AURORA_DBNAME = module.aurora.database_name
  }
}

resource "aws_apigatewayv2_integration" "friends" {
  api_id                 = module.api_gateway.api_id
  integration_type       = "AWS_PROXY"
  integration_uri        = module.friends.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "get_friends" {
  api_id             = module.api_gateway.api_id
  route_key          = "GET /v1/friends"
  target             = "integrations/${aws_apigatewayv2_integration.friends.id}"
  authorization_type = "JWT"
  authorizer_id      = module.api_gateway.authorizer_id
}

resource "aws_apigatewayv2_route" "delete_friend" {
  api_id             = module.api_gateway.api_id
  route_key          = "DELETE /v1/friends/{userId}"
  target             = "integrations/${aws_apigatewayv2_integration.friends.id}"
  authorization_type = "JWT"
  authorizer_id      = module.api_gateway.authorizer_id
}

resource "aws_apigatewayv2_route" "get_friend_requests" {
  api_id             = module.api_gateway.api_id
  route_key          = "GET /v1/friend-requests"
  target             = "integrations/${aws_apigatewayv2_integration.friends.id}"
  authorization_type = "JWT"
  authorizer_id      = module.api_gateway.authorizer_id
}

resource "aws_apigatewayv2_route" "post_friend_requests" {
  api_id             = module.api_gateway.api_id
  route_key          = "POST /v1/friend-requests"
  target             = "integrations/${aws_apigatewayv2_integration.friends.id}"
  authorization_type = "JWT"
  authorizer_id      = module.api_gateway.authorizer_id
}

resource "aws_apigatewayv2_route" "post_friend_requests_accept" {
  api_id             = module.api_gateway.api_id
  route_key          = "POST /v1/friend-requests/{id}/accept"
  target             = "integrations/${aws_apigatewayv2_integration.friends.id}"
  authorization_type = "JWT"
  authorizer_id      = module.api_gateway.authorizer_id
}

resource "aws_apigatewayv2_route" "post_friend_requests_decline" {
  api_id             = module.api_gateway.api_id
  route_key          = "POST /v1/friend-requests/{id}/decline"
  target             = "integrations/${aws_apigatewayv2_integration.friends.id}"
  authorization_type = "JWT"
  authorizer_id      = module.api_gateway.authorizer_id
}

resource "aws_apigatewayv2_route" "delete_friend_request" {
  api_id             = module.api_gateway.api_id
  route_key          = "DELETE /v1/friend-requests/{id}"
  target             = "integrations/${aws_apigatewayv2_integration.friends.id}"
  authorization_type = "JWT"
  authorizer_id      = module.api_gateway.authorizer_id
}

resource "aws_lambda_permission" "friends_api_gateway" {
  statement_id  = "AllowFriendsAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.friends.function_name
  qualifier     = "live"
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${module.api_gateway.api_execution_arn}/*/*/v1/friend*"
}

# ---------------------------------------------------------------------------
# knotify-bookmarks Lambda — story 6.3
#
# PROD NOTE: authored for `terraform plan`; apply gated per PROD_CUTOVER.md.
# Mirrors the dev wiring exactly. Three routes (GET/POST /v1/bookmarks,
# DELETE /v1/bookmarks/{userId}) with JWT authorization.
# ---------------------------------------------------------------------------

module "bookmarks" {
  source = "../../modules/lambda"

  function_name = "knotify-bookmarks-${var.environment}"
  handler       = "handler.handler"
  filename      = "${path.module}/../../../build/bookmarks.zip"

  layers = [
    module.observability_layer.layer_arn,
    module.db_layer.layer_arn,
  ]

  role_arn = module.iam_roles.role_arns["aurora_writer"]

  vpc_config = {
    subnet_ids         = module.networking.private_subnet_ids
    security_group_ids = [module.networking.lambda_security_group_id]
  }

  environment_variables = {
    DB_SECRET_NAME = "knotify-${var.environment}-app-user-credential"
    EDGE_SECRET    = module.cloudfront.edge_secret

    # Aurora connection endpoint params — see profile Lambda above for
    # rationale.
    AURORA_HOST   = module.aurora.cluster_endpoint
    AURORA_PORT   = tostring(module.aurora.port)
    AURORA_DBNAME = module.aurora.database_name
  }
}

resource "aws_apigatewayv2_integration" "bookmarks" {
  api_id                 = module.api_gateway.api_id
  integration_type       = "AWS_PROXY"
  integration_uri        = module.bookmarks.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "get_bookmarks" {
  api_id             = module.api_gateway.api_id
  route_key          = "GET /v1/bookmarks"
  target             = "integrations/${aws_apigatewayv2_integration.bookmarks.id}"
  authorization_type = "JWT"
  authorizer_id      = module.api_gateway.authorizer_id
}

resource "aws_apigatewayv2_route" "post_bookmarks" {
  api_id             = module.api_gateway.api_id
  route_key          = "POST /v1/bookmarks"
  target             = "integrations/${aws_apigatewayv2_integration.bookmarks.id}"
  authorization_type = "JWT"
  authorizer_id      = module.api_gateway.authorizer_id
}

resource "aws_apigatewayv2_route" "delete_bookmark" {
  api_id             = module.api_gateway.api_id
  route_key          = "DELETE /v1/bookmarks/{userId}"
  target             = "integrations/${aws_apigatewayv2_integration.bookmarks.id}"
  authorization_type = "JWT"
  authorizer_id      = module.api_gateway.authorizer_id
}

resource "aws_lambda_permission" "bookmarks_api_gateway" {
  statement_id  = "AllowBookmarksAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.bookmarks.function_name
  qualifier     = "live"
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${module.api_gateway.api_execution_arn}/*/*/v1/bookmarks*"
}

# ---------------------------------------------------------------------------
# knotify-match Lambda — story 7.0 scaffold
#
# Empty dispatcher (returns 404 for all routes until 7.5 wires them).
# Handles two routes registered in story 7.5:
#   POST /v1/match/search   — candidate search with preference-vector ranking
#   GET  /v1/match/deck     — swipe-deck with cursor pagination
#
# Uses the aurora_reader_match IAM role (Aurora read-only via app_user
# credential; no write access, no DynamoDB). EDGE_SECRET is injected so the
# @with_edge_secret decorator validates all traffic arrived via CloudFront.
# Routes are wired in story 7.5 (depends on 7.0b, 7.1, 7.2).
#
# PROD NOTE: apply is gated per docs/PROD_CUTOVER.md. Authored + planned only.
# ---------------------------------------------------------------------------

module "match" {
  source = "../../modules/lambda"

  function_name = "knotify-match-${var.environment}"
  handler       = "handler.handler"
  filename      = "${path.module}/../../../build/match.zip"

  layers = [
    module.observability_layer.layer_arn,
    module.db_layer.layer_arn,
  ]

  role_arn = module.iam_roles.role_arns["aurora_reader_match"]

  vpc_config = {
    subnet_ids         = module.networking.private_subnet_ids
    security_group_ids = [module.networking.lambda_security_group_id]
  }

  environment_variables = {
    DB_SECRET_NAME = "knotify-${var.environment}-app-user-credential"
    EDGE_SECRET    = module.cloudfront.edge_secret

    # Aurora connection endpoint params — secret carries only username +
    # password (db_migrator writer pattern); host/port/dbname come from
    # aurora module outputs.
    AURORA_HOST   = module.aurora.cluster_endpoint
    AURORA_PORT   = tostring(module.aurora.port)
    AURORA_DBNAME = module.aurora.database_name
  }
}
