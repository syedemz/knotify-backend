output "function_name" {
  description = "Name of the write_audit_log Lambda function."
  value       = module.lambda.function_name
}

output "lambda_arn" {
  description = "ARN of the write_audit_log Lambda live alias. Used as the write_audit_log ARN in the step_functions module's lambda_arns input."
  value       = module.lambda.alias_arn
}
