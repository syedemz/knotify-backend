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
