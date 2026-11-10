variable "function_name" {
  description = "Name of the chat_resolver Lambda function."
  type        = string
}

variable "filename" {
  description = "Path to the chat_resolver deployment package .zip file."
  type        = string
}

variable "role_arn" {
  description = "ARN of the IAM execution role (chat_resolver) to assign to the Lambda function."
  type        = string
}

variable "layers" {
  description = "List of Lambda layer ARNs to attach (must include knotify_db and knotify_obs layers)."
  type        = list(string)
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
  description = "Friendly name of the app_user credential Secrets Manager secret."
  type        = string
}
