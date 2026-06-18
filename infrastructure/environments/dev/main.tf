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
  # chat_resolver Lambda ARN (story 8.1). Forward reference — Terraform
  # resolves this after the chat_resolver module is declared below.
  chat_resolver_lambda_arn = module.chat_resolver.lambda_arn

  # Scope room_state_publisher DynamoDB stream actions to ChatRooms stream ARN
  # (story 8.9a). Stream ARN is now output by the dynamodb module.
  chat_rooms_stream_arn = module.dynamodb.chat_rooms_stream_arn

  # Scope room_state_publisher AppSync publish permissions to the exact
  # publish-mutation field ARNs (story 8.9a). API ARN sourced from appsync module.
  appsync_api_arn = module.appsync.api_arn
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

    # Friendly name of the aurora_refresh credential secret to create/update
    # (story 7.4). Mirrors APP_USER_SECRET_NAME pattern — same create-or-update
    # idempotency; no new abstraction.
    AURORA_REFRESH_SECRET_NAME = "knotify-${var.environment}-aurora-refresh-credential"

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

    # Aurora connection endpoint params — the app_user_credential secret
    # carries only username + password (matching the db_migrator writer
    # pattern), so the connection host / port / dbname come from these env
    # vars sourced from the aurora module outputs.
    AURORA_HOST   = module.aurora.cluster_endpoint
    AURORA_PORT   = tostring(module.aurora.port)
    AURORA_DBNAME = module.aurora.database_name
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
# WAF web ACL — story 5.4
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
#
# web_acl_id accepts the WAFv2 ARN directly — this is the supported path for
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
# dev path: domain_name = "" and hosted_zone_id = "" → module produces ZERO
# resources (count = 0 branch). No Route 53 hosted zone is required in dev;
# the app connects via the auto-generated d*.cloudfront.net hostname.
#
# cloudfront_hosted_zone_id is the CloudFront global hosted zone ID — the
# same well-known constant (Z2FDTNDATAQYW2) in every AWS account.
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
# Writes three Terraform outputs to infrastructure/src/tests/integration/.env.test
# so the phase-5.7 integration tests (and future phase-6 tests) can load
# endpoint URLs and the edge secret without hard-coding them.
#
# The file contains only key=value pairs (no shell export syntax); test code
# uses python-dotenv or a simple parser to read them.
#
# file_permission = "0600" — the file contains the edge secret (a sensitive
# Terraform value). Terraform writes it but the plan/apply output shows
# (sensitive value) for the content field.
#
# path.root resolves to infrastructure/environments/dev; the relative path
# ../../src/tests/integration/.env.test resolves to the correct repo-relative
# path regardless of which directory terraform is invoked from.
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
# Handles four routes:
#   GET  /v1/profile/me              — own full profile (RLS own-row exception)
#   PATCH /v1/profile/me             — partial update with immutable-field semantics
#   GET  /v1/profiles?username=...   — case-insensitive username search (deck-view)
#   GET  /v1/profiles/{userId}       — profile by ID (deck-view only)
#
# Uses the aurora_writer IAM role (Aurora rights via app_user credential;
# no DynamoDB access needed for profile operations).
# EDGE_SECRET is injected so the @with_edge_secret decorator can validate
# that all traffic arrived via CloudFront.
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

# ---------------------------------------------------------------------------
# API Gateway wiring — story 6.1
#
# One integration + four routes + one Lambda permission.
# All routes use JWT authorization (Cognito User Pool, same authorizer as
# every other route in the HTTP API).
# ---------------------------------------------------------------------------

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

# Lambda permission — scoped to the four profile routes' source ARNs.
# Using a wildcard over the profile function's paths avoids having to
# enumerate the execute-API ARN pattern for each route individually while
# remaining tightly scoped to the profile function (not the entire API).
resource "aws_lambda_permission" "profile_api_gateway" {
  statement_id  = "AllowProfileAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.profile.function_name
  qualifier     = "live"
  principal     = "apigateway.amazonaws.com"
  # Scoped to all routes on this API — the route-level JWT authorizer already
  # gates access so wildcard source_arn within this API is safe.
  source_arn = "${module.api_gateway.api_execution_arn}/*/*/v1/profile*"
}

# ---------------------------------------------------------------------------
# knotify-blocks Lambda — story 6.4
#
# Handles three routes:
#   GET    /v1/blocks           — list the caller's block list
#   POST   /v1/blocks           — block a current friend (auto-unfriend +
#                                 deactivate chat room)
#   DELETE /v1/blocks/{userId}  — unblock a user (reactivate chat room)
#
# Uses the blocks_writer IAM role (Aurora + DynamoDB:UpdateItem on ChatRooms).
# EDGE_SECRET is injected so the @with_edge_secret decorator validates all
# traffic arrived via CloudFront (not via the raw execute-api endpoint).
# TABLE_CHAT_ROOMS is resolved from the dynamodb module output.
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

# ---------------------------------------------------------------------------
# API Gateway wiring — story 6.4
#
# One integration + three routes + one Lambda permission.
# All routes use JWT authorization (Cognito User Pool, same authorizer as
# every other route in the HTTP API).
# ---------------------------------------------------------------------------

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

# Lambda permission — scoped to all blocks routes on this API.
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
# Handles seven routes:
#   GET    /v1/friends                          — block-filtered friend list
#   DELETE /v1/friends/{userId}                 — remove a friendship
#   GET    /v1/friend-requests                  — block-filtered request list
#   POST   /v1/friend-requests                  — send a request (block-aware)
#   POST   /v1/friend-requests/{id}/accept      — accept a pending request
#   POST   /v1/friend-requests/{id}/decline     — decline a pending request
#   DELETE /v1/friend-requests/{id}             — cancel an outgoing request
#
# Uses the aurora_writer IAM role (Aurora rights via app_user credential;
# no DynamoDB access needed — friends operations are Aurora-only).
# EDGE_SECRET is injected so the @with_edge_secret decorator validates all
# traffic arrived via CloudFront.
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

# ---------------------------------------------------------------------------
# API Gateway wiring — story 6.2
#
# One integration + seven routes + one Lambda permission.
# All routes use JWT authorization (Cognito User Pool, same authorizer as
# every other route in the HTTP API).
# ---------------------------------------------------------------------------

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

# Lambda permission — single wildcard covering both /v1/friends* and
# /v1/friend-requests* via the /v1/friend* prefix. This is intentionally
# broad within the friends function boundary; the JWT authorizer gates
# every request before it reaches the Lambda.
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
# Handles three routes:
#   GET    /v1/bookmarks            — block-filtered bookmark list
#   POST   /v1/bookmarks            — bookmark a user (idempotent, block-aware)
#   DELETE /v1/bookmarks/{userId}   — remove a bookmark (idempotent)
#
# Uses the aurora_writer IAM role (Aurora-only; no DynamoDB access needed).
# EDGE_SECRET is injected so the @with_edge_secret decorator validates all
# traffic arrived via CloudFront (not via the raw execute-api endpoint).
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

# ---------------------------------------------------------------------------
# API Gateway wiring — story 6.3
#
# One integration + three routes + one Lambda permission.
# All routes use JWT authorization (Cognito User Pool, same authorizer as
# every other route in the HTTP API).
# ---------------------------------------------------------------------------

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

# Lambda permission — scoped to all bookmarks routes on this API.
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
# AppSync Lambda resolver for all chat domain fields (Query, Mutation, and
# Subscription types).  Story 8.0 ships an empty dispatcher; subsequent
# stories (8.3, 8.4, 8.5, 8.7, 8.8) extend handler.py with concrete
# (typeName, fieldName) implementations.
#
# Placement: inside the VPC (private subnets) to reach Aurora on the private
# endpoint. DynamoDB is accessed via the VPC endpoint (networking module).
#
# The module output lambda_arn is consumed by the AppSync module in story 8.1
# to register the Lambda data source.
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
# Provisions the knotify chat AppSync API with:
#   - Primary auth: AMAZON_COGNITO_USER_POOLS (Cognito JWT for client access)
#   - Secondary auth: AWS_IAM (backend publisher Lambdas: 8.9a, 8.9c)
#   - Five DynamoDB datasources (five chat domain tables)
#   - One Lambda datasource (chat_resolver_ds → chat_resolver Lambda)
#   - log_config at ALL field-level detail to CloudWatch (7-day retention)
#
# Schema is a placeholder ("type Query { _placeholder: String }") until
# story 8.2 ships the full hand-written SDL. The appsync module's main.tf
# carries a "# Schema body lands in story 8.2" comment marking the swap point.
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

  # DynamoDB service role — the chat_resolver IAM role already has
  # dynamodb:GetItem / PutItem / Query / etc. on the five chat tables (story 8.0)
  # and is the natural service role for DDB datasources used by pipeline resolvers.
  dynamodb_role_arn = module.iam_roles.role_arns["chat_resolver"]
}

# ---------------------------------------------------------------------------
# knotify-refresh-deck-view Lambda — story 7.4
#
# Dedicated Lambda for refreshing the deck_view materialized view.
# Runs as aurora_refresh (privileged role with EXECUTE on refresh_deck_view())
# rather than app_user, keeping the RLS-bearing app_user surface minimal.
#
# Triggered by:
#   - EventBridge CloudWatch rule every 15 minutes (defined in the module)
#   - Async boto3 lambda.invoke from the profile Lambda on profile_complete
#     false→true flip (InvocationType="Event")
#
# The module exposes function_arn so the profile Lambda can receive it as
# REFRESH_LAMBDA_ARN and the iam_roles module can scope lambda:InvokeFunction.
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

  # EventBridge schedule: every 15 minutes (default in module variables.tf).
  # Override here for visibility; changing to rate(5 minutes) in a future
  # phase requires only this field.
  schedule_expression = "rate(15 minutes)"
}

# ---------------------------------------------------------------------------
# room_state_publisher Lambda — story 8.9a
#
# Consumes the ChatRooms DynamoDB Stream (NEW_AND_OLD_IMAGES) and publishes
# AppSync mutations when room status transitions occur:
#   active→deactivated : _publishRoomDeactivated(roomId, payload)
#   deactivated→active : _publishRoomReactivated(roomId, payload)
#
# Placement: OUTSIDE the VPC — AppSync HTTPS is reachable via public DNS.
# No Aurora access — DynamoDB-only (no db layer, no app_user credential needed).
# SigV4 signing handled by botocore at runtime using the Lambda execution role.
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
