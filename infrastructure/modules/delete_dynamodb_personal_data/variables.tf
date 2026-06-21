variable "function_name" {
  description = "Name of the delete_dynamodb_personal_data Lambda function."
  type        = string
}

variable "filename" {
  description = "Path to the delete_dynamodb_personal_data deployment package .zip file."
  type        = string
}

variable "role_arn" {
  description = "ARN of the IAM execution role (delete_dynamodb_personal_data) to assign to the Lambda function."
  type        = string
}

variable "notifications_table_name" {
  description = "Name of the Notifications DynamoDB table. Injected as TABLE_NOTIFICATIONS env var."
  type        = string
  default     = "Notifications"
}

variable "push_notification_tokens_table_name" {
  description = "Name of the PushNotificationTokens DynamoDB table. Injected as TABLE_PUSH_NOTIFICATION_TOKENS env var."
  type        = string
  default     = "PushNotificationTokens"
}
