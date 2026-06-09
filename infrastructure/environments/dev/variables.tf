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

variable "aurora_postgresql_log_retention_days" {
  description = "Retention in days for the Aurora /aws/rds/cluster/<id>/postgresql CloudWatch log group (1 in dev, 7 in prod)"
  type        = number
  default     = 1
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

# ---------------------------------------------------------------------------
# ACM certificate module variables — story 5.2
# Empty string in dev → module produces zero resources (dev path).
# Prod values are set in prod/prod.tfvars per docs/PROD_CUTOVER.md §4b.
# ---------------------------------------------------------------------------

variable "domain_name" {
  description = "Primary domain name for the ACM certificate. Empty string in dev (zero resources). Set in prod.tfvars for the prod cutover."
  type        = string
  default     = ""
}

variable "hosted_zone_id" {
  description = "Route 53 hosted zone ID for DNS validation records. Empty string in dev. Set in prod.tfvars for the prod cutover."
  type        = string
  default     = ""
}

# ---------------------------------------------------------------------------
# API Gateway throttling — story 5.3 (env-level wiring, deferred from 5.1)
# Dev uses relaxed values matching pre-launch reality (brainstorm Mn4).
# ---------------------------------------------------------------------------

variable "api_gateway_throttling_burst_limit" {
  description = "Maximum concurrent requests allowed by the API Gateway default stage. Dev: 10. Prod: 500."
  type        = number
  default     = 10
}

variable "api_gateway_throttling_rate_limit" {
  description = "Maximum steady-state request rate (req/s) for the API Gateway default stage. Dev: 25. Prod: 1000."
  type        = number
  default     = 25
}
