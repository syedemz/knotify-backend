output "function_name" {
  description = "Name of the room_state_publisher Lambda function."
  value       = module.lambda.function_name
}

output "lambda_arn" {
  description = "ARN of the room_state_publisher Lambda live alias — used in event source mapping and any future invoke permissions."
  value       = module.lambda.alias_arn
}
