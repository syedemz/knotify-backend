variable "environment" {
  description = "Deployment environment (dev or prod). Used in resource names and ARN patterns."
  type        = string
}

variable "aurora_cluster_resource_id" {
  description = "Resource ID of the Aurora cluster (e.g. cluster-ABCDEF1234567890). Used to scope the db_migrator role's GetSecretValue permission to the Aurora-managed master secret."
  type        = string
}
