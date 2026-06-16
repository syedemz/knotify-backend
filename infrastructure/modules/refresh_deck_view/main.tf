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
# Lambda function — wraps the shared lambda module
#
# The refresh_deck_view Lambda does NOT integrate with API Gateway. It is
# invoked by:
#   - The EventBridge scheduled rule below (every 15 minutes)
#   - Async boto3 lambda.invoke from the profile Lambda (InvocationType="Event")
#     after a profile_complete_verified false→true flip commits
#
# No HTTP API integration, no authorizer, no CloudFront wiring needed.
# ---------------------------------------------------------------------------

module "lambda" {
  source = "../lambda"

  function_name = var.function_name
  handler       = "handler.handler"
  filename      = var.filename
  role_arn      = var.role_arn
  layers        = var.layers

  vpc_config = var.vpc_config

  environment_variables = {
    # The aurora_refresh credential secret — NOT the app_user credential.
    # The refresh Lambda connects as aurora_refresh, which has EXECUTE on
    # refresh_deck_view() but is NOT the RLS-bearing app_user.
    DB_SECRET_NAME = var.db_secret_name

    # Aurora connection endpoint params — secret carries only username +
    # password; host/port/dbname come from aurora module outputs.
    AURORA_HOST   = var.aurora_host
    AURORA_PORT   = var.aurora_port
    AURORA_DBNAME = var.aurora_dbname
  }
}

# ---------------------------------------------------------------------------
# EventBridge CloudWatch scheduled rule — fires every 15 minutes
#
# This is the project's first EventBridge scheduled rule. Convention:
#   - resource name: knotify-<env>-<purpose>-schedule
#   - schedule_expression: "rate(15 minutes)" (configurable via var)
#   - event pattern: none (schedule-only rule)
#   - state: ENABLED
#
# The rule targets the Lambda's live alias so that any in-flight alias-weight
# shift (future blue/green) is respected without changing the rule target.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_event_rule" "deck_view_refresh" {
  name                = "knotify-${var.environment}-deck-view-refresh"
  description         = "Triggers the refresh_deck_view Lambda every 15 minutes to keep deck_view current."
  schedule_expression = var.schedule_expression
  state               = "ENABLED"
}

resource "aws_cloudwatch_event_target" "deck_view_refresh" {
  rule      = aws_cloudwatch_event_rule.deck_view_refresh.name
  target_id = "knotify-refresh-deck-view"
  arn       = module.lambda.alias_arn
}

# Lambda permission — grants EventBridge (events.amazonaws.com) the right to
# invoke the refresh Lambda. Scoped to the specific rule ARN — not wildcard.
resource "aws_lambda_permission" "eventbridge_deck_view_refresh" {
  statement_id  = "AllowEventBridgeDeckViewRefresh"
  action        = "lambda:InvokeFunction"
  function_name = module.lambda.function_name
  qualifier     = "live"
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.deck_view_refresh.arn
}
