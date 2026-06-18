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
# notifications_publisher Lambda function
#
# Consumes the Notifications DynamoDB Stream (NEW_IMAGE) and publishes AppSync
# mutations when new notification rows are inserted:
#   - Generic types (friend_request_received, bookmark, match, ...)
#       → publishNotification(notification: <payload>) via SigV4 (IAM auth mode)
#   - friend_request_accepted
#       → _publishFriendRequestUpdated(payload: <payload>) via SigV4 (IAM auth mode)
#
# Placement: OUTSIDE the VPC.
# WHY: AppSync HTTPS endpoints are reachable via public DNS — no VPC endpoint
# or NAT gateway is needed.  Inside-VPC placement would repeat hotfix #106's
# blackhole (private subnets without NAT cannot reach public service endpoints).
#
# CONSUMER LIMIT NOTE: with this ESM the Notifications stream has 2 ESM consumers
# (notifications_publisher + push_fanout from 8.10), which is the AWS default
# limit of 2 simultaneous consumers per DynamoDB stream.  A third consumer
# would require Kinesis Data Streams for DynamoDB or a fan-out Lambda.
#
# No layers: this Lambda only needs boto3/botocore (Lambda runtime) and
# requests (bundled into the zip).  It does NOT connect to Aurora and does NOT
# need knotify_db or knotify_obs.
#
# Architecture: ARM64 (Graviton2) — consistent with all other Lambda functions.
# Timeout: 30s — each invocation may POST multiple AppSync mutations in sequence.
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
    # AppSync GraphQL HTTPS endpoint URL — signed POSTs are sent here.
    # Sourced from module.appsync.graphql_url in the root module.
    APPSYNC_GRAPHQL_URL = var.appsync_graphql_url
  }
}

# ---------------------------------------------------------------------------
# EventSourceMapping — Notifications DynamoDB Stream
#
# batch_size=10: process up to 10 Notifications records per Lambda invocation.
#   New notification inserts are triggered by friend requests, bookmarks, and
#   match events — infrequent enough that batch_size=10 is a comfortable buffer.
#
# starting_position=LATEST: process only records written after this ESM is
#   created.  Historic stream records (from before story 8.9c deployed) should
#   not trigger retrospective publish calls — those notifications were already
#   handled.
#
# The alias_arn (live alias) is used rather than the unqualified function ARN
#   so any future blue/green weight shift respects the alias target without
#   changing the ESM configuration.
# ---------------------------------------------------------------------------

resource "aws_lambda_event_source_mapping" "notifications_stream" {
  event_source_arn  = var.notifications_stream_arn
  function_name     = module.lambda.alias_arn
  batch_size        = 10
  starting_position = "LATEST"
  enabled           = true
}
