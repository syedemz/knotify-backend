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
# Data sources — portable region and account ID for constructing ARN patterns
# ---------------------------------------------------------------------------

data "aws_region" "current" {}

# ---------------------------------------------------------------------------
# CloudWatch Logs group — AppSync execution logs
#
# 7-day retention keeps costs proportional to a dev/prod chat workload while
# providing enough history for incident forensics.
# The log group is created explicitly so the retention_in_days is enforced
# and the group survives a terraform destroy of the API (AppSync would
# auto-create an unmanaged group otherwise).
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "appsync" {
  name              = "/aws/appsync/apis/knotify-${var.environment}"
  retention_in_days = 7
}

# ---------------------------------------------------------------------------
# AppSync GraphQL API
#
# Primary auth:   AMAZON_COGNITO_USER_POOLS
#   - All client mutations, queries, and subscription requests carry a
#     Cognito JWT. default_action = "DENY" means unauthenticated requests
#     are rejected by AppSync before reaching any resolver.
#
# Secondary auth: AWS_IAM
#   - Backend publisher Lambdas (room_state_publisher story 8.9a and
#     notifications_publisher story 8.9c) authenticate via SigV4.
#   - Publish mutations declared in story 8.2 are annotated @aws_iam so
#     JWT-authenticated clients cannot invoke them directly.
#
# Schema:
#   Placeholder schema that satisfies terraform validate until story 8.2
#   ships the full hand-written SDL at infrastructure/modules/appsync/schema.graphql.
#   # Schema body lands in story 8.2
# ---------------------------------------------------------------------------

resource "aws_appsync_graphql_api" "knotify" {
  name                = "knotify-${var.environment}-chat-api"
  authentication_type = "AMAZON_COGNITO_USER_POOLS"

  user_pool_config {
    user_pool_id   = var.user_pool_id
    aws_region     = data.aws_region.current.region
    default_action = "DENY"
  }

  # AWS_IAM secondary — consumed by backend publisher Lambdas (8.9a, 8.9c)
  additional_authentication_provider {
    authentication_type = "AWS_IAM"
  }

  log_config {
    cloudwatch_logs_role_arn = var.appsync_logs_role_arn
    # "ALL" enables field-level resolver logging (the Terraform attribute name
    # is field_log_level; "ALL" is the value that captures per-field execution
    # details, which the AC calls "FIELD level").
    field_log_level         = "ALL"
    exclude_verbose_content = false
  }

  # Hand-authored SDL for the full chat domain — story 8.2.
  # Defines Message, ChatRoom, ChatRoomMembership, MessageRead, Notification,
  # TypingEvent types plus scoped Query / Mutation / Subscription per §5.4.
  schema = file("${path.module}/schema.graphql")
}

# ---------------------------------------------------------------------------
# DynamoDB datasources — five chat domain tables
#
# Each datasource is backed by an IAM service role (dynamodb_role_arn) that
# grants the AppSync service principal dynamodb:GetItem / Query / PutItem /
# UpdateItem / BatchGetItem / TransactWriteItems on the respective table.
# The chat_resolver Lambda is the primary execution engine; these DynamoDB
# datasources exist for potential direct VTL resolvers added in later phases
# (e.g., the None datasource for setTyping in story 8.8 needs a sibling DDB
# datasource to support pipeline resolvers on the membership-check function).
# ---------------------------------------------------------------------------

resource "aws_appsync_datasource" "chat_rooms" {
  api_id           = aws_appsync_graphql_api.knotify.id
  name             = "ChatRoomsDS"
  type             = "AMAZON_DYNAMODB"
  service_role_arn = var.dynamodb_role_arn

  dynamodb_config {
    table_name = var.chat_rooms_table_name
    region     = data.aws_region.current.region
  }
}

resource "aws_appsync_datasource" "chat_room_membership" {
  api_id           = aws_appsync_graphql_api.knotify.id
  name             = "ChatRoomMembershipDS"
  type             = "AMAZON_DYNAMODB"
  service_role_arn = var.dynamodb_role_arn

  dynamodb_config {
    table_name = var.chat_room_membership_table_name
    region     = data.aws_region.current.region
  }
}

resource "aws_appsync_datasource" "chat_messages" {
  api_id           = aws_appsync_graphql_api.knotify.id
  name             = "ChatMessagesDS"
  type             = "AMAZON_DYNAMODB"
  service_role_arn = var.dynamodb_role_arn

  dynamodb_config {
    table_name = var.chat_messages_table_name
    region     = data.aws_region.current.region
  }
}

resource "aws_appsync_datasource" "message_reads" {
  api_id           = aws_appsync_graphql_api.knotify.id
  name             = "MessageReadsDS"
  type             = "AMAZON_DYNAMODB"
  service_role_arn = var.dynamodb_role_arn

  dynamodb_config {
    table_name = var.message_reads_table_name
    region     = data.aws_region.current.region
  }
}

resource "aws_appsync_datasource" "notifications" {
  api_id           = aws_appsync_graphql_api.knotify.id
  name             = "NotificationsDS"
  type             = "AMAZON_DYNAMODB"
  service_role_arn = var.dynamodb_role_arn

  dynamodb_config {
    table_name = var.notifications_table_name
    region     = data.aws_region.current.region
  }
}

# ---------------------------------------------------------------------------
# Lambda datasource — chat_resolver
#
# The chat_resolver Lambda (story 8.0) handles all Query and Mutation fields
# that require Aurora or DynamoDB business logic. Resolver attachments in
# stories 8.3–8.8 reference this datasource by its name (chat_resolver_ds).
#
# service_role_arn is the appsync_chat_resolver_invoke role provisioned in
# modules/iam_roles (story 8.1); it grants lambda:InvokeFunction on the
# chat_resolver Lambda ARN.
# ---------------------------------------------------------------------------

resource "aws_appsync_datasource" "chat_resolver_ds" {
  api_id           = aws_appsync_graphql_api.knotify.id
  name             = "chat_resolver_ds"
  type             = "AWS_LAMBDA"
  service_role_arn = var.appsync_invoke_role_arn

  lambda_config {
    function_arn = var.chat_resolver_lambda_arn
  }
}

# ---------------------------------------------------------------------------
# IAM policy note: AppSync → chat_resolver invoke
#
# The appsync_chat_resolver_invoke IAM role (trust: appsync.amazonaws.com) and
# its lambda:InvokeFunction inline policy are both provisioned in
# modules/iam_roles (aws_iam_role.appsync_chat_resolver_invoke and
# aws_iam_role_policy.appsync_chat_resolver_invoke_lambda — story 8.1).
#
# The appsync module receives the role ARN as var.appsync_invoke_role_arn and
# registers it on the chat_resolver_ds datasource (service_role_arn above).
# No duplicate policy is created here — the iam_roles module is the single
# owner of all IAM resources per the project's single-responsibility pattern.
# ---------------------------------------------------------------------------
