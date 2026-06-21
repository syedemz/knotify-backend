output "function_name" {
  description = "Name of the delete_dynamodb_personal_data Lambda function."
  value       = module.lambda.function_name
}

output "lambda_arn" {
  description = "ARN of the delete_dynamodb_personal_data Lambda live alias. Used as the delete_dynamodb_personal_data ARN in the step_functions module's lambda_arns input."
  value       = module.lambda.alias_arn
}
