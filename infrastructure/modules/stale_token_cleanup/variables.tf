variable "environment" {
  description = "Deployment environment (dev or prod). Used in resource names."
  type        = string
}

variable "function_name" {
  description = "Name of the stale_token_cleanup Lambda function."
  type        = string
}

variable "filename" {
  description = "Path to the stale_token_cleanup deployment package .zip file."
  type        = string
}

variable "role_arn" {
  description = "ARN of the IAM execution role (stale_token_cleanup) to assign to the Lambda function."
  type        = string
}

variable "table_push_tokens_name" {
  description = "Name of the PushNotificationTokens DynamoDB table. Injected as TABLE_PUSH_TOKENS env var."
  type        = string
  default     = "PushNotificationTokens"
}
