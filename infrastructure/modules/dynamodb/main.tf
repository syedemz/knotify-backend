terraform {
  required_version = ">= 1.9.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.70"
    }
  }
}

# ---------------------------------------------------------------------------
# ChatRooms table
#
# PK: room_id (S) — deterministic SHA-256 of the canonical user-pair
#   (sha256(canonical_pair(userA, userB)), see architecture.md §5.4).
#   Storing user_a and user_b as plain attributes (written by the
#   application) lets resolvers verify participant membership in O(1)
#   without recomputing the hash.
#
# billing_mode: PAY_PER_REQUEST — chat volume is bursty and unpredictable;
#   on-demand pricing is appropriate for both dev and prod.
#
# deletion_protection_enabled: variable-driven (true in prod, false in dev).
#   Terraform's lifecycle.prevent_destroy cannot reference variables, so the
#   native DynamoDB flag is the prod-safety mechanism (brainstorm finding #9).
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "chat_rooms" {
  name         = "ChatRooms"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "room_id"

  attribute {
    name = "room_id"
    type = "S"
  }

  server_side_encryption {
    enabled = true
  }

  point_in_time_recovery {
    enabled = var.point_in_time_recovery_enabled
  }

  deletion_protection_enabled = var.deletion_protection_enabled

  tags = {
    Name        = "ChatRooms"
    Environment = var.environment
    Project     = var.project_name
  }
}

# ---------------------------------------------------------------------------
# ChatRoomMembership table
#
# PK: user_id (S), SK: room_id (S)
#
# Exactly 2 rows exist per room (one per participant).
# Keyed by user_id so "list all rooms for a user" is a simple Query.
# "Get my state for a specific room" is a single GetItem(user_id, room_id).
# "Authorize a subscription/mutation" is the same GetItem — if the item
# exists, the user is a participant (see architecture.md §5.4).
#
# No GSI is needed for "list participants of room" — the participants are
# derivable from ChatRooms.user_a and ChatRooms.user_b directly.
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "chat_room_membership" {
  name         = "ChatRoomMembership"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "user_id"
  range_key    = "room_id"

  attribute {
    name = "user_id"
    type = "S"
  }

  attribute {
    name = "room_id"
    type = "S"
  }

  server_side_encryption {
    enabled = true
  }

  point_in_time_recovery {
    enabled = var.point_in_time_recovery_enabled
  }

  deletion_protection_enabled = var.deletion_protection_enabled

  tags = {
    Name        = "ChatRoomMembership"
    Environment = var.environment
    Project     = var.project_name
  }
}

# ---------------------------------------------------------------------------
# ChatMessages table
#
# PK: room_id (S) — groups all messages in a conversation together.
# SK: created_at_message_id (S) — ISO-8601 timestamp prefixed sort key that
#   keeps messages in chronological order within a room, e.g.
#   "2024-01-15T12:34:56.789Z#<ulid>" (see architecture.md §5.4).
#
# stream_enabled: true with NEW_IMAGE — the DynamoDB stream feeds the
#   Phase 8 chat fan-out Lambda which broadcasts new messages to
#   connected WebSocket clients. Only the new image is needed to push
#   the message; old images are irrelevant and excluded to reduce
#   stream payload size.
#
# billing_mode: PAY_PER_REQUEST — same rationale as ChatRooms: bursty,
#   unpredictable traffic where on-demand pricing avoids over-provisioning.
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "chat_messages" {
  name         = "ChatMessages"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "room_id"
  range_key    = "created_at_message_id"

  attribute {
    name = "room_id"
    type = "S"
  }

  attribute {
    name = "created_at_message_id"
    type = "S"
  }

  stream_enabled   = true
  stream_view_type = "NEW_IMAGE"

  server_side_encryption {
    enabled = true
  }

  point_in_time_recovery {
    enabled = var.point_in_time_recovery_enabled
  }

  deletion_protection_enabled = var.deletion_protection_enabled

  tags = {
    Name        = "ChatMessages"
    Environment = var.environment
    Project     = var.project_name
  }
}

# ---------------------------------------------------------------------------
# MessageReads table
#
# PK: room_id (S), SK: user_id (S)
#
# Tracks the last-read cursor per user per room. A single PutItem
# (upsert) on each received message advances the read pointer; a
# Query(room_id, user_id) returns that user's read state for a given
# room, enabling unread-badge counts in the UI (see architecture.md §5.4).
#
# No stream needed: reads are transient state not requiring fan-out.
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "message_reads" {
  name         = "MessageReads"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "room_id"
  range_key    = "user_id"

  attribute {
    name = "room_id"
    type = "S"
  }

  attribute {
    name = "user_id"
    type = "S"
  }

  server_side_encryption {
    enabled = true
  }

  point_in_time_recovery {
    enabled = var.point_in_time_recovery_enabled
  }

  deletion_protection_enabled = var.deletion_protection_enabled

  tags = {
    Name        = "MessageReads"
    Environment = var.environment
    Project     = var.project_name
  }
}

# ---------------------------------------------------------------------------
# Notifications table
#
# PK: user_id (S) — partitions all notifications for a given recipient.
# SK: created_at_notification_id (S) — ISO-8601 timestamp prefixed sort key,
#   e.g. "2024-01-15T12:34:56.789Z#<ulid>", keeps notifications in
#   chronological order per user (see architecture.md §5.4).
#
# stream_enabled: true with NEW_IMAGE — the DynamoDB stream feeds the
#   Phase 8 push fan-out Lambda which delivers push notifications to
#   users with the app closed/backgrounded (see architecture.md §5.4.2).
#
# TTL: items expire after 90 days from creation. The application writes
#   the epoch-seconds expiry into the `ttl` attribute at insert time.
#
# billing_mode: PAY_PER_REQUEST — notification volume is bursty and
#   strongly correlated with user-growth events; on-demand pricing avoids
#   over-provisioning.
#
# deletion_protection_enabled: variable-driven (true in prod, false in dev).
#   Terraform's lifecycle.prevent_destroy cannot reference variables, so the
#   native DynamoDB flag is the prod-safety mechanism (brainstorm finding #9).
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "notifications" {
  name         = "Notifications"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "user_id"
  range_key    = "created_at_notification_id"

  attribute {
    name = "user_id"
    type = "S"
  }

  attribute {
    name = "created_at_notification_id"
    type = "S"
  }

  # GSI SK attribute — declared here so DynamoDB knows its type.
  # The attribute is written by the application only when read = false;
  # it is removed (via UpdateItem DELETE action) when the item flips to
  # read = true. Items missing this attribute are not indexed, giving a
  # sparse index over unread notifications only (see UnreadIndex block below).
  attribute {
    name = "notification_id"
    type = "S"
  }

  stream_enabled   = true
  stream_view_type = "NEW_IMAGE"

  server_side_encryption {
    enabled = true
  }

  point_in_time_recovery {
    enabled = var.point_in_time_recovery_enabled
  }

  deletion_protection_enabled = var.deletion_protection_enabled

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  # UnreadIndex is application-enforced sparse:
  # writers attach the `notification_id` attribute only when read=false, and
  # remove it when read flips true. DynamoDB doesn't index items missing the
  # GSI key attribute, which gives a sparse index over unread notifications
  # only. Terraform just declares the keys and projection — sparsity is a
  # write-pattern contract, not an attribute. See phase-2 brainstorm finding #10.
  global_secondary_index {
    name            = "UnreadIndex"
    hash_key        = "user_id"
    range_key       = "notification_id"
    projection_type = "ALL"
  }

  tags = {
    Name        = "Notifications"
    Environment = var.environment
    Project     = var.project_name
  }
}

# ---------------------------------------------------------------------------
# PushNotificationTokens table
#
# PK: user_id (S) — the Cognito/app user who owns the device token.
# SK: device_id (S) — opaque device identifier (e.g., UUID generated at
#   first app launch on a given physical device). Allows a single user to
#   hold multiple FCM/APNs tokens (one per device) and lets the application
#   delete a specific device's token on logout without invalidating others
#   (see architecture.md §5.4).
#
# No stream needed: token registrations do not require fan-out. The Phase 8
# push-fan-out Lambda reads tokens synchronously on demand when delivering
# a push notification triggered by the Notifications stream.
#
# billing_mode: PAY_PER_REQUEST — token writes are infrequent (app install /
#   token refresh), and reads are point lookups; on-demand pricing is correct.
#
# point_in_time_recovery: follows the same variable as the other tables for
#   consistency — disabled in dev, enabled in prod via var.point_in_time_recovery_enabled.
#
# deletion_protection_enabled: variable-driven (true in prod, false in dev).
#   Terraform's lifecycle.prevent_destroy cannot reference variables, so the
#   native DynamoDB flag is the prod-safety mechanism (brainstorm finding #9).
# ---------------------------------------------------------------------------

resource "aws_dynamodb_table" "push_notification_tokens" {
  name         = "PushNotificationTokens"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "user_id"
  range_key    = "device_id"

  attribute {
    name = "user_id"
    type = "S"
  }

  attribute {
    name = "device_id"
    type = "S"
  }

  server_side_encryption {
    enabled = true
  }

  point_in_time_recovery {
    enabled = var.point_in_time_recovery_enabled
  }

  deletion_protection_enabled = var.deletion_protection_enabled

  tags = {
    Name        = "PushNotificationTokens"
    Environment = var.environment
    Project     = var.project_name
  }
}
