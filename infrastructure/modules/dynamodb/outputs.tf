output "chat_rooms_table_name" {
  description = "Name of the ChatRooms DynamoDB table"
  value       = aws_dynamodb_table.chat_rooms.name
}

output "chat_room_membership_table_name" {
  description = "Name of the ChatRoomMembership DynamoDB table"
  value       = aws_dynamodb_table.chat_room_membership.name
}

output "chat_messages_table_name" {
  description = "Name of the ChatMessages DynamoDB table"
  value       = aws_dynamodb_table.chat_messages.name
}

output "message_reads_table_name" {
  description = "Name of the MessageReads DynamoDB table"
  value       = aws_dynamodb_table.message_reads.name
}

output "chat_messages_stream_arn" {
  description = "DynamoDB stream ARN for the ChatMessages table, consumed by the Phase 8 chat fan-out Lambda"
  value       = aws_dynamodb_table.chat_messages.stream_arn
}

output "notifications_table_name" {
  description = "Name of the Notifications DynamoDB table"
  value       = aws_dynamodb_table.notifications.name
}

output "notifications_stream_arn" {
  description = "DynamoDB stream ARN for the Notifications table, consumed by the Phase 8 push fan-out Lambda"
  value       = aws_dynamodb_table.notifications.stream_arn
}

output "push_tokens_table_name" {
  description = "Name of the PushNotificationTokens DynamoDB table"
  value       = aws_dynamodb_table.push_notification_tokens.name
}
