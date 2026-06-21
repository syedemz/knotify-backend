output "function_name" {
  description = "Name of the anonymize_chat_messages Lambda function."
  value       = module.lambda.function_name
}

output "lambda_arn" {
  description = "ARN of the anonymize_chat_messages Lambda live alias. Used as the anonymize_chat_messages ARN in the step_functions module's lambda_arns input."
  value       = module.lambda.alias_arn
}
