output "chat_rooms_table_name" {
  description = "Name of the ChatRooms DynamoDB table"
  value       = aws_dynamodb_table.chat_rooms.name
}

output "chat_rooms_arn" {
  description = "ARN of the ChatRooms DynamoDB table — consumed by IAM policies in stories 8.0, 8.9, 8.9b"
  value       = aws_dynamodb_table.chat_rooms.arn
}

output "chat_rooms_stream_arn" {
  description = "DynamoDB stream ARN for the ChatRooms table — consumed by the room_state_publisher Lambda (story 8.9a). Stream view type is NEW_AND_OLD_IMAGES so the publisher can detect status transitions."
  value       = aws_dynamodb_table.chat_rooms.stream_arn
}

output "chat_room_membership_table_name" {
  description = "Name of the ChatRoomMembership DynamoDB table"
  value       = aws_dynamodb_table.chat_room_membership.name
}

output "chat_room_membership_arn" {
  description = "ARN of the ChatRoomMembership DynamoDB table — consumed by IAM policies in stories 8.0, 8.9, 8.9b"
  value       = aws_dynamodb_table.chat_room_membership.arn
}

output "chat_messages_table_name" {
  description = "Name of the ChatMessages DynamoDB table"
  value       = aws_dynamodb_table.chat_messages.name
}

output "chat_messages_arn" {
  description = "ARN of the ChatMessages DynamoDB table — consumed by IAM policies in stories 8.0, 8.10"
  value       = aws_dynamodb_table.chat_messages.arn
}

output "message_reads_table_name" {
  description = "Name of the MessageReads DynamoDB table"
  value       = aws_dynamodb_table.message_reads.name
}

output "message_reads_arn" {
  description = "ARN of the MessageReads DynamoDB table — consumed by IAM policies in stories 8.0, 8.7"
  value       = aws_dynamodb_table.message_reads.arn
}

output "chat_messages_stream_arn" {
  description = "DynamoDB stream ARN for the ChatMessages table, consumed by the Phase 8 chat fan-out Lambda"
  value       = aws_dynamodb_table.chat_messages.stream_arn
}

output "notifications_table_name" {
  description = "Name of the Notifications DynamoDB table"
  value       = aws_dynamodb_table.notifications.name
}

output "notifications_arn" {
  description = "ARN of the Notifications DynamoDB table — consumed by IAM policies in stories 8.0, 8.9c, 8.10, 8.12"
  value       = aws_dynamodb_table.notifications.arn
}

output "notifications_stream_arn" {
  description = "DynamoDB stream ARN for the Notifications table, consumed by the Phase 8 push fan-out Lambda"
  value       = aws_dynamodb_table.notifications.stream_arn
}

output "push_tokens_table_name" {
  description = "Name of the PushNotificationTokens DynamoDB table"
  value       = aws_dynamodb_table.push_notification_tokens.name
}

output "push_notification_tokens_arn" {
  description = "ARN of the PushNotificationTokens DynamoDB table — consumed by IAM policies in stories 8.10, 8.11, 8.12"
  value       = aws_dynamodb_table.push_notification_tokens.arn
}

output "account_deletion_audit_arn" {
  description = "ARN of the account_deletion_audit DynamoDB table — consumed by the write_audit_log IAM role (story 9.8)"
  value       = aws_dynamodb_table.account_deletion_audit.arn
}

output "account_deletion_audit_table_name" {
  description = "Name of the account_deletion_audit DynamoDB table"
  value       = aws_dynamodb_table.account_deletion_audit.name
}
