output "lambda_arn" {
  description = "ARN of the chat_resolver Lambda live alias — consumed by the AppSync module in story 8.1 to register the Lambda data source."
  value       = module.lambda.alias_arn
}

output "function_name" {
  description = "Name of the chat_resolver Lambda function — used in aws_lambda_permission resources."
  value       = module.lambda.function_name
}

output "invoke_arn" {
  description = "Invoke ARN of the chat_resolver Lambda live alias — used in AppSync data source configuration."
  value       = module.lambda.invoke_arn
}
