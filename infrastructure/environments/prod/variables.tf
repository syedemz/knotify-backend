variable "environment" {
  description = "Deployment environment name (dev or prod)"
  type        = string
  default     = "prod"
}

# ---------------------------------------------------------------------------
# Aurora module variables — wired from this env root into modules/aurora.
# Prod-safe defaults; dev/dev.tfvars supplies more permissive values.
# ---------------------------------------------------------------------------

variable "aurora_min_acu" {
  description = "Minimum Aurora Capacity Units for Serverless v2 (dev: 0.5, prod: 1.0)"
  type        = number
  default     = 1.0
}

variable "aurora_max_acu" {
  description = "Maximum Aurora Capacity Units for Serverless v2 (dev: 2.0, prod: 8.0)"
  type        = number
  default     = 8.0
}

variable "aurora_deletion_protection" {
  description = "Enable deletion protection on the Aurora cluster (false in dev, true in prod)"
  type        = bool
  default     = true
}

variable "aurora_skip_final_snapshot" {
  description = "Skip the final snapshot on cluster deletion (true in dev, false in prod)"
  type        = bool
  default     = false
}

variable "aurora_apply_immediately" {
  description = "Apply Aurora changes immediately (true in dev, false in prod)"
  type        = bool
  default     = false
}

variable "aurora_backup_retention_period" {
  description = "Number of days to retain Aurora automated backups (7 in dev, 30 in prod)"
  type        = number
  default     = 30
}

variable "aurora_postgresql_log_retention_days" {
  description = "Retention in days for the Aurora /aws/rds/cluster/<id>/postgresql CloudWatch log group (1 in dev, 7 in prod)"
  type        = number
  default     = 7
}

# ---------------------------------------------------------------------------
# DynamoDB module variables — wired from this env root into modules/dynamodb.
# Prod-safe defaults; dev/dev.tfvars supplies more permissive values.
# ---------------------------------------------------------------------------

variable "dynamodb_point_in_time_recovery" {
  description = "Enable PITR on DynamoDB tables (false in dev, true in prod)"
  type        = bool
  default     = true
}

variable "dynamodb_deletion_protection" {
  description = "Enable DynamoDB native deletion protection (false in dev, true in prod)"
  type        = bool
  default     = true
}

# ---------------------------------------------------------------------------
# Cognito module variables — story 4.5
# advanced_security_mode is set to AUDIT (the minimum required by the V2
# PreTokenGeneration trigger — brainstorm B2). Flip to ENFORCED in the
# phase 11 hardening pass (architecture.md §13 #1).
# ---------------------------------------------------------------------------

variable "advanced_security_mode" {
  description = "Cognito Advanced Security Mode. Valid values: OFF | AUDIT | ENFORCED. Default AUDIT is the minimum required by the V2 PreTokenGeneration trigger (story 4.4 / brainstorm B2). Upgrade to ENFORCED in phase 11."
  type        = string
  default     = "AUDIT"
}
