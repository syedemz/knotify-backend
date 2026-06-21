variable "function_name" {
  description = "Name of the deactivate_chat_rooms Lambda function."
  type        = string
}

variable "filename" {
  description = "Path to the deactivate_chat_rooms deployment package .zip file."
  type        = string
}

variable "role_arn" {
  description = "ARN of the IAM execution role (deactivate_chat_rooms) to assign to the Lambda function."
  type        = string
}

variable "chat_rooms_table_name" {
  description = "Name of the ChatRooms DynamoDB table. Injected as TABLE_CHAT_ROOMS env var."
  type        = string
  default     = "ChatRooms"
}

variable "chat_room_membership_table_name" {
  description = "Name of the ChatRoomMembership DynamoDB table. Injected as TABLE_CHAT_ROOM_MEMBERSHIP env var."
  type        = string
  default     = "ChatRoomMembership"
}
