output "layer_arn" {
  description = "ARN of the published db layer version — pass this to the lambda module's layers input"
  value       = aws_lambda_layer_version.db.arn
}

output "layer_version" {
  description = "Published version number of the db layer"
  value       = aws_lambda_layer_version.db.version
}
