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

  # Scopes aurora_writer's cognito-idp:AdminUpdateUserAttributes to this pool
  # only. Sourced from the cognito module output (story 7.0b).
  cognito_user_pool_arn = module.cognito.user_pool_arn

  # Scopes aurora_writer's lambda:InvokeFunction to the refresh Lambda ARN
  # only (story 7.4). Terraform resolves the forward reference from the
  # refresh_deck_view module declared later in this file.
  refresh_lambda_arn = module.refresh_deck_view.function_arn

  # Scope chat_resolver DynamoDB permissions to exact table ARNs (story 8.0).
  # Sourced from the dynamodb module outputs added in story 8.0.
  chat_rooms_table_arn           = module.dynamodb.chat_rooms_arn
  chat_room_membership_table_arn = module.dynamodb.chat_room_membership_arn
  chat_messages_table_arn        = module.dynamodb.chat_messages_arn
  message_reads_table_arn        = module.dynamodb.message_reads_arn
  notifications_table_arn        = module.dynamodb.notifications_arn

  # Scope appsync_chat_resolver_invoke's lambda:InvokeFunction to the exact
  # chat_resolver Lambda ARN (story 8.1). Forward reference resolved by Terraform.
  chat_resolver_lambda_arn = module.chat_resolver.lambda_arn

  # Scope room_state_publisher DynamoDB stream actions to ChatRooms stream ARN
  # (story 8.9a). Stream ARN now output by the dynamodb module.
  chat_rooms_stream_arn = module.dynamodb.chat_rooms_stream_arn

  # Scope room_state_publisher AppSync publish permissions to the exact
  # publish-mutation field ARNs (story 8.9a). API ARN sourced from appsync module.
  appsync_api_arn = module.appsync.api_arn

  # Scope notifications_publisher DynamoDB stream actions to Notifications stream ARN
  # (story 8.9c). Stream ARN sourced from dynamodb module outputs.tf:61.
  notifications_stream_arn = module.dynamodb.notifications_stream_arn

  # Scope push_fanout DynamoDB stream actions to ChatMessages stream ARN (story 8.10).
  chat_messages_stream_arn = module.dynamodb.chat_messages_stream_arn

  # Scope push_fanout DynamoDB data-plane permissions on PushNotificationTokens (story 8.10).
  push_notification_tokens_table_arn = module.dynamodb.push_notification_tokens_arn

  # Scope push_fanout Secrets Manager permission to the prod Expo push credential ARN.
  # Forward reference — Terraform resolves this after the secret resource below is declared.
  expo_push_secret_arn = aws_secretsmanager_secret.expo_push_credential.arn
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

    # Friendly name of the aurora_refresh credential secret to create/update
    # (story 7.4). Mirrors APP_USER_SECRET_NAME pattern.
    AURORA_REFRESH_SECRET_NAME = "knotify-${var.environment}-aurora-refresh-credential"

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

  # PATCH /v1/profile/me on the false→true profile_complete flip does
  # Aurora UPDATE + Cognito admin_update_user_attributes (3–4s from a VPC
  # Lambda via the cognito-idp interface endpoint) + lambda.invoke of the
  # refresh_deck_view Lambda. Worst case overruns the 10s module default;
  # direct invokes measured ~5s warm, longer on cold start. 30s gives
  # headroom without masking real latency regressions.
  timeout = 30

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

    # Cognito User Pool ID — needed by the profile PATCH handler to call
    # admin_update_user_attributes after a profile-completion flip (story 7.0b).
    USER_POOL_ID = module.cognito.user_pool_id

    # ARN of the refresh_deck_view Lambda — async-invoked by the profile PATCH
    # handler when profile_complete_verified flips false→true (story 7.4).
    REFRESH_LAMBDA_ARN = module.refresh_deck_view.function_arn
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
# knotify-friends Lambda — story 6.2, extended by story 8.9b
#
# PROD NOTE: authored for `terraform plan`; apply gated per PROD_CUTOVER.md.
# Mirrors the dev wiring exactly. Seven routes (GET/DELETE /v1/friends,
# GET/POST /v1/friend-requests, POST accept/decline, DELETE request) with
# JWT authorization.
#
# Role switched from aurora_writer to friends_writer (story 8.9b) to add
# scoped DynamoDB UpdateItem on ChatRooms for friendship_active maintenance.
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

  role_arn = module.iam_roles.role_arns["friends_writer"]

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

# ---------------------------------------------------------------------------
# API Gateway wiring — story 7.5
#
# One integration + two routes + one Lambda permission for the match Lambda.
# Both routes require JWT authorization (Cognito User Pool, same authorizer as
# all other routes in the HTTP API) and the @with_edge_secret + @require_profile_complete
# decorator chain enforced at the Lambda layer.
#
# Lambda permission source_arn MUST use api_execution_arn (not default_stage_arn).
# Per hotfix #86: using default_stage_arn causes 5xx with no Lambda invocation
# log entry because it is the management ARN, not the execute-api principal ARN.
#
# PROD NOTE: authored for `terraform plan`; apply gated per PROD_CUTOVER.md.
# Mirrors the dev wiring exactly.
# ---------------------------------------------------------------------------

resource "aws_apigatewayv2_integration" "match" {
  api_id                 = module.api_gateway.api_id
  integration_type       = "AWS_PROXY"
  integration_uri        = module.match.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "post_match_search" {
  api_id             = module.api_gateway.api_id
  route_key          = "POST /v1/match/search"
  target             = "integrations/${aws_apigatewayv2_integration.match.id}"
  authorization_type = "JWT"
  authorizer_id      = module.api_gateway.authorizer_id
}

resource "aws_apigatewayv2_route" "get_match_deck" {
  api_id             = module.api_gateway.api_id
  route_key          = "GET /v1/match/deck"
  target             = "integrations/${aws_apigatewayv2_integration.match.id}"
  authorization_type = "JWT"
  authorizer_id      = module.api_gateway.authorizer_id
}

# Lambda permission — scoped to all match routes on this API.
# source_arn uses api_execution_arn (execute-api ARN) + wildcard suffix per
# hotfix #86. The /v1/match* prefix covers both /v1/match/search and
# /v1/match/deck while remaining tightly scoped to the match Lambda.
resource "aws_lambda_permission" "match_api_gateway" {
  statement_id  = "AllowMatchAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.match.function_name
  qualifier     = "live"
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${module.api_gateway.api_execution_arn}/*/*/v1/match*"
}

# ---------------------------------------------------------------------------
# knotify-chat-resolver Lambda — story 8.0 scaffold
#
# AppSync Lambda resolver for all chat domain fields.  Story 8.0 ships an
# empty dispatcher; subsequent stories extend handler.py.
#
# PROD NOTE: authored for `terraform plan`; apply gated per PROD_CUTOVER.md.
# Mirrors the dev wiring exactly.  No API Gateway integration.
# ---------------------------------------------------------------------------

module "chat_resolver" {
  source = "../../modules/chat_resolver"

  environment   = var.environment
  function_name = "knotify-chat-resolver-${var.environment}"
  filename      = "${path.module}/../../../build/chat_resolver.zip"
  role_arn      = module.iam_roles.role_arns["chat_resolver"]

  layers = [
    module.observability_layer.layer_arn,
    module.db_layer.layer_arn,
  ]

  vpc_config = {
    subnet_ids         = module.networking.private_subnet_ids
    security_group_ids = [module.networking.lambda_security_group_id]
  }

  db_secret_name = "knotify-${var.environment}-app-user-credential"
  aurora_host    = module.aurora.cluster_endpoint
  aurora_port    = tostring(module.aurora.port)
  aurora_dbname  = module.aurora.database_name
}

# ---------------------------------------------------------------------------
# AppSync GraphQL API — story 8.1
#
# PROD NOTE: authored for `terraform plan`; apply gated per PROD_CUTOVER.md.
# Mirrors the dev wiring exactly.
#
# Provisions the knotify chat AppSync API with:
#   - Primary auth: AMAZON_COGNITO_USER_POOLS (Cognito JWT for client access)
#   - Secondary auth: AWS_IAM (backend publisher Lambdas: 8.9a, 8.9c)
#   - Five DynamoDB datasources (five chat domain tables)
#   - One Lambda datasource (chat_resolver_ds → chat_resolver Lambda)
#   - log_config at ALL field-level detail to CloudWatch (7-day retention)
#
# Schema is a placeholder until story 8.2 ships the full hand-written SDL.
# ---------------------------------------------------------------------------

module "appsync" {
  source = "../../modules/appsync"

  environment = var.environment

  # Cognito User Pool — primary auth mode
  user_pool_id = module.cognito.user_pool_id

  # IAM roles from iam_roles module (story 8.1)
  appsync_logs_role_arn   = module.iam_roles.role_arns["appsync_logs"]
  appsync_invoke_role_arn = module.iam_roles.role_arns["appsync_chat_resolver_invoke"]

  # chat_resolver Lambda datasource
  chat_resolver_lambda_arn = module.chat_resolver.lambda_arn

  # Five chat domain DynamoDB datasources
  chat_rooms_table_name           = module.dynamodb.chat_rooms_table_name
  chat_rooms_table_arn            = module.dynamodb.chat_rooms_arn
  chat_room_membership_table_name = module.dynamodb.chat_room_membership_table_name
  chat_room_membership_table_arn  = module.dynamodb.chat_room_membership_arn
  chat_messages_table_name        = module.dynamodb.chat_messages_table_name
  chat_messages_table_arn         = module.dynamodb.chat_messages_arn
  message_reads_table_name        = module.dynamodb.message_reads_table_name
  message_reads_table_arn         = module.dynamodb.message_reads_arn
  notifications_table_name        = module.dynamodb.notifications_table_name
  notifications_table_arn         = module.dynamodb.notifications_arn

  # DynamoDB service role — the chat_resolver IAM role grants DDB access (story 8.0)
  dynamodb_role_arn = module.iam_roles.role_arns["chat_resolver"]
}

# ---------------------------------------------------------------------------
# knotify-refresh-deck-view Lambda — story 7.4
#
# PROD NOTE: authored for `terraform plan`; apply gated per PROD_CUTOVER.md.
# Mirrors the dev wiring exactly. No API Gateway integration.
# ---------------------------------------------------------------------------

module "refresh_deck_view" {
  source = "../../modules/refresh_deck_view"

  environment   = var.environment
  function_name = "knotify-refresh-deck-view-${var.environment}"
  filename      = "${path.module}/../../../build/refresh_deck_view.zip"
  role_arn      = module.iam_roles.role_arns["aurora_refresh_lambda"]

  layers = [
    module.observability_layer.layer_arn,
    module.db_layer.layer_arn,
  ]

  vpc_config = {
    subnet_ids         = module.networking.private_subnet_ids
    security_group_ids = [module.networking.lambda_security_group_id]
  }

  # The aurora_refresh credential — NOT the app_user credential.
  db_secret_name = "knotify-${var.environment}-aurora-refresh-credential"

  # Aurora connection endpoint params — aurora_refresh credential carries only
  # username + password; host/port/dbname come from aurora module outputs.
  aurora_host   = module.aurora.cluster_endpoint
  aurora_port   = tostring(module.aurora.port)
  aurora_dbname = module.aurora.database_name

  # EventBridge schedule: every 15 minutes.
  schedule_expression = "rate(15 minutes)"
}

# ---------------------------------------------------------------------------
# room_state_publisher Lambda — story 8.9a
#
# PROD NOTE: authored for `terraform plan`; apply gated per PROD_CUTOVER.md.
# Mirrors the dev wiring exactly.
#
# Placement: OUTSIDE the VPC — AppSync HTTPS reachable via public DNS.
# No Aurora access — DynamoDB stream only.
# ---------------------------------------------------------------------------

module "room_state_publisher" {
  source = "../../modules/room_state_publisher"

  environment           = var.environment
  function_name         = "knotify-room-state-publisher-${var.environment}"
  filename              = "${path.module}/../../../build/room_state_publisher.zip"
  role_arn              = module.iam_roles.role_arns["room_state_publisher"]
  chat_rooms_stream_arn = module.dynamodb.chat_rooms_stream_arn
  appsync_graphql_url   = module.appsync.graphql_url
}

# ---------------------------------------------------------------------------
# notifications_publisher Lambda — story 8.9c
#
# PROD NOTE: authored for `terraform plan`; apply gated per PROD_CUTOVER.md.
# Mirrors the dev wiring exactly.
#
# Placement: OUTSIDE the VPC — AppSync HTTPS reachable via public DNS.
# No Aurora access — Notifications DynamoDB stream only.
# ---------------------------------------------------------------------------

module "notifications_publisher" {
  source = "../../modules/notifications_publisher"

  environment              = var.environment
  function_name            = "knotify-notifications-publisher-${var.environment}"
  filename                 = "${path.module}/../../../build/notifications_publisher.zip"
  role_arn                 = module.iam_roles.role_arns["notifications_publisher"]
  notifications_stream_arn = module.dynamodb.notifications_stream_arn
  appsync_graphql_url      = module.appsync.graphql_url
}

# ---------------------------------------------------------------------------
# Expo push credential — Secrets Manager secret (prod only)
#
# PROD NOTE: authored for `terraform plan`; apply gated per PROD_CUTOVER.md.
#
# The actual secret value (the Expo access token) is placed manually by an
# operator via the AWS Console or CLI. Terraform creates the secret resource
# without a value on first apply; subsequent plans respect the
# lifecycle.ignore_changes = [secret_string] annotation so Terraform never
# overwrites an operator-set value.
#
# The push_fanout Lambda reads this secret on cold start when EXPO_AUTH_MODE=bearer.
# Secret name matches the hardcoded constant in handler.py: knotify-prod-expo-push-credential.
# ---------------------------------------------------------------------------

resource "aws_secretsmanager_secret" "expo_push_credential" {
  name        = "knotify-prod-expo-push-credential"
  description = "Expo Push API access token for knotify prod push notifications (story 8.10). Value set manually by operator."

  # Recovery window of 7 days — allows accidental deletion to be undone.
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "expo_push_credential" {
  secret_id     = aws_secretsmanager_secret.expo_push_credential.id
  secret_string = "placeholder-replace-with-real-expo-token"

  lifecycle {
    # Operator sets the real token value manually; Terraform must never overwrite it.
    ignore_changes = [secret_string]
  }
}

# ---------------------------------------------------------------------------
# push_fanout Lambda — story 8.10
#
# PROD NOTE: authored for `terraform plan`; apply gated per PROD_CUTOVER.md.
# Mirrors the dev wiring with EXPO_AUTH_MODE=bearer (Secrets Manager token).
#
# Placement: OUTSIDE the VPC — only touches DynamoDB and Expo (open internet).
#
# CONSUMER LIMIT: Notifications stream now has 2 ESM consumers
# (notifications_publisher + push_fanout), which is the AWS default limit.
# ---------------------------------------------------------------------------

module "push_fanout" {
  source = "../../modules/push_fanout"

  environment              = var.environment
  function_name            = "knotify-push-fanout-${var.environment}"
  filename                 = "${path.module}/../../../build/push_fanout.zip"
  role_arn                 = module.iam_roles.role_arns["push_fanout"]
  chat_messages_stream_arn = module.dynamodb.chat_messages_stream_arn
  notifications_stream_arn = module.dynamodb.notifications_stream_arn
  expo_push_url            = "https://exp.host/--/api/v2/push/send"
  expo_auth_mode           = "bearer"
}

# ---------------------------------------------------------------------------
# knotify-push-tokens Lambda — story 8.11
#
# PROD NOTE: authored for `terraform plan`; apply gated per PROD_CUTOVER.md.
# Mirrors the dev wiring (identical role, module, and route wiring).
#
# Handles one route:
#   POST /v1/push-tokens — register or refresh a device push notification token
#
# NOT gated by @require_profile_complete — token registration happens at first
# app launch before onboarding completes.
# JWT authorizer is enforced by the HTTP API Cognito authorizer (API Gateway).
#
# Uses the push_tokens IAM role (dynamodb:PutItem on PushNotificationTokens only).
# Placed in the VPC (private subnets + lambda SG) to reach the DynamoDB VPC
# endpoint, consistent with other REST Lambdas.
# ---------------------------------------------------------------------------

module "push_tokens" {
  source = "../../modules/push_tokens"

  environment   = var.environment
  function_name = "knotify-push-tokens-${var.environment}"
  filename      = "${path.module}/../../../build/push_tokens.zip"
  role_arn      = module.iam_roles.role_arns["push_tokens"]

  layers = [
    module.observability_layer.layer_arn,
  ]

  vpc_config = {
    subnet_ids         = module.networking.private_subnet_ids
    security_group_ids = [module.networking.lambda_security_group_id]
  }

  table_push_tokens_name = module.dynamodb.push_tokens_table_name
}

# ---------------------------------------------------------------------------
# API Gateway wiring — story 8.11 (prod mirror)
#
# One integration + one route + one Lambda permission.
# Route uses JWT authorization (Cognito User Pool, same authorizer as all
# other routes in the HTTP API).
#
# Lambda permission source_arn MUST use api_execution_arn (not default_stage_arn).
# Per hotfix #86: using default_stage_arn causes 5xx with no Lambda invocation
# log entry because it is the management ARN, not the execute-api principal ARN.
# ---------------------------------------------------------------------------

resource "aws_apigatewayv2_integration" "push_tokens" {
  api_id                 = module.api_gateway.api_id
  integration_type       = "AWS_PROXY"
  integration_uri        = module.push_tokens.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "post_push_tokens" {
  api_id             = module.api_gateway.api_id
  route_key          = "POST /v1/push-tokens"
  target             = "integrations/${aws_apigatewayv2_integration.push_tokens.id}"
  authorization_type = "JWT"
  authorizer_id      = module.api_gateway.authorizer_id
}

# Lambda permission — scoped to the push-tokens route on this API.
# source_arn uses api_execution_arn (execute-api ARN) per hotfix #86 lesson.
resource "aws_lambda_permission" "push_tokens_api_gateway" {
  statement_id  = "AllowPushTokensAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.push_tokens.function_name
  qualifier     = "live"
  principal     = "apigateway.amazonaws.com"
  # Scoped to POST /v1/push-tokens via the execute-api ARN.
  # source_arn = api_execution_arn (NOT default_stage_arn) per hotfix #86.
  source_arn = "${module.api_gateway.api_execution_arn}/*/*/v1/push-tokens"
}

# ---------------------------------------------------------------------------
# stale_token_cleanup Lambda — story 8.12 (prod mirror)
#
# PROD NOTE: authored for `terraform plan`; apply gated per PROD_CUTOVER.md.
# Mirrors the dev wiring (identical role, module configuration).
#
# Daily EventBridge cron: scan PushNotificationTokens, delete rows whose
# last_seen is older than 60 days.
#
# Placement: OUTSIDE the VPC — only touches DynamoDB (no Aurora, no external
# HTTP).  Running outside the VPC avoids the hotfix #106 blackhole trap.
#
# No layers required: this Lambda only needs boto3 (bundled in the runtime).
# ---------------------------------------------------------------------------

module "stale_token_cleanup" {
  source = "../../modules/stale_token_cleanup"

  environment            = var.environment
  function_name          = "knotify-stale-token-cleanup-${var.environment}"
  filename               = "${path.module}/../../../build/stale_token_cleanup.zip"
  role_arn               = module.iam_roles.role_arns["stale_token_cleanup"]
  table_push_tokens_name = module.dynamodb.push_tokens_table_name
}
