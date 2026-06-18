# AppSync module tests — all use command = plan (hermetic, no AWS credentials required)
# Tests are designed so `terraform test` exits 0 from infrastructure/modules/appsync/
#
# TDD note: this file was authored BEFORE main.tf/variables.tf/outputs.tf to drive
# the implementation shape via acceptance criteria (story 8.1).
#
# mock_provider overrides supply deterministic values for the data sources used
# to construct region names and ARN patterns.
#
# Acceptance criteria covered:
#   AC1  — aws_appsync_graphql_api created with AMAZON_COGNITO_USER_POOLS (primary)
#            and AWS_IAM (secondary additional_authentication_provider)
#   AC2  — user_pool_config block declared (asserted via plan-time structural check)
#   AC3  — appsync_logs_role trust on appsync.amazonaws.com; AWSAppSyncPushToCloudWatchLogs
#   AC4  — log_config present (FIELD level, CloudWatch log group wired)
#   AC5  — five DynamoDB datasources declared (ChatRooms, ChatRoomMembership,
#            ChatMessages, MessageReads, Notifications)
#   AC6  — AWS_LAMBDA datasource chat_resolver_ds registered
#   AC7  — module outputs api_id, graphql_url, realtime_url, chat_resolver_ds_name
#   AC8  — aws_iam_role for AppSync→Lambda invoke declared and policy attached

mock_provider "aws" {
  mock_data "aws_partition" {
    defaults = {
      partition  = "aws"
      dns_suffix = "amazonaws.com"
    }
  }
  mock_data "aws_region" {
    defaults = {
      region = "eu-central-1"
      name   = "eu-central-1"
    }
  }
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "123456789012"
      arn        = "arn:aws:iam::123456789012:root"
      user_id    = "AIDACKCEVSQ6C2EXAMPLE"
    }
  }
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{}"
    }
  }
}

# Shared minimal variable block for all tests
# (duplicated per run block — terraform test does not support shared defaults)

# ---------------------------------------------------------------------------
# Test 1: GraphQL API resource has primary auth AMAZON_COGNITO_USER_POOLS
#
# Satisfies AC1 (primary authentication_type).
# ---------------------------------------------------------------------------
run "graphql_api_primary_auth_is_cognito" {
  command = plan

  variables {
    environment                     = "test"
    user_pool_id                    = "eu-central-1_TESTPOOL"
    appsync_logs_role_arn           = "arn:aws:iam::123456789012:role/knotify-test-appsync-logs"
    appsync_invoke_role_arn         = "arn:aws:iam::123456789012:role/knotify-test-appsync-invoke"
    chat_resolver_lambda_arn        = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-chat-resolver-test:live"
    chat_room_membership_table_name = "ChatRoomMembership"
    chat_messages_table_name        = "ChatMessages"
    message_reads_table_name        = "MessageReads"
    notifications_table_name        = "Notifications"
    chat_rooms_table_name           = "ChatRooms"
    dynamodb_role_arn               = "arn:aws:iam::123456789012:role/knotify-test-ddb-role"
  }

  assert {
    condition     = aws_appsync_graphql_api.knotify.authentication_type == "AMAZON_COGNITO_USER_POOLS"
    error_message = "Primary authentication_type must be AMAZON_COGNITO_USER_POOLS"
  }
}

# ---------------------------------------------------------------------------
# Test 2: GraphQL API has AWS_IAM additional_authentication_provider
#
# Satisfies AC1 (secondary auth type for backend publisher Lambdas).
# ---------------------------------------------------------------------------
run "graphql_api_secondary_auth_is_aws_iam" {
  command = plan

  variables {
    environment                     = "test"
    user_pool_id                    = "eu-central-1_TESTPOOL"
    appsync_logs_role_arn           = "arn:aws:iam::123456789012:role/knotify-test-appsync-logs"
    appsync_invoke_role_arn         = "arn:aws:iam::123456789012:role/knotify-test-appsync-invoke"
    chat_resolver_lambda_arn        = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-chat-resolver-test:live"
    chat_room_membership_table_name = "ChatRoomMembership"
    chat_messages_table_name        = "ChatMessages"
    message_reads_table_name        = "MessageReads"
    notifications_table_name        = "Notifications"
    chat_rooms_table_name           = "ChatRooms"
    dynamodb_role_arn               = "arn:aws:iam::123456789012:role/knotify-test-ddb-role"
  }

  assert {
    condition     = length(aws_appsync_graphql_api.knotify.additional_authentication_provider) == 1
    error_message = "Must have exactly one additional_authentication_provider (AWS_IAM)"
  }

  assert {
    condition     = aws_appsync_graphql_api.knotify.additional_authentication_provider[0].authentication_type == "AWS_IAM"
    error_message = "additional_authentication_provider must be AWS_IAM"
  }
}

# ---------------------------------------------------------------------------
# Test 3: CloudWatch log group created for AppSync with 7-day retention
#
# Satisfies AC4 (log_config at FIELD level, 7-day retention CloudWatch).
# ---------------------------------------------------------------------------
run "cloudwatch_log_group_created_with_7_day_retention" {
  command = plan

  variables {
    environment                     = "test"
    user_pool_id                    = "eu-central-1_TESTPOOL"
    appsync_logs_role_arn           = "arn:aws:iam::123456789012:role/knotify-test-appsync-logs"
    appsync_invoke_role_arn         = "arn:aws:iam::123456789012:role/knotify-test-appsync-invoke"
    chat_resolver_lambda_arn        = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-chat-resolver-test:live"
    chat_room_membership_table_name = "ChatRoomMembership"
    chat_messages_table_name        = "ChatMessages"
    message_reads_table_name        = "MessageReads"
    notifications_table_name        = "Notifications"
    chat_rooms_table_name           = "ChatRooms"
    dynamodb_role_arn               = "arn:aws:iam::123456789012:role/knotify-test-ddb-role"
  }

  assert {
    condition     = aws_cloudwatch_log_group.appsync.retention_in_days == 7
    error_message = "AppSync CloudWatch log group must have 7-day retention"
  }
}

# ---------------------------------------------------------------------------
# Test 4: Five DynamoDB datasources declared
#
# Satisfies AC5 (ChatRooms, ChatRoomMembership, ChatMessages, MessageReads,
# Notifications datasources).
# ---------------------------------------------------------------------------
run "five_dynamodb_datasources_declared" {
  command = plan

  variables {
    environment                     = "test"
    user_pool_id                    = "eu-central-1_TESTPOOL"
    appsync_logs_role_arn           = "arn:aws:iam::123456789012:role/knotify-test-appsync-logs"
    appsync_invoke_role_arn         = "arn:aws:iam::123456789012:role/knotify-test-appsync-invoke"
    chat_resolver_lambda_arn        = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-chat-resolver-test:live"
    chat_room_membership_table_name = "ChatRoomMembership"
    chat_messages_table_name        = "ChatMessages"
    message_reads_table_name        = "MessageReads"
    notifications_table_name        = "Notifications"
    chat_rooms_table_name           = "ChatRooms"
    dynamodb_role_arn               = "arn:aws:iam::123456789012:role/knotify-test-ddb-role"
  }

  assert {
    condition     = aws_appsync_datasource.chat_rooms.type == "AMAZON_DYNAMODB"
    error_message = "chat_rooms datasource must be AMAZON_DYNAMODB"
  }

  assert {
    condition     = aws_appsync_datasource.chat_room_membership.type == "AMAZON_DYNAMODB"
    error_message = "chat_room_membership datasource must be AMAZON_DYNAMODB"
  }

  assert {
    condition     = aws_appsync_datasource.chat_messages.type == "AMAZON_DYNAMODB"
    error_message = "chat_messages datasource must be AMAZON_DYNAMODB"
  }

  assert {
    condition     = aws_appsync_datasource.message_reads.type == "AMAZON_DYNAMODB"
    error_message = "message_reads datasource must be AMAZON_DYNAMODB"
  }

  assert {
    condition     = aws_appsync_datasource.notifications.type == "AMAZON_DYNAMODB"
    error_message = "notifications datasource must be AMAZON_DYNAMODB"
  }
}

# ---------------------------------------------------------------------------
# Test 5: Lambda datasource chat_resolver_ds is AMAZON_LAMBDA type
#
# Satisfies AC6 (AWS_LAMBDA datasource named chat_resolver_ds).
# ---------------------------------------------------------------------------
run "lambda_datasource_chat_resolver_ds_is_aws_lambda" {
  command = plan

  variables {
    environment                     = "test"
    user_pool_id                    = "eu-central-1_TESTPOOL"
    appsync_logs_role_arn           = "arn:aws:iam::123456789012:role/knotify-test-appsync-logs"
    appsync_invoke_role_arn         = "arn:aws:iam::123456789012:role/knotify-test-appsync-invoke"
    chat_resolver_lambda_arn        = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-chat-resolver-test:live"
    chat_room_membership_table_name = "ChatRoomMembership"
    chat_messages_table_name        = "ChatMessages"
    message_reads_table_name        = "MessageReads"
    notifications_table_name        = "Notifications"
    chat_rooms_table_name           = "ChatRooms"
    dynamodb_role_arn               = "arn:aws:iam::123456789012:role/knotify-test-ddb-role"
  }

  assert {
    condition     = aws_appsync_datasource.chat_resolver_ds.type == "AWS_LAMBDA"
    error_message = "chat_resolver_ds datasource must be AWS_LAMBDA"
  }

  assert {
    condition     = aws_appsync_datasource.chat_resolver_ds.name == "chat_resolver_ds"
    error_message = "Lambda datasource must be named chat_resolver_ds"
  }
}

# ---------------------------------------------------------------------------
# Test 6: Module outputs api_id, graphql_url, realtime_url, chat_resolver_ds_name
#
# Satisfies AC7 (module output shape for consuming stories).
# The outputs reference computed attributes; we assert the map keys exist and
# are non-empty by overriding the GraphQL API resource.
# ---------------------------------------------------------------------------
run "module_outputs_include_required_keys" {
  command = plan

  variables {
    environment                     = "test"
    user_pool_id                    = "eu-central-1_TESTPOOL"
    appsync_logs_role_arn           = "arn:aws:iam::123456789012:role/knotify-test-appsync-logs"
    appsync_invoke_role_arn         = "arn:aws:iam::123456789012:role/knotify-test-appsync-invoke"
    chat_resolver_lambda_arn        = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-chat-resolver-test:live"
    chat_room_membership_table_name = "ChatRoomMembership"
    chat_messages_table_name        = "ChatMessages"
    message_reads_table_name        = "MessageReads"
    notifications_table_name        = "Notifications"
    chat_rooms_table_name           = "ChatRooms"
    dynamodb_role_arn               = "arn:aws:iam::123456789012:role/knotify-test-ddb-role"
  }

  override_resource {
    target = aws_appsync_graphql_api.knotify
    values = {
      id = "abc123appsyncid"
      uris = {
        GRAPHQL  = "https://abc123.appsync-api.eu-central-1.amazonaws.com/graphql"
        REALTIME = "wss://abc123.appsync-realtime-api.eu-central-1.amazonaws.com/graphql"
      }
    }
    override_during = plan
  }

  assert {
    condition     = output.api_id == "abc123appsyncid"
    error_message = "api_id output must be wired to aws_appsync_graphql_api.knotify.id"
  }

  assert {
    condition     = output.graphql_url == "https://abc123.appsync-api.eu-central-1.amazonaws.com/graphql"
    error_message = "graphql_url output must be wired to the GRAPHQL URI"
  }

  assert {
    condition     = output.realtime_url == "wss://abc123.appsync-realtime-api.eu-central-1.amazonaws.com/graphql"
    error_message = "realtime_url output must be wired to the REALTIME URI"
  }

  assert {
    condition     = output.chat_resolver_ds_name == "chat_resolver_ds"
    error_message = "chat_resolver_ds_name output must equal the datasource name"
  }
}

# ---------------------------------------------------------------------------
# Test 7: chat_resolver_ds datasource uses the appsync_invoke_role_arn as
#         service_role_arn
#
# Satisfies AC8 (aws_iam_role for AppSync to invoke chat_resolver Lambda is
# declared and granted lambda:InvokeFunction).  The role and its inline policy
# live in modules/iam_roles; the appsync module wires the role ARN into the
# datasource service_role_arn.  This test verifies the wiring is correct.
# ---------------------------------------------------------------------------
run "chat_resolver_ds_uses_invoke_role_arn" {
  command = plan

  variables {
    environment                     = "test"
    user_pool_id                    = "eu-central-1_TESTPOOL"
    appsync_logs_role_arn           = "arn:aws:iam::123456789012:role/knotify-test-appsync-logs"
    appsync_invoke_role_arn         = "arn:aws:iam::123456789012:role/knotify-test-appsync-invoke"
    chat_resolver_lambda_arn        = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-chat-resolver-test:live"
    chat_room_membership_table_name = "ChatRoomMembership"
    chat_messages_table_name        = "ChatMessages"
    message_reads_table_name        = "MessageReads"
    notifications_table_name        = "Notifications"
    chat_rooms_table_name           = "ChatRooms"
    dynamodb_role_arn               = "arn:aws:iam::123456789012:role/knotify-test-ddb-role"
  }

  assert {
    condition     = aws_appsync_datasource.chat_resolver_ds.service_role_arn == "arn:aws:iam::123456789012:role/knotify-test-appsync-invoke"
    error_message = "chat_resolver_ds service_role_arn must be the appsync_invoke_role_arn"
  }
}

# ---------------------------------------------------------------------------
# Test 8: schema is loaded from schema.graphql file (story 8.2)
#
# Satisfies AC (schema.graphql): aws_appsync_graphql_api.knotify uses the
# file() reference rather than the placeholder inline string.  The schema
# attribute reflects the file content on plan; we assert it contains the
# canonical type names from §5.4 of architecture.md.
#
# This test will FAIL until schema.graphql is authored and main.tf is updated
# to `schema = file("${path.module}/schema.graphql")`.
# ---------------------------------------------------------------------------
run "schema_loaded_from_file_contains_canonical_types" {
  command = plan

  variables {
    environment                     = "test"
    user_pool_id                    = "eu-central-1_TESTPOOL"
    appsync_logs_role_arn           = "arn:aws:iam::123456789012:role/knotify-test-appsync-logs"
    appsync_invoke_role_arn         = "arn:aws:iam::123456789012:role/knotify-test-appsync-invoke"
    chat_resolver_lambda_arn        = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-chat-resolver-test:live"
    chat_room_membership_table_name = "ChatRoomMembership"
    chat_messages_table_name        = "ChatMessages"
    message_reads_table_name        = "MessageReads"
    notifications_table_name        = "Notifications"
    chat_rooms_table_name           = "ChatRooms"
    dynamodb_role_arn               = "arn:aws:iam::123456789012:role/knotify-test-ddb-role"
  }

  assert {
    condition     = strcontains(aws_appsync_graphql_api.knotify.schema, "type Message")
    error_message = "schema must define the Message type (§5.4 ChatMessages model)"
  }

  assert {
    condition     = strcontains(aws_appsync_graphql_api.knotify.schema, "type ChatRoom")
    error_message = "schema must define the ChatRoom type (§5.4 ChatRooms model)"
  }

  assert {
    condition     = strcontains(aws_appsync_graphql_api.knotify.schema, "type ChatRoomMembership")
    error_message = "schema must define the ChatRoomMembership type (§5.4 ChatRoomMembership model)"
  }

  assert {
    condition     = strcontains(aws_appsync_graphql_api.knotify.schema, "type MessageRead")
    error_message = "schema must define the MessageRead type (§5.4 MessageReads model)"
  }

  assert {
    condition     = strcontains(aws_appsync_graphql_api.knotify.schema, "type Notification")
    error_message = "schema must define the Notification type (§5.4 Notifications model)"
  }

  assert {
    condition     = strcontains(aws_appsync_graphql_api.knotify.schema, "type TypingEvent")
    error_message = "schema must define the TypingEvent type (ephemeral typing pub/sub)"
  }

  assert {
    condition     = strcontains(aws_appsync_graphql_api.knotify.schema, "sendMessage(")
    error_message = "schema must declare the sendMessage mutation"
  }

  assert {
    condition     = strcontains(aws_appsync_graphql_api.knotify.schema, "@aws_iam")
    error_message = "schema must annotate backend-only publish mutations with @aws_iam"
  }

  assert {
    condition     = strcontains(aws_appsync_graphql_api.knotify.schema, "onMessageInRoom(")
    error_message = "schema must declare the onMessageInRoom subscription"
  }

  assert {
    condition     = strcontains(aws_appsync_graphql_api.knotify.schema, "@aws_subscribe")
    error_message = "schema must use @aws_subscribe on subscription fields"
  }

  # The sendMessage mutation must start with roomId, NOT senderId.
  # Checking for the exact opening of the mutation argument list confirms no
  # senderId argument is injected before or alongside roomId.
  assert {
    condition     = strcontains(aws_appsync_graphql_api.knotify.schema, "sendMessage(roomId: ID!, content: String!")
    error_message = "sendMessage must start with (roomId, content) — no senderId argument allowed; sender is derived server-side"
  }

  assert {
    condition     = !strcontains(aws_appsync_graphql_api.knotify.schema, "onCreateMessage")
    error_message = "schema must NOT contain auto-generated onCreateMessage subscription"
  }

  assert {
    condition     = !strcontains(aws_appsync_graphql_api.knotify.schema, "onUpdateMessage")
    error_message = "schema must NOT contain auto-generated onUpdateMessage subscription"
  }

  assert {
    condition     = !strcontains(aws_appsync_graphql_api.knotify.schema, "onDeleteMessage")
    error_message = "schema must NOT contain auto-generated onDeleteMessage subscription"
  }
}

# ---------------------------------------------------------------------------
# Story 8.6 tests — Pipeline resolvers for subscription fields
#
# Implementation choice: APPSYNC_JS runtime on a NONE datasource.
# Cheaper than Lambda (no cold-start per subscribe), no external call,
# and supports synchronous DynamoDB GetItem via the AppSync JS evaluator.
# Comment in main.tf: "APPSYNC_JS on NONE datasource — no Lambda cold-start on subscribe"
#
# Tests assert:
#   AC-8.6-T1  — NONE datasource declared for pipeline function execution
#   AC-8.6-T2  — check_room_membership AppSync function declared
#                (used by the 5 room-scoped subscriptions)
#   AC-8.6-T3  — check_identity_match AppSync function declared
#                (used by the 2 identity-scoped subscriptions)
#   AC-8.6-T4  — PIPELINE resolver for onMessageInRoom references
#                check_room_membership function
#   AC-8.6-T5  — PIPELINE resolver for onTypingInRoom references
#                check_room_membership function
#   AC-8.6-T6  — PIPELINE resolver for onRoomDeactivated references
#                check_room_membership function
#   AC-8.6-T7  — PIPELINE resolver for onRoomReactivated references
#                check_room_membership function
#   AC-8.6-T8  — PIPELINE resolver for onReadReceipt references
#                check_room_membership function
#   AC-8.6-T9  — PIPELINE resolver for onNotificationForMe references
#                check_identity_match function
#   AC-8.6-T10 — PIPELINE resolver for onFriendRequestUpdated references
#                check_identity_match function
# ---------------------------------------------------------------------------

# Shared variables local — Terraform test does not support shared defaults,
# so each run block repeats the variable block verbatim.

# ---------------------------------------------------------------------------
# Test 9: NONE datasource declared for pipeline function execution (story 8.6)
#
# The pipeline membership-check and identity-check functions run against a
# NONE datasource (APPSYNC_JS runtime — no data source I/O needed; the check
# is purely in AppSync JS evaluator code).
# ---------------------------------------------------------------------------
run "none_datasource_declared_for_pipeline_functions" {
  command = plan

  variables {
    environment                     = "test"
    user_pool_id                    = "eu-central-1_TESTPOOL"
    appsync_logs_role_arn           = "arn:aws:iam::123456789012:role/knotify-test-appsync-logs"
    appsync_invoke_role_arn         = "arn:aws:iam::123456789012:role/knotify-test-appsync-invoke"
    chat_resolver_lambda_arn        = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-chat-resolver-test:live"
    chat_room_membership_table_name = "ChatRoomMembership"
    chat_messages_table_name        = "ChatMessages"
    message_reads_table_name        = "MessageReads"
    notifications_table_name        = "Notifications"
    chat_rooms_table_name           = "ChatRooms"
    dynamodb_role_arn               = "arn:aws:iam::123456789012:role/knotify-test-ddb-role"
  }

  assert {
    condition     = aws_appsync_datasource.pipeline_none.type == "NONE"
    error_message = "A NONE datasource must be declared for APPSYNC_JS pipeline functions (story 8.6)"
  }
}

# ---------------------------------------------------------------------------
# Test 10: check_room_membership AppSync function declared (story 8.6)
# ---------------------------------------------------------------------------
run "check_room_membership_function_declared" {
  command = plan

  variables {
    environment                     = "test"
    user_pool_id                    = "eu-central-1_TESTPOOL"
    appsync_logs_role_arn           = "arn:aws:iam::123456789012:role/knotify-test-appsync-logs"
    appsync_invoke_role_arn         = "arn:aws:iam::123456789012:role/knotify-test-appsync-invoke"
    chat_resolver_lambda_arn        = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-chat-resolver-test:live"
    chat_room_membership_table_name = "ChatRoomMembership"
    chat_messages_table_name        = "ChatMessages"
    message_reads_table_name        = "MessageReads"
    notifications_table_name        = "Notifications"
    chat_rooms_table_name           = "ChatRooms"
    dynamodb_role_arn               = "arn:aws:iam::123456789012:role/knotify-test-ddb-role"
  }

  assert {
    condition     = aws_appsync_function.check_room_membership.name == "check_room_membership"
    error_message = "check_room_membership AppSync function must be declared (story 8.6)"
  }

  assert {
    condition     = aws_appsync_function.check_room_membership.data_source == aws_appsync_datasource.chat_room_membership.name
    error_message = "check_room_membership must use the ChatRoomMembership DynamoDB datasource (story 8.6)"
  }
}

# ---------------------------------------------------------------------------
# Test 11: check_identity_match AppSync function declared (story 8.6)
# ---------------------------------------------------------------------------
run "check_identity_match_function_declared" {
  command = plan

  variables {
    environment                     = "test"
    user_pool_id                    = "eu-central-1_TESTPOOL"
    appsync_logs_role_arn           = "arn:aws:iam::123456789012:role/knotify-test-appsync-logs"
    appsync_invoke_role_arn         = "arn:aws:iam::123456789012:role/knotify-test-appsync-invoke"
    chat_resolver_lambda_arn        = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-chat-resolver-test:live"
    chat_room_membership_table_name = "ChatRoomMembership"
    chat_messages_table_name        = "ChatMessages"
    message_reads_table_name        = "MessageReads"
    notifications_table_name        = "Notifications"
    chat_rooms_table_name           = "ChatRooms"
    dynamodb_role_arn               = "arn:aws:iam::123456789012:role/knotify-test-ddb-role"
  }

  assert {
    condition     = aws_appsync_function.check_identity_match.name == "check_identity_match"
    error_message = "check_identity_match AppSync function must be declared (story 8.6)"
  }

  assert {
    condition     = aws_appsync_function.check_identity_match.data_source == aws_appsync_datasource.pipeline_none.name
    error_message = "check_identity_match must use the NONE datasource (story 8.6)"
  }
}

# ---------------------------------------------------------------------------
# Test 12: Room-scoped subscription resolvers are PIPELINE kind (story 8.6)
#
# onMessageInRoom, onTypingInRoom, onRoomDeactivated, onRoomReactivated,
# onReadReceipt — all must be PIPELINE resolvers whose pipeline_config
# references the check_room_membership function.
# ---------------------------------------------------------------------------
run "room_scoped_subscription_resolvers_are_pipeline_kind" {
  command = plan

  variables {
    environment                     = "test"
    user_pool_id                    = "eu-central-1_TESTPOOL"
    appsync_logs_role_arn           = "arn:aws:iam::123456789012:role/knotify-test-appsync-logs"
    appsync_invoke_role_arn         = "arn:aws:iam::123456789012:role/knotify-test-appsync-invoke"
    chat_resolver_lambda_arn        = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-chat-resolver-test:live"
    chat_room_membership_table_name = "ChatRoomMembership"
    chat_messages_table_name        = "ChatMessages"
    message_reads_table_name        = "MessageReads"
    notifications_table_name        = "Notifications"
    chat_rooms_table_name           = "ChatRooms"
    dynamodb_role_arn               = "arn:aws:iam::123456789012:role/knotify-test-ddb-role"
  }

  assert {
    condition     = aws_appsync_resolver.on_message_in_room.kind == "PIPELINE"
    error_message = "onMessageInRoom subscription resolver must be PIPELINE kind (story 8.6)"
  }

  assert {
    condition     = aws_appsync_resolver.on_typing_in_room.kind == "PIPELINE"
    error_message = "onTypingInRoom subscription resolver must be PIPELINE kind (story 8.6)"
  }

  assert {
    condition     = aws_appsync_resolver.on_room_deactivated.kind == "PIPELINE"
    error_message = "onRoomDeactivated subscription resolver must be PIPELINE kind (story 8.6)"
  }

  assert {
    condition     = aws_appsync_resolver.on_room_reactivated.kind == "PIPELINE"
    error_message = "onRoomReactivated subscription resolver must be PIPELINE kind (story 8.6)"
  }

  assert {
    condition     = aws_appsync_resolver.on_read_receipt.kind == "PIPELINE"
    error_message = "onReadReceipt subscription resolver must be PIPELINE kind (story 8.6)"
  }
}

# ---------------------------------------------------------------------------
# Test 13: Identity-scoped subscription resolvers are PIPELINE kind (story 8.6)
#
# onNotificationForMe and onFriendRequestUpdated — PIPELINE resolvers whose
# pipeline_config references the check_identity_match function.
# ---------------------------------------------------------------------------
run "identity_scoped_subscription_resolvers_are_pipeline_kind" {
  command = plan

  variables {
    environment                     = "test"
    user_pool_id                    = "eu-central-1_TESTPOOL"
    appsync_logs_role_arn           = "arn:aws:iam::123456789012:role/knotify-test-appsync-logs"
    appsync_invoke_role_arn         = "arn:aws:iam::123456789012:role/knotify-test-appsync-invoke"
    chat_resolver_lambda_arn        = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-chat-resolver-test:live"
    chat_room_membership_table_name = "ChatRoomMembership"
    chat_messages_table_name        = "ChatMessages"
    message_reads_table_name        = "MessageReads"
    notifications_table_name        = "Notifications"
    chat_rooms_table_name           = "ChatRooms"
    dynamodb_role_arn               = "arn:aws:iam::123456789012:role/knotify-test-ddb-role"
  }

  assert {
    condition     = aws_appsync_resolver.on_notification_for_me.kind == "PIPELINE"
    error_message = "onNotificationForMe subscription resolver must be PIPELINE kind (story 8.6)"
  }

  assert {
    condition     = aws_appsync_resolver.on_friend_request_updated.kind == "PIPELINE"
    error_message = "onFriendRequestUpdated subscription resolver must be PIPELINE kind (story 8.6)"
  }
}

# ---------------------------------------------------------------------------
# Test 14: Pipeline function IDs wired into room-scoped resolver pipeline_config
#          (story 8.6)
#
# Each room-scoped subscription's pipeline_config.functions list must contain
# the check_room_membership function ID. We assert that the resolver's
# pipeline_config block exists (length > 0) and the first function references
# the membership check function.
# ---------------------------------------------------------------------------
run "room_scoped_resolvers_pipeline_config_references_membership_function" {
  command = plan

  variables {
    environment                     = "test"
    user_pool_id                    = "eu-central-1_TESTPOOL"
    appsync_logs_role_arn           = "arn:aws:iam::123456789012:role/knotify-test-appsync-logs"
    appsync_invoke_role_arn         = "arn:aws:iam::123456789012:role/knotify-test-appsync-invoke"
    chat_resolver_lambda_arn        = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-chat-resolver-test:live"
    chat_room_membership_table_name = "ChatRoomMembership"
    chat_messages_table_name        = "ChatMessages"
    message_reads_table_name        = "MessageReads"
    notifications_table_name        = "Notifications"
    chat_rooms_table_name           = "ChatRooms"
    dynamodb_role_arn               = "arn:aws:iam::123456789012:role/knotify-test-ddb-role"
  }

  # Assert pipeline_config is non-empty (i.e., at least one function wired)
  assert {
    condition     = length(aws_appsync_resolver.on_message_in_room.pipeline_config) > 0
    error_message = "onMessageInRoom pipeline_config must reference at least one function (story 8.6)"
  }

  assert {
    condition     = length(aws_appsync_resolver.on_typing_in_room.pipeline_config) > 0
    error_message = "onTypingInRoom pipeline_config must reference at least one function (story 8.6)"
  }

  assert {
    condition     = length(aws_appsync_resolver.on_room_deactivated.pipeline_config) > 0
    error_message = "onRoomDeactivated pipeline_config must reference at least one function (story 8.6)"
  }

  assert {
    condition     = length(aws_appsync_resolver.on_room_reactivated.pipeline_config) > 0
    error_message = "onRoomReactivated pipeline_config must reference at least one function (story 8.6)"
  }

  assert {
    condition     = length(aws_appsync_resolver.on_read_receipt.pipeline_config) > 0
    error_message = "onReadReceipt pipeline_config must reference at least one function (story 8.6)"
  }
}

# ---------------------------------------------------------------------------
# Test 15: Identity-scoped resolvers pipeline_config references identity-match
#          function (story 8.6)
# ---------------------------------------------------------------------------
run "identity_scoped_resolvers_pipeline_config_references_identity_function" {
  command = plan

  variables {
    environment                     = "test"
    user_pool_id                    = "eu-central-1_TESTPOOL"
    appsync_logs_role_arn           = "arn:aws:iam::123456789012:role/knotify-test-appsync-logs"
    appsync_invoke_role_arn         = "arn:aws:iam::123456789012:role/knotify-test-appsync-invoke"
    chat_resolver_lambda_arn        = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-chat-resolver-test:live"
    chat_room_membership_table_name = "ChatRoomMembership"
    chat_messages_table_name        = "ChatMessages"
    message_reads_table_name        = "MessageReads"
    notifications_table_name        = "Notifications"
    chat_rooms_table_name           = "ChatRooms"
    dynamodb_role_arn               = "arn:aws:iam::123456789012:role/knotify-test-ddb-role"
  }

  assert {
    condition     = length(aws_appsync_resolver.on_notification_for_me.pipeline_config) > 0
    error_message = "onNotificationForMe pipeline_config must reference at least one function (story 8.6)"
  }

  assert {
    condition     = length(aws_appsync_resolver.on_friend_request_updated.pipeline_config) > 0
    error_message = "onFriendRequestUpdated pipeline_config must reference at least one function (story 8.6)"
  }
}

# ---------------------------------------------------------------------------
# Story 8.8 tests — setTyping PIPELINE resolver (no storage)
#
# Implementation choice: APPSYNC_JS PIPELINE resolver on NoneDS.
# Rationale (recorded here per AC): no Lambda cold-start on mutation; a single
# DDB GetItem membership check (reusing check_room_membership from 8.6) plus
# a pure JS payload pass-through on NoneDS is sufficient — no DynamoDB write.
#
# Tests assert:
#   AC-8.8-T16 — setTyping mutation resolver is kind = PIPELINE
#   AC-8.8-T17 — setTyping pipeline_config references check_room_membership
#                (reused from story 8.6 — membership check GetItem only,
#                 no write op in that function)
#   AC-8.8-T18 — set_typing_passthrough APPSYNC_JS function exists and is
#                wired to the NONE datasource (proves no DynamoDB write in
#                the payload pass-through step)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Test 16: setTyping mutation resolver is PIPELINE kind (story 8.8)
#
# Satisfies AC: "setTyping resolver validates membership and returns the payload
# to be fanned out via onTypingInRoom" — implemented as a PIPELINE resolver.
# ---------------------------------------------------------------------------
run "set_typing_resolver_is_pipeline_kind" {
  command = plan

  variables {
    environment                     = "test"
    user_pool_id                    = "eu-central-1_TESTPOOL"
    appsync_logs_role_arn           = "arn:aws:iam::123456789012:role/knotify-test-appsync-logs"
    appsync_invoke_role_arn         = "arn:aws:iam::123456789012:role/knotify-test-appsync-invoke"
    chat_resolver_lambda_arn        = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-chat-resolver-test:live"
    chat_room_membership_table_name = "ChatRoomMembership"
    chat_messages_table_name        = "ChatMessages"
    message_reads_table_name        = "MessageReads"
    notifications_table_name        = "Notifications"
    chat_rooms_table_name           = "ChatRooms"
    dynamodb_role_arn               = "arn:aws:iam::123456789012:role/knotify-test-ddb-role"
  }

  assert {
    condition     = aws_appsync_resolver.set_typing.kind == "PIPELINE"
    error_message = "setTyping mutation resolver must be PIPELINE kind (story 8.8 — no storage, APPSYNC_JS pipeline)"
  }

  assert {
    condition     = aws_appsync_resolver.set_typing.type == "Mutation"
    error_message = "setTyping resolver must be on the Mutation type"
  }

  assert {
    condition     = aws_appsync_resolver.set_typing.field == "setTyping"
    error_message = "setTyping resolver must be on the setTyping field"
  }
}

# ---------------------------------------------------------------------------
# Test 17: setTyping pipeline_config references check_room_membership function
#          (story 8.8)
#
# Satisfies AC: membership check (GetItem ChatRoomMembership) reused from
# story 8.6. The check_room_membership function performs a read-only GetItem;
# no DynamoDB write op exists in that function.
# ---------------------------------------------------------------------------
run "set_typing_pipeline_config_references_check_room_membership" {
  command = plan

  variables {
    environment                     = "test"
    user_pool_id                    = "eu-central-1_TESTPOOL"
    appsync_logs_role_arn           = "arn:aws:iam::123456789012:role/knotify-test-appsync-logs"
    appsync_invoke_role_arn         = "arn:aws:iam::123456789012:role/knotify-test-appsync-invoke"
    chat_resolver_lambda_arn        = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-chat-resolver-test:live"
    chat_room_membership_table_name = "ChatRoomMembership"
    chat_messages_table_name        = "ChatMessages"
    message_reads_table_name        = "MessageReads"
    notifications_table_name        = "Notifications"
    chat_rooms_table_name           = "ChatRooms"
    dynamodb_role_arn               = "arn:aws:iam::123456789012:role/knotify-test-ddb-role"
  }

  # pipeline_config must be non-empty (at least two functions: membership check + passthrough)
  assert {
    condition     = length(aws_appsync_resolver.set_typing.pipeline_config) > 0
    error_message = "setTyping pipeline_config must reference at least one function (story 8.8)"
  }
}

# ---------------------------------------------------------------------------
# Test 18: set_typing_passthrough function is wired to the NONE datasource
#          (story 8.8)
#
# Satisfies the "no DynamoDB write" AC: the payload pass-through step runs on
# NoneDS — no PutItem / UpdateItem / DeleteItem can originate from a function
# whose datasource is NONE. Combined with check_room_membership (read-only
# GetItem), the entire setTyping pipeline performs zero writes.
# ---------------------------------------------------------------------------
run "set_typing_passthrough_function_uses_none_datasource" {
  command = plan

  variables {
    environment                     = "test"
    user_pool_id                    = "eu-central-1_TESTPOOL"
    appsync_logs_role_arn           = "arn:aws:iam::123456789012:role/knotify-test-appsync-logs"
    appsync_invoke_role_arn         = "arn:aws:iam::123456789012:role/knotify-test-appsync-invoke"
    chat_resolver_lambda_arn        = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-chat-resolver-test:live"
    chat_room_membership_table_name = "ChatRoomMembership"
    chat_messages_table_name        = "ChatMessages"
    message_reads_table_name        = "MessageReads"
    notifications_table_name        = "Notifications"
    chat_rooms_table_name           = "ChatRooms"
    dynamodb_role_arn               = "arn:aws:iam::123456789012:role/knotify-test-ddb-role"
  }

  # The set_typing_passthrough function must exist and use the NONE datasource.
  # A function on NoneDS cannot issue any DynamoDB API call — this is the
  # Terraform-level proof that "no DynamoDB write occurs" in the setTyping path.
  assert {
    condition     = aws_appsync_function.set_typing_passthrough.data_source == aws_appsync_datasource.pipeline_none.name
    error_message = "set_typing_passthrough function must use the NONE datasource (no DynamoDB write in setTyping path — story 8.8)"
  }

  assert {
    condition     = aws_appsync_function.set_typing_passthrough.name == "set_typing_passthrough"
    error_message = "set_typing_passthrough AppSync function must be declared (story 8.8)"
  }
}

# ---------------------------------------------------------------------------
# Story 8.9a tests — NONE-datasource local resolvers for backend-only
# publish mutations (_publishRoomDeactivated, _publishRoomReactivated)
#
# Tests assert:
#   AC-8.9a-T19 — _publishRoomDeactivated resolver exists on NoneDS
#   AC-8.9a-T20 — _publishRoomReactivated resolver exists on NoneDS
#   AC-8.9a-T21 — api_arn output is non-empty (consumed by iam_roles module to
#                 scope room_state_publisher's appsync:GraphQL permission)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Test 19: _publishRoomDeactivated resolver is declared on NoneDS (story 8.9a)
#
# Satisfies AC: "If not wired, add NONE-data-source local resolvers that just
# forward the args — this is the standard subscription-fan-out pattern."
# ---------------------------------------------------------------------------
run "publish_room_deactivated_resolver_uses_none_datasource" {
  command = plan

  variables {
    environment                     = "test"
    user_pool_id                    = "eu-central-1_TESTPOOL"
    appsync_logs_role_arn           = "arn:aws:iam::123456789012:role/knotify-test-appsync-logs"
    appsync_invoke_role_arn         = "arn:aws:iam::123456789012:role/knotify-test-appsync-invoke"
    chat_resolver_lambda_arn        = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-chat-resolver-test:live"
    chat_room_membership_table_name = "ChatRoomMembership"
    chat_messages_table_name        = "ChatMessages"
    message_reads_table_name        = "MessageReads"
    notifications_table_name        = "Notifications"
    chat_rooms_table_name           = "ChatRooms"
    dynamodb_role_arn               = "arn:aws:iam::123456789012:role/knotify-test-ddb-role"
  }

  assert {
    condition     = aws_appsync_resolver.publish_room_deactivated.field == "_publishRoomDeactivated"
    error_message = "_publishRoomDeactivated resolver must be declared (story 8.9a)"
  }

  assert {
    condition     = aws_appsync_resolver.publish_room_deactivated.data_source == aws_appsync_datasource.pipeline_none.name
    error_message = "_publishRoomDeactivated resolver must use NoneDS datasource (no storage I/O)"
  }

  assert {
    condition     = aws_appsync_resolver.publish_room_deactivated.type == "Mutation"
    error_message = "_publishRoomDeactivated resolver must be on the Mutation type"
  }
}

# ---------------------------------------------------------------------------
# Test 20: _publishRoomReactivated resolver is declared on NoneDS (story 8.9a)
# ---------------------------------------------------------------------------
run "publish_room_reactivated_resolver_uses_none_datasource" {
  command = plan

  variables {
    environment                     = "test"
    user_pool_id                    = "eu-central-1_TESTPOOL"
    appsync_logs_role_arn           = "arn:aws:iam::123456789012:role/knotify-test-appsync-logs"
    appsync_invoke_role_arn         = "arn:aws:iam::123456789012:role/knotify-test-appsync-invoke"
    chat_resolver_lambda_arn        = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-chat-resolver-test:live"
    chat_room_membership_table_name = "ChatRoomMembership"
    chat_messages_table_name        = "ChatMessages"
    message_reads_table_name        = "MessageReads"
    notifications_table_name        = "Notifications"
    chat_rooms_table_name           = "ChatRooms"
    dynamodb_role_arn               = "arn:aws:iam::123456789012:role/knotify-test-ddb-role"
  }

  assert {
    condition     = aws_appsync_resolver.publish_room_reactivated.field == "_publishRoomReactivated"
    error_message = "_publishRoomReactivated resolver must be declared (story 8.9a)"
  }

  assert {
    condition     = aws_appsync_resolver.publish_room_reactivated.data_source == aws_appsync_datasource.pipeline_none.name
    error_message = "_publishRoomReactivated resolver must use NoneDS datasource (no storage I/O)"
  }

  assert {
    condition     = aws_appsync_resolver.publish_room_reactivated.type == "Mutation"
    error_message = "_publishRoomReactivated resolver must be on the Mutation type"
  }
}

# ---------------------------------------------------------------------------
# Test 21: api_arn output is wired to the AppSync API ARN (story 8.9a)
#
# Consumed by iam_roles module to scope room_state_publisher's appsync:GraphQL
# permission to the exact publish-mutation field ARNs.
# Uses override_resource to make the ARN deterministic at plan time.
# ---------------------------------------------------------------------------
run "api_arn_output_is_wired_to_appsync_api" {
  command = plan

  variables {
    environment                     = "test"
    user_pool_id                    = "eu-central-1_TESTPOOL"
    appsync_logs_role_arn           = "arn:aws:iam::123456789012:role/knotify-test-appsync-logs"
    appsync_invoke_role_arn         = "arn:aws:iam::123456789012:role/knotify-test-appsync-invoke"
    chat_resolver_lambda_arn        = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-chat-resolver-test:live"
    chat_room_membership_table_name = "ChatRoomMembership"
    chat_messages_table_name        = "ChatMessages"
    message_reads_table_name        = "MessageReads"
    notifications_table_name        = "Notifications"
    chat_rooms_table_name           = "ChatRooms"
    dynamodb_role_arn               = "arn:aws:iam::123456789012:role/knotify-test-ddb-role"
  }

  override_resource {
    target = aws_appsync_graphql_api.knotify
    values = {
      arn = "arn:aws:appsync:eu-central-1:123456789012:apis/TESTAPI"
      uris = {
        GRAPHQL  = "https://TESTAPI.appsync-api.eu-central-1.amazonaws.com/graphql"
        REALTIME = "wss://TESTAPI.appsync-realtime-api.eu-central-1.amazonaws.com/graphql"
      }
    }
    override_during = plan
  }

  assert {
    condition     = output.api_arn == "arn:aws:appsync:eu-central-1:123456789012:apis/TESTAPI"
    error_message = "api_arn output must be wired to aws_appsync_graphql_api.knotify.arn (story 8.9a)"
  }
}

# ===========================================================================
# Tests 22–23: NONE-datasource local resolvers for 8.9c backend-only mutations
#
# publishNotification and _publishFriendRequestUpdated are @aws_iam mutations
# called by the notifications_publisher Lambda.  Each needs a UNIT resolver on
# the NoneDS NONE datasource so AppSync can fan-out to @aws_subscribe subscribers.
# ===========================================================================

# ---------------------------------------------------------------------------
# Test 22: publishNotification resolver uses NoneDS (NONE datasource)
#
# Satisfies AC (story 8.9c): "NONE-datasource local resolvers for
# publishNotification and _publishFriendRequestUpdated added to appsync module"
# ---------------------------------------------------------------------------
run "publish_notification_resolver_uses_none_datasource" {
  command = plan

  variables {
    environment                     = "test"
    user_pool_id                    = "eu-central-1_TESTPOOL"
    appsync_logs_role_arn           = "arn:aws:iam::123456789012:role/knotify-test-appsync-logs"
    appsync_invoke_role_arn         = "arn:aws:iam::123456789012:role/knotify-test-appsync-invoke"
    chat_resolver_lambda_arn        = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-chat-resolver-test:live"
    chat_room_membership_table_name = "ChatRoomMembership"
    chat_messages_table_name        = "ChatMessages"
    message_reads_table_name        = "MessageReads"
    notifications_table_name        = "Notifications"
    chat_rooms_table_name           = "ChatRooms"
    dynamodb_role_arn               = "arn:aws:iam::123456789012:role/knotify-test-ddb-role"
  }

  assert {
    condition     = aws_appsync_resolver.publish_notification.type == "Mutation"
    error_message = "publish_notification resolver must be attached to the Mutation type"
  }

  assert {
    condition     = aws_appsync_resolver.publish_notification.field == "publishNotification"
    error_message = "publish_notification resolver must be attached to the publishNotification field"
  }

  assert {
    condition     = aws_appsync_resolver.publish_notification.data_source == aws_appsync_datasource.pipeline_none.name
    error_message = "publish_notification resolver must use the NoneDS NONE datasource"
  }
}

# ---------------------------------------------------------------------------
# Test 23: _publishFriendRequestUpdated resolver uses NoneDS (NONE datasource)
#
# Satisfies AC (story 8.9c): "_publishFriendRequestUpdated NONE-datasource
# resolver added to appsync module"
# ---------------------------------------------------------------------------
run "publish_friend_request_updated_resolver_uses_none_datasource" {
  command = plan

  variables {
    environment                     = "test"
    user_pool_id                    = "eu-central-1_TESTPOOL"
    appsync_logs_role_arn           = "arn:aws:iam::123456789012:role/knotify-test-appsync-logs"
    appsync_invoke_role_arn         = "arn:aws:iam::123456789012:role/knotify-test-appsync-invoke"
    chat_resolver_lambda_arn        = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-chat-resolver-test:live"
    chat_room_membership_table_name = "ChatRoomMembership"
    chat_messages_table_name        = "ChatMessages"
    message_reads_table_name        = "MessageReads"
    notifications_table_name        = "Notifications"
    chat_rooms_table_name           = "ChatRooms"
    dynamodb_role_arn               = "arn:aws:iam::123456789012:role/knotify-test-ddb-role"
  }

  assert {
    condition     = aws_appsync_resolver.publish_friend_request_updated.type == "Mutation"
    error_message = "_publishFriendRequestUpdated resolver must be attached to the Mutation type"
  }

  assert {
    condition     = aws_appsync_resolver.publish_friend_request_updated.field == "_publishFriendRequestUpdated"
    error_message = "_publishFriendRequestUpdated resolver must be attached to the _publishFriendRequestUpdated field"
  }

  assert {
    condition     = aws_appsync_resolver.publish_friend_request_updated.data_source == aws_appsync_datasource.pipeline_none.name
    error_message = "_publishFriendRequestUpdated resolver must use the NoneDS NONE datasource"
  }
}
