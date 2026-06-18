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
# stale_token_cleanup Lambda function
#
# Daily cron: scan PushNotificationTokens, delete rows whose last_seen is
# older than 60 days.
#
# Placement: OUTSIDE the VPC.
# WHY: only touches DynamoDB — no Aurora, no external HTTP.  Running outside
# the VPC avoids the ENI cold-start penalty and eliminates the hotfix #106
# trap (private subnets without NAT cannot reach DynamoDB if no VPC endpoint
# is configured).  DynamoDB is publicly accessible via its HTTPS endpoint.
#
# Architecture: ARM64 (Graviton2) — consistent with all other Lambda functions.
# Timeout: 300s (5 minutes) — allows scanning a large PushNotificationTokens
# table with multiple Scan pages and issuing DeleteItem calls for all stale rows.
# Memory: 256 MB — adequate for a boto3-only scan-and-delete workload.
# No layers: this Lambda only needs boto3 (bundled in the Lambda runtime).
#   It does NOT connect to Aurora and does NOT need knotify_db or knotify_obs.
# ---------------------------------------------------------------------------

module "lambda" {
  source = "../lambda"

  function_name = var.function_name
  handler       = "handler.handler"
  filename      = var.filename
  role_arn      = var.role_arn
  architectures = ["arm64"]
  timeout       = 300
  memory_size   = 256

  # No vpc_config — Lambda runs OUTSIDE the VPC (see placement note above).

  environment_variables = {
    # DynamoDB table name for PushNotificationTokens.
    # Default "PushNotificationTokens" matches the table name in modules/dynamodb/main.tf.
    TABLE_PUSH_TOKENS = var.table_push_tokens_name
  }
}

# ---------------------------------------------------------------------------
# EventBridge (CloudWatch Events) scheduled rule — rate(1 day)
#
# Triggers the Lambda once every 24 hours.  "rate(1 day)" is the simplest
# scheduling expression; a cron expression is not necessary here because exact
# wall-clock time of the daily run is not a business requirement.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_event_rule" "daily" {
  name                = "${var.function_name}-daily"
  description         = "Daily trigger for knotify stale push-token cleanup"
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
# function_name + qualifier = the live alias ARN (consistent with all other
# Lambda permissions in this project).
# ---------------------------------------------------------------------------

resource "aws_lambda_permission" "allow_eventbridge" {
  statement_id  = "AllowStaleTokenCleanupEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.lambda.function_name
  qualifier     = "live"
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.daily.arn
}
