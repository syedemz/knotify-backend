variable "function_name" {
  description = "Name of the hard_delete_user_chat_messages Lambda function."
  type        = string
}

variable "filename" {
  description = "Path to the hard_delete_user_chat_messages deployment package .zip file."
  type        = string
}

variable "role_arn" {
  description = "ARN of the IAM execution role (hard_delete_user_chat_messages) to assign to the Lambda function."
  type        = string
}

variable "chat_messages_table_name" {
  description = "Name of the ChatMessages DynamoDB table. Injected as TABLE_CHAT_MESSAGES env var."
  type        = string
  default     = "ChatMessages"
}
