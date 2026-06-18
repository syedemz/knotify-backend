output "function_name" {
  description = "Name of the push_tokens Lambda function. Used in aws_lambda_permission."
  value       = module.lambda.function_name
}

output "invoke_arn" {
  description = "Invoke ARN of the push_tokens Lambda live alias. Used in aws_apigatewayv2_integration.push_tokens.integration_uri."
  value       = module.lambda.invoke_arn
}

output "lambda_arn" {
  description = "ARN of the push_tokens Lambda live alias. Used as a qualifier in aws_lambda_permission.push_tokens_api_gateway."
  value       = module.lambda.alias_arn
}
