output "chat_rooms_table_name" {
  description = "Name of the ChatRooms DynamoDB table"
  value       = aws_dynamodb_table.chat_rooms.name
}

output "chat_room_membership_table_name" {
  description = "Name of the ChatRoomMembership DynamoDB table"
  value       = aws_dynamodb_table.chat_room_membership.name
}
