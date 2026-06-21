variable "function_name" {
  description = "Name of the validate_deletion_request Lambda function."
  type        = string
}

variable "filename" {
  description = "Path to the validate_deletion_request deployment package .zip file."
  type        = string
}

variable "role_arn" {
  description = "ARN of the IAM execution role (validate_deletion_request) to assign to the Lambda function."
  type        = string
}

variable "audit_table_name" {
  description = "Name of the account_deletion_audit DynamoDB table. Injected as TABLE_AUDIT env var."
  type        = string
  default     = "account_deletion_audit"
}
