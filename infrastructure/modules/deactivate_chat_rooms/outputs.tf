output "function_name" {
  description = "Name of the deactivate_chat_rooms Lambda function."
  value       = module.lambda.function_name
}

output "lambda_arn" {
  description = "ARN of the deactivate_chat_rooms Lambda live alias. Used as the deactivate_chat_rooms ARN in the step_functions module's lambda_arns input."
  value       = module.lambda.alias_arn
}
