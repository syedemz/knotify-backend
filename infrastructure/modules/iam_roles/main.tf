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
# Table ARN sourced from var.chat_rooms_table_arn (story 8.9 AC-2: must not be
# hardcoded; default wildcard fallback is used only in isolated unit tests where
# the dynamodb module is not wired).
data "aws_iam_policy_document" "blocks_writer_dynamodb" {
  statement {
    sid    = "ChatRoomsUpdateItem"
    effect = "Allow"
    actions = [
      "dynamodb:UpdateItem",
    ]
    resources = [
      var.chat_rooms_table_arn != "" ? var.chat_rooms_table_arn : "arn:aws:dynamodb:*:*:table/ChatRooms",
    ]
  }
}

resource "aws_iam_role_policy" "blocks_writer_dynamodb" {
  name   = "blocks-writer-dynamodb"
  role   = aws_iam_role.blocks_writer.name
  policy = data.aws_iam_policy_document.blocks_writer_dynamodb.json
}

# ===========================================================================
# Role: friends_writer
#
# For the knotify-friends Lambda (phase 6 story 6.2, extended by story 8.9b).
# Needs Aurora app-user access (same as aurora_writer) PLUS DynamoDB UpdateItem
# on the ChatRooms table to flip friendship_active on accept and unfriend.
# The DynamoDB action is scoped to the ChatRooms table only — no other tables.
# Mirrors the blocks_writer pattern exactly (story 8.9 precedent).
# ===========================================================================

resource "aws_iam_role" "friends_writer" {
  name               = "knotify-${var.environment}-friends-writer"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

resource "aws_iam_role_policy_attachment" "friends_writer_vpc_access" {
  role       = aws_iam_role.friends_writer.name
  policy_arn = local.vpc_access_policy_arn
}

# Allow reading the app_user credential so the friends Lambda can connect to Aurora.
data "aws_iam_policy_document" "friends_writer_app_user_credential" {
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

resource "aws_iam_role_policy" "friends_writer_app_user_credential" {
  name   = "friends-writer-app-user-credential"
  role   = aws_iam_role.friends_writer.name
  policy = data.aws_iam_policy_document.friends_writer_app_user_credential.json
}

# Allow DynamoDB UpdateItem on the ChatRooms table ONLY.
# No other DynamoDB actions and no other tables — least-privilege per codingprinciples.md.
# Table ARN sourced from var.chat_rooms_table_arn (story 8.9b AC-2: must not be
# hardcoded; default wildcard fallback is used only in isolated unit tests where
# the dynamodb module is not wired).
data "aws_iam_policy_document" "friends_writer_dynamodb" {
  statement {
    sid    = "ChatRoomsUpdateItem"
    effect = "Allow"
    actions = [
      "dynamodb:UpdateItem",
    ]
    resources = [
      var.chat_rooms_table_arn != "" ? var.chat_rooms_table_arn : "arn:aws:dynamodb:*:*:table/ChatRooms",
    ]
  }
}

resource "aws_iam_role_policy" "friends_writer_dynamodb" {
  name   = "friends-writer-dynamodb"
  role   = aws_iam_role.friends_writer.name
  policy = data.aws_iam_policy_document.friends_writer_dynamodb.json
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

# ===========================================================================
# Role: chat_resolver
#
# For the knotify-chat-resolver AppSync Lambda resolver (phase 8 story 8.0).
# Connects to Aurora as app_user via the app_user_credential.
# Has scoped DynamoDB permissions on the five chat domain tables:
#   ChatRooms, ChatRoomMembership, ChatMessages, MessageReads, Notifications.
# Table ARNs are sourced from module.dynamodb outputs; default "" is used in
# unit tests (dynamodb module not yet created).
#
# Actions granted match the chat resolver's access pattern:
#   GetItem        — membership checks, room lookups, read-receipt reads
#   PutItem        — new chat room creation, new message creation
#   UpdateItem     — last_message_* on ChatRooms, MessageReads upsert
#   Query          — messages-by-room, membership-by-user pagination
#   BatchGetItem   — bulk room fetch (listMyRooms)
#   TransactWriteItems — atomic multi-table writes (createOrGetRoom, sendMessage)
# ===========================================================================

resource "aws_iam_role" "chat_resolver" {
  name               = "knotify-${var.environment}-chat-resolver"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

resource "aws_iam_role_policy_attachment" "chat_resolver_vpc_access" {
  role       = aws_iam_role.chat_resolver.name
  policy_arn = local.vpc_access_policy_arn
}

# Allow reading the app_user credential so the chat resolver can connect to Aurora.
# Scoped to the knotify-<env>-app-user-credential wildcard (matches the
# Secrets Manager secret name pattern used by the db_migrator on first run).
data "aws_iam_policy_document" "chat_resolver_app_user_credential" {
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

resource "aws_iam_role_policy" "chat_resolver_app_user_credential" {
  name   = "chat-resolver-app-user-credential"
  role   = aws_iam_role.chat_resolver.name
  policy = data.aws_iam_policy_document.chat_resolver_app_user_credential.json
}

# Scoped DynamoDB permissions on the five chat domain tables.
# Table ARNs sourced from module.dynamodb outputs (var.*_table_arn) so the
# policy is portable across environments and avoids hardcoded ARN strings.
# Default "" fallback is a wildcard pattern used only in IAM unit tests.
data "aws_iam_policy_document" "chat_resolver_dynamodb" {
  statement {
    sid    = "ChatTableReadWrite"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:Query",
      "dynamodb:BatchGetItem",
      "dynamodb:TransactWriteItems",
    ]
    resources = [
      var.chat_rooms_table_arn != "" ? var.chat_rooms_table_arn : "arn:aws:dynamodb:*:*:table/ChatRooms",
      var.chat_room_membership_table_arn != "" ? var.chat_room_membership_table_arn : "arn:aws:dynamodb:*:*:table/ChatRoomMembership",
      var.chat_messages_table_arn != "" ? var.chat_messages_table_arn : "arn:aws:dynamodb:*:*:table/ChatMessages",
      var.message_reads_table_arn != "" ? var.message_reads_table_arn : "arn:aws:dynamodb:*:*:table/MessageReads",
      var.notifications_table_arn != "" ? var.notifications_table_arn : "arn:aws:dynamodb:*:*:table/Notifications",
    ]
  }
}

resource "aws_iam_role_policy" "chat_resolver_dynamodb" {
  name   = "chat-resolver-dynamodb"
  role   = aws_iam_role.chat_resolver.name
  policy = data.aws_iam_policy_document.chat_resolver_dynamodb.json
}

# ===========================================================================
# Role: appsync_logs
#
# Grants AppSync the permission to push execution logs to CloudWatch Logs.
# Trust principal is appsync.amazonaws.com (not lambda.amazonaws.com).
# Managed policy AWSAppSyncPushToCloudWatchLogs is the AWS-published policy
# for this purpose — no custom inline policy needed.
# This role ARN is passed into aws_appsync_graphql_api.log_config
# .cloudwatch_logs_role_arn in the appsync module (story 8.1).
# ===========================================================================

data "aws_iam_policy_document" "appsync_assume_role" {
  statement {
    sid     = "AppSyncAssumeRole"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["appsync.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "appsync_logs" {
  name               = "knotify-${var.environment}-appsync-logs"
  assume_role_policy = data.aws_iam_policy_document.appsync_assume_role.json
}

resource "aws_iam_role_policy_attachment" "appsync_logs_cloudwatch" {
  role       = aws_iam_role.appsync_logs.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSAppSyncPushToCloudWatchLogs"
}

# ===========================================================================
# Role: appsync_chat_resolver_invoke
#
# Grants AppSync the permission to invoke the chat_resolver Lambda function.
# Trust principal is appsync.amazonaws.com.
# Inline policy grants lambda:InvokeFunction scoped to the exact Lambda ARN
# supplied from module.chat_resolver.lambda_arn in the root modules.
# This role is registered as the service_role_arn on the chat_resolver_ds
# AWS_LAMBDA datasource in the appsync module (story 8.1).
# Default "" ARN value is used in IAM unit tests (chat_resolver module not
# yet wired when running isolated module tests).
# ===========================================================================

resource "aws_iam_role" "appsync_chat_resolver_invoke" {
  name               = "knotify-${var.environment}-appsync-chat-resolver-invoke"
  assume_role_policy = data.aws_iam_policy_document.appsync_assume_role.json
}

data "aws_iam_policy_document" "appsync_chat_resolver_invoke_lambda" {
  statement {
    sid    = "InvokeChatResolverLambda"
    effect = "Allow"
    actions = [
      "lambda:InvokeFunction",
    ]
    resources = [
      var.chat_resolver_lambda_arn != "" ? var.chat_resolver_lambda_arn : "arn:aws:lambda:*:*:function:knotify-chat-resolver-*",
    ]
  }
}

resource "aws_iam_role_policy" "appsync_chat_resolver_invoke_lambda" {
  name   = "appsync-chat-resolver-invoke-lambda"
  role   = aws_iam_role.appsync_chat_resolver_invoke.name
  policy = data.aws_iam_policy_document.appsync_chat_resolver_invoke_lambda.json
}

# ===========================================================================
# Role: room_state_publisher
#
# For the room_state_publisher Lambda (story 8.9a).
# Consumes the ChatRooms DynamoDB Stream and publishes AppSync mutations via
# SigV4 (IAM auth mode).
#
# OUTSIDE the VPC: AppSync HTTPS is reachable via public DNS; no VPC endpoint
# or ENI attachment needed (same rationale as hotfix #106 lesson).
# No VPC access policy attached — this role does NOT run in a VPC.
#
# DynamoDB stream actions scoped to the ChatRooms stream ARN only:
#   dynamodb:DescribeStream + GetRecords + GetShardIterator + ListStreams
#
# AppSync actions scoped to the exact publish-mutation field ARNs:
#   appsync:GraphQL on ${appsync_api_arn}/types/Mutation/fields/_publishRoomDeactivated
#   appsync:GraphQL on ${appsync_api_arn}/types/Mutation/fields/_publishRoomReactivated
#   NOT a wildcard on the entire API — least-privilege per codingprinciples.md.
# ===========================================================================

resource "aws_iam_role" "room_state_publisher" {
  name               = "knotify-${var.environment}-room-state-publisher"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

# Basic Lambda execution — creates log group and can write CloudWatch logs.
# The managed AWSLambdaBasicExecutionRole is used instead of VPC access because
# this Lambda runs OUTSIDE the VPC (no ENI attachment needed).
resource "aws_iam_role_policy_attachment" "room_state_publisher_basic_execution" {
  role       = aws_iam_role.room_state_publisher.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# DynamoDB stream read actions — scoped to the ChatRooms stream ARN.
# These four actions are exactly what a Lambda ESM consumer needs to read
# a DynamoDB stream: DescribeStream + GetRecords + GetShardIterator + ListStreams.
data "aws_iam_policy_document" "room_state_publisher_dynamodb_stream" {
  statement {
    sid    = "ChatRoomsStreamRead"
    effect = "Allow"
    actions = [
      "dynamodb:DescribeStream",
      "dynamodb:GetRecords",
      "dynamodb:GetShardIterator",
      "dynamodb:ListStreams",
    ]
    resources = [
      var.chat_rooms_stream_arn != "" ? var.chat_rooms_stream_arn : "arn:aws:dynamodb:*:*:table/ChatRooms/stream/*",
    ]
  }
}

resource "aws_iam_role_policy" "room_state_publisher_dynamodb_stream" {
  name   = "room-state-publisher-dynamodb-stream"
  role   = aws_iam_role.room_state_publisher.name
  policy = data.aws_iam_policy_document.room_state_publisher_dynamodb_stream.json
}

# AppSync GraphQL publish — scoped to the two backend-only publish mutation
# field ARNs.  NOT a wildcard on ${appsync_api_arn}/* — codingprinciples.md
# forbids wildcard Resource on any committed IAM policy.
# Field ARN format: ${api_arn}/types/Mutation/fields/${field_name}
data "aws_iam_policy_document" "room_state_publisher_appsync" {
  statement {
    sid    = "AppSyncPublishRoomState"
    effect = "Allow"
    actions = [
      "appsync:GraphQL",
    ]
    resources = var.appsync_api_arn != "" ? [
      "${var.appsync_api_arn}/types/Mutation/fields/_publishRoomDeactivated",
      "${var.appsync_api_arn}/types/Mutation/fields/_publishRoomReactivated",
      ] : [
      "arn:aws:appsync:*:*:apis/*/types/Mutation/fields/_publishRoomDeactivated",
      "arn:aws:appsync:*:*:apis/*/types/Mutation/fields/_publishRoomReactivated",
    ]
  }
}

resource "aws_iam_role_policy" "room_state_publisher_appsync" {
  name   = "room-state-publisher-appsync"
  role   = aws_iam_role.room_state_publisher.name
  policy = data.aws_iam_policy_document.room_state_publisher_appsync.json
}

# ===========================================================================
# Role: notifications_publisher
#
# For the notifications_publisher Lambda (story 8.9c).
# Consumes the Notifications DynamoDB Stream and publishes AppSync mutations
# via SigV4 (IAM auth mode).
#
# OUTSIDE the VPC: AppSync HTTPS is reachable via public DNS; no VPC endpoint
# or ENI attachment needed (same rationale as room_state_publisher hotfix #106
# lesson).
# No VPC access policy attached — this role does NOT run in a VPC.
#
# DynamoDB stream actions scoped to the Notifications stream ARN only:
#   dynamodb:DescribeStream + GetRecords + GetShardIterator + ListStreams
#
# AppSync actions scoped to the exact publish-mutation field ARNs:
#   appsync:GraphQL on ${appsync_api_arn}/types/Mutation/fields/publishNotification
#   appsync:GraphQL on ${appsync_api_arn}/types/Mutation/fields/_publishFriendRequestUpdated
#   NOT a wildcard on the entire API — least-privilege per codingprinciples.md.
# ===========================================================================

resource "aws_iam_role" "notifications_publisher" {
  name               = "knotify-${var.environment}-notifications-publisher"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

# Basic Lambda execution — creates log group and can write CloudWatch logs.
# The managed AWSLambdaBasicExecutionRole is used instead of VPC access because
# this Lambda runs OUTSIDE the VPC (no ENI attachment needed).
resource "aws_iam_role_policy_attachment" "notifications_publisher_basic_execution" {
  role       = aws_iam_role.notifications_publisher.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# DynamoDB stream read actions — scoped to the Notifications stream ARN.
# These four actions are exactly what a Lambda ESM consumer needs to read
# a DynamoDB stream: DescribeStream + GetRecords + GetShardIterator + ListStreams.
data "aws_iam_policy_document" "notifications_publisher_dynamodb_stream" {
  statement {
    sid    = "NotificationsStreamRead"
    effect = "Allow"
    actions = [
      "dynamodb:DescribeStream",
      "dynamodb:GetRecords",
      "dynamodb:GetShardIterator",
      "dynamodb:ListStreams",
    ]
    resources = [
      var.notifications_stream_arn != "" ? var.notifications_stream_arn : "arn:aws:dynamodb:*:*:table/Notifications/stream/*",
    ]
  }
}

resource "aws_iam_role_policy" "notifications_publisher_dynamodb_stream" {
  name   = "notifications-publisher-dynamodb-stream"
  role   = aws_iam_role.notifications_publisher.name
  policy = data.aws_iam_policy_document.notifications_publisher_dynamodb_stream.json
}

# AppSync GraphQL publish — scoped to the two backend-only publish mutation
# field ARNs.  NOT a wildcard on ${appsync_api_arn}/* — codingprinciples.md
# forbids wildcard Resource on any committed IAM policy.
# Field ARN format: ${api_arn}/types/Mutation/fields/${field_name}
data "aws_iam_policy_document" "notifications_publisher_appsync" {
  statement {
    sid    = "AppSyncPublishNotifications"
    effect = "Allow"
    actions = [
      "appsync:GraphQL",
    ]
    resources = var.appsync_api_arn != "" ? [
      "${var.appsync_api_arn}/types/Mutation/fields/publishNotification",
      "${var.appsync_api_arn}/types/Mutation/fields/_publishFriendRequestUpdated",
      ] : [
      "arn:aws:appsync:*:*:apis/*/types/Mutation/fields/publishNotification",
      "arn:aws:appsync:*:*:apis/*/types/Mutation/fields/_publishFriendRequestUpdated",
    ]
  }
}

resource "aws_iam_role_policy" "notifications_publisher_appsync" {
  name   = "notifications-publisher-appsync"
  role   = aws_iam_role.notifications_publisher.name
  policy = data.aws_iam_policy_document.notifications_publisher_appsync.json
}

# ===========================================================================
# Role: push_fanout
#
# For the push_fanout Lambda (story 8.10).
# Consumes BOTH ChatMessages and Notifications DynamoDB Streams and sends
# push notifications via the Expo Push API.
#
# OUTSIDE the VPC: only touches DynamoDB and Expo (open internet); no VPC
# endpoint or ENI attachment needed.  Avoids hotfix #106 blackhole trap.
# No VPC access policy attached — this role does NOT run in a VPC.
#
# DynamoDB data-plane actions on four tables:
#   ChatRooms            — GetItem (find recipient from user_a/user_b)
#   ChatRoomMembership   — GetItem (check notifications_muted, cached_other_name)
#   Notifications        — UpdateItem (SET delivered=true on HTTP 200)
#   PushNotificationTokens — Query (get tokens for recipient),
#                            DeleteItem (purge DeviceNotRegistered tokens)
#
# DynamoDB stream read actions on BOTH stream ARNs:
#   ChatMessages stream  — DescribeStream + GetRecords + GetShardIterator + ListStreams
#   Notifications stream — same four actions
#
# Secrets Manager (prod only):
#   GetSecretValue on knotify-prod-expo-push-credential
#   Conditional on var.expo_push_secret_arn != "" so dev keeps a wildcard fallback.
# ===========================================================================

resource "aws_iam_role" "push_fanout" {
  name               = "knotify-${var.environment}-push-fanout"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

# Basic Lambda execution — creates log group and can write CloudWatch logs.
# AWSLambdaBasicExecutionRole instead of VPC access because this Lambda runs
# OUTSIDE the VPC (no ENI attachment needed).
resource "aws_iam_role_policy_attachment" "push_fanout_basic_execution" {
  role       = aws_iam_role.push_fanout.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# DynamoDB stream read actions on ChatMessages and Notifications stream ARNs.
# Both stream ARNs in a single statement — same four actions required for each.
data "aws_iam_policy_document" "push_fanout_dynamodb_streams" {
  statement {
    sid    = "ChatMessagesStreamRead"
    effect = "Allow"
    actions = [
      "dynamodb:DescribeStream",
      "dynamodb:GetRecords",
      "dynamodb:GetShardIterator",
      "dynamodb:ListStreams",
    ]
    resources = [
      var.chat_messages_stream_arn != "" ? var.chat_messages_stream_arn : "arn:aws:dynamodb:*:*:table/ChatMessages/stream/*",
    ]
  }

  statement {
    sid    = "NotificationsStreamRead"
    effect = "Allow"
    actions = [
      "dynamodb:DescribeStream",
      "dynamodb:GetRecords",
      "dynamodb:GetShardIterator",
      "dynamodb:ListStreams",
    ]
    resources = [
      var.notifications_stream_arn != "" ? var.notifications_stream_arn : "arn:aws:dynamodb:*:*:table/Notifications/stream/*",
    ]
  }
}

resource "aws_iam_role_policy" "push_fanout_dynamodb_streams" {
  name   = "push-fanout-dynamodb-streams"
  role   = aws_iam_role.push_fanout.name
  policy = data.aws_iam_policy_document.push_fanout_dynamodb_streams.json
}

# DynamoDB data-plane permissions on the four tables the handler touches.
# Scoped to exact table ARNs — no wildcard Resource per codingprinciples.md.
# Default fallback patterns are used only in isolated IAM unit tests.
data "aws_iam_policy_document" "push_fanout_dynamodb_tables" {
  statement {
    sid    = "ChatRoomsAndMembershipRead"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
    ]
    resources = [
      var.chat_rooms_table_arn != "" ? var.chat_rooms_table_arn : "arn:aws:dynamodb:*:*:table/ChatRooms",
      var.chat_room_membership_table_arn != "" ? var.chat_room_membership_table_arn : "arn:aws:dynamodb:*:*:table/ChatRoomMembership",
    ]
  }

  statement {
    sid    = "NotificationsMarkDelivered"
    effect = "Allow"
    actions = [
      "dynamodb:UpdateItem",
    ]
    resources = [
      var.notifications_table_arn != "" ? var.notifications_table_arn : "arn:aws:dynamodb:*:*:table/Notifications",
    ]
  }

  statement {
    sid    = "PushTokensQueryAndDelete"
    effect = "Allow"
    actions = [
      "dynamodb:Query",
      "dynamodb:DeleteItem",
    ]
    resources = [
      var.push_notification_tokens_table_arn != "" ? var.push_notification_tokens_table_arn : "arn:aws:dynamodb:*:*:table/PushNotificationTokens",
    ]
  }
}

resource "aws_iam_role_policy" "push_fanout_dynamodb_tables" {
  name   = "push-fanout-dynamodb-tables"
  role   = aws_iam_role.push_fanout.name
  policy = data.aws_iam_policy_document.push_fanout_dynamodb_tables.json
}

# Secrets Manager — GetSecretValue on the Expo prod credential.
# Only provisioned when expo_push_secret_arn is non-empty (prod root module
# passes the real ARN; dev omits it so the policy uses a wildcard fallback that
# still validates but won't match any real secret in the dev account).
data "aws_iam_policy_document" "push_fanout_secrets" {
  statement {
    sid    = "ReadExpoPushCredential"
    effect = "Allow"
    actions = [
      "secretsmanager:GetSecretValue",
    ]
    resources = [
      var.expo_push_secret_arn != "" ? var.expo_push_secret_arn : "${local.sm_arn_prefix}:secret:knotify-prod-expo-push-credential-*",
    ]
  }
}

resource "aws_iam_role_policy" "push_fanout_secrets" {
  name   = "push-fanout-secrets"
  role   = aws_iam_role.push_fanout.name
  policy = data.aws_iam_policy_document.push_fanout_secrets.json
}

# ===========================================================================
# Role: push_tokens
#
# For the push_tokens Lambda (story 8.11 — POST /v1/push-tokens).
# Registers or refreshes device push notification tokens via a single
# DynamoDB PutItem (unconditional upsert) on PushNotificationTokens.
#
# Inside the VPC: the Lambda is placed in private subnets (same pattern as
# other REST handlers) so it can reach the DynamoDB VPC endpoint.
# AWSLambdaVPCAccessExecutionRole is attached for ENI attachment capability.
#
# NOT decorated with @require_profile_complete — token registration happens
# at first app launch before onboarding completes.
#
# DynamoDB permission:
#   dynamodb:PutItem on PushNotificationTokens only — no read, no delete,
#   no other tables.  Least-privilege per codingprinciples.md.
# ===========================================================================

resource "aws_iam_role" "push_tokens" {
  name               = "knotify-${var.environment}-push-tokens"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

# VPC access — Lambda runs inside the VPC (private subnets + lambda SG)
# to reach the DynamoDB VPC endpoint. Consistent with other REST handlers.
resource "aws_iam_role_policy_attachment" "push_tokens_vpc_access" {
  role       = aws_iam_role.push_tokens.name
  policy_arn = local.vpc_access_policy_arn
}

# DynamoDB PutItem on PushNotificationTokens — scoped to exact table ARN.
# Default wildcard fallback is used only in isolated IAM unit tests.
data "aws_iam_policy_document" "push_tokens_dynamodb" {
  statement {
    sid    = "PushTokensPutItem"
    effect = "Allow"
    actions = [
      "dynamodb:PutItem",
    ]
    resources = [
      var.push_notification_tokens_table_arn != "" ? var.push_notification_tokens_table_arn : "arn:aws:dynamodb:*:*:table/PushNotificationTokens",
    ]
  }
}

resource "aws_iam_role_policy" "push_tokens_dynamodb" {
  name   = "push-tokens-dynamodb"
  role   = aws_iam_role.push_tokens.name
  policy = data.aws_iam_policy_document.push_tokens_dynamodb.json
}

# ===========================================================================
# Role: stepfn_deletion_exec
#
# Dedicated Step Functions execution role for the account-deletion state machine
# (story 9.1). Trust principal is states.amazonaws.com.
#
# Three inline policies:
#   1. lambda:InvokeFunction — scoped to every deletion task Lambda ARN
#      (all nine task Lambdas from stories 9.2–9.8/9.11–9.12).
#      When deletion_task_lambda_arns is empty (unit-test default), a wildcard
#      fallback pattern is used so validate still passes.
#   2. cloudwatch:PutMetricData — needed to emit the DeletionFailed metric
#      from the global Catch handler. CloudWatch PutMetricData does not support
#      resource-level scoping; the resource is "*" per AWS documentation.
#   3. logs:* — scoped to the Step Functions log group ARN with the :* suffix
#      required by the Step Functions logging integration.
#      When deletion_sfn_log_group_arn is empty (unit-test default), a wildcard
#      fallback pattern is used so validate still passes.
#
# NOT a Lambda execution role — no VPC access managed policy is attached.
# Step Functions invokes Lambdas directly; the Lambda functions themselves
# run in the VPC under their own Lambda execution roles.
# ===========================================================================

resource "aws_iam_role" "stepfn_deletion_exec" {
  name               = "knotify-${var.environment}-stepfn-deletion-exec"
  assume_role_policy = data.aws_iam_policy_document.states_assume_role.json
}

# lambda:InvokeFunction scoped to each deletion task Lambda ARN.
# codingprinciples.md forbids wildcard Resource; the fallback wildcard is used
# only in isolated module tests where no real ARNs are provided.
data "aws_iam_policy_document" "stepfn_deletion_exec_lambda_invoke" {
  statement {
    sid    = "InvokeDeletionTaskLambdas"
    effect = "Allow"
    actions = [
      "lambda:InvokeFunction",
    ]
    resources = length(var.deletion_task_lambda_arns) > 0 ? var.deletion_task_lambda_arns : [
      "arn:aws:lambda:*:*:function:knotify-*-deletion-*",
    ]
  }
}

resource "aws_iam_role_policy" "stepfn_deletion_exec_lambda_invoke" {
  name   = "stepfn-deletion-exec-lambda-invoke"
  role   = aws_iam_role.stepfn_deletion_exec.name
  policy = data.aws_iam_policy_document.stepfn_deletion_exec_lambda_invoke.json
}

# cloudwatch:PutMetricData — no resource-level scoping available for this action.
# The DeletionFailed custom metric is emitted from the global Catch on every
# failed execution. The resource "*" is required by AWS; this is the one
# permitted exception to the no-wildcard-Resource rule per AWS documentation
# (CloudWatch PutMetricData does not support resource-level permissions).
data "aws_iam_policy_document" "stepfn_deletion_exec_cloudwatch" {
  statement {
    sid    = "PutDeletionFailedMetric"
    effect = "Allow"
    actions = [
      "cloudwatch:PutMetricData",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "stepfn_deletion_exec_cloudwatch" {
  name   = "stepfn-deletion-exec-cloudwatch"
  role   = aws_iam_role.stepfn_deletion_exec.name
  policy = data.aws_iam_policy_document.stepfn_deletion_exec_cloudwatch.json
}

# logs:* scoped to the Step Functions log group ARN.
# Step Functions requires logs:CreateLogDelivery, logs:GetLogDelivery,
# logs:UpdateLogDelivery, logs:DeleteLogDelivery, logs:ListLogDeliveries,
# logs:PutResourcePolicy, logs:DescribeResourcePolicies, and
# logs:DescribeLogGroups on the log group — using logs:* captures all of
# these without separately listing each. Scoped to the log group ARN.
data "aws_iam_policy_document" "stepfn_deletion_exec_logs" {
  statement {
    sid    = "StepFunctionsLogging"
    effect = "Allow"
    actions = [
      "logs:*",
    ]
    resources = [
      var.deletion_sfn_log_group_arn != "" ? var.deletion_sfn_log_group_arn : "arn:aws:logs:*:*:log-group:/aws/states/knotify-*-account-deletion:*",
    ]
  }
}

resource "aws_iam_role_policy" "stepfn_deletion_exec_logs" {
  name   = "stepfn-deletion-exec-logs"
  role   = aws_iam_role.stepfn_deletion_exec.name
  policy = data.aws_iam_policy_document.stepfn_deletion_exec_logs.json
}

# ===========================================================================
# Role: write_audit_log
#
# For the knotify-write-audit-log Lambda (story 9.8).
# Writes audit records to the account_deletion_audit DynamoDB table on every
# account-deletion workflow event (initiated / completed / failed).
#
# OUTSIDE the VPC: DynamoDB is reachable via public service endpoints or
# a VPC endpoint; however, this Lambda has no Aurora access and no AppSync
# calls so running it outside the VPC avoids the ENI attachment cold-start
# penalty and prevents the blackhole failure documented in hotfix #106.
# AWSLambdaBasicExecutionRole is sufficient — no VPC access policy attached.
#
# DynamoDB permission:
#   dynamodb:PutItem on account_deletion_audit only — no read, no delete,
#   no other tables.  Least-privilege per codingprinciples.md.
# ===========================================================================

resource "aws_iam_role" "write_audit_log" {
  name               = "knotify-${var.environment}-write-audit-log"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

# Basic Lambda execution — CloudWatch Logs only.
# No VPC access policy: this Lambda runs OUTSIDE the VPC.
resource "aws_iam_role_policy_attachment" "write_audit_log_basic_execution" {
  role       = aws_iam_role.write_audit_log.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# DynamoDB PutItem on account_deletion_audit — scoped to exact table ARN.
# Default wildcard fallback is used only in isolated IAM unit tests where
# the dynamodb module is not wired.
data "aws_iam_policy_document" "write_audit_log_dynamodb" {
  statement {
    sid    = "AuditLogPutItem"
    effect = "Allow"
    actions = [
      "dynamodb:PutItem",
    ]
    resources = [
      var.account_deletion_audit_table_arn != "" ? var.account_deletion_audit_table_arn : "arn:aws:dynamodb:*:*:table/account_deletion_audit",
    ]
  }
}

resource "aws_iam_role_policy" "write_audit_log_dynamodb" {
  name   = "write-audit-log-dynamodb"
  role   = aws_iam_role.write_audit_log.name
  policy = data.aws_iam_policy_document.write_audit_log_dynamodb.json
}

# ===========================================================================
# Role: cognito_user_state
#
# For the knotify-cognito-user-state Lambda (story 9.3).
# Dispatches to Cognito IDP AdminDisableUser or AdminDeleteUser depending on
# the mode input.  Called twice by the account-deletion Step Functions state
# machine: DisableCognitoUser (mode=disable) and DeleteCognitoUser (mode=delete).
#
# OUTSIDE the VPC: only calls Cognito IDP (public HTTPS endpoint) — no Aurora,
# no DynamoDB.  AWSLambdaBasicExecutionRole is sufficient — no ENI attachment.
# No VPC access policy attached — consistent with room_state_publisher and
# write_audit_log which also run outside the VPC.
#
# Least-privilege Cognito IDP permissions:
#   cognito-idp:AdminDisableUser — needed for mode=disable
#   cognito-idp:AdminDeleteUser  — needed for mode=delete
#   cognito-idp:AdminGetUser     — needed to verify the already-disabled state
# All three actions are scoped to the exact Cognito user pool ARN.
# When cognito_user_pool_arn is empty (unit-test default), a wildcard fallback
# is used so `terraform validate` passes in isolated module tests.
# ===========================================================================

resource "aws_iam_role" "cognito_user_state" {
  name               = "knotify-${var.environment}-cognito-user-state"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

# Basic Lambda execution — CloudWatch Logs only.
# No VPC access policy: this Lambda runs OUTSIDE the VPC.
resource "aws_iam_role_policy_attachment" "cognito_user_state_basic_execution" {
  role       = aws_iam_role.cognito_user_state.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Cognito IDP actions scoped to the project's user pool ARN.
# AdminGetUser is included alongside the write actions because the handler uses
# it in the idempotency check path to detect already-disabled users cleanly.
data "aws_iam_policy_document" "cognito_user_state_cognito" {
  statement {
    sid    = "CognitoUserStateActions"
    effect = "Allow"
    actions = [
      "cognito-idp:AdminDisableUser",
      "cognito-idp:AdminDeleteUser",
      "cognito-idp:AdminGetUser",
    ]
    resources = [
      var.cognito_user_pool_arn != "" ? var.cognito_user_pool_arn : "arn:aws:cognito-idp:*:*:userpool/*",
    ]
  }
}

resource "aws_iam_role_policy" "cognito_user_state_cognito" {
  name   = "cognito-user-state-cognito"
  role   = aws_iam_role.cognito_user_state.name
  policy = data.aws_iam_policy_document.cognito_user_state_cognito.json
}

# ===========================================================================
# Role: deactivate_chat_rooms
#
# For the knotify-deactivate-chat-rooms Lambda (story 9.4).
# Deactivates all ChatRooms the deleted user was a member of and removes the
# deleted user's ChatRoomMembership rows.  Called from the account-deletion
# Step Functions state machine.
#
# OUTSIDE the VPC: only touches DynamoDB (ChatRooms and ChatRoomMembership).
# No Aurora, no AppSync.  AWSLambdaBasicExecutionRole is sufficient —
# no ENI attachment needed.  Consistent with write_audit_log and push_fanout.
#
# Least-privilege DynamoDB permissions (scoped to exact table ARNs):
#   dynamodb:Query          — ChatRoomMembership (collect all room_ids for user_id)
#   dynamodb:UpdateItem     — ChatRooms (conditional deactivation per room)
#   dynamodb:BatchWriteItem — ChatRoomMembership (delete user's membership rows)
# ===========================================================================

resource "aws_iam_role" "deactivate_chat_rooms" {
  name               = "knotify-${var.environment}-deactivate-chat-rooms"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

# Basic Lambda execution — CloudWatch Logs only.
# No VPC access policy: this Lambda runs OUTSIDE the VPC.
resource "aws_iam_role_policy_attachment" "deactivate_chat_rooms_basic_execution" {
  role       = aws_iam_role.deactivate_chat_rooms.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# DynamoDB permissions — scoped to the exact table ARNs sourced from module.dynamodb.
# Default wildcard fallbacks are used only in isolated IAM unit tests where
# the dynamodb module is not wired.
data "aws_iam_policy_document" "deactivate_chat_rooms_dynamodb" {
  statement {
    sid    = "ChatRoomMembershipQuery"
    effect = "Allow"
    actions = [
      "dynamodb:Query",
    ]
    resources = [
      var.chat_room_membership_table_arn != "" ? var.chat_room_membership_table_arn : "arn:aws:dynamodb:*:*:table/ChatRoomMembership",
    ]
  }

  statement {
    sid    = "ChatRoomsConditionalUpdate"
    effect = "Allow"
    actions = [
      "dynamodb:UpdateItem",
    ]
    resources = [
      var.chat_rooms_table_arn != "" ? var.chat_rooms_table_arn : "arn:aws:dynamodb:*:*:table/ChatRooms",
    ]
  }

  statement {
    sid    = "ChatRoomMembershipBatchDelete"
    effect = "Allow"
    actions = [
      "dynamodb:BatchWriteItem",
    ]
    resources = [
      var.chat_room_membership_table_arn != "" ? var.chat_room_membership_table_arn : "arn:aws:dynamodb:*:*:table/ChatRoomMembership",
    ]
  }
}

resource "aws_iam_role_policy" "deactivate_chat_rooms_dynamodb" {
  name   = "deactivate-chat-rooms-dynamodb"
  role   = aws_iam_role.deactivate_chat_rooms.name
  policy = data.aws_iam_policy_document.deactivate_chat_rooms_dynamodb.json
}

# ===========================================================================
# Role: anonymize_chat_messages
#
# For the knotify-anonymize-chat-messages Lambda (story 9.6).
# Rewrites sender_id to '[deleted-user]' on every ChatMessages row sent by
# the deleted user, across all rooms they were a member of.
# Called from the soft-delete branch of the account-deletion Step Functions
# state machine inside ParallelCleanup.
#
# OUTSIDE the VPC: only touches DynamoDB (ChatMessages table) — no Aurora,
# no AppSync.  AWSLambdaBasicExecutionRole is sufficient — no ENI attachment.
# Consistent with write_audit_log, deactivate_chat_rooms, and push_fanout
# which also run outside the VPC (hotfix #106 lesson).
#
# Least-privilege DynamoDB permissions (scoped to ChatMessages table only):
#   dynamodb:Query      — fetch all messages in a room sent by the deleted user
#                         (room_id PK + sender_id FilterExpression, no GSI)
#   dynamodb:UpdateItem — rewrite sender_id to '[deleted-user]' per row
# ===========================================================================

resource "aws_iam_role" "anonymize_chat_messages" {
  name               = "knotify-${var.environment}-anonymize-chat-messages"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

# Basic Lambda execution — CloudWatch Logs only.
# No VPC access policy: this Lambda runs OUTSIDE the VPC.
resource "aws_iam_role_policy_attachment" "anonymize_chat_messages_basic_execution" {
  role       = aws_iam_role.anonymize_chat_messages.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# DynamoDB permissions scoped to the ChatMessages table only.
# Query + UpdateItem is the minimum required to anonymize messages:
#   Query   — find all messages sent by the deleted user in a given room
#   UpdateItem — rewrite sender_id on each matched row
# No other tables, no other actions — least-privilege per codingprinciples.md.
# Default wildcard fallback is used only in isolated IAM unit tests where
# the dynamodb module is not wired.
data "aws_iam_policy_document" "anonymize_chat_messages_dynamodb" {
  statement {
    sid    = "ChatMessagesQueryAndUpdate"
    effect = "Allow"
    actions = [
      "dynamodb:Query",
      "dynamodb:UpdateItem",
    ]
    resources = [
      var.chat_messages_table_arn != "" ? var.chat_messages_table_arn : "arn:aws:dynamodb:*:*:table/ChatMessages",
    ]
  }
}

resource "aws_iam_role_policy" "anonymize_chat_messages_dynamodb" {
  name   = "anonymize-chat-messages-dynamodb"
  role   = aws_iam_role.anonymize_chat_messages.name
  policy = data.aws_iam_policy_document.anonymize_chat_messages_dynamodb.json
}

# ===========================================================================
# Role: stale_token_cleanup
#
# For the stale_token_cleanup Lambda (story 8.12 — daily EventBridge cron).
# Scans PushNotificationTokens and deletes rows whose last_seen is older
# than 60 days.
#
# OUTSIDE the VPC: only touches DynamoDB (no Aurora, no external HTTP).
# AWSLambdaBasicExecutionRole is sufficient — no ENI attachment needed.
# Consistent with push_fanout and room_state_publisher placement strategy
# (hotfix #106 lesson: private subnets without NAT cannot reach DynamoDB
# service endpoints when placed outside a VPC endpoint; running outside the
# VPC is simpler for DynamoDB-only Lambdas).
#
# DynamoDB permissions (PushNotificationTokens only — least privilege):
#   dynamodb:Scan   — paginated full-table scan to find stale items.
#   dynamodb:DeleteItem — remove each stale row by (user_id, device_id).
# ===========================================================================

resource "aws_iam_role" "stale_token_cleanup" {
  name               = "knotify-${var.environment}-stale-token-cleanup"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

# Basic Lambda execution — CloudWatch Logs only.
# No VPC access policy: this Lambda runs OUTSIDE the VPC.
resource "aws_iam_role_policy_attachment" "stale_token_cleanup_basic_execution" {
  role       = aws_iam_role.stale_token_cleanup.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# DynamoDB Scan + DeleteItem on PushNotificationTokens — scoped to exact ARN.
# Default wildcard fallback is used only in isolated IAM unit tests.
data "aws_iam_policy_document" "stale_token_cleanup_dynamodb" {
  statement {
    sid    = "PushTokensScanAndDelete"
    effect = "Allow"
    actions = [
      "dynamodb:Scan",
      "dynamodb:DeleteItem",
    ]
    resources = [
      var.push_notification_tokens_table_arn != "" ? var.push_notification_tokens_table_arn : "arn:aws:dynamodb:*:*:table/PushNotificationTokens",
    ]
  }
}

resource "aws_iam_role_policy" "stale_token_cleanup_dynamodb" {
  name   = "stale-token-cleanup-dynamodb"
  role   = aws_iam_role.stale_token_cleanup.name
  policy = data.aws_iam_policy_document.stale_token_cleanup_dynamodb.json
}
