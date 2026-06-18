output "function_name" {
  description = "Name of the push_fanout Lambda function."
  value       = module.lambda.function_name
}

output "lambda_arn" {
  description = "ARN of the push_fanout Lambda live alias — used in event source mappings."
  value       = module.lambda.alias_arn
}
