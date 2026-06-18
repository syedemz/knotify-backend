variable "environment" {
  description = "Deployment environment (dev or prod). Used in resource names."
  type        = string
}

variable "function_name" {
  description = "Name of the push_tokens Lambda function."
  type        = string
}

variable "filename" {
  description = "Path to the push_tokens deployment package .zip file."
  type        = string
}

variable "role_arn" {
  description = "ARN of the IAM execution role (push_tokens) to assign to the Lambda function."
  type        = string
}

variable "table_push_tokens_name" {
  description = "Name of the PushNotificationTokens DynamoDB table. Injected as TABLE_PUSH_TOKENS env var."
  type        = string
  default     = "PushNotificationTokens"
}

variable "layers" {
  description = "List of Lambda layer ARNs to attach (knotify_obs layer required for init_logger)."
  type        = list(string)
  default     = []
}

variable "vpc_config" {
  description = "VPC configuration for the Lambda function. Set to null to run outside a VPC."
  type = object({
    subnet_ids         = list(string)
    security_group_ids = list(string)
  })
  default = null
}
