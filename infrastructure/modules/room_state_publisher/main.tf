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
# room_state_publisher Lambda function
#
# Consumes the ChatRooms DynamoDB Stream and publishes AppSync mutations when
# room status transitions occur (active→deactivated or deactivated→active).
#
# Placement: OUTSIDE the VPC.
# WHY: AppSync HTTPS endpoints are reachable via public DNS — no VPC endpoint
# or NAT gateway is needed.  Inside-VPC placement would repeat hotfix #106's
# blackhole (private subnets without NAT cannot reach public service endpoints).
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
# EventSourceMapping — ChatRooms DynamoDB Stream
#
# batch_size=10: process up to 10 ChatRooms records per Lambda invocation.
#   Room state transitions are infrequent (each block/unblock triggers one);
#   batch_size=10 provides a reasonable buffer without over-waiting.
#
# starting_position=LATEST: process only records written after this ESM is
#   created.  Historic stream records (from before story 8.9a deployed) should
#   not trigger retrospective publish calls — those transitions already happened.
#
# The alias_arn (live alias) is used rather than the unqualified function ARN
#   so any future blue/green weight shift respects the alias target without
#   changing the ESM configuration.
# ---------------------------------------------------------------------------

resource "aws_lambda_event_source_mapping" "chat_rooms_stream" {
  event_source_arn  = var.chat_rooms_stream_arn
  function_name     = module.lambda.alias_arn
  batch_size        = 10
  starting_position = "LATEST"
  enabled           = true
}
