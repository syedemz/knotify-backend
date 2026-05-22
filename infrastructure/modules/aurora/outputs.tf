output "cluster_endpoint" {
  description = "Writer endpoint for the Aurora cluster"
  value       = aws_rds_cluster.this.endpoint
}

output "reader_endpoint" {
  description = "Reader endpoint for the Aurora cluster"
  value       = aws_rds_cluster.this.reader_endpoint
}

output "port" {
  description = "Port the Aurora cluster listens on"
  value       = aws_rds_cluster.this.port
}

output "master_user_secret_arn" {
  description = "ARN of the Secrets Manager secret holding the Aurora master credential (Aurora-managed rotation)"
  # master_user_secret is populated after apply; try() gracefully handles the
  # empty list during plan and in terraform test mock runs.
  value     = try(aws_rds_cluster.this.master_user_secret[0].secret_arn, null)
  sensitive = true
}

output "cluster_resource_id" {
  description = "Resource ID of the Aurora cluster (used for IAM auth policies)"
  value       = aws_rds_cluster.this.cluster_resource_id
}

output "database_name" {
  description = "Name of the initial database created in the cluster"
  value       = aws_rds_cluster.this.database_name
}
