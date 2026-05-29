output "user_pool_id" {
  description = "ID of the Cognito User Pool"
  value       = aws_cognito_user_pool.this.id
}

output "user_pool_arn" {
  description = "ARN of the Cognito User Pool"
  value       = aws_cognito_user_pool.this.arn
}

output "user_pool_endpoint" {
  description = "Issuer URL for the Cognito User Pool — used as the JWT authorizer issuer in phase 5 (HTTP API) and as the AppSync auth mode issuer in phase 8. Format: https://cognito-idp.<region>.amazonaws.com/<user_pool_id>"
  value       = "https://cognito-idp.${data.aws_region.current.region}.amazonaws.com/${aws_cognito_user_pool.this.id}"
}
