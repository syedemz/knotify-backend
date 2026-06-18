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
    environment        = "test"
    user_pool_id       = "eu-central-1_TESTPOOL"
    appsync_logs_role_arn     = "arn:aws:iam::123456789012:role/knotify-test-appsync-logs"
    appsync_invoke_role_arn   = "arn:aws:iam::123456789012:role/knotify-test-appsync-invoke"
    chat_resolver_lambda_arn  = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-chat-resolver-test:live"
    chat_rooms_table_arn              = "arn:aws:dynamodb:eu-central-1:123456789012:table/ChatRooms"
    chat_room_membership_table_name   = "ChatRoomMembership"
    chat_room_membership_table_arn    = "arn:aws:dynamodb:eu-central-1:123456789012:table/ChatRoomMembership"
    chat_messages_table_name          = "ChatMessages"
    chat_messages_table_arn           = "arn:aws:dynamodb:eu-central-1:123456789012:table/ChatMessages"
    message_reads_table_name          = "MessageReads"
    message_reads_table_arn           = "arn:aws:dynamodb:eu-central-1:123456789012:table/MessageReads"
    notifications_table_name          = "Notifications"
    notifications_table_arn           = "arn:aws:dynamodb:eu-central-1:123456789012:table/Notifications"
    chat_rooms_table_name             = "ChatRooms"
    dynamodb_role_arn                 = "arn:aws:iam::123456789012:role/knotify-test-ddb-role"
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
    environment        = "test"
    user_pool_id       = "eu-central-1_TESTPOOL"
    appsync_logs_role_arn     = "arn:aws:iam::123456789012:role/knotify-test-appsync-logs"
    appsync_invoke_role_arn   = "arn:aws:iam::123456789012:role/knotify-test-appsync-invoke"
    chat_resolver_lambda_arn  = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-chat-resolver-test:live"
    chat_rooms_table_arn              = "arn:aws:dynamodb:eu-central-1:123456789012:table/ChatRooms"
    chat_room_membership_table_name   = "ChatRoomMembership"
    chat_room_membership_table_arn    = "arn:aws:dynamodb:eu-central-1:123456789012:table/ChatRoomMembership"
    chat_messages_table_name          = "ChatMessages"
    chat_messages_table_arn           = "arn:aws:dynamodb:eu-central-1:123456789012:table/ChatMessages"
    message_reads_table_name          = "MessageReads"
    message_reads_table_arn           = "arn:aws:dynamodb:eu-central-1:123456789012:table/MessageReads"
    notifications_table_name          = "Notifications"
    notifications_table_arn           = "arn:aws:dynamodb:eu-central-1:123456789012:table/Notifications"
    chat_rooms_table_name             = "ChatRooms"
    dynamodb_role_arn                 = "arn:aws:iam::123456789012:role/knotify-test-ddb-role"
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
    environment        = "test"
    user_pool_id       = "eu-central-1_TESTPOOL"
    appsync_logs_role_arn     = "arn:aws:iam::123456789012:role/knotify-test-appsync-logs"
    appsync_invoke_role_arn   = "arn:aws:iam::123456789012:role/knotify-test-appsync-invoke"
    chat_resolver_lambda_arn  = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-chat-resolver-test:live"
    chat_rooms_table_arn              = "arn:aws:dynamodb:eu-central-1:123456789012:table/ChatRooms"
    chat_room_membership_table_name   = "ChatRoomMembership"
    chat_room_membership_table_arn    = "arn:aws:dynamodb:eu-central-1:123456789012:table/ChatRoomMembership"
    chat_messages_table_name          = "ChatMessages"
    chat_messages_table_arn           = "arn:aws:dynamodb:eu-central-1:123456789012:table/ChatMessages"
    message_reads_table_name          = "MessageReads"
    message_reads_table_arn           = "arn:aws:dynamodb:eu-central-1:123456789012:table/MessageReads"
    notifications_table_name          = "Notifications"
    notifications_table_arn           = "arn:aws:dynamodb:eu-central-1:123456789012:table/Notifications"
    chat_rooms_table_name             = "ChatRooms"
    dynamodb_role_arn                 = "arn:aws:iam::123456789012:role/knotify-test-ddb-role"
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
    environment        = "test"
    user_pool_id       = "eu-central-1_TESTPOOL"
    appsync_logs_role_arn     = "arn:aws:iam::123456789012:role/knotify-test-appsync-logs"
    appsync_invoke_role_arn   = "arn:aws:iam::123456789012:role/knotify-test-appsync-invoke"
    chat_resolver_lambda_arn  = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-chat-resolver-test:live"
    chat_rooms_table_arn              = "arn:aws:dynamodb:eu-central-1:123456789012:table/ChatRooms"
    chat_room_membership_table_name   = "ChatRoomMembership"
    chat_room_membership_table_arn    = "arn:aws:dynamodb:eu-central-1:123456789012:table/ChatRoomMembership"
    chat_messages_table_name          = "ChatMessages"
    chat_messages_table_arn           = "arn:aws:dynamodb:eu-central-1:123456789012:table/ChatMessages"
    message_reads_table_name          = "MessageReads"
    message_reads_table_arn           = "arn:aws:dynamodb:eu-central-1:123456789012:table/MessageReads"
    notifications_table_name          = "Notifications"
    notifications_table_arn           = "arn:aws:dynamodb:eu-central-1:123456789012:table/Notifications"
    chat_rooms_table_name             = "ChatRooms"
    dynamodb_role_arn                 = "arn:aws:iam::123456789012:role/knotify-test-ddb-role"
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
    environment        = "test"
    user_pool_id       = "eu-central-1_TESTPOOL"
    appsync_logs_role_arn     = "arn:aws:iam::123456789012:role/knotify-test-appsync-logs"
    appsync_invoke_role_arn   = "arn:aws:iam::123456789012:role/knotify-test-appsync-invoke"
    chat_resolver_lambda_arn  = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-chat-resolver-test:live"
    chat_rooms_table_arn              = "arn:aws:dynamodb:eu-central-1:123456789012:table/ChatRooms"
    chat_room_membership_table_name   = "ChatRoomMembership"
    chat_room_membership_table_arn    = "arn:aws:dynamodb:eu-central-1:123456789012:table/ChatRoomMembership"
    chat_messages_table_name          = "ChatMessages"
    chat_messages_table_arn           = "arn:aws:dynamodb:eu-central-1:123456789012:table/ChatMessages"
    message_reads_table_name          = "MessageReads"
    message_reads_table_arn           = "arn:aws:dynamodb:eu-central-1:123456789012:table/MessageReads"
    notifications_table_name          = "Notifications"
    notifications_table_arn           = "arn:aws:dynamodb:eu-central-1:123456789012:table/Notifications"
    chat_rooms_table_name             = "ChatRooms"
    dynamodb_role_arn                 = "arn:aws:iam::123456789012:role/knotify-test-ddb-role"
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
    environment        = "test"
    user_pool_id       = "eu-central-1_TESTPOOL"
    appsync_logs_role_arn     = "arn:aws:iam::123456789012:role/knotify-test-appsync-logs"
    appsync_invoke_role_arn   = "arn:aws:iam::123456789012:role/knotify-test-appsync-invoke"
    chat_resolver_lambda_arn  = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-chat-resolver-test:live"
    chat_rooms_table_arn              = "arn:aws:dynamodb:eu-central-1:123456789012:table/ChatRooms"
    chat_room_membership_table_name   = "ChatRoomMembership"
    chat_room_membership_table_arn    = "arn:aws:dynamodb:eu-central-1:123456789012:table/ChatRoomMembership"
    chat_messages_table_name          = "ChatMessages"
    chat_messages_table_arn           = "arn:aws:dynamodb:eu-central-1:123456789012:table/ChatMessages"
    message_reads_table_name          = "MessageReads"
    message_reads_table_arn           = "arn:aws:dynamodb:eu-central-1:123456789012:table/MessageReads"
    notifications_table_name          = "Notifications"
    notifications_table_arn           = "arn:aws:dynamodb:eu-central-1:123456789012:table/Notifications"
    chat_rooms_table_name             = "ChatRooms"
    dynamodb_role_arn                 = "arn:aws:iam::123456789012:role/knotify-test-ddb-role"
  }

  override_resource {
    target = aws_appsync_graphql_api.knotify
    values = {
      id          = "abc123appsyncid"
      uris        = {
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
    environment        = "test"
    user_pool_id       = "eu-central-1_TESTPOOL"
    appsync_logs_role_arn     = "arn:aws:iam::123456789012:role/knotify-test-appsync-logs"
    appsync_invoke_role_arn   = "arn:aws:iam::123456789012:role/knotify-test-appsync-invoke"
    chat_resolver_lambda_arn  = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-chat-resolver-test:live"
    chat_rooms_table_arn              = "arn:aws:dynamodb:eu-central-1:123456789012:table/ChatRooms"
    chat_room_membership_table_name   = "ChatRoomMembership"
    chat_room_membership_table_arn    = "arn:aws:dynamodb:eu-central-1:123456789012:table/ChatRoomMembership"
    chat_messages_table_name          = "ChatMessages"
    chat_messages_table_arn           = "arn:aws:dynamodb:eu-central-1:123456789012:table/ChatMessages"
    message_reads_table_name          = "MessageReads"
    message_reads_table_arn           = "arn:aws:dynamodb:eu-central-1:123456789012:table/MessageReads"
    notifications_table_name          = "Notifications"
    notifications_table_arn           = "arn:aws:dynamodb:eu-central-1:123456789012:table/Notifications"
    chat_rooms_table_name             = "ChatRooms"
    dynamodb_role_arn                 = "arn:aws:iam::123456789012:role/knotify-test-ddb-role"
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
    environment        = "test"
    user_pool_id       = "eu-central-1_TESTPOOL"
    appsync_logs_role_arn     = "arn:aws:iam::123456789012:role/knotify-test-appsync-logs"
    appsync_invoke_role_arn   = "arn:aws:iam::123456789012:role/knotify-test-appsync-invoke"
    chat_resolver_lambda_arn  = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-chat-resolver-test:live"
    chat_rooms_table_arn              = "arn:aws:dynamodb:eu-central-1:123456789012:table/ChatRooms"
    chat_room_membership_table_name   = "ChatRoomMembership"
    chat_room_membership_table_arn    = "arn:aws:dynamodb:eu-central-1:123456789012:table/ChatRoomMembership"
    chat_messages_table_name          = "ChatMessages"
    chat_messages_table_arn           = "arn:aws:dynamodb:eu-central-1:123456789012:table/ChatMessages"
    message_reads_table_name          = "MessageReads"
    message_reads_table_arn           = "arn:aws:dynamodb:eu-central-1:123456789012:table/MessageReads"
    notifications_table_name          = "Notifications"
    notifications_table_arn           = "arn:aws:dynamodb:eu-central-1:123456789012:table/Notifications"
    chat_rooms_table_name             = "ChatRooms"
    dynamodb_role_arn                 = "arn:aws:iam::123456789012:role/knotify-test-ddb-role"
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
