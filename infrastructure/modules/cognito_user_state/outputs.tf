output "function_name" {
  description = "Name of the cognito_user_state Lambda function."
  value       = module.lambda.function_name
}

output "lambda_arn" {
  description = "ARN of the cognito_user_state Lambda live alias. Used as the cognito_user_state ARN in the step_functions module's lambda_arns input."
  value       = module.lambda.alias_arn
}
