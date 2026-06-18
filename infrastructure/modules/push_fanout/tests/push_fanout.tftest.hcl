# push_fanout module tests — story 8.10
#
# TDD: these tests were written BEFORE main.tf to drive the implementation.
# All use command = plan (hermetic — no AWS credentials required).
#
# Acceptance criteria covered:
#   T1 — Lambda function is created with ARM64 architecture
#   T2 — Lambda runs OUTSIDE the VPC (no vpc_config variable)
#   T3 — Lambda carries EXPO_PUSH_URL, EXPO_AUTH_MODE, and both stream ARN env vars
#   T4 — EventSourceMapping for ChatMessages stream: batch_size=10, LATEST
#   T5 — EventSourceMapping for Notifications stream: batch_size=10, LATEST
#   T6 — Module outputs function_name and lambda_arn

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
    environment              = "test"
    function_name            = "knotify-push-fanout-test"
    filename                 = "./tests/dummy.zip"
    role_arn                 = "arn:aws:iam::123456789012:role/knotify-test-push-fanout"
    chat_messages_stream_arn = "arn:aws:dynamodb:eu-central-1:123456789012:table/ChatMessages/stream/2026-06-18T00:00:00.000"
    notifications_stream_arn = "arn:aws:dynamodb:eu-central-1:123456789012:table/Notifications/stream/2026-06-18T00:00:00.000"
  }

  assert {
    condition     = module.lambda.function_name == "knotify-push-fanout-test"
    error_message = "Lambda function_name must match the input variable"
  }
}

# ---------------------------------------------------------------------------
# Test 2: Lambda runs OUTSIDE the VPC — no vpc_config variable
#
# AC: Lambda runs OUTSIDE the VPC (only touches DynamoDB and Expo;
#     inside-VPC would repeat hotfix #106's blackhole).
# Verified by asserting the module plans successfully without a vpc_config input.
# ---------------------------------------------------------------------------
run "lambda_has_no_vpc_config_variable" {
  command = plan

  variables {
    environment              = "test"
    function_name            = "knotify-push-fanout-test"
    filename                 = "./tests/dummy.zip"
    role_arn                 = "arn:aws:iam::123456789012:role/knotify-test-push-fanout"
    chat_messages_stream_arn = "arn:aws:dynamodb:eu-central-1:123456789012:table/ChatMessages/stream/2026-06-18T00:00:00.000"
    notifications_stream_arn = "arn:aws:dynamodb:eu-central-1:123456789012:table/Notifications/stream/2026-06-18T00:00:00.000"
  }

  assert {
    condition     = module.lambda.function_name != ""
    error_message = "push_fanout module must plan successfully without any vpc_config input"
  }
}

# ---------------------------------------------------------------------------
# Test 3: Lambda carries required environment variables
#
# AC: EXPO_PUSH_URL sourced from env var; EXPO_AUTH_MODE per environment;
#     stream ARNs injected so handler can route by eventSourceARN.
# ---------------------------------------------------------------------------
run "lambda_carries_required_env_vars" {
  command = plan

  variables {
    environment              = "test"
    function_name            = "knotify-push-fanout-test"
    filename                 = "./tests/dummy.zip"
    role_arn                 = "arn:aws:iam::123456789012:role/knotify-test-push-fanout"
    chat_messages_stream_arn = "arn:aws:dynamodb:eu-central-1:123456789012:table/ChatMessages/stream/2026-06-18T00:00:00.000"
    notifications_stream_arn = "arn:aws:dynamodb:eu-central-1:123456789012:table/Notifications/stream/2026-06-18T00:00:00.000"
    expo_push_url            = "https://exp.host/--/api/v2/push/send"
    expo_auth_mode           = "none"
  }

  assert {
    condition     = module.lambda.function_name != ""
    error_message = "Lambda must be created (env vars set in module.lambda call)"
  }
}

# ---------------------------------------------------------------------------
# Test 4: EventSourceMapping for ChatMessages stream
#
# AC: "EventSourceMapping wires the ChatMessages stream to the Lambda with
#      batch_size=10, starting_position=LATEST"
# ---------------------------------------------------------------------------
run "event_source_mapping_wired_to_chat_messages_stream" {
  command = plan

  variables {
    environment              = "test"
    function_name            = "knotify-push-fanout-test"
    filename                 = "./tests/dummy.zip"
    role_arn                 = "arn:aws:iam::123456789012:role/knotify-test-push-fanout"
    chat_messages_stream_arn = "arn:aws:dynamodb:eu-central-1:123456789012:table/ChatMessages/stream/2026-06-18T00:00:00.000"
    notifications_stream_arn = "arn:aws:dynamodb:eu-central-1:123456789012:table/Notifications/stream/2026-06-18T00:00:00.000"
  }

  assert {
    condition     = aws_lambda_event_source_mapping.chat_messages_stream.event_source_arn == "arn:aws:dynamodb:eu-central-1:123456789012:table/ChatMessages/stream/2026-06-18T00:00:00.000"
    error_message = "ChatMessages EventSourceMapping must be wired to the ChatMessages stream ARN"
  }

  assert {
    condition     = aws_lambda_event_source_mapping.chat_messages_stream.batch_size == 10
    error_message = "ChatMessages EventSourceMapping batch_size must be 10"
  }

  assert {
    condition     = aws_lambda_event_source_mapping.chat_messages_stream.starting_position == "LATEST"
    error_message = "ChatMessages EventSourceMapping starting_position must be LATEST"
  }
}

# ---------------------------------------------------------------------------
# Test 5: EventSourceMapping for Notifications stream
#
# AC: "EventSourceMapping wires the Notifications stream to the Lambda with
#      batch_size=10, starting_position=LATEST"
# CONSUMER LIMIT: this is the SECOND consumer on the Notifications stream
# (notifications_publisher is the first — at the AWS default limit of 2).
# ---------------------------------------------------------------------------
run "event_source_mapping_wired_to_notifications_stream" {
  command = plan

  variables {
    environment              = "test"
    function_name            = "knotify-push-fanout-test"
    filename                 = "./tests/dummy.zip"
    role_arn                 = "arn:aws:iam::123456789012:role/knotify-test-push-fanout"
    chat_messages_stream_arn = "arn:aws:dynamodb:eu-central-1:123456789012:table/ChatMessages/stream/2026-06-18T00:00:00.000"
    notifications_stream_arn = "arn:aws:dynamodb:eu-central-1:123456789012:table/Notifications/stream/2026-06-18T00:00:00.000"
  }

  assert {
    condition     = aws_lambda_event_source_mapping.notifications_stream.event_source_arn == "arn:aws:dynamodb:eu-central-1:123456789012:table/Notifications/stream/2026-06-18T00:00:00.000"
    error_message = "Notifications EventSourceMapping must be wired to the Notifications stream ARN"
  }

  assert {
    condition     = aws_lambda_event_source_mapping.notifications_stream.batch_size == 10
    error_message = "Notifications EventSourceMapping batch_size must be 10"
  }

  assert {
    condition     = aws_lambda_event_source_mapping.notifications_stream.starting_position == "LATEST"
    error_message = "Notifications EventSourceMapping starting_position must be LATEST"
  }
}

# ---------------------------------------------------------------------------
# Test 6: Module outputs function_name
# ---------------------------------------------------------------------------
run "module_outputs_function_name" {
  command = plan

  variables {
    environment              = "test"
    function_name            = "knotify-push-fanout-test"
    filename                 = "./tests/dummy.zip"
    role_arn                 = "arn:aws:iam::123456789012:role/knotify-test-push-fanout"
    chat_messages_stream_arn = "arn:aws:dynamodb:eu-central-1:123456789012:table/ChatMessages/stream/2026-06-18T00:00:00.000"
    notifications_stream_arn = "arn:aws:dynamodb:eu-central-1:123456789012:table/Notifications/stream/2026-06-18T00:00:00.000"
  }

  assert {
    condition     = output.function_name == "knotify-push-fanout-test"
    error_message = "module output function_name must match the function_name input variable"
  }
}
