terraform {
  required_version = ">= 1.9.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.20"
    }
  }
}

# ---------------------------------------------------------------------------
# Data sources — used to construct portable ARN patterns without hardcoding
# partition, region, or account ID.
# ---------------------------------------------------------------------------

data "aws_partition" "current" {}
data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

# ---------------------------------------------------------------------------
# Locals
# ---------------------------------------------------------------------------

locals {
  # Managed policy ARN for Lambda ENI attachment. Every role here attaches this
  # so the Lambda can run inside the project VPC.
  vpc_access_policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"

  # Shared ARN prefix used in all inline policy resource ARN patterns.
  sm_arn_prefix = "arn:${data.aws_partition.current.partition}:secretsmanager:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}"
}

# ---------------------------------------------------------------------------
# Trust policy documents
# ---------------------------------------------------------------------------

# All Lambda roles share the same Lambda service trust policy.
data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    sid     = "LambdaAssumeRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

# Step Functions tasks use the states service principal.
data "aws_iam_policy_document" "states_assume_role" {
  statement {
    sid     = "StatesAssumeRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
  }
}

# ===========================================================================
# Role: db_migrator
#
# Runs yoyo migrations against the Aurora cluster and manages the app_user
# credential in Secrets Manager. Connects as the Aurora master user via the
# Aurora-managed Secrets Manager secret (no IAM DB auth needed).
# ===========================================================================

resource "aws_iam_role" "db_migrator" {
  name               = "knotify-${var.environment}-db-migrator"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

resource "aws_iam_role_policy_attachment" "db_migrator_vpc_access" {
  role       = aws_iam_role.db_migrator.name
  policy_arn = local.vpc_access_policy_arn
}

# Allow reading the Aurora-managed master secret.
# Scoped to the exact ARN of the master_user_secret output from the aurora
# module. The constructed-name approach (rds!cluster-<cluster_resource_id>-*)
# does NOT work because RDS embeds a different internal UUID in the
# master-secret name than the cluster_resource_id Terraform exposes; the
# resulting policy never matches the real secret and GetSecretValue is denied.
data "aws_iam_policy_document" "db_migrator_secrets" {
  statement {
    sid    = "ReadAuroraMasterSecret"
    effect = "Allow"
    actions = [
      "secretsmanager:GetSecretValue",
    ]
    resources = [
      var.aurora_master_user_secret_arn,
    ]
  }
}

resource "aws_iam_role_policy" "db_migrator_secrets" {
  name   = "db-migrator-secrets"
  role   = aws_iam_role.db_migrator.name
  policy = data.aws_iam_policy_document.db_migrator_secrets.json
}

# Allow creating and updating the app_user credential post-migration.
# CreateSecret on first run; PutSecretValue + DescribeSecret on subsequent
# runs (brainstorm B2 — split 0007 fix).
data "aws_iam_policy_document" "db_migrator_app_user_credential" {
  statement {
    sid    = "ManageAppUserCredential"
    effect = "Allow"
    actions = [
      "secretsmanager:CreateSecret",
      "secretsmanager:PutSecretValue",
      "secretsmanager:DescribeSecret",
    ]
    resources = [
      "${local.sm_arn_prefix}:secret:knotify-${var.environment}-app-user-credential-*",
    ]
  }
}

resource "aws_iam_role_policy" "db_migrator_app_user_credential" {
  name   = "db-migrator-app-user-credential"
  role   = aws_iam_role.db_migrator.name
  policy = data.aws_iam_policy_document.db_migrator_app_user_credential.json
}

# ===========================================================================
# Role: cognito_trigger
#
# Handles PostConfirmation Cognito events. Reads the app_user credential to
# open a DB connection and insert the new user row.
# Cognito-side invoke permission (aws_lambda_permission) is intentionally NOT
# created here — it ships with phase 4 story 4.3 (brainstorm N1).
# ===========================================================================

resource "aws_iam_role" "cognito_trigger" {
  name               = "knotify-${var.environment}-cognito-trigger"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

resource "aws_iam_role_policy_attachment" "cognito_trigger_vpc_access" {
  role       = aws_iam_role.cognito_trigger.name
  policy_arn = local.vpc_access_policy_arn
}

# Allow reading the app_user credential so the handler can connect to Aurora
# as app_user after confirmation.
data "aws_iam_policy_document" "cognito_trigger_app_user_credential" {
  statement {
    sid    = "ReadAppUserCredential"
    effect = "Allow"
    actions = [
      "secretsmanager:GetSecretValue",
    ]
    resources = [
      "${local.sm_arn_prefix}:secret:knotify-${var.environment}-app-user-credential-*",
    ]
  }
}

resource "aws_iam_role_policy" "cognito_trigger_app_user_credential" {
  name   = "cognito-trigger-app-user-credential"
  role   = aws_iam_role.cognito_trigger.name
  policy = data.aws_iam_policy_document.cognito_trigger_app_user_credential.json
}

# ===========================================================================
# Role: aurora_reader
#
# For Lambdas that read from Aurora (phases 6–9).
# Trust policy + VPC access only; per-action DB policies ship with the
# consuming phase (AC bullet 5).
# ===========================================================================

resource "aws_iam_role" "aurora_reader" {
  name               = "knotify-${var.environment}-aurora-reader"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

resource "aws_iam_role_policy_attachment" "aurora_reader_vpc_access" {
  role       = aws_iam_role.aurora_reader.name
  policy_arn = local.vpc_access_policy_arn
}

# ===========================================================================
# Role: aurora_writer
#
# For Lambdas that write to Aurora (phases 6–9): profile, friends, bookmarks.
# Trust policy + VPC access + app_user credential read.
# The app_user credential is required so domain Lambdas can open a DB
# connection as app_user (same pattern as cognito_trigger).
# ===========================================================================

resource "aws_iam_role" "aurora_writer" {
  name               = "knotify-${var.environment}-aurora-writer"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

resource "aws_iam_role_policy_attachment" "aurora_writer_vpc_access" {
  role       = aws_iam_role.aurora_writer.name
  policy_arn = local.vpc_access_policy_arn
}

# Allow reading the app_user credential so domain Lambdas can connect to Aurora.
data "aws_iam_policy_document" "aurora_writer_app_user_credential" {
  statement {
    sid    = "ReadAppUserCredential"
    effect = "Allow"
    actions = [
      "secretsmanager:GetSecretValue",
    ]
    resources = [
      "${local.sm_arn_prefix}:secret:knotify-${var.environment}-app-user-credential-*",
    ]
  }
}

resource "aws_iam_role_policy" "aurora_writer_app_user_credential" {
  name   = "aurora-writer-app-user-credential"
  role   = aws_iam_role.aurora_writer.name
  policy = data.aws_iam_policy_document.aurora_writer_app_user_credential.json
}

# Allow the profile Lambda (aurora_writer role) to set the custom:profile_complete
# Cognito attribute after a successful profile-completion flip (story 7.0b).
# Scoped to the specific user pool ARN — not "*" — per least-privilege principle.
# The default "" value is used in the IAM unit tests (which mock the cognito module);
# the real ARN is plumbed from module.cognito.user_pool_arn in each env root module.
data "aws_iam_policy_document" "aurora_writer_cognito_profile_complete" {
  statement {
    sid    = "CognitoSetProfileCompleteAttribute"
    effect = "Allow"
    actions = [
      "cognito-idp:AdminUpdateUserAttributes",
    ]
    resources = [
      var.cognito_user_pool_arn != "" ? var.cognito_user_pool_arn : "arn:aws:cognito-idp:*:*:userpool/*",
    ]
  }
}

resource "aws_iam_role_policy" "aurora_writer_cognito_profile_complete" {
  name   = "aurora-writer-cognito-profile-complete"
  role   = aws_iam_role.aurora_writer.name
  policy = data.aws_iam_policy_document.aurora_writer_cognito_profile_complete.json
}

# ===========================================================================
# Role: dynamodb_chat_writer
#
# For the chat-message Lambda that writes to the DynamoDB chat table (phase 6).
# Trust policy + VPC access only; per-action policies ship with the consuming phase.
# ===========================================================================

resource "aws_iam_role" "dynamodb_chat_writer" {
  name               = "knotify-${var.environment}-dynamodb-chat-writer"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

resource "aws_iam_role_policy_attachment" "dynamodb_chat_writer_vpc_access" {
  role       = aws_iam_role.dynamodb_chat_writer.name
  policy_arn = local.vpc_access_policy_arn
}

# ===========================================================================
# Role: dynamodb_notifications_writer
#
# For the notifications Lambda that writes to the DynamoDB notifications table
# (phase 6). Trust policy + VPC access only; per-action policies ship later.
# ===========================================================================

resource "aws_iam_role" "dynamodb_notifications_writer" {
  name               = "knotify-${var.environment}-dynamodb-notifications-writer"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

resource "aws_iam_role_policy_attachment" "dynamodb_notifications_writer_vpc_access" {
  role       = aws_iam_role.dynamodb_notifications_writer.name
  policy_arn = local.vpc_access_policy_arn
}

# ===========================================================================
# Role: blocks_writer
#
# For the knotify-blocks Lambda (phase 6 story 6.4).
# Needs Aurora app-user access (same as aurora_writer) PLUS DynamoDB UpdateItem
# on the ChatRooms table to deactivate/reactivate chat rooms on block/unblock.
# The DynamoDB action is scoped to the ChatRooms table only — no other tables.
# ===========================================================================

resource "aws_iam_role" "blocks_writer" {
  name               = "knotify-${var.environment}-blocks-writer"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

resource "aws_iam_role_policy_attachment" "blocks_writer_vpc_access" {
  role       = aws_iam_role.blocks_writer.name
  policy_arn = local.vpc_access_policy_arn
}

# Allow reading the app_user credential so the blocks Lambda can connect to Aurora.
data "aws_iam_policy_document" "blocks_writer_app_user_credential" {
  statement {
    sid    = "ReadAppUserCredential"
    effect = "Allow"
    actions = [
      "secretsmanager:GetSecretValue",
    ]
    resources = [
      "${local.sm_arn_prefix}:secret:knotify-${var.environment}-app-user-credential-*",
    ]
  }
}

resource "aws_iam_role_policy" "blocks_writer_app_user_credential" {
  name   = "blocks-writer-app-user-credential"
  role   = aws_iam_role.blocks_writer.name
  policy = data.aws_iam_policy_document.blocks_writer_app_user_credential.json
}

# Allow DynamoDB UpdateItem on the ChatRooms table ONLY.
# No other DynamoDB actions and no other tables — least-privilege per codingprinciples.md.
data "aws_iam_policy_document" "blocks_writer_dynamodb" {
  statement {
    sid    = "ChatRoomsUpdateItem"
    effect = "Allow"
    actions = [
      "dynamodb:UpdateItem",
    ]
    resources = [
      "arn:aws:dynamodb:*:*:table/ChatRooms",
    ]
  }
}

resource "aws_iam_role_policy" "blocks_writer_dynamodb" {
  name   = "blocks-writer-dynamodb"
  role   = aws_iam_role.blocks_writer.name
  policy = data.aws_iam_policy_document.blocks_writer_dynamodb.json
}

# ===========================================================================
# Role: stepfn_task
#
# For Step Functions state machine tasks (phase 8–9 orchestration flows).
# Trust principal is states.amazonaws.com, not lambda.amazonaws.com.
# AWSLambdaVPCAccessExecutionRole is still attached so any Lambda invoked by
# this state machine can attach ENIs if needed.
# Trust policy + VPC access only; per-action policies ship with the consuming phase.
# ===========================================================================

resource "aws_iam_role" "stepfn_task" {
  name               = "knotify-${var.environment}-stepfn-task"
  assume_role_policy = data.aws_iam_policy_document.states_assume_role.json
}

resource "aws_iam_role_policy_attachment" "stepfn_task_vpc_access" {
  role       = aws_iam_role.stepfn_task.name
  policy_arn = local.vpc_access_policy_arn
}

# ===========================================================================
# Role: aurora_reader_match
#
# For the knotify-match Lambda (phase 7).
# Reads from Aurora via the app_user credential (same app_user connection
# pattern as aurora_writer — app_user has SELECT on users and deck_view).
# No DynamoDB, no write access.
# ===========================================================================

resource "aws_iam_role" "aurora_reader_match" {
  name               = "knotify-${var.environment}-aurora-reader-match"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

resource "aws_iam_role_policy_attachment" "aurora_reader_match_vpc_access" {
  role       = aws_iam_role.aurora_reader_match.name
  policy_arn = local.vpc_access_policy_arn
}

# Allow reading the app_user credential so the match Lambda can connect to Aurora.
# Scoped to the knotify-<env>-app-user-credential wildcard (matches the
# Secrets Manager secret name pattern used by the db_migrator on first run).
data "aws_iam_policy_document" "aurora_reader_match_app_user_credential" {
  statement {
    sid    = "ReadAppUserCredential"
    effect = "Allow"
    actions = [
      "secretsmanager:GetSecretValue",
    ]
    resources = [
      "${local.sm_arn_prefix}:secret:knotify-${var.environment}-app-user-credential-*",
    ]
  }
}

resource "aws_iam_role_policy" "aurora_reader_match_app_user_credential" {
  name   = "aurora-reader-match-app-user-credential"
  role   = aws_iam_role.aurora_reader_match.name
  policy = data.aws_iam_policy_document.aurora_reader_match_app_user_credential.json
}

# ===========================================================================
# Role: aurora_writer — lambda:InvokeFunction on refresh_deck_view (story 7.4)
#
# The profile Lambda (aurora_writer role) async-invokes the refresh Lambda
# after a profile_complete_verified false→true flip commits.  Scoped to the
# refresh Lambda's function ARN — not "*" — per least-privilege principle.
# Default "" ARN value is used in unit tests (refresh module not yet created).
# ===========================================================================

data "aws_iam_policy_document" "aurora_writer_invoke_refresh" {
  statement {
    sid    = "InvokeRefreshDeckViewLambda"
    effect = "Allow"
    actions = [
      "lambda:InvokeFunction",
    ]
    resources = [
      var.refresh_lambda_arn != "" ? var.refresh_lambda_arn : "arn:aws:lambda:*:*:function:knotify-refresh-deck-view-*",
    ]
  }
}

resource "aws_iam_role_policy" "aurora_writer_invoke_refresh" {
  name   = "aurora-writer-invoke-refresh"
  role   = aws_iam_role.aurora_writer.name
  policy = data.aws_iam_policy_document.aurora_writer_invoke_refresh.json
}

# ===========================================================================
# Role: aurora_refresh_lambda
#
# For the knotify-refresh-deck-view Lambda (story 7.4).
# Connects to Aurora as the aurora_refresh role using the dedicated
# knotify-<env>-aurora-refresh-credential Secrets Manager secret.
# Does NOT use the app_user credential — isolated surface per design.
#
# KMS: the project uses the default AWS-managed Secrets Manager KMS key
# (aws/secretsmanager), so no explicit kms:Decrypt statement is required —
# the default key policy grants Decrypt to the secret's resource-based policy
# and the IAM principal automatically. Mirroring aurora_reader_match which
# also has no explicit kms:Decrypt statement (story 7.0 precedent).
# ===========================================================================

resource "aws_iam_role" "aurora_refresh_lambda" {
  name               = "knotify-${var.environment}-aurora-refresh-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

resource "aws_iam_role_policy_attachment" "aurora_refresh_lambda_vpc_access" {
  role       = aws_iam_role.aurora_refresh_lambda.name
  policy_arn = local.vpc_access_policy_arn
}

# Allow reading the aurora_refresh credential so the refresh Lambda can connect
# to Aurora as aurora_refresh (NOT as app_user).
data "aws_iam_policy_document" "aurora_refresh_lambda_credential" {
  statement {
    sid    = "ReadAuroraRefreshCredential"
    effect = "Allow"
    actions = [
      "secretsmanager:GetSecretValue",
    ]
    resources = [
      "${local.sm_arn_prefix}:secret:knotify-${var.environment}-aurora-refresh-credential-*",
    ]
  }
}

resource "aws_iam_role_policy" "aurora_refresh_lambda_credential" {
  name   = "aurora-refresh-lambda-credential"
  role   = aws_iam_role.aurora_refresh_lambda.name
  policy = data.aws_iam_policy_document.aurora_refresh_lambda_credential.json
}

# Also allow the db_migrator to write (create/update) the aurora-refresh
# credential secret. The db_migrator IAM role is extended in the same module
# rather than opening a separate policy attachment.
data "aws_iam_policy_document" "db_migrator_aurora_refresh_credential" {
  statement {
    sid    = "ManageAuroraRefreshCredential"
    effect = "Allow"
    actions = [
      "secretsmanager:CreateSecret",
      "secretsmanager:PutSecretValue",
      "secretsmanager:DescribeSecret",
    ]
    resources = [
      "${local.sm_arn_prefix}:secret:knotify-${var.environment}-aurora-refresh-credential-*",
    ]
  }
}

resource "aws_iam_role_policy" "db_migrator_aurora_refresh_credential" {
  name   = "db-migrator-aurora-refresh-credential"
  role   = aws_iam_role.db_migrator.name
  policy = data.aws_iam_policy_document.db_migrator_aurora_refresh_credential.json
}
