# DynamoDB module tests — all use command = plan (hermetic, no AWS credentials required)
# Tests are designed so `terraform test` exits 0 from infrastructure/modules/dynamodb/

mock_provider "aws" {}

# ---------------------------------------------------------------------------
# Test 1: ChatRooms billing mode and hash key
# Satisfies AC: billing_mode PAY_PER_REQUEST; PK room_id (S)
# ---------------------------------------------------------------------------
run "chat_rooms_billing_mode_and_hash_key" {
  command = plan

  variables {
    environment                  = "dev"
    point_in_time_recovery_enabled = false
    deletion_protection_enabled  = false
  }

  assert {
    condition     = aws_dynamodb_table.chat_rooms.billing_mode == "PAY_PER_REQUEST"
    error_message = "ChatRooms billing_mode must be PAY_PER_REQUEST"
  }

  assert {
    condition     = aws_dynamodb_table.chat_rooms.hash_key == "room_id"
    error_message = "ChatRooms hash_key must be room_id"
  }

  assert {
    condition = anytrue([
      for attr in aws_dynamodb_table.chat_rooms.attribute : attr.name == "room_id" && attr.type == "S"
    ])
    error_message = "ChatRooms must define attribute room_id of type S"
  }
}

# ---------------------------------------------------------------------------
# Test 2: ChatRoomMembership billing mode, hash key, and range key
# Satisfies AC: PK user_id (S), SK room_id (S), billing_mode PAY_PER_REQUEST
# ---------------------------------------------------------------------------
run "chat_room_membership_keys_and_billing" {
  command = plan

  variables {
    environment                  = "dev"
    point_in_time_recovery_enabled = false
    deletion_protection_enabled  = false
  }

  assert {
    condition     = aws_dynamodb_table.chat_room_membership.billing_mode == "PAY_PER_REQUEST"
    error_message = "ChatRoomMembership billing_mode must be PAY_PER_REQUEST"
  }

  assert {
    condition     = aws_dynamodb_table.chat_room_membership.hash_key == "user_id"
    error_message = "ChatRoomMembership hash_key must be user_id"
  }

  assert {
    condition     = aws_dynamodb_table.chat_room_membership.range_key == "room_id"
    error_message = "ChatRoomMembership range_key must be room_id"
  }

  assert {
    condition = anytrue([
      for attr in aws_dynamodb_table.chat_room_membership.attribute : attr.name == "user_id" && attr.type == "S"
    ])
    error_message = "ChatRoomMembership must define attribute user_id of type S"
  }

  assert {
    condition = anytrue([
      for attr in aws_dynamodb_table.chat_room_membership.attribute : attr.name == "room_id" && attr.type == "S"
    ])
    error_message = "ChatRoomMembership must define attribute room_id of type S"
  }
}

# ---------------------------------------------------------------------------
# Test 3: Server-side encryption enabled on both tables
# Satisfies AC: server_side_encryption enabled on both tables
# ---------------------------------------------------------------------------
run "server_side_encryption_enabled" {
  command = plan

  variables {
    environment                  = "dev"
    point_in_time_recovery_enabled = false
    deletion_protection_enabled  = false
  }

  assert {
    condition     = aws_dynamodb_table.chat_rooms.server_side_encryption[0].enabled == true
    error_message = "ChatRooms server_side_encryption must be enabled"
  }

  assert {
    condition     = aws_dynamodb_table.chat_room_membership.server_side_encryption[0].enabled == true
    error_message = "ChatRoomMembership server_side_encryption must be enabled"
  }
}

# ---------------------------------------------------------------------------
# Test 4: Tags — Environment and Project on both tables (dev values)
# Satisfies AC: tags for Environment and Project
# ---------------------------------------------------------------------------
run "tags_present_in_dev" {
  command = plan

  variables {
    environment                  = "dev"
    point_in_time_recovery_enabled = false
    deletion_protection_enabled  = false
  }

  assert {
    condition     = aws_dynamodb_table.chat_rooms.tags["Environment"] == "dev"
    error_message = "ChatRooms Environment tag must be dev"
  }

  assert {
    condition     = aws_dynamodb_table.chat_rooms.tags["Project"] == "knotify"
    error_message = "ChatRooms Project tag must be knotify"
  }

  assert {
    condition     = aws_dynamodb_table.chat_room_membership.tags["Environment"] == "dev"
    error_message = "ChatRoomMembership Environment tag must be dev"
  }

  assert {
    condition     = aws_dynamodb_table.chat_room_membership.tags["Project"] == "knotify"
    error_message = "ChatRoomMembership Project tag must be knotify"
  }
}

# ---------------------------------------------------------------------------
# Test 5: Dev safety flags — deletion_protection_enabled false,
# point_in_time_recovery disabled (module defaults for dev)
# Satisfies AC: deletion_protection_enabled and PITR variable-driven; false in dev
# ---------------------------------------------------------------------------
run "dev_safety_flags" {
  command = plan

  variables {
    environment                  = "dev"
    point_in_time_recovery_enabled = false
    deletion_protection_enabled  = false
  }

  assert {
    condition     = aws_dynamodb_table.chat_rooms.deletion_protection_enabled == false
    error_message = "ChatRooms deletion_protection_enabled must be false in dev"
  }

  assert {
    condition     = aws_dynamodb_table.chat_room_membership.deletion_protection_enabled == false
    error_message = "ChatRoomMembership deletion_protection_enabled must be false in dev"
  }

  assert {
    condition     = aws_dynamodb_table.chat_rooms.point_in_time_recovery[0].enabled == false
    error_message = "ChatRooms point_in_time_recovery must be disabled in dev"
  }

  assert {
    condition     = aws_dynamodb_table.chat_room_membership.point_in_time_recovery[0].enabled == false
    error_message = "ChatRoomMembership point_in_time_recovery must be disabled in dev"
  }
}

# ---------------------------------------------------------------------------
# Test 6: Prod safety flags — deletion_protection_enabled true,
# point_in_time_recovery enabled
# Satisfies AC: deletion_protection_enabled and PITR variable-driven; true in prod
# Note: lifecycle.prevent_destroy cannot reference variables (Terraform limitation),
# so native DynamoDB deletion_protection_enabled is the prod-safety mechanism per
# brainstorm finding #9.
# ---------------------------------------------------------------------------
run "prod_safety_flags" {
  command = plan

  variables {
    environment                  = "prod"
    point_in_time_recovery_enabled = true
    deletion_protection_enabled  = true
  }

  assert {
    condition     = aws_dynamodb_table.chat_rooms.deletion_protection_enabled == true
    error_message = "ChatRooms deletion_protection_enabled must be true in prod"
  }

  assert {
    condition     = aws_dynamodb_table.chat_room_membership.deletion_protection_enabled == true
    error_message = "ChatRoomMembership deletion_protection_enabled must be true in prod"
  }

  assert {
    condition     = aws_dynamodb_table.chat_rooms.point_in_time_recovery[0].enabled == true
    error_message = "ChatRooms point_in_time_recovery must be enabled in prod"
  }

  assert {
    condition     = aws_dynamodb_table.chat_room_membership.point_in_time_recovery[0].enabled == true
    error_message = "ChatRoomMembership point_in_time_recovery must be enabled in prod"
  }
}
