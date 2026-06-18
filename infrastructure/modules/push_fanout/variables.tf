variable "environment" {
  description = "Deployment environment (dev or prod). Used in resource names."
  type        = string
}

variable "function_name" {
  description = "Name of the push_fanout Lambda function."
  type        = string
}

variable "filename" {
  description = "Path to the push_fanout deployment package .zip file."
  type        = string
}

variable "role_arn" {
  description = "ARN of the IAM execution role (push_fanout) to assign to the Lambda function."
  type        = string
}

variable "chat_messages_stream_arn" {
  description = "DynamoDB stream ARN for the ChatMessages table. Wired as the first event source for the Lambda."
  type        = string
}

variable "notifications_stream_arn" {
  description = "DynamoDB stream ARN for the Notifications table. Wired as the second event source for the Lambda."
  type        = string
}

variable "expo_push_url" {
  description = "HTTPS URL of the Expo Push API endpoint. Injected as EXPO_PUSH_URL env var so unit tests can target a mock."
  type        = string
  default     = "https://exp.host/--/api/v2/push/send"
}

variable "expo_auth_mode" {
  description = "Expo authentication mode. 'none' for dev (unauthenticated), 'bearer' for prod (Secrets Manager token)."
  type        = string
  default     = "none"

  validation {
    condition     = contains(["none", "bearer"], var.expo_auth_mode)
    error_message = "expo_auth_mode must be 'none' or 'bearer'."
  }
}
