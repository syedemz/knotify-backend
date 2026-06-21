output "function_name" {
  description = "Name of the hard_delete_user_chat_messages Lambda function."
  value       = module.lambda.function_name
}

output "lambda_arn" {
  description = "ARN of the hard_delete_user_chat_messages Lambda live alias. Used as the hard_delete_user_chat_msgs ARN in the step_functions module's lambda_arns input."
  value       = module.lambda.alias_arn
}
