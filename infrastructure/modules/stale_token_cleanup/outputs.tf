output "function_name" {
  description = "Name of the stale_token_cleanup Lambda function."
  value       = module.lambda.function_name
}

output "lambda_arn" {
  description = "ARN of the stale_token_cleanup Lambda live alias."
  value       = module.lambda.alias_arn
}
