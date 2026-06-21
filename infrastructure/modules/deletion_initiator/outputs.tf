output "function_name" {
  description = "Name of the deletion_initiator Lambda function. Used to scope the aws_lambda_permission for the DELETE /v1/profile/me route."
  value       = module.lambda.function_name
}

output "invoke_arn" {
  description = "Invoke ARN of the deletion_initiator Lambda live alias. Used as integration_uri in the aws_apigatewayv2_integration for DELETE /v1/profile/me."
  value       = module.lambda.invoke_arn
}

output "lambda_arn" {
  description = "ARN of the deletion_initiator Lambda live alias. Exposed for use in the stepfn_deletion_exec IAM role's lambda:InvokeFunction scope if needed."
  value       = module.lambda.alias_arn
}
