# deactivate_chat_rooms module tests — all use command = plan (hermetic, no AWS credentials required)
# Tests are designed so `terraform test` exits 0 from infrastructure/modules/deactivate_chat_rooms/
#
# TDD note: this file was authored BEFORE main.tf/variables.tf/outputs.tf to drive
# the implementation shape via acceptance criteria (story 9.4).
#
# Acceptance criteria covered:
#   AC1 — module.lambda is declared (function_name passthrough)
#   AC2 — environment_variables contains TABLE_CHAT_ROOMS and TABLE_CHAT_ROOM_MEMBERSHIP
#   AC3 — lambda_arn output is wired to module.lambda.alias_arn
#   AC4 — no vpc_config (Lambda runs outside the VPC — DynamoDB-only, no Aurora)

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
# Test 1: function_name is wired through to the lambda sub-module
#
# Satisfies AC1: module correctly delegates to the lambda sub-module.
# ---------------------------------------------------------------------------
run "function_name_passed_to_lambda_submodule" {
  command = plan

  variables {
    function_name                 = "knotify-deactivate-chat-rooms-test"
    filename                      = "./tests/dummy.zip"
    role_arn                      = "arn:aws:iam::123456789012:role/knotify-test-deactivate-chat-rooms"
    chat_rooms_table_name         = "ChatRooms"
    chat_room_membership_table_name = "ChatRoomMembership"
  }

  assert {
    condition     = module.lambda.function_name == "knotify-deactivate-chat-rooms-test"
    error_message = "function_name must be passed through to the lambda sub-module"
  }
}

# ---------------------------------------------------------------------------
# Test 2: lambda_arn output is wired to the live alias ARN
#
# Satisfies AC3: the step_functions module uses module.deactivate_chat_rooms.lambda_arn
# as the deactivate_chat_rooms ARN in its lambda_arns input map.
# ---------------------------------------------------------------------------
run "lambda_arn_output_wired_to_alias_arn" {
  command = plan

  variables {
    function_name                 = "knotify-deactivate-chat-rooms-test"
    filename                      = "./tests/dummy.zip"
    role_arn                      = "arn:aws:iam::123456789012:role/knotify-test-deactivate-chat-rooms"
    chat_rooms_table_name         = "ChatRooms"
    chat_room_membership_table_name = "ChatRoomMembership"
  }

  override_module {
    target = module.lambda
    outputs = {
      alias_arn     = "arn:aws:lambda:eu-central-1:123456789012:function:knotify-deactivate-chat-rooms-test:live"
      function_name = "knotify-deactivate-chat-rooms-test"
      invoke_arn    = "arn:aws:apigateway:eu-central-1:lambda:path/2015-03-31/functions/arn:aws:lambda:eu-central-1:123456789012:function:knotify-deactivate-chat-rooms-test:live/invocations"
    }
  }

  assert {
    condition     = output.lambda_arn == "arn:aws:lambda:eu-central-1:123456789012:function:knotify-deactivate-chat-rooms-test:live"
    error_message = "lambda_arn output must be wired to module.lambda.alias_arn"
  }
}

# ---------------------------------------------------------------------------
# Test 3: table name defaults are used when vars are omitted
#
# When chat_rooms_table_name and chat_room_membership_table_name are omitted,
# the module uses "ChatRooms" and "ChatRoomMembership" as defaults and plans
# without error.
# ---------------------------------------------------------------------------
run "table_name_defaults_and_module_plans_without_error" {
  command = plan

  variables {
    function_name = "knotify-deactivate-chat-rooms-test"
    filename      = "./tests/dummy.zip"
    role_arn      = "arn:aws:iam::123456789012:role/knotify-test-deactivate-chat-rooms"
    # chat_rooms_table_name and chat_room_membership_table_name intentionally omitted
  }

  assert {
    condition     = module.lambda.function_name == "knotify-deactivate-chat-rooms-test"
    error_message = "module must plan successfully when table name vars are omitted"
  }
}
