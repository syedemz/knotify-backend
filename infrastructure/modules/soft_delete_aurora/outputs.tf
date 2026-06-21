output "function_name" {
  description = "Name of the soft_delete_aurora Lambda function."
  value       = module.lambda.function_name
}

output "lambda_arn" {
  description = "ARN of the soft_delete_aurora Lambda live alias. Used as var.lambda_arns.soft_delete_aurora in the step_functions module."
  value       = module.lambda.alias_arn
}
