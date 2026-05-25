output "vpc_id" {
  description = "ID of the VPC"
  value       = aws_vpc.this.id
}

output "private_subnet_ids" {
  description = "List of private subnet IDs (Lambda placement)"
  value       = aws_subnet.private[*].id
}

output "db_subnet_group_name" {
  description = "Name of the Aurora DB subnet group"
  value       = aws_db_subnet_group.this.name
}

output "lambda_security_group_id" {
  description = "ID of the Lambda execution security group"
  value       = aws_security_group.lambda.id
}

output "aurora_security_group_id" {
  description = "ID of the Aurora security group"
  value       = aws_security_group.aurora.id
}

output "secretsmanager_vpc_endpoint_id" {
  description = "ID of the Secrets Manager Interface VPC endpoint"
  value       = aws_vpc_endpoint.secretsmanager.id
}

output "dynamodb_vpc_endpoint_id" {
  description = "ID of the DynamoDB Gateway VPC endpoint"
  value       = aws_vpc_endpoint.dynamodb.id
}
