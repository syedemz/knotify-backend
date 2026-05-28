variable "environment" {
  description = "Deployment environment name (e.g. dev, prod)"
  type        = string
}

variable "cluster_identifier" {
  description = "Identifier for the Aurora cluster"
  type        = string
}

variable "database_name" {
  description = "Name of the initial database created in the cluster"
  type        = string
}

variable "master_username" {
  description = "Master username for the Aurora cluster"
  type        = string
  default     = "knotify_admin"
}

variable "aurora_security_group_id" {
  description = "ID of the Aurora security group (from phase-1 networking module output aurora_security_group_id)"
  type        = string
}

variable "db_subnet_group_name" {
  description = "Name of the DB subnet group (from phase-1 networking module output db_subnet_group_name)"
  type        = string
}

# ---------------------------------------------------------------------------
# Serverless v2 scaling — dev defaults are cost-conservative;
# prod caller overrides to min_acu=1.0, max_acu=8.0
# ---------------------------------------------------------------------------

variable "min_acu" {
  description = "Minimum Aurora Capacity Units for Serverless v2 scaling. Default is 0 which enables scale-to-zero auto-pause (Aurora pauses compute after ~5 min idle and resumes on the next connection, ~15-30s cold start). Prod callers override to 1.0 to avoid cold starts."
  type        = number
  default     = 0
}

variable "max_acu" {
  description = "Maximum Aurora Capacity Units for Serverless v2 scaling (dev default: 2.0)"
  type        = number
  default     = 2.0
}

# ---------------------------------------------------------------------------
# Environment-specific safety flags.
# Defaults are dev-safe (no deletion protection, no final snapshot, apply immediately).
# Prod caller sets deletion_protection=true, skip_final_snapshot=false,
# apply_immediately=false, backup_retention_period=30.
# ---------------------------------------------------------------------------

variable "deletion_protection" {
  description = "Whether to enable deletion protection on the Aurora cluster (true in prod, false in dev)"
  type        = bool
  default     = false
}

variable "skip_final_snapshot" {
  description = "Whether to skip the final snapshot on cluster deletion (true in dev, false in prod)"
  type        = bool
  default     = true
}

variable "apply_immediately" {
  description = "Whether to apply changes immediately (true in dev, false in prod)"
  type        = bool
  default     = true
}

variable "backup_retention_period" {
  description = "Number of days to retain automated backups (7 in dev, 30 in prod)"
  type        = number
  default     = 7
}

# ---------------------------------------------------------------------------
# CloudWatch log group retention for the postgresql log export.
# Without an explicit aws_cloudwatch_log_group resource, Aurora auto-creates
# the group with "Never expire" retention. We declare the group ourselves so
# Terraform owns it and retention is explicit per environment.
# ---------------------------------------------------------------------------

variable "postgresql_log_retention_days" {
  description = "CloudWatch Logs retention for the /aws/rds/cluster/<id>/postgresql log group (dev: 1, prod: 7)"
  type        = number
  default     = 1
}
