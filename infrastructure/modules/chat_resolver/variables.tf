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
  description = "VPC configuration for the Lambda function. The chat_resolver runs inside the VPC to reach Aurora."
  type = object({
    subnet_ids         = list(string)
    security_group_ids = list(string)
  })
}

variable "aurora_host" {
  description = "Aurora cluster endpoint hostname."
  type        = string
}

variable "aurora_port" {
  description = "Aurora cluster port (string for env-var injection)."
  type        = string
}

variable "aurora_dbname" {
  description = "Aurora database name."
  type        = string
}

variable "db_secret_name" {
  description = "Friendly name of the app_user credential Secrets Manager secret."
  type        = string
}
