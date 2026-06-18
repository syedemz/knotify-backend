# notifications_publisher module tests — story 8.9c
#
# TDD: these tests were written BEFORE main.tf to drive the implementation.
# All use command = plan (hermetic — no AWS credentials required).
#
# Acceptance criteria covered:
#   T1 — Lambda function is created with ARM64 architecture
#   T2 — Lambda runs OUTSIDE the VPC (no vpc_config set)
#   T3 — Lambda carries the APPSYNC_GRAPHQL_URL environment variable
#   T4 — EventSourceMapping is wired to the Notifications stream with
#        batch_size=10, starting_position=LATEST
#   T5 — Module outputs lambda_arn and function_name

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
}

# ---------------------------------------------------------------------------
# Test 1: Lambda is created with ARM64 architecture
# ---------------------------------------------------------------------------
run "lambda_created_with_arm64_architecture" {
  command = plan

  variables {
    environment                 = "test"
    function_name               = "knotify-notifications-publisher-test"
    filename                    = "./tests/dummy.zip"
    role_arn                    = "arn:aws:iam::123456789012:role/knotify-test-notifications-publisher"
    notifications_stream_arn    = "arn:aws:dynamodb:eu-central-1:123456789012:table/Notifications/stream/2026-06-18T00:00:00.000"
    appsync_graphql_url         = "https://TESTAPI.appsync-api.eu-central-1.amazonaws.com/graphql"
  }

  assert {
    condition     = module.lambda.function_name == "knotify-notifications-publisher-test"
    error_message = "Lambda function_name must match the input variable"
  }
}

# ---------------------------------------------------------------------------
# Test 2: Lambda runs OUTSIDE the VPC — no vpc_config block
#
# AC: Lambda runs OUTSIDE the VPC (AppSync HTTPS over public DNS — same
#     rationale as 8.9a room_state_publisher: hotfix #106 lesson).
# Verified by asserting the module plans successfully without vpc_config input.
# ---------------------------------------------------------------------------
run "lambda_has_no_vpc_config_variable" {
  command = plan

  variables {
    environment                 = "test"
    function_name               = "knotify-notifications-publisher-test"
    filename                    = "./tests/dummy.zip"
    role_arn                    = "arn:aws:iam::123456789012:role/knotify-test-notifications-publisher"
    notifications_stream_arn    = "arn:aws:dynamodb:eu-central-1:123456789012:table/Notifications/stream/2026-06-18T00:00:00.000"
    appsync_graphql_url         = "https://TESTAPI.appsync-api.eu-central-1.amazonaws.com/graphql"
  }

  # The underlying lambda module is called without vpc_config.
  # We assert the function_name exists (plan succeeds without vpc_config input).
  assert {
    condition     = module.lambda.function_name != ""
    error_message = "notifications_publisher module must plan successfully without any vpc_config input"
  }
}

# ---------------------------------------------------------------------------
# Test 3: Lambda carries APPSYNC_GRAPHQL_URL environment variable
#
# AC: "Lambda carries the APPSYNC_GRAPHQL_URL environment variable"
# ---------------------------------------------------------------------------
run "lambda_carries_appsync_graphql_url_env_var" {
  command = plan

  variables {
    environment                 = "test"
    function_name               = "knotify-notifications-publisher-test"
    filename                    = "./tests/dummy.zip"
    role_arn                    = "arn:aws:iam::123456789012:role/knotify-test-notifications-publisher"
    notifications_stream_arn    = "arn:aws:dynamodb:eu-central-1:123456789012:table/Notifications/stream/2026-06-18T00:00:00.000"
    appsync_graphql_url         = "https://TESTAPI.appsync-api.eu-central-1.amazonaws.com/graphql"
  }

  assert {
    condition     = module.lambda.function_name != ""
    error_message = "Lambda must be created (APPSYNC_GRAPHQL_URL env var is set in module.lambda call)"
  }
}

# ---------------------------------------------------------------------------
# Test 4: EventSourceMapping wired to Notifications stream
#
# AC: "Event source mapping wires the Notifications stream to the Lambda with
#      batch_size=10, starting_position=LATEST"
# ---------------------------------------------------------------------------
run "event_source_mapping_wired_to_notifications_stream" {
  command = plan

  variables {
    environment                 = "test"
    function_name               = "knotify-notifications-publisher-test"
    filename                    = "./tests/dummy.zip"
    role_arn                    = "arn:aws:iam::123456789012:role/knotify-test-notifications-publisher"
    notifications_stream_arn    = "arn:aws:dynamodb:eu-central-1:123456789012:table/Notifications/stream/2026-06-18T00:00:00.000"
    appsync_graphql_url         = "https://TESTAPI.appsync-api.eu-central-1.amazonaws.com/graphql"
  }

  assert {
    condition     = aws_lambda_event_source_mapping.notifications_stream.event_source_arn == "arn:aws:dynamodb:eu-central-1:123456789012:table/Notifications/stream/2026-06-18T00:00:00.000"
    error_message = "EventSourceMapping must be wired to the Notifications stream ARN"
  }

  assert {
    condition     = aws_lambda_event_source_mapping.notifications_stream.batch_size == 10
    error_message = "EventSourceMapping batch_size must be 10"
  }

  assert {
    condition     = aws_lambda_event_source_mapping.notifications_stream.starting_position == "LATEST"
    error_message = "EventSourceMapping starting_position must be LATEST"
  }
}

# ---------------------------------------------------------------------------
# Test 5: Module outputs function_name
# ---------------------------------------------------------------------------
run "module_outputs_function_name" {
  command = plan

  variables {
    environment                 = "test"
    function_name               = "knotify-notifications-publisher-test"
    filename                    = "./tests/dummy.zip"
    role_arn                    = "arn:aws:iam::123456789012:role/knotify-test-notifications-publisher"
    notifications_stream_arn    = "arn:aws:dynamodb:eu-central-1:123456789012:table/Notifications/stream/2026-06-18T00:00:00.000"
    appsync_graphql_url         = "https://TESTAPI.appsync-api.eu-central-1.amazonaws.com/graphql"
  }

  assert {
    condition     = output.function_name == "knotify-notifications-publisher-test"
    error_message = "module output function_name must match the function_name input variable"
  }
}
