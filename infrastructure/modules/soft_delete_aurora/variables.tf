variable "function_name" {
  description = "Name of the soft_delete_aurora Lambda function."
  type        = string
}

variable "filename" {
  description = "Path to the soft_delete_aurora deployment package .zip file."
  type        = string
}

variable "role_arn" {
  description = "ARN of the IAM execution role (aurora_writer) to assign to the Lambda function. Must have VPC access (AWSLambdaVPCAccessExecutionRole) and Secrets Manager GetSecretValue on the Aurora app_user credential."
  type        = string
}

variable "layers" {
  description = "List of Lambda layer ARNs to attach (knotify_db + knotify_obs)."
  type        = list(string)
  default     = []
}

variable "vpc_config" {
  description = "VPC configuration for the Lambda function. Required — Aurora is VPC-private."
  type = object({
    subnet_ids         = list(string)
    security_group_ids = list(string)
  })
}

variable "db_secret_name" {
  description = "Friendly Secrets Manager secret name for the Aurora app_user credential (knotify-<env>-app-user-credential). Passed as DB_SECRET_NAME env var."
  type        = string
}

variable "aurora_host" {
  description = "Aurora cluster writer endpoint hostname. Passed as AURORA_HOST env var."
  type        = string
}

variable "aurora_port" {
  description = "Aurora cluster port (as string for env var injection). Passed as AURORA_PORT env var."
  type        = string
}

variable "aurora_dbname" {
  description = "Aurora database name. Passed as AURORA_DBNAME env var."
  type        = string
}

variable "refresh_lambda_arn" {
  description = "ARN of the refresh_deck_view Lambda (hotfix #6). Soft-delete commits a deleted_at marker on the users row, but deck_view (materialised) keeps the pre-delete snapshot until refreshed. The Lambda async-invokes the refresh after a successful soft-delete to clear the staleness immediately. Passed as REFRESH_LAMBDA_ARN env var; empty string disables the invoke."
  type        = string
  default     = ""
}
