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
# hard_purge Lambda function — story 9.11
#
# Scheduled daily (EventBridge rate(1 day)) hard-delete of users whose
# soft-delete is older than 30 days:
#
#   DELETE FROM users
#   WHERE deleted_at IS NOT NULL
#     AND deleted_at < NOW() - INTERVAL '30 days'
#
# Aurora ON DELETE CASCADE removes rows in siblings, friendships,
# friend_requests, bookmarks, and blocks automatically.
#
# Also supports per-user invocation (user_id input) for the purge_immediately
# branch in the account-deletion Step Functions state machine.  In that mode
# the 30-day window is dropped but the soft-delete guard is retained:
#
#   DELETE FROM users
#   WHERE user_id = :id
#     AND deleted_at IS NOT NULL
#
# Placement: INSIDE the VPC.
# WHY: Aurora is VPC-private — the Lambda must reach the cluster writer
# endpoint which is on a private subnet.  Mirrors the soft_delete_aurora
# module pattern (story 9.5).
#
# Architecture: ARM64 (Graviton2) — consistent with all other Lambda functions.
# Timeout: 300s — a daily batch DELETE may touch many rows; 5 minutes is
# generous headroom while remaining within Lambda's max.
# Memory: 128 MB — single DELETE with no result sets; minimal footprint.
# Layers: knotify_db (psycopg2 + knotify_db helpers) +
#         knotify_obs (structured logging).
# ---------------------------------------------------------------------------

module "lambda" {
  source = "../lambda"

  function_name = var.function_name
  handler       = "handler.handler"
  filename      = var.filename
  role_arn      = var.role_arn
  layers        = var.layers
  architectures = ["arm64"]
  timeout       = 300
  memory_size   = 128

  # VPC config — REQUIRED: Aurora is VPC-private.
  vpc_config = var.vpc_config

  environment_variables = {
    # Secrets Manager secret name for the Aurora app_user credential.
    DB_SECRET_NAME = var.db_secret_name

    # Aurora connection endpoint params — sourced from aurora module outputs.
    AURORA_HOST   = var.aurora_host
    AURORA_PORT   = var.aurora_port
    AURORA_DBNAME = var.aurora_dbname

    # ARN of the refresh_deck_view Lambda (hotfix #6) — async-invoked by the
    # handler after a successful DELETE to evict the purged row from the
    # deck_view materialised view immediately rather than waiting for the
    # 15-minute scheduled refresh. Empty default keeps the env var absent
    # when the caller does not supply it (the handler skips the invoke).
    REFRESH_LAMBDA_ARN = var.refresh_lambda_arn
  }
}

# ---------------------------------------------------------------------------
# EventBridge (CloudWatch Events) scheduled rule — rate(1 day)
#
# Triggers the Lambda once every 24 hours.  "rate(1 day)" is the simplest
# scheduling expression; exact wall-clock time is not a business requirement.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_event_rule" "daily" {
  name                = "${var.function_name}-daily"
  description         = "Daily trigger for knotify hard-purge of expired soft-deleted users"
  schedule_expression = "rate(1 day)"
  state               = "ENABLED"
}

# ---------------------------------------------------------------------------
# EventBridge target — points the rule at the Lambda live alias
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_event_target" "lambda" {
  rule = aws_cloudwatch_event_rule.daily.name
  arn  = module.lambda.alias_arn
}

# ---------------------------------------------------------------------------
# Lambda permission — allow EventBridge to invoke this Lambda
#
# principal = "events.amazonaws.com" (EventBridge service principal)
# source_arn = the scheduled rule ARN so only this rule can trigger the Lambda.
# ---------------------------------------------------------------------------

resource "aws_lambda_permission" "allow_eventbridge" {
  statement_id  = "AllowHardPurgeEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.lambda.function_name
  qualifier     = "live"
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.daily.arn
}
