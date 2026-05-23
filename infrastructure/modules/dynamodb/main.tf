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
