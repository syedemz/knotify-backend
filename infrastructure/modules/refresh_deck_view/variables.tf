variable "environment" {
  description = "Deployment environment (dev or prod). Used in resource names."
  type        = string
}

variable "function_name" {
  description = "Name of the refresh_deck_view Lambda function."
  type        = string
}

variable "filename" {
  description = "Path to the deployment package .zip file."
  type        = string
}

variable "role_arn" {
  description = "ARN of the IAM execution role (aurora_refresh_lambda) to assign to the Lambda."
  type        = string
}

variable "layers" {
  description = "List of Lambda layer ARNs to attach (knotify_obs + knotify_db)."
  type        = list(string)
  default     = []
}

variable "vpc_config" {
  description = "VPC configuration for the Lambda function."
  type = object({
    subnet_ids         = list(string)
    security_group_ids = list(string)
  })
}

variable "db_secret_name" {
  description = "Friendly Secrets Manager secret name for the aurora_refresh credential (knotify-<env>-aurora-refresh-credential)."
  type        = string
}

variable "aurora_host" {
  description = "Aurora cluster endpoint hostname."
  type        = string
}

variable "aurora_port" {
  description = "Aurora cluster port (as string for env var injection)."
  type        = string
}

variable "aurora_dbname" {
  description = "Aurora database name."
  type        = string
}

variable "schedule_expression" {
  description = "EventBridge cron/rate expression for the scheduled refresh. Defaults to every 15 minutes."
  type        = string
  default     = "rate(15 minutes)"
}
