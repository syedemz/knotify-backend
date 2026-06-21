output "function_name" {
  description = "Name of the hard_purge Lambda function."
  value       = module.lambda.function_name
}

output "lambda_arn" {
  description = "ARN of the hard_purge Lambda live alias. Used as var.lambda_arns.hard_purge_now in the step_functions module."
  value       = module.lambda.alias_arn
}
