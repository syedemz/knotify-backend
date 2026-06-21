variable "function_name" {
  description = "Name of the cognito_user_state Lambda function."
  type        = string
}

variable "filename" {
  description = "Path to the cognito_user_state deployment package .zip file."
  type        = string
}

variable "role_arn" {
  description = "ARN of the IAM execution role (cognito_user_state) to assign to the Lambda function."
  type        = string
}

variable "user_pool_id" {
  description = "Cognito User Pool ID. Injected as USER_POOL_ID env var so the handler knows which pool to target. Pass module.cognito.user_pool_id from each environment root module."
  type        = string
  default     = ""
}
