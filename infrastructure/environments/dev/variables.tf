variable "environment" {
  description = "Deployment environment name (dev or prod)"
  type        = string
  default     = "dev"
}

# ---------------------------------------------------------------------------
# Aurora module variables — wired from this env root into modules/aurora.
# Dev-safe defaults; prod/prod.tfvars overrides all of these.
# ---------------------------------------------------------------------------

variable "aurora_min_acu" {
  description = "Minimum Aurora Capacity Units for Serverless v2 (dev: 0 → scale-to-zero auto-pause; prod: 1.0)"
  type        = number
  default     = 0
}

variable "aurora_max_acu" {
  description = "Maximum Aurora Capacity Units for Serverless v2 (dev: 2.0, prod: 8.0)"
  type        = number
  default     = 2.0
}

variable "aurora_deletion_protection" {
  description = "Enable deletion protection on the Aurora cluster (false in dev, true in prod)"
  type        = bool
  default     = false
}

variable "aurora_skip_final_snapshot" {
  description = "Skip the final snapshot on cluster deletion (true in dev, false in prod)"
  type        = bool
  default     = true
}

variable "aurora_apply_immediately" {
  description = "Apply Aurora changes immediately (true in dev, false in prod)"
  type        = bool
  default     = true
}

variable "aurora_backup_retention_period" {
  description = "Number of days to retain Aurora automated backups (7 in dev, 30 in prod)"
  type        = number
  default     = 7
}

# ---------------------------------------------------------------------------
# DynamoDB module variables — wired from this env root into modules/dynamodb.
# Dev-safe defaults; prod/prod.tfvars overrides both.
# ---------------------------------------------------------------------------

variable "dynamodb_point_in_time_recovery" {
  description = "Enable PITR on DynamoDB tables (false in dev, true in prod)"
  type        = bool
  default     = false
}

variable "dynamodb_deletion_protection" {
  description = "Enable DynamoDB native deletion protection (false in dev, true in prod)"
  type        = bool
  default     = false
}
