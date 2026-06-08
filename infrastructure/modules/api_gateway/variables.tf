variable "name" {
  description = "Name prefix for all resources in this module (e.g. 'knotify-dev-api')."
  type        = string
}

variable "cognito_user_pool_endpoint" {
  description = "Issuer URL of the Cognito User Pool used by the JWT authorizer. Format: https://cognito-idp.<region>.amazonaws.com/<user_pool_id>. Supplied via module.cognito.user_pool_endpoint in the env-level call."
  type        = string
}

variable "cognito_audience_client_ids" {
  description = "List of Cognito app client IDs that the JWT authorizer will accept. Build this list with compact([module.cognito.app_client_id, module.cognito.integration_test_app_client_id]) at the env level so that dev receives both IDs and prod receives only the production client (the integration-test output is \"\" in prod, which compact drops)."
  type        = list(string)
}

variable "throttling_burst_limit" {
  description = "Maximum number of concurrent requests allowed by the API Gateway default stage. Dev default: 10 (pre-launch). Prod recommended: 500."
  type        = number
  default     = 10
}

variable "throttling_rate_limit" {
  description = "Maximum steady-state request rate (requests per second) for the API Gateway default stage. Dev default: 25 (pre-launch). Prod recommended: 1000."
  type        = number
  default     = 25
}

variable "access_log_retention_days" {
  description = "Number of days to retain API Gateway access logs in CloudWatch. Matches the project-wide convention from architecture.md §10.6."
  type        = number
  default     = 7
}
