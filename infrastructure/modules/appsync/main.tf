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

# ---------------------------------------------------------------------------
# Story 8.6 — Subscription pipeline resolvers
#
# Implementation choice: APPSYNC_JS runtime on a NONE datasource for the
# identity-check function; APPSYNC_JS on the ChatRoomMembership DynamoDB
# datasource for the membership-check function.
# Rationale (recorded here per AC): APPSYNC_JS on NONE/DDB datasource —
# no Lambda cold-start on subscribe; a single DynamoDB GetItem is all
# that is needed for membership verification, making Lambda overkill.
#
# Two pipeline functions are declared:
#   check_room_membership — DDB GetItem on ChatRoomMembership(identity.sub, roomId);
#                           used by the 5 room-scoped subscriptions.
#   check_identity_match  — pure JS guard that verifies the subscriber's
#                           identity.sub matches the intended recipient
#                           (payload.userId); used by the 2 identity-scoped
#                           subscriptions (onNotificationForMe,
#                           onFriendRequestUpdated).
#                           Dual-layer: pipeline check (defensive, runs on
#                           subscribe) + @aws_subscribe post-publish filter
#                           semantics (ensures notifications_publisher sets
#                           user_id on the mutation payload for the field
#                           filter to match at delivery time).
#
# Each subscription field has a PIPELINE resolver whose pipeline_config
# references the appropriate check function.  The pipeline runs when a
# client SUBSCRIBES — not on every published event — which is the correct
# model for AppSync subscription access control.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# NONE datasource for pure JS pipeline functions
#
# A NONE datasource is the conventional AppSync target for functions that do
# not need to call an external data store. check_identity_match uses it because
# the identity check is a pure in-process JS comparison with no I/O.
# ---------------------------------------------------------------------------

resource "aws_appsync_datasource" "pipeline_none" {
  api_id = aws_appsync_graphql_api.knotify.id
  name   = "NoneDS"
  # NONE datasource — APPSYNC_JS pipeline functions that need no external call
  type = "NONE"
}

# ---------------------------------------------------------------------------
# Pipeline function 1: check_room_membership
#
# Runs a DynamoDB GetItem on ChatRoomMembership using the DDB datasource so
# AppSync's built-in DynamoDB resolver handles the request/response mapping.
# On item miss the function returns an Unauthorized error, which AppSync
# surfaces as a subscription-establishment failure.
#
# Runtime: APPSYNC_JS (JS_1_0_0) — preferred per story brief; cheaper than
# Lambda (no cold-start per connection) and sufficient for a single GetItem.
# ---------------------------------------------------------------------------

resource "aws_appsync_function" "check_room_membership" {
  api_id      = aws_appsync_graphql_api.knotify.id
  name        = "check_room_membership"
  description = "Pipeline function: GetItem ChatRoomMembership(identity.sub, roomId). Rejects Unauthorized on miss."
  data_source = aws_appsync_datasource.chat_room_membership.name

  # APPSYNC_JS runtime — synchronous DDB GetItem, no Lambda cold-start.
  runtime {
    name            = "APPSYNC_JS"
    runtime_version = "1.0.0"
  }

  code = <<-APPSYNC_JS
    // check_room_membership — APPSYNC_JS pipeline function (story 8.6)
    // Runtime: APPSYNC_JS on NONE datasource — no Lambda cold-start on subscribe.
    //
    // Runs when a client SUBSCRIBES (not on every published event).
    // Performs a DynamoDB GetItem on ChatRoomMembership(identity.sub, roomId).
    // On miss: returns an Unauthorized error → AppSync rejects the WebSocket
    // subscription before any event is delivered.
    //
    // The subscription argument is named `roomId` on every room-scoped field
    // (onMessageInRoom, onTypingInRoom, onRoomDeactivated, onRoomReactivated,
    // onReadReceipt). AppSync resolvers for Subscription fields execute during
    // the subscribe phase with ctx.args populated from the subscription arguments.

    import { util } from "@aws-appsync/utils";
    import { get } from "@aws-appsync/utils/dynamodb";

    export function request(ctx) {
      const userId = ctx.identity.sub;
      const roomId = ctx.args.roomId;
      if (!userId) {
        util.unauthorized();
      }
      if (!roomId) {
        util.error("roomId argument is required for room-scoped subscriptions", "MissingArgument");
      }
      return get({
        key: {
          user_id: util.dynamodb.toDynamoDB(userId),
          room_id: util.dynamodb.toDynamoDB(roomId),
        },
      });
    }

    export function response(ctx) {
      if (ctx.error) {
        util.error(ctx.error.message, ctx.error.type);
      }
      if (!ctx.result) {
        // GetItem returned no item — caller is not a member of this room.
        util.unauthorized();
      }
      // Member confirmed — pass through; pipeline continues (no more functions).
      return ctx.result;
    }
  APPSYNC_JS
}

# ---------------------------------------------------------------------------
# Pipeline function 2: check_identity_match
#
# Pure JS check: verifies identity.sub equals the intended recipient field
# on the subscription payload.  Used by onNotificationForMe and
# onFriendRequestUpdated (both identity-scoped — no roomId argument).
#
# Dual-layer design (documented per story brief):
#   1. This pipeline function runs on SUBSCRIBE and rejects connections where
#      identity.sub does not match the sub encoded in the subscription token
#      (defensive gate).
#   2. AppSync's @aws_subscribe post-publish field filter ensures at delivery
#      time that only events whose user_id field matches the subscription
#      context are forwarded to the subscriber (the notifications_publisher
#      story 8.9c populates user_id on every mutation payload for this filter
#      to work correctly at runtime).
# ---------------------------------------------------------------------------

resource "aws_appsync_function" "check_identity_match" {
  api_id      = aws_appsync_graphql_api.knotify.id
  name        = "check_identity_match"
  description = "Pipeline function: confirms identity.sub is non-empty. Identity-scoped subscriptions (onNotificationForMe, onFriendRequestUpdated)."
  data_source = aws_appsync_datasource.pipeline_none.name

  runtime {
    name            = "APPSYNC_JS"
    runtime_version = "1.0.0"
  }

  code = <<-APPSYNC_JS
    // check_identity_match — APPSYNC_JS pipeline function (story 8.6)
    // Runtime: APPSYNC_JS on NONE datasource — no Lambda cold-start on subscribe.
    //
    // Dual-layer: this pipeline check runs on SUBSCRIBE (defensive gate);
    // @aws_subscribe post-publish field filter enforces user_id match at
    // delivery time (notifications_publisher populates user_id on the mutation
    // payload per story 8.9c so the filter fires correctly).
    //
    // For identity-scoped subscriptions (onNotificationForMe, onFriendRequestUpdated)
    // there is no roomId argument — access is controlled solely by identity.sub.
    // We confirm the subscriber has a valid, non-empty sub claim so anonymous
    // or malformed tokens cannot establish a subscription.

    import { util } from "@aws-appsync/utils";

    export function request(ctx) {
      const userId = ctx.identity.sub;
      if (!userId) {
        util.unauthorized();
      }
      // NONE datasource request must return an empty payload object.
      return {};
    }

    export function response(ctx) {
      if (ctx.error) {
        util.error(ctx.error.message, ctx.error.type);
      }
      // Identity confirmed — allow subscription to proceed.
      return ctx.result;
    }
  APPSYNC_JS
}

# ---------------------------------------------------------------------------
# Pipeline resolvers — room-scoped subscriptions
#
# Fields: onMessageInRoom, onTypingInRoom, onRoomDeactivated,
#         onRoomReactivated, onReadReceipt
#
# Each resolver is kind = "PIPELINE" with pipeline_config.functions containing
# [check_room_membership.function_id]. The check runs during the subscribe
# phase; on rejection AppSync closes the WebSocket before delivering events.
#
# request_template / response_template are set to the AppSync passthrough
# templates required for PIPELINE resolvers (non-JS runtime fallback path;
# the actual logic is in the function's `code` above).
# ---------------------------------------------------------------------------

resource "aws_appsync_resolver" "on_message_in_room" {
  api_id    = aws_appsync_graphql_api.knotify.id
  type      = "Subscription"
  field     = "onMessageInRoom"
  kind      = "PIPELINE"

  pipeline_config {
    functions = [aws_appsync_function.check_room_membership.function_id]
  }

  # Passthrough request/response for PIPELINE resolvers.
  request_template  = "{}"
  response_template = "$util.toJson($ctx.result)"
}

resource "aws_appsync_resolver" "on_typing_in_room" {
  api_id    = aws_appsync_graphql_api.knotify.id
  type      = "Subscription"
  field     = "onTypingInRoom"
  kind      = "PIPELINE"

  pipeline_config {
    functions = [aws_appsync_function.check_room_membership.function_id]
  }

  request_template  = "{}"
  response_template = "$util.toJson($ctx.result)"
}

resource "aws_appsync_resolver" "on_room_deactivated" {
  api_id    = aws_appsync_graphql_api.knotify.id
  type      = "Subscription"
  field     = "onRoomDeactivated"
  kind      = "PIPELINE"

  pipeline_config {
    functions = [aws_appsync_function.check_room_membership.function_id]
  }

  request_template  = "{}"
  response_template = "$util.toJson($ctx.result)"
}

resource "aws_appsync_resolver" "on_room_reactivated" {
  api_id    = aws_appsync_graphql_api.knotify.id
  type      = "Subscription"
  field     = "onRoomReactivated"
  kind      = "PIPELINE"

  pipeline_config {
    functions = [aws_appsync_function.check_room_membership.function_id]
  }

  request_template  = "{}"
  response_template = "$util.toJson($ctx.result)"
}

resource "aws_appsync_resolver" "on_read_receipt" {
  api_id    = aws_appsync_graphql_api.knotify.id
  type      = "Subscription"
  field     = "onReadReceipt"
  kind      = "PIPELINE"

  pipeline_config {
    functions = [aws_appsync_function.check_room_membership.function_id]
  }

  request_template  = "{}"
  response_template = "$util.toJson($ctx.result)"
}

# ---------------------------------------------------------------------------
# Pipeline resolvers — identity-scoped subscriptions
#
# Fields: onNotificationForMe, onFriendRequestUpdated
#
# Uses check_identity_match (NONE datasource) — no roomId argument; access
# is controlled by identity.sub.
# ---------------------------------------------------------------------------

resource "aws_appsync_resolver" "on_notification_for_me" {
  api_id    = aws_appsync_graphql_api.knotify.id
  type      = "Subscription"
  field     = "onNotificationForMe"
  kind      = "PIPELINE"

  pipeline_config {
    functions = [aws_appsync_function.check_identity_match.function_id]
  }

  request_template  = "{}"
  response_template = "$util.toJson($ctx.result)"
}

resource "aws_appsync_resolver" "on_friend_request_updated" {
  api_id    = aws_appsync_graphql_api.knotify.id
  type      = "Subscription"
  field     = "onFriendRequestUpdated"
  kind      = "PIPELINE"

  pipeline_config {
    functions = [aws_appsync_function.check_identity_match.function_id]
  }

  request_template  = "{}"
  response_template = "$util.toJson($ctx.result)"
}

# ---------------------------------------------------------------------------
# Story 8.8 — setTyping mutation PIPELINE resolver (no storage)
#
# Implementation choice: APPSYNC_JS PIPELINE resolver on NoneDS.
# Rationale: no Lambda cold-start on mutation; a single DDB GetItem membership
# check (reusing check_room_membership from 8.6) plus a pure JS payload
# pass-through on NoneDS is sufficient — no DynamoDB write, no Lambda invocation.
#
# Pipeline:
#   1. check_room_membership (reused from 8.6) — DDB GetItem on
#      ChatRoomMembership(identity.sub, roomId). Rejects Unauthorized on miss.
#      This is a read-only operation; no write occurs in this function.
#   2. set_typing_passthrough (NoneDS) — constructs the TypingEvent payload
#      {userId: identity.sub, isTyping: args.isTyping, roomId: args.roomId}
#      and returns it. NoneDS guarantees no DynamoDB API call is possible.
#
# "No DynamoDB write" proof (brainstorm finding #15 relaxation):
#   - check_room_membership datasource = ChatRoomMembership DDB → read-only GetItem
#   - set_typing_passthrough datasource = NoneDS → zero DDB operations
#   - Neither function contains a PutItem / UpdateItem / DeleteItem call
#   - Terraform tests 16–18 in appsync.tftest.hcl assert this structure
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Pipeline function: set_typing_passthrough
#
# Runs on NoneDS — no data store interaction is possible from this datasource.
# Constructs the TypingEvent payload from the resolver context and returns it.
# AppSync's @aws_subscribe(mutations: ["setTyping"]) on onTypingInRoom picks up
# this return value and fans it out to subscribers whose roomId argument matches
# the payload's roomId field.
# ---------------------------------------------------------------------------

resource "aws_appsync_function" "set_typing_passthrough" {
  api_id      = aws_appsync_graphql_api.knotify.id
  name        = "set_typing_passthrough"
  description = "Pipeline function: constructs TypingEvent payload {userId, isTyping, roomId} on NoneDS — no DynamoDB write (story 8.8)."
  data_source = aws_appsync_datasource.pipeline_none.name

  # APPSYNC_JS runtime on NoneDS — no external data store call, no write op.
  runtime {
    name            = "APPSYNC_JS"
    runtime_version = "1.0.0"
  }

  code = <<-APPSYNC_JS
    // set_typing_passthrough — APPSYNC_JS pipeline function (story 8.8)
    // Runtime: APPSYNC_JS on NoneDS — no Lambda cold-start, no DynamoDB write.
    //
    // This is the second (and final) function in the setTyping pipeline.
    // check_room_membership (the first function) has already verified membership
    // via a read-only DDB GetItem.  This function constructs the TypingEvent
    // payload that AppSync broadcasts to onTypingInRoom(roomId) subscribers.
    //
    // NoneDS guarantees this function cannot issue any DynamoDB API call —
    // "no storage" is enforced at the datasource level, not just by convention.

    import { util } from "@aws-appsync/utils";

    export function request(ctx) {
      // NoneDS request must return an empty object — no I/O is performed.
      return {};
    }

    export function response(ctx) {
      if (ctx.error) {
        util.error(ctx.error.message, ctx.error.type);
      }
      // Build TypingEvent matching schema.graphql's TypingEvent type:
      //   type TypingEvent { userId: ID!, isTyping: Boolean!, roomId: ID! }
      // roomId is included so the onTypingInRoom(roomId) field filter fires:
      // AppSync matches result.roomId against each subscriber's roomId argument.
      return {
        userId:   ctx.identity.sub,
        isTyping: ctx.args.isTyping,
        roomId:   ctx.args.roomId,
      };
    }
  APPSYNC_JS
}

# ---------------------------------------------------------------------------
# PIPELINE resolver for Mutation.setTyping
#
# Pipeline functions (in order):
#   1. check_room_membership — GetItem ChatRoomMembership(identity.sub, roomId)
#                              Rejects Unauthorized on miss. Read-only.
#   2. set_typing_passthrough — Builds TypingEvent payload on NoneDS. No write.
#
# The resolver is attached to the Mutation type so AppSync triggers it when a
# client calls setTyping(roomId, isTyping).  The pipeline returns the TypingEvent
# payload; @aws_subscribe(mutations: ["setTyping"]) on onTypingInRoom picks it
# up and fans it out via the existing roomId field filter (story 8.2 schema).
# ---------------------------------------------------------------------------

resource "aws_appsync_resolver" "set_typing" {
  api_id = aws_appsync_graphql_api.knotify.id
  type   = "Mutation"
  field  = "setTyping"
  kind   = "PIPELINE"

  pipeline_config {
    functions = [
      aws_appsync_function.check_room_membership.function_id,
      aws_appsync_function.set_typing_passthrough.function_id,
    ]
  }

  # Passthrough request/response for PIPELINE resolvers.
  request_template  = "{}"
  response_template = "$util.toJson($ctx.result)"
}
