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

# ---------------------------------------------------------------------------
# Test 7: ChatMessages keys and billing mode
# Satisfies AC: PK room_id (S), SK created_at_message_id (S), PAY_PER_REQUEST
# ---------------------------------------------------------------------------
run "chat_messages_keys_and_billing" {
  command = plan

  variables {
    environment                    = "dev"
    point_in_time_recovery_enabled = false
    deletion_protection_enabled    = false
  }

  assert {
    condition     = aws_dynamodb_table.chat_messages.billing_mode == "PAY_PER_REQUEST"
    error_message = "ChatMessages billing_mode must be PAY_PER_REQUEST"
  }

  assert {
    condition     = aws_dynamodb_table.chat_messages.hash_key == "room_id"
    error_message = "ChatMessages hash_key must be room_id"
  }

  assert {
    condition     = aws_dynamodb_table.chat_messages.range_key == "created_at_message_id"
    error_message = "ChatMessages range_key must be created_at_message_id"
  }

  assert {
    condition = anytrue([
      for attr in aws_dynamodb_table.chat_messages.attribute : attr.name == "room_id" && attr.type == "S"
    ])
    error_message = "ChatMessages must define attribute room_id of type S"
  }

  assert {
    condition = anytrue([
      for attr in aws_dynamodb_table.chat_messages.attribute : attr.name == "created_at_message_id" && attr.type == "S"
    ])
    error_message = "ChatMessages must define attribute created_at_message_id of type S"
  }
}

# ---------------------------------------------------------------------------
# Test 8: ChatMessages stream enabled with NEW_IMAGE view type
# Satisfies AC: stream_enabled true, stream_view_type NEW_IMAGE
# ---------------------------------------------------------------------------
run "chat_messages_stream_enabled" {
  command = plan

  variables {
    environment                    = "dev"
    point_in_time_recovery_enabled = false
    deletion_protection_enabled    = false
  }

  assert {
    condition     = aws_dynamodb_table.chat_messages.stream_enabled == true
    error_message = "ChatMessages stream_enabled must be true"
  }

  assert {
    condition     = aws_dynamodb_table.chat_messages.stream_view_type == "NEW_IMAGE"
    error_message = "ChatMessages stream_view_type must be NEW_IMAGE"
  }
}

# ---------------------------------------------------------------------------
# Test 9: MessageReads keys and billing mode
# Satisfies AC: PK room_id (S), SK user_id (S), PAY_PER_REQUEST
# ---------------------------------------------------------------------------
run "message_reads_keys_and_billing" {
  command = plan

  variables {
    environment                    = "dev"
    point_in_time_recovery_enabled = false
    deletion_protection_enabled    = false
  }

  assert {
    condition     = aws_dynamodb_table.message_reads.billing_mode == "PAY_PER_REQUEST"
    error_message = "MessageReads billing_mode must be PAY_PER_REQUEST"
  }

  assert {
    condition     = aws_dynamodb_table.message_reads.hash_key == "room_id"
    error_message = "MessageReads hash_key must be room_id"
  }

  assert {
    condition     = aws_dynamodb_table.message_reads.range_key == "user_id"
    error_message = "MessageReads range_key must be user_id"
  }

  assert {
    condition = anytrue([
      for attr in aws_dynamodb_table.message_reads.attribute : attr.name == "room_id" && attr.type == "S"
    ])
    error_message = "MessageReads must define attribute room_id of type S"
  }

  assert {
    condition = anytrue([
      for attr in aws_dynamodb_table.message_reads.attribute : attr.name == "user_id" && attr.type == "S"
    ])
    error_message = "MessageReads must define attribute user_id of type S"
  }
}

# ---------------------------------------------------------------------------
# Test 10: SSE enabled on ChatMessages and MessageReads
# Satisfies AC: server_side_encryption enabled on both new tables
# ---------------------------------------------------------------------------
run "new_tables_server_side_encryption" {
  command = plan

  variables {
    environment                    = "dev"
    point_in_time_recovery_enabled = false
    deletion_protection_enabled    = false
  }

  assert {
    condition     = aws_dynamodb_table.chat_messages.server_side_encryption[0].enabled == true
    error_message = "ChatMessages server_side_encryption must be enabled"
  }

  assert {
    condition     = aws_dynamodb_table.message_reads.server_side_encryption[0].enabled == true
    error_message = "MessageReads server_side_encryption must be enabled"
  }
}

# ---------------------------------------------------------------------------
# Test 11: Dev safety flags on ChatMessages and MessageReads
# Satisfies AC: deletion_protection_enabled and PITR false in dev
# ---------------------------------------------------------------------------
run "new_tables_dev_safety_flags" {
  command = plan

  variables {
    environment                    = "dev"
    point_in_time_recovery_enabled = false
    deletion_protection_enabled    = false
  }

  assert {
    condition     = aws_dynamodb_table.chat_messages.deletion_protection_enabled == false
    error_message = "ChatMessages deletion_protection_enabled must be false in dev"
  }

  assert {
    condition     = aws_dynamodb_table.message_reads.deletion_protection_enabled == false
    error_message = "MessageReads deletion_protection_enabled must be false in dev"
  }

  assert {
    condition     = aws_dynamodb_table.chat_messages.point_in_time_recovery[0].enabled == false
    error_message = "ChatMessages point_in_time_recovery must be disabled in dev"
  }

  assert {
    condition     = aws_dynamodb_table.message_reads.point_in_time_recovery[0].enabled == false
    error_message = "MessageReads point_in_time_recovery must be disabled in dev"
  }
}

# ---------------------------------------------------------------------------
# Test 12: Prod safety flags on ChatMessages and MessageReads
# Satisfies AC: deletion_protection_enabled and PITR true in prod
# ---------------------------------------------------------------------------
run "new_tables_prod_safety_flags" {
  command = plan

  variables {
    environment                    = "prod"
    point_in_time_recovery_enabled = true
    deletion_protection_enabled    = true
  }

  assert {
    condition     = aws_dynamodb_table.chat_messages.deletion_protection_enabled == true
    error_message = "ChatMessages deletion_protection_enabled must be true in prod"
  }

  assert {
    condition     = aws_dynamodb_table.message_reads.deletion_protection_enabled == true
    error_message = "MessageReads deletion_protection_enabled must be true in prod"
  }

  assert {
    condition     = aws_dynamodb_table.chat_messages.point_in_time_recovery[0].enabled == true
    error_message = "ChatMessages point_in_time_recovery must be enabled in prod"
  }

  assert {
    condition     = aws_dynamodb_table.message_reads.point_in_time_recovery[0].enabled == true
    error_message = "MessageReads point_in_time_recovery must be enabled in prod"
  }
}

# ---------------------------------------------------------------------------
# Test 13: Tags on ChatMessages and MessageReads (dev values)
# Satisfies AC: tags for Environment and Project on both new tables
# ---------------------------------------------------------------------------
run "new_tables_tags" {
  command = plan

  variables {
    environment                    = "dev"
    point_in_time_recovery_enabled = false
    deletion_protection_enabled    = false
  }

  assert {
    condition     = aws_dynamodb_table.chat_messages.tags["Environment"] == "dev"
    error_message = "ChatMessages Environment tag must be dev"
  }

  assert {
    condition     = aws_dynamodb_table.chat_messages.tags["Project"] == "knotify"
    error_message = "ChatMessages Project tag must be knotify"
  }

  assert {
    condition     = aws_dynamodb_table.message_reads.tags["Environment"] == "dev"
    error_message = "MessageReads Environment tag must be dev"
  }

  assert {
    condition     = aws_dynamodb_table.message_reads.tags["Project"] == "knotify"
    error_message = "MessageReads Project tag must be knotify"
  }
}

# ---------------------------------------------------------------------------
# Test 14: Notifications hash key, range key, and billing mode
# Satisfies AC: PK user_id (S), SK created_at_notification_id (S),
#               billing_mode PAY_PER_REQUEST
# ---------------------------------------------------------------------------
run "notifications_keys_and_billing" {
  command = plan

  variables {
    environment                    = "dev"
    point_in_time_recovery_enabled = false
    deletion_protection_enabled    = false
  }

  assert {
    condition     = aws_dynamodb_table.notifications.billing_mode == "PAY_PER_REQUEST"
    error_message = "Notifications billing_mode must be PAY_PER_REQUEST"
  }

  assert {
    condition     = aws_dynamodb_table.notifications.hash_key == "user_id"
    error_message = "Notifications hash_key must be user_id"
  }

  assert {
    condition     = aws_dynamodb_table.notifications.range_key == "created_at_notification_id"
    error_message = "Notifications range_key must be created_at_notification_id"
  }

  assert {
    condition = anytrue([
      for attr in aws_dynamodb_table.notifications.attribute : attr.name == "user_id" && attr.type == "S"
    ])
    error_message = "Notifications must define attribute user_id of type S"
  }

  assert {
    condition = anytrue([
      for attr in aws_dynamodb_table.notifications.attribute : attr.name == "created_at_notification_id" && attr.type == "S"
    ])
    error_message = "Notifications must define attribute created_at_notification_id of type S"
  }
}

# ---------------------------------------------------------------------------
# Test 15: Notifications stream enabled with NEW_IMAGE view type
# Satisfies AC: stream_enabled true, stream_view_type NEW_IMAGE
# ---------------------------------------------------------------------------
run "notifications_stream_enabled" {
  command = plan

  variables {
    environment                    = "dev"
    point_in_time_recovery_enabled = false
    deletion_protection_enabled    = false
  }

  assert {
    condition     = aws_dynamodb_table.notifications.stream_enabled == true
    error_message = "Notifications stream_enabled must be true"
  }

  assert {
    condition     = aws_dynamodb_table.notifications.stream_view_type == "NEW_IMAGE"
    error_message = "Notifications stream_view_type must be NEW_IMAGE"
  }
}

# ---------------------------------------------------------------------------
# Test 16: Notifications server-side encryption enabled
# Satisfies AC: server_side_encryption enabled
# ---------------------------------------------------------------------------
run "notifications_server_side_encryption" {
  command = plan

  variables {
    environment                    = "dev"
    point_in_time_recovery_enabled = false
    deletion_protection_enabled    = false
  }

  assert {
    condition     = aws_dynamodb_table.notifications.server_side_encryption[0].enabled == true
    error_message = "Notifications server_side_encryption must be enabled"
  }
}

# ---------------------------------------------------------------------------
# Test 17: Notifications TTL enabled on attribute "ttl"
# Satisfies AC: ttl block with attribute_name "ttl" and enabled true
# ---------------------------------------------------------------------------
run "notifications_ttl_enabled" {
  command = plan

  variables {
    environment                    = "dev"
    point_in_time_recovery_enabled = false
    deletion_protection_enabled    = false
  }

  assert {
    condition     = aws_dynamodb_table.notifications.ttl[0].attribute_name == "ttl"
    error_message = "Notifications TTL attribute_name must be 'ttl'"
  }

  assert {
    condition     = aws_dynamodb_table.notifications.ttl[0].enabled == true
    error_message = "Notifications TTL enabled must be true"
  }
}

# ---------------------------------------------------------------------------
# Test 18: Notifications UnreadIndex GSI keys
# Satisfies AC: GSI named UnreadIndex with PK user_id and SK notification_id
# ---------------------------------------------------------------------------
run "notifications_unread_index_gsi" {
  command = plan

  variables {
    environment                    = "dev"
    point_in_time_recovery_enabled = false
    deletion_protection_enabled    = false
  }

  assert {
    condition = anytrue([
      for gsi in aws_dynamodb_table.notifications.global_secondary_index :
        gsi.name == "UnreadIndex" && gsi.hash_key == "user_id" && gsi.range_key == "notification_id"
    ])
    error_message = "Notifications must have UnreadIndex GSI with PK user_id and SK notification_id"
  }

  assert {
    condition = anytrue([
      for attr in aws_dynamodb_table.notifications.attribute : attr.name == "notification_id" && attr.type == "S"
    ])
    error_message = "Notifications must define attribute notification_id of type S for the UnreadIndex GSI SK"
  }
}

# ---------------------------------------------------------------------------
# Test 19: Notifications dev safety flags — deletion_protection false, PITR disabled
# Satisfies AC: deletion_protection_enabled and PITR variable-driven; false in dev
# ---------------------------------------------------------------------------
run "notifications_dev_safety_flags" {
  command = plan

  variables {
    environment                    = "dev"
    point_in_time_recovery_enabled = false
    deletion_protection_enabled    = false
  }

  assert {
    condition     = aws_dynamodb_table.notifications.deletion_protection_enabled == false
    error_message = "Notifications deletion_protection_enabled must be false in dev"
  }

  assert {
    condition     = aws_dynamodb_table.notifications.point_in_time_recovery[0].enabled == false
    error_message = "Notifications point_in_time_recovery must be disabled in dev"
  }
}

# ---------------------------------------------------------------------------
# Test 20: Notifications prod safety flags — deletion_protection true, PITR enabled
# Satisfies AC: deletion_protection_enabled and PITR variable-driven; true in prod
# ---------------------------------------------------------------------------
run "notifications_prod_safety_flags" {
  command = plan

  variables {
    environment                    = "prod"
    point_in_time_recovery_enabled = true
    deletion_protection_enabled    = true
  }

  assert {
    condition     = aws_dynamodb_table.notifications.deletion_protection_enabled == true
    error_message = "Notifications deletion_protection_enabled must be true in prod"
  }

  assert {
    condition     = aws_dynamodb_table.notifications.point_in_time_recovery[0].enabled == true
    error_message = "Notifications point_in_time_recovery must be enabled in prod"
  }
}

# ---------------------------------------------------------------------------
# Test 21: Notifications tags (dev values)
# Satisfies AC: tags for Environment and Project
# ---------------------------------------------------------------------------
run "notifications_tags" {
  command = plan

  variables {
    environment                    = "dev"
    point_in_time_recovery_enabled = false
    deletion_protection_enabled    = false
  }

  assert {
    condition     = aws_dynamodb_table.notifications.tags["Environment"] == "dev"
    error_message = "Notifications Environment tag must be dev"
  }

  assert {
    condition     = aws_dynamodb_table.notifications.tags["Project"] == "knotify"
    error_message = "Notifications Project tag must be knotify"
  }
}
