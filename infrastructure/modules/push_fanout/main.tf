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
# push_fanout Lambda function
#
# Receives DynamoDB stream events from BOTH ChatMessages and Notifications
# (two EventSourceMappings below) and fans out push notifications to Expo.
#
# Placement: OUTSIDE the VPC.
# WHY: only touches DynamoDB (no VPC required) and Expo (open internet);
# inside-VPC placement would repeat hotfix #106's blackhole (private subnets
# without NAT cannot reach public service endpoints).
#
# No layers: this Lambda only needs boto3 (Lambda runtime) and requests
# (bundled into the zip). It does NOT connect to Aurora and does NOT need
# knotify_db or knotify_obs.
#
# Architecture: ARM64 (Graviton2) — consistent with all other Lambda functions.
# Timeout: 30s — each invocation may POST to Expo for multiple tokens in sequence.
# ---------------------------------------------------------------------------

module "lambda" {
  source = "../lambda"

  function_name = var.function_name
  handler       = "handler.handler"
  filename      = var.filename
  role_arn      = var.role_arn
  architectures = ["arm64"]
  timeout       = 30

  # No vpc_config — Lambda runs OUTSIDE the VPC (see placement note above).

  environment_variables = {
    # Expo Push API HTTPS endpoint. Overridable in tests via EXPO_PUSH_URL env var.
    EXPO_PUSH_URL = var.expo_push_url

    # Authentication mode: "none" (dev) or "bearer" (prod).
    # In bearer mode the Lambda reads the access token from Secrets Manager
    # (knotify-prod-expo-push-credential) on cold start.
    EXPO_AUTH_MODE = var.expo_auth_mode

    # Stream ARNs are injected so the handler can route records to the correct
    # processing path (ChatMessages vs Notifications) by comparing eventSourceARN.
    CHAT_MESSAGES_STREAM_ARN = var.chat_messages_stream_arn
    NOTIFICATIONS_STREAM_ARN = var.notifications_stream_arn
  }
}

# ---------------------------------------------------------------------------
# EventSourceMapping 1 — ChatMessages DynamoDB Stream
#
# batch_size=10: process up to 10 ChatMessages records per Lambda invocation.
#   Chat messages are frequent during active conversations; batch_size=10
#   balances throughput with per-message push latency.
#
# starting_position=LATEST: process only records written after this ESM is
#   created. Historic ChatMessages stream records must not trigger retrospective
#   push notifications to users' devices.
#
# ChatMessages stream currently has 0 consumers; this is the first (of the
# AWS default limit of 2 simultaneous ESM consumers per DynamoDB stream).
# ---------------------------------------------------------------------------

resource "aws_lambda_event_source_mapping" "chat_messages_stream" {
  event_source_arn  = var.chat_messages_stream_arn
  function_name     = module.lambda.alias_arn
  batch_size        = 10
  starting_position = "LATEST"
  enabled           = true
}

# ---------------------------------------------------------------------------
# EventSourceMapping 2 — Notifications DynamoDB Stream
#
# batch_size=10: process up to 10 Notifications records per Lambda invocation.
#
# starting_position=LATEST: same rationale as above.
#
# CONSUMER LIMIT: the Notifications stream already has 1 ESM consumer
# (notifications_publisher from story 8.9c). This is the SECOND (and final)
# consumer at the AWS default limit of 2 simultaneous ESM consumers per
# DynamoDB stream. Adding a third consumer would require Kinesis Data Streams
# for DynamoDB — see story 8.9c notes for the full flag.
# ---------------------------------------------------------------------------

resource "aws_lambda_event_source_mapping" "notifications_stream" {
  event_source_arn  = var.notifications_stream_arn
  function_name     = module.lambda.alias_arn
  batch_size        = 10
  starting_position = "LATEST"
  enabled           = true
}
