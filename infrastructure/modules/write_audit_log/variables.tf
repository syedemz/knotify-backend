variable "function_name" {
  description = "Name of the write_audit_log Lambda function."
  type        = string
}

variable "filename" {
  description = "Path to the write_audit_log deployment package .zip file."
  type        = string
}

variable "role_arn" {
  description = "ARN of the IAM execution role (write_audit_log) to assign to the Lambda function."
  type        = string
}

variable "audit_table_name" {
  description = "Name of the account_deletion_audit DynamoDB table. Injected as TABLE_AUDIT env var."
  type        = string
  default     = "account_deletion_audit"
}
