output "function_name" {
  description = "Name of the Lambda function"
  value       = aws_lambda_function.this.function_name
}

output "function_arn" {
  description = "ARN of the Lambda function (unqualified — no version suffix)"
  value       = aws_lambda_function.this.arn
}

output "alias_arn" {
  description = "ARN of the live alias — use this in event source mappings and invoke permissions"
  value       = aws_lambda_alias.live.arn
}

output "invoke_arn" {
  description = "Invoke ARN for the live alias — used in API Gateway integrations"
  value       = aws_lambda_alias.live.invoke_arn
}

output "log_group_name" {
  description = "Name of the CloudWatch log group for this function"
  value       = aws_cloudwatch_log_group.this.name
}
