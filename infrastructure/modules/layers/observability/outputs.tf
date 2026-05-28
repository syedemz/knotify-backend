output "layer_arn" {
  description = "ARN of the published observability layer version — pass this to the lambda module's layers input"
  value       = aws_lambda_layer_version.observability.arn
}

output "layer_version" {
  description = "Published version number of the observability layer"
  value       = aws_lambda_layer_version.observability.version
}
