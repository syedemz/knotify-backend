variable "environment" {
  description = "Deployment environment (dev or prod). Used in resource names and ARN patterns."
  type        = string
}

variable "aurora_master_user_secret_arn" {
  description = "Exact ARN of the Aurora-managed master user secret in Secrets Manager (i.e. aws_rds_cluster.master_user_secret[0].secret_arn). Used to scope db_migrator's GetSecretValue permission to that specific secret. The internal cluster id RDS embeds in this ARN is NOT the Terraform-visible cluster_resource_id, so the ARN must be plumbed through from the aurora module rather than reconstructed."
  type        = string
}

variable "cognito_user_pool_arn" {
  description = "ARN of the Cognito User Pool. Used to scope aurora_writer's cognito-idp:AdminUpdateUserAttributes permission (story 7.0b). Pass module.cognito.user_pool_arn from each environment root module."
  type        = string
  default     = ""
}

variable "refresh_lambda_arn" {
  description = "ARN of the refresh_deck_view Lambda function. Used to scope aurora_writer's lambda:InvokeFunction permission (story 7.4) and to scope aurora_refresh_lambda's db_migrator credential access. Pass module.refresh_deck_view.function_arn from each environment root module."
  type        = string
  default     = ""
}
