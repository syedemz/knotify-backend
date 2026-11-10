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
  description = "VPC configuration for the Lambda function. Set to null when use_supabase=true (Lambda runs outside the VPC). Set to the networking module's subnet_ids and security_group_ids otherwise."
  type = object({
    subnet_ids         = list(string)
    security_group_ids = list(string)
  })
  default = null
}

variable "db_env_vars" {
  description = "DB-side environment variables sourced from the env root's local.db_env_vars. Carries USE_SUPABASE + either SUPABASE_* or AURORA_* keys + DB_SECRET_ARN depending on the use_supabase flag."
  type        = map(string)
}

variable "db_secret_name" {
  description = "Friendly Secrets Manager secret name for the aurora_refresh credential (knotify-<env>-aurora-refresh-credential)."
  type        = string
}

variable "schedule_expression" {
  description = "EventBridge cron/rate expression for the scheduled refresh. Defaults to every 15 minutes."
  type        = string
  default     = "rate(15 minutes)"
}
