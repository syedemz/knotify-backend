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
