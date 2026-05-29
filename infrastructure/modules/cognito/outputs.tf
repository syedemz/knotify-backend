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

output "app_client_id" {
  description = "Client ID of the production SRP-only app client. Used by the React Native app via aws-amplify."
  value       = aws_cognito_user_pool_client.app.id
}

output "integration_test_app_client_id" {
  description = "Client ID of the dev-only integration-test app client (ADMIN_USER_PASSWORD_AUTH). Empty string in prod — this client is not created outside of the dev environment. The id is not a credential; this output is non-sensitive."
  value       = try(aws_cognito_user_pool_client.integration_test[0].id, "")
}
