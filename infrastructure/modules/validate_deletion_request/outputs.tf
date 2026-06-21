output "function_name" {
  description = "Name of the validate_deletion_request Lambda function."
  value       = module.lambda.function_name
}

output "lambda_arn" {
  description = "ARN of the validate_deletion_request Lambda live alias. Used as the validate_deletion_request ARN in the step_functions module's lambda_arns input."
  value       = module.lambda.alias_arn
}
