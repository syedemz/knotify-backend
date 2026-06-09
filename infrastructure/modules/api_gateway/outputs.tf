output "api_id" {
  description = "ID of the HTTP API Gateway. Used to attach routes and integrations."
  value       = aws_apigatewayv2_api.this.id
}

output "api_arn" {
  description = "ARN of the HTTP API Gateway."
  value       = aws_apigatewayv2_api.this.arn
}

output "execute_api_endpoint" {
  description = "Default HTTPS endpoint for the HTTP API (the raw execute-api URL, before CloudFront). Format: https://<api-id>.execute-api.<region>.amazonaws.com. Used in story 5.7 smoke tests to verify edge-secret enforcement at the raw API level."
  value       = aws_apigatewayv2_api.this.api_endpoint
}

output "authorizer_id" {
  description = "ID of the Cognito JWT authorizer. Routes that require authentication reference this ID via authorization_type=JWT and authorizer_id=module.api_gateway.authorizer_id."
  value       = aws_apigatewayv2_authorizer.cognito_jwt.id
}

output "default_stage_arn" {
  description = "ARN of the $default stage. Used as a dependency anchor for IAM or CloudWatch policy attachments."
  value       = aws_apigatewayv2_stage.default.arn
}

output "access_log_group_name" {
  description = "Name of the CloudWatch log group that receives API Gateway access logs. Useful for wiring CloudWatch Logs Insights queries."
  value       = aws_cloudwatch_log_group.access_logs.name
}
